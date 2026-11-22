"""Synthetic failure provenance through production and diagnostic boundaries."""

import json
from types import SimpleNamespace

import pytest
import requests

from tests.test_news_diagnostic import END, START, synthetic_get
from tradingagents import news_diagnostic as diagnostic
from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.run_diagnostics import diagnostic_scope


@pytest.mark.parametrize("error", [requests.exceptions.ReadTimeout("token=SYNTHETIC"),
                                  OSError(10013, "https://synthetic.invalid/?token=SYNTHETIC")])
@pytest.mark.parametrize("path", ["production", "diagnostic"])
def test_network_failure_keeps_transport_stage_without_close_or_retry(tmp_path, monkeypatch, error, path):
    calls, events = [], []

    def fail(*args, **kwargs):
        calls.append(True)
        raise error

    if path == "production":
        monkeypatch.setattr(akshare_data.requests, "get", fail)
        with diagnostic_scope(events.append), pytest.raises(type(error)) as caught:
            akshare_data._stock_news_em("600895", START, END)
        assert caught.value is error
    else:
        result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH",
                                    get=fail, start=START, end=END)
        assert result["stage"] == "transport" and result["request_count"] == 1
        assert result["checks"]["parser"]["status"] == "not_started"
        assert not result["body_saved"] and "http_status" not in result
        events = result["diagnostics"]
        assert "SYNTHETIC" not in (tmp_path / "capture.json").read_text()
    assert calls == [True]
    primary = [e["payload"] for e in events if e["payload"].get("role") == "primary"]
    assert primary[0]["stage"] == "http_request"
    assert primary[0]["error_type"] == ("ReadTimeout" if type(error) is requests.exceptions.ReadTimeout else "OSError")
    assert all(e["http_status"] is None for e in primary)
    assert not any(e["payload"].get("role") == "cleanup" for e in events)
    assert "SYNTHETIC" not in json.dumps(events)


@pytest.mark.parametrize("failure", ["http", "parser", "none"])
def test_production_close_failure_cannot_replace_existing_primary(monkeypatch, failure):
    calls, closes, events = [], [], []
    cleanup = OSError("token=SYNTHETIC-cleanup")

    def get(url, *, params, **kwargs):
        calls.append(True)
        response = synthetic_get(extra={"code": 0} if failure == "parser" else None)(
            url, params=params)
        if failure == "http":
            response.status_code = 503

        def close():
            closes.append(True)
            raise cleanup

        response.close = close
        return response

    monkeypatch.setattr(akshare_data.requests, "get", get)
    with diagnostic_scope(events.append), pytest.raises(OSError if failure == "none" else AcquisitionFailure) as caught:
        akshare_data._stock_news_em("600895", START, END)
    if failure == "none":
        assert caught.value is cleanup
    else:
        assert caught.value.error_code == ("news_http_status" if failure == "http" else "news_business_status_unverified")
    errors = [e["payload"] for e in events if e["kind"] == "news_exception"]
    assert [e["role"] for e in errors] == (["cleanup"] if failure == "none" else ["primary", "cleanup"])
    assert errors[-1]["stage"] == "response_close"
    assert calls == closes == [True] and "SYNTHETIC" not in json.dumps(events)


def test_cli_retention_failure_is_local_even_when_cleanup_fails(tmp_path, monkeypatch, capsys):
    from tests.test_news_diagnostic import _news_cli

    calls, closes = [], []

    def get(session, url, *, params, **kwargs):
        calls.append(True)

        def close():
            closes.append(True)
            raise OSError("Cookie: SYNTHETIC")

        return SimpleNamespace(status_code=200, content=b'other({"code":0,"result":{}})', close=close)

    monkeypatch.setattr(requests.Session, "get", get)
    code, result = _news_cli(monkeypatch, tmp_path / "output", mode="news-content")
    summary = json.loads(capsys.readouterr().out)
    assert code == 1 and result["http_status"] == summary["http_status"] == 200
    assert result["stage"] == summary["stage"] == "retention"
    assert result["checks"]["retention"]["reason"] == "articles_missing"
    assert result["checks"]["parser"]["status"] == "not_started"
    errors = [e["payload"] for e in result["diagnostics"] if e["kind"] == "news_exception"]
    assert errors[0]["stage"] == "retention" and errors[0]["role"] == "primary"
    assert all(e["stage"] == "retention" and e["http_status"] == 200
               for e in errors if e["role"] == "primary")
    assert any(e["stage"] == "response_close" and e["role"] == "cleanup" for e in errors)
    assert calls == closes == [True] and not result["body_saved"]
    assert not (tmp_path / "output" / diagnostic.BODY_NAME).exists()
    assert "SYNTHETIC" not in (tmp_path / "output/capture.json").read_text()


