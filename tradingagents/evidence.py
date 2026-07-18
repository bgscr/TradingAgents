"""Typed evidence shared by analysts and deterministic decision gates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field

_NUMERIC_CLAIM_PATTERN = re.compile(
    r"(?<!\w)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?!\w)"
)


def _numeric_claim_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).replace(",", "").removeprefix("+")
        for match in _NUMERIC_CLAIM_PATTERN.finditer(text)
    )


class EvidenceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CONFLICTED = "conflicted"


class EvidenceReadiness(str, Enum):
    DECISION_READY = "decision_ready"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"


class DecisionConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InstrumentIdentityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str


class MarketSnapshotEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    provider: str
    retrieved_at: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    history_rows: int = Field(ge=0)


class MaterialClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim_id: str
    analyst: str
    statement: str
    source_refs: tuple[str, ...] = Field(min_length=1)


class AnalystEvidenceReport(BaseModel):
    """Typed final payload emitted by a tool-calling analyst."""

    model_config = ConfigDict(frozen=True)

    report_markdown: str
    material_claims: tuple[MaterialClaim, ...] = ()


class EvidenceSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    status: EvidenceStatus
    required: bool
    detail: str = ""


class EvidenceState(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_identity: InstrumentIdentityEvidence | None = None
    market_snapshot: MarketSnapshotEvidence | None = None
    material_claims: tuple[MaterialClaim, ...] = ()
    sources: tuple[EvidenceSource, ...] = ()


def merge_material_claims(
    evidence: EvidenceState | Mapping[str, Any] | None,
    claims: Iterable[MaterialClaim],
) -> EvidenceState:
    """Merge analyst claims into checkpoint-safe shared evidence by claim ID."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {claim.claim_id: claim for claim in current.material_claims}
    merged.update((claim.claim_id, claim) for claim in claims)
    return current.model_copy(update={"material_claims": tuple(merged.values())})


