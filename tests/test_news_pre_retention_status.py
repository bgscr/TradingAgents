"""Synthetic metadata-only status observations before the news retention gate."""

import json
from types import SimpleNamespace

import pytest
import requests

from tests.test_news_diagnostic import END, START, _news_cli, synthetic_get
from tradingagents import news_diagnostic as diagnostic, run_diagnostics
from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.acquisition import AcquisitionFailure


def rejected_get(payload, calls, closes, cleanup=None):
    def get(*args, **kwargs):
        calls.append(True)

        def close():
            closes.append(True)
            if cleanup is not None:
                raise cleanup

        return SimpleNamespace(status_code=200, close=close,
                               content=("differentCallback(" + json.dumps(payload) + ")").encode())

    return get


@pytest.mark.parametrize("status,expected", [
    ("FAILED", "failed"),
    ("token=SYNTHETIC-PRIVATE", "other"),
    ("https://synthetic.invalid/?token=SYNTHETIC-PRIVATE", "other"),
])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_rejected_body_keeps_only_closed_status_before_retention(tmp_path, monkeypatch, status, expected, cleanup_fails):
    calls, closes = [], []
    payload = {"code": 0, "status": status, "success": False,
               "error": {"token": "SYNTHETIC-PRIVATE"},
               "SYNTHETIC-PRIVATE-FIELD": "SYNTHETIC-PRIVATE",
               "result": {"syntheticOther": [{"content": "SYNTHETIC-PRIVATE"}]}}

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("rejected body reached business parsing")

    monkeypatch.setattr(diagnostic.eastmoney_news, "parse_news_page", forbidden_parse)
    cleanup = OSError("Cookie: SYNTHETIC-PRIVATE") if cleanup_fails else None
    result = diagnostic.capture(tmp_path, get=rejected_get(payload, calls, closes, cleanup),
                                ticker="600895.SH", start=START, end=END, transport="direct")
    observations = [event for event in result["diagnostics"] if event["kind"] == "news_business_status"]
    assert len(observations) == 1
    event = observations[0]
    assert event["qualification_authority"] is False
    assert event["payload"] == {
        "stage": "decode", "endpoint": "eastmoney_search_news", "contract": "news-business-status-diagnostic-v1",
        "fields": [
            {"scope": "root", "field": "code", "type": "integer", "value_class": "zero"},
            {"scope": "root", "field": "status", "type": "string", "value_class": expected},
            {"scope": "root", "field": "success", "type": "boolean", "value_class": "false"},
            {"scope": "root", "field": "error", "type": "object", "value_class": "other"},
        ], "semantic_status": "metadata_only_not_provider_success",
    }
    assert result["stage"] == "retention" and result["http_status"] == 200
    assert result["checks"]["retention"]["reason"] == "articles_missing"
    assert result["checks"]["parser"]["status"] == "not_started"
    errors = [event["payload"] for event in result["diagnostics"] if event["kind"] == "news_exception"]
    assert errors[0]["role"] == "primary" and errors[0]["stage"] == "retention"
    assert len([error for error in errors if error["role"] == "cleanup"]) == int(cleanup_fails)
    assert calls == closes == [True] and result["request_count"] == 1
    assert not result["body_saved"] and not result.get("artifact_sha256")
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_optional_classifier_error_cannot_replace_retention_failure(tmp_path, monkeypatch, cleanup_fails):
    calls, closes, stages = [], [], []
    original = run_diagnostics.record_news_business_status

    def broken(payload, result, *, stage="response_parse"):
        stages.append(stage)
        if stage == "decode":
            raise RuntimeError("token=SYNTHETIC-PRIVATE")
        return original(payload, result, stage=stage)

    monkeypatch.setattr(run_diagnostics, "record_news_business_status", broken)
    cleanup = OSError("Cookie: SYNTHETIC-PRIVATE") if cleanup_fails else None
    result = diagnostic.capture(tmp_path, get=rejected_get({"code": 0, "result": {}}, calls, closes, cleanup),
                                ticker="600895.SH", start=START, end=END, transport="direct")
    assert stages == ["decode"] and calls == closes == [True]
    assert result["stage"] == "retention" and result["checks"]["retention"]["reason"] == "articles_missing"
    assert result["checks"]["parser"]["status"] == "not_started" and not result["body_saved"]
    assert not any(e["kind"] == "news_business_status" for e in result["diagnostics"])
    errors = [e["payload"] for e in result["diagnostics"] if e["kind"] == "news_exception"]
    assert errors[0]["role"] == "primary" and errors[0]["stage"] == "retention"
    assert all(e["error_type"] == "ValueError" for e in errors if e["role"] == "primary")
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("cleanup", [None, OSError("Cookie: SYNTHETIC-PRIVATE"),
                                     run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")])
def test_status_persistence_failure_stays_terminal_and_closes_once(tmp_path, monkeypatch, cleanup):
    calls, closes, attempts = [], [], []
    first = run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")
    original = run_diagnostics.emit

    def fail_status(kind, payload):
        attempts.append((kind, payload))
        if kind == "news_business_status":
            raise first
        return original(kind, payload)

    monkeypatch.setattr(run_diagnostics, "emit", fail_status)
    with pytest.raises(run_diagnostics.DiagnosticPersistenceError) as caught:
        diagnostic.capture(tmp_path, get=rejected_get({"code": 0, "result": {}}, calls, closes, cleanup),
                           ticker="600895.SH", start=START, end=END, transport="direct")
    assert caught.value is first and calls == closes == [True]
    assert attempts[0][0] == "news_business_status" and attempts[0][1]["stage"] == "decode"
    assert all(payload.get("role") == "cleanup" for kind, payload in attempts[1:])
    assert "SYNTHETIC-PRIVATE" not in json.dumps(attempts)
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert not (tmp_path / "capture.json").exists()


def test_decode_failure_never_invokes_status_classifier(tmp_path, monkeypatch):
    calls, closes = [], []

    def forbidden(*args, **kwargs):
        raise AssertionError("classifier called without decoded object")

    def get(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, content=b"not-json", close=lambda: closes.append(True))

    monkeypatch.setattr(run_diagnostics, "record_news_business_status", forbidden)
    result = diagnostic.capture(tmp_path, get=get, ticker="600895.SH", start=START, end=END, transport="direct")
    assert result["stage"] == "decode" and calls == closes == [True] and not result["body_saved"]
    assert not any(e["kind"] == "news_business_status" for e in result["diagnostics"])


def test_production_status_stage_and_business_rejection_unchanged(monkeypatch):
    calls, closes, events = [], [], []
    monkeypatch.setattr(akshare_data.requests, "get", rejected_get({"code": 0, "result": {}}, calls, closes))
    with run_diagnostics.diagnostic_scope(events.append), pytest.raises(AcquisitionFailure) as caught:
        akshare_data._stock_news_em("600895", START, END)
    assert caught.value.error_code == "news_business_status_unverified"
    statuses = [e["payload"] for e in events if e["kind"] == "news_business_status"]
    assert len(statuses) == 1 and statuses[0]["stage"] == "response_parse"
    assert calls == closes == [True]


def test_safe_retained_code_zero_still_cannot_become_available(tmp_path):
    result = diagnostic.capture(tmp_path, get=synthetic_get(extra={"code": 0}),
                                ticker="600895.SH", start=START, end=END, transport="direct")
    stages = [e["payload"]["stage"] for e in result["diagnostics"] if e["kind"] == "news_business_status"]
    assert stages == ["decode", "response_parse"]
    assert result["body_saved"] and not result["capture_passed"] and not result.get("artifact_sha256")
    assert result["checks"]["parser"]["reason"] == "business_status_unverified"


def test_cli_exposes_only_decode_status_and_keeps_original_refusal(tmp_path, monkeypatch, capsys):
    calls, closes = [], []
    fake_get = rejected_get({"code": 0, "msg": "SYNTHETIC-PRIVATE", "result": {}}, calls, closes)
    monkeypatch.setattr(requests.Session, "get", lambda self, *args, **kwargs: fake_get(*args, **kwargs))
    code, result = _news_cli(monkeypatch, tmp_path / "output", mode="news-content")
    output = capsys.readouterr().out
    assert code == 1 and result["stage"] == "retention" and result["request_count"] == 1
    assert result["checks"]["parser"]["status"] == "not_started" and not result["body_saved"]
    statuses = [e for e in result["diagnostics"] if e["kind"] == "news_business_status"]
    assert len(statuses) == 1 and statuses[0]["payload"]["stage"] == "decode"
    assert calls == closes == [True] and "SYNTHETIC-PRIVATE" not in output + json.dumps(result)


def test_shared_classifier_keeps_eight_closed_fields_without_short_secret_values():
    events = []
    payload = {"code": 1, "status": "token=PRIVATE", "success": None,
               "error": "Bearer PRIVATE", "PRIVATE-FIELD": "PRIVATE"}
    result = {"code": 200, "status": " FAILED ", "success": True, "error": False}
    with run_diagnostics.diagnostic_scope(events.append):
        run_diagnostics.record_news_business_status(payload, result, stage="decode")
    fields = events[0]["payload"]["fields"]
    assert len(events) == 1 and len(fields) == 8
    assert [field["value_class"] for field in fields] == [
        "one", "other", "null", "other", "http_ok", "failed", "true", "false"]
    assert {field["field"] for field in fields} == {"code", "status", "success", "error"}
    assert all(set(field) == {"scope", "field", "type", "value_class"} for field in fields)
    assert "PRIVATE" not in json.dumps(events) and events[0]["qualification_authority"] is False


@pytest.mark.parametrize("stage", ["token=PRIVATE", True, ["decode"]])
def test_status_stage_is_closed_and_never_echoed(stage):
    events = []
    with run_diagnostics.diagnostic_scope(events.append), pytest.raises(ValueError) as caught:
        run_diagnostics.record_news_business_status({"code": 0}, {}, stage=stage)
    assert str(caught.value) == "invalid_news_business_status_stage" and events == []


def test_optional_classifier_failure_does_not_reject_safe_normal_response(tmp_path, monkeypatch):
    stages = []
    original = run_diagnostics.record_news_business_status

    def broken(payload, result, *, stage="response_parse"):
        stages.append(stage)
        if stage == "decode":
            raise RuntimeError("token=PRIVATE")
        return original(payload, result, stage=stage)

    monkeypatch.setattr(run_diagnostics, "record_news_business_status", broken)
    result = diagnostic.capture(tmp_path, get=synthetic_get(), ticker="600895.SH",
                                start=START, end=END, transport="direct")
    assert stages == ["decode", "response_parse"]
    assert result["capture_passed"] and result["artifact_sha256"] and result["request_count"] == 1
    assert "PRIVATE" not in (tmp_path / "capture.json").read_text()
