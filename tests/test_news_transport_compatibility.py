"""Synthetic wire bytes through real Requests decoding; never provider replays."""
import base64
import gzip
import io
import json
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from urllib3.response import HTTPResponse

from scripts import diagnose_public_news as cli
from tradingagents import news_diagnostic as diagnostic

START, END = "2026-10-31", "2026-11-07"
CALLBACK = "jQuery35101792940631092459_1764599530165"
# Generated locally from _body() using zstandard 0.25.0, with an independent
# decompression equality check. Tests require no optional compression package.
ZSTD_BODY = base64.b64decode(
    "KLUv/SDxpQUAYsslIEBr6/as/WzHs002Zam3Gz0z8+NIOojMSA0SDnmWYQAgAU2T0LzFwvjoH6eDx3mae1e9d7+blrkpTBHqcvkHt3Ci4uZ0yYAKO8y9M3xsGMBkIHHNoSJ0d+0uxCdtgjmh0CJd6p80ZRCWtkew8AN/zL0UmJAps/TfMl0qA5AU75G6E6IUrNAJQb18qEUatYKX09GQvhQ0BAkAPgNUvMiIT2e9BAnFdYIUeMsYTP+WQJkB"
)


def _value():
    return {"result": {"cmsArticleWebOld": [{
        "date": "2026-11-05 12:00:00", "mediaName": "Synthetic compression fixture",
        "code": "202611050000", "title": "600919 synthetic news",
        "content": "Synthetic public content",
    }]}}


def _body(value=None):
    return (CALLBACK + "(" + json.dumps(_value() if value is None else value,
                                       ensure_ascii=False, separators=(",", ":")) + ")").encode()


@pytest.fixture
def memory_transport(monkeypatch):
    """Only replace the adapter's network send, retaining Session and raw.stream."""
    calls, responses, policies = [], [], []
    selected = {"encoding": "gzip", "body": _body(), "status": 200, "negotiate": False}
    original_send = requests.Session.send

    def observe_send(session, request, **kwargs):
        policies.append({"trust_env": session.trust_env, "session_proxies_empty": session.proxies == {},
                         "verify": kwargs.get("verify"), "timeout": kwargs.get("timeout"),
                         "allow_redirects": kwargs.get("allow_redirects"),
                         "request_proxies_empty": kwargs.get("proxies") == {}})
        return original_send(session, request, **kwargs)

    def send(adapter, request, **kwargs):
        if calls:
            raise AssertionError("synthetic transport received a second attempt")
        assert request.method == "GET" and urlsplit(request.url).scheme == "https"
        query = parse_qs(urlsplit(request.url).query)
        assert query["cb"] == [CALLBACK]
        inner = json.loads(query["param"][0])
        assert inner["keyword"] == "600919"
        assert inner["param"]["cmsArticleWebOld"]["pageIndex"] == 1
        assert inner["param"]["cmsArticleWebOld"]["pageSize"] == 10
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        advertised = [item.strip() for item in request.headers["Accept-Encoding"].split(",")]
        encoding = selected["encoding"]
        if selected["negotiate"]:
            encoding = "zstd" if "zstd" in advertised else "gzip" if "gzip" in advertised else None
        body = selected["body"]
        if "wire" in selected:
            wire = selected["wire"]
        elif encoding in {"gzip", "x-gzip", "GZip"}:
            wire = gzip.compress(body, mtime=0)
        elif encoding == "deflate":
            wire = zlib.compress(body)
        elif encoding == "zstd":
            assert body == _body()
            wire = ZSTD_BODY
        else:
            wire = body
        headers = {"Content-Encoding": encoding} if encoding else {}
        if selected["status"] == 302:
            headers["Location"] = "https://synthetic.invalid/redirect"
        raw = HTTPResponse(body=io.BytesIO(wire), status=selected["status"], headers=headers,
                           preload_content=False, decode_content=False)
        response = adapter.build_response(request, raw)
        closed = []
        original_close = response.close

        def close():
            closed.append(True)
            return original_close()

        response.close = close
        responses.append((response, closed))
        calls.append({"advertised": advertised, "encoding": encoding})
        return response

    def forbidden_sdk(*args, **kwargs):
        raise AssertionError("obsolete SDK path is outside the synthetic transport")

    monkeypatch.setattr(requests.Session, "send", observe_send)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(diagnostic.akshare_data.ak, "stock_news_em", forbidden_sdk)
    yield selected, calls, responses, policies
    for response, _ in responses:
        response.close()


def _capture(tmp_path):
    return cli._capture_direct(tmp_path, start=START, end=END, ticker="600919.SH")


def _assert_one_direct_attempt(result, transport):
    _, calls, _, policies = transport
    assert result["request_count"] == len(calls) == len(policies) == 1
    assert policies == [{"trust_env": False, "session_proxies_empty": True, "verify": True,
                         "timeout": 15, "allow_redirects": False, "request_proxies_empty": True}]


