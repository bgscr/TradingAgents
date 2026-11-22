"""Synthetic regression for finalizer language and the observed news timeout boundary."""
import importlib
import json
from unittest.mock import MagicMock

import pytest
import requests
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tests.test_structured_agents import _tool_exchange
from tradingagents.agents.analysts.submission import process_analyst_response
from tradingagents.dataflows import akshare_data, config, interface
from tradingagents.dataflows.acquisition import AcquisitionController
from tradingagents.evidence import AnalystEvidenceReport, EvidenceState, SubmittedMaterialClaim
from tradingagents.run_diagnostics import diagnostic_scope


@pytest.mark.parametrize("analyst,tool,args", [
    ("market", "get_indicators", {"symbol": "NVDA", "curr_date": "2026-01-15"}),
    ("news", "get_news", {"ticker": "NVDA", "start_date": "2026-01-08", "end_date": "2026-01-15"}),
    ("fundamentals", "get_income_statement", {"ticker": "NVDA", "curr_date": "2026-01-15"}),
])
@pytest.mark.parametrize("language,repair", [("Chinese", False), ("Chinese", True), ("English", False)])
def test_finalizer_preserves_run_language_and_exact_source(monkeypatch, analyst, tool, args, language, repair):
    module = importlib.import_module(f"tradingagents.agents.analysts.{analyst}_analyst")
    monkeypatch.setattr(module, "get_instrument_context_from_state", lambda state: "Synthetic identity")
    monkeypatch.setitem(config._config, "output_language", language)
    quote = "Synthetic observation was 55."
    source_ref = f"{tool}:NVDA:2026-01-15"
    narrative = "合成观测值为55；资料缺口保留。" if language == "Chinese" else "Synthetic observation was 55."
    payload = AnalystEvidenceReport(report_markdown=narrative, material_claims=(
        SubmittedMaterialClaim(claim_id=f"{analyst}.synthetic", statement=narrative,
                               source_ref=source_ref, source_quote=quote),))
    finalizer = MagicMock()
    finalizer.invoke.side_effect = [ValueError("synthetic schema error"), payload] if repair else [payload]
    initial = []

    def draft(prompt):
        initial.append(prompt.to_string())
        return AIMessage(content="UNVALIDATED SYNTHETIC DRAFT")

    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(draft)
    llm.with_structured_output.return_value = finalizer
    result = getattr(module, f"create_{analyst}_analyst")(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-01-15", "asset_type": "stock",
        "messages": _tool_exchange(tool, args, quote, "synthetic-source"),
        "evidence_state": EvidenceState().model_dump(mode="json"),
    })
    assert len(initial) == 1 and finalizer.invoke.call_count == (2 if repair else 1)
    for call in finalizer.invoke.call_args_list:
        prompt = call.args[0]
        if language == "Chinese":
            assert "Write your entire response in Chinese." in initial[0]
            assert "Write your entire response in Chinese." in prompt
        else:
            assert "Write your entire response in Chinese." not in prompt
        assert "JSON schema keys" in prompt and "source_quote" in prompt
        assert "must not be translated" in prompt
        assert source_ref in prompt and quote in prompt
    assert narrative in result[f"{analyst}_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.material_claims[0].source_quote == quote
    assert evidence.material_claims[0].source_refs == (source_ref,)


def test_direct_structured_reply_is_not_translated_or_reissued(monkeypatch):
    monkeypatch.setitem(config._config, "output_language", "Chinese")
    finalizer = MagicMock()
    result = process_analyst_response(AIMessage(content="", tool_calls=[{
        "name": "AnalystEvidenceReport", "id": "synthetic-direct", "type": "tool_call",
        "args": {"report_markdown": "合成中文正文", "material_claims": [], "evidence_notes": []},
    }]), finalizer=finalizer, analyst="news", ticker="NVDA", trade_date="2026-01-15", messages=[])
    assert result.report == "合成中文正文"
    finalizer.invoke.assert_not_called()


@pytest.mark.parametrize("error_type", [requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout])
def test_news_timeout_stops_before_parse_and_next_call_uses_circuit(monkeypatch, error_type):
    from tradingagents.dataflows import eastmoney_news

    monkeypatch.setitem(config._config, "tool_vendors", {"get_news": "akshare"})
    controller = AcquisitionController(providers=())
    monkeypatch.setattr(interface, "get_active_acquisition_controller", lambda: controller)
    wire = MagicMock(side_effect=error_type("synthetic-secret https://example.invalid/?token=synthetic-secret"))
    parser = MagicMock(side_effect=AssertionError("parser must not run after timeout"))
    monkeypatch.setattr(akshare_data.requests, "get", wire)
    monkeypatch.setattr(eastmoney_news, "parse_news_page", parser)
    events = []
    with diagnostic_scope(events.append):
        results = [interface.route_to_vendor_acquired(
            "get_news", "600895.SS", "2026-10-05", "2026-10-12", tool_call_id=f"synthetic-{n}",
            source_ref="synthetic-news:600895", capability="get_news",
        ) for n in range(2)]
    assert wire.call_count == 1
    parser.assert_not_called()
    assert all(result.artifact is None and result.value is None for result in results)
    first, second = (result.outcomes[0] for result in results)
    assert first.reason.value == "provider_error" and first.error_code == "provider_timeout"
    assert first.retryable is False and first.attempt == 1 and first.http_status is None
    assert second.reason.value == "circuit_open" and second.retryable is False
    assert [e["payload"]["stage"] for e in events if e["kind"] == "news_exception"] == ["http_request", "provider_call"]
    assert not any(e["kind"] in {"news_business_status", "news_response_shape", "news_selection"} for e in events)
    saved = json.dumps(events) + first.model_dump_json() + second.model_dump_json()
    assert "synthetic-secret" not in saved and "example.invalid" not in saved
