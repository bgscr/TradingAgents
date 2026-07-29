from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from typer.testing import CliRunner
from typing_extensions import NotRequired

import cli.main as cli_main
import tradingagents.dataflows.akshare_data as akshare_data
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as vendor_interface
import tradingagents.dataflows.market_snapshot as market_snapshot
from tradingagents.agents.analysts.submission import (
    render_allowed_source_ref_catalog,
    source_ref_tool_call_ids,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialToolMessageAuditEnvelope,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    MaterialClaim,
    build_tool_evidence_state,
    decision_ready_material_claims,
    render_analysis_outcome,
)
from tradingagents.graph.checkpointer import get_checkpointer, thread_id
from tradingagents.graph.financial_tools import FinancialDispatchToolNode
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history import MarketHistoryConfig, MarketHistoryStore
from tradingagents.reporting import write_report_tree


class _FinancialGraphState(MessagesState):
    evidence_state: dict[str, Any]
    asset_configuration: dict[str, Any]
    trade_date: str
    financial_dispatch_ledger: NotRequired[dict[str, Any]]


def _compiled_financial_tool_graph():
    workflow = StateGraph(_FinancialGraphState)
    workflow.add_node(
        "tools_fundamentals",
        TradingAgentsGraph._create_tool_nodes(None)["fundamentals"],
    )
    workflow.add_edge(START, "tools_fundamentals")
    workflow.add_edge("tools_fundamentals", END)
    return workflow.compile()


def _financial_vendor_methods(balance_sheet_provider):
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    return {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": balance_sheet_provider},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }


def _analysis_outcome_state(evidence: EvidenceState, *, ticker: str) -> dict[str, Any]:
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE,),
    )
    return {
        "company_of_interest": ticker,
        "trade_date": "2026-07-28",
        "asset_type": "stock",
        "graph_signature": "analysts=fundamentals|evidence_schema=4",
        "evidence_gate_mode": "enforce",
        "evidence_state": evidence.model_dump(mode="json"),
        "evidence_preflight": {
            "passed": False,
            "readiness": "insufficient",
            "blockers": ["authoritative market snapshot is missing"],
            "diagnostic_codes": ["snapshot_unavailable"],
        },
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
    }


def test_compiled_financial_graph_suppresses_duplicate_calls_with_distinct_ids(
    monkeypatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_calls = 0

    def balance_sheet_provider(symbol: str, frequency: str, current_date: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(
        ("get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"),
        "scripted",
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(
        vendor_interface,
        "VENDOR_METHODS",
        _financial_vendor_methods(balance_sheet_provider),
    )
    calls = [
        {
            "name": "get_balance_sheet",
            "args": {
                "ticker": "601328.SS",
                "freq": "quarterly",
                "curr_date": "2026-07-28",
            },
            "id": tool_call_id,
            "type": "tool_call",
        }
        for tool_call_id in ("balance-sheet-1", "balance-sheet-2")
    ]

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=calls)],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )

    tool_messages = tuple(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    envelopes = tuple(
        FinancialToolMessageAuditEnvelope.model_validate(message.artifact)
        for message in tool_messages
    )
    assert provider_calls == 1
    assert [message.tool_call_id for message in tool_messages] == [
        "balance-sheet-1",
        "balance-sheet-2",
    ]
    assert [envelope.disposition for envelope in envelopes] == [
        "executed",
        "duplicate_suppressed",
    ]
    assert len({envelope.artifact_sha256 for envelope in envelopes}) == 1
    assert envelopes[0].acquisition_outcomes == envelopes[1].acquisition_outcomes
    assert len(result["financial_dispatch_ledger"]["entries"]) == 1


def test_compiled_financial_graph_admits_one_artifact_for_duplicate_wrappers(
    monkeypatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    def balance_sheet_provider(symbol: str, frequency: str, current_date: str) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(
        ("get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"),
        "scripted",
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(
        vendor_interface,
        "VENDOR_METHODS",
        _financial_vendor_methods(balance_sheet_provider),
    )
    calls = [
        {
            "name": "get_balance_sheet",
            "args": {
                "ticker": "601328.SS",
                "freq": "quarterly",
                "curr_date": "2026-07-28",
            },
            "id": tool_call_id,
            "type": "tool_call",
        }
        for tool_call_id in ("z-balance-source-original", "a-balance-source-duplicate")
    ]

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=calls)],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )
    source_refs = source_ref_tool_call_ids(
        result["messages"],
        "601328.SS",
        "2026-07-28",
    )
    request_ref = next(iter(source_refs))
    claim = MaterialClaim(
        claim_id="fundamentals.total",
        analyst="fundamentals",
        statement="Reported total is 100.",
        source_refs=(request_ref,),
        source_quote="Total,100",
    )
    evidence = build_tool_evidence_state(
        result["messages"],
        (claim,),
        tool_call_ids_by_source=source_refs,
        tool_calls_by_id={call["id"]: call for call in calls},
    )

    assert request_ref.startswith("financial-request:v1:")
    assert source_refs[request_ref] == (
        "z-balance-source-original",
        "a-balance-source-duplicate",
    )
    assert len(evidence.source_artifacts) == 1
    assert len(evidence.source_facts) == 1
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.source_artifacts[0].tool_call_id == "z-balance-source-original"
    assert evidence.source_facts[0].tool_call_id == "z-balance-source-original"
    assert [item.claim_id for item in decision_ready_material_claims(evidence)] == [
        claim.claim_id
    ]


