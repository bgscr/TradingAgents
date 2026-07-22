"""Tests for structured-output agents (Trader, Research Manager, Sentiment Analyst).

The Portfolio Manager has its own coverage in tests/test_memory_log.py
(which exercises the full memory-log → PM injection cycle).  This file
covers the parallel schemas, render functions, and graceful-fallback
behavior we added for the Trader, Research Manager, and Sentiment Analyst
so they share the same deterministic output shape.
"""

import inspect
from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from tradingagents.agents.analysts import sentiment_analyst as sentiment_module
from tradingagents.agents.analysts.fundamentals_analyst import (
    create_fundamentals_analyst,
)
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    SentimentBand,
    SentimentReport,
    TraderAction,
    TraderProposal,
    render_research_plan,
    render_sentiment_report,
    render_trader_proposal,
)
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.structured import (
    bind_required_structured,
    invoke_required_structured,
)
from tradingagents.dataflows.acquisition import AcquisitionResult
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    AnalystEvidenceReport,
    ClaimValidationStatus,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SubmittedMaterialClaim,
    ToolExecutionEvidenceEnvelope,
    make_source_acquisition_outcome,
)

# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyst_prompts_do_not_expose_final_transaction_proposal_signal():
    analyst_factories = (
        create_market_analyst,
        create_sentiment_analyst,
        create_news_analyst,
        create_fundamentals_analyst,
    )

    for factory in analyst_factories:
        assert "FINAL TRANSACTION PROPOSAL" not in inspect.getsource(factory)


def _tool_exchange(tool_name, args, content, call_id):
    symbol = args.get("ticker") or args.get("symbol")
    trade_date = args.get("curr_date") or args.get("end_date")
    source_kind = "snapshot" if tool_name == "get_verified_market_snapshot" else tool_name
    source_ref = f"{source_kind}:{symbol}:{trade_date}"
    capability = "market_snapshot" if source_kind == "snapshot" else source_kind
    outcome = make_source_acquisition_outcome(
        provider="fixture-provider",
        capability=capability,
        attempt=1,
        retrieved_at="2026-07-19T00:00:00+00:00",
        source_ref=source_ref,
        tool_call_id=call_id,
        tool_name=tool_name,
        content=content,
        status="success",
    )
    envelope = ToolExecutionEvidenceEnvelope(
        tool_call_id=call_id,
        tool_name=tool_name,
        source_ref=source_ref,
        capability=capability,
        acquisition_outcomes=(outcome,),
        selected_artifact=(
            outcome.artifact
            if isinstance(outcome, SourceAcquisitionAvailable)
            else None
        ),
    ).model_dump(mode="json")
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool_name,
                    "args": args,
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=tool_name,
            artifact=envelope,
        ),
    ]


@pytest.mark.unit
def test_required_structured_binding_runtime_failure_is_fail_closed():
    llm = MagicMock()
    llm.with_structured_output.side_effect = RuntimeError(
        "provider response format is unavailable"
    )

    assert bind_required_structured(llm, SentimentReport, "Sentiment Analyst") is None


@pytest.mark.unit
def test_required_structured_raw_envelope_repairs_without_logging_payload(caplog):
    secret_payload = "PRIVATE_PROVIDER_PAYLOAD"
    valid = SentimentReport(
        overall_band=SentimentBand.NEUTRAL,
        overall_score=5.0,
        confidence="low",
        narrative="Neutral evidence.",
    )
    binding = MagicMock()
    binding.schema_name = "SentimentReport"
    binding.model_name = "deepseek-v4-flash"
    binding.invoke.side_effect = [
        {
            "raw": AIMessage(
                content=secret_payload,
                response_metadata={"finish_reason": "stop"},
            ),
            "parsed": None,
            "parsing_error": ValueError(secret_payload),
        },
        {
            "raw": AIMessage(
                content="",
                response_metadata={"finish_reason": "tool_calls"},
            ),
            "parsed": valid,
            "parsing_error": None,
        },
    ]

    with caplog.at_level("INFO"):
        result = invoke_required_structured(
            binding,
            "original prompt",
            "Sentiment Analyst",
        )

    assert result.value == valid
    assert result.attempts == 2
    assert binding.invoke.call_count == 2
    assert "finish_reason=stop" in caplog.text
    assert "finish_reason=tool_calls" in caplog.text
    assert secret_payload not in caplog.text


