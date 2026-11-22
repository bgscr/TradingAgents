"""Current production route with code-defined responses, never live captures."""
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents import news_diagnostic as diagnostic

START, END = "2026-10-05", "2026-10-12"


@pytest.fixture(autouse=True)
def no_sdk_or_default_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unmocked transport or obsolete SDK")

    monkeypatch.setattr(diagnostic.akshare_data.ak, "stock_news_em", forbidden)
    import requests
    monkeypatch.setattr(requests, "get", forbidden)


def synthetic_get(*, symbol="600895", extra=None, dates=None, mutate=None, closed=None):
    def get(url, *, params, **kwargs):
        rows = [{"date": date, "mediaName": "Synthetic public fixture", "code": "202610100000",
                 "title": symbol + " synthetic title", "content": "Synthetic content"}
                for date in (dates if dates is not None else (
                    "2026-10-01 12:00:00", "2026-10-10 12:00:00", "invalid-date"))]
        value = {"result": {"cmsArticleWebOld": rows}, **(extra or {})}
        if mutate:
            mutate(value)
        body = (params["cb"] + "(" + json.dumps(value) + ")").encode()
        return SimpleNamespace(status_code=200, content=body,
                               close=lambda: closed.append(True) if closed is not None else None)
    return get


def capture(root, **kwargs):
    return diagnostic.capture(root, transport="direct", ticker="600895.SH", get=synthetic_get(**kwargs), start=START, end=END)


def replay(root):
    return diagnostic.replay(root, transport="direct", ticker="600895.SH", start=START, end=END)


def test_capture_current_parser_counts_and_exact_byte_replay(tmp_path):
    closed = []
    result = capture(tmp_path, extra={"hits": 3}, closed=closed)
    assert result["contract"] == "news-production-diagnostic-v4"
    assert result["capture_passed"] and closed == [True]
    assert result["request_count"] == 1 and result["raw_count"] == 3
    assert result["selection"] == {
        "status": "observed", "excluded_counts": {"malformed": 0, "undated": 1, "outside_window": 1, "wrong_stock": 0},
        "accepted_count": 1, "coverage": "unknown", "counts_saturated": False,
    }
    original = (tmp_path / "capture.json").read_bytes()
    replayed = replay(tmp_path)
    assert replayed["acceptance_passed"]
    assert replayed["same_source_bindings"] and replayed["same_adapter_frame_sha256"]
    assert replayed["same_artifact_sha256"]
    assert replayed["data_gaps"] == ["news_window_coverage_unknown"]
    assert replayed["historical_availability"] == "not_established_by_publication_date"
    assert (tmp_path / "capture.json").read_bytes() == original


@pytest.mark.parametrize("marker,reason", [
    ({"code": 0}, "business_status_unverified"), ({"code": 200}, "business_status_unverified"),
    ({"success": True}, "business_status_unverified"), ({"status": "ok"}, "business_status_unverified"),
    ({"error": None}, "business_status_unverified"), ({"success": False}, "business_failure"),
    ({"error": "SYNTHETIC-PRIVATE"}, "business_failure"), ({"status": "failed"}, "business_failure"),
])
@pytest.mark.parametrize("scope", ["root", "result"])
def test_business_status_never_becomes_success_from_http_200(tmp_path, marker, reason, scope):
    def mutate(value):
        (value if scope == "root" else value["result"]).update(marker)
    # Keep the parser's status semantics covered independently of retention.
    body = synthetic_get(mutate=mutate)(None, params={"cb": "synthetic"}).content
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError) as error:
        diagnostic.eastmoney_news.parse_news_page(
            body, expected_callback="synthetic", ticker="600895", start_date=START, end_date=END)
    assert error.value.reason == reason
    result = capture(tmp_path, mutate=mutate)
    assert result["checks"]["http"]["status"] == "passed"
    retained = "code" in marker or (scope == "root" and marker == {"success": True})
    assert result["body_saved"] is retained
    if retained:
        assert result["checks"]["parser"]["reason"] == reason
    else:
        assert result["checks"]["retention"]["status"] == "failed"
        assert result["checks"]["parser"]["status"] == "not_started"
    assert not result.get("artifact_sha256")
    assert result["selection"]["status"] == "not_observed"
    assert not result["capture_passed"] and not result["projection_saved"]
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()
    assert not replay(tmp_path)["acceptance_passed"]


