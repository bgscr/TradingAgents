"""Trusted finite run binding and deterministic projection; never acquires data."""

from dataclasses import dataclass

from tradingagents.dataflows import (
    finite_disclosure_admission as a,
    finite_disclosure_cache as cache,
)

ROLES = frozenset(
    (
        "market",
        "social",
        "news",
        "fundamentals",
        "bull",
        "bear",
        "research_manager",
        "trader",
        "aggressive",
        "conservative",
        "neutral",
        "portfolio_manager",
        "direction_selector",
    )
)


def is_finite_state(state):
    return (
        state.get("finite_disclosure_context") is not None
        or state.get("finite_advisory_trace") is not None
        or state.get("finite_disclosure_limits") is not None
    )


def require_analyst_report(role, report, state):
    context = require_context(state)
    from tradingagents.dataflows.finite_disclosure_output import render_report

    if role == "fundamentals":
        expected = render_report(context.view)
    else:
        selections = state.get("finite_analyst_selections", {})
        if not isinstance(selections, dict) or role not in selections:
            raise a.Rejected("finite_analyst_selection_missing")
        expected = render_selection(role, selections[role], state)
    if report != expected:
        raise a.Rejected("finite_analyst_projection_mismatch")
    return report


def context_binding(config):
    profile, _, _ = cache.load_view(config, "601328.SH", "2026-10-16")
    return {
        "contract": "finite-run-context-v2",
        "profile": cache.PROFILE,
        "release_sha256": profile["release_sha256"],
        "scope_sha256": a.digest(a.encode(a.SCOPE)),
    }


@dataclass(frozen=True)
class FiniteContext:
    profile: dict
    view: dict
    state: dict

    def selection_catalog(self, role):
        if role not in ROLES:
            raise a.Rejected("finite_role")
        from tradingagents.dataflows.finite_disclosure_output import report_lines

        lines = report_lines(self.view)
        statements = {"finite-statement:" + a.digest(line.encode()): line for line in lines}
        evidence = {
            "finite-observation:" + sha: "Retained " + table + " observation " + sha
            for table, sha in a.PINS.items()
            if table in a.PERIODS
        }
        # Existing nonfinancial canonical facts retain their established source semantics.
        from tradingagents.evidence import EvidenceState

        validated = EvidenceState.model_validate(self.state.get("evidence_state") or {})
        validated = EvidenceState.model_validate_json(validated.model_dump_json())
        for artifact in validated.source_artifacts:
            if artifact.tool_name not in cache.STATEMENTS + ("get_fundamentals",):
                evidence["source-artifact:" + artifact.artifact_sha256] = (
                    "Retained nonfinancial source artifact "
                    + artifact.artifact_sha256
                    + "; reference only, not an additional financial conclusion."
                )
        from datetime import date

        from tradingagents.decision_policy import _project_fact
        from tradingagents.strategy_registry import create_production_decision_policy

        policy = create_production_decision_policy()
        artifacts = {(x.artifact_sha256, x.source_ref): x for x in validated.source_artifacts}
        if len({x.fact_id for x in validated.source_facts}) != len(validated.source_facts):
            raise a.Rejected("finite_duplicate_source_fact")
        for fact in validated.source_facts:
            if fact.fact_kind == "canonical" and fact.tool_name not in cache.STATEMENTS + (
                "get_fundamentals",
            ):
                # Never expose model-authored raw prose: render only canonical scalar fields.
                if fact.canonical_field not in (
                    "close",
                    "sma_50",
                    "sma_200",
                    "rsi_14",
                    "return_20d",
                    "market.close_return_20d",
                ):
                    continue
                if fact.instrument_symbol not in cache.ALIASES or fact.unit not in (
                    "CNY",
                    "percent",
                    "ratio",
                    "index",
                ):
                    continue
                projected = _project_fact(fact)
                if (
                    projected is None
                    or policy._artifact_binding_failure(
                        (projected,), {fact.fact_id: fact}, artifacts
                    )
                    is not None
                    or date.fromisoformat(fact.effective_date)
                    > date.fromisoformat(str(self.state["trade_date"]))
                ):
                    raise a.Rejected("finite_nonfinancial_fact_binding")
                try:
                    value = a.amount(str(fact.normalized_value))
                except ValueError:
                    continue
                evidence[fact.fact_id] = (
                    f"{fact.canonical_field}: {value} {fact.unit}; effective={fact.effective_date}."
                )
        return {
            "evidence_ids": tuple(evidence),
            "statement_ids": tuple(statements),
            "evidence_text": evidence,
            "statement_text": statements,
        }