@pytest.mark.unit
def test_market_analyst_submits_typed_claims_to_shared_evidence():
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was USD 189.50.",
        source_quote="The effective-date close was USD 189.50.",
        source_refs=("snapshot:NVDA:2026-01-15",),
    )
    indicator_claim = MaterialClaim(
        claim_id="market.rsi",
        analyst="market",
        statement="The effective-date RSI was 55.",
        source_quote="The effective-date RSI was 55.",
        source_refs=("get_indicators:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## Market Analysis\n\nMomentum remained constructive.",
                    "material_claims": [
                        claim.model_dump(mode="json"),
                        indicator_claim.model_dump(mode="json"),
                    ],
                },
                "id": "market-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)
    analyst = create_market_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [
                *_tool_exchange(
                    "get_verified_market_snapshot",
                    {"symbol": "NVDA", "curr_date": "2026-01-15"},
                    (
                        "Authoritative snapshot for NVDA on 2026-01-15. "
                        "The effective-date close was USD 189.50."
                    ),
                    "snapshot-tool-call",
                ),
                *_tool_exchange(
                    "get_indicators",
                    {"symbol": "NVDA", "curr_date": "2026-01-15"},
                    "The effective-date RSI was 55.",
                    "indicator-tool-call",
                ),
            ],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["market_report"] == "## Market Analysis\n\nMomentum remained constructive."
    assert result["messages"][0].content == result["market_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert tuple(
        item.model_copy(update={"fact_ids": ()})
        for item in evidence.material_claims
    ) == (claim, indicator_claim)
    assert all(item.fact_ids for item in evidence.material_claims)
    assert evidence.sources == (
        EvidenceSource(
            source_id="analyst.market.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_tool",
        ),
        EvidenceSource(
            source_id="snapshot:NVDA:2026-01-15",
            status=EvidenceStatus.AVAILABLE,
            required=False,
        ),
        EvidenceSource(
            source_id="get_indicators:NVDA:2026-01-15",
            status=EvidenceStatus.AVAILABLE,
            required=False,
        ),
    )


@pytest.mark.unit
def test_market_analyst_conflicts_numeric_claim_absent_from_its_source():
    claim = MaterialClaim(
        claim_id="market.rsi",
        analyst="market",
        statement="The effective-date RSI was 72.",
        source_quote="The effective-date RSI was 72.",
        source_refs=("get_indicators:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## Market Analysis\n\nMomentum looked elevated.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "market-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)
    analyst = create_market_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                "get_indicators",
                {"symbol": "NVDA", "curr_date": "2026-01-15"},
                "The effective-date RSI was 55.",
                "indicator-tool-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item for item in evidence.sources
        if item.source_id == "get_indicators:NVDA:2026-01-15"
    )
    assert source.status is EvidenceStatus.AVAILABLE
    validation = next(
        item for item in evidence.claim_validations if item.claim_id == claim.claim_id
    )
    assert validation.status is ClaimValidationStatus.UNSUPPORTED
    assert validation.detail == "source quote is absent from the cited tool result"


@pytest.mark.unit
def test_market_analyst_marks_claim_without_matching_tool_result_unavailable():
    claim = MaterialClaim(
        claim_id="market.rsi",
        analyst="market",
        statement="The effective-date RSI was 55.",
        source_quote="The effective-date RSI was 55.",
        source_refs=("get_indicators:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## Market Analysis\n\nMomentum was constructive.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "market-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)
    analyst = create_market_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.sources == (
        EvidenceSource(
            source_id="analyst.market.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_tool",
        ),
        EvidenceSource(
            source_id="get_indicators:NVDA:2026-01-15",
            status=EvidenceStatus.UNAVAILABLE,
            required=False,
            detail="source ref is not in allowed catalog",
        ),
    )


@pytest.mark.unit
def test_news_analyst_submits_typed_claims_to_shared_evidence():
    claim = MaterialClaim(
        claim_id="news.guidance_update",
        analyst="news",
        statement="Management raised full-year revenue guidance.",
        source_quote="Management raised full-year revenue guidance.",
        source_refs=("get_news:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## News Analysis\n\nGuidance improved.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "news-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)
    analyst = create_news_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                "get_news",
                {
                    "ticker": "NVDA",
                    "start_date": "2026-01-08",
                    "end_date": "2026-01-15",
                },
                "Management raised full-year revenue guidance.",
                "news-tool-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"] == "## News Analysis\n\nGuidance improved."
    assert result["messages"][0].content == result["news_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert tuple(
        item.model_copy(update={"fact_ids": ()})
        for item in evidence.material_claims
    ) == (claim,)
    assert evidence.material_claims[0].fact_ids
    assert evidence.sources == (
        EvidenceSource(
            source_id="analyst.news.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_tool",
        ),
        EvidenceSource(
            source_id="get_news:NVDA:2026-01-15",
            status=EvidenceStatus.AVAILABLE,
            required=False,
        ),
    )


@pytest.mark.unit
def test_fundamentals_analyst_submits_typed_claims_to_shared_evidence():
    claim = MaterialClaim(
        claim_id="fundamentals.revenue_growth",
        analyst="fundamentals",
        statement="Reported revenue grew 18% year over year.",
        source_quote="Reported revenue grew 18% year over year.",
        source_refs=("get_income_statement:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## Fundamentals\n\nRevenue growth remained strong.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "fundamentals-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)
    analyst = create_fundamentals_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                "get_income_statement",
                {"ticker": "NVDA", "curr_date": "2026-01-15"},
                "Reported revenue grew 18% year over year.",
                "income-statement-tool-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["fundamentals_report"] == (
        "## Fundamentals\n\nRevenue growth remained strong."
    )
    assert result["messages"][0].content == result["fundamentals_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert tuple(
        item.model_copy(update={"fact_ids": ()})
        for item in evidence.material_claims
    ) == (claim,)
    assert evidence.material_claims[0].fact_ids
    assert evidence.sources == (
        EvidenceSource(
            source_id="analyst.fundamentals.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_tool",
        ),
        EvidenceSource(
            source_id="get_income_statement:NVDA:2026-01-15",
            status=EvidenceStatus.AVAILABLE,
            required=False,
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "analyst_name", "report_key", "tool_name"),
    (
        (create_market_analyst, "market", "market_report", "get_indicators"),
        (create_news_analyst, "news", "news_report", "get_news"),
        (
            create_fundamentals_analyst,
            "fundamentals",
            "fundamentals_report",
            "get_income_statement",
        ),
    ),
)
def test_tool_analyst_plain_text_gets_one_structured_finalization(
    factory,
    analyst_name,
    report_key,
    tool_name,
):
    source_ref = f"{tool_name}:NVDA:2026-01-15"
    tool_args = {
        "get_indicators": {"symbol": "NVDA", "curr_date": "2026-01-15"},
        "get_news": {
            "ticker": "NVDA",
            "start_date": "2026-01-08",
            "end_date": "2026-01-15",
        },
        "get_income_statement": {
            "ticker": "NVDA",
            "curr_date": "2026-01-15",
        },
    }[tool_name]
    structured = MagicMock()
    structured.invoke.return_value = AnalystEvidenceReport(
        report_markdown=f"## {analyst_name.title()} Analysis\n\nValidated report.",
        material_claims=(
            SubmittedMaterialClaim(
                claim_id=f"{analyst_name}.signal",
                statement="The observed signal was 55.",
                source_quote="The observed signal was 55.",
                source_ref=source_ref,
            ),
        ),
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(
        lambda _: AIMessage(content="UNVALIDATED DIRECTIONAL DRAFT")
    )
    llm.with_structured_output.return_value = structured

    result = factory(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                tool_name,
                tool_args,
                "The observed signal was 55.",
                "source-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result[report_key] == f"## {analyst_name.title()} Analysis\n\nValidated report."
    assert "UNVALIDATED DIRECTIONAL DRAFT" not in result[report_key]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.material_claims[0].analyst == analyst_name
    assert EvidenceSource(
        source_id=f"analyst.{analyst_name}.submission",
        status=EvidenceStatus.AVAILABLE,
        required=True,
        detail="finalized_structured",
    ) in evidence.sources
    structured.invoke.assert_called_once()
    finalization_prompt = structured.invoke.call_args.args[0]
    assert "UNVALIDATED DIRECTIONAL DRAFT" in finalization_prompt
    assert source_ref in finalization_prompt


@pytest.mark.unit
def test_tool_analyst_finalizer_failure_is_explicitly_unavailable():
    structured = MagicMock()
    structured.invoke.side_effect = ValueError("provider returned malformed JSON")
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(
        lambda _: AIMessage(content="BUY because the model says so")
    )
    llm.with_structured_output.return_value = structured

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"].startswith("ANALYSIS_UNAVAILABLE:")
    assert "BUY" not in result["news_report"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.material_claims == ()
    assert EvidenceSource(
        source_id="analyst.news.submission",
        status=EvidenceStatus.UNAVAILABLE,
        required=True,
        detail="validation_error",
    ) in evidence.sources
    assert structured.invoke.call_count == 2


@pytest.mark.unit
def test_tool_analyst_repairs_none_parsed_once_without_admitting_draft():
    structured = MagicMock()
    structured.invoke.side_effect = [
        None,
        AnalystEvidenceReport(
            report_markdown="## News Analysis\n\nValidated report.",
            material_claims=(),
        ),
    ]
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(
        lambda _: AIMessage(content="BUY from unvalidated prose")
    )
    llm.with_structured_output.return_value = structured

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"] == "## News Analysis\n\nValidated report."
    assert "BUY" not in result["news_report"]
    assert structured.invoke.call_count == 2
    assert "Previous structured attempt failed" in structured.invoke.call_args.args[0]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert EvidenceSource(
        source_id="analyst.news.submission",
        status=EvidenceStatus.AVAILABLE,
        required=True,
        detail="finalized_structured",
    ) in evidence.sources


@pytest.mark.unit
def test_tool_analyst_does_not_retry_transport_failure():
    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("provider timeout")
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: AIMessage(content="draft"))
    llm.with_structured_output.return_value = structured

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"].startswith("ANALYSIS_UNAVAILABLE:")
    assert structured.invoke.call_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("completion_mode", ("direct_tool", "finalized_structured"))
def test_tool_analyst_rejects_source_ref_outside_exact_catalog(completion_mode):
    claim = MaterialClaim(
        claim_id="market.rsi",
        analyst="market",
        statement="The effective-date RSI was 55.",
        source_quote="The effective-date RSI was 55.",
        source_refs=("get_indicators:OTHER:1999-01-01",),
    )
    submitted = AnalystEvidenceReport(
        report_markdown="## Market Analysis\n\nMomentum was constructive.",
        material_claims=(
            SubmittedMaterialClaim(
                claim_id=claim.claim_id,
                statement=claim.statement,
                source_ref=claim.source_refs[0],
                source_quote=claim.source_quote,
            ),
        ),
    )
    if completion_mode == "direct_tool":
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "AnalystEvidenceReport",
                    "args": submitted.model_dump(mode="json"),
                    "id": "market-report",
                    "type": "tool_call",
                }
            ],
        )
    else:
        response = AIMessage(content="Unvalidated prose draft.")

    structured = MagicMock()
    structured.invoke.return_value = submitted
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: response)
    llm.with_structured_output.return_value = structured

    result = create_market_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                "get_indicators",
                {"symbol": "NVDA", "curr_date": "2026-01-15"},
                "The effective-date RSI was 55.",
                "indicator-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "get_indicators:OTHER:1999-01-01"
    )
    assert source.status is EvidenceStatus.UNAVAILABLE
    assert source.detail == "source ref is not in allowed catalog"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("statement", "source_content"),
    (
        (
            "Management cut full-year revenue guidance.",
            "Management raised full-year revenue guidance.",
        ),
        ("The effective-date MACD was 55.", "The effective-date RSI was 55."),
    ),
)
def test_tool_analyst_conflicts_claim_not_expressed_by_source(
    statement,
    source_content,
):
    claim = MaterialClaim(
        claim_id="news.material_premise",
        analyst="news",
        statement=statement,
        source_quote=statement,
        source_refs=("get_news:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## News Analysis\n\nA material event occurred.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "news-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": _tool_exchange(
                "get_news",
                {
                    "ticker": "NVDA",
                    "start_date": "2026-01-08",
                    "end_date": "2026-01-15",
                },
                source_content,
                "news-call",
            ),
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "get_news:NVDA:2026-01-15"
    )
    assert source.status is EvidenceStatus.AVAILABLE
    validation = next(
        item for item in evidence.claim_validations if item.claim_id == claim.claim_id
    )
    assert validation.status is ClaimValidationStatus.UNSUPPORTED
    assert validation.detail == "source quote is absent from the cited tool result"