def merge_evidence_sources(
    evidence: EvidenceState | Mapping[str, Any] | None,
    sources: Iterable[EvidenceSource],
) -> EvidenceState:
    """Merge deterministic source statuses into shared evidence by source ID."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {source.source_id: source for source in current.sources}
    severity = {
        EvidenceStatus.AVAILABLE: 0,
        EvidenceStatus.UNAVAILABLE: 1,
        EvidenceStatus.CONFLICTED: 2,
    }
    for source in sources:
        existing = merged.get(source.source_id)
        if existing is None:
            merged[source.source_id] = source
            continue
        selected = (
            existing
            if severity[existing.status] >= severity[source.status]
            else source
        )
        merged[source.source_id] = selected.model_copy(
            update={"required": existing.required or source.required}
        )
    return current.model_copy(update={"sources": tuple(merged.values())})


def evidence_sources_from_tool_messages(
    messages: Iterable[Any],
    claims: Iterable[MaterialClaim],
) -> tuple[EvidenceSource, ...]:
    """Register claim refs only when their named tool actually returned a message."""
    tool_messages: dict[str, list[Any]] = {}
    for message in messages:
        tool_name = getattr(message, "name", None)
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_name and tool_call_id:
            tool_messages.setdefault(tool_name, []).append(message)

    claims_by_source: dict[str, list[MaterialClaim]] = {}
    for claim in claims:
        for source_ref in claim.source_refs:
            claims_by_source.setdefault(source_ref, []).append(claim)

    def message_is_unavailable(message: Any) -> bool:
        return getattr(message, "status", None) == "error" or any(
            marker in str(getattr(message, "content", "")).upper()
            for marker in (
                "DATA_UNAVAILABLE",
                "NOT_AVAILABLE",
                "NOT APPLICABLE",
                "UNAVAILABLE",
            )
        )

    sources = []
    for source_ref, referenced_claims in claims_by_source.items():
        tool_name = source_ref.partition(":")[0]
        if tool_name == "snapshot":
            tool_name = "get_verified_market_snapshot"
        matching_messages = tool_messages.get(tool_name, [])
        if not matching_messages:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="no matching tool result",
                )
            )
            continue
        available_messages = tuple(
            message for message in matching_messages if not message_is_unavailable(message)
        )
        if not available_messages:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="tool returned unavailable",
                )
            )
            continue

        message_token_sets = tuple(
            set(_numeric_claim_tokens(str(getattr(message, "content", ""))))
            for message in available_messages
        )
        unsupported_tokens = []
        for claim in referenced_claims:
            claim_tokens = _numeric_claim_tokens(claim.statement)
            if claim_tokens and not any(
                set(claim_tokens).issubset(message_tokens)
                for message_tokens in message_token_sets
            ):
                unsupported_tokens.extend(
                    token
                    for token in claim_tokens
                    if token not in unsupported_tokens
                    and not any(token in tokens for tokens in message_token_sets)
                )
        if unsupported_tokens:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.CONFLICTED,
                    required=False,
                    detail=(
                        "source does not contain numeric claim(s): "
                        + ", ".join(unsupported_tokens)
                    ),
                )
            )
            continue
        sources.append(
            EvidenceSource(
                source_id=source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=False,
            )
        )
    return tuple(sources)


class AdmissionGateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    admitted: bool
    readiness: EvidenceReadiness
    coverage: float = Field(ge=0.0, le=1.0)
    diagnostics: tuple[str, ...] = ()


class DraftThesis(BaseModel):
    """Structured directional thesis evaluated before a decision is rendered."""

    model_config = ConfigDict(frozen=True)

    rating: str
    narrative: str
    material_claim_ids: tuple[str, ...] = ()


class DecisionGateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    permitted: bool
    readiness: EvidenceReadiness
    evidence_coverage: float = Field(ge=0.0, le=1.0)
    confidence: DecisionConfidence
    diagnostics: tuple[str, ...] = ()
    revision_applied: bool = False


def evaluate_decision_gate(
    draft: DraftThesis,
    evidence: EvidenceState,
    *,
    original_draft: DraftThesis | None = None,
) -> DecisionGateResult:
    """Validate the material premises used by a directional draft thesis."""
    total_evidence = 2 + len(evidence.sources)
    available_evidence = int(evidence.instrument_identity is not None) + int(
        evidence.market_snapshot is not None
    )
    available_evidence += sum(
        source.status is EvidenceStatus.AVAILABLE for source in evidence.sources
    )
    evidence_coverage = available_evidence / total_evidence
    if original_draft is not None and draft.rating != original_draft.rating:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=(
                "Constrained revision cannot change rating from "
                f"{original_draft.rating} to {draft.rating}.",
            ),
            revision_applied=True,
        )
    if original_draft is not None:
        original_claim_ids = set(original_draft.material_claim_ids)
        added_claim_ids = tuple(
            claim_id
            for claim_id in draft.material_claim_ids
            if claim_id not in original_claim_ids
        )
        if added_claim_ids:
            return DecisionGateResult(
                permitted=False,
                readiness=EvidenceReadiness.INSUFFICIENT,
                evidence_coverage=evidence_coverage,
                confidence=DecisionConfidence.LOW,
                diagnostics=tuple(
                    f"Constrained revision cannot add material claim: {claim_id}."
                    for claim_id in added_claim_ids
                ),
                revision_applied=True,
            )
    if not draft.material_claim_ids:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=("Draft thesis cites no material claims.",),
            revision_applied=original_draft is not None,
        )
    claims_by_id = {claim.claim_id: claim for claim in evidence.material_claims}
    missing_claim_ids = tuple(
        claim_id
        for claim_id in draft.material_claim_ids
        if claim_id not in claims_by_id
    )
    if missing_claim_ids:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise missing from shared evidence: {claim_id}."
                for claim_id in missing_claim_ids
            ),
        )
    sources_by_id = {source.source_id: source for source in evidence.sources}
    conflicted_sources = tuple(
        (claim, source)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        for source in (sources_by_id.get(source_ref),)
        if source is not None and source.status is EvidenceStatus.CONFLICTED
    )
    if conflicted_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} uses conflicted source "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''}."
                for claim, source in conflicted_sources
            ),
        )
    unavailable_sources = tuple(
        (claim, source)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        for source in (sources_by_id.get(source_ref),)
        if source is not None and source.status is EvidenceStatus.UNAVAILABLE
    )
    if unavailable_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} uses unavailable source "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''}."
                for claim, source in unavailable_sources
            ),
        )
    unregistered_sources = tuple(
        (claim, source_ref)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        if source_ref not in sources_by_id
        and not (
            evidence.market_snapshot is not None
            and source_ref
            == (
                f"snapshot:{evidence.market_snapshot.symbol}:"
                f"{evidence.market_snapshot.effective_trading_date}"
            )
        )
    )
    if unregistered_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} cites unregistered source "
                f"{source_ref}."
                for claim, source_ref in unregistered_sources
            ),
        )
    supported_numeric_claims = {
        token
        for claim_id in draft.material_claim_ids
        for token in _numeric_claim_tokens(claims_by_id[claim_id].statement)
    }
    unsupported_numeric_claims = tuple(
        token
        for token in _numeric_claim_tokens(draft.narrative)
        if token not in supported_numeric_claims
    )
    if unsupported_numeric_claims:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft narrative contains unsupported numeric claim: {token}."
                for token in unsupported_numeric_claims
            ),
        )
    readiness = (
        EvidenceReadiness.DEGRADED
        if any(
            not source.required and source.status is EvidenceStatus.UNAVAILABLE
            for source in evidence.sources
        )
        else EvidenceReadiness.DECISION_READY
    )
    confidence = (
        DecisionConfidence.HIGH
        if readiness is EvidenceReadiness.DECISION_READY
        else (
            DecisionConfidence.MEDIUM
            if evidence_coverage >= 0.5
            else DecisionConfidence.LOW
        )
    )
    return DecisionGateResult(
        permitted=True,
        readiness=readiness,
        evidence_coverage=evidence_coverage,
        confidence=confidence,
        revision_applied=original_draft is not None,
    )


def build_evidence_state(
    *,
    symbol: str,
    identity: Mapping[str, str],
    snapshot: Any | None,
) -> EvidenceState:
    """Convert acquired identity and snapshot data into checkpoint-safe evidence."""
    name = identity.get("company_name") or identity.get("name")
    instrument_identity = (
        InstrumentIdentityEvidence(symbol=symbol, name=name) if name else None
    )
    market_snapshot = None
    if snapshot is not None:
        market_snapshot = MarketSnapshotEvidence(
            symbol=snapshot.symbol,
            provider=snapshot.provider,
            retrieved_at=snapshot.retrieved_at,
            adjustment_basis=snapshot.adjustment_basis,
            requested_date=snapshot.requested_date,
            effective_trading_date=snapshot.effective_trading_date,
            history_rows=len(snapshot.frame),
        )
    return EvidenceState(
        instrument_identity=instrument_identity,
        market_snapshot=market_snapshot,
    )


def acquire_run_evidence(symbol: str, requested_date: str) -> EvidenceState:
    """Acquire run identity and the validated five-year market snapshot."""
    from tradingagents.agents.utils.agent_utils import resolve_instrument_identity
    from tradingagents.dataflows.errors import NoMarketDataError
    from tradingagents.dataflows.market_snapshot import (
        get_authoritative_market_snapshot,
    )

    requested = datetime.strptime(requested_date, "%Y-%m-%d")
    start_date = (requested - relativedelta(years=5)).strftime("%Y-%m-%d")
    identity = resolve_instrument_identity(symbol)
    try:
        snapshot = get_authoritative_market_snapshot(
            symbol,
            start_date,
            requested_date,
        )
    except NoMarketDataError:
        snapshot = None
    return build_evidence_state(
        symbol=symbol,
        identity=identity,
        snapshot=snapshot,
    )


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    readiness: EvidenceReadiness
    summary: str
    diagnostics: tuple[str, ...]
    evidence_coverage: float = Field(ge=0.0, le=1.0)


def render_analysis_outcome(outcome: AnalysisOutcome) -> str:
    """Render a non-directional outcome without portfolio recommendation fields."""
    labels = {
        EvidenceReadiness.INSUFFICIENT: "Insufficient Evidence",
        EvidenceReadiness.CONFLICTED: "Conflicted Evidence",
    }
    lines = [
        f"**Analysis Outcome:** {labels[outcome.readiness]}",
        f"**Evidence Coverage:** {outcome.evidence_coverage:.1%}",
        "",
        outcome.summary,
        "",
        "No Trading Decision was issued.",
    ]
    if outcome.diagnostics:
        lines.extend(["", "### Diagnostics", ""])
        lines.extend(f"- {diagnostic}" for diagnostic in outcome.diagnostics)
    return "\n".join(lines)


def evaluate_admission_gate(
    evidence: EvidenceState,
    minimum_history_rows: int = 200,
) -> AdmissionGateResult:
    """Evaluate whether acquired evidence may proceed to thesis synthesis."""
    total_evidence = 2 + len(evidence.sources)
    available_evidence = int(evidence.instrument_identity is not None) + int(
        evidence.market_snapshot is not None
    )
    available_evidence += sum(
        source.status is EvidenceStatus.AVAILABLE for source in evidence.sources
    )
    coverage = available_evidence / total_evidence

    if evidence.instrument_identity is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Required evidence missing: resolved instrument identity.",
            ),
        )

    if evidence.market_snapshot is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Required evidence missing: Authoritative Market Snapshot.",
            ),
        )

    if not evidence.market_snapshot.adjustment_basis.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Required evidence missing: known Adjustment Basis.",
            ),
        )

    if not evidence.market_snapshot.effective_trading_date.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Required evidence missing: Effective Trading Date.",
            ),
        )

    if evidence.market_snapshot.history_rows < minimum_history_rows:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Insufficient market history: "
                f"{evidence.market_snapshot.history_rows} rows available; "
                f"{minimum_history_rows} required.",
            ),
        )

    required_conflicted = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.CONFLICTED
    )
    if required_conflicted:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            coverage=coverage,
            diagnostics=tuple(
                "Required evidence conflicted: "
                f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
                for source in required_conflicted
            ),
        )

    required_unavailable = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.UNAVAILABLE
    )
    if required_unavailable:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=tuple(
                "Required evidence unavailable: "
                f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
                for source in required_unavailable
            ),
        )

    optional_unavailable = tuple(
        source
        for source in evidence.sources
        if not source.required and source.status is EvidenceStatus.UNAVAILABLE
    )
    diagnostics = tuple(
        "Optional evidence unavailable: "
        f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
        for source in optional_unavailable
    )

    return AdmissionGateResult(
        admitted=True,
        readiness=(
            EvidenceReadiness.DEGRADED
            if optional_unavailable
            else EvidenceReadiness.DECISION_READY
        ),
        coverage=coverage,
        diagnostics=diagnostics,
    )
