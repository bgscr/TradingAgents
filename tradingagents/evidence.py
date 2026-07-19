"""Typed evidence shared by analysts and deterministic decision gates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, model_validator

_NUMERIC_CLAIM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
    r"(?![A-Za-z0-9_])"
)
_CLAIM_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"

_NUMERIC_TRANSLATION = str.maketrans(
    {
        "\u2212": "-",
        "\ufe63": "-",
        "\uff0d": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\uff0b": "+",
    }
)


def _normalize_numeric_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_NUMERIC_TRANSLATION)


def _numeric_claim_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).replace(",", "").removeprefix("+")
        for match in _NUMERIC_CLAIM_PATTERN.finditer(_normalize_numeric_text(text))
    )


def _numeric_token_parts(token: str) -> tuple[Decimal, int, bool] | None:
    normalized = token.replace(",", "").removeprefix("+")
    is_percent = normalized.endswith("%")
    number = normalized.removesuffix("%")
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None
    decimals = len(number.partition(".")[2]) if "." in number else 0
    return value, decimals, is_percent


def _numeric_token_supported(claim_token: str, source_tokens: Iterable[str]) -> bool:
    claim_parts = _numeric_token_parts(claim_token)
    if claim_parts is None:
        return False
    claim_value, claim_decimals, claim_percent = claim_parts
    for source_token in source_tokens:
        source_parts = _numeric_token_parts(source_token)
        if source_parts is None:
            continue
        source_value, _, source_percent = source_parts
        if claim_percent != source_percent:
            continue
        if claim_value == source_value:
            return True
        if claim_decimals > 0:
            tolerance = Decimal(5).scaleb(-(claim_decimals + 1))
            if abs(claim_value - source_value) <= tolerance:
                return True
    return False


def _source_quote_is_unavailable(quote: str) -> bool:
    normalized = quote.strip().upper()
    return normalized == "N/A" or any(
        marker in normalized
        for marker in (
            "DATA_UNAVAILABLE",
            "NOT_AVAILABLE",
            "UNAVAILABLE",
            "N/A: INSUFFICIENT HISTORY",
            "N/A: NOT A TRADING DAY",
        )
    )


def _source_quote_is_metadata(quote: str) -> bool:
    normalized = quote.strip().casefold()
    normalized = re.sub(r"^(?:#{1,6}|[-*+>])\s*", "", normalized).strip()
    return any(
        normalized.startswith(prefix)
        for prefix in (
            "source:",
            "provider:",
            "adjustment basis:",
            "effective trading date:",
            "retrieved at:",
            "frame sha-256:",
            "snapshot id:",
            "total records:",
            "history rows:",
        )
    )


class EvidenceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CONFLICTED = "conflicted"
    NOT_APPLICABLE = "not_applicable"


class EvidenceReadiness(str, Enum):
    DECISION_READY = "decision_ready"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"


class ClaimValidationStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


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
    frame_sha256: str = ""
    snapshot_id: str = ""


class MaterialClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    analyst: str
    statement: str
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=1)
    source_quote: str = Field(
        min_length=1,
        description=(
            "Exact source-language quotation copied from one cited source. "
            "The localized statement may paraphrase this quote."
        ),
    )
    fact_ids: tuple[str, ...] = ()
    minimum_history_rows: int = Field(
        default=1,
        ge=1,
        description=(
            "Minimum accepted snapshot history needed to support this claim's "
            "calculation; use the exact indicator warmup, or 1 for observations."
        ),
    )


class SubmittedMaterialClaim(BaseModel):
    """Minimal claim DTO exposed to an LLM structured-output schema.

    Provenance-derived fields on :class:`MaterialClaim` are deliberately absent:
    the application, not the model, assigns the analyst, materializes fact IDs,
    and computes indicator history requirements after exact quote validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(
        pattern=_CLAIM_ID_PATTERN,
        description=(
            "Stable claim ID using only letters, digits, underscore, dot, colon, "
            "or hyphen; it must start with a letter."
        ),
    )
    statement: str
    source_ref: str = Field(min_length=1)
    source_quote: str = Field(
        min_length=1,
        description="Exact contiguous source-language quotation from source_ref.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_internal_claim_for_programmatic_compatibility(cls, value: Any) -> Any:
        # This compatibility path does not alter the JSON schema shown to the
        # model. It only avoids breaking programmatic callers that already hold
        # a fully validated internal MaterialClaim.
        if isinstance(value, MaterialClaim):
            return {
                "claim_id": value.claim_id,
                "statement": value.statement,
                "source_ref": value.source_refs[0],
                "source_quote": value.source_quote,
            }
        if (
            isinstance(value, Mapping)
            and "source_ref" not in value
            and "source_refs" in value
        ):
            source_refs = value.get("source_refs")
            if isinstance(source_refs, (list, tuple)) and len(source_refs) == 1:
                # Legacy direct-tool payloads may still include internal fields.
                # Copy only the four submission fields; analyst/fact/history data
                # is never trusted and is deterministically rebuilt downstream.
                return {
                    "claim_id": value.get("claim_id"),
                    "statement": value.get("statement"),
                    "source_ref": source_refs[0],
                    "source_quote": value.get("source_quote"),
                }
        return value


class SourceFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str
    source_ref: str
    tool_call_id: str
    tool_name: str = ""
    artifact_sha256: str
    raw_text: str
    source_span_start: int = Field(ge=0)
    source_span_end: int = Field(ge=0)
    normalized_numeric_tokens: tuple[str, ...] = ()
    calculation_ids: tuple[str, ...] = ()


class SourceArtifact(BaseModel):
    """Exact immutable tool result whose digest is cited by Source Facts."""

    model_config = ConfigDict(frozen=True)

    artifact_sha256: str
    source_ref: str
    tool_call_id: str
    tool_name: str
    raw_text: str


class ClaimValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim_id: str
    status: ClaimValidationStatus
    detail: str = ""
    fact_ids: tuple[str, ...] = ()


class AnalystEvidenceReport(BaseModel):
    """Typed final payload emitted by a tool-calling analyst."""

    model_config = ConfigDict(frozen=True)

    report_markdown: str
    material_claims: tuple[SubmittedMaterialClaim, ...] = ()


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
    source_facts: tuple[SourceFact, ...] = ()
    source_artifacts: tuple[SourceArtifact, ...] = ()
    claim_validations: tuple[ClaimValidation, ...] = ()
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
    for claim in claims:
        existing = merged.get(claim.claim_id)
        if existing is not None and existing != claim:
            raise ValueError(
                f"Material claim ID {claim.claim_id!r} was redefined; claim IDs are immutable."
            )
        merged[claim.claim_id] = claim
    return current.model_copy(update={"material_claims": tuple(merged.values())})


def merge_source_facts(
    evidence: EvidenceState | Mapping[str, Any] | None,
    facts: Iterable[SourceFact],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {fact.fact_id: fact for fact in current.source_facts}
    for fact in facts:
        existing = merged.get(fact.fact_id)
        if existing is not None and existing != fact:
            raise ValueError(f"Source fact ID {fact.fact_id!r} was redefined.")
        merged[fact.fact_id] = fact
    return current.model_copy(update={"source_facts": tuple(merged.values())})


def merge_source_artifacts(
    evidence: EvidenceState | Mapping[str, Any] | None,
    artifacts: Iterable[SourceArtifact],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {
        (artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in current.source_artifacts
    }
    for artifact in artifacts:
        key = (artifact.tool_call_id, artifact.source_ref)
        existing = merged.get(key)
        if existing is not None and existing != artifact:
            raise ValueError(
                f"Source artifact {artifact.artifact_sha256!r} was redefined."
            )
        merged[key] = artifact
    return current.model_copy(update={"source_artifacts": tuple(merged.values())})


def merge_claim_validations(
    evidence: EvidenceState | Mapping[str, Any] | None,
    validations: Iterable[ClaimValidation],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {
        validation.claim_id: validation for validation in current.claim_validations
    }
    for validation in validations:
        existing = merged.get(validation.claim_id)
        if existing is not None and existing != validation:
            raise ValueError(
                f"Claim validation for {validation.claim_id!r} was redefined."
            )
        merged[validation.claim_id] = validation
    return current.model_copy(update={"claim_validations": tuple(merged.values())})


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
        EvidenceStatus.NOT_APPLICABLE: -1,
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
    *,
    tool_call_ids_by_source: Mapping[str, Iterable[str]],
) -> tuple[EvidenceSource, ...]:
    """Compatibility wrapper returning source availability from paired results.

    Claim support is deliberately represented separately in
    ``EvidenceState.claim_validations`` so one bad claim cannot poison every
    other claim that cites the same available source.
    """
    return build_tool_evidence_state(
        messages,
        claims,
        tool_call_ids_by_source=tool_call_ids_by_source,
    ).sources


def build_tool_evidence_state(
    messages: Iterable[Any],
    claims: Iterable[MaterialClaim],
    *,
    tool_call_ids_by_source: Mapping[str, Iterable[str]],
    tool_calls_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> EvidenceState:
    """Materialize immutable source facts and per-claim validation results."""
    message_list = tuple(messages)
    claim_list = tuple(claims)
    call_catalog = tool_calls_by_id or {}

    def calculation_ids_for_span(
        message: Any,
        content: str,
        span_start: int,
    ) -> tuple[str, ...]:
        if getattr(message, "name", None) != "get_indicators":
            return ()
        call = call_catalog.get(str(getattr(message, "tool_call_id", "")))
        if call is None:
            return ()
        args = call.get("args") or {}
        raw_indicators = args.get("indicator", "") if isinstance(args, Mapping) else ""
        indicators = tuple(
            indicator.strip().casefold()
            for indicator in str(raw_indicators).split(",")
            if indicator.strip()
        )
        section_starts = []
        for indicator in indicators:
            match = re.search(
                rf"(?mi)^##\s+{re.escape(indicator)}\s+values\b",
                content,
            )
            if match:
                section_starts.append((match.start(), indicator))
        section_starts.sort()
        for index, (section_start, indicator) in enumerate(section_starts):
            section_end = (
                section_starts[index + 1][0]
                if index + 1 < len(section_starts)
                else len(content)
            )
            if section_start <= span_start < section_end:
                return (indicator,)
        return ()
    tool_messages: dict[str, list[Any]] = {}
    for message in message_list:
        tool_call_id = getattr(message, "tool_call_id", None)
        if getattr(message, "name", None) and tool_call_id:
            tool_messages.setdefault(str(tool_call_id), []).append(message)

    claims_by_source: dict[str, list[MaterialClaim]] = {}
    for claim in claims:
        for source_ref in claim.source_refs:
            claims_by_source.setdefault(source_ref, []).append(claim)
    allowed_refs = frozenset(tool_call_ids_by_source)

    def message_is_unavailable(message: Any) -> bool:
        if getattr(message, "status", None) == "error":
            return True
        content = "\n".join(
            line
            for line in str(getattr(message, "content", "")).upper().splitlines()
            if not line.lstrip().startswith("DATA_DEGRADED:")
        )
        return any(
            marker in content
            for marker in (
                "DATA_UNAVAILABLE",
                "NOT_AVAILABLE",
                "NOT APPLICABLE",
                "UNAVAILABLE",
            )
        )

    sources: list[EvidenceSource] = []
    available_messages_by_source: dict[str, tuple[Any, ...]] = {}
    for source_ref in claims_by_source:
        if source_ref not in allowed_refs:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="source ref is not in allowed catalog",
                )
            )
            continue
        source_kind = source_ref.partition(":")[0]
        allowed_tool_names = (
            {"get_verified_market_snapshot", "get_indicators"}
            if source_kind == "snapshot"
            else {source_kind}
        )
        matching_messages = tuple(
            message
            for tool_call_id in tool_call_ids_by_source[source_ref]
            for message in tool_messages.get(str(tool_call_id), ())
            if getattr(message, "name", None) in allowed_tool_names
        )
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
        available_messages_by_source[source_ref] = available_messages
        sources.append(
            EvidenceSource(
                source_id=source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=False,
            )
        )

    facts: list[SourceFact] = []
    artifacts: dict[tuple[str, str, str], SourceArtifact] = {}
    validations: list[ClaimValidation] = []
    enriched_claims: list[MaterialClaim] = []
    for claim in claim_list:
        claim_facts: list[SourceFact] = []
        unavailable_refs: list[str] = []
        numeric_diagnostics: list[str] = []
        ambiguous_quote = False
        quote = claim.source_quote.strip() or claim.statement.strip()
        for source_ref in claim.source_refs:
            available_messages = available_messages_by_source.get(source_ref, ())
            if not available_messages:
                unavailable_refs.append(source_ref)
                continue
            for message in available_messages:
                content = str(getattr(message, "content", ""))
                if quote and content.count(quote) > 1:
                    ambiguous_quote = True
                    continue
                span_start = content.find(quote) if quote else -1
                if span_start < 0:
                    continue
                tool_call_id = str(getattr(message, "tool_call_id", ""))
                artifact_digest = sha256(content.encode()).hexdigest()
                artifact_key = (artifact_digest, tool_call_id, source_ref)
                artifacts[artifact_key] = SourceArtifact(
                    artifact_sha256=artifact_digest,
                    source_ref=source_ref,
                    tool_call_id=tool_call_id,
                    tool_name=str(getattr(message, "name", "")),
                    raw_text=content,
                )
                fact_digest = sha256(
                    f"{source_ref}\0{tool_call_id}\0{quote}".encode()
                ).hexdigest()
                claim_facts.append(
                    SourceFact(
                        fact_id=f"fact:{fact_digest}",
                        source_ref=source_ref,
                        tool_call_id=tool_call_id,
                        tool_name=str(getattr(message, "name", "")),
                        artifact_sha256=artifact_digest,
                        raw_text=quote,
                        source_span_start=span_start,
                        source_span_end=span_start + len(quote),
                        normalized_numeric_tokens=_numeric_claim_tokens(quote),
                        calculation_ids=calculation_ids_for_span(
                            message,
                            content,
                            span_start,
                        ),
                    )
                )
                break

        unique_facts = {fact.fact_id: fact for fact in claim_facts}
        claim_fact_ids = tuple(unique_facts)
        if claim_fact_ids:
            unsupported_tokens = unsupported_numeric_claim_tokens((claim,), (quote,))
            numeric_diagnostics.extend(
                token for token in unsupported_tokens if token not in numeric_diagnostics
            )
        if not claim_fact_ids:
            if unavailable_refs and len(unavailable_refs) == len(claim.source_refs):
                detail = "cited source unavailable: " + ", ".join(unavailable_refs)
            elif ambiguous_quote:
                detail = "source quote is ambiguous within the cited tool result"
            else:
                detail = "source quote is absent from the cited tool result"
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail=detail,
                )
            )
        elif _source_quote_is_unavailable(quote):
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail="source quote reports unavailable evidence",
                    fact_ids=claim_fact_ids,
                )
            )
        elif _source_quote_is_metadata(quote):
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail="source quote contains provenance metadata, not a material fact",
                    fact_ids=claim_fact_ids,
                )
            )
        elif numeric_diagnostics:
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail=(
                        "source does not contain numeric claim(s): "
                        + ", ".join(numeric_diagnostics)
                    ),
                    fact_ids=claim_fact_ids,
                )
            )
        else:
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.SUPPORTED,
                    fact_ids=claim_fact_ids,
                )
            )
        facts.extend(unique_facts.values())
        enriched_claims.append(claim.model_copy(update={"fact_ids": claim_fact_ids}))

    return EvidenceState(
        material_claims=tuple(enriched_claims),
        source_facts=tuple({fact.fact_id: fact for fact in facts}.values()),
        source_artifacts=tuple(artifacts.values()),
        claim_validations=tuple(validations),
        sources=tuple(sources),
    )