@pytest.mark.unit
def test_tool_result_for_different_ticker_is_not_catalogued_as_run_evidence():
    claim = MaterialClaim(
        claim_id="news.guidance_update",
        analyst="news",
        statement="Management raised full-year revenue guidance.",
        source_quote="Management raised full-year revenue guidance.",
        source_refs=("get_news:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## News Analysis\n\nGuidance improved.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "news-report",
                "type": "tool_call",
            }
        ],
    )
    data_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_news",
                "args": {
                    "ticker": "AAPL",
                    "start_date": "2026-01-08",
                    "end_date": "2026-01-15",
                },
                "id": "wrong-ticker-news",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [
                data_call,
                ToolMessage(
                    content="Management raised full-year revenue guidance.",
                    tool_call_id="wrong-ticker-news",
                    name="get_news",
                ),
            ],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "get_news:NVDA:2026-01-15"
    )
    assert source.status is EvidenceStatus.UNAVAILABLE
    assert source.detail == "source ref is not in allowed catalog"


@pytest.mark.unit
def test_other_ticker_tool_result_cannot_support_target_source_ref():
    claim = MaterialClaim(
        claim_id="news.guidance_update",
        analyst="news",
        statement="Management raised full-year revenue guidance.",
        source_quote="Management raised full-year revenue guidance.",
        source_refs=("get_news:NVDA:2026-01-15",),
    )
    submission = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "## News Analysis\n\nGuidance improved.",
                    "material_claims": [claim.model_dump(mode="json")],
                },
                "id": "news-report",
                "type": "tool_call",
            }
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: submission)

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [
                *_tool_exchange(
                    "get_news",
                    {
                        "ticker": "NVDA",
                        "start_date": "2026-01-08",
                        "end_date": "2026-01-15",
                    },
                    "DATA_UNAVAILABLE",
                    "target-news",
                ),
                *_tool_exchange(
                    "get_news",
                    {
                        "ticker": "AAPL",
                        "start_date": "2026-01-08",
                        "end_date": "2026-01-15",
                    },
                    "Management raised full-year revenue guidance.",
                    "other-ticker-news",
                ),
            ],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "get_news:NVDA:2026-01-15"
    )
    assert source.status is EvidenceStatus.UNAVAILABLE
    assert source.detail == "tool returned unavailable"