def test_code_zero_diagnostic_keeps_only_safe_metadata(tmp_path):
    result = capture(tmp_path, extra={"code": 0})
    event = next(x for x in result["diagnostics"] if x["kind"] == "news_business_status")
    assert event["payload"]["fields"] == [{"field": "code", "scope": "root", "type": "integer", "value_class": "zero"}]
    assert event["qualification_authority"] is False
    assert result["body_saved"]  # Retain the recognized negative, never qualify it.


@pytest.mark.parametrize("status,collection,reason,retention", [
    ({"code": 0, "msg": "OK"}, {"passportWeb": []}, "business_status_unverified", "articles_missing"),
    ({"code": 0}, {"passportWeb": [{}]}, "business_status_unverified", "articles_missing"),
    ({"code": 0, "msg": "OK"}, {"cmsArticleWebOld": []}, "business_status_unverified", "unknown_envelope_fields"),
    ({}, {"passportWeb": []}, "schema_invalid", "articles_missing"),
    ({}, {}, "schema_invalid", "articles_missing"),
    ({}, {"cmsArticleWebOld": None}, "schema_invalid", "articles_null"),
    ({}, {"cmsArticleWebOld": {}}, "schema_invalid", "articles_not_list"),
    ({}, {"cmsArticleWebOld": "SYNTHETIC-PRIVATE"}, "schema_invalid", "articles_not_list"),
    ({}, {"cmsArticleWebOld": False}, "schema_invalid", "articles_not_list"),
    ({}, {"cmsArticleWebOld": 0}, "schema_invalid", "articles_not_list"),
])
def test_status_and_target_collection_fail_independently_not_as_no_news(tmp_path, status, collection, reason, retention):
    # Synthetic shape from the supplied issue clue, not a retained response or
    # authority for the provider's current business-code semantics.
    def get(url, *, params, **kwargs):
        body = (params["cb"] + "(" + json.dumps({**status, "result": collection}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, close=lambda: None)
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError) as error:
        diagnostic.eastmoney_news.parse_news_page(
            get(None, params={"cb": "synthetic"}).content, expected_callback="synthetic",
            ticker="600895", start_date=START, end_date=END)
    assert error.value.reason == reason
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert result["request_count"] == 1
    assert result["checks"]["retention"]["reason"] == retention
    assert result["checks"]["parser"]["status"] == "not_started"
    assert result["selection"]["excluded_counts"] is None
    assert result["selection"]["accepted_count"] is None
    assert result["selection"]["coverage"] == "unknown"
    assert result["raw_count"] == (0 if isinstance(collection.get("cmsArticleWebOld"), list) else None)
    assert not result["body_saved"] and not result["projection_saved"]
    assert not result["capture_passed"] and not result.get("artifact_sha256")
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


def test_empty_target_list_is_distinct_from_missing_null_or_wrong_type(tmp_path):
    result = capture(tmp_path, dates=[])
    assert result["checks"]["retention"]["status"] == "passed"
    assert result["raw_count"] == 0
    assert result["checks"]["parser"]["reason"] == "no_usable_articles"
    assert result["selection"]["coverage"] == "unknown"
    assert not result["capture_passed"] and not result.get("artifact_sha256")


