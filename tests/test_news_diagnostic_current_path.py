"""Actual current route/parser; only the transport response is synthetic."""
import json
from types import SimpleNamespace

import pytest

from tests.test_news_diagnostic import synthetic_get
from tradingagents import news_diagnostic as diagnostic


def test_current_route_reaches_one_transport_and_preserves_request_settings(tmp_path, monkeypatch):
    calls = []
    closed = []

    def obsolete_sdk(*args, **kwargs):
        raise AssertionError("obsolete SDK path used")

    monkeypatch.setattr(diagnostic.akshare_data.ak, "stock_news_em", obsolete_sdk)

    def get(url, *, params, headers, allow_redirects, timeout):
        calls.append((allow_redirects, timeout, json.loads(params["param"])))
        body = (params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": [{
            "date": "2026-10-10 12:00:00", "mediaName": "Synthetic public source",
            "code": "202610100000", "title": "600895 synthetic news", "content": "Synthetic content",
        }]}}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, text=body.decode(), headers={},
                               close=lambda: closed.append(True))

    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start="2026-10-05", end="2026-10-12")
    assert len(calls) == 1, result["checks"]
    assert calls[0][:2] == (False, 15)
    assert calls[0][2]["param"]["cmsArticleWebOld"]["pageSize"] == 10
    assert closed == [True]
    assert result["capture_passed"]
    assert result["artifact_sha256"]
    assert result["selection"]["coverage"] == "unknown"
    assert result["selection"]["excluded_counts"] == {
        "malformed": 0, "undated": 0, "outside_window": 0, "wrong_stock": 0,
    }


@pytest.mark.parametrize("malformed_title,retained", [({}, False), ("", True)])
def test_current_parser_counts_each_exclusion_once_without_weakening_dates(tmp_path, malformed_title, retained):
    rows = [
        {"date": "2026-10-10 12:00:00", "code": "n1", "title": "600895 valid"},
        {"date": "2026-10-10 12:00:00", "code": "n2", "title": malformed_title},
        {"date": "bad-date", "code": "n3", "title": "600895 undated"},
        {"date": "2026-10-13 00:00:00", "code": "n4", "title": "600895 future"},
        {"date": "2026-10-10 12:00:00", "code": "n5", "title": "600919 unrelated"},
    ]
    # Keep both the original malformed object and a retainable malformed string.
    for row in rows:
        row.update(mediaName="Synthetic", content="Synthetic")

    def get(url, *, params, **kwargs):
        body = (params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": rows}}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, text=body.decode(), headers={}, close=lambda: None)

    expected_counts = {
        "malformed": 1, "undated": 1, "outside_window": 1, "wrong_stock": 1,
    }
    page = diagnostic.eastmoney_news.parse_news_page(
        get(None, params={"cb": "synthetic"}).content, expected_callback="synthetic", ticker="600895",
        start_date="2026-10-05", end_date="2026-10-12")
    assert page.excluded_counts == expected_counts and len(page.rows) == 1
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start="2026-10-05", end="2026-10-12")
    assert result["capture_passed"] is retained
    if retained:
        assert result["selection"]["excluded_counts"] == expected_counts
        assert result["selection"]["accepted_count"] == 1
        assert result["checks"]["production"]["status"] == "passed"
    else:
        assert result["checks"]["retention"]["reason"] == "non_string_article_field"
        assert result["checks"]["parser"]["status"] == "not_started"
        assert result["selection"]["status"] == "not_observed"
        assert not result.get("artifact_sha256")


