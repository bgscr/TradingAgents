"""Synthetic live-transport seams only; no original browser or network bodies."""
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from tests.test_news_diagnostic import END, START, synthetic_get
from tradingagents import news_diagnostic as diagnostic, run_diagnostics

MARKER = "SYNTHETIC_UNRETAINED_CONTENT_7341"
EDGE_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")


@pytest.fixture(autouse=True)
def no_real_send(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected real transport")
    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(diagnostic.akshare_data.ak, "stock_news_em", forbidden)


def capture(root, get):
    return diagnostic.capture(root, get=get, ticker="600895.SH", transport="direct",
                              start=START, end=END, metadata_only=True)


def assert_no_content(root, result, bodies):
    serialized = json.dumps(result)
    assert MARKER not in serialized
    assert not result["body_saved"] and not result["projection_saved"]
    assert not result["capture_passed"] and not result["acceptance_passed"]
    assert result["byte_replay_available"] is False
    for key in ("response_sha256", "saved_body_sha256", "structure", "adapter_frame_sha256", "artifact_sha256"):
        assert key not in result
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert MARKER.encode() not in data
            for body in bodies:
                assert body not in data
                assert hashlib.sha256(body).hexdigest().encode() not in data
    assert not (root / diagnostic.BODY_NAME).exists()


@pytest.mark.parametrize("scenario,reason", [
    ("valid", None), ("missing", "articles_missing"),
    ("extra", "unknown_envelope_fields"), ("sensitive", "sensitive_article_text"),
    ("business", "business_status_unverified"), ("future", "no_usable_articles"),
    ("wrong_stock", "no_usable_articles"), ("invalid_json", "json_invalid"),
    ("oversize", "response_limit_or_type"), ("http", "http_failure_or_redirect"),
])
def test_live_metadata_keeps_admission_and_never_retains_content(tmp_path, scenario, reason):
    calls, closes, bodies = [], [], []
    def get(url, *, params, **kwargs):
        calls.append(True)
        def mutate(value):
            rows = value["result"]["cmsArticleWebOld"]
            for row in rows:
                row["content"] = MARKER
            if scenario == "missing":
                value["result"] = {MARKER: MARKER}
            elif scenario == "extra":
                value[MARKER] = {MARKER: [MARKER]}
            elif scenario == "sensitive":
                rows[0]["content"] = "Authorization: " + MARKER
            elif scenario == "business":
                value["code"] = 0
            elif scenario == "future":
                rows[0]["date"] = "2026-10-13"
            elif scenario == "wrong_stock":
                rows[0]["title"] = "600919 synthetic unrelated"
        response = synthetic_get(dates=["2026-10-10"], mutate=mutate, closed=closes)(url, params=params)
        if scenario == "invalid_json":
            response.content = b'cb({"synthetic":)' + MARKER.encode() + b')'
        elif scenario == "oversize":
            response.content = MARKER.encode() + b" " * diagnostic.LIMIT
        elif scenario == "http":
            response.status_code = 403
        bodies.append(response.content)
        return response
    result = capture(tmp_path, get)
    assert result["mode"] == "live_metadata_only" and result["contract"] == diagnostic.METADATA_CONTRACT
    assert result["request_count"] == 1 and calls == closes == [True]
    assert result["metadata_passed"] is (scenario == "valid")
    if reason:
        assert reason in {check.get("reason") for check in result["checks"].values()}
    else:
        assert result["selection"]["accepted_count"] == 1
        assert result["checks"]["network"]["reason"] == "response_received"
    assert_no_content(tmp_path, result, bodies)
    assert diagnostic.replay(tmp_path, ticker="600895.SH", transport="direct", start=START, end=END)["checks"]["replay"]["reason"] == "unsupported_capture_contract"


@pytest.mark.parametrize("classifier_failure", [False, True])
def test_rejected_metadata_closes_and_keeps_first_error(tmp_path, monkeypatch, classifier_failure):
    calls, closes = [], []
    if classifier_failure:
        def bad_shape(*args, **kwargs):
            raise ValueError(MARKER)
        monkeypatch.setattr(run_diagnostics, "record_news_response_shape", bad_shape)
    def forbidden_parse(*args, **kwargs):
        raise AssertionError("rejected body parsed")
    monkeypatch.setattr(diagnostic.eastmoney_news, "parse_news_page", forbidden_parse)
    def get(*args, **kwargs):
        calls.append(True)
        def close():
            closes.append(True)
            raise OSError(MARKER)
        return SimpleNamespace(status_code=200, content=b'other({"code":0,"result":{}})', close=close)
    result = capture(tmp_path, get)
    assert result["stage"] == "retention" and result["callback_matches"] is False
    assert result["checks"]["parser"]["status"] == "not_started"
    errors = [event["payload"] for event in result["diagnostics"] if event["kind"] == "news_exception"]
    assert errors[0]["role"] == "primary" and errors[0]["stage"] == "retention"
    assert any(e["role"] == "cleanup" and e["stage"] == "response_close" for e in errors)
    assert calls == closes == [True] and MARKER not in json.dumps(result)


def test_metadata_persistence_failure_is_terminal_and_closes(tmp_path, monkeypatch):
    closes = []
    failure = run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")
    def failed(*args, **kwargs):
        raise failure
    monkeypatch.setattr(run_diagnostics, "record_news_response_shape", failed)
    with pytest.raises(run_diagnostics.DiagnosticPersistenceError) as caught:
        capture(tmp_path, synthetic_get(closed=closes))
    assert caught.value is failure and closes == [True]
    assert not (tmp_path / diagnostic.BODY_NAME).exists()


def test_metadata_network_failure_is_not_retried_or_reclassified(tmp_path):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise requests.exceptions.ReadTimeout(MARKER)
    result = capture(tmp_path, fail)
    assert result["stage"] == "transport" and calls == [True]
    assert result["checks"]["network"]["exception_type"] == "ReadTimeout"
    assert MARKER not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("callback_matches", [False, True])
def test_metadata_preserves_observed_profile_callback_binding(tmp_path, callback_matches):
    from tests.test_news_observed_envelope import envelope, wire
    closes = []
    def get(url, *, params, **kwargs):
        value = envelope()
        value["result"]["cmsArticleWebOld"][0]["content"] = MARKER
        return SimpleNamespace(status_code=200,
            content=wire(value, params["cb"] if callback_matches else "different"),
            close=lambda: closes.append(True))
    result = diagnostic.capture(tmp_path, get=get, ticker="600919.SH", transport="direct",
        start="2026-10-31", end="2026-11-07", metadata_only=True)
    assert result["metadata_passed"] is callback_matches and closes == [True]
    assert result["callback_matches"] is callback_matches
    if not callback_matches:
        assert result["checks"]["retention"]["status"] == "failed"
        assert result["checks"]["parser"]["status"] == "not_started"
    assert_no_content(tmp_path, result, [])


@pytest.mark.parametrize("success", [False, True])
def test_metadata_cli_persists_only_receipts_and_reserves_one_live_get(tmp_path, monkeypatch, capsys, success):
    import diagnose_public_news as cli
    import sys
    output = tmp_path / "output"
    installs, closes, bodies = [], [], []
    def install(*args, **kwargs):
        installs.append(kwargs)
        return {}, []
    def send(session, request, **kwargs):
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(mutate=lambda value: value["result"]["cmsArticleWebOld"][1].update(content=MARKER),
                                 closed=closes)(None, params=params)
        if not success:
            response.content = b'other({"code":0,"result":{}})'
        bodies.append(response.content)
        response.headers, response.url = {}, request.url
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(cli, "SOURCE", tmp_path / "synthetic-source")
    monkeypatch.setattr(cli, "install", install)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True))
    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(output),
        "--start", START, "--end", END, "--ticker", "600895.SH", "--transport", "direct",
        "--authorize-one-public-get-metadata-only", "--percent-encoded-spaces"])
    assert cli.main() == (0 if success else 1)
    result = json.loads((output / "result.json").read_text())
    reservation = json.loads((output / "reservation.json").read_text())
    summary = json.loads(capsys.readouterr().out)
    assert installs == [{"offline": False}] and closes == [True]
    assert reservation["live"] and reservation["physical_request_limit"] == 1
    assert result["transport_metadata"]["send_attempts"] == 1
    assert summary["metadata_passed"] is success and result["acceptance"]["passed"] is success
    assert MARKER not in json.dumps(summary)
    assert_no_content(output, result, bodies)


