"""Synthetic transports only: headers survive body failure; raw scalar types survive."""

import asyncio
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd
import pytest
from langchain_core.messages import HumanMessage
from openai import APIConnectionError
from test_current_tushare_statements import (
    Clock,
    dispatcher,
    request,
    runtime_config,
    statement_rows,
)
from test_deepseek_high_budget import budget, client
from test_tushare_financial_https import fake_connection

from tradingagents import (
    financial_response_completeness as assessment,
    tushare_financial_https as financial_transport,
)
from tradingagents.dataflows._tushare_statement_artifacts import encode_provider_artifact
from tradingagents.dataflows.current_tushare_statements import decode_current_statement_artifact
from tradingagents.dataflows.market_snapshot import get_active_run_telemetry
from tradingagents.market_history.coordinator import PhysicalAttemptFailure, PhysicalAttemptOutcome


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [200, 503, None])
def test_headers_recorded_before_body_failure_without_extra_send(monkeypatch, asynchronous, status):
    sends = []

    class BrokenSync(httpx.SyncByteStream):
        def __iter__(self):
            yield b"\n\n"
            yield b"synthetic-partial-body"
            raise httpx.RemoteProtocolError("synthetic-private-body-detail")

    class BrokenAsync(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\n\n"
            yield b"synthetic-partial-body"
            raise httpx.RemoteProtocolError("synthetic-private-body-detail")

    def reply(request):
        sends.append(json.loads(request.content))
        if status is None:
            raise httpx.ConnectError("synthetic-private-connect-detail")
        return httpx.Response(
            status,
            stream=BrokenAsync() if asynchronous else BrokenSync(),
            headers={"x-request-id": "invalid secret id!", "x-private-header": "private-header-value"},
        )

    def sync_wire(transport, request):
        return reply(request)

    async def async_wire(transport, request):
        return reply(request)

    with client(monkeypatch, [], timeout=15) as (llm, _), budget() as counter:
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", sync_wire)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_wire)
        ledger = get_active_run_telemetry()
        messages = [HumanMessage(content="Synthetic")]
        config = {"metadata": {"langgraph_node": "Bear Researcher"}}
        with pytest.raises(APIConnectionError):
            if asynchronous:
                asyncio.run(llm.ainvoke(messages, config=config))
            else:
                llm.invoke(messages, config=config)
        attempts = ledger.finalize(terminal_route="operational_failure").model_transport.attempts
        assert len(attempts) == counter.receipt()["model_attempts"] == len(sends) == 1
        record = attempts[0]
        assert record.disposition == "terminal_transport_failure"
        assert record.http_status == status
        assert record.configured_max_retries == 0
        assert record.exception_class == ("ConnectError" if status is None else "RemoteProtocolError")
        assert record.provider_request_id is None
        assert record.send_limits.stage == "research_debate"
        assert record.send_limits.httpx_timeout_seconds == dict.fromkeys(
            ("connect", "read", "write", "pool"), 15.0
        )
        assert sends[0]["max_tokens"] == 65536 and sends[0]["reasoning_effort"] == "high"
        encoded = json.dumps(record.model_dump(mode="json"))
        assert all(value not in encoded for value in (
            "synthetic-private", "synthetic-partial", "invalid secret", "private-header-value",
            "public-synthetic-test-placeholder",
        ))


def table(values):
    return {"code": 0, "data": {"fields": [
        "ts_code", "ann_date", "end_date", "cost_fin_assets", "fair_value_fin_assets",
        "opaque-column-must-not-be-in-counts",
    ], "items": [["600919.SH", "20261030", "20260930", *values]]}}


def fetch(monkeypatch, payload, metadata):
    calls = fake_connection(monkeypatch, payload=payload)
    frame = financial_transport._Client(
        "https://api.tushare.pro", "synthetic-test-token", failure_metadata=metadata
    ).query("balancesheet", ts_code="600919.SH", start_date="20200101",
            end_date="20261107", report_type="1")
    assert sum(call[0] == "request" for call in calls) == 1
    return frame


@pytest.mark.parametrize("value,kind", [
    (float("nan"), "nonfinite"), (float("inf"), "nonfinite"),
    (-float("inf"), "nonfinite"), ({"private-key": "private-value"}, "unsupported"),
    (["private-value"], "unsupported"),
])
def test_raw_invalid_cells_rejected_before_dataframe_once(monkeypatch, value, kind):
    metadata = {}
    calls = fake_connection(monkeypatch, payload=table([value, None, "ordinary-text"]))
    constructed = []
    monkeypatch.setattr(financial_transport.pd, "DataFrame", lambda *a, **k: constructed.append(True))
    with pytest.raises(PhysicalAttemptFailure) as caught:
        financial_transport._Client(
            "https://api.tushare.pro", "synthetic-test-token", failure_metadata=metadata
        ).query("balancesheet", ts_code="600919.SH", start_date="20200101",
                end_date="20261107", report_type="1")
    assert caught.value.outcome is PhysicalAttemptOutcome.MALFORMED_RESPONSE
    assert caught.value.retryable is False and caught.value.status_code == 200
    assert sum(call[0] == "request" for call in calls) == 1 and not constructed
    summary = metadata["response_values"]
    assert summary["status"] == "invalid" and summary["counts"][kind] == 1
    assert summary["tracked_fields"]["cost_fin_assets"][kind] == 1
    encoded = json.dumps(metadata, allow_nan=False)
    assert all(value not in encoded for value in (
        "private-key", "private-value", "ordinary-text", "opaque-column", "synthetic-test-token",
    ))


@pytest.mark.parametrize("literal", ["1e400", "-1e400"])
def test_json_numeric_overflow_rejected_before_conversion(monkeypatch, literal):
    metadata = {}
    payload = table(["replace-numeric", None, False])
    fake_connection(monkeypatch, payload=payload)
    raw = json.dumps(payload).replace('"replace-numeric"', literal).encode()
    monkeypatch.setattr(financial_transport.http.client.HTTPSConnection, "read", lambda self, n: raw[:n])
    with pytest.raises(PhysicalAttemptFailure) as caught:
        financial_transport._Client(
            "https://api.tushare.pro", "synthetic-test-token", failure_metadata=metadata
        ).query("balancesheet", ts_code="600919.SH", start_date="20200101",
                end_date="20261107", report_type="1")
    assert caught.value.outcome is PhysicalAttemptOutcome.MALFORMED_RESPONSE
    assert metadata["response_values"]["counts"]["nonfinite"] == 1


@pytest.mark.parametrize("values", [[None, 0, False], [12.5, "NaN", "private-text"]])
def test_valid_values_counts_and_encoded_roundtrip(monkeypatch, values):
    metadata = {}
    frame = fetch(monkeypatch, table(values), metadata)
    assert frame.iloc[0].tolist()[3:] == values
    summary = frame.attrs["response_values"]
    assert summary == metadata["response_values"]
    assert summary["status"] == "valid"
    assert summary["counts"]["nonfinite"] == summary["counts"]["unsupported"] == 0
    assert "private-text" not in json.dumps(summary)
    assert "opaque-column" not in json.dumps(summary)
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    encoded = encode_provider_artifact(
        endpoint_id="balancesheet", frame=frame, retrieved_at=now, observed_at=now,
        adapter_metadata=frame.attrs,
    )
    payload = decode_current_statement_artifact(encoded, endpoint_id="balancesheet")
    assert payload["adapter_metadata"]["response_values"] == summary
    assert payload["rows"][0]["cost_fin_assets"] == values[0]
    assert payload["rows"][0]["fair_value_fin_assets"] == values[1]


def test_old_artifact_remains_readable_without_inventing_value_evidence():
    response = table([None, 0, True])
    frame = pd.DataFrame(response["data"]["items"], columns=response["data"]["fields"], dtype=object)
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    metadata = {"response_completeness": assessment.inspect_response_completeness(response)}
    encoded = encode_provider_artifact(endpoint_id="balancesheet", frame=frame,
        retrieved_at=now, observed_at=now, adapter_metadata=metadata)
    before = bytes(encoded)
    decoded = decode_current_statement_artifact(encoded, endpoint_id="balancesheet")
    assert decoded["adapter_metadata"] == metadata and encoded == before
    assert "response_values" not in decoded["adapter_metadata"]


@pytest.mark.parametrize("change", ["extra", "boolean_count", "wrong_count", "wrong_field", "invalid_status"])
def test_retained_counts_closed_and_consistent(monkeypatch, change):
    frame = fetch(monkeypatch, table([None, 0, False]), {})
    metadata = copy.deepcopy(frame.attrs)
    summary = metadata["response_values"]
    if change == "extra":
        summary["private-key"] = "private-value"
    elif change == "boolean_count":
        summary["counts"]["null"] = True
    elif change == "wrong_count":
        summary["counts"]["null"] = 2
    elif change == "wrong_field":
        summary["tracked_fields"]["private-column"] = summary["tracked_fields"].pop("cost_fin_assets")
    else:
        summary["status"] = "invalid"
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    encoded = encode_provider_artifact(endpoint_id="balancesheet", frame=frame,
        retrieved_at=now, observed_at=now, adapter_metadata=metadata)
    with pytest.raises(ValueError, match="financial_response_values_invalid"):
        decode_current_statement_artifact(encoded, endpoint_id="balancesheet")


def test_counts_saturate_and_unknown_columns_never_escape():
    fields = ["cost_fin_assets", "private-column"]
    rows = [[None, 1]] * (assessment.VALUE_COUNT_LIMIT + 1)
    summary = assessment.inspect_response_values(fields, rows)
    assert summary["row_count"] == assessment.VALUE_COUNT_LIMIT
    assert summary["counts"]["null"] == summary["counts"]["number"] == assessment.VALUE_COUNT_LIMIT
    assert summary["tracked_fields"]["fair_value_fin_assets"] is None
    assert "private-column" not in json.dumps(summary)


def test_real_current_acquisition_retains_counts_and_reuses_without_token(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    source = statement_rows("balancesheet")
    source["cost_fin_assets"] = None
    source["fair_value_fin_assets"] = None
    payload = {"code": 0, "data": {"fields": list(source.columns), "items": source.values.tolist()}}
    calls = fake_connection(monkeypatch, payload=payload)
    clock = Clock()
    first = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(request("get_balance_sheet"))
    assert first.artifact is not None
    assert sum(call[0] == "request" for call in calls) == 1
    database = Path(config["data_cache_dir"]) / "current-financial-statements.sqlite3"
    with sqlite3.connect(database) as connection:
        saved = connection.execute("SELECT payload FROM observations").fetchall()
    assert len(saved) == 1
    decoded = decode_current_statement_artifact(saved[0][0], endpoint_id="balancesheet")
    values = decoded["adapter_metadata"]["response_values"]
    assert values["tracked_fields"]["cost_fin_assets"]["null"] == 2
    assert values["tracked_fields"]["fair_value_fin_assets"]["null"] == 2
    clock.value += timedelta(hours=1)
    import tradingagents.dataflows.current_tushare_statements as current

    def unexpected_client(*args, **kwargs):
        raise AssertionError("cache reuse must not acquire credentials or send")

    monkeypatch.setattr(current, "create_financial_statement_client", unexpected_client)
    second = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(request("get_balance_sheet"))
    assert second.artifact is not None and second.value == first.value
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT payload FROM observations").fetchall() == saved
    assert sum(call[0] == "request" for call in calls) == 1


def test_empty_table_counts_are_valid_without_claiming_coverage():
    summary = assessment.inspect_response_values(["cost_fin_assets", "extension"], [])
    assert summary["status"] == "valid" and summary["row_count"] == 0
    assert all(value == 0 for value in summary["counts"].values())
    assert assessment.validate_retained_values(summary, ["cost_fin_assets", "extension"], []) == summary


@pytest.mark.parametrize("failure", ["http", "headers_disconnect", "body_disconnect", "json"])
def test_acceptance_failure_never_inherits_previous_table_counts(tmp_path, monkeypatch, failure):
    from tradingagents import statement_acceptance

    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            self.status = 200

        def request(self, method, path, *, body, headers):
            self.endpoint = json.loads(body)["api_name"]
            calls.append(self.endpoint)
            if len(calls) == 2 and failure == "http":
                self.status = 503

        def getresponse(self):
            if len(calls) == 2 and failure == "headers_disconnect":
                raise financial_transport.http.client.RemoteDisconnected("synthetic-private-detail")
            return self

        def getheader(self, name, default=None):
            return default

        def read(self, limit):
            if len(calls) == 2:
                if failure == "body_disconnect":
                    raise financial_transport.http.client.RemoteDisconnected("synthetic-private-detail")
                if failure == "json":
                    return b"synthetic-invalid-json"
            frame = statement_rows(self.endpoint)
            return json.dumps({"code": 0, "data": {"fields": list(frame.columns),
                               "items": frame.values.tolist()}}).encode()[:limit]

        def close(self):
            pass

    monkeypatch.setattr(financial_transport.http.client, "HTTPSConnection", Connection)
    result = statement_acceptance.acquire(tmp_path, ticker="600895.SH",
        destination="https://api.tushare.pro", account="synthetic", as_of="2026-10-15",
        token="synthetic-only")
    assert result["status"] == "stopped" and result["physical_requests"] == len(calls) == 2
    assert len(result["completed_tools"]) == 1
    receipt = result["failure"]
    assert "response_values" not in receipt and "response_completeness" not in receipt
    expected = {"http": (503, "provider_error"), "headers_disconnect": (None, "disconnect"),
                "body_disconnect": (200, "disconnect"), "json": (200, "malformed_response")}
    status, outcome = expected[failure]
    assert receipt["http_status"] == status
    assert receipt["error_code"] == "financial_https_" + outcome and not receipt["retryable"]
    assert "synthetic-private-detail" not in json.dumps(receipt)
