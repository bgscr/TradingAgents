"""Real routing modules, offline tools and bounded dispatch; no LLM construction."""

import json
from datetime import date

import pytest

from tradingagents.dataflows import (
    finite_disclosure_admission as a,
    finite_disclosure_cache as cache,
)

pytest_plugins = ["test_finite_disclosure_admission"]


def test_default_not_selected():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["finite_disclosure_profile"] is None
    assert not cache.enabled(DEFAULT_CONFIG)


@pytest.mark.parametrize(
    "method", ["get_fundamentals", "get_balance_sheet", "get_income_statement", "get_cashflow"]
)
def test_actual_interface_finite_path(installation, monkeypatch, method):
    config, _, _ = installation
    from tradingagents.dataflows import interface

    monkeypatch.setattr(interface, "get_config", lambda: config)
    result = json.loads(interface.route_to_vendor(method, "601328.SH", "2026-10-16"))
    assert result["new_physical_requests"] == 0
    assert result["full_window_coverage"] is False
    assert len(result["rows"]) == (60 if method == "get_fundamentals" else 20)


def test_default_interface_still_dispatches_legacy(monkeypatch):
    from tradingagents.dataflows import interface

    monkeypatch.setattr(interface, "get_config", lambda: {"finite_disclosure_profile": None})
    with pytest.raises((ValueError, KeyError)):
        interface.route_to_vendor("not_a_real_tool", "601328.SH", "2026-10-16")


def test_finite_readiness_does_not_require_complete_announcement_window(installation):
    from tradingagents.budget_readiness import _inspect_financial

    config, _, _ = installation
    issues, checks = [], []
    _inspect_financial(config, "601328.SH", date(2026, 10, 16), None, issues, checks)
    assert not issues and len(checks) == 6
    assert all(check.requires_provider_request is False for check in checks)


@pytest.mark.parametrize(
    "symbol,cutoff,freq",
    [
        ("600000.SH", "2026-10-16", "quarterly"),
        ("601328.SH", "2026-10-17", "quarterly"),
        ("601328.SH", "2026-10-16", "annual"),
    ],
)
def test_tool_scope_rejected(installation, symbol, cutoff, freq):
    from tradingagents.dataflows.finite_disclosure_tools import route_finite_tool

    config, _, _ = installation
    with pytest.raises(a.Rejected):
        route_finite_tool(config, "get_balance_sheet", ticker=symbol, curr_date=cutoff, freq=freq)


def test_budget_stop_propagates_before_read(installation, monkeypatch):
    from tradingagents import analysis_budget
    from tradingagents.dataflows.finite_disclosure_tools import route_finite_tool

    config, _, _ = installation

    class Stop(Exception):
        pass

    class StoppedBudget:
        def check(self):
            raise Stop

    monkeypatch.setattr(analysis_budget, "active_budget", lambda: StoppedBudget())
    with pytest.raises(Stop):
        route_finite_tool(config, "get_fundamentals", ticker="601328.SH", curr_date="2026-10-16")


def test_actual_dispatch_node_preserves_evidence_and_members(installation):
    from langchain_core.messages import AIMessage

    from tradingagents.evidence import EvidenceState, IdentityProvenance, InstrumentIdentityEvidence
    from tradingagents.graph.financial_tools import FinancialDispatchToolNode

    config, _, _ = installation
    identity = InstrumentIdentityEvidence(
        symbol="601328.SH",
        venue="XSHG",
        instrument_kind="equity",
        currency="CNY",
        provenance=IdentityProvenance(
            provider="test",
            source_ref="synthetic-identity",
            retrieved_at="2026-10-22T04:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )
    evidence = EvidenceState(instrument_identity=identity).model_dump(mode="json")
    state = {
        "company_of_interest": "601328.SH",
        "trade_date": "2026-10-16",
        "evidence_state": evidence,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_fundamentals",
                        "args": {"ticker": "601328.SH", "curr_date": "2026-10-16"},
                        "id": "finite-test",
                    }
                ],
            )
        ],
    }
    node = FinancialDispatchToolNode(config=config)
    result = node(state)
    assert result["evidence_state"] == evidence
    payload = json.loads(result["messages"][0].content)
    assert sum(len(g["record_indexes_1based"]) for g in payload["groups"]) == 9
    assert all(g["selected_member"] is None for g in payload["groups"])