@pytest.mark.parametrize("percent", [False, True])
def test_real_prepared_request_only_changes_space_encoding(tmp_path, monkeypatch, percent):
    import diagnose_public_news as cli
    calls, closes = [], []
    def send(session, request, **kwargs):
        calls.append(request.url)
        assert kwargs == {"allow_redirects": False, "timeout": 15, "proxies": {}, "verify": True}
        assert session.trust_env is False and session.auth is None and not session.cookies
        assert request.method == "GET" and request.body is None
        assert not {name.lower() for name in request.headers} & {"authorization", "cookie", "proxy-authorization"}
        assert request.headers["Accept-Encoding"] == "gzip, deflate"
        assert "Chrome/142." in request.headers["User-Agent"] and "Edg/" not in request.headers["User-Agent"]
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(symbol="600919", dates=["2026-11-13"], closed=closes)(None, params=params)
        response.headers = {"Content-Type": "text/javascript;charset=UTF-8", "Content-Encoding": "gzip",
                            "Transfer-Encoding": "chunked", "Set-Cookie": MARKER}
        response.url = request.url
        response.raw = SimpleNamespace(version=11)
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
                                 metadata_only=True, percent_encoded_spaces=percent)
    metadata = result["transport_metadata"]
    assert result["metadata_passed"] and metadata["send_attempts"] == 1 and closes == [True]
    assert len(calls) == 1 and metadata["decoded_query_equal"]
    assert metadata["byte_url_equal"] is (not percent)
    assert calls[0].count("%20") == (25 if percent else 0)
    assert calls[0].count("+") == (0 if percent else 25)
    if percent:
        assert len(calls[0]) == 611
        assert hashlib.sha256(calls[0].encode("ascii")).hexdigest() == "c665f7667ded81f7cb715cc9f10802cbcafb38c762f3c6069085a244b06a8c8d"
    assert metadata["response"]["http_protocol"] == "HTTP/1.1"
    assert MARKER not in json.dumps(result)
    assert cli._acceptance(result, "metadata-only")["content_source_passed"] is False