def build_inline_evidence_state(
    source_text_by_ref: Mapping[str, str | None],
    claims: Iterable[MaterialClaim],
) -> EvidenceState:
    """Validate claims against pre-fetched source blocks kept in the prompt."""
    claim_list = tuple(claims)
    normalized_sources = {
        source_ref: text if text is not None and text.strip() else None
        for source_ref, text in source_text_by_ref.items()
    }
    sources = tuple(
        EvidenceSource(
            source_id=source_ref,
            status=(
                EvidenceStatus.AVAILABLE
                if text is not None
                else EvidenceStatus.UNAVAILABLE
            ),
            required=False,
            detail="" if text is not None else "source returned unavailable",
        )
        for source_ref, text in normalized_sources.items()
    )
    facts: list[SourceFact] = []
    artifacts: dict[tuple[str, str, str], SourceArtifact] = {}
    validations: list[ClaimValidation] = []
    enriched_claims: list[MaterialClaim] = []
    for claim in claim_list:
        quote = claim.source_quote.strip() or claim.statement.strip()
        claim_facts: dict[str, SourceFact] = {}
        unavailable_refs: list[str] = []
        numeric_diagnostics: list[str] = []
        ambiguous_quote = False
        for source_ref in claim.source_refs:
            content = normalized_sources.get(source_ref)
            if content is None:
                unavailable_refs.append(source_ref)
                continue
            numeric_diagnostics.extend(
                token
                for token in unsupported_numeric_claim_tokens((claim,), (content,))
                if token not in numeric_diagnostics
            )
            if quote and content.count(quote) > 1:
                ambiguous_quote = True
                continue
            span_start = content.find(quote) if quote else -1
            if span_start < 0:
                continue
            artifact_digest = sha256(content.encode()).hexdigest()
            tool_call_id = f"inline:{source_ref}"
            artifact_key = (artifact_digest, tool_call_id, source_ref)
            artifacts[artifact_key] = SourceArtifact(
                artifact_sha256=artifact_digest,
                source_ref=source_ref,
                tool_call_id=tool_call_id,
                tool_name="inline_source",
                raw_text=content,
            )
            fact_digest = sha256(
                f"{source_ref}\0{artifact_digest}\0{quote}".encode()
            ).hexdigest()
            fact = SourceFact(
                fact_id=f"fact:{fact_digest}",
                source_ref=source_ref,
                tool_call_id=tool_call_id,
                tool_name="inline_source",
                artifact_sha256=artifact_digest,
                raw_text=quote,
                source_span_start=span_start,
                source_span_end=span_start + len(quote),
                normalized_numeric_tokens=_numeric_claim_tokens(quote),
            )
            claim_facts[fact.fact_id] = fact

        claim_fact_ids = tuple(claim_facts)
        if not claim.source_refs:
            detail = "claim cites no source refs"
            status = ClaimValidationStatus.UNSUPPORTED
        elif not claim_fact_ids:
            status = ClaimValidationStatus.UNSUPPORTED
            detail = (
                "cited source unavailable: " + ", ".join(unavailable_refs)
                if unavailable_refs and len(unavailable_refs) == len(claim.source_refs)
                else (
                    "source quote is ambiguous within the cited source block"
                    if ambiguous_quote
                    else "source quote is absent from the cited source block"
                )
            )
        elif _source_quote_is_unavailable(quote):
            status = ClaimValidationStatus.UNSUPPORTED
            detail = "source quote reports unavailable evidence"
        elif _source_quote_is_metadata(quote):
            status = ClaimValidationStatus.UNSUPPORTED
            detail = "source quote contains provenance metadata, not a material fact"
        elif numeric_diagnostics:
            status = ClaimValidationStatus.UNSUPPORTED
            detail = (
                "source does not contain numeric claim(s): "
                + ", ".join(numeric_diagnostics)
            )
        else:
            status = ClaimValidationStatus.SUPPORTED
            detail = ""
        validations.append(
            ClaimValidation(
                claim_id=claim.claim_id,
                status=status,
                detail=detail,
                fact_ids=claim_fact_ids,
            )
        )
        facts.extend(claim_facts.values())
        enriched_claims.append(claim.model_copy(update={"fact_ids": claim_fact_ids}))

    known_source_ids = set(normalized_sources)
    unknown_source_ids = tuple(
        dict.fromkeys(
            source_ref
            for claim in claim_list
            for source_ref in claim.source_refs
            if source_ref not in known_source_ids
        )
    )
    return EvidenceState(
        material_claims=tuple(enriched_claims),
        source_facts=tuple({fact.fact_id: fact for fact in facts}.values()),
        source_artifacts=tuple(artifacts.values()),
        claim_validations=tuple(validations),
        sources=(
            *sources,
            *(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="source ref is not in allowed catalog",
                )
                for source_ref in unknown_source_ids
            ),
        ),
    )


