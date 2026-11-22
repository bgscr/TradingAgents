"""Synthetic cross-feature seams; fake model and in-memory report storage only."""

import copy
import json
from datetime import date, timedelta
from hashlib import sha256
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tests.test_multiyear_trend import POINTS, SYMBOL, _replace_strings, synthetic_evidence
from tests.test_reporting import HORIZON, _decision_state
from tests.test_supplemental_presentation import MemoryStore, export, payload, request
from tradingagents import reporting
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.dataflows import config
from tradingagents.evidence import (
    AnalystEvidenceReport,
    EvidenceSource,
    EvidenceState,
    SourceAcquisitionUnavailable,
)
from tradingagents.graph.evidence_gate import create_preflight_gate_node, route_after_preflight
from tradingagents.graph.propagation import Propagator
from tradingagents.strategy_registry import create_production_decision_policy
from tradingagents.terminal_contract import apply_terminal_contract

ASOF = "2026-07-18"


def combined_state():
    value = _replace_strings(_decision_state())
    existing = EvidenceState.model_validate(value["evidence_state"])
    # Enough rows for the existing 20-session rule; this intentionally sparse
    # synthetic path does not claim complete trading-calendar coverage.
    tail = [((date(2026, 6, 28) + timedelta(days=i)).isoformat(), "200") for i in range(20)]
    prices = synthetic_evidence([*POINTS[:-1], *tail], as_of=ASOF)
    timeout = SourceAcquisitionUnavailable(
        provider="akshare", capability="get_news", source_ref="synthetic:news-timeout",
        attempt=1, retrieved_at="2026-07-18T00:00:00Z", retryable=False,
        reason="provider_error", error_code="provider_timeout",
    )
    ev = EvidenceState.model_validate({
        **existing.model_dump(mode="python"),
        "instrument_identity": prices.instrument_identity,
        "market_snapshot": prices.market_snapshot,
        "source_artifacts": (*existing.source_artifacts, *prices.source_artifacts),
        "acquisition_outcomes": (*existing.acquisition_outcomes, *prices.acquisition_outcomes, timeout),
        "sources": (*existing.sources, EvidenceSource(
            source_id=timeout.source_ref, status="unavailable", required=False,
            detail="synthetic optional news unavailable",
        )),
    })
    value["evidence_state"] = ev.model_dump(mode="json")
    return value, ev