def test_explicit_jiangsu_bank_target_binds_request_selection_and_replay(tmp_path):
    calls = []
    rows = [
        {"date": "2026-10-31 00:00:00", "title": "600919 first day"},
        {"date": "2026-11-07T15:59:59Z", "title": "600919 last second"},
        {"date": "2026-11-07T16:00:00Z", "title": "600919 outside Shanghai cutoff"},
        {"date": "2026-10-30 23:59:59", "title": "600919 before window"},
        {"date": "2026-11-01", "title": "600895 unrelated"},
        {"date": "invalid-date", "title": "600919 undated"},
    ]
    for row in rows:
        row.update(mediaName="Synthetic", code="synthetic-article", content="Synthetic public content")

    def get(url, *, params, allow_redirects, timeout, **kwargs):
        request = json.loads(params["param"])
        calls.append(request["keyword"])
        assert request["keyword"] == "600919"
        assert request["param"]["cmsArticleWebOld"]["pageIndex"] == 1
        assert request["param"]["cmsArticleWebOld"]["pageSize"] == 10
        assert allow_redirects is False and timeout == 15
        body = (params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": rows}}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, close=lambda: None)

    result = diagnostic.capture(tmp_path, transport="direct", ticker="600919.SH", get=get,
                                start="2026-10-31", end="2026-11-07")
    assert calls == ["600919"] and result["request_count"] == 1
    assert result["ticker"] == "600919.SH" and result["capture_passed"]
    assert result["selection"]["accepted_count"] == 2
    assert result["selection"]["excluded_counts"] == {
        "malformed": 0, "undated": 1, "outside_window": 2, "wrong_stock": 1,
    }
    replayed = diagnostic.replay(tmp_path, transport="direct", ticker="600919.SH", start="2026-10-31", end="2026-11-07")
    assert replayed["acceptance_passed"] and replayed["same_ticker"]
    assert calls == ["600919"]  # replay supplied bytes without the getter


@pytest.mark.parametrize("ticker", [None, 600919, {}, [], "600919", "600919.SS", "000001.SZ", "SYNTHETIC-PRIVATE"])
def test_invalid_target_cannot_reach_transport_or_enter_receipt(tmp_path, ticker):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid target reached transport")

    result = diagnostic.capture(tmp_path, transport="direct", ticker=ticker, get=forbidden,
                                start="2026-10-31", end="2026-11-07")
    assert result["request_count"] == 0 and result["ticker"] is None
    assert result["checks"]["parser"]["reason"] == "request_invalid"
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()
    assert not diagnostic.replay(tmp_path, transport="direct", ticker=ticker, start="2026-10-31", end="2026-11-07")["acceptance_passed"]


def test_library_requires_explicit_target(tmp_path):
    with pytest.raises(TypeError, match="ticker"):
        diagnostic.capture(tmp_path, transport="direct", get=lambda *a, **k: None, start="2026-10-31", end="2026-11-07")
    with pytest.raises(TypeError, match="ticker"):
        diagnostic.replay(tmp_path, transport="direct", start="2026-10-31", end="2026-11-07")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", ["different", "missing", "v2"])
def test_replay_rejects_unbound_stock_before_body_processing(tmp_path, monkeypatch, mutation):
    from tests.test_news_diagnostic import synthetic_get

    diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=synthetic_get(),
                       start="2026-10-05", end="2026-10-12")
    path = tmp_path / "capture.json"
    receipt = json.loads(path.read_text())
    if mutation == "missing":
        receipt.pop("ticker")
    elif mutation == "v2":
        receipt["contract"] = "news-production-diagnostic-v2"
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()
    original_read = diagnostic._read_retained

    def read(root, name):
        assert name == "capture.json", "mismatched stock must not read the body"
        return original_read(root, name)

    monkeypatch.setattr(diagnostic, "_read_retained", read)
    result = diagnostic.replay(tmp_path, transport="direct", ticker="600919.SH", start="2026-10-05", end="2026-10-12")
    assert result["request_count"] == 0 and not result["acceptance_passed"]
    assert result["checks"]["replay"]["reason"] == (
        "unsupported_capture_contract" if mutation == "v2" else "ticker_mismatch")
    assert path.read_bytes() == before


def test_wrong_request_stock_is_rejected_before_supplied_getter(tmp_path, monkeypatch):
    original = diagnostic.akshare_data._stock_news_em

    def wrong_stock(symbol, start, end, *, instrument_aliases=()):
        return original("600895", start, end, instrument_aliases=instrument_aliases)

    monkeypatch.setattr(diagnostic.akshare_data, "_stock_news_em", wrong_stock)
    calls = []
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600919.SH", get=lambda *a, **k: calls.append(True),
                                start="2026-10-31", end="2026-11-07")
    assert calls == [] and result["request_count"] == 0
    assert result["checks"]["network"]["reason"] == "request_scope_or_budget"
    assert not result["capture_passed"]