@pytest.mark.parametrize("change,reason", [
    (lambda v: v.update(private_field="SYNTHETIC-PRIVATE"), "unknown_envelope_fields"),
    (lambda v: v["result"].update(private_field="SYNTHETIC-PRIVATE"), "unknown_result_fields"),
    (lambda v: v.update(hits="SYNTHETIC-PRIVATE"), "invalid_public_metadata"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(private_field="SYNTHETIC-PRIVATE"), "unknown_or_missing_article_fields"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(url={"hidden": "SYNTHETIC-PRIVATE"}), "unknown_or_missing_article_fields"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(content={"hidden": "SYNTHETIC-PRIVATE"}), "non_string_article_field"),
])
def test_rejected_original_has_no_projection_or_secret_receipt(tmp_path, change, reason):
    result = capture(tmp_path, mutate=change)
    assert result["checks"]["retention"]["reason"] == reason
    assert result["checks"]["parser"]["status"] == "not_started"
    assert not result.get("artifact_sha256")
    assert not result["capture_passed"] and not result["body_saved"] and not result["projection_saved"]
    assert not (tmp_path / "public-projection.txt").exists()
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("text", [
    "token=SYNTHETIC-PRIVATE", "Authorization: Bearer SYNTHETIC-PRIVATE",
    "cookie=SYNTHETIC-PRIVATE", "sk-SYNTHETIC-PRIVATE", "https://synthetic.invalid/private",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_sensitive_article_text_is_never_retained(tmp_path, text):
    result = capture(tmp_path, mutate=lambda v: v["result"]["cmsArticleWebOld"][0].update(content=text))
    assert result["checks"]["retention"]["reason"] == "sensitive_article_text"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert not result.get("artifact_sha256")
    assert not result["body_saved"]
    assert text not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("close_failure", [False, True])
@pytest.mark.parametrize("problem,stage,reason", [
    ("sensitive", "retention", "sensitive_article_text"),
    ("unknown", "retention", "unknown_envelope_fields"),
    ("invalid", "decode", "wrapper_invalid"),
    ("oversize", "decode", "response_limit_or_type"),
    ("read", "decode", "body_read_exception"),
    ("write", "retention", "body_write_failed"),
])
def test_rejected_response_stops_real_route_before_parser(
        tmp_path, monkeypatch, problem, stage, reason, close_failure):
    calls, closes, parses = [], [], []

    def forbidden_parser(*args, **kwargs):
        parses.append(True)
        raise AssertionError("rejected response reached business parser")

    monkeypatch.setattr(diagnostic.eastmoney_news, "parse_news_page", forbidden_parser)
    if problem == "write":
        (tmp_path / diagnostic.BODY_NAME).write_bytes(b"existing synthetic evidence")

    def get(url, *, params, **kwargs):
        calls.append(True)
        def mutate(value):
            if problem == "sensitive":
                value["result"]["cmsArticleWebOld"][0]["content"] = "token=SYNTHETIC-PRIVATE"
            elif problem == "unknown":
                value["unreviewed"] = "SYNTHETIC-PRIVATE"
        body = synthetic_get(dates=["2026-10-10"], mutate=mutate)(url, params=params).content
        if problem == "invalid":
            body = b"bad"
        elif problem == "oversize":
            body = b"x" * (diagnostic.LIMIT + 1)

        class Response:
            status_code = 200

            @property
            def content(self):
                if problem == "read":
                    raise OSError("SYNTHETIC-PRIVATE body read")
                return body

            def close(self):
                closes.append(True)
                if close_failure:
                    raise OSError("SYNTHETIC-PRIVATE cleanup")

        return Response()

    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH",
                                get=get, start=START, end=END)
    assert calls == closes == [True] and parses == []
    assert result["request_count"] == 1
    assert result["checks"][stage]["reason"] == reason
    assert result["checks"]["parser"]["status"] == "not_started"
    assert result["checks"]["production"]["status"] == "failed"
    assert not result["body_saved"] and not result["capture_passed"]
    assert not result.get("artifact_sha256") and not result.get("adapter_frame_sha256")
    primary = [event["payload"] for event in result["diagnostics"]
               if event["payload"].get("role") == "primary"]
    expected_stage = "body_read" if problem == "read" else stage
    expected_type = {"read": "OSError", "write": "FileExistsError"}.get(problem, "ValueError")
    assert primary and all(event["stage"] == expected_stage for event in primary)
    assert all(event["error_type"] == expected_type and event["http_status"] == 200 for event in primary)
    first_exception = next(event for event in result["diagnostics"] if event["kind"] == "news_exception")
    assert first_exception["payload"]["role"] == "primary"
    cleanup = [event for event in result["diagnostics"]
               if event["payload"].get("role") == "cleanup"]
    assert len(cleanup) == int(close_failure)
    if close_failure:
        assert cleanup[0]["payload"]["stage"] == "response_close"
    if problem == "write":
        assert (tmp_path / diagnostic.BODY_NAME).read_bytes() == b"existing synthetic evidence"
    else:
        assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("field", ["content", "mediaName"])
def test_optional_parser_fields_do_not_relax_exact_retention(tmp_path, field):
    def mutate(value):
        for row in value["result"]["cmsArticleWebOld"]:
            row.pop(field)
    page = diagnostic.eastmoney_news.parse_news_page(
        synthetic_get(mutate=mutate)(None, params={"cb": "synthetic"}).content,
        expected_callback="synthetic", ticker="600895", start_date=START, end_date=END)
    assert len(page.rows) == 1  # Still optional for the parser, required for retention.
    result = capture(tmp_path, mutate=mutate)
    assert result["checks"]["retention"]["status"] == "failed"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert result["checks"]["production"]["status"] == "failed"
    assert not result.get("artifact_sha256")
    assert not result["capture_passed"]


@pytest.mark.parametrize("dates,counts", [
    ([], (0, 0, 0)), (["bad-date"], (1, 0, 0)), (["2026-10-13"], (0, 1, 0)),
    (["2026-10-12T16:00:00Z"], (0, 1, 0)),
])
def test_no_usable_articles_keeps_date_exclusions(tmp_path, dates, counts):
    result = capture(tmp_path, dates=dates)
    assert result["checks"]["parser"]["reason"] == "no_usable_articles"
    assert result["selection"]["accepted_count"] == 0
    assert result["selection"]["excluded_counts"] == dict(zip(diagnostic.EXCLUSIONS, (0, *counts), strict=True))
    assert not result["capture_passed"]


def test_shanghai_cutoff_and_visible_stock_association(tmp_path):
    result = capture(tmp_path, dates=["2026-10-12T15:59:59Z", "2026-10-12T16:00:00Z", "2026-10-10"],
                     mutate=lambda v: v["result"]["cmsArticleWebOld"][2].update(
                         title="600919 unrelated", content="query keyword is insufficient"))
    assert result["selection"]["excluded_counts"] == {"malformed": 0, "undated": 0, "outside_window": 1, "wrong_stock": 1}
    assert result["selection"]["accepted_count"] == 1


def test_local_keyword_does_not_prove_stock_association():
    body = synthetic_get(dates=["2026-10-10"], mutate=lambda v:
                         v["result"]["cmsArticleWebOld"][0].update(
                             title="600919 unrelated", keyword="600895"))(None, params={"cb": "synthetic"}).content
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError) as error:
        diagnostic.eastmoney_news.parse_news_page(
            body, expected_callback="synthetic", ticker="600895", start_date=START, end_date=END)
    assert error.value.reason == "no_usable_articles"
    assert error.value.excluded_counts["wrong_stock"] == 1


