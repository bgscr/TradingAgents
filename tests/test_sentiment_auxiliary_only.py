"""Synthetic offline checks for an explicitly source-disabled analyst run."""
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import sentiment_analyst as sa
from tradingagents.agents.schemas import SentimentBand, SentimentReport
from tradingagents.agents.utils.structured import invoke_required_structured
from tradingagents.analysis_budget import AnalysisBudgetStopped
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.evidence import ClaimValidationStatus, EvidenceState, EvidenceStatus


def test_auxiliary_only_skips_all_sources_without_minting_evidence(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("source must not run")

    for name in ("acquire_news", "acquire_stocktwits_messages", "acquire_reddit_posts",
                 "get_china_a_local_sentiment", "get_china_a_enhancements_for_categories",
                 "_collect_sentiment_blocks"):
        monkeypatch.setattr(sa, name, forbidden)
    from tradingagents import margin_balance, moneyflow
    monkeypatch.setattr(moneyflow, "moneyflow_for_state", lambda state: None)
    monkeypatch.setattr(margin_balance, "margin_balance_for_state", lambda state: None)
    seen = []
    report = SentimentReport(overall_band=SentimentBand.BULLISH, overall_score=9,
        confidence="high", narrative="合成辅助说明：并未评估情绪。",
        material_claims=({"claim_id": "sentiment.unsupported", "statement": "Synthetic value is 99.",
                         "source_ref": "sentiment.news", "source_quote": "Synthetic value is 99."},))
    binding = MagicMock()
    binding.invoke.side_effect = lambda prompt: (seen.append(prompt) or
        {"raw": AIMessage(content="synthetic"), "parsed": report, "parsing_error": None})
    monkeypatch.setattr(sa, "bind_required_structured", lambda *args: binding)
    old = get_config()
    try:
        set_config({"output_language": "Chinese"})
        result = sa.create_sentiment_analyst(object(), external_sources=False,
            structured_max_attempts=1)({"company_of_interest": "600919.SH",
            "trade_date": "2026-11-13", "messages": [], "evidence_state": {}})
    finally:
        set_config(old)
    assert binding.invoke.call_count == 1
    assert "explicitly disabled" in " ".join(m.content for m in seen[0])
    assert result["sentiment_report"].startswith("情绪方向及评分：未评估")
    assert "Bullish" not in result["sentiment_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.source_artifacts == () and evidence.source_facts == ()
    assert evidence.acquisition_outcomes == ()
    assert all(s.status is EvidenceStatus.UNAVAILABLE for s in evidence.sources)
    assert evidence.claim_validations[0].status is ClaimValidationStatus.UNSUPPORTED


def test_no_format_repair_keeps_first_failure_and_one_call():
    binding = MagicMock()
    binding.invoke.return_value = {"raw": AIMessage(content="synthetic malformed"),
        "parsed": None, "parsing_error": ValueError("synthetic secret")}
    result = invoke_required_structured(binding, "original", "Sentiment Analyst", max_attempts=1)
    assert result.value is None and result.attempts == 1
    assert binding.invoke.call_count == 1


def test_budget_stop_remains_terminal():
    binding = MagicMock()
    binding.invoke.side_effect = AnalysisBudgetStopped("synthetic_stop")
    with pytest.raises(AnalysisBudgetStopped):
        invoke_required_structured(binding, "original", "Sentiment Analyst", max_attempts=1)
    assert binding.invoke.call_count == 1


@pytest.mark.parametrize("bad", [0, 3, True, "1", None])
def test_invalid_attempt_limit_is_rejected_before_invocation(bad):
    binding = MagicMock()
    with pytest.raises(ValueError):
        invoke_required_structured(binding, [], "Sentiment Analyst", max_attempts=bad)
    assert not binding.invoke.called
    with pytest.raises(ValueError):
        sa.create_sentiment_analyst(binding, structured_max_attempts=bad)


@pytest.mark.parametrize("bad", [0, 1, "False", None])
def test_invalid_source_switch_is_rejected(bad):
    with pytest.raises(ValueError):
        sa.create_sentiment_analyst(object(), external_sources=bad)


def test_production_model_adapter_carries_auxiliary_blocks_without_tools(monkeypatch):
    """Real prompt/JSON adapter and budget; HTTP send replaced by synthetic transport."""
    import json

    import httpx

    from tradingagents import margin_balance, moneyflow
    from tradingagents.analysis_budget import analysis_budget_scope
    from tradingagents.llm_clients.factory import create_llm_client
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-offline-key")
    flow = "Synthetic moneyflow 12.5 万元 retrospective_backfill"
    margin = "Synthetic margin 1000 元 retrospective_backfill"
    monkeypatch.setattr(moneyflow, "moneyflow_for_state", lambda state: {"synthetic": True})
    monkeypatch.setattr(moneyflow, "moneyflow_markdown", lambda *args, **kwargs: flow)
    monkeypatch.setattr(margin_balance, "margin_balance_for_state", lambda state: {"synthetic": True})
    monkeypatch.setattr(margin_balance, "margin_balance_markdown", lambda *args, **kwargs: margin)
    seen = []
    def fake_send(transport, request):
        body = json.loads(request.content)
        seen.append(body)
        text = " ".join(m["content"] for m in body["messages"])
        assert flow in text and margin in text
        assert "tools" not in body and "tool_choice" not in body
        assert body["max_tokens"] == 4096
        assert "synthetic-offline-key" not in text
        content = json.dumps({"overall_band": "Neutral", "overall_score": 5,
            "confidence": "low", "narrative": "合成资金流12.5万元，融资余额1000元；不是舆情。",
            "material_claims": []}, ensure_ascii=False)
        return httpx.Response(200, request=request, json={"id": "synthetic",
            "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_send)
    old = get_config()
    http = httpx.Client(follow_redirects=False, trust_env=False)
    llm = None
    try:
        set_config({"output_language": "Chinese"})
        llm = create_llm_client("deepseek", "deepseek-flash", base_url="https://api.deepseek.com",
            max_retries=0, max_tokens=4096, reasoning_effort="high", http_client=http).get_llm()
        with analysis_budget_scope({"analysis_budget_enabled": True, "analysis_max_calls": 8,
            "analysis_timeout_seconds": 300, "llm_provider": "deepseek", "llm_max_retries": 0,
            "market_history_mode": "current", "llm_max_output_tokens": 4096}) as budget:
            result = sa.create_sentiment_analyst(llm, external_sources=False, structured_max_attempts=1)(
                {"company_of_interest": "600919.SH", "trade_date": "2026-11-13",
                 "evidence_state": {}, "messages": []})
            assert budget.receipt()["model_attempts"] == 1
            assert budget.receipt()["data_attempts"] == 0
        assert len(seen) == 1
        assert "12.5万元" in result["sentiment_report"]
        assert result["sentiment_report"].startswith("情绪方向及评分：未评估")
    finally:
        (llm.root_client if llm is not None else http).close()
        set_config(old)