@pytest.mark.parametrize("language", ["Chinese", "English"])
@pytest.mark.parametrize("supplemental", [False, True], ids=["basic", "explicit-appendix"])
def test_initial_context_to_finalizer_and_export_keeps_trend_and_degraded_data_separate(
    monkeypatch, language, supplemental,
):
    value, ev = combined_state()
    evidence_before = ev.model_dump(mode="json")
    monkeypatch.setitem(config._config, "output_language", language)
    # Exercise the shared original entry's initializer; do not launch/acquire providers.
    initial = Propagator().create_initial_state(
        SYMBOL, ASOF, evidence_state=ev, instrument_context="Synthetic bound equity",
    )
    assert initial["evidence_state"] == evidence_before
    initial.update(create_preflight_gate_node(create_production_decision_policy(), HORIZON)(initial))
    assert route_after_preflight(initial) == "admitted", initial["evidence_preflight"]
    seen = []

    def draft(prompt):
        seen.append(prompt.to_string())
        return AIMessage(content="Synthetic draft requiring the real structured finalizer")

    commentary = "合成市场正文；新闻来源不可用。" if language == "Chinese" else "Synthetic market; news unavailable."
    structured = MagicMock()
    structured.invoke.return_value = AnalystEvidenceReport(
        report_markdown=commentary, material_claims=[], evidence_notes=[],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(draft)
    llm.with_structured_output.return_value = structured
    update = create_market_analyst(llm)(initial)
    assert len(seen) == structured.invoke.call_count == 1
    title = "多年价格趋势" if language == "Chinese" else "Multi-year price trend"
    for text in [seen[0], structured.invoke.call_args.args[0]]:
        assert title in text and "100.00%" in text
        assert ev.market_snapshot.snapshot_id in text
        assert "retrospective_backfill" in text
    if language == "Chinese":
        assert "Write your entire response in Chinese." in structured.invoke.call_args.args[0]
    updated = EvidenceState.model_validate(update["evidence_state"])
    assert updated.source_facts == ev.source_facts
    assert updated.material_claims == ev.material_claims
    assert updated.acquisition_outcomes == ev.acquisition_outcomes
    assert updated.market_snapshot == ev.market_snapshot
    assert updated.instrument_identity == ev.instrument_identity
    value.update(evidence_state=updated.model_dump(mode="json"), market_report=update["market_report"])
    # Existing optional-source degradation receipt; no budget or request is started.
    value["analysis_budget"] = {"data_gaps": [{
        "tool": "get_news", "provider": "akshare", "reason": "news_unavailable",
    }]}
    retained = payload()
    retained["rows"][0]["ts_code"] = SYMBOL.replace(".SS", ".SH")
    req = request(retained) if supplemental else None
    # The original graph finalizes this contract before save_reports/appendix validation.
    apply_terminal_contract(value, audit_digest="b" * 64)
    value["decision_audit_created_at"] = "2026-07-18T00:00:00Z"
    before = copy.deepcopy(value)
    audit_inputs = []

    def audit(state, *args, **kwargs):
        audit_inputs.append(copy.deepcopy(state))
        apply_terminal_contract(state, audit_digest="b" * 64)
        state["decision_audit_created_at"] = "2026-07-18T00:00:00Z"

    MemoryStore.opened = []
    monkeypatch.setattr(reporting, "ReportStore", MemoryStore)
    monkeypatch.setattr(reporting, "write_immutable_decision_audit", audit)
    documents = export(copy.deepcopy(value), req, language)
    complete = documents["complete_report.md"].decode()
    trend = documents["1_analysts/multiyear_trend.md"].decode()
    assert trend in complete and title in trend and commentary in complete
    assert "news_unavailable" in complete and "DATA_UNAVAILABLE: `get_news`" in complete
    assert "news_page_no_eligible_articles" not in complete
    for token in [ev.market_snapshot.snapshot_id, ev.market_snapshot.frame_sha256,
                  "2021-11-15", "2026-07-17", "retrospective_backfill"]:
        assert token in trend
    assert ("辅助" if language == "Chinese" else "advisory") in trend
    assert ("未取得可独立复核的日历覆盖证明" if language == "Chinese" else "session completeness is unverified") in trend
    assert ("非红利再投资总回报" if language == "Chinese" else "not dividend-reinvested total return") in trend
    if supplemental:
        assert "cost_fin_assets" in complete and "fair_value_fin_assets" in complete
        assert ("缺失（原响应为 null）" if language == "Chinese" else "Missing (response null)") in complete
        assert ("缺失（原响应无该键）" if language == "Chinese" else "Missing (key absent)") in complete
        assert ("未知" if language == "Chinese" else "Unknown") in complete
        assert req.expected_sha256 in complete
    else:
        assert "cost_fin_assets" not in complete and "fair_value_fin_assets" not in complete
    manifest = json.loads(documents["manifest.json"])
    assert manifest["output_language"] == language
    for filename, digest in manifest["files"].items():
        assert sha256(documents[filename]).hexdigest() == digest
    assert "1_analysts/multiyear_trend.md" in manifest["files"]
    monkeypatch.setattr(reporting, "multiyear_trend_markdown", lambda *args, **kwargs: "")
    baseline = export(copy.deepcopy(value), req, language)
    assert documents["5_portfolio/decision.md"] == baseline["5_portfolio/decision.md"]
    assert audit_inputs[0] == audit_inputs[1] == before
    assert value == before and ev.model_dump(mode="json") == evidence_before
    assert title not in documents["5_portfolio/decision.md"].decode()
    for filename in baseline:
        if filename not in {"manifest.json", "complete_report.md"}:
            assert documents[filename] == baseline[filename]


@pytest.mark.parametrize("invalid", ["missing", "wrong-stock"])
def test_invalid_baseline_stops_before_analysts_and_renders_no_trend_numbers(monkeypatch, invalid):
    ev = synthetic_evidence()
    if invalid == "missing":
        ev = ev.model_copy(update={"market_snapshot": None})
    else:
        ev = ev.model_copy(update={"market_snapshot": ev.market_snapshot.model_copy(
            update={"symbol": "600000.SS"},
        )})
    state = Propagator().create_initial_state(SYMBOL, "2026-11-13", evidence_state=ev)
    update = create_preflight_gate_node(create_production_decision_policy(), HORIZON)(state)
    state.update(update)
    assert route_after_preflight(state) == "blocked"
    expected = "snapshot_unavailable" if invalid == "missing" else "snapshot_invalid"
    assert expected in state["evidence_preflight"]["diagnostic_codes"]
    assert state["analysis_outcome_contract"]["reason"] == "preflight_blocked"
    seen = []

    def audit(value, *args, **kwargs):
        seen.append(copy.deepcopy(value))
        apply_terminal_contract(value, audit_digest="b" * 64)
        value["decision_audit_created_at"] = "2026-11-14T00:00:00Z"

    MemoryStore.opened = []
    monkeypatch.setattr(reporting, "ReportStore", MemoryStore)
    monkeypatch.setattr(reporting, "write_immutable_decision_audit", audit)
    documents = export(state, None, "Chinese")
    assert "5_portfolio/decision.md" not in documents
    assert "5_portfolio/analysis_outcome.md" in documents
    trend = documents["1_analysts/multiyear_trend.md"].decode()
    assert "不可用" in trend and "%" not in trend
    assert trend in documents["complete_report.md"].decode()
    assert len(seen) == 1