def test_raw_count_and_structure_are_observed_and_bounded(tmp_path):
    result = capture(tmp_path, dates=["2026-10-10"] * 11,
                     extra={"SYNTHETIC-PRIVATE-" + str(i): "SYNTHETIC-PRIVATE" for i in range(40)})
    assert result["raw_count"] == 11
    assert result["structure"]["envelope"]["truncated"]
    assert len(result["structure"]["envelope"]["fields"]) == 32
    assert result["structure"]["articles"]["truncated"]
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()
    assert not result["body_saved"]


def test_production_decoder_preserves_bom_whitespace_callback_and_semicolon_bytes(tmp_path):
    bodies = []
    def get(url, *, params, **kwargs):
        response = synthetic_get()(url, params=params, **kwargs)
        body = b"\xef\xbb\xbf \n" + response.content.replace(params["cb"].encode(), b"otherCallback", 1) + b" ; \r\n"
        bodies.append(body)
        class Response:
            status_code = 200
            content = body
            @property
            def text(self):
                raise AssertionError("Response.text must not be consulted")
            def close(self):
                pass
        return Response()
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert result["capture_passed"] and result["callback_matches"] is False
    assert (tmp_path / diagnostic.BODY_NAME).read_bytes() == bodies[0]
    assert replay(tmp_path)["acceptance_passed"]


@pytest.mark.parametrize("body,reason", [
    (b"bad", "wrapper_invalid"), (b"\xff", "encoding_invalid"),
    (b'cb({"result":{},"result":{}})', "duplicate_json_key"),
    (b'cb({"value":NaN})', "nonfinite_json_number"),
    (b'cb({}); evil()', "trailing_syntax"),
])
def test_invalid_bytes_have_safe_parser_failure_and_no_retention(tmp_path, body, reason):
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError) as error:
        diagnostic.eastmoney_news.parse_news_page(
            body, expected_callback="synthetic", ticker="600895", start_date=START, end_date=END)
    assert error.value.reason == reason
    closed = []
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=lambda *a, **k: SimpleNamespace(
        status_code=200, content=body, close=lambda: closed.append(True)), start=START, end=END)
    assert result["checks"]["decode"]["reason"] == reason
    assert result["checks"]["parser"]["status"] == "not_started"
    assert not result["body_saved"] and closed == [True]


