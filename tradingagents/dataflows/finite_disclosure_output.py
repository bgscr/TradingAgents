"""Closed finite disclosure output, not keyword filtering of arbitrary model prose."""

from . import finite_disclosure_admission as a
from .finite_disclosure_cache import enabled, load_view


def report_lines(view):
    lines = []
    for row in view["rows"]:
        lines.append(
            f"{row['table']}/{row['period']}/{row['field']}: "
            f"API amount={row['raw_api_value']}; disclosure contribution="
            f"{row['derived_disclosure_contribution']}; PDF million={row['pdf_million']}; "
            f"page={row['pdf_page']}; meaning={row['field_meaning']}."
        )
    by_key = {(r["table"], r["period"], r["field"]): r for r in view["rows"] if not r["missing"]}
    for table, periods in a.PERIODS.items():
        for field in a.CORE[table]:
            pair = [by_key.get((table, p, field)) for p in periods]
            if not all(pair):
                continue
            latest, comparison = [a.amount(r["derived_disclosure_contribution"]) for r in pair]
            percent = (
                str((latest - comparison) / comparison * 100)
                if comparison > 0
                else "not_applicable_nonpositive_base"
            )
            kind = "year_end_comparison" if table == "balancesheet" else "midyear_yoy"
            lines.append(
                f"{table}/{field}/{kind}: disclosure contribution difference={latest - comparison}; "
                f"percentage_change={percent}; display-scale arithmetic, not independently exact yuan."
            )
    return lines


def render_report(view):
    return (
        "# 601328.SH — limited 2026 midyear disclosure\n\n"
        "Group CNY; user-confirmed official-site original PDF, not independent identical-byte verification. "
        "2025 comparatives are from the 2026 report and may be reclassified. "
        "Amounts are display-consistent, not independently exact to yuan. "
        "All equivalent members remain; no latest-version or complete-window claim.\n\n"
        + "\n".join(report_lines(view))
        + "\n\n"
        + "Restrictions: "
        + ", ".join(a.LIMITS)
        + "."
    )


def finite_prompt(config):
    if not enabled(config):
        return ""
    return (
        "\nFINITE DISCLOSURE MODE: call get_fundamentals for the six selected filing groups. "
        "Copy approved_report_markdown exactly into the final report. Do not add free-form financial conclusions. "
        "Material claims, if submitted, must copy one complete approved data line exactly with its tool source quote. "
        "Expense/payment amounts and negative disclosure contributions are distinct; do not use abs. "
        "No latest, annual/single-quarter growth, TTM, valuation, bank-risk or historical PIT claims.\n"
    )


def validate_submission(result, config):
    if not enabled(config) or not result.report:
        return
    _, _, view = load_view(config, "601328.SH", "2026-10-16")
    if result.report != render_report(view):
        raise a.Rejected("finite_report_not_closed_projection")
    allowed = set(report_lines(view))
    for claim in result.claims:
        if claim.statement not in allowed or claim.source_quote != claim.statement:
            raise a.Rejected("finite_claim_out_of_scope")


ANALYST_REPORTS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}
FINITE_MARKERS = (
    "finite_disclosure_context",
    "finite_analyst_selections",
    "finite_advisory_trace",
    "finite_disclosure_limits",
)
FINITE_RUNTIME_NOTICE = (
    "Finite runtime event; content is published only through validated stage reports."
)
FINITE_REFLECTION = (
    "Finite disclosure reflection: the retained observations and advisory selections "
    "remain limited to their original disclosure scope. The finite evidence does "
    "not establish attribution of subsequent investment outcomes."
)


def publication_config(config=None):
    if config is None:
        from tradingagents.dataflows.config import get_config

        config = get_config()
    return config


def finite_output_enabled(config=None):
    return enabled(publication_config(config))


def _as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _reads(state):
    evidence = _as_dict(state.get("evidence_state") or {})
    if not isinstance(evidence, dict):
        raise a.Rejected("finite_evidence_shape")
    values = evidence.get("finite_cache_reads", ())
    if not isinstance(values, (tuple, list)):
        raise a.Rejected("finite_cache_read_shape")
    return values


def has_finite_material(state):
    return any(state.get(key) is not None for key in FINITE_MARKERS) or bool(_reads(state))