def test_response_negotiation_metadata_never_echoes_untrusted_headers():
    import diagnose_public_news as cli
    response = SimpleNamespace(headers={key: MARKER for key in
        ("Content-Type", "Content-Encoding", "Transfer-Encoding", "Content-Length", "Set-Cookie")},
        raw=SimpleNamespace(version=MARKER))
    result = cli._response_metadata(response)
    assert result == {"content_type": "other", "content_encoding": "other", "transfer_encoding": "other",
                      "content_length_header": None, "http_protocol": None, "cache_source": "unknown"}


@pytest.mark.parametrize("success", [False, True])
def test_metadata_session_close_cannot_leak_or_override_first_error(tmp_path, monkeypatch, success):
    import diagnose_public_news as cli
    closes = []
    def send(session, request, **kwargs):
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(closed=closes)(None, params=params)
        if not success:
            response.content = b'other({"code":0,"result":{}})'
        response.headers, response.url = {}, request.url
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    def close(session):
        raise OSError(MARKER)
    monkeypatch.setattr(requests.Session, "close", close)
    result = cli._capture_direct(tmp_path, ticker="600895.SH", start=START, end=END, metadata_only=True)
    assert result["stage"] == ("session_close" if success else "retention")
    assert result["metadata_passed"] is False and closes == [True]
    assert result["transport_metadata"]["session_close"] == "failed"
    assert result["checks"]["cleanup"]["reason"] == "session_close_failed"
    receipt = json.loads((tmp_path / "capture.json").read_text())
    assert receipt == result and MARKER not in json.dumps(receipt)