@pytest.mark.unit
def test_tool_analyst_bind_value_error_becomes_explicitly_unavailable():
    llm = MagicMock()
    llm.with_structured_output.side_effect = ValueError(
        "provider rejects this response format"
    )
    llm.bind_tools.return_value = RunnableLambda(
        lambda _: AIMessage(content="BUY because the model says so")
    )

    result = create_news_analyst(llm)(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"].startswith("ANALYSIS_UNAVAILABLE:")
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert EvidenceSource(
        source_id="analyst.news.submission",
        status=EvidenceStatus.UNAVAILABLE,
        required=True,
        detail="unsupported",
    ) in evidence.sources


@pytest.mark.unit
def test_tool_analyst_executes_data_calls_before_accepting_report_submission():
    mixed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_news",
                "args": {
                    "ticker": "NVDA",
                    "start_date": "2026-01-08",
                    "end_date": "2026-01-15",
                },
                "id": "news-call",
                "type": "tool_call",
            },
            {
                "name": "AnalystEvidenceReport",
                "args": {
                    "report_markdown": "Premature report.",
                    "material_claims": [],
                },
                "id": "news-report",
                "type": "tool_call",
            },
        ],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: mixed)
    analyst = create_news_analyst(llm)

    result = analyst(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-15",
            "asset_type": "stock",
            "messages": [],
            "evidence_state": EvidenceState().model_dump(mode="json"),
        }
    )

    assert result["news_report"] == ""
    assert [call["name"] for call in result["messages"][0].tool_calls] == ["get_news"]
    llm.with_structured_output.return_value.invoke.assert_not_called()