def unsupported_numeric_claim_tokens(
    claims: Iterable[MaterialClaim],
    source_texts: Iterable[str],
) -> tuple[str, ...]:
    """Return numeric claim tokens absent from every supplied source text."""
    source_tokens = tuple(
        token
        for source_text in source_texts
        for token in _numeric_claim_tokens(source_text)
    )
    unsupported = []
    for claim in claims:
        claim_tokens = _numeric_claim_tokens(claim.statement)
        unsupported.extend(
            token
            for token in claim_tokens
            if token not in unsupported
            and not _numeric_token_supported(token, source_tokens)
        )
    return tuple(unsupported)


def _normalize_material_text(text: str) -> str:
    tokens = re.findall(r"\w+(?:\.\w+)*|[%+-]", text.casefold())
    return " ".join(tokens)


def unsupported_material_claim_ids(
    claims: Iterable[MaterialClaim],
    source_texts: Iterable[str],
) -> tuple[str, ...]:
    """Return claims whose source-expressed statement is absent from all sources."""
    normalized_sources = tuple(
        _normalize_material_text(source_text) for source_text in source_texts
    )
    unsupported = []
    for claim in claims:
        source_expression = _normalize_material_text(
            claim.source_quote or claim.statement
        )
        if not source_expression or not any(
            source_expression in source_text for source_text in normalized_sources
        ):
            unsupported.append(claim.claim_id)
    return tuple(unsupported)