@pytest.mark.parametrize("flags", [
    ["--authorize-one-public-get-and-save", "--authorize-one-public-get-metadata-only"],
    ["--authorize-one-public-get-and-save", "--percent-encoded-spaces"],
    ["--authorize-one-public-get-metadata-only", "--acceptance-mode", "original-text"],
])
def test_cli_modes_fail_before_install_or_request(tmp_path, monkeypatch, flags):
    import diagnose_public_news as cli
    import sys
    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(tmp_path / "unused"),
        "--start", START, "--end", END, "--ticker", "600895.SH", "--transport", "direct", *flags])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not (tmp_path / "unused").exists()


@pytest.mark.parametrize("success", [False, True])
def test_referer_variant_is_exact_single_header_deletion(tmp_path, monkeypatch, success):
    import diagnose_public_news as cli
    sent, closes, bodies = [], [], []
    def send(session, request, **kwargs):
        sent.append({"url": request.url, "method": request.method, "body": request.body,
                     "headers": dict(request.headers), "cookies": dict(request._cookies),
                     "hooks": request.hooks, "send_kwargs": kwargs,
                     "trust_env": session.trust_env, "retries": session.get_adapter(request.url).max_retries.total})
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(symbol="600919", dates=["2026-11-13"], closed=closes,
            mutate=lambda value: value["result"]["cmsArticleWebOld"][0].update(content=MARKER))(None, params=params)
        if not success:
            response.content = b'other({"code":0,"result":{}})'
        response.url, response.headers = request.url, {}
        bodies.append(response.content)
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    results = []
    for variant in ("production", "omit-referer-v1"):
        output = tmp_path / variant
        output.mkdir()
        result = cli._capture_direct(output, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
            metadata_only=True, percent_encoded_spaces=True, request_variant=variant)
        results.append(result)
        assert result["request_count"] == result["transport_metadata"]["send_attempts"] == 1
        assert result["metadata_passed"] is success
        if not success:
            assert result["stage"] == "retention" and result["checks"]["parser"]["status"] == "not_started"
        assert_no_content(output, result, bodies)
    expected = dict(sent[0])
    expected["headers"] = dict(expected["headers"])
    key = next(key for key in expected["headers"] if key.lower() == "referer")
    assert expected["headers"].pop(key) == "https://so.eastmoney.com/news/s?keyword=600919"
    assert sent[1] == expected and closes == [True, True]
    assert hashlib.sha256(sent[1]["url"].encode()).hexdigest() == "c665f7667ded81f7cb715cc9f10802cbcafb38c762f3c6069085a244b06a8c8d"
    assert results[0]["transport_metadata"]["request_header_delta"] == []
    metadata = results[1]["transport_metadata"]
    assert metadata["request_variant"] == "omit-referer-v1" and metadata["prepared_variant_verified"]
    assert metadata["request_headers"]["referer"] == "absent"
    assert metadata["request_header_delta"] == [{"field": "referer", "operation": "remove"}]
    # This legacy field continues to bind the production input, never secretly
    # redefined as a fingerprint of the modified PreparedRequest.
    assert results[0]["request_fingerprint"] == results[1]["request_fingerprint"]
    assert metadata["request_fingerprint_scope"] == "production_request_before_diagnostic_variant"