def test_response_limit_and_body_read_failures_preserve_close(tmp_path):
    closed = []
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=lambda *a, **k: SimpleNamespace(
        status_code=200, content=b" " * (diagnostic.LIMIT + 1), close=lambda: closed.append(True)), start=START, end=END)
    assert result["checks"]["decode"]["reason"] == "response_limit_or_type"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert closed == [True] and not result["body_saved"]


@pytest.mark.parametrize("status", [301, 403, 429, 500])
def test_http_failures_are_closed_and_do_not_claim_parser_success(tmp_path, status):
    closed = []
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=lambda *a, **k: SimpleNamespace(
        status_code=status, close=lambda: closed.append(True)), start=START, end=END)
    assert result["checks"]["http"]["status"] == "failed"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert closed == [True] and not result["capture_passed"]


def test_transport_exception_message_and_dynamic_class_name_are_not_retained(tmp_path):
    def get(*args, **kwargs):
        raise type("SYNTHETIC_PRIVATE_CLASS", (RuntimeError,), {})("SYNTHETIC-PRIVATE")
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert result["checks"]["network"]["reason"] == "transport_exception"
    assert result["checks"]["network"]["exception_type"] == "OtherError"
    assert "SYNTHETIC" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("name", ["public-response.bin", "capture.json"])
def test_existing_evidence_is_preserved_on_write_failure(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"existing evidence")
    result = capture(tmp_path)
    assert not result["capture_passed"]
    assert path.read_bytes() == b"existing evidence"


@pytest.mark.parametrize("field", ["source_bindings", "request_fingerprint", "window", "adapter_frame_sha256", "artifact_sha256"])
def test_replay_rejects_binding_tampering(tmp_path, field):
    capture(tmp_path)
    path = tmp_path / "capture.json"
    value = json.loads(path.read_text())
    value[field] = {"changed": True} if field in {"source_bindings", "window"} else "0" * 64
    path.write_text(json.dumps(value))
    result = replay(tmp_path)
    assert not result["acceptance_passed"]
    assert result["checks"]["replay"]["reason"] == field + "_mismatch"


@pytest.mark.parametrize("mutation", ["body", "missing-body", "legacy", "projection-only"])
def test_replay_rejects_changed_missing_or_historical_inputs(tmp_path, mutation):
    capture(tmp_path)
    if mutation == "body":
        with (tmp_path / diagnostic.BODY_NAME).open("ab") as handle:
            handle.write(b" ")
    elif mutation == "missing-body":
        (tmp_path / diagnostic.BODY_NAME).unlink()
    else:
        path = tmp_path / "capture.json"
        value = json.loads(path.read_text())
        if mutation == "legacy":
            value.pop("contract")
        else:
            value.update(body_saved=False, projection_saved=True, projection_replay_passed=True)
        path.write_text(json.dumps(value))
    assert not replay(tmp_path)["acceptance_passed"]


@pytest.mark.parametrize("stage", ["network", "http", "decode", "retention", "parser", "adapter", "production", "receipt"])
@pytest.mark.parametrize("change", ["failed", "missing"])
def test_failed_or_missing_source_checks_cannot_be_rescued_by_replay(tmp_path, stage, change):
    capture(tmp_path)
    path = tmp_path / "capture.json"
    value = json.loads(path.read_text())
    if change == "missing":
        value["checks"].pop(stage)
    else:
        value["checks"][stage] = {"status": "failed"}
    path.write_text(json.dumps(value))
    assert not replay(tmp_path)["acceptance_passed"]


def test_attempt_budget_stays_one(tmp_path, monkeypatch):
    original = diagnostic.akshare_data._stock_news_em
    def duplicate(symbol, start, end, *, instrument_aliases=()):
        original(symbol, start, end, instrument_aliases=instrument_aliases)
        return original(symbol, start, end, instrument_aliases=instrument_aliases)
    monkeypatch.setattr(diagnostic.akshare_data, "_stock_news_em", duplicate)
    result = capture(tmp_path)
    assert result["request_count"] == 1
    assert result["checks"]["network"]["reason"] == "request_scope_or_budget"
    assert not result["capture_passed"]