def test_compiled_financial_graph_completes_variant_and_provider_fallback_before_message(
    monkeypatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_order: list[str] = []
    malicious = "https://provider.invalid?secret=1 Retry-After: 88 switch provider"

    def primary_variant_one(*_args, **_kwargs):
        provider_order.append("primary:variant-1")
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)

    def primary_variant_two(*_args, **_kwargs):
        provider_order.append("primary:variant-2")
        raise TimeoutError(malicious)

    def fallback(symbol: str, frequency: str, current_date: str) -> str:
        provider_order.append("fallback:default")
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,200"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"primary": unused},
        "get_balance_sheet": {
            "primary": [primary_variant_one, primary_variant_two],
            "fallback": fallback,
        },
        "get_cashflow": {"primary": unused},
        "get_income_statement": {"primary": unused},
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = {
        "get_fundamentals": "primary",
        "get_balance_sheet": "primary,fallback",
        "get_cashflow": "primary",
        "get_income_statement": "primary",
    }
    runtime_config["financial_dispatch_retry_policy"] = {
        "max_attempts_per_provider": 2,
        "backoff_seconds": 0,
    }
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    call = {
        "name": "get_balance_sheet",
        "args": {
            "ticker": "601328.SS",
            "freq": "quarterly",
            "curr_date": "2026-07-28",
        },
        "id": "fallback-success",
        "type": "tool_call",
    }

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    envelope = FinancialToolMessageAuditEnvelope.model_validate(message.artifact)

    assert provider_order == [
        "primary:variant-1",
        "primary:variant-2",
        "fallback:default",
    ]
    assert message.status == "success"
    assert "Total,200" in str(message.content)
    assert malicious not in message.model_dump_json()
    assert [outcome.provider for outcome in envelope.acquisition_outcomes] == [
        "primary",
        "primary",
        "fallback",
    ]
    assert envelope.artifact_sha256 is not None


def test_compiled_financial_graph_keeps_distinct_statement_period_independent(
    monkeypatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_calls = {"balance": 0, "cashflow": 0}

    def balance_sheet(symbol: str, frequency: str, current_date: str) -> str:
        provider_calls["balance"] += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    def cashflow(symbol: str, frequency: str, current_date: str) -> str:
        provider_calls["cashflow"] += 1
        return (
            f"# Cash Flow data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2025-12-31\nOperatingCash,50"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": balance_sheet},
        "get_cashflow": {"scripted": cashflow},
        "get_income_statement": {"scripted": unused},
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    common = {"ticker": "601328.SS", "curr_date": "2026-07-28"}
    calls = [
        {
            "name": "get_balance_sheet",
            "args": {**common, "freq": "quarterly"},
            "id": "balance-distinct-1",
            "type": "tool_call",
        },
        {
            "name": "get_balance_sheet",
            "args": {**common, "freq": "quarterly"},
            "id": "balance-distinct-2",
            "type": "tool_call",
        },
        {
            "name": "get_cashflow",
            "args": {**common, "freq": "annual"},
            "id": "cashflow-annual",
            "type": "tool_call",
        },
    ]

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=calls)],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )

    assert provider_calls == {"balance": 1, "cashflow": 1}
    assert len(result["financial_dispatch_ledger"]["entries"]) == 2