def validate_publication(state, config=None, *, terminal=False):
    """Revalidate every publishable stage before display, logging or persistence.

    The selected profile is required independently of checkpoint markers. An
    enabled profile with stripped markers therefore cannot enter a default path.
    """
    config = publication_config(config)
    if not enabled(config):
        if has_finite_material(state):
            raise a.Rejected("finite_publication_requires_selected_profile")
        return False
    from tradingagents.agents.finite_disclosure_advisory import validate_finite_inputs
    from tradingagents.dataflows.finite_disclosure_cache import PROFILE
    from tradingagents.finite_disclosure_runtime import (
        render_selection,
        require_analyst_report,
        require_context,
        validate_finite_envelope,
    )

    if config.get("finite_disclosure_profile") != PROFILE:
        raise a.Rejected("finite_publication_profile")
    context = require_context(state)
    selections = state.get("finite_analyst_selections", {})
    if not isinstance(selections, dict) or set(selections) - set(ANALYST_REPORTS):
        raise a.Rejected("finite_analyst_selections_shape")
    for role, selection in selections.items():
        render_selection(role, selection, state)
    for role, field in ANALYST_REPORTS.items():
        report = state.get(field)
        if report not in (None, ""):
            require_analyst_report(role, report, state)
    ids = set()
    for raw in _reads(state):
        envelope = validate_finite_envelope(_as_dict(raw))
        if (
            envelope.tool_call_id in ids
            or envelope.release_sha256 != state["finite_disclosure_context"]["release_sha256"]
        ):
            raise a.Rejected("finite_cache_read_duplicate_or_binding")
        ids.add(envelope.tool_call_id)
    raw_telemetry = state.get("run_telemetry")
    if raw_telemetry is not None:
        telemetry = _as_dict(raw_telemetry)
        telemetry_reads = telemetry.get("finite_cache_reads", ())
        expected_reads = {
            item["tool_call_id"]: item for item in (_as_dict(x) for x in _reads(state))
        }
        actual_reads = {}
        for raw in telemetry_reads:
            item = validate_finite_envelope(_as_dict(raw)).model_dump(mode="json")
            if item["tool_call_id"] in actual_reads:
                raise a.Rejected("finite_telemetry_duplicate")
            actual_reads[item["tool_call_id"]] = item
        expected_reads = {
            key: validate_finite_envelope(value).model_dump(mode="json")
            for key, value in expected_reads.items()
        }
        if actual_reads != expected_reads:
            raise a.Rejected("finite_telemetry_ledger_mismatch")
    blocked_preflight = (state.get("evidence_preflight") or {}).get("passed") is False
    if (state.get("fundamentals_report") or (terminal and not blocked_preflight)) and not ids:
        raise a.Rejected("finite_publication_cache_read_missing")
    # Financial claims may retain source observations, never new financial prose.
    evidence = _as_dict(state.get("evidence_state") or {})
    allowed = set(report_lines(context.view))
    for role in ANALYST_REPORTS:
        catalog = context.selection_catalog(role)
        allowed.update(catalog.get("evidence_text", {}).values())
    for raw in evidence.get("material_claims", ()):
        claim = _as_dict(raw)
        refs = claim.get("source_refs", ())
        financial = claim.get("analyst") == "fundamentals" or any(
            str(ref).startswith(
                (
                    "finite-cache:",
                    "get_fundamentals:",
                    "get_balance_sheet:",
                    "get_income_statement:",
                    "get_cashflow:",
                )
            )
            for ref in refs
        )
        if financial and (
            claim.get("statement") not in allowed
            or claim.get("source_quote") != claim.get("statement")
        ):
            raise a.Rejected("finite_publication_financial_claim")
        if claim.get("statement") not in allowed or claim.get("source_quote") != claim.get(
            "statement"
        ):
            raise a.Rejected("finite_publication_claim_not_closed")
    trace_state = dict(state)
    if state.get("final_trade_decision") or state.get("trading_decision") is not None:
        # This function does not call validate_publication, avoiding recursion.
        from tradingagents.terminal_contract import validated_trading_decision_from_state

        if validated_trading_decision_from_state(state) is None:
            raise a.Rejected("finite_terminal_decision_unbound")
        risk = state.get("risk_debate_state")
        if isinstance(risk, dict) and risk.get("judge_decision") == state.get(
            "final_trade_decision"
        ):
            trace_state["risk_debate_state"] = {
                **risk,
                "judge_decision": state.get("portfolio_advisory", ""),
                "latest_speaker": "Judge"
                if state.get("portfolio_advisory")
                else risk.get("latest_speaker", ""),
            }
    if state.get("analysis_outcome") or state.get("analysis_outcome_contract") is not None:
        from tradingagents.evidence import AnalysisOutcome, render_analysis_outcome

        outcome = AnalysisOutcome.model_validate(state.get("analysis_outcome_contract"))
        if state.get("analysis_outcome") != render_analysis_outcome(outcome):
            raise a.Rejected("finite_terminal_outcome_unbound")
    validate_finite_inputs(trace_state)
    return True


def validate_terminal_output(state, config=None):
    return validate_publication(state, config, terminal=True)


def validate_evidence_publication(evidence, state=None, config=None):
    """Require the state binding before a finite evidence artifact can be written."""
    payload = _as_dict(evidence)
    finite_reads = payload.get("finite_cache_reads", ())
    if (
        not finite_reads
        and not finite_output_enabled(config)
        and not (state and has_finite_material(state))
    ):
        return False
    if state is None:
        raise a.Rejected("finite_evidence_publication_requires_state")
    validate_publication(state, config)
    from tradingagents.evidence import EvidenceState

    expected = EvidenceState.model_validate(state.get("evidence_state") or {}).model_dump(
        mode="json"
    )
    actual = EvidenceState.model_validate(payload).model_dump(mode="json")
    if actual != expected:
        raise a.Rejected("finite_evidence_publication_mismatch")
    return True


