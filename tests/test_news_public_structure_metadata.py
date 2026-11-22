"""Synthetic opt-in public metadata; no retained real callback or article body."""
import json
from types import SimpleNamespace

import pytest
import requests

from tests.test_news_metadata_only import MARKER, assert_no_content, no_real_send  # noqa: F401
from tradingagents import news_diagnostic as diagnostic

CALLBACK = "jQuery35101234567890_1760000000000"


def run(tmp_path, monkeypatch, *, callback=CALLBACK, fields=None, enabled=True):
    import diagnose_public_news as cli
    sent, closed, bodies = [], [], []
    def send(session, request, **kwargs):
        sent.append(request)
        assert not session.trust_env and not session.auth and not session.cookies
        assert request.headers["accept"] == "*/*"
        assert request.headers["referer"] == "https://so.eastmoney.com/news/s?keyword=600919"
        assert kwargs == {"allow_redirects": False, "timeout": 15, "proxies": {}, "verify": True}
        body = (callback + "(" + json.dumps({"code": 0, "msg": "OK", "bizMsg": "",
            "result": {} if fields is None else fields}) + ")").encode()
        bodies.append(body)
        return SimpleNamespace(status_code=200, content=body, headers={}, url=request.url,
            close=lambda: closed.append(True))
    monkeypatch.setattr(requests.Session, "send", send)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=enabled)
    assert len(sent) == len(closed) == result["request_count"] == 1
    assert result["transport_metadata"]["session_close"] == "closed"
    assert result["metadata_passed"] is False and result["checks"]["parser"]["status"] == "not_started"
    assert_no_content(tmp_path, result, bodies)
    return result


def test_public_names_opt_in_only_and_bound_request(tmp_path, monkeypatch):
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    result = run(ordinary, monkeypatch, enabled=False)
    assert "public_structure" not in result
    detailed = tmp_path / "detailed"
    detailed.mkdir()
    result = run(detailed, monkeypatch, fields={"passportWeb": [{MARKER: MARKER}], "news": []})
    view = result["public_structure"]
    assert view["contract"] == "news-public-structure-v1"
    assert view["public_request_binding"] == "verified"
    assert view["callback"] == {"status": "public_jquery_identifier", "name": CALLBACK}
    assert view["result"]["fields"] == [
        {"name": "passportWeb", "type": "array", "emptiness": "nonempty"},
        {"name": "news", "type": "array", "emptiness": "empty"},
    ]
    assert view["result"]["inspection_complete"] is True
    assert result["callback_matches"] is False  # New names never repair the binding.


@pytest.mark.parametrize("callback", [MARKER, "token_" + MARKER, "Cookie", "foo.bar", "https://example.invalid",
    "a();alert(1)", "jQuery" + "1" * 50 + "_1760000000000", "jQuery123456_0000000000000"])
def test_unsafe_callback_not_retained(tmp_path, monkeypatch, callback):
    result = run(tmp_path, monkeypatch, callback=callback)
    view = result["public_structure"]
    assert view["callback"]["name"] is None
    assert view["callback"]["status"] in {"hidden_format_or_semantics", "unavailable"}
    assert callback not in json.dumps(view)


def test_public_keys_sensitive_unknown_scalar_and_nested_values_hidden(tmp_path, monkeypatch):
    fields = {"token": MARKER, "Cookie": {MARKER: MARKER}, "authorization": MARKER,
        "credential": MARKER, "sessionId": MARKER, "apiKey": MARKER,
        "QxNtAvRbYpLcWzKs": [], MARKER: [], "x" * 90: [], "data/url": [],
        "news": "Bearer " + MARKER, "items": [], "data": {"token": MARKER}}
    view = run(tmp_path, monkeypatch, fields=fields)["public_structure"]
    assert view["result"]["fields"] == [
        {"name": "items", "type": "array", "emptiness": "empty"},
        {"name": "data", "type": "object", "emptiness": "nonempty"},
    ]
    assert view["result"]["hidden_counts"] == {
        "sensitive_name": 6, "name_not_reviewed": 2, "name_format": 2,
        "unverified_scalar_value": 1,
    }
    assert view["result"]["inspection_complete"] is True
    assert "QxNtAvRbYpLcWzKs" not in json.dumps(view)


def test_public_key_limit_hides_all_without_sampling_names(tmp_path, monkeypatch):
    view = run(tmp_path, monkeypatch, fields={MARKER + str(i): [] for i in range(33)})["public_structure"]
    assert view["result"]["status"] == "field_limit"
    assert view["result"]["fields"] == []
    assert view["result"]["inspection_complete"] is False
    assert view["result"]["field_count"] == 32 and view["result"]["count_capped"] is True


@pytest.mark.parametrize("metadata,percent,ticker,variant", [
    (False, True, "600919.SH", "production"), (True, False, "600919.SH", "production"),
    (True, True, "600895.SH", "production"), (True, True, "600919.SH", "omit-referer-v1"),
])
def test_public_metadata_scope_rejected_before_session(tmp_path, monkeypatch, metadata, percent, ticker, variant):
    import diagnose_public_news as cli
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("unexpected session"))
    with pytest.raises(ValueError):
        cli._capture_direct(tmp_path, ticker=ticker, start="2026-11-06", end="2026-11-13",
            metadata_only=metadata, percent_encoded_spaces=percent, request_variant=variant,
            public_structure_metadata=True)