@pytest.mark.parametrize("replacement", [None, MARKER])
def test_referer_variant_rejects_unexpected_baseline_without_send(tmp_path, monkeypatch, replacement):
    import diagnose_public_news as cli
    original = requests.Session.prepare_request
    def prepare(session, request):
        result = original(session, request)
        if replacement is None:
            del result.headers["referer"]
        else:
            result.headers["referer"] = replacement
        return result
    monkeypatch.setattr(requests.Session, "prepare_request", prepare)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, request_variant="omit-referer-v1")
    assert result["transport_metadata"]["send_attempts"] == 0
    assert result["metadata_passed"] is False and "http_status" not in result
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize("variant", ["omit-referer-v1", "accept-javascript-v1", "accept-edge-value-v1"])
def test_referer_variant_preserves_one_send_budget(tmp_path, monkeypatch, variant):
    import diagnose_public_news as cli
    original = diagnostic.akshare_data._stock_news_em
    sends, closes = [], []
    def duplicate(*args, **kwargs):
        original(*args, **kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(diagnostic.akshare_data, "_stock_news_em", duplicate)
    def send(session, request, **kwargs):
        sends.append(True)
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(symbol="600919", dates=["2026-11-13"], closed=closes)(None, params=params)
        response.url, response.headers = request.url, {}
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, request_variant=variant)
    assert result["transport_metadata"]["send_attempts"] == 1 and sends == closes == [True]
    assert result["checks"]["network"]["reason"] == "request_scope_or_budget"
    assert result["metadata_passed"] is False


@pytest.mark.parametrize("variant,metadata,percent,ticker", [
    (MARKER, True, True, "600919.SH"), ("omit-referer-v1", False, True, "600919.SH"),
    ("omit-referer-v1", True, False, "600919.SH"), ("omit-referer-v1", True, True, "600895.SH"),
    ("accept-edge-value-v1", False, True, "600919.SH"),
    ("accept-edge-value-v1", True, False, "600919.SH"),
    ("accept-edge-value-v1", True, True, "600895.SH"),
])
def test_referer_variant_scope_rejected_before_session(tmp_path, monkeypatch, variant, metadata, percent, ticker):
    import diagnose_public_news as cli
    def forbidden():
        raise AssertionError("session created before variant validation")
    monkeypatch.setattr(requests, "Session", forbidden)
    with pytest.raises(ValueError) as error:
        cli._capture_direct(tmp_path, ticker=ticker, start=START, end=END,
            metadata_only=metadata, percent_encoded_spaces=percent, request_variant=variant)
    assert MARKER not in str(error.value)