def test_finite_claim_reaches_existing_trusted_evidence(installation):
    from langchain_core.messages import AIMessage

    from tradingagents.dataflows.finite_disclosure_output import report_lines
    from tradingagents.dataflows.finite_disclosure_tools import dispatch_finite_node
    from tradingagents.evidence import (
        EvidenceState,
        IdentityProvenance,
        InstrumentIdentityEvidence,
        MaterialClaim,
        build_tool_evidence_state,
    )

    config, view, _ = installation
    identity = InstrumentIdentityEvidence(
        symbol="601328.SH",
        venue="XSHG",
        instrument_kind="equity",
        currency="CNY",
        provenance=IdentityProvenance(
            provider="test",
            source_ref="synthetic-identity",
            retrieved_at="2026-10-22T04:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )
    call = {
        "name": "get_fundamentals",
        "args": {"ticker": "601328.SH", "curr_date": "2026-10-16"},
        "id": "finite-evidence-test",
    }
    state = {
        "company_of_interest": "601328.SH",
        "trade_date": "2026-10-16",
        "evidence_state": EvidenceState(instrument_identity=identity).model_dump(mode="json"),
        "messages": [AIMessage(content="", tool_calls=[call])],
    }
    messages = dispatch_finite_node(state, config)["messages"]
    ref = messages[0].artifact["source_ref"]
    line = report_lines(view)[0]
    claim = MaterialClaim(
        claim_id="fundamentals.finite_test",
        analyst="fundamentals",
        statement=line,
        source_refs=(ref,),
        source_quote=line,
    )
    evidence = build_tool_evidence_state(
        messages,
        [claim],
        tool_call_ids_by_source={ref: [call["id"]]},
        tool_calls_by_id={call["id"]: call},
    )
    assert evidence.claim_validations[0].status.value == "supported", evidence.claim_validations[
        0
    ].detail
    assert len(evidence.finite_cache_reads) == 1
    assert evidence.acquisition_outcomes == ()
    assert evidence.physical_attempt_count == 0


@pytest.mark.parametrize("finite", [True, False])
def test_market_readiness_selects_explicit_finite_manifest(installation, monkeypatch, finite):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from tradingagents import budget_readiness as readiness
    from tradingagents.market_history import current_cache

    config, _, _ = installation
    config = dict(config)
    if not finite:
        config["finite_disclosure_profile"] = None
    config.update(market_history_mode="current", data_usage_mode="personal_research")
    calls = []

    def diagnostic(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(current_cache, "compatibility_diagnostic", diagnostic)
    monkeypatch.setattr(
        current_cache,
        "read_prepared_sse_calendar",
        lambda *args: SimpleNamespace(observed_at=datetime(2026, 10, 21, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(readiness, "_readonly_history", lambda config: nullcontext(object()))
    monkeypatch.setattr(readiness, "_market_chain", lambda config: ())
    issues, checked = [], []
    readiness._inspect_market(
        config, "601328.SH", date(2026, 10, 1), date(2026, 10, 16), issues, checked
    )
    assert not issues
    expected = (
        {
            "manifest_path": cache.PROJECT_ROOT
            / "tradingagents/market_history/current_cache_compatibility_finite_v2.json"
        }
        if finite
        else {}
    )
    assert calls == [expected]


def test_finite_preflight_reports_four_zero_request_cache_routes(installation):
    from tradingagents.budget_preflight import inspect_budget_routes
    from tradingagents.dataflows.finite_disclosure_tools import FINITE_TOOLS
    from tradingagents.default_config import DEFAULT_CONFIG

    config, _, _ = installation
    config = {**DEFAULT_CONFIG, **config}
    report = inspect_budget_routes(config, symbol="601328.SH")
    financial = [(name, chain) for name, chain in report.chains if name.startswith("fundamentals.")]
    assert {name.split(".")[1] for name, _ in financial} == set(FINITE_TOOLS)
    assert all(chain == ("finite_disclosure_cache",) for _, chain in financial)
    assert not [x for x in report.issues if x.location.startswith("fundamentals.")]
    assert "finite_selected_periods_pinned_cache_original_ttl_new_requests_0" in report.dependencies
    assert not any("validated_current_cache_windows" in x for x in report.dependencies)
    assert any(x.location == "market_snapshot" for x in report.issues)
    assert any(x.location.startswith("news.") for x in report.issues)
    legacy = dict(config, finite_disclosure_profile=None)
    old = inspect_budget_routes(legacy, symbol="601328.SH")
    assert [x for x in old.issues if not x.location.startswith("fundamentals.")] == [
        x for x in report.issues if not x.location.startswith("fundamentals.")
    ]


@pytest.mark.parametrize("change", ["profile", "budget", "symbol", "cache_only", "statement_route"])
def test_finite_preflight_rejects_bad_configuration(installation, change):
    from tradingagents.budget_preflight import inspect_budget_routes

    config, _, _ = installation
    config = dict(config)
    symbol = "601328.SH"
    if change == "profile":
        config["finite_disclosure_profile"] = "unreviewed"
    if change == "budget":
        config["analysis_max_calls"] = 31
    if change == "symbol":
        symbol = "600000.SH"
    if change == "cache_only":
        config["financial_cache_only"] = False
    if change == "statement_route":
        config["tool_vendors"] = {"get_balance_sheet": "akshare"}
    report = inspect_budget_routes(config, symbol=symbol)
    assert any(x.reason == "invalid_finite_configuration_or_binding" for x in report.issues)
    assert not any(chain == ("finite_disclosure_cache",) for _, chain in report.chains)


def test_finite_preflight_uses_resolved_financial_configuration(installation):
    from tradingagents.budget_preflight import inspect_budget_routes

    config, _, _ = installation
    outer = dict(config, finite_disclosure_profile=None)
    resolved = inspect_budget_routes(outer, financial_config=config, symbol="601328.SH")
    assert sum(chain == ("finite_disclosure_cache",) for _, chain in resolved.chains) == 4
    ordinary = inspect_budget_routes(config, financial_config=outer, symbol="601328.SH")
    assert not any(chain == ("finite_disclosure_cache",) for _, chain in ordinary.chains)