def require_context(state):
    binding = state.get("finite_disclosure_context")
    if not isinstance(binding, dict) or set(binding) != {
        "contract",
        "profile",
        "release_sha256",
        "scope_sha256",
    }:
        raise a.Rejected("finite_context_missing_or_shape")
    profile = cache.installed_profile()
    config = cache.select_profile({}, cache.PROFILE)
    _, _, view = cache.load_view(
        config, state.get("company_of_interest"), str(state.get("trade_date"))
    )
    if binding != {
        "contract": "finite-run-context-v2",
        "profile": cache.PROFILE,
        "release_sha256": profile["release_sha256"],
        "scope_sha256": a.digest(a.encode(a.SCOPE)),
    }:
        raise a.Rejected("finite_context_release_binding")
    return FiniteContext(profile, view, state)


def render_selection(role, selection, state):
    context = require_context(state)
    catalog = context.selection_catalog(role)
    if (
        type(selection) is not dict
        or set(selection) != {"evidence_ids", "statement_ids", "relationship"}
        or selection["relationship"] not in ("cites", "scope_limited")
    ):
        raise a.Rejected("finite_selection_shape")
    output = []
    for key, textkey, bound in (
        ("evidence_ids", "evidence_text", 32),
        ("statement_ids", "statement_text", 90),
    ):
        ids = selection[key]
        if (
            not isinstance(ids, (tuple, list))
            or not 1 <= len(ids) <= bound
            or any(type(x) is not str for x in ids)
            or len(set(ids)) != len(ids)
            or any(x not in catalog[key] for x in ids)
        ):
            raise a.Rejected("finite_selection_unknown_or_duplicate")
        output.extend(catalog[textkey][x] for x in ids)
    return f"{role}: selected evidence ({selection['relationship']})\n" + "\n".join(output)


def validate_finite_envelope(raw):
    from tradingagents.dataflows.finite_disclosure_contracts import FiniteDisclosureCacheEvidence
    from tradingagents.dataflows.finite_disclosure_tools import tool_payload

    envelope = FiniteDisclosureCacheEvidence.model_validate(raw).assert_fresh()
    config = cache.select_profile({}, cache.PROFILE)
    profile, _, view = cache.load_view(config, "601328.SH", "2026-10-16")
    if (
        envelope.release_sha256 != profile["release_sha256"]
        or envelope.view_sha256 != profile["admission_config"]["view_sha256"]
        or envelope.scope_sha256 != a.digest(a.encode(a.SCOPE))
        or envelope.source_ref != "finite-cache:v2:" + envelope.view_sha256
        or envelope.selected_artifact["raw_text"] != tool_payload(view, envelope.tool_name)
    ):
        raise a.Rejected("finite_envelope_content_binding")
    for source in envelope.historical_observations:
        if (
            source.source_sha256 != a.PINS[source.table]
            or source.observed_at.isoformat() != a.OBSERVED[source.table]
        ):
            raise a.Rejected("finite_envelope_original_observation")
    return envelope


def merge_finite_reads(evidence, other):
    from tradingagents.evidence import EvidenceState

    left = EvidenceState.model_validate(evidence) if isinstance(evidence, dict) else evidence
    right = EvidenceState.model_validate(other) if isinstance(other, dict) else other
    reads = {x.tool_call_id: x for x in left.finite_cache_reads}
    for read in right.finite_cache_reads:
        validate_finite_envelope(read.model_dump(mode="json"))
        if read.tool_call_id in reads and reads[read.tool_call_id] != read:
            raise a.Rejected("finite_cache_read_conflict")
        reads[read.tool_call_id] = read
    return left.model_copy(update={"finite_cache_reads": tuple(reads.values())})