@pytest.mark.unit
class TestRenderTraderProposal:
    def test_minimal_required_fields(self):
        p = TraderProposal(action=TraderAction.HOLD, reasoning="Balanced setup; no edge.")
        md = render_trader_proposal(p)
        assert "**Action**: Hold" in md
        assert "**Reasoning**: Balanced setup; no edge." in md
        # The trailing FINAL TRANSACTION PROPOSAL line is preserved for
        # reports and any external code that greps for it.
        assert "FINAL TRANSACTION PROPOSAL: **HOLD**" in md

    def test_optional_fields_included_when_present(self):
        p = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong technicals + fundamentals.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        md = render_trader_proposal(p)
        assert "**Action**: Buy" in md
        assert "**Entry Price**: 189.5" in md
        assert "**Stop Loss**: 178.0" in md
        assert "**Position Sizing**: 6% of portfolio" in md
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in md

    def test_optional_fields_omitted_when_absent(self):
        p = TraderProposal(action=TraderAction.SELL, reasoning="Guidance cut.")
        md = render_trader_proposal(p)
        assert "Entry Price" not in md
        assert "Stop Loss" not in md
        assert "Position Sizing" not in md
        assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md


@pytest.mark.unit
class TestNullishFloatCoercion:
    """A weak LLM may write "None"/"N/A" into an optional float field (#1058);
    coerce those to None so the structured call validates instead of erroring."""

    def test_trader_nullish_strings_coerce_to_none(self):
        for sentinel in ("None", "N/A", "null", "-", "", "TBD"):
            p = TraderProposal(
                action=TraderAction.HOLD,
                reasoning="x",
                entry_price=sentinel,
                stop_loss=sentinel,
            )
            assert p.entry_price is None
            assert p.stop_loss is None

    def test_trader_real_numeric_string_still_parses(self):
        p = TraderProposal(action=TraderAction.BUY, reasoning="x", entry_price="189.5")
        assert p.entry_price == 189.5

    def test_pm_nullish_price_target_coerces_to_none(self):
        d = PortfolioDecision(
            rating=PortfolioRating.OVERWEIGHT,
            executive_summary="s",
            investment_thesis="t",
            price_target="N/A",
        )
        assert d.price_target is None


@pytest.mark.unit
class TestRenderResearchPlan:
    def test_required_fields(self):
        p = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            rationale="Bull case carried; tailwinds intact.",
            strategic_actions="Build position over two weeks; cap at 5%.",
        )
        md = render_research_plan(p)
        assert "**Recommendation**: Overweight" in md
        assert "**Rationale**: Bull case carried" in md
        assert "**Strategic Actions**: Build position" in md

    def test_all_5_tier_ratings_render(self):
        for rating in PortfolioRating:
            p = ResearchPlan(
                recommendation=rating,
                rationale="r",
                strategic_actions="s",
            )
            md = render_research_plan(p)
            assert f"**Recommendation**: {rating.value}" in md


# ---------------------------------------------------------------------------
# Trader agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_trader_state():
    return {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ...\n**Strategic Actions**: ...",
    }


def _structured_trader_llm(captured: dict, proposal: TraderProposal | None = None):
    """Build a MagicMock LLM whose with_structured_output binding captures the
    prompt and returns a real TraderProposal so render_trader_proposal works.
    """
    if proposal is None:
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong setup.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or proposal
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
def test_invoke_structured_falls_back_when_result_is_none():
    # A thinking model can answer in plain text, leaving the parser with None.
    # That must fall back to free text, not crash on render(None) (#1051).
    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.return_value = None
    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="FREETEXT")

    out = invoke_structured_or_freetext(
        structured, plain, "prompt", render=lambda r: r.rating, agent_name="t"
    )
    assert out == "FREETEXT"
    plain.invoke.assert_called_once()


@pytest.mark.unit
class TestTraderAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="AI capex cycle intact; institutional flows constructive.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        llm = _structured_trader_llm(captured, proposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert "**Action**: Buy" in plan
        assert "**Entry Price**: 189.5" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan
        # The same rendered markdown is also added to messages for downstream agents.
        assert plan in result["messages"][0].content

    def test_prompt_includes_investment_plan(self):
        captured = {}
        llm = _structured_trader_llm(captured)
        trader = create_trader(llm)
        trader(_make_trader_state())
        # The investment plan is in the user message of the captured prompt.
        prompt = captured["prompt"]
        assert any("Proposed Investment Plan" in m["content"] for m in prompt)

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = (
            "**Action**: Sell\n\nGuidance cut hits margins.\n\n"
            "FINAL TRANSACTION PROPOSAL: **SELL**"
        )
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        assert result["trader_investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# Research Manager agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_rm_state():
    return {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "Bull and bear arguments here.",
            "bull_history": "Bull says...",
            "bear_history": "Bear says...",
            "current_response": "",
            "judge_decision": "",
            "count": 1,
        },
    }