def test_jiangsu_bank_transport_failure_does_not_repeat_supplied_getter(tmp_path):
    calls = []

    def get(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("SYNTHETIC-PRIVATE")

    result = diagnostic.capture(tmp_path, transport="direct", ticker="600919.SH", get=get,
                                start="2026-10-31", end="2026-11-07")
    assert calls == [True] and result["request_count"] == 1
    assert result["checks"]["network"]["reason"] == "transport_exception"
    assert not result["capture_passed"]
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("target_args", [[], ["--ticker", "600919.SS"]])
def test_cli_requires_explicit_supported_target_before_output(tmp_path, monkeypatch, target_args):
    import sys

    from scripts import diagnose_public_news as cli

    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(tmp_path / "output"),
                                    "--start", "2026-10-31", "--end", "2026-11-07",
                                    "--transport", "direct", "--authorize-one-public-get-and-save", *target_args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("start,end", [("SYNTHETIC-PRIVATE", "2026-11-07"),
                                      ("2026-11-08", "2026-11-07"),
                                      ("20261031", "2026-11-07")])
def test_cli_rejects_invalid_window_before_reservation(tmp_path, monkeypatch, capsys, start, end):
    import sys

    from scripts import diagnose_public_news as cli

    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(tmp_path / "output"),
                                    "--start", start, "--end", end, "--ticker", "600919.SH",
                                    "--transport", "direct", "--authorize-one-public-get-and-save"])
    monkeypatch.setattr(cli, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "output").exists()
    assert "SYNTHETIC-PRIVATE" not in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["success", "code_zero", "http_503", "SSLError", "ProxyError", "Timeout"])
def test_direct_session_prepares_exact_request_without_discovery_or_fallback(tmp_path, monkeypatch, outcome):
    """Real Requests preparation and current production route; send is synthetic."""
    from urllib.parse import parse_qs, urlsplit

    import requests

    from scripts import diagnose_public_news as cli

    def forbidden(*args, **kwargs):
        raise AssertionError("implicit proxy or credential discovery")

    # Both imported aliases and Windows-specific fallback must stay unused.
    for module, name in ((requests.sessions, "get_netrc_auth"),
                         (requests.sessions, "get_environ_proxies"),
                         (requests.utils, "getproxies"),
                         (requests.utils, "getproxies_environment"),
                         (requests.utils, "proxy_bypass_registry")):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    sent, closed = [], []
    original_close = requests.Session.close

    def close(session):
        closed.append(session)
        original_close(session)

    def send(session, request, **kwargs):
        sent.append(session)
        assert session.trust_env is False and session.proxies == {} and session.verify is True
        assert session.get_adapter(request.url).max_retries.total == 0
        assert kwargs["verify"] is True and kwargs["proxies"] == {}
        assert kwargs["timeout"] == 15 and kwargs["allow_redirects"] is False
        assert request.method == "GET"
        assert "Authorization" not in request.headers and "Proxy-Authorization" not in request.headers
        params = {key: values[0] for key, values in parse_qs(urlsplit(request.url).query).items()}
        inner = json.loads(params["param"])
        assert inner["keyword"] == "600919"
        assert inner["param"]["cmsArticleWebOld"]["pageIndex"] == 1
        assert inner["param"]["cmsArticleWebOld"]["pageSize"] == 10
        if outcome in {"SSLError", "ProxyError", "Timeout"}:
            raise getattr(requests.exceptions, outcome)("SYNTHETIC-PRIVATE")
        response = requests.Response()
        response.status_code = 503 if outcome == "http_503" else 200
        response._content = synthetic_get(
            symbol="600919", dates=["2026-11-06"], extra={"code": 0} if outcome == "code_zero" else None,
        )(diagnostic.URL, params=params).content
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(requests.Session, "close", close)
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-10-31", end="2026-11-07")
    assert len(sent) == 1 and closed == sent
    assert result["transport"] == "direct" and result["request_count"] == 1
    assert result["capture_passed"] is (outcome == "success")
    assert not result["qualification_authority"]
    if outcome in {"SSLError", "ProxyError", "Timeout"}:
        assert result["checks"]["network"]["exception_type"] == outcome
        assert not result["body_saved"]
    elif outcome == "code_zero":
        assert result["checks"]["parser"]["reason"] == "business_status_unverified"
    elif outcome == "http_503":
        assert result["http_status"] == 503 and result["checks"]["network"]["status"] == "passed"
        assert result["checks"]["http"]["reason"] == "http_failure_or_redirect"
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


