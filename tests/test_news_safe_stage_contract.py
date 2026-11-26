"""Production news failure stages with synthetic transports and no response retention."""
import json
from types import SimpleNamespace

import pytest
import requests

from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.run_diagnostics import diagnostic_scope


@pytest.mark.parametrize("error", [requests.ReadTimeout("private-sentinel"), OSError("private-sentinel")])
def test_request_failure_is_single_and_sanitized(monkeypatch, error):
    calls, events = [], []

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(akshare_data.requests, "get", fail)
    with diagnostic_scope(events.append), pytest.raises(type(error)) as caught:
        akshare_data._stock_news_em("600895", "2026-11-06", "2026-11-13")
    assert caught.value is error and calls == [1]
    errors = [row for row in events if row["kind"] == "news_exception"]
    assert [(row["payload"]["stage"], row["payload"]["role"], row["payload"]["http_status"])
            for row in errors] == [("http_request", "primary", None)]
    assert "private-sentinel" not in json.dumps(events)


def test_cleanup_failure_preserves_primary_http_failure(monkeypatch):
    calls, events = [], []

    def close():
        calls.append("close")
        raise OSError("private-sentinel")

    def response(*args, **kwargs):
        calls.append("get")
        return SimpleNamespace(status_code=503, close=close)

    monkeypatch.setattr(akshare_data.requests, "get", response)
    with diagnostic_scope(events.append), pytest.raises(AcquisitionFailure):
        akshare_data._stock_news_em("600895", "2026-11-06", "2026-11-13")
    errors = [row["payload"] for row in events if row["kind"] == "news_exception"]
    assert calls == ["get", "close"]
    assert [row["role"] for row in errors] == ["primary", "cleanup"]
    assert errors[-1]["stage"] == "response_close"
    assert "private-sentinel" not in json.dumps(events)