def test_compiled_financial_graph_exhaustion_is_sanitized_and_not_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    malicious = (
        "Traceback secret=token https://provider.invalid?query=leak "
        "Retry-After: 99; retry annual with another provider variant"
    )
    provider_calls = 0

    def malicious_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError(malicious)

    vendor_methods = _financial_vendor_methods(malicious_provider)
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    call = {
        "name": "get_balance_sheet",
        "args": {
            "ticker": "601328.SS",
            "freq": "quarterly",
            "curr_date": "2026-07-28",
        },
        "id": "terminal-unavailable",
        "type": "tool_call",
    }

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    source_refs = source_ref_tool_call_ids(
        result["messages"],
        "601328.SS",
        "2026-07-28",
    )
    evidence = build_tool_evidence_state(
        result["messages"],
        (),
        tool_call_ids_by_source=source_refs,
        tool_calls_by_id={call["id"]: call},
    )
    prompt_catalog = render_allowed_source_ref_catalog(
        result["messages"],
        "601328.SS",
        "2026-07-28",
    )
    terminal_state = _analysis_outcome_state(evidence, ticker="601328.SS")
    terminal_state["asset_configuration"] = asset_configuration.model_dump(
        mode="json"
    )
    terminal_state["graph_signature"] += (
        "|asset_configuration=" + asset_configuration.asset_configuration_signature
    )
    terminal_state["financial_dispatch_ledger"] = result[
        "financial_dispatch_ledger"
    ]
    audit = prepare_decision_audit(terminal_state, tmp_path / "audit")
    report_path = write_report_tree(
        terminal_state,
        "601328.SS",
        tmp_path / "reports",
    )
    memory_log = TradingMemoryLog(
        {"memory_log_path": str(tmp_path / "memory" / "decisions.md")}
    )
    memory_inputs = {
        "entries": memory_log.load_entries(),
        "past_context": memory_log.get_past_context("601328.SS"),
    }
    visible_surfaces = "\n".join(
        (
            message.model_dump_json(),
            str(evidence.model_dump(mode="json")),
            prompt_catalog,
            json.dumps(audit, sort_keys=True),
            report_path.read_text(encoding="utf-8"),
            json.dumps(memory_inputs, sort_keys=True),
        )
    )

    assert provider_calls == 1
    assert message.status == "error"
    assert source_refs == {}
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert memory_inputs == {"entries": [], "past_context": ""}
    assert "Allowed source_refs for this turn: none" in prompt_catalog
    assert audit["financial_dispatch"]["request_count"] == 1
    assert audit["financial_dispatch"]["acquisition_attempt_count"] == 1
    assert audit["financial_dispatch"]["duplicate_suppressed_count"] == 0
    audited_request = audit["financial_dispatch"]["requests"][0]
    assert audited_request["artifact_sha256"] is None
    assert audited_request["outcomes"][0]["reason"] == "provider_error"
    assert "## Financial dispatch operations" in report_path.read_text(
        encoding="utf-8"
    )
    for forbidden in (
        malicious,
        "provider.invalid",
        "secret=token",
        "Retry-After",
        "another provider",
        "variant",
    ):
        assert forbidden not in visible_surfaces