@pytest.mark.parametrize("variant", ["omit-referer-v1", "accept-javascript-v1", "accept-edge-value-v1"])
def test_referer_variant_cli_flag_is_bound_into_receipts(tmp_path, monkeypatch, capsys, variant):
    import diagnose_public_news as cli
    import sys
    output = tmp_path / "output"
    sends = []
    def send(session, request, **kwargs):
        sends.append(request.url)
        if variant == "omit-referer-v1":
            assert "referer" not in request.headers
        else:
            assert request.headers["referer"] == "https://so.eastmoney.com/news/s?keyword=600919"
            assert request.headers["accept"] == (EDGE_ACCEPT if variant == "accept-edge-value-v1" else "text/javascript")
        return SimpleNamespace(status_code=200, content=b'other({"code":0,"result":{}})',
                               close=lambda: None, url=request.url, headers={})
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(cli, "SOURCE", tmp_path / "synthetic-source")
    monkeypatch.setattr(cli, "install", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True))
    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(output),
        "--start", "2026-11-06", "--end", "2026-11-13", "--ticker", "600919.SH", "--transport", "direct",
        "--authorize-one-public-get-metadata-only", "--percent-encoded-spaces", "--request-variant", variant])
    assert cli.main() == 1 and len(sends) == 1
    result = json.loads((output / "result.json").read_text())
    reservation = json.loads((output / "reservation.json").read_text())
    assert reservation["request_variant"] == result["transport_metadata"]["request_variant"] == variant
    summary = json.loads(capsys.readouterr().out)
    assert summary["transport_metadata"]["request_header_delta"] == (
        [{"field": "referer", "operation": "remove"}] if variant == "omit-referer-v1" else
        [{"field":"accept","operation":"replace","before":"*/*",
          "after":EDGE_ACCEPT if variant == "accept-edge-value-v1" else "text/javascript"}])
    assert not (output / diagnostic.BODY_NAME).exists()


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("variant,expected_accept", [
    ("accept-javascript-v1", "text/javascript"), ("accept-edge-value-v1", EDGE_ACCEPT),
])
def test_accept_variant_is_exact_single_header_replacement(tmp_path, monkeypatch, success, variant, expected_accept):
    import diagnose_public_news as cli
    sent, closes, bodies = [], [], []
    def send(session, request, **kwargs):
        sent.append({"url":request.url,"method":request.method,"body":request.body,
            "headers":dict(request.headers),"cookies":dict(request._cookies),"hooks":request.hooks,
            "kwargs":kwargs,"trust_env":session.trust_env,"retries":session.get_adapter(request.url).max_retries.total})
        params = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        response = synthetic_get(symbol="600919", dates=["2026-11-13"], closed=closes,
            mutate=lambda value:value["result"]["cmsArticleWebOld"][0].update(content=MARKER))(None,params=params)
        if not success:
            response.content=b'other({"code":0,"result":{}})'
        bodies.append(response.content)
        response.url,response.headers=request.url,{}
        return response
    monkeypatch.setattr(requests.Session,"send",send)
    results=[]
    for selected in ("production",variant):
        output=tmp_path/selected
        output.mkdir()
        result=cli._capture_direct(output,ticker="600919.SH",start="2026-11-06",end="2026-11-13",
            metadata_only=True,percent_encoded_spaces=True,request_variant=selected)
        results.append(result)
        assert result["metadata_passed"] is success
        assert result["request_count"]==result["transport_metadata"]["send_attempts"]==1
        assert_no_content(output,result,bodies)
    expected=dict(sent[0]);expected["headers"]=dict(expected["headers"])
    key=next(key for key in expected["headers"] if key.lower()=="accept")
    assert expected["headers"][key]=="*/*"
    expected["headers"][key]=expected_accept
    assert sent[1]==expected and closes==[True,True]
    metadata=results[1]["transport_metadata"]
    assert metadata["request_headers"]["referer"]=="https://so.eastmoney.com/news/s?keyword=600919"
    assert metadata["request_headers"]["accept"]==expected_accept
    assert metadata["request_variant"]==variant
    assert metadata["request_header_delta"]==[{"field":"accept","operation":"replace","before":"*/*","after":expected_accept}]


@pytest.mark.parametrize("replacement",[None,MARKER])
@pytest.mark.parametrize("variant", ["accept-javascript-v1", "accept-edge-value-v1"])
def test_accept_variant_rejects_unexpected_baseline_without_send(tmp_path,monkeypatch,replacement,variant):
    import diagnose_public_news as cli
    original=requests.Session.prepare_request
    def prepare(session,request):
        result=original(session,request)
        if replacement is None:
            del result.headers["accept"]
        else:
            result.headers["accept"]=replacement
        return result
    monkeypatch.setattr(requests.Session,"prepare_request",prepare)
    result=cli._capture_direct(tmp_path,ticker="600919.SH",start="2026-11-06",end="2026-11-13",
        metadata_only=True,percent_encoded_spaces=True,request_variant=variant)
    assert result["transport_metadata"]["send_attempts"]==0 and not result["metadata_passed"]
    assert MARKER not in json.dumps(result)


