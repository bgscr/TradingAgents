"""Offline regressions for the retained 2026-10-28 analysis failure.

Only external responses are synthetic; production classification, CLI status,
pre-send accounting and acquisition circuit state are exercised unchanged.
"""

import json
from unittest.mock import MagicMock

import pytest
import requests
from langchain_core.messages import AIMessage
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from tests.test_core_send_boundaries import core_config, fake_wire
from tradingagents.agents.utils.structured import invoke_required_structured
from tradingagents.analysis_budget import AnalysisBudgetStopped, analysis_budget_scope
from tradingagents.dataflows.acquisition import AcquisitionController, AcquisitionRequest


def length_error():
    return LengthFinishReasonError(completion=ChatCompletion(
        id="synthetic", object="chat.completion", created=0, model="deepseek-flash",
        choices=[{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": "PRIVATE_SYNTHETIC_PAYLOAD"}}],
        usage={"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202},
    ))


def test_sdk_output_limit_is_not_transport_or_retry(caplog):
    runnable = MagicMock()
    runnable.invoke.side_effect = length_error()
    result = invoke_required_structured(runnable, "synthetic", "Market Analyst")
    assert result.reason == "output_limit"
    assert result.value is None and result.attempts == 1
    assert runnable.invoke.call_count == 1
    assert "PRIVATE_SYNTHETIC_PAYLOAD" not in caplog.text


@pytest.mark.parametrize("parsed", [None, {"otherwise": "valid"}])
@pytest.mark.parametrize("via_metadata", [True, False])
def test_truncated_envelope_never_admits_partial_evidence(parsed, via_metadata):
    runnable = MagicMock()
    runnable.invoke.return_value = {
        "raw": AIMessage(content="PRIVATE_SYNTHETIC_PAYLOAD", response_metadata={
            "finish_reason": "length" if via_metadata else "stop"}),
        "parsed": parsed,
        "parsing_error": None if via_metadata else length_error(),
    }
    result = invoke_required_structured(runnable, "synthetic", "Market Analyst")
    assert result.reason == "output_limit" and result.value is None
    assert runnable.invoke.call_count == result.attempts == 1


def test_other_transport_failure_remains_transport():
    runnable = MagicMock()
    runnable.invoke.side_effect = ConnectionError("PRIVATE_SYNTHETIC_PAYLOAD")
    result = invoke_required_structured(runnable, "synthetic", "Market Analyst")
    assert result.reason == "transport_error" and result.attempts == 1


def test_cli_retains_safe_output_limit_status():
    import cli.main as main
    assert main._safe_submission_reason_class("output_limit: PRIVATE_SYNTHETIC_PAYLOAD") == "output_limit"
    assert main._submission_agent_status({"status": "unavailable", "reason_class": "output_limit"}) == "failed"
    assert main._safe_submission_reason_class("PRIVATE_SYNTHETIC_PAYLOAD") is None


def test_redirect_keeps_charge_stops_and_records_only_public_endpoint(monkeypatch):
    sent = fake_wire(monkeypatch, 302, {"location": "https://outside.invalid/PRIVATE_SYNTHETIC_PAYLOAD"})
    monkeypatch.setenv("NO_PROXY", "*")
    with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"), analysis_budget_scope(core_config()) as budget:
        requests.get("https://search-api-web.eastmoney.com/search/jsonp?param=PRIVATE_SYNTHETIC_PAYLOAD")
    receipt = budget.receipt()
    assert len(sent) == receipt["data_attempts"] == receipt["admitted_attempts"] == 1
    assert receipt["stop_reason"] == "data_redirect_forbidden"
    assert receipt["data_transport_denial"] == {
        "reason": "data_redirect_forbidden", "method": "GET", "host": "search-api-web.eastmoney.com",
        "path": "/search/jsonp", "http_status": 302,
    }
    assert "PRIVATE_SYNTHETIC_PAYLOAD" not in json.dumps(receipt)
    with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"):
        budget.admit("data")
    assert budget.receipt()["admitted_attempts"] == 1


@pytest.mark.parametrize("error,code", [
    (KeyError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_schema_key_missing"),
    (json.JSONDecodeError("PRIVATE_SYNTHETIC_PAYLOAD", "PRIVATE_SYNTHETIC_PAYLOAD", 0), "provider_response_json_invalid"),
    (requests.exceptions.JSONDecodeError("PRIVATE_SYNTHETIC_PAYLOAD", "PRIVATE_SYNTHETIC_PAYLOAD", 0), "provider_response_json_invalid"),
    (requests.exceptions.HTTPError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_http_error"),
    (requests.exceptions.ConnectTimeout("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_timeout"),
    (requests.exceptions.ConnectionError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_connection_error"),
    (ValueError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_value_invalid"),
    (TypeError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_type_invalid"),
    (RuntimeError("PRIVATE_SYNTHETIC_PAYLOAD"), "provider_exception"),
])
def test_provider_failure_retains_fixed_category_without_retry_or_payload(error, code):
    provider = MagicMock(side_effect=error)
    controller = AcquisitionController(providers=(("akshare", provider),))
    request = AcquisitionRequest(capability="news", source_ref="news:synthetic", tool_call_id="synthetic", tool_name="get_news")
    first = controller.acquire(request, validator=lambda value: value, serializer=json.dumps)
    later = controller.acquire(request.model_copy(update={"tool_call_id": "second"}), validator=lambda value: value, serializer=json.dumps)
    assert first.value is first.artifact is None
    assert first.outcomes[0].reason.value == "provider_error"
    assert first.outcomes[0].error_code == code and first.outcomes[0].retryable is False
    assert later.outcomes[0].reason.value == "circuit_open"
    assert provider.call_count == 1
    assert "PRIVATE_SYNTHETIC_PAYLOAD" not in first.outcomes[0].model_dump_json()