def _structured_rm_llm(captured: dict, plan: ResearchPlan | None = None):
    if plan is None:
        plan = ResearchPlan(
            recommendation=PortfolioRating.HOLD,
            rationale="Balanced view across both sides.",
            strategic_actions="Hold current position; reassess after earnings.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or plan
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestResearchManagerAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        plan = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            rationale="Bull case is stronger; AI tailwind intact.",
            strategic_actions="Build position gradually over two weeks.",
        )
        llm = _structured_rm_llm(captured, plan)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        ip = result["investment_plan"]
        assert "**Recommendation**: Overweight" in ip
        assert "**Rationale**: Bull case" in ip
        assert "**Strategic Actions**: Build position" in ip

    def test_prompt_uses_5_tier_rating_scale(self):
        """The RM prompt must list all five tiers so the schema enum matches user expectations."""
        captured = {}
        llm = _structured_rm_llm(captured)
        rm = create_research_manager(llm)
        rm(_make_rm_state())
        prompt = captured["prompt"]
        for tier in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            assert f"**{tier}**" in prompt, f"missing {tier} in prompt"

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = "**Recommendation**: Sell\n\n**Rationale**: ...\n\n**Strategic Actions**: ..."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        assert result["investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# Sentiment Analyst: schema, render, structured happy path + fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderSentimentReport:
    def test_header_contains_band_and_score(self):
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.2,
            confidence="high",
            narrative="Source breakdown here.",
        )
        md = render_sentiment_report(report)
        assert "**Overall Sentiment:** **Bullish**" in md
        assert "(Score: 7.2/10)" in md

    def test_header_contains_confidence(self):
        report = SentimentReport(
            overall_band=SentimentBand.NEUTRAL,
            overall_score=5.0,
            confidence="low",
            narrative="Limited data.",
        )
        assert "**Confidence:** Low" in render_sentiment_report(report)

    def test_narrative_preserved_in_output(self):
        narrative = "## Breakdown\n\nStockTwits: 70% bullish.\n\n| Signal | Direction |\n|---|---|\n| News | Neutral |"
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BULLISH,
            overall_score=6.0,
            confidence="medium",
            narrative=narrative,
        )
        assert narrative in render_sentiment_report(report)

    def test_all_six_bands_render(self):
        for band in SentimentBand:
            report = SentimentReport(
                overall_band=band, overall_score=5.0,
                confidence="medium", narrative="n",
            )
            assert band.value in render_sentiment_report(report)

    def test_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            SentimentReport(
                overall_band=SentimentBand.BULLISH, overall_score=11.0,
                confidence="high", narrative="n",
            )

    def test_unknown_fields_are_rejected_by_the_structured_output_contract(self):
        payload = {
            "overall_band": "Neutral",
            "overall_score": 5.0,
            "confidence": "low",
            "narrative": "Available evidence was neutral.",
        }

        with pytest.raises(ValidationError):
            SentimentReport.model_validate(
                {**payload, "unknown_field": "must not be ignored"}
            )
        assert SentimentReport.model_json_schema()["additionalProperties"] is False


def _make_sentiment_state():
    return {
        "company_of_interest": "NVDA",
        "trade_date": "2026-01-15",
        "asset_type": "stock",
        "messages": [],
    }


