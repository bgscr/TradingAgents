"""Finite financial tools. Every call rechecks pinned files and original TTL."""

import json

from . import finite_disclosure_admission as a
from .finite_disclosure_cache import load_view

TABLES = {
    "get_balance_sheet": "balancesheet",
    "get_income_statement": "income",
    "get_cashflow": "cashflow",
}
FINITE_TOOLS = (*TABLES, "get_fundamentals")


def route_finite_tool(config, method, ticker=None, curr_date=None, freq="quarterly", **extra):
    if method not in FINITE_TOOLS or extra or freq != "quarterly":
        raise a.Rejected("finite_tool_arguments_out_of_scope")
    from tradingagents.analysis_budget import active_budget

    budget = active_budget()
    if budget is not None:
        budget.check()
    _, _, view = load_view(config, ticker, curr_date)
    if budget is not None:
        budget.check()
    return tool_payload(view, method)


def tool_payload(view, method):
    from .finite_disclosure_output import render_report

    tables = list(a.PERIODS) if method == "get_fundamentals" else [TABLES[method]]
    # One occurrence of each canonical statement makes exact source spans unambiguous.
    return json.dumps(
        {
            "contract": "finite-disclosure-tool-result-v2",
            "scope": a.SCOPE,
            "requested_tables": tables,
            "new_physical_requests": 0,
            "rows": [r for r in view["rows"] if r["table"] in tables],
            "groups": [g for g in view["groups"] if g["table"] in tables],
            "approved_report_markdown": render_report(view),
            "full_window_coverage": False,
        },
        sort_keys=True,
    )


def cache_envelope(config, tool_name, tool_call_id, content):
    from datetime import datetime, timezone

    from .finite_disclosure_contracts import FiniteDisclosureCacheEvidence

    profile, _, view = load_view(config, "601328.SH", "2026-10-16")
    source_ref = "finite-cache:v2:" + profile["admission_config"]["view_sha256"]
    observations = [
        {
            "table": table,
            "source_sha256": a.PINS[table],
            "observed_at": source["time"]["original_observed_at"],
            "expires_at": source["time"]["expires_at"],
        }
        for table, source in view["sources"].items()
    ]
    artifact = {
        "artifact_sha256": a.digest(content.encode()),
        "source_ref": source_ref,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "raw_text": content,
    }
    envelope = FiniteDisclosureCacheEvidence(
        release_sha256=profile["release_sha256"],
        view_sha256=profile["admission_config"]["view_sha256"],
        scope_sha256=a.digest(a.encode(a.SCOPE)),
        source_ref=source_ref,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        read_at=datetime.now(timezone.utc),
        historical_observations=observations,
        selected_artifact=artifact,
    )
    from tradingagents.analysis_budget import active_budget
    from tradingagents.dataflows.market_snapshot import get_active_run_telemetry

    budget = active_budget()
    if budget is not None:
        budget.record_finite_cache_read(envelope)
    telemetry = get_active_run_telemetry()
    if telemetry is not None:
        telemetry.record_finite_cache_read(envelope)
    return envelope.model_dump(mode="json")


def dispatch_finite_node(state, config):
    from langchain_core.messages import ToolMessage

    from tradingagents.evidence import EvidenceState

    evidence = EvidenceState.model_validate(state.get("evidence_state") or {})
    identity = evidence.instrument_identity
    if (
        identity is None
        or not identity.is_authoritative
        or identity.symbol not in ("601328.SH", "601328.SS")
        or identity.currency != "CNY"
        or str(getattr(identity.instrument_kind, "value", identity.instrument_kind)) != "equity"
    ):
        raise a.Rejected("finite_authoritative_identity_required")
    messages = state.get("messages") or []
    calls = getattr(messages[-1], "tool_calls", None) if messages else None
    if not calls:
        raise a.Rejected("finite_tool_calls_missing")
    results = []
    for call in calls:
        args = call.get("args")
        if (
            not isinstance(args, dict)
            or call.get("name") not in FINITE_TOOLS
            or args.get("ticker") != state.get("company_of_interest")
            or args.get("curr_date") != state.get("trade_date")
        ):
            raise a.Rejected("finite_tool_state_binding")
        content = route_finite_tool(config, call["name"], **args)
        results.append(
            ToolMessage(
                content=content,
                name=call["name"],
                tool_call_id=call["id"],
                artifact=cache_envelope(config, call["name"], call["id"], content),
            )
        )
    return {"messages": results, "evidence_state": evidence.model_dump(mode="json")}