def test_direct_session_fails_before_send_if_default_retries_change(tmp_path, monkeypatch):
    import requests

    from scripts import diagnose_public_news as cli

    monkeypatch.setattr(requests.Session, "get_adapter", lambda *a: SimpleNamespace(max_retries=SimpleNamespace(total=1)))
    monkeypatch.setattr(requests.Session, "send", lambda *a, **k: pytest.fail("retry drift reached send"))
    result = cli._capture_direct(tmp_path, ticker="600919.SH", start="2026-10-31", end="2026-11-07")
    assert not result["capture_passed"] and result["stage"] == "transport"


@pytest.mark.parametrize("transport", [None, {}, [], "implicit", "proxy", "SYNTHETIC-PRIVATE"])
def test_invalid_transport_is_rejected_without_discovery_or_retention(tmp_path, transport):
    result = diagnostic.capture(tmp_path, ticker="600919.SH", transport=transport,
                                get=lambda *a, **k: pytest.fail("invalid transport reached getter"),
                                start="2026-10-31", end="2026-11-07")
    assert result["request_count"] == 0 and result["transport"] is None
    assert not result["capture_passed"]
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()
    assert not diagnostic.replay(tmp_path, ticker="600919.SH", transport=transport,
                                 start="2026-10-31", end="2026-11-07")["acceptance_passed"]


def test_library_has_no_default_transport(tmp_path):
    with pytest.raises(TypeError, match="transport"):
        diagnostic.capture(tmp_path, ticker="600919.SH", get=lambda *a, **k: None,
                           start="2026-10-31", end="2026-11-07")
    with pytest.raises(TypeError, match="transport"):
        diagnostic.replay(tmp_path, ticker="600919.SH", start="2026-10-31", end="2026-11-07")


@pytest.mark.parametrize("change", ["missing_transport", "different_transport", "v3"])
def test_transport_or_legacy_receipt_rejected_before_body(tmp_path, monkeypatch, change):
    diagnostic.capture(tmp_path, ticker="600895.SH", transport="direct", get=synthetic_get(),
                       start="2026-10-05", end="2026-10-12")
    path = tmp_path / "capture.json"
    receipt = json.loads(path.read_text())
    if change == "v3":
        receipt["contract"] = "news-production-diagnostic-v3"
    elif change == "missing_transport":
        receipt.pop("transport")
    else:
        receipt["transport"] = "implicit"
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()
    original_read = diagnostic._read_retained

    def read(root, name):
        assert name == "capture.json", "mismatched policy must not read body"
        return original_read(root, name)

    monkeypatch.setattr(diagnostic, "_read_retained", read)
    result = diagnostic.replay(tmp_path, ticker="600895.SH", transport="direct",
                               start="2026-10-05", end="2026-10-12")
    assert not result["acceptance_passed"] and result["request_count"] == 0
    assert result["checks"]["replay"]["reason"] == (
        "unsupported_capture_contract" if change == "v3" else "transport_mismatch")
    assert path.read_bytes() == before