def test_public_view_requires_positive_prepared_binding(tmp_path):
    body = (CALLBACK + '({"result":{"passportWeb":[]}})').encode()
    closed = []
    result = diagnostic._run(tmp_path, get=lambda *a, **k: SimpleNamespace(status_code=200,
        content=body, close=lambda: closed.append(True)), ticker="600919.SH", transport="direct",
        start="2026-11-06", end="2026-11-13", retain=True, metadata_only=True,
        public_metadata_binding=lambda: False)
    assert result["public_structure"]["public_request_binding"] == "unverified"
    assert CALLBACK not in json.dumps(result["public_structure"])
    assert closed == [True]


@pytest.mark.parametrize("fatal", [False, True])
def test_public_view_failure_no_leak_preserves_close(tmp_path, monkeypatch, fatal):
    import diagnose_public_news as cli
    failure = KeyboardInterrupt(MARKER) if fatal else ValueError(MARKER)
    closed = []
    def failed(*args):
        raise failure
    monkeypatch.setattr(diagnostic, "_public_structure_metadata", failed)
    def send(session, request, **kwargs):
        def close():
            closed.append(True)
            raise OSError(MARKER)
        return SimpleNamespace(status_code=200, content=(CALLBACK + '({"code":0,"result":{}})').encode(),
            headers={}, url=request.url, close=close)
    monkeypatch.setattr(requests.Session, "send", send)
    if fatal:
        with pytest.raises(KeyboardInterrupt) as caught:
            cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
                metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True)
        assert caught.value is failure
    else:
        result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
            metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True)
        assert result["public_structure"]["callback"]["name"] is None
        assert result["stage"] == "retention" and result["checks"]["retention"]["reason"] == "articles_missing"
        assert MARKER not in json.dumps(result)
    assert closed == [True]


@pytest.mark.parametrize("header", ["Cookie", "Authorization", "Proxy-Authorization"])
def test_public_view_cannot_follow_authenticated_prepared_request(tmp_path, monkeypatch, header):
    import diagnose_public_news as cli
    prepare = requests.Session.prepare_request
    def tainted(session, request):
        prepared = prepare(session, request)
        prepared.headers[header] = MARKER
        return prepared
    monkeypatch.setattr(requests.Session, "prepare_request", tainted)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True)
    assert result["transport_metadata"]["send_attempts"] == 0
    assert "public_structure" not in result and "http_status" not in result
    assert MARKER not in json.dumps(result)


def test_public_view_cli_opt_in_in_reservation_and_result(tmp_path, monkeypatch, capsys):
    import diagnose_public_news as cli
    import sys
    output = tmp_path / "output"
    def send(session, request, **kwargs):
        return SimpleNamespace(status_code=200, content=(CALLBACK + '({"result":{"passportWeb":[]}})').encode(),
            headers={}, url=request.url, close=lambda: None)
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(cli, "SOURCE", tmp_path / "synthetic-source")
    monkeypatch.setattr(cli, "install", lambda *a, **k: ({}, []))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True))
    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(output),
        "--start", "2026-11-06", "--end", "2026-11-13", "--ticker", "600919.SH", "--transport", "direct",
        "--authorize-one-public-get-metadata-only", "--percent-encoded-spaces", "--public-structure-metadata"])
    assert cli.main() == 1
    result = json.loads((output / "result.json").read_text())
    reservation = json.loads((output / "reservation.json").read_text())
    assert reservation["public_structure_metadata"] is True
    assert result["transport_metadata"]["public_structure_metadata"] is True
    assert result["public_structure"]["callback"]["name"] == CALLBACK
    assert MARKER not in capsys.readouterr().out
    assert_no_content(output, result, [])


def test_public_view_preserves_success_stock_future_and_unordered_filtering(tmp_path, monkeypatch):
    import diagnose_public_news as cli
    from urllib.parse import parse_qsl, urlsplit
    from tests.test_news_diagnostic import synthetic_get
    closed, bodies = [], []
    def mutate(value):
        rows = value["result"]["cmsArticleWebOld"]
        rows[1]["title"] = "600895 synthetic unrelated"
        for row in rows:
            row["content"] = MARKER
    def send(session, request, **kwargs):
        response = synthetic_get(symbol="600919", dates=["2026-11-14", "2026-11-13", "2026-11-12"],
                                 mutate=mutate, closed=closed)(request.url,
                                     params=dict(parse_qsl(urlsplit(request.url).query)))
        response.headers, response.url = {}, request.url
        bodies.append(response.content)
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, public_structure_metadata=True)
    assert result["metadata_passed"] is True and result["callback_matches"] is True
    assert result["selection"]["accepted_count"] == 1
    assert result["selection"]["excluded_counts"] == {"malformed": 0, "undated": 0, "outside_window": 1, "wrong_stock": 1}
    assert result["public_structure"]["result"]["fields"] == [
        {"name": "cmsArticleWebOld", "type": "array", "emptiness": "nonempty"}]
    assert closed == [True]
    assert_no_content(tmp_path, result, bodies)