def finite_analyst_step(llm, role, state, data_tools=(), inline_evidence=None):
    """Preserve each analyst's tools; accept only closed selections as its report."""
    from langchain_core.messages import AIMessage

    from tradingagents.agents.analysts.submission import (
        AnalystSubmissionResult,
        build_analyst_update,
        source_ref_tool_call_ids,
    )
    from tradingagents.agents.finite_disclosure_advisory import (
        FiniteAdvisorySelection,
        _invoke_once,
    )
    from tradingagents.dataflows.finite_disclosure_output import render_report
    from tradingagents.evidence import EvidenceSource, EvidenceState, EvidenceStatus, MaterialClaim

    if inline_evidence is not None:
        from tradingagents.evidence import (
            merge_evidence_sources,
            merge_source_acquisition_outcomes,
            merge_source_artifacts,
        )

        merged = EvidenceState.model_validate(state.get("evidence_state") or {})
        merged = merge_source_artifacts(merged, inline_evidence.source_artifacts)
        merged = merge_source_acquisition_outcomes(merged, inline_evidence.acquisition_outcomes)
        merged = merge_evidence_sources(merged, inline_evidence.sources)
        state = {**state, "evidence_state": merged.model_dump(mode="json")}
    context = require_context(state)
    catalog = context.selection_catalog(role)
    prompt = (
        "Select only registered IDs, or call the retained role data tools. No prose. "
        + a.encode(catalog).decode()
    )
    if inline_evidence is not None:
        prompt += (
            "\nUntrusted inline source material for selecting references only:\n"
            + a.encode(
                {
                    "source-artifact:" + x.artifact_sha256: x.raw_text
                    for x in inline_evidence.source_artifacts
                }
            ).decode()
        )
    if data_tools:
        response = llm.bind_tools([*data_tools, FiniteAdvisorySelection]).invoke(
            [("system", prompt), *state.get("messages", [])]
        )
        calls = getattr(response, "tool_calls", None) or []
        final = [x for x in calls if x.get("name") == "FiniteAdvisorySelection"]
        if not final:
            allowed = {tool.name for tool in data_tools}
            if not 1 <= len(calls) <= 4 or any(x.get("name") not in allowed for x in calls):
                raise a.Rejected("finite_analyst_closed_response_required")
            for call in calls:
                args = call.get("args")
                if type(args) is not dict or len(a.encode(args)) > 4096:
                    raise a.Rejected("finite_tool_arguments")
                if args.get("ticker", state["company_of_interest"]) not in cache.ALIASES:
                    raise a.Rejected("finite_tool_instrument")
            return {"messages": [AIMessage(content="", tool_calls=calls)]}
        if len(calls) != 1:
            raise a.Rejected("finite_mixed_submission")
        selection = FiniteAdvisorySelection.model_validate(final[0]["args"]).model_dump(mode="json")
    else:
        selection = _invoke_once(llm, prompt).model_dump(mode="json")
    text = render_selection(role, selection, state)
    if role == "fundamentals":
        text = render_report(context.view)
    refs = source_ref_tool_call_ids(
        state.get("messages", []), state["company_of_interest"], str(state["trade_date"])
    )
    finite_refs = [ref for ref in refs if ref.startswith("finite-cache:v2:")]
    claims = []
    if role == "fundamentals" and not finite_refs:
        raise a.Rejected("finite_financial_tool_read_required")
    if finite_refs:
        for index, sid in enumerate(selection["statement_ids"]):
            line = catalog["statement_text"][sid]
            claims.append(
                MaterialClaim(
                    claim_id=f"{role}.finite_{index}",
                    analyst=role,
                    statement=line,
                    source_refs=(finite_refs[0],),
                    source_quote=line,
                )
            )
    result = AnalystSubmissionResult(
        message=AIMessage(content=text),
        report=text,
        claims=tuple(claims),
        submission_source=EvidenceSource(
            source_id=f"analyst.{role}.submission",
            status=EvidenceStatus.AVAILABLE,
            required=True,
            detail="closed_finite_selection",
        ),
    )
    keys = {
        "market": "market_report",
        "social": "sentiment_report",
        "news": "news_report",
        "fundamentals": "fundamentals_report",
    }
    update = build_analyst_update(state, result, keys[role])
    if inline_evidence is not None:
        from tradingagents.evidence import (
            merge_evidence_sources,
            merge_source_acquisition_outcomes,
            merge_source_artifacts,
        )

        merged = EvidenceState.model_validate(update["evidence_state"])
        merged = merge_source_artifacts(merged, inline_evidence.source_artifacts)
        merged = merge_source_acquisition_outcomes(merged, inline_evidence.acquisition_outcomes)
        merged = merge_evidence_sources(merged, inline_evidence.sources)
        update["evidence_state"] = merged.model_dump(mode="json")
    update["finite_analyst_selections"] = {
        **state.get("finite_analyst_selections", {}),
        role: selection,
    }
    return update