@pytest.mark.parametrize("transport_args", [[], ["--transport", "implicit"]])
def test_cli_requires_explicit_direct_choice_before_output(tmp_path, monkeypatch, transport_args):
    import sys

    from scripts import diagnose_public_news as cli

    monkeypatch.setattr(sys, "argv", ["diagnose_public_news.py", "--output", str(tmp_path / "output"),
                                    "--start", "2026-10-31", "--end", "2026-11-07", "--ticker", "600919.SH",
                                    "--authorize-one-public-get-and-save", *transport_args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not (tmp_path / "output").exists()


def _synthetic_wrapped_connection_error():
    import errno

    from requests.exceptions import ConnectionError
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    leaf = PermissionError(errno.EACCES, "SYNTHETIC-PRIVATE https://secret.invalid/ token=secret")
    connection = NewConnectionError(None, "SYNTHETIC-PRIVATE")
    connection.__cause__ = leaf
    exhausted = MaxRetryError(None, "https://secret.invalid/", connection)
    return ConnectionError(exhausted)


def test_transport_detail_preserves_bounded_nested_wrappers_without_raw_data(tmp_path):
    import errno

    error = _synthetic_wrapped_connection_error()
    calls = []

    def get(*args, **kwargs):
        calls.append(True)
        raise error

    result = diagnostic.capture(tmp_path, ticker="600919.SH", transport="direct", get=get,
                                start="2026-10-31", end="2026-11-07")
    assert calls == [True] and result["request_count"] == 1
    detail = result["checks"]["network"]["transport_detail"]
    assert detail["contract"] == "news-transport-error-v1" and detail["termination"] == "no_link"
    assert [node["type"] for node in detail["chain"]] == [
        "requests.ConnectionError", "urllib3.MaxRetryError", "urllib3.NewConnectionError", "builtins.PermissionError"]
    assert [node["via"] for node in detail["chain"]] == ["root", "argument", "reason", "cause"]
    assert detail["chain"][1]["category"] == "transport_wrapper"  # not a retry count
    assert detail["chain"][-1]["errno"] == errno.EACCES
    assert not result["body_saved"] and not result["capture_passed"] and result["stage"] == "transport"
    saved = (tmp_path / "capture.json").read_text()
    assert all(value not in saved for value in ("SYNTHETIC-PRIVATE", "secret.invalid", "token=secret"))
    assert all(set(node) == {"type", "category", "via", "errno", "winerror"} for node in detail["chain"])


@pytest.mark.parametrize("edge", ["cause", "context", "argument", "reason"])
def test_transport_detail_detects_cycles(edge):
    from requests.exceptions import ConnectionError
    from urllib3.exceptions import MaxRetryError

    error = MaxRetryError(None, None, None) if edge == "reason" else ConnectionError("private")
    if edge == "cause":
        error.__cause__ = error
    elif edge == "context":
        error.__context__ = error
    elif edge == "argument":
        error.args = (error,)
    else:
        error.reason = error
    detail = diagnostic._transport_error_detail(error)
    assert len(detail["chain"]) == 1 and detail["termination"] == "cycle"


def test_transport_detail_bounds_depth_arguments_and_suppressed_context():
    from requests.exceptions import ConnectionError

    root = current = ConnectionError("private")
    for _ in range(20):
        child = ConnectionError("private")
        current.__cause__ = child
        current = child
    detail = diagnostic._transport_error_detail(root)
    assert len(detail["chain"]) == 8 and detail["termination"] == "node_limit"
    root = ConnectionError("private")
    root.args = ("a", "b", "c", "d", PermissionError(13, "private"))
    detail = diagnostic._transport_error_detail(root)
    assert len(detail["chain"]) == 1 and detail["termination"] == "argument_limit"
    root = ConnectionError("private")
    root.__context__ = PermissionError(13, "private")
    root.__suppress_context__ = True
    assert len(diagnostic._transport_error_detail(root)["chain"]) == 1
    root.__suppress_context__ = False
    assert diagnostic._transport_error_detail(root)["chain"][1]["via"] == "context"


def test_unknown_class_names_properties_and_formatting_are_never_read():
    class Hostile(Exception):
        def __str__(self):
            raise AssertionError("str called")

        def __repr__(self):
            raise AssertionError("repr called")

        def __getattribute__(self, key):
            raise AssertionError("arbitrary attribute read")

    error = Hostile()
    BaseException.__cause__.__set__(error, PermissionError(13, "secret.invalid"))
    detail = diagnostic._transport_error_detail(error)
    assert detail["chain"][0]["type"] == "other"
    assert detail["chain"][1]["type"] == "builtins.PermissionError"
    assert "Hostile" not in json.dumps(detail)


@pytest.mark.parametrize("code", [True, False, None, "10013", 13.0, 123456789, -9999])
def test_transport_detail_rejects_nonwhitelisted_numeric_values(code):
    error = OSError()
    error.errno = code
    if hasattr(OSError, "winerror"):
        error.winerror = code
    node = diagnostic._transport_error_detail(error)["chain"][0]
    assert node["errno"] is None and node["winerror"] is None


def test_transport_detail_distinguishes_dns_tls_and_windows_error_codes():
    import socket
    import ssl

    from urllib3.exceptions import NameResolutionError, ProxyError

    dns = socket.gaierror(socket.EAI_NONAME, "secret.invalid")
    detail = diagnostic._transport_error_detail(NameResolutionError("secret.invalid", None, dns))
    assert detail["chain"][1]["type"] == "socket.gaierror"
    assert detail["chain"][1]["errno"] == socket.EAI_NONAME
    tls = ssl.SSLCertVerificationError(int(ssl.SSL_ERROR_SSL), "private certificate text")
    node = diagnostic._transport_error_detail(tls)["chain"][0]
    assert node["category"] == "tls_certificate" and node["errno"] == ssl.SSL_ERROR_SSL
    error = PermissionError(13, "private")
    if hasattr(OSError, "winerror"):
        error.winerror = 10013
        assert diagnostic._transport_error_detail(error)["chain"][0]["winerror"] == 10013
    detail = diagnostic._transport_error_detail(ProxyError("private", error))
    assert detail["chain"][1]["via"] == "reason" and detail["chain"][1]["category"] == "permission"


def test_exception_reference_priority_and_nonexception_properties_are_bounded():
    import ssl

    from urllib3.exceptions import MaxRetryError

    class NotAnException:
        @property
        def __class__(self):
            raise AssertionError("arbitrary property read")

    outer = MaxRetryError(None, None, None)
    outer.reason = NotAnException()
    outer.args = (NotAnException(), "SYNTHETIC-PRIVATE")
    assert len(diagnostic._transport_error_detail(outer)["chain"]) == 1
    outer.reason = PermissionError(13, "private")
    outer.__cause__ = ConnectionResetError(10054, "private")
    detail = diagnostic._transport_error_detail(outer)
    assert detail["chain"][1]["type"] == "builtins.ConnectionResetError"
    assert detail["chain"][1]["via"] == "cause"
    # Only native plain integer errno values are emitted, not int-like objects.
    node = diagnostic._transport_error_detail(ssl.SSLError(ssl.SSL_ERROR_SSL, "private"))["chain"][0]
    assert node["errno"] is None


def test_transport_detail_failure_cannot_change_original_exception_or_retry(tmp_path, monkeypatch):
    from requests.exceptions import ConnectionError

    original = ConnectionError("SYNTHETIC-PRIVATE")
    seen = []
    old_record = diagnostic.run_diagnostics.record_news_exception

    def record(error, **kwargs):
        seen.append(error)
        return old_record(error, **kwargs)

    def detail(error):
        raise RuntimeError("SYNTHETIC-PRIVATE metadata failure")

    def get(*args, **kwargs):
        raise original

    monkeypatch.setattr(diagnostic, "_transport_error_detail", detail)
    monkeypatch.setattr(diagnostic.run_diagnostics, "record_news_exception", record)
    result = diagnostic.capture(tmp_path, ticker="600919.SH", transport="direct", get=get,
                                start="2026-10-31", end="2026-11-07")
    assert result["request_count"] == 1 and result["stage"] == "transport" and not result["capture_passed"]
    assert seen and all(error is original for error in seen)
    assert result["checks"]["network"]["transport_detail"] == {
        "contract": "news-transport-error-v1", "chain": [], "termination": "unavailable"}
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


def test_transport_detail_rejects_wrapper_dictionary_subclasses_without_hooks():
    from urllib3.exceptions import MaxRetryError

    class HostileDictionary(dict):
        def get(self, *args):
            raise BaseException("dictionary get hook")

        def items(self):
            raise BaseException("dictionary items hook")

        def __len__(self):
            raise BaseException("dictionary length hook")

    error = MaxRetryError(None, None, None)
    error.__dict__ = HostileDictionary(reason=PermissionError(13, "private"))
    detail = diagnostic._transport_error_detail(error)
    assert len(detail["chain"]) == 1 and detail["termination"] == "reference_unavailable"


def test_transport_detail_never_hash_looks_up_untrusted_dictionary_keys():
    from urllib3.exceptions import MaxRetryError

    class HostileKey:
        def __hash__(self):
            return hash("reason")

        def __eq__(self, other):
            raise BaseException("dictionary equality hook")

    error = MaxRetryError(None, None, None)
    error.__dict__ = {HostileKey(): PermissionError(13, "private")}
    detail = diagnostic._transport_error_detail(error)
    assert len(detail["chain"]) == 1 and detail["termination"] == "no_link"


def test_transport_detail_bounds_known_reference_dictionary_scan():
    from urllib3.exceptions import MaxRetryError

    error = MaxRetryError(None, None, None)
    error.__dict__ = {str(index): "private" for index in range(8)}
    error.reason = PermissionError(13, "private")
    detail = diagnostic._transport_error_detail(error)
    assert len(detail["chain"]) == 1 and detail["termination"] == "reference_unavailable"
