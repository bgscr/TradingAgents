"""Synthetic business-status metadata only; no retained upstream body exists."""
import copy
import json

import pytest

from tradingagents.dataflows.eastmoney_news import NewsResponseError, parse_news_page
from tradingagents.run_diagnostics import DiagnosticPersistenceError, diagnostic_scope


def parse(payload):
    return parse_news_page(("cb(" + json.dumps(payload) + ");").encode(), expected_callback="cb",
                           ticker="601328", start_date="2026-10-01", end_date="2026-10-16")


def body():
    return {"result": {"cmsArticleWebOld": [{"title": "601328 synthetic headline", "code": "article001",
        "date": "2026-10-15 12:00:00", "content": "public synthetic fixture", "mediaName": "fixture"}]}}


def test_status_diagnostic_is_fixed_metadata_and_does_not_change_rejection():
    payload = body()
    payload["code"] = 0
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError, match="business_status_unverified"):
        parse(payload)
    events = [event for event in events if event["kind"] == "news_business_status"]
    assert len(events) == 1
    assert events[0]["kind"] == "news_business_status"
    assert events[0]["qualification_authority"] is False
    assert events[0]["payload"] == {"stage": "response_parse", "endpoint": "eastmoney_search_news",
        "contract": "news-business-status-diagnostic-v1", "fields": [
            {"scope": "root", "field": "code", "type": "integer", "value_class": "zero"}],
        "semantic_status": "metadata_only_not_provider_success"}


@pytest.mark.parametrize("scope,key,value,reason,kind,category", [
    ("root", "success", False, "business_failure", "boolean", "false"),
    ("result", "success", True, "business_status_unverified", "boolean", "true"),
    ("root", "status", "error", "business_failure", "string", "error"),
    ("result", "status", "FAILED", "business_failure", "string", "failed"),
    ("root", "status", "ok", "business_status_unverified", "string", "ok"),
    ("root", "code", 1, "business_status_unverified", "integer", "one"),
    ("result", "code", 200, "business_status_unverified", "integer", "http_ok"),
    ("root", "error", None, "business_status_unverified", "null", "null"),
    ("root", "code", 40101, "business_status_unverified", "integer", "other"),
    ("root", "error", {"token": "never-retain"}, "business_failure", "object", "other"),
    ("root", "code", ["never-retain"], "business_status_unverified", "array", "other"),
    ("root", "code", 0.5, "business_status_unverified", "number", "other"),
])
def test_fixed_status_types_preserve_original_failure(scope, key, value, reason, kind, category):
    payload = body()
    target = payload if scope == "root" else payload["result"]
    target[key] = value
    original = copy.deepcopy(payload)
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError, match=reason):
        parse(payload)
    assert payload == original
    status = next(event for event in events if event["kind"] == "news_business_status")
    assert status["payload"]["fields"] == [{"scope": scope, "field": key, "type": kind, "value_class": category}]
    assert "never-retain" not in json.dumps(events)


def test_arbitrary_values_keys_urls_and_article_text_are_never_retained():
    secret = "sk-" + "x" * 40
    payload = body()
    payload.update({"code": secret, "status": "https://example.invalid/private?token=" + secret,
                    "success": 123456789, "error": "private-person " + secret, "arbitrary-secret-key": secret})
    payload["result"].update({"code": secret, "status": {"private": secret}, "success": [secret], "error": ""})
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError):
        parse(payload)
    saved = json.dumps(events)
    assert secret not in saved and "example.invalid" not in saved and "private-person" not in saved
    assert "arbitrary-secret-key" not in saved and "123456789" not in saved
    assert "synthetic headline" not in saved
    status = next(event for event in events if event["kind"] == "news_business_status")
    assert len(status["payload"]["fields"]) == 8


def test_no_status_fields_preserves_success_and_unknown_coverage_with_shape_only():
    events = []
    with diagnostic_scope(events.append):
        page = parse(body())
    assert [event["kind"] for event in events] == ["news_response_shape", "news_selection"]
    assert page.business_status == "not_supplied" and page.coverage_status == "unknown"


def test_invalid_wrapper_still_fails_before_business_metadata():
    events = []
    with diagnostic_scope(events.append), pytest.raises(NewsResponseError, match="trailing_syntax"):
        parse_news_page(b"cb({}); alert(1)", expected_callback="cb", ticker="601328",
                        start_date="2026-10-01", end_date="2026-10-16")
    assert events == []


def test_diagnostic_persistence_failure_remains_terminal():
    payload = body()
    payload["code"] = 0

    def broken(_event):
        raise OSError("synthetic retention failure")

    with diagnostic_scope(broken), pytest.raises(DiagnosticPersistenceError):
        parse(payload)


def test_actual_parser_to_cli_retains_safe_profile_before_closing_writer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_run_diagnostic_retention import capture, events
    from tradingagents.dataflows import akshare_data
    from tradingagents.dataflows.acquisition import AcquisitionFailure

    payload = body()
    payload.update({"code": 0, "status": "synthetic-private-never-retain"})
    wire = ("cb(" + json.dumps(payload) + ");").encode()
    calls, closed = [], []

    def offline_response(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, content=wire, close=lambda: closed.append(True))

    monkeypatch.setattr(akshare_data.requests, "get", offline_response)
    scope, writer, artifacts = capture(tmp_path)
    try:
        with scope, pytest.raises(AcquisitionFailure) as caught:
            akshare_data._stock_news_em("601328", "2026-10-01", "2026-10-16")
        assert caught.value.error_code == "news_business_status_unverified"
        retained = events(artifacts)
        assert len([e for e in retained if e["kind"] == "news_business_status"]) == 1
        assert len([e for e in retained if e["kind"] == "news_exception"]) == 1
        assert "synthetic-private-never-retain" not in json.dumps(retained)
        assert calls == closed == [True]
    finally:
        writer.close()


def test_cli_classifies_business_status_metadata_as_critical():
    import cli.main as cli

    classified = []

    class Writer:
        def record_diagnostic(self, timestamp, event, *, critical):
            classified.append(critical)

    payload = body() | {"code": 0}
    with cli._run_diagnostic_scope({"runtime_writer": Writer()}), pytest.raises(NewsResponseError):
        parse(payload)
    assert classified == [True, True]
