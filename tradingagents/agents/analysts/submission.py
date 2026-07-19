"""Fail-closed completion handling shared by tool-calling analysts."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tradingagents.dataflows.market_snapshot import (
    _minimum_history_for_indicator,
    get_active_authoritative_market_snapshot,
)
from tradingagents.evidence import (
    AnalystEvidenceReport,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MarketSnapshotEvidence,
    MaterialClaim,
    build_tool_evidence_state,
    merge_claim_validations,
    merge_evidence_sources,
    merge_material_claims,
    merge_source_artifacts,
    merge_source_facts,
)

logger = logging.getLogger(__name__)

_REPORT_TOOL_NAME = AnalystEvidenceReport.__name__
_SOURCE_PREVIEW_LIMIT = 4_000


@dataclass(frozen=True)
class AnalystSubmissionResult:
    message: AIMessage
    report: str
    claims: tuple[MaterialClaim, ...] = ()
    submission_source: EvidenceSource | None = None


def bind_analyst_finalizer(llm: Any, analyst: str) -> Any | None:
    """Bind the one-shot structured finalizer when the provider supports it."""
    try:
        return llm.with_structured_output(AnalystEvidenceReport)
    except (NotImplementedError, AttributeError, ValueError, TypeError):
        logger.warning(
            "%s Analyst: structured finalization is unsupported by the provider",
            analyst.title(),
        )
        return None


def _submission_source(
    analyst: str,
    status: EvidenceStatus,
    detail: str,
) -> EvidenceSource:
    return EvidenceSource(
        source_id=f"analyst.{analyst}.submission",
        status=status,
        required=True,
        detail=detail,
    )


def _tool_calls_by_id(messages: list[Any]) -> dict[str, Mapping[str, Any]]:
    calls = {}
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None and isinstance(message, Mapping):
            tool_calls = message.get("tool_calls")
        for call in tool_calls or ():
            call_id = call.get("id")
            if call_id:
                calls[str(call_id)] = call
    return calls


def _source_ref(
    message: Any,
    ticker: str,
    trade_date: str,
    tool_calls_by_id: Mapping[str, Mapping[str, Any]],
) -> str | None:
    tool_name = getattr(message, "name", None)
    tool_call_id = getattr(message, "tool_call_id", None)
    if not tool_name or not tool_call_id:
        return None
    tool_call = tool_calls_by_id.get(str(tool_call_id))
    if tool_call is None or tool_call.get("name") != tool_name:
        return None
    args = tool_call.get("args") or {}
    if not isinstance(args, Mapping):
        return None
    called_ticker = args.get("ticker", args.get("symbol"))
    if called_ticker is not None and str(called_ticker).strip().upper() != str(
        ticker
    ).strip().upper():
        return None
    called_date = args.get("curr_date", args.get("end_date"))
    if called_date is not None and str(called_date) != str(trade_date):
        return None
    if tool_name in {"get_verified_market_snapshot", "get_indicators"}:
        snapshot_match = re.search(
            r"(?mi)^#?\s*Snapshot ID:\s*(snapshot:[0-9a-f]{64})\s*$",
            str(getattr(message, "content", "")),
        )
        if snapshot_match:
            return snapshot_match.group(1)
    if tool_name == "get_verified_market_snapshot":
        return f"snapshot:{ticker}:{trade_date}"
    return f"{tool_name}:{ticker}:{trade_date}"


def allowed_source_refs(
    messages: list[Any],
    ticker: str,
    trade_date: str,
) -> tuple[str, ...]:
    """Return the exact source-ref vocabulary represented by tool messages."""
    return tuple(source_ref_tool_call_ids(messages, ticker, trade_date))


def source_ref_tool_call_ids(
    messages: list[Any],
    ticker: str,
    trade_date: str,
) -> dict[str, tuple[str, ...]]:
    """Map each exact source ref to the paired tool-call IDs that produced it."""
    refs: dict[str, list[str]] = {}
    tool_calls_by_id = _tool_calls_by_id(messages)
    for message in messages:
        source_ref = _source_ref(
            message,
            ticker,
            trade_date,
            tool_calls_by_id,
        )
        tool_call_id = getattr(message, "tool_call_id", None)
        if source_ref is None or not tool_call_id:
            continue
        call_ids = refs.setdefault(source_ref, [])
        normalized_call_id = str(tool_call_id)
        if normalized_call_id not in call_ids:
            call_ids.append(normalized_call_id)
    return {source_ref: tuple(call_ids) for source_ref, call_ids in refs.items()}


def render_allowed_source_ref_catalog(
    messages: list[Any],
    ticker: str,
    trade_date: str,
) -> str:
    refs = allowed_source_refs(messages, ticker, trade_date)
    if not refs:
        return (
            "Allowed source_refs for this turn: none. Call a data tool before "
            "submitting any material claim."
        )
    return (
        "Allowed source_refs for this turn (copy exactly):\n"
        + "\n".join(f"- {source_ref}" for source_ref in refs)
        + "\nEach MaterialClaim.source_quote must copy one exact contiguous "
        "source-language phrase from its cited tool output. The localized "
        "statement may paraphrase that quote."
    )


def _finalization_prompt(
    *,
    analyst: str,
    ticker: str,
    trade_date: str,
    draft: str,
    messages: list[Any],
) -> str:
    catalog = []
    seen_artifacts = set()
    tool_calls_by_id = _tool_calls_by_id(messages)
    for message in messages:
        source_ref = _source_ref(
            message,
            ticker,
            trade_date,
            tool_calls_by_id,
        )
        artifact_key = (source_ref, str(getattr(message, "tool_call_id", "")))
        if source_ref is None or artifact_key in seen_artifacts:
            continue
        seen_artifacts.add(artifact_key)
        content = str(getattr(message, "content", "")).strip()
        if len(content) > _SOURCE_PREVIEW_LIMIT:
            head_length = _SOURCE_PREVIEW_LIMIT // 2
            tail_length = _SOURCE_PREVIEW_LIMIT - head_length
            content = (
                content[:head_length]
                + "\n... [middle omitted] ...\n"
                + content[-tail_length:]
            )
        catalog.append(f"- {source_ref}\n  Evidence: {content or '[empty tool result]'}")
    source_catalog = "\n".join(catalog) if catalog else "- (no source refs available)"
    return (
        f"Finalize the {analyst} analyst report for {ticker} on {trade_date}. "
        "Return exactly one AnalystEvidenceReport. Preserve only claims supported "
        "by the source catalog. Every material claim must use source_refs copied "
        "verbatim from the allowed list; inventing a source ref is forbidden. "
        "Each MaterialClaim.source_quote must copy one exact contiguous "
        "source-language phrase from the cited tool evidence. The statement may "
        "be a localized paraphrase, but source_quote must not be translated or "
        "rewritten. "
        "If no listed source supports a decision-relevant premise, omit that claim.\n\n"
        "Allowed source refs and collected evidence:\n"
        f"{source_catalog}\n\n"
        "Unvalidated prose draft (do not return it unless converted into the schema):\n"
        f"{draft.strip() or '[empty draft]'}"
    )


def _normalize_submission(
    submitted: AnalystEvidenceReport | Mapping[str, Any],
    analyst: str,
) -> tuple[str, tuple[MaterialClaim, ...]]:
    validated = AnalystEvidenceReport.model_validate(submitted)
    report = validated.report_markdown.strip()
    if not report:
        raise ValueError("structured report markdown was empty")
    claim_ids = tuple(claim.claim_id for claim in validated.material_claims)
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("structured report contained duplicate material claim IDs")
    invalid_ids = tuple(
        claim_id
        for claim_id in claim_ids
        if not claim_id.startswith(f"{analyst}.")
    )
    if invalid_ids:
        raise ValueError(
            f"{analyst} material claim IDs must use the {analyst}. namespace"
        )
    claims = tuple(
        claim.model_copy(update={"analyst": analyst})
        for claim in validated.material_claims
    )
    return report, claims


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return "validation_error"
    return "transport_error"


def _apply_deterministic_history_requirements(
    evidence: EvidenceState,
) -> EvidenceState:
    """Derive indicator warmups from exact fact-to-calculation bindings."""
    facts_by_id = {fact.fact_id: fact for fact in evidence.source_facts}
    claims = []
    for claim in evidence.material_claims:
        calculation_ids = tuple(
            calculation_id
            for fact_id in claim.fact_ids
            for fact in (facts_by_id.get(fact_id),)
            if fact is not None
            for calculation_id in fact.calculation_ids
        )
        derived_requirements = tuple(
            _minimum_history_for_indicator(calculation_id)
            for calculation_id in calculation_ids
        )
        minimum_history_rows = max(derived_requirements, default=1)
        claims.append(
            claim.model_copy(
                update={"minimum_history_rows": minimum_history_rows}
            )
        )
    return evidence.model_copy(update={"material_claims": tuple(claims)})


def _unavailable_result(analyst: str, reason: str) -> AnalystSubmissionResult:
    label = analyst.title()
    report = (
        f"ANALYSIS_UNAVAILABLE: The {label} Analyst did not produce a validated "
        "structured evidence report. No directional conclusion was issued."
    )
    return AnalystSubmissionResult(
        message=AIMessage(content=report),
        report=report,
        submission_source=_submission_source(
            analyst,
            EvidenceStatus.UNAVAILABLE,
            reason,
        ),
    )


def _finalize_once(
    *,
    finalizer: Any | None,
    analyst: str,
    ticker: str,
    trade_date: str,
    draft: str,
    messages: list[Any],
) -> AnalystSubmissionResult:
    if finalizer is None:
        return _unavailable_result(analyst, "unsupported")
    prompt = _finalization_prompt(
        analyst=analyst,
        ticker=ticker,
        trade_date=trade_date,
        draft=draft,
        messages=messages,
    )
    try:
        submitted = finalizer.invoke(prompt)
        if submitted is None:
            return _unavailable_result(analyst, "none_parsed")
        report, claims = _normalize_submission(submitted, analyst)
    except Exception as exc:  # noqa: BLE001 - classified without provider payloads
        reason = _failure_reason(exc)
        logger.warning(
            "%s Analyst: structured finalization failed (%s)",
            analyst.title(),
            reason,
        )
        return _unavailable_result(analyst, reason)
    return AnalystSubmissionResult(
        message=AIMessage(content=report),
        report=report,
        claims=claims,
        submission_source=_submission_source(
            analyst,
            EvidenceStatus.AVAILABLE,
            "finalized_structured",
        ),
    )


def process_analyst_response(
    response: AIMessage,
    *,
    finalizer: Any | None,
    analyst: str,
    ticker: str,
    trade_date: str,
    messages: list[Any],
) -> AnalystSubmissionResult:
    """Accept a typed report, continue data calls, or finalize prose exactly once."""
    tool_calls = tuple(response.tool_calls or ())
    report_calls = tuple(
        call for call in tool_calls if call["name"] == _REPORT_TOOL_NAME
    )
    data_calls = tuple(
        call for call in tool_calls if call["name"] != _REPORT_TOOL_NAME
    )

    if data_calls:
        # A report emitted alongside data calls is premature. Remove only the
        # report pseudo-tool so the graph's ToolNode can execute every data call.
        return AnalystSubmissionResult(
            message=response.model_copy(update={"tool_calls": list(data_calls)}),
            report="",
        )

    if report_calls:
        try:
            report, claims = _normalize_submission(report_calls[0]["args"], analyst)
        except (ValidationError, ValueError, TypeError):
            return _finalize_once(
                finalizer=finalizer,
                analyst=analyst,
                ticker=ticker,
                trade_date=trade_date,
                draft=str(response.content),
                messages=messages,
            )
        return AnalystSubmissionResult(
            message=AIMessage(content=report),
            report=report,
            claims=claims,
            submission_source=_submission_source(
                analyst,
                EvidenceStatus.AVAILABLE,
                "direct_tool",
            ),
        )

    return _finalize_once(
        finalizer=finalizer,
        analyst=analyst,
        ticker=ticker,
        trade_date=trade_date,
        draft=str(response.content),
        messages=messages,
    )


def build_analyst_update(
    state: Mapping[str, Any],
    result: AnalystSubmissionResult,
    report_key: str,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "messages": [result.message],
        report_key: result.report,
    }
    if result.submission_source is None:
        return update
    call_ids_by_source = source_ref_tool_call_ids(
        state["messages"],
        str(state["company_of_interest"]),
        str(state["trade_date"]),
    )
    tool_calls_by_id = _tool_calls_by_id(state["messages"])
    tool_evidence = build_tool_evidence_state(
        state["messages"],
        result.claims,
        tool_call_ids_by_source=call_ids_by_source,
        tool_calls_by_id=tool_calls_by_id,
    )
    tool_evidence = _apply_deterministic_history_requirements(tool_evidence)
    evidence = EvidenceState.model_validate(state.get("evidence_state", {}))
    active_snapshot = get_active_authoritative_market_snapshot(
        str(state["company_of_interest"]),
        str(state["trade_date"]),
    )
    if active_snapshot is not None:
        evidence = evidence.model_copy(
            update={
                "market_snapshot": MarketSnapshotEvidence(
                    symbol=active_snapshot.symbol,
                    provider=active_snapshot.provider,
                    retrieved_at=active_snapshot.retrieved_at,
                    adjustment_basis=active_snapshot.adjustment_basis,
                    requested_date=active_snapshot.requested_date,
                    effective_trading_date=active_snapshot.effective_trading_date,
                    history_rows=len(active_snapshot.frame),
                    frame_sha256=active_snapshot.frame_sha256,
                    snapshot_id=active_snapshot.snapshot_id,
                )
            }
        )
    try:
        evidence = merge_material_claims(
            evidence,
            tool_evidence.material_claims,
        )
        evidence = merge_source_facts(evidence, tool_evidence.source_facts)
        evidence = merge_source_artifacts(
            evidence,
            tool_evidence.source_artifacts,
        )
        evidence = merge_claim_validations(evidence, tool_evidence.claim_validations)
    except ValueError:
        conflicted_submission = result.submission_source.model_copy(
            update={
                "status": EvidenceStatus.CONFLICTED,
                "detail": "immutable claim or fact ID was redefined",
            }
        )
        update["evidence_state"] = merge_evidence_sources(
            evidence,
            (conflicted_submission,),
        ).model_dump(mode="json")
        return update
    update["evidence_state"] = merge_evidence_sources(
        evidence,
        (result.submission_source, *tool_evidence.sources),
    ).model_dump(mode="json")
    return update