@pytest.mark.parametrize("failed_records", [("primary",), ("cleanup",), ("primary", "cleanup")])
def test_rejection_record_failure_remains_terminal_and_closes_once(tmp_path, monkeypatch, failed_records):
    from tradingagents import run_diagnostics

    calls, closes, records = [], [], []
    failures = {role: run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")
                for role in failed_records}
    original_record = run_diagnostics.record_news_exception

    def record(error, *, stage, role="primary", **kwargs):
        records.append((role, stage))
        if role in failures:
            raise failures[role]
        return original_record(error, stage=stage, role=role, **kwargs)

    def close():
        closes.append(True)
        raise OSError("Cookie: SYNTHETIC")

    def get(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, content=b'other({"code":0,"result":{}})', close=close)

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("rejected response must not reach business parsing")

    monkeypatch.setattr(run_diagnostics, "record_news_exception", record)
    monkeypatch.setattr(diagnostic.eastmoney_news, "parse_news_page", forbidden_parse)
    with pytest.raises(run_diagnostics.DiagnosticPersistenceError) as caught:
        diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert caught.value is failures[failed_records[0]]
    assert calls == closes == [True]
    assert records == [("primary", "retention"), ("cleanup", "response_close")]
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert not (tmp_path / "capture.json").exists()


@pytest.mark.parametrize("failure", ["http", "parser", "none"])
def test_production_terminal_close_failure_is_never_downgraded(monkeypatch, failure):
    from tradingagents import run_diagnostics

    calls, closes, events = [], [], []
    terminal = run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")

    def get(url, *, params, **kwargs):
        calls.append(True)
        response = synthetic_get(extra={"code": 0} if failure == "parser" else None)(url, params=params)
        if failure == "http":
            response.status_code = 503

        def close():
            closes.append(True)
            raise terminal

        response.close = close
        return response

    monkeypatch.setattr(akshare_data.requests, "get", get)
    with diagnostic_scope(events.append), pytest.raises(run_diagnostics.DiagnosticPersistenceError) as caught:
        akshare_data._stock_news_em("600895", START, END)
    assert caught.value is terminal and calls == closes == [True]
    errors = [e["payload"] for e in events if e["kind"] == "news_exception"]
    assert [e["role"] for e in errors] == (["cleanup"] if failure == "none" else ["primary", "cleanup"])
    assert errors[-1]["stage"] == "response_close"


@pytest.mark.parametrize("primary_record_fails", [False, True])
def test_diagnostic_terminal_close_keeps_first_terminal(tmp_path, monkeypatch, primary_record_fails):
    from tradingagents import run_diagnostics

    calls, closes, records = [], [], []
    first = run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")
    cleanup = run_diagnostics.DiagnosticPersistenceError("diagnostic_persistence_failed")
    original_record = run_diagnostics.record_news_exception

    def record(error, *, stage, role="primary", **kwargs):
        records.append((role, stage))
        if role == "primary" and primary_record_fails:
            raise first
        return original_record(error, stage=stage, role=role, **kwargs)

    def close():
        closes.append(True)
        raise cleanup

    def get(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, content=b'other({"code":0,"result":{}})', close=close)

    monkeypatch.setattr(run_diagnostics, "record_news_exception", record)
    with pytest.raises(run_diagnostics.DiagnosticPersistenceError) as caught:
        diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=get, start=START, end=END)
    assert caught.value is (first if primary_record_fails else cleanup)
    assert calls == closes == [True]
    assert records == [("primary", "retention"), ("cleanup", "response_close")]
    assert not (tmp_path / diagnostic.BODY_NAME).exists()
    assert not (tmp_path / "capture.json").exists()