def _news_cli(monkeypatch, output, *, source=None, mode="original-text"):
    from scripts import diagnose_public_news as cli
    args = ["diagnose_public_news.py", "--output", str(output), "--start", START,
            "--end", END, "--acceptance-mode", mode, "--ticker", "600895.SH", "--transport", "direct"]
    args += ["--replay", str(source)] if source else ["--authorize-one-public-get-and-save"]
    with monkeypatch.context() as scoped:
        scoped.setattr(cli, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True))
        scoped.setattr(cli, "SOURCE", output.parent / "synthetic-source")
        scoped.setattr(cli, "install", lambda *a, **k: ({}, []))
        scoped.setattr(sys, "argv", args)
        code = cli.main()
    return code, json.loads((output / "result.json").read_text())


@pytest.mark.parametrize("mode", ["original-text", "news-content"])
@pytest.mark.parametrize("phase", ["capture", "replay"])
@pytest.mark.parametrize("marker", [None, {"code": 0}, {"success": False}])
def test_cli_modes_preserve_production_and_retention_failure(tmp_path, monkeypatch, mode, phase, marker):
    import requests
    get = synthetic_get(extra=marker)
    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **k: get(*a, **k))
    source = None
    if phase == "replay":
        source = tmp_path / "input"
        source.mkdir()
        captured = diagnostic.capture(source, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
        if not captured["body_saved"]:
            with pytest.raises(SystemExit) as error:
                _news_cli(monkeypatch, tmp_path / "output", source=source, mode=mode)
            assert error.value.code == 2
            return
    code, result = _news_cli(monkeypatch, tmp_path / "output", source=source, mode=mode)
    assert code == (0 if marker is None else 1)
    assert result["acceptance"]["passed"] is (marker is None)
    assert not result["projection_replay_passed"]


def test_cli_invalid_mode_fails_before_output(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as error:
        _news_cli(monkeypatch, tmp_path / "output", mode="all-success")
    assert error.value.code == 2
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("matches", [True, False])
def test_cli_stdout_shows_callback_boolean_without_rescuing_unrelated_collection(tmp_path, monkeypatch, capsys, matches):
    import requests

    def get(_session, _url, *, params, **kwargs):
        callback = params["cb"] if matches else "syntheticOtherCallback"
        # Synthetic shape only; the real manual response bytes were not retained.
        body = (callback + "(" + json.dumps({"code": 0, "result": {"passportWeb": [{}]}}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, close=lambda: None)

    monkeypatch.setattr(requests.Session, "get", get)
    code, result = _news_cli(monkeypatch, tmp_path / "output", mode="news-content")
    summary = json.loads(capsys.readouterr().out)
    assert summary["callback_matches"] is matches
    assert summary["callback_matches"] is result["callback_matches"]
    assert code == 1 and not summary["acceptance"]["passed"]
    assert summary["checks"]["retention"]["reason"] == "articles_missing"
    assert summary["checks"]["parser"]["status"] == "not_started"
    assert summary["selection"]["status"] == "not_observed"
    assert summary["selection"]["coverage"] == "unknown"
    assert not summary["body_saved"] and not summary["projection_saved"]
    assert "syntheticOtherCallback" not in json.dumps(summary)


def test_cli_stdout_callback_is_unknown_before_response_decode(tmp_path, monkeypatch, capsys):
    import requests

    def get(*args, **kwargs):
        raise requests.exceptions.ConnectionError("SYNTHETIC-PRIVATE")

    monkeypatch.setattr(requests.Session, "get", get)
    code, result = _news_cli(monkeypatch, tmp_path / "output")
    printed = capsys.readouterr().out
    summary = json.loads(printed)
    assert code == 1
    assert summary["callback_matches"] is None and result.get("callback_matches") is None
    assert summary["request_count"] == 1 and not summary["capture_passed"]
    assert "SYNTHETIC-PRIVATE" not in printed


def test_retained_digest_is_original_bytes(tmp_path):
    result = capture(tmp_path)
    assert result["saved_body_sha256"] == hashlib.sha256((tmp_path / diagnostic.BODY_NAME).read_bytes()).hexdigest()


@pytest.mark.parametrize("start,end", [("SYNTHETIC-PRIVATE", END), (END, START), (None, END)])
def test_invalid_date_request_is_rejected_before_transport_and_not_retained(tmp_path, start, end):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid date must not reach supplied transport")
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=forbidden, start=start, end=end)
    assert result["request_count"] == 0
    assert result["checks"]["parser"]["reason"] == "request_invalid"
    assert result["window"] == {"start": None, "end": None}
    assert "SYNTHETIC" not in (tmp_path / "capture.json").read_text()


def test_body_read_failure_keeps_safe_stage_and_closes_response(tmp_path):
    closed = []
    class Response:
        status_code = 200
        @property
        def content(self):
            raise OSError("SYNTHETIC-PRIVATE")
        def close(self):
            closed.append(True)
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=lambda *a, **k: Response(), start=START, end=END)
    assert result["checks"]["decode"]["reason"] == "body_read_exception"
    assert closed == [True] and not result["capture_passed"]
    assert "SYNTHETIC" not in (tmp_path / "capture.json").read_text()


def test_close_error_does_not_erase_primary_business_status_failure(tmp_path):
    def get(url, *, params, **kwargs):
        response = synthetic_get(extra={"code": 0})(url, params=params, **kwargs)
        def close():
            raise OSError("SYNTHETIC-PRIVATE")
        response.close = close
        return response
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert result["checks"]["parser"]["reason"] == "business_status_unverified"
    events = [x["payload"] for x in result["diagnostics"] if x["kind"] == "news_exception"]
    assert any(x["role"] == "primary" and x["error_code"] == "news_business_status_unverified" for x in events)
    assert any(x["role"] == "cleanup" and x["stage"] == "response_close" for x in events)
    assert not result["capture_passed"]


def test_selection_counter_saturation_is_explicit():
    value = diagnostic._observation(START, END, retain=True)
    diagnostic._observe_selection(value, dict.fromkeys(diagnostic.EXCLUSIONS, 70000), 70000)
    assert value["selection"]["counts_saturated"]
    assert value["selection"]["accepted_count"] == 65535
    assert set(value["selection"]["excluded_counts"].values()) == {65535}


def test_raw_count_saturation_is_explicit(tmp_path):
    def get(url, *, params, **kwargs):
        body = (params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": [{}] * 65536}}) + ")").encode()
        return SimpleNamespace(status_code=200, content=body, close=lambda: None)
    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert result["raw_count"] == 65535 and result["raw_count_saturated"] is True
    assert result["selection"]["status"] == "not_observed"
    assert result["selection"]["excluded_counts"] is None
    assert not result["body_saved"] and not result["capture_passed"]


def test_cli_read_replay_input_preserves_exact_original(tmp_path):
    from scripts import diagnose_public_news as cli
    path = tmp_path / "public-response.bin"
    data = b"synthetic exact bytes\r\n"
    path.write_bytes(data)
    assert cli._read_replay_input(tmp_path, path.name) == data


def test_cli_rejects_original_hardlink_before_copy(tmp_path):
    from scripts import diagnose_public_news as cli
    original = tmp_path / "original"
    original.write_bytes(b"synthetic input")
    linked = tmp_path / "public-response.bin"
    os.link(original, linked)
    with pytest.raises(ValueError, match="invalid retained input"):
        cli._read_replay_input(tmp_path, linked.name)
    assert original.read_bytes() == b"synthetic input"


@pytest.mark.parametrize("target", ["root", "file"])
def test_cli_rejects_original_reparse_metadata_before_open(tmp_path, monkeypatch, target):
    from scripts import diagnose_public_news as cli
    path = tmp_path / "public-response.bin"
    path.write_bytes(b"synthetic input")
    actual = Path.lstat
    selected = tmp_path if target == "root" else path
    def lstat(item):
        result = actual(item)
        if item == selected:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=1024)
        return result
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="linked retained input"):
        cli._read_replay_input(tmp_path, path.name)


def test_cli_rejects_handle_identity_mismatch_before_read(tmp_path, monkeypatch):
    from scripts import diagnose_public_news as cli
    path = tmp_path / "public-response.bin"
    path.write_bytes(b"synthetic input")
    actual = os.fstat
    def mismatch(fd):
        info = actual(fd)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                               st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                               st_nlink=info.st_nlink, st_mode=info.st_mode)
    monkeypatch.setattr(os, "fstat", mismatch)
    with pytest.raises(ValueError, match="changed retained input"):
        cli._read_replay_input(tmp_path, path.name)