def _claim_provenance_failure(
    claim: MaterialClaim,
    evidence: EvidenceState,
) -> str | None:
    validations = {
        validation.claim_id: validation for validation in evidence.claim_validations
    }
    validation = validations.get(claim.claim_id)
    if validation is None:
        return "claim has no deterministic validation record"
    if validation.status is not ClaimValidationStatus.SUPPORTED:
        return validation.detail or "claim validation is unsupported"
    if not claim.fact_ids or set(validation.fact_ids) != set(claim.fact_ids):
        return "claim validation is not bound to immutable Source Facts"
    facts = {fact.fact_id: fact for fact in evidence.source_facts}
    artifacts = {
        (artifact.artifact_sha256, artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in evidence.source_artifacts
    }
    bound_facts = tuple(facts.get(fact_id) for fact_id in claim.fact_ids)
    if any(fact is None for fact in bound_facts):
        return "claim cites an unregistered Source Fact"
    calculation_ids: list[str] = []
    for fact in bound_facts:
        assert fact is not None
        if (
            fact.source_ref not in claim.source_refs
            or fact.raw_text != claim.source_quote
            or not fact.tool_call_id.strip()
            or not fact.tool_name.strip()
            or len(fact.artifact_sha256) != 64
            or fact.source_span_end - fact.source_span_start != len(fact.raw_text)
        ):
            return "claim Source Fact lacks an exact call, artifact, source, or span binding"
        artifact = artifacts.get(
            (fact.artifact_sha256, fact.tool_call_id, fact.source_ref)
        )
        if (
            artifact is None
            or artifact.tool_name != fact.tool_name
            or sha256(artifact.raw_text.encode()).hexdigest()
            != artifact.artifact_sha256
            or fact.source_span_end > len(artifact.raw_text)
            or artifact.raw_text[fact.source_span_start : fact.source_span_end]
            != fact.raw_text
        ):
            return (
                "claim Source Fact is not verifiable against its immutable "
                "Source Artifact"
            )
        calculation_ids.extend(fact.calculation_ids)
    if calculation_ids:
        from tradingagents.dataflows.market_snapshot import (
            _minimum_history_for_indicator,
        )

        required_rows = max(
            _minimum_history_for_indicator(calculation_id)
            for calculation_id in calculation_ids
        )
        if claim.minimum_history_rows < required_rows:
            return (
                "claim understates calculation history: "
                f"{claim.minimum_history_rows} declared; {required_rows} required"
            )
    return None


def decision_ready_material_claims(
    evidence: EvidenceState | Mapping[str, Any],
) -> tuple[MaterialClaim, ...]:
    """Return claims whose individual validation and cited sources permit use."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence)
    )
    sources = {source.source_id: source for source in current.sources}

    def source_is_ready(source_ref: str) -> bool:
        snapshot = current.market_snapshot
        if (
            snapshot is not None
            and snapshot.snapshot_id
            and source_ref.startswith("snapshot:")
            and source_ref != snapshot.snapshot_id
        ):
            return False
        source = sources.get(source_ref)
        return source is not None and source.status is EvidenceStatus.AVAILABLE

    return tuple(
        claim
        for claim in current.material_claims
        if _claim_provenance_failure(claim, current) is None
        and all(source_is_ready(source_ref) for source_ref in claim.source_refs)
    )


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


def _evidence_coverage(evidence: EvidenceState) -> float:
    snapshot = evidence.market_snapshot
    authoritative_snapshot_ids = (
        {
            snapshot.snapshot_id,
            f"snapshot:{snapshot.symbol}:{snapshot.effective_trading_date}",
        }
        if snapshot is not None
        else set()
    )
    authoritative_snapshot_ids.discard("")
    applicable_sources = tuple(
        source
        for source in evidence.sources
        if source.status is not EvidenceStatus.NOT_APPLICABLE
        and not (
            any(
                source.source_id == snapshot_id
                or source.source_id.startswith(f"{snapshot_id}:")
                for snapshot_id in authoritative_snapshot_ids
            )
        )
    )
    total_evidence = 2 + len(applicable_sources)
    available_evidence = int(evidence.instrument_identity is not None) + int(
        evidence.market_snapshot is not None
    )
    available_evidence += sum(
        source.status is EvidenceStatus.AVAILABLE for source in applicable_sources
    )
    return available_evidence / total_evidence


def evaluate_decision_gate(
    draft: DraftThesis,
    evidence: EvidenceState,
    *,
    original_draft: DraftThesis | None = None,
) -> DecisionGateResult:
    """Validate the material premises used by a directional draft thesis."""
    evidence_coverage = _evidence_coverage(evidence)
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
    unsupported_claims = tuple(
        (claims_by_id[claim_id], detail)
        for claim_id in draft.material_claim_ids
        for detail in (_claim_provenance_failure(claims_by_id[claim_id], evidence),)
        if detail is not None
    )
    if unsupported_claims:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} is unsupported"
                f" ({detail})."
                for claim, detail in unsupported_claims
            ),
            revision_applied=original_draft is not None,
        )
    sources_by_id = {source.source_id: source for source in evidence.sources}
    authoritative_snapshot_id = (
        evidence.market_snapshot.snapshot_id if evidence.market_snapshot else ""
    )
    snapshot_mismatches = tuple(
        (claim, source_ref)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        if authoritative_snapshot_id
        and source_ref.startswith("snapshot:")
        and source_ref != authoritative_snapshot_id
    )
    if snapshot_mismatches:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} cites snapshot {source_ref}, not "
                f"the Authoritative Market Snapshot {authoritative_snapshot_id}."
                for claim, source_ref in snapshot_mismatches
            ),
            revision_applied=original_draft is not None,
        )
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
        for source_text in (
            claims_by_id[claim_id].statement,
            claims_by_id[claim_id].source_quote,
        )
        for token in _numeric_claim_tokens(source_text)
    }
    numeric_narrative = draft.narrative
    for claim_id in draft.material_claim_ids:
        # The closed-ledger renderer annotates each premise as ``[claim_id]``.
        # Digits inside that immutable identifier are provenance, not a factual
        # assertion in the thesis, and must not be compared with source values.
        numeric_narrative = numeric_narrative.replace(f"[{claim_id}]", "")
    unsupported_numeric_claims = tuple(
        token
        for token in _numeric_claim_tokens(numeric_narrative)
        if not _numeric_token_supported(token, supported_numeric_claims)
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
            frame_sha256=snapshot.frame_sha256,
            snapshot_id=snapshot.snapshot_id,
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
            minimum_history_rows=1,
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
    minimum_history_rows: int = 1,
) -> AdmissionGateResult:
    """Evaluate whether acquired evidence may proceed to thesis synthesis."""
    coverage = _evidence_coverage(evidence)

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

    if (
        len(evidence.market_snapshot.frame_sha256) != 64
        or not evidence.market_snapshot.snapshot_id.startswith("snapshot:")
    ):
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Required evidence missing: immutable Authoritative Market "
                "Snapshot ID and frame digest.",
            ),
        )

    ready_claims = decision_ready_material_claims(evidence)
    required_history_rows = max(
        (claim.minimum_history_rows for claim in ready_claims),
        default=minimum_history_rows,
    )
    required_history_rows = max(required_history_rows, minimum_history_rows)
    if evidence.market_snapshot.history_rows < required_history_rows:
        calculations = tuple(
            claim.claim_id
            for claim in ready_claims
            if claim.minimum_history_rows == required_history_rows
            and claim.minimum_history_rows > minimum_history_rows
        )
        calculation_suffix = (
            f" by {', '.join(calculations)}" if calculations else ""
        )
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            coverage=coverage,
            diagnostics=(
                "Insufficient market history: "
                f"{evidence.market_snapshot.history_rows} rows available; "
                f"{required_history_rows} required{calculation_suffix}.",
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