def test_edge_accept_html_remains_rejected_and_unretained(tmp_path, monkeypatch):
    import diagnose_public_news as cli
    closes = []
    body = ("<html><body>" + MARKER + "</body></html>").encode()
    def send(session, request, **kwargs):
        assert request.headers["accept"] == EDGE_ACCEPT
        return SimpleNamespace(status_code=200, content=body,
            close=lambda: closes.append(True), url=request.url,
            headers={"Content-Type": "text/html", "Set-Cookie": MARKER})
    monkeypatch.setattr(requests.Session, "send", send)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-11-06", end="2026-11-13",
        metadata_only=True, percent_encoded_spaces=True, request_variant="accept-edge-value-v1")
    assert result["request_count"] == result["transport_metadata"]["send_attempts"] == 1
    assert result["metadata_passed"] is False and closes == [True]
    assert result["checks"]["decode"]["status"] == "failed"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert_no_content(tmp_path, result, [body])


@pytest.mark.parametrize("wrapper", ["plain_json", "expected_jsonp", "other_valid_jsonp", "invalid_or_unknown"])
def test_closed_shape_reports_only_fixed_fields_and_classes(tmp_path, wrapper):
    closes, bodies = [], []
    value = {"code": 0, "msg": "OK", "bizMsg": "", "bizCode": MARKER,
        "result": {"error": {MARKER: MARKER}, "data": [MARKER]},
        "data": {"code": 17, "msg": MARKER, "cmsArticleWebOld": [MARKER, None]},
        MARKER: {MARKER: MARKER}}
    def get(url, *, params, **kwargs):
        text = json.dumps(value)
        if wrapper == "expected_jsonp":
            text = params["cb"] + "(" + text + ")"
        elif wrapper == "other_valid_jsonp":
            text = MARKER + "(" + text + ")"
        elif wrapper == "invalid_or_unknown":
            text = "<html>" + MARKER + "</html>"
        bodies.append(text.encode())
        return SimpleNamespace(status_code=200, content=bodies[-1], close=lambda: closes.append(True))
    result = capture(tmp_path, get)
    shape = result["closed_shape"]
    assert shape["contract"] == "news-closed-shape-v1"
    assert shape["wrapper"] == wrapper and shape["qualification_authority"] is False
    assert set(shape) == {"contract", "classification", "wrapper", "qualification_authority", "scopes"}
    assert set(shape["scopes"]) == {"root", "result", "data"}
    known = {"code", "msg", "bizCode", "bizMsg", "result", "data", "error", "hitsTotal",
             "cmsArticleWebOld", "status", "success"}
    for scope in shape["scopes"].values():
        assert set(scope) == {"container_type", "fields", "code_class", "message_classes", "article_count", "count_capped"}
        assert set(scope["fields"]) == known
        assert set(scope["message_classes"]) == {"msg", "bizMsg"}
    if wrapper != "invalid_or_unknown":
        root, nested = shape["scopes"]["root"], shape["scopes"]["data"]
        assert root["code_class"] == "integer_zero"
        assert root["message_classes"] == {"msg": "exact_OK", "bizMsg": "empty_string"}
        assert root["fields"]["hitsTotal"] == "absent"
        assert shape["scopes"]["result"]["fields"]["error"] == "object"
        assert nested["code_class"] == "integer_nonzero"
        assert nested["message_classes"]["msg"] == "unknown"
        assert nested["fields"]["cmsArticleWebOld"] == "array" and nested["article_count"] == 2
    else:
        assert shape["classification"] == "unavailable"
        assert shape["scopes"]["root"]["container_type"] == "unavailable"
    assert result["metadata_passed"] is False
    assert result["checks"]["parser"]["status"] == "not_started"
    assert result["request_count"] == 1 and closes == [True]
    assert_no_content(tmp_path, result, bodies)