def test_advertised_encodings_are_supported_by_actual_decoder(tmp_path, memory_transport):
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["capture_passed"]
    advertised = memory_transport[1][0]["advertised"]
    assert advertised and set(advertised) <= set(HTTPResponse.CONTENT_DECODERS)


def test_negotiated_compression_preserves_production_news(tmp_path, memory_transport):
    memory_transport[0]["negotiate"] = True
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["capture_passed"], result["checks"]["decode"]["reason"]
    assert (tmp_path / diagnostic.BODY_NAME).read_bytes() == _body()
    assert result["selection"]["accepted_count"] == 1
    assert result["selection"]["coverage"] == "unknown"


@pytest.mark.parametrize("encoding", [None, "gzip", "x-gzip", "GZip", "deflate", "raw-deflate"])
def test_supported_wire_decodes_before_retention_and_admission(tmp_path, memory_transport, encoding):
    selected, _, responses, _ = memory_transport
    selected["encoding"] = encoding
    if encoding == "raw-deflate":
        selected.update(encoding="deflate", wire=zlib.compress(_body(), wbits=-zlib.MAX_WBITS))
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["capture_passed"] and result["body_saved"]
    assert result["callback_matches"] is True
    assert (tmp_path / diagnostic.BODY_NAME).read_bytes() == _body()
    assert result["response_bytes"] == len(_body())
    assert responses[0][1] == [True]
    assert result["selection"]["coverage"] == "unknown"


def test_unrequested_zstd_is_not_admitted_without_a_decoder(tmp_path, memory_transport):
    if "zstd" in HTTPResponse.CONTENT_DECODERS:
        pytest.skip("this negative checks the environment without a zstd decoder")
    memory_transport[0]["encoding"] = "zstd"
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["checks"]["decode"]["status"] == "failed"
    assert not result["capture_passed"] and not result["body_saved"]
    assert not result["projection_saved"] and "artifact_sha256" not in result
    assert memory_transport[2][0][1] == [True]


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_corrupt_compression_stays_failed_without_retry(tmp_path, memory_transport, encoding):
    memory_transport[0].update(encoding=encoding, wire=b"not-compressed-synthetic-bytes")
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["checks"]["network"]["reason"] == "transport_exception"
    assert not result["capture_passed"] and not result["body_saved"]
    assert not result["projection_saved"] and "artifact_sha256" not in result


@pytest.mark.parametrize("status", [302, 429, 500])
def test_http_failure_does_not_redirect_retry_or_publish(tmp_path, memory_transport, status):
    memory_transport[0]["status"] = status
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["checks"]["http"]["reason"] == "http_failure_or_redirect"
    assert not result["capture_passed"] and not result["body_saved"]
    # Requests also closes a redirect while preparing its unused next request.
    # Idempotent additional closure is valid; there must still be only one send.
    assert memory_transport[2][0][1]


@pytest.mark.parametrize("problem,reason", [
    ("status", "business_status_unverified"), ("missing", "schema_invalid"),
    ("null", "schema_invalid"), ("date", "no_usable_articles"),
    ("stock", "no_usable_articles"),
])
def test_compression_does_not_relax_business_or_evidence_checks(tmp_path, memory_transport, problem, reason):
    value = _value()
    if problem == "status":
        value["code"] = 0
    elif problem == "missing":
        value["result"] = {}
    elif problem == "null":
        value["result"]["cmsArticleWebOld"] = None
    else:
        row = value["result"]["cmsArticleWebOld"][0]
        if problem == "date":
            row["date"] = "2026-11-08 00:00:00"
        else:
            row["title"] = "600895 unrelated synthetic news"
    memory_transport[0]["body"] = _body(value)
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    if problem in {"missing", "null"}:
        assert result["checks"]["retention"]["reason"] == (
            "articles_missing" if problem == "missing" else "articles_null")
        assert result["checks"]["parser"]["status"] == "not_started"
    else:
        assert result["checks"]["parser"]["reason"] == reason
    assert not result.get("artifact_sha256")
    assert not result["capture_passed"] and not result["projection_saved"]
    assert result["selection"]["coverage"] == "unknown"
    assert memory_transport[2][0][1] == [True]


def test_decoded_size_limit_is_preserved(tmp_path, memory_transport):
    memory_transport[0]["body"] = b"x" * (diagnostic.LIMIT + 1)
    result = _capture(tmp_path)
    _assert_one_direct_attempt(result, memory_transport)
    assert result["checks"]["decode"]["reason"] == "response_limit_or_type"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert not result.get("artifact_sha256")
    assert memory_transport[2][0][1] == [True]
    assert not result["capture_passed"] and not result["body_saved"]
