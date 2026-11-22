"""Synthetic single-query-delta and bounded cache metadata; no live traffic."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from tests.test_news_metadata_only import MARKER, assert_no_content, no_real_send  # noqa: F401


def fake_send(sent, closed):
    def send(session, request, **kwargs):
        sent.append((request.method, request.url, request.body, dict(request.headers), kwargs))
        assert not session.trust_env and not session.auth and not session.cookies
        return SimpleNamespace(status_code=200, content=b'jQuery35101234567890_1760000000000({"code":0,"msg":"OK","result":{"passportWeb":[]}})',
            headers={}, url=request.url, close=lambda: closed.append(True))
    return send


def test_fresh_query_changes_only_one_encoded_value(tmp_path, monkeypatch):
    import diagnose_public_news as cli
    sent, closed, clocks = [], [], []
    monkeypatch.setattr(requests.Session, "send", fake_send(sent, closed))
    def now():
        clocks.append(True)
        return 1794733200123000000
    monkeypatch.setattr(cli.time, "time_ns", now)
    results = []
    for variant in ("production", "fresh-cachebuster-v1"):
        root = tmp_path / variant
        root.mkdir()
        result = cli._capture_direct(root, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
            metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True, request_variant=variant)
        results.append(result)
        assert result["request_count"] == result["transport_metadata"]["send_attempts"] == 1
        assert result["checks"]["retention"]["reason"] == "articles_missing"
        assert result["checks"]["parser"]["status"] == "not_started"
        assert result["transport_metadata"]["session_close"] == "closed"
        assert_no_content(root, result, [])
    baseline, changed = sent
    assert changed[0] == baseline[0] == "GET" and changed[2:] == baseline[2:]
    assert changed[1] == baseline[1].removesuffix("1764599530176") + "1794733200123"
    a, b = (parse_qsl(urlsplit(row[1]).query, keep_blank_values=True) for row in sent)
    assert a[:-1] == b[:-1] and b[-1] == ("_", "1794733200123")
    assert [x[0] for x in b] == ["cb", "param", "_"]
    assert "+" not in changed[1] and changed[1].count("%20") == baseline[1].count("%20")
    assert clocks == [True] and closed == [True, True]
    assert "cache_response" not in results[0]["transport_metadata"]
    metadata = results[1]["transport_metadata"]
    assert metadata["decoded_query_equal"] is False and metadata["request_header_delta"] == []
    assert metadata["request_query_delta"] == [{"field": "_", "before": "1764599530176", "after": "1794733200123"}]
    assert metadata["request_headers"]["pragma"] == "no-cache"
    assert results[0]["request_fingerprint"] == results[1]["request_fingerprint"]


def test_cache_header_view_known_fields_and_presence_only():
    import diagnose_public_news as cli
    class Headers(dict):
        def get(self, name, default=None):
            assert name not in {"ETag", "Via", "Vary", "Cache-Status", "X-Cache", "CF-Cache-Status", "Cookie", "Set-Cookie", "Authorization"}
            return super().get(name, default)
    headers = Headers({"Date": "Sun, 15 Nov 2026 09:00:00 GMT", "Age": "123",
        "Expires": "Sun, 15 Nov 2026 10:00:00 GMT", "Last-Modified": "Sat, 14 Nov 2026 09:00:00 GMT",
        "Cache-Control": "public, max-age=60, must-revalidate", "ETag": MARKER,
        "Via": MARKER, "Vary": MARKER, "Set-Cookie": MARKER, "Authorization": MARKER})
    result = cli._cache_response_metadata(headers)
    assert result["date"] == {"status": "standard_http_date", "value": headers["Date"]}
    assert result["age"] == {"status": "delta_seconds", "seconds": 123}
    assert result["cache_control"]["directives"] == {"public": True, "max-age": 60, "must-revalidate": True}
    assert result["presence_only"] == {"etag": True, "vary": True, "via": True, "cache-status": False, "x-cache": False, "cf-cache-status": False}
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize("name,value", [
    ("Date", MARKER), ("Date", "Mon, 15 Nov 2026 09:00:00 GMT"),
    ("Date", "Sun, 31 Feb 2026 09:00:00 GMT"), ("Date", "Sun, 15 Nov 2026 09:00:00 UTC"),
    ("Age", "-1"), ("Age", "9" * 100), ("Age", MARKER),
    ("Cache-Control", 'private="' + MARKER + '"'), ("Cache-Control", "max-age=1,max-age=2"),
    ("Cache-Control", "no-cache,no-cache"), ("Cache-Control", "public," + MARKER),
    ("Cache-Control", "public,private"),
    ("Cache-Control", MARKER * 20), ("Cache-Control", "max-age=9999999999"),
])
def test_cache_header_unknown_malformed_or_duplicate_hidden(name, value):
    import diagnose_public_news as cli
    result = cli._cache_response_metadata({name: value})
    key = "cache_control" if name == "Cache-Control" else name.lower()
    assert result[key]["status"] == "hidden"
    assert MARKER not in json.dumps(result)
    if name == "Cache-Control":
        assert result[key]["directives"] == {}


@pytest.mark.parametrize("value", [0, 1764599530176000000, 9999999999999999999])
def test_invalid_or_unchanged_fresh_value_sends_zero(tmp_path, monkeypatch, value):
    import diagnose_public_news as cli
    monkeypatch.setattr(cli.time, "time_ns", lambda: value)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True,
        request_variant="fresh-cachebuster-v1")
    assert result["transport_metadata"]["send_attempts"] == 0
    assert "http_status" not in result


@pytest.mark.parametrize("fatal", [False, True])
def test_cache_observer_failure_preserves_first_close_and_no_body(tmp_path, monkeypatch, fatal):
    import diagnose_public_news as cli
    closed = []
    failure = KeyboardInterrupt(MARKER) if fatal else ValueError(MARKER)
    def observe(headers):
        raise failure
    def send(session, request, **kwargs):
        def close():
            closed.append(True)
            raise OSError(MARKER)
        return SimpleNamespace(status_code=200, content=b'jQuery35101234567890_1760000000000({"result":{}})',
            headers={}, url=request.url, close=close)
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(cli, "_cache_response_metadata", observe)
    monkeypatch.setattr(cli.time, "time_ns", lambda: 1794733200123000000)
    kwargs = dict(ticker="600919.SH", start="2026-11-06", end="2026-11-13", metadata_only=True,
        percent_encoded_spaces=True, public_structure_metadata=True, request_variant="fresh-cachebuster-v1")
    if fatal:
        with pytest.raises(KeyboardInterrupt) as caught:
            cli._capture_direct(tmp_path, **kwargs)
        assert caught.value is failure
    else:
        result = cli._capture_direct(tmp_path, **kwargs)
        assert result["transport_metadata"]["cache_response"]["status"] == "unavailable"
        assert result["checks"]["retention"]["reason"] == "articles_missing"
        assert_no_content(tmp_path, result, [])
    assert closed == [True]


def test_fresh_variant_requires_opt_in_before_session(tmp_path, monkeypatch):
    import diagnose_public_news as cli
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("unexpected session"))
    with pytest.raises(ValueError):
        cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
            metadata_only=True, percent_encoded_spaces=True, request_variant="fresh-cachebuster-v1")