def test_financial_audit_projection_rejects_forged_request_and_artifact_bindings(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    def balance_sheet_provider(symbol: str, frequency: str, current_date: str) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    def unavailable_provider(*_args, **_kwargs):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.NO_DATA,
            retryable=False,
        )

    vendor_methods = _financial_vendor_methods(balance_sheet_provider)
    vendor_methods["get_balance_sheet"] = {
        "primary": unavailable_provider,
        "fallback": balance_sheet_provider,
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    runtime_config["tool_vendors"]["get_balance_sheet"] = "primary,fallback"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    call = {
        "name": "get_balance_sheet",
        "args": {
            "ticker": "601328.SS",
            "freq": "quarterly",
            "curr_date": "2026-07-28",
        },
        "id": "audit-integrity",
        "type": "tool_call",
    }
    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )
    corruptions = []
    forged_request = copy.deepcopy(result["financial_dispatch_ledger"])
    forged_request["entries"][0]["canonical_request_key"]["request_key"] = (
        "financial-request:v1:" + "f" * 64
    )
    corruptions.append(
        (
            forged_request,
            FinancialDispatchCheckpointFailureReason.CANONICAL_REQUEST_MISMATCH,
        )
    )
    forged_artifact = copy.deepcopy(result["financial_dispatch_ledger"])
    forged_artifact["entries"][0]["artifact_sha256"] = "b" * 64
    corruptions.append(
        (
            forged_artifact,
            FinancialDispatchCheckpointFailureReason.ARTIFACT_REFERENCE_INVALID,
        )
    )
    truncated_plan = copy.deepcopy(result["financial_dispatch_ledger"])
    truncated_entry = truncated_plan["entries"][0]
    first_outcome = truncated_entry["terminal"]["plan_outcomes"][0]
    truncated_entry["provider_variant_progress"] = [
        truncated_entry["provider_variant_progress"][0]
    ]
    truncated_entry["consumed_attempt_budget"] = 1
    truncated_entry["terminal"] = {
        "value": None,
        "artifact": None,
        "plan_outcomes": [first_outcome],
        "terminal_outcome": first_outcome["outcome"],
        "provider": None,
        "variant_id": None,
    }
    truncated_entry["artifact_sha256"] = None
    corruptions.append(
        (
            truncated_plan,
            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID,
        )
    )

    for ledger, expected_reason in corruptions:
        terminal_state = _analysis_outcome_state(EvidenceState(), ticker="601328.SS")
        terminal_state["asset_configuration"] = asset_configuration.model_dump(
            mode="json"
        )
        terminal_state["graph_signature"] += (
            "|asset_configuration="
            + asset_configuration.asset_configuration_signature
        )
        terminal_state["financial_dispatch_ledger"] = ledger
        with pytest.raises(FinancialDispatchCheckpointError) as raised:
            prepare_decision_audit(terminal_state, tmp_path / expected_reason.value)
        assert raised.value.reason is expected_reason