@pytest.mark.parametrize("body,wrapper", [
    (b'{"code":0,"code":1}', "invalid_or_unknown"),
    (b'{"code":NaN}', "invalid_or_unknown"),
    (b'{"code":1e9999}', "invalid_or_unknown"),
    (b'{"result":{}} trailing', "invalid_or_unknown"),
    (b'[' * 1500 + b']' * 1499, "invalid_or_unknown"),
    (b'\xff', "invalid_or_unknown"),
    (b'null', "plain_json"), (b'[]', "plain_json"),
])
def test_closed_shape_malformed_or_nested_input_is_safe(tmp_path, body, wrapper):
    closes = []
    result = capture(tmp_path, lambda *args, **kwargs: SimpleNamespace(status_code=200,
        content=body, close=lambda: closes.append(True)))
    assert result["closed_shape"]["wrapper"] == wrapper
    assert result["metadata_passed"] is False and closes == [True]
    assert result["checks"]["parser"]["status"] == "not_started"
    assert_no_content(tmp_path, result, [])


@pytest.mark.parametrize("code,classification", [(False, "wrong_type"), (None, "wrong_type"),
    (0, "integer_zero"), (1, "integer_nonzero"), (-9, "integer_nonzero"),
    ("0", "wrong_type"), ({MARKER: MARKER}, "wrong_type")])
def test_closed_shape_code_classes_and_bounded_count(code, classification):
    body = json.dumps({"code": code, "msg": " OK ", "bizMsg": "ok",
        "result": {"cmsArticleWebOld": [None] * (diagnostic.COUNT_LIMIT + 1)}}).encode()
    assert len(body) < diagnostic.LIMIT
    shape = diagnostic._metadata_shape(body, None, None)
    assert shape["scopes"]["root"]["code_class"] == classification
    assert shape["scopes"]["root"]["message_classes"] == {"msg": "unknown", "bizMsg": "unknown"}
    assert shape["scopes"]["result"]["article_count"] == diagnostic.COUNT_LIMIT
    assert shape["scopes"]["result"]["count_capped"] is True
    assert MARKER not in json.dumps(shape)


@pytest.mark.parametrize("fatal", [False, True])
def test_closed_shape_failure_preserves_primary_and_close(tmp_path, monkeypatch, fatal):
    closes = []
    failure = KeyboardInterrupt(MARKER) if fatal else ValueError(MARKER)
    def fail(*args):
        raise failure
    monkeypatch.setattr(diagnostic, "_metadata_shape", fail)
    def close():
        closes.append(True)
        raise OSError(MARKER)
    get = lambda *args, **kwargs: SimpleNamespace(status_code=200,
        content=b'other({"code":0,"result":{}})', close=close)
    if fatal:
        with pytest.raises(KeyboardInterrupt) as caught:
            capture(tmp_path, get)
        assert caught.value is failure
    else:
        result = capture(tmp_path, get)
        assert result["closed_shape"]["classification"] == "unavailable"
        assert result["stage"] == "retention" and result["checks"]["retention"]["reason"] == "articles_missing"
        assert result["checks"]["parser"]["status"] == "not_started"
        assert_no_content(tmp_path, result, [])
    assert closes == [True]


def test_closed_shape_is_metadata_only_and_not_admission(tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError("metadata observer used in default capture")
    original = diagnostic._metadata_shape
    monkeypatch.setattr(diagnostic, "_metadata_shape", unexpected)
    default = tmp_path / "default"
    default.mkdir()
    result = diagnostic.capture(default, get=synthetic_get(), ticker="600895.SH",
        transport="direct", start=START, end=END)
    assert "closed_shape" not in result and result["capture_passed"]
    monkeypatch.setattr(diagnostic, "_metadata_shape", original)
    meta = tmp_path / "metadata"
    meta.mkdir()
    result = capture(meta, synthetic_get())
    assert result["metadata_passed"] and result["closed_shape"]["wrapper"] == "expected_jsonp"
    assert result["closed_shape"]["scopes"]["result"]["article_count"] == 3
    assert_no_content(meta, result, [])