def _structured_sentiment_llm(captured: dict, report: SentimentReport | None = None):
    """MagicMock LLM whose structured binding captures the prompt and returns
    a real SentimentReport so render_sentiment_report works."""
    if report is None:
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH, overall_score=7.5,
            confidence="high",
            narrative="StockTwits 75% bullish. News constructive. Reddit upbeat.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or report
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestSentimentAnalystAgent:
    @pytest.fixture(autouse=True)
    def sentiment_acquisition_blocks(self, monkeypatch):
        blocks: dict[str, str | None] = {
            "news": "One neutral headline.",
            "stocktwits": "StockTwits sentiment was neutral.",
            "reddit": None,
        }
        retrieved_at = "2026-01-15T00:00:00+00:00"

        def acquisition_result(
            source: str,
            *,
            tool_name: str,
            tool_call_id: str,
            source_ref: str,
            capability: str,
        ) -> AcquisitionResult[str]:
            provider = f"fixture-{source}"
            block = blocks[source]
            if block is None:
                unavailable = SourceAcquisitionUnavailable(
                    provider=provider,
                    capability=capability,
                    source_ref=source_ref,
                    attempt=1,
                    retrieved_at=retrieved_at,
                    retryable=False,
                    reason=AcquisitionUnavailableReason.NO_DATA,
                )
                return AcquisitionResult(
                    value=None,
                    artifact=None,
                    outcomes=(unavailable,),
                    provider=provider,
                )

            artifact = SourceArtifact(
                artifact_sha256=sha256(block.encode("utf-8")).hexdigest(),
                source_ref=source_ref,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                raw_text=block,
            )
            available = SourceAcquisitionAvailable(
                provider=provider,
                capability=capability,
                source_ref=source_ref,
                attempt=1,
                retrieved_at=retrieved_at,
                artifact=artifact,
            )
            return AcquisitionResult(
                value=block,
                artifact=artifact,
                outcomes=(available,),
                provider=provider,
            )

        def acquire_news(
            *_args,
            tool_call_id: str,
            source_ref: str,
            capability: str,
            **_kwargs,
        ) -> AcquisitionResult[str]:
            return acquisition_result(
                "news",
                tool_name="get_news",
                tool_call_id=tool_call_id,
                source_ref=source_ref,
                capability=capability,
            )

        def acquire_stocktwits(
            *_args,
            tool_call_id: str,
            source_ref: str,
            capability: str,
            **_kwargs,
        ) -> AcquisitionResult[str]:
            return acquisition_result(
                "stocktwits",
                tool_name="fetch_stocktwits_messages",
                tool_call_id=tool_call_id,
                source_ref=source_ref,
                capability=capability,
            )

        def acquire_reddit(
            *_args,
            tool_call_id: str,
            source_ref: str,
            capability: str,
            **_kwargs,
        ) -> AcquisitionResult[str]:
            return acquisition_result(
                "reddit",
                tool_name="fetch_reddit_posts",
                tool_call_id=tool_call_id,
                source_ref=source_ref,
                capability=capability,
            )

        monkeypatch.setattr(sentiment_module, "acquire_news", acquire_news)
        monkeypatch.setattr(
            sentiment_module,
            "acquire_stocktwits_messages",
            acquire_stocktwits,
        )
        monkeypatch.setattr(sentiment_module, "acquire_reddit_posts", acquire_reddit)
        return blocks

    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BEARISH, overall_score=4.0,
            confidence="medium", narrative="Mixed signals across sources.",
        )
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured, report))
        sr = analyst(_make_sentiment_state())["sentiment_report"]
        assert "**Overall Sentiment:** **Mildly Bearish**" in sr
        assert "(Score: 4.0/10)" in sr
        assert "Mixed signals across sources." in sr

    def test_sentiment_report_also_in_messages(self):
        captured = {}
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured))
        result = analyst(_make_sentiment_state())
        assert len(result["messages"]) == 1
        assert result["sentiment_report"] == result["messages"][0].content

    def test_prompt_states_claim_id_contract_enforced_by_validator(self):
        captured = {}
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured))

        analyst(_make_sentiment_state())

        prompt_text = "\n".join(str(message.content) for message in captured["prompt"])
        assert "claim_id must be unique" in prompt_text
        assert "must begin with `sentiment.`" in prompt_text

        claim_schema = SentimentReport.model_json_schema()["properties"][
            "material_claims"
        ]
        assert "unique" in claim_schema["description"]
        assert "'sentiment.'" in claim_schema["description"]

    def test_invalid_claim_namespace_is_repaired_once(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": "One neutral headline.",
                "stocktwits": "75% bullish across 20 messages.",
                "reddit": None,
            }
        )
        invalid_claim = SubmittedMaterialClaim(
            claim_id="claim_1",
            statement="StockTwits messages were 75% bullish.",
            source_ref="sentiment.stocktwits",
            source_quote="75% bullish across 20 messages.",
        )
        valid_claim = invalid_claim.model_copy(
            update={"claim_id": "sentiment.stocktwits_bullish_share"}
        )
        invalid_report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.0,
            confidence="medium",
            narrative="Retail sentiment was constructive.",
            material_claims=(invalid_claim,),
        )
        valid_report = invalid_report.model_copy(
            update={"material_claims": (valid_claim,)}
        )
        structured = MagicMock()
        structured.invoke.side_effect = [invalid_report, valid_report]
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        result = create_sentiment_analyst(llm)(_make_sentiment_state())

        assert structured.invoke.call_count == 2
        assert "Previous structured attempt failed" in str(
            structured.invoke.call_args.args[0][-1].content
        )
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert any(
            claim.claim_id == "sentiment.stocktwits_bullish_share"
            for claim in evidence.material_claims
        )
        assert EvidenceSource(
            source_id="analyst.sentiment.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_structured",
        ) in evidence.sources

    def test_structured_material_claims_merge_into_shared_evidence(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": "Two constructive headlines.",
                "stocktwits": (
                    "StockTwits messages were 75% bullish across 20 messages."
                ),
                "reddit": None,
            }
        )
        existing_claim = MaterialClaim(
            claim_id="market.latest_close",
            analyst="market",
            statement="The effective-date close was USD 189.50.",
            source_quote="The effective-date close was USD 189.50.",
            source_refs=("snapshot:NVDA:2026-01-15",),
        )
        sentiment_claim = MaterialClaim(
            claim_id="sentiment.stocktwits_bullish_share",
            analyst="sentiment",
            statement="StockTwits messages were 75% bullish.",
            source_quote="StockTwits messages were 75% bullish.",
            source_refs=("sentiment.stocktwits",),
        )
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.5,
            confidence="high",
            narrative="StockTwits sentiment was constructive.",
            material_claims=(sentiment_claim,),
        )
        state = _make_sentiment_state()
        state["evidence_state"] = EvidenceState(
            material_claims=(existing_claim,)
        ).model_dump(mode="json")

        result = create_sentiment_analyst(
            _structured_sentiment_llm({}, report)
        )(state)

        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert evidence.material_claims == (existing_claim, sentiment_claim)
        assert evidence.sources == (
            EvidenceSource(
                source_id="analyst.sentiment.submission",
                status=EvidenceStatus.AVAILABLE,
                required=True,
                detail="direct_structured",
            ),
            EvidenceSource(
                source_id="sentiment.news",
                status=EvidenceStatus.AVAILABLE,
                required=False,
                detail="",
            ),
            EvidenceSource(
                source_id="sentiment.stocktwits",
                status=EvidenceStatus.AVAILABLE,
                required=False,
                detail="",
            ),
            EvidenceSource(
                source_id="sentiment.reddit",
                status=EvidenceStatus.UNAVAILABLE,
                required=False,
                detail="acquired_reddit",
            ),
        )

    def test_numeric_claim_absent_from_sentiment_block_is_conflicted(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": "Two constructive headlines.",
                "stocktwits": "75% bullish across 20 messages.",
                "reddit": None,
            }
        )
        fabricated = MaterialClaim(
            claim_id="sentiment.stocktwits_bullish_share",
            analyst="sentiment",
            statement="StockTwits messages were 95% bullish.",
            source_quote="StockTwits messages were 95% bullish.",
            source_refs=("sentiment.stocktwits",),
        )
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.5,
            confidence="high",
            narrative="StockTwits sentiment was constructive.",
            material_claims=(fabricated,),
        )

        result = create_sentiment_analyst(
            _structured_sentiment_llm({}, report)
        )(_make_sentiment_state())

        evidence = EvidenceState.model_validate(result["evidence_state"])
        source = next(
            item
            for item in evidence.sources
            if item.source_id == "sentiment.stocktwits"
        )
        assert source.status is EvidenceStatus.AVAILABLE
        validation = next(
            item for item in evidence.claim_validations
            if item.claim_id == fabricated.claim_id
        )
        assert validation.status is ClaimValidationStatus.UNSUPPORTED
        assert validation.detail == "source quote is absent from the cited source block"

    def test_nonnumeric_claim_absent_from_sentiment_block_is_conflicted(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": "Two constructive headlines.",
                "stocktwits": "StockTwits sentiment was bullish.",
                "reddit": None,
            }
        )
        fabricated = MaterialClaim(
            claim_id="sentiment.stocktwits_direction",
            analyst="sentiment",
            statement="StockTwits sentiment was bearish.",
            source_quote="StockTwits sentiment was bearish.",
            source_refs=("sentiment.stocktwits",),
        )
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.5,
            confidence="high",
            narrative="StockTwits sentiment was constructive.",
            material_claims=(fabricated,),
        )

        result = create_sentiment_analyst(
            _structured_sentiment_llm({}, report)
        )(_make_sentiment_state())

        evidence = EvidenceState.model_validate(result["evidence_state"])
        source = next(
            item
            for item in evidence.sources
            if item.source_id == "sentiment.stocktwits"
        )
        assert source.status is EvidenceStatus.AVAILABLE
        validation = next(
            item for item in evidence.claim_validations
            if item.claim_id == fabricated.claim_id
        )
        assert validation.status is ClaimValidationStatus.UNSUPPORTED
        assert validation.detail == "source quote is absent from the cited source block"

    def test_prompt_contains_ticker(self):
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_make_sentiment_state())
        assert any("NVDA" in str(m) for m in captured["prompt"])

    @pytest.mark.parametrize(
        "bind_error",
        (
            NotImplementedError("provider unsupported"),
            ValueError("provider rejects this response format"),
        ),
    )
    def test_structured_unavailable_is_explicitly_unavailable(
        self,
        sentiment_acquisition_blocks,
        bind_error,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": None,
                "stocktwits": None,
                "reddit": None,
            }
        )
        plain = "**Overall Sentiment:** **Bearish** (Score: 3.0/10)\n**Confidence:** Low\n\nLimited data."
        llm = MagicMock()
        llm.with_structured_output.side_effect = bind_error
        llm.invoke.return_value = MagicMock(content=plain)
        result = create_sentiment_analyst(llm)(_make_sentiment_state())

        assert result["sentiment_report"].startswith("ANALYSIS_UNAVAILABLE:")
        assert plain not in result["sentiment_report"]
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert EvidenceSource(
            source_id="analyst.sentiment.submission",
            status=EvidenceStatus.UNAVAILABLE,
            required=True,
            detail="unsupported",
        ) in evidence.sources
        llm.invoke.assert_not_called()

    def test_structured_call_failure_is_explicitly_unavailable(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": None,
                "stocktwits": None,
                "reddit": None,
            }
        )
        plain = "Fallback free-text sentiment."
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("bad JSON from model")
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        llm.invoke.return_value = MagicMock(content=plain)
        result = create_sentiment_analyst(llm)(_make_sentiment_state())

        assert result["sentiment_report"].startswith("ANALYSIS_UNAVAILABLE:")
        assert plain not in result["sentiment_report"]
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert EvidenceSource(
            source_id="analyst.sentiment.submission",
            status=EvidenceStatus.UNAVAILABLE,
            required=True,
            detail="validation_error",
        ) in evidence.sources
        llm.invoke.assert_not_called()
        assert structured.invoke.call_count == 2

    def test_none_parsed_sentiment_is_repaired_once(
        self,
        sentiment_acquisition_blocks,
    ):
        sentiment_acquisition_blocks.update(
            {
                "news": "One neutral headline.",
                "stocktwits": None,
                "reddit": None,
            }
        )
        report = SentimentReport(
            overall_band=SentimentBand.NEUTRAL,
            overall_score=5.0,
            confidence="low",
            narrative="Available news was neutral.",
            material_claims=(),
        )
        structured = MagicMock()
        structured.invoke.side_effect = [None, report]
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        result = create_sentiment_analyst(llm)(_make_sentiment_state())

        assert result["sentiment_report"].startswith("**Overall Sentiment:** **Neutral**")
        assert structured.invoke.call_count == 2
        evidence = EvidenceState.model_validate(result["evidence_state"])
        assert EvidenceSource(
            source_id="analyst.sentiment.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="direct_structured",
        ) in evidence.sources