def cache_read_projection(evidence, state=None, config=None):
    """Truthful cache reads, separately retained from physical acquisitions."""
    if not validate_evidence_publication(evidence, state, config):
        return []
    from tradingagents.finite_disclosure_runtime import validate_finite_envelope

    projected = []
    for raw in _as_dict(evidence).get("finite_cache_reads", ()):
        item = validate_finite_envelope(_as_dict(raw)).model_dump(mode="json")
        artifact = item.pop("selected_artifact")
        item["selected_artifact_ref"] = "artifact=sha256:" + artifact["artifact_sha256"]
        projected.append(item)
    return sorted(projected, key=lambda item: item["tool_call_id"])


def publication_projection(state, config=None):
    if not validate_publication(state, config):
        return None
    return {
        "contract": "finite-publication-v2",
        "finite_disclosure_context": dict(state["finite_disclosure_context"]),
        "finite_analyst_selections": state.get("finite_analyst_selections", {}),
        "finite_advisory_trace": state.get("finite_advisory_trace", []),
        "finite_cache_reads": cache_read_projection(
            state.get("evidence_state") or {}, state, config
        ),
    }


def validate_stage_text(section, text, state, config=None):
    if not validate_publication(state, config):
        return
    fields = {
        *ANALYST_REPORTS.values(),
        "investment_plan",
        "trader_investment_plan",
        "portfolio_advisory",
        "final_trade_decision",
        "analysis_outcome",
    }
    if section not in fields or not isinstance(text, str) or text != state.get(section):
        raise a.Rejected("finite_stage_text_unbound")


def render_limits(state, config=None):
    if not validate_publication(state, config):
        return ""
    return (
        "\n\n## Limited financial disclosure scope\n"
        "601328.SH: BS 20260630/20251231; income/cashflow 20260630/20250630. "
        "2026 filing comparative version; reclassification possible; original observation TTL retained. "
        "User-confirmed official PDF source, no independent identical-byte certification. "
        "Unknown transport metadata remains unresolved outside these corroborated fields.\n"
        + "Restrictions: "
        + ", ".join(a.LIMITS)
        + ".\n"
    )


def finite_export_documents(state, language="English", config=None):
    """Regenerate the complete allowed finite document set at the write boundary."""
    if not validate_publication(state, config, terminal=True):
        raise a.Rejected("finite_export_requires_selected_profile")
    from tradingagents.report_presentation import advisory_markdown, advisory_sections, is_chinese

    documents, stages = {}, []
    for path, title, chinese_title, content in advisory_sections(state, config=config):
        documents[path] = advisory_markdown(
            chinese_title if is_chinese(language) else title,
            content,
            language,
        )
        stages.append(path)
    terminal = state.get("final_trade_decision") or state.get("analysis_outcome")
    if not terminal:
        raise a.Rejected("finite_export_terminal_missing")
    terminal_path = (
        "5_portfolio/decision.md"
        if state.get("trading_decision") is not None
        else "5_portfolio/analysis_outcome.md"
    )
    # Every line comes from checked stage templates, checked typed terminal
    # rendering, or this fixed scope/accounting description.
    import json

    ledger = json.dumps(
        publication_projection(state, config), ensure_ascii=True, sort_keys=True, indent=2
    )
    documents[terminal_path] = terminal + render_limits(state, config)
    documents["finite_publication.json"] = ledger + "\n"
    documents["complete_report.md"] = (
        "# Finite disclosure analysis: 601328.SH / 2026-10-16\n\n"
        + documents[terminal_path]
        + "\n\n"
        + "\n\n".join(documents[path] for path in stages)
        + "\n\nFinite cache read provenance is retained in finite_publication.json. "
        "Cache reads make zero new physical requests.\n"
    )
    return documents, stages


def finite_reflection(final_decision, state, config=None):
    if state is None or not validate_publication(state, config, terminal=True):
        raise a.Rejected("finite_reflection_requires_state")
    if final_decision != state.get("final_trade_decision"):
        raise a.Rejected("finite_reflection_decision_unbound")
    return FINITE_REFLECTION


def replay_state_payload(state, config=None):
    """Closed replay/memory snapshot retains context, selections and the ledger."""
    validate_publication(state, config, terminal=True)
    keys = {
        "company_of_interest",
        "trade_date",
        "asset_type",
        "evidence_state",
        "finite_disclosure_context",
        "finite_analyst_selections",
        "finite_advisory_trace",
        "investment_debate_state",
        "risk_debate_state",
        "investment_plan",
        "trader_investment_plan",
        "portfolio_advisory",
        "final_trade_decision",
        "analysis_outcome",
        "analysis_outcome_contract",
        "trading_decision",
        "decision_gate",
        "decision_gate_v2",
        "validated_decision_context",
        "admitted_evidence_binding",
        "evidence_preflight",
        "admission_gate",
        "evidence_gate_mode",
        "terminal_contract",
        "run_identity",
        "run_id",
        "configuration_digest",
        "decision_audit_sha256",
        "decision_audit_created_at",
        *ANALYST_REPORTS.values(),
    }
    import json

    return json.loads(
        json.dumps(
            {key: _as_dict(state[key]) for key in keys if key in state},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