def test_compiled_mainland_financial_transport_has_exact_published_cardinality(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    history_root = tmp_path / "history"
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(
                history_root / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "akshare",
    )
    transport_calls: list[str] = []

    def business(**_kwargs: object) -> pd.DataFrame:
        transport_calls.append("stock_zyjs_ths")
        return pd.DataFrame([{"主营业务": "retail banking"}])

    def financial_abstract(**_kwargs: object) -> pd.DataFrame:
        transport_calls.append("stock_financial_abstract")
        return pd.DataFrame(
            [{"选项": "常用指标", "指标": "总资产", "20260630": "100"}]
        )

    def fund_flow(**_kwargs: object) -> pd.DataFrame:
        transport_calls.append("stock_individual_fund_flow")
        return pd.DataFrame([{"日期": "2026-07-28", "收盘价": 10}])

    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", business)
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        financial_abstract,
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        fund_flow,
    )
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    monkeypatch.setattr(
        vendor_interface,
        "VENDOR_METHODS",
        {
            "get_fundamentals": {"akshare": akshare_data.get_fundamentals},
            "get_balance_sheet": {"akshare": unused},
            "get_cashflow": {"akshare": unused},
            "get_income_statement": {"akshare": unused},
        },
    )
    call = {
        "name": "get_fundamentals",
        "args": {"ticker": "601328.SS", "curr_date": "2026-07-28"},
        "id": "mainland-financial-cardinality",
        "type": "tool_call",
    }
    initial_state = {
        "messages": [AIMessage(content="", tool_calls=[call])],
        "evidence_state": EvidenceState(
            instrument_identity=asset_configuration.instrument_identity
        ).model_dump(mode="json"),
        "asset_configuration": asset_configuration.model_dump(mode="json"),
        "trade_date": "2026-07-28",
    }

    with market_snapshot.authoritative_snapshot_run():
        graph_result = _compiled_financial_tool_graph().invoke(initial_state)
    graph_evidence = EvidenceState.model_validate(graph_result["evidence_state"])
    checkpointed_attempt_evidence = EvidenceState(
        physical_attempt_events=graph_evidence.physical_attempt_events,
        physical_attempt_count=graph_evidence.physical_attempt_count,
    )

    # A resumed process has a new run context. Checkpointed evidence must retain
    # the already-consumed physical attempts when the terminal audit is published.
    with market_snapshot.authoritative_snapshot_run():
        terminal_state = _analysis_outcome_state(
            checkpointed_attempt_evidence,
            ticker="601328.SS",
        )
        audit = prepare_decision_audit(terminal_state, tmp_path / "audit")
        write_report_tree(terminal_state, "601328.SS", tmp_path / "reports")
    report = (
        tmp_path / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")
    history_config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(history_config) as store:
        persisted_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert transport_calls == [
        "stock_zyjs_ths",
        "stock_financial_abstract",
        "stock_individual_fund_flow",
    ]
    assert persisted_count == (3,)
    assert graph_evidence.physical_attempt_count == 3
    assert len(graph_evidence.physical_attempt_events) == 3
    assert audit["evidence_state"]["physical_attempt_count"] == 3
    assert len(audit["evidence_state"]["physical_attempt_events"]) == 3
    assert report.count("### Physical attempt") == 3
    assert "**Total physical-attempt count:** 3" in report


def test_compiled_financial_duplicate_after_checkpoint_resume_uses_zero_provider_io(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_calls = 0

    def balance_sheet_provider(symbol: str, frequency: str, current_date: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(
        ("get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"),
        "scripted",
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(
        vendor_interface,
        "VENDOR_METHODS",
        _financial_vendor_methods(balance_sheet_provider),
    )
    resume_allowed = False

    def scripted_model(state: _FinancialGraphState) -> dict[str, Any]:
        nonlocal resume_allowed
        tool_messages = tuple(
            message for message in state["messages"] if isinstance(message, ToolMessage)
        )
        if not tool_messages:
            call_id = "before-checkpoint"
        elif len(tool_messages) == 1:
            if not resume_allowed:
                raise RuntimeError("simulated interruption after financial checkpoint")
            call_id = "after-checkpoint"
        else:
            return {"messages": [AIMessage(content="complete")]}
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_balance_sheet",
                            "args": {
                                "ticker": "601328.SS",
                                "freq": "quarterly",
                                "curr_date": "2026-07-28",
                            },
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def route_after_model(state: _FinancialGraphState):
        last_message = state["messages"][-1]
        return "tools" if getattr(last_message, "tool_calls", ()) else END

    workflow = StateGraph(_FinancialGraphState)
    workflow.add_node("scripted_model", scripted_model)
    workflow.add_node(
        "tools",
        TradingAgentsGraph._create_tool_nodes(None)["fundamentals"],
    )
    workflow.add_edge(START, "scripted_model")
    workflow.add_conditional_edges("scripted_model", route_after_model, ["tools", END])
    workflow.add_edge("tools", "scripted_model")
    checkpoint_config = {"configurable": {"thread_id": "financial-graph-resume"}}
    initial_state = {
        "messages": [],
        "evidence_state": EvidenceState(
            instrument_identity=asset_configuration.instrument_identity
        ).model_dump(mode="json"),
        "asset_configuration": asset_configuration.model_dump(mode="json"),
        "trade_date": "2026-07-28",
    }

    with get_checkpointer(tmp_path, "601328.SS") as saver:
        graph = workflow.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            graph.invoke(initial_state, config=checkpoint_config)
    assert provider_calls == 1

    resume_allowed = True
    with get_checkpointer(tmp_path, "601328.SS") as saver:
        resumed = workflow.compile(checkpointer=saver).invoke(
            None,
            config=checkpoint_config,
        )

    tool_messages = tuple(
        message for message in resumed["messages"] if isinstance(message, ToolMessage)
    )
    assert provider_calls == 1
    assert [message.tool_call_id for message in tool_messages] == [
        "before-checkpoint",
        "after-checkpoint",
    ]
    assert len(resumed["financial_dispatch_ledger"]["entries"]) == 1
    assert resumed["financial_dispatch_ledger"]["entries"][0]["reuse_count"] == 1


def test_financial_checkpoint_validation_fails_before_resume_model_or_provider_work(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_calls = 0

    def balance_sheet_provider(symbol: str, frequency: str, current_date: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    vendor_methods = _financial_vendor_methods(balance_sheet_provider)
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "checkpoint_enabled": True,
            "data_cache_dir": str(tmp_path),
            "results_dir": str(tmp_path / "results"),
        }
    )
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    request = {
        "name": "get_balance_sheet",
        "args": {
            "ticker": "601328.SS",
            "freq": "quarterly",
            "curr_date": "2026-07-28",
        },
        "id": "checkpoint-source",
        "type": "tool_call",
    }
    initial_state = {
        "messages": [AIMessage(content="", tool_calls=[request])],
        "evidence_state": EvidenceState(
            instrument_identity=asset_configuration.instrument_identity
        ).model_dump(mode="json"),
        "asset_configuration": asset_configuration.model_dump(mode="json"),
        "trade_date": "2026-07-28",
    }
    completed = _compiled_financial_tool_graph().invoke(initial_state)
    assert provider_calls == 1
    corrupted_state = copy.deepcopy(completed)
    contradictory_asset = asset_configuration.model_copy(
        update={"registry_digest": "b" * 64}
    )
    corrupted_state["asset_configuration"] = contradictory_asset.model_dump(
        mode="json"
    )
    corrupted_state["financial_dispatch_ledger"][
        "run_asset_configuration_signature"
    ] = contradictory_asset.asset_configuration_signature

    model_calls = 0
    allow_resume = False

    def resumable_model(_state: _FinancialGraphState) -> dict[str, Any]:
        nonlocal model_calls
        model_calls += 1
        if not allow_resume:
            raise RuntimeError("seed interrupted before model completion")
        return {"messages": [AIMessage(content="resume should not run")]}

    workflow = StateGraph(_FinancialGraphState)
    workflow.add_node("fundamentals_model", resumable_model)
    workflow.add_edge(START, "fundamentals_model")
    workflow.add_edge("fundamentals_model", END)
    subject = TradingAgentsGraph.__new__(TradingAgentsGraph)
    subject.config = runtime_config
    subject.asset_configuration = asset_configuration
    subject.selected_analysts = ("fundamentals",)
    subject.decision_policy = None
    subject.decision_horizon = None
    subject.workflow = workflow
    subject.graph = workflow.compile()
    subject.tool_nodes = {
        "fundamentals": FinancialDispatchToolNode(
            config=runtime_config,
            vendor_methods=vendor_methods,
        )
    }
    signature = subject._run_signature("stock")
    checkpoint_config = {
        "configurable": {
            "thread_id": thread_id("601328.SS", "2026-07-28", signature)
        }
    }
    with get_checkpointer(tmp_path, "601328.SS") as saver:
        checkpointed = workflow.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="seed interrupted"):
            checkpointed.invoke(corrupted_state, config=checkpoint_config)

    provider_calls = 0
    model_calls = 0
    allow_resume = True
    with (
        pytest.raises(FinancialDispatchCheckpointError) as raised,
        subject.checkpoint_scope("601328.SS", "2026-07-28", "stock") as session,
    ):
        subject.graph.invoke(None, config=session.graph_config)

    assert (
        raised.value.reason
        is FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
    )
    assert provider_calls == 0
    assert model_calls == 0


def test_public_cli_financial_dispatch_acceptance_is_deterministic_and_audited(
    monkeypatch,
    tmp_path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    malicious = (
        "Traceback secret=token https://provider.invalid?query=leak "
        "Retry-After: 99; retry annual with another provider variant"
    )
    provider_calls = {"balance_primary": 0, "balance_fallback": 0, "cashflow": 0}

    def balance_primary(*_args, **_kwargs):
        provider_calls["balance_primary"] += 1
        raise RuntimeError(malicious)

    def balance_fallback(symbol: str, frequency: str, current_date: str) -> str:
        provider_calls["balance_fallback"] += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    def cashflow(symbol: str, frequency: str, current_date: str) -> str:
        provider_calls["cashflow"] += 1
        return (
            f"# Cash Flow data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2025-12-31\nOperatingCash,50"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"unused": unused},
        "get_balance_sheet": {
            "primary": balance_primary,
            "fallback": balance_fallback,
        },
        "get_cashflow": {"cashflow": cashflow},
        "get_income_statement": {"unused": unused},
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "checkpoint_enabled": False,
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "runs"),
            "financial_dispatch_retry_policy": {
                "max_attempts_per_provider": 1,
                "backoff_seconds": 0,
            },
        }
    )
    runtime_config["tool_vendors"] = {
        "get_fundamentals": "unused",
        "get_balance_sheet": "primary,fallback",
        "get_cashflow": "cashflow",
        "get_income_statement": "unused",
    }
    calls = [
        {
            "name": "get_balance_sheet",
            "args": {
                "ticker": "601328.SS",
                "freq": "quarterly",
                "curr_date": "2026-07-28",
            },
            "id": call_id,
            "type": "tool_call",
        }
        for call_id in ("cli-balance-1", "cli-balance-2")
    ]
    calls.append(
        {
            "name": "get_cashflow",
            "args": {
                "ticker": "601328.SS",
                "freq": "annual",
                "curr_date": "2026-07-28",
            },
            "id": "cli-cashflow-annual",
            "type": "tool_call",
        }
    )
    tool_calls_by_id = {call["id"]: call for call in calls}

    def scripted_model(_state: _FinancialGraphState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="", tool_calls=calls)]}

    workflow = StateGraph(_FinancialGraphState)
    workflow.add_node("scripted_model", scripted_model)
    workflow.add_node(
        "tools_fundamentals",
        FinancialDispatchToolNode(
            config=runtime_config,
            vendor_methods=vendor_methods,
        ),
    )
    workflow.add_edge(START, "scripted_model")
    workflow.add_edge("scripted_model", "tools_fundamentals")
    workflow.add_edge("tools_fundamentals", END)
    compiled = workflow.compile()

    class CliFinancialStream:
        def stream(self, initial_state, **_kwargs):
            result = compiled.invoke(initial_state)
            source_refs = source_ref_tool_call_ids(
                result["messages"],
                "601328.SS",
                "2026-07-28",
            )
            evidence = build_tool_evidence_state(
                result["messages"],
                (),
                tool_call_ids_by_source=source_refs,
                tool_calls_by_id=tool_calls_by_id,
            )
            yield {
                **result,
                **_analysis_outcome_state(evidence, ticker="601328.SS"),
            }

    class CliPropagator:
        @staticmethod
        def get_graph_args(*_args, **_kwargs):
            return {}

    class CliFinancialGraph:
        def __init__(self, *_args, **_kwargs):
            self.asset_configuration = asset_configuration
            self.graph = CliFinancialStream()
            self.propagator = CliPropagator()

        @staticmethod
        def resolve_evidence_state(*_args, **_kwargs):
            return EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            )

        def create_initial_state(self, *_args, evidence_state, **_kwargs):
            return {
                "messages": [],
                "company_of_interest": "601328.SS",
                "trade_date": "2026-07-28",
                "asset_type": "stock",
                "asset_configuration": asset_configuration.model_dump(mode="json"),
                "evidence_state": evidence_state.model_dump(mode="json"),
                "run_id": "run:" + "a" * 64,
            }

        @staticmethod
        def _run_signature(_asset_type: str) -> str:
            return (
                "analysts=fundamentals|evidence_schema=4|asset_configuration="
                + asset_configuration.asset_configuration_signature
            )

    class NoOpDisplay:
        def start(self):
            return None

        def refresh(self, *_args, **_kwargs):
            return None

        def publish_event(self, *_args, **_kwargs):
            return None

        def report_ready(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-28",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="fundamentals")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "scripted-quick",
        "deep_thinker": "scripted-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", runtime_config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", CliFinancialGraph)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: NoOpDisplay(),
    )
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    result = CliRunner().invoke(cli_main.app, [])

    assert result.exit_code == 0, result.output
    assert provider_calls == {
        "balance_primary": 1,
        "balance_fallback": 1,
        "cashflow": 1,
    }
    audit_path = next((tmp_path / "runs").rglob("decision-audit.json"))
    audit_text = audit_path.read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    report_text = next((tmp_path / "runs").rglob("complete_report.md")).read_text(
        encoding="utf-8"
    )
    assert audit["financial_dispatch"]["request_count"] == 2
    assert audit["financial_dispatch"]["acquisition_attempt_count"] == 3
    assert audit["financial_dispatch"]["duplicate_suppressed_count"] == 1
    assert "## Financial dispatch operations" in report_text
    assert "**Acquisition attempt count:** 3" in report_text
    assert "**Duplicate-suppressed call count:** 1" in report_text
    for forbidden in (
        malicious,
        "provider.invalid",
        "secret=token",
        "Retry-After",
        "another provider",
        "variant",
    ):
        assert forbidden not in audit_text
        assert forbidden not in report_text


def test_only_fundamentals_tool_route_is_replaced() -> None:
    nodes = TradingAgentsGraph._create_tool_nodes(None)

    assert isinstance(nodes["fundamentals"], FinancialDispatchToolNode)
    assert all(isinstance(nodes[name], ToolNode) for name in ("market", "social", "news"))


@pytest.mark.parametrize(
    ("tool_name", "frequency", "heading"),
    [
        ("get_fundamentals", None, "Company Fundamentals"),
        ("get_balance_sheet", "quarterly", "Balance Sheet"),
        ("get_cashflow", "annual", "Cash Flow"),
        ("get_income_statement", "quarterly", "Income Statement"),
    ],
)
def test_every_compiled_financial_capability_uses_dispatcher(
    monkeypatch,
    tool_name: str,
    frequency: str | None,
    heading: str,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_calls = 0

    def provider(symbol: str, *arguments: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        if tool_name == "get_fundamentals":
            return (
                f"# Company Fundamentals for {symbol}\n"
                "# Data retrieved on: 2026-07-28 12:00:00\n\n"
                "Name: Deterministic Company\nMarket Cap: 100"
            )
        return (
            f"# {heading} data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        name: {"scripted": provider if name == tool_name else unused}
        for name in (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        )
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    arguments = {"ticker": "601328.SS", "curr_date": "2026-07-28"}
    if frequency is not None:
        arguments["freq"] = frequency
    call = {
        "name": tool_name,
        "args": arguments,
        "id": f"compiled-{tool_name}",
        "type": "tool_call",
    }

    result = _compiled_financial_tool_graph().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "evidence_state": EvidenceState(
                instrument_identity=asset_configuration.instrument_identity
            ).model_dump(mode="json"),
            "asset_configuration": asset_configuration.model_dump(mode="json"),
            "trade_date": "2026-07-28",
        }
    )
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    envelope = FinancialToolMessageAuditEnvelope.model_validate(message.artifact)

    assert provider_calls == 1
    assert message.name == tool_name
    assert message.status == "success"
    assert envelope.disposition == "executed"
    assert envelope.artifact_sha256 is not None
