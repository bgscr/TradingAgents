"""Typed evidence shared by analysts and deterministic decision gates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Annotated, Any, Literal

from dateutil.relativedelta import relativedelta
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

EVIDENCE_CONTRACT_VERSION = "1.0"
ANALYSIS_OUTCOME_CONTRACT_VERSION = "2.0"
ACQUISITION_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_ACQUISITION_NAMESPACE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
)


def stable_acquisition_source_ref(namespace: str, *components: object) -> str:
    """Return a versioned opaque locator without retaining raw source metadata."""

    if _ACQUISITION_NAMESPACE_PATTERN.fullmatch(namespace) is None:
        raise ValueError("acquisition source-ref namespace is invalid")
    digest = sha256()
    for component in components:
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"acq.v1:{namespace}:{digest.hexdigest()}"

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


def _require_concrete_utc_timestamp(value: str) -> str:
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "retrieved_at must be a concrete ISO-8601 UTC timestamp"
        ) from exc
    if (
        instant.tzinfo is None
        or instant.utcoffset() != timezone.utc.utcoffset(instant)
    ):
        raise ValueError("retrieved_at must be a concrete ISO-8601 UTC timestamp")
    return value


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


class InstrumentKind(str, Enum):
    UNKNOWN = "unknown"
    EQUITY = "equity"
    FUND = "fund"
    INDEX = "index"
    BOND = "bond"
    CRYPTO = "crypto"


class EvidenceCapability(str, Enum):
    MARKET_SNAPSHOT = "market_snapshot"
    COMPANY_FINANCIALS = "company_financials"
    VALUATION = "valuation"
    CORPORATE_ACTIONS = "corporate_actions"
    BENCHMARK = "benchmark"
    NAV_PREMIUM = "nav_premium"
    HOLDINGS_EXPOSURE = "holdings_exposure"
    TRACKING_ERROR = "tracking_error"
    LIQUIDITY = "liquidity"


class IdentityProvenance(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    provider: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityProfile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    profile_id: str = Field(min_length=1)
    instrument_kind: InstrumentKind
    required_capabilities: tuple[EvidenceCapability, ...]
    optional_capabilities: tuple[EvidenceCapability, ...] = ()
    applicable_analysts: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_unique_disjoint_members(self) -> CapabilityProfile:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if len(set(self.optional_capabilities)) != len(self.optional_capabilities):
            raise ValueError("optional capabilities must be unique")
        if set(self.required_capabilities) & set(self.optional_capabilities):
            raise ValueError("required and optional capabilities must be disjoint")
        if len(set(self.applicable_analysts)) != len(self.applicable_analysts):
            raise ValueError("applicable analysts must be unique")
        if any(not analyst.strip() for analyst in self.applicable_analysts):
            raise ValueError("applicable analysts must be nonblank")
        return self

    @property
    def all_capabilities(self) -> frozenset[EvidenceCapability]:
        return frozenset((*self.required_capabilities, *self.optional_capabilities))


_CAPABILITY_PROFILES = {
    InstrumentKind.EQUITY: CapabilityProfile(
        profile_id="equity.v1",
        instrument_kind=InstrumentKind.EQUITY,
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
        optional_capabilities=(
            EvidenceCapability.COMPANY_FINANCIALS,
            EvidenceCapability.VALUATION,
            EvidenceCapability.CORPORATE_ACTIONS,
        ),
        applicable_analysts=("market", "social", "news", "fundamentals"),
    ),
    InstrumentKind.FUND: CapabilityProfile(
        profile_id="fund.v1",
        instrument_kind=InstrumentKind.FUND,
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
        optional_capabilities=(
            EvidenceCapability.BENCHMARK,
            EvidenceCapability.NAV_PREMIUM,
            EvidenceCapability.HOLDINGS_EXPOSURE,
            EvidenceCapability.TRACKING_ERROR,
            EvidenceCapability.LIQUIDITY,
        ),
        applicable_analysts=("market", "social", "news"),
    ),
}


def capability_profile_for(instrument_kind: InstrumentKind | str) -> CapabilityProfile:
    """Return the registered evidence capability profile for an instrument kind."""
    normalized = InstrumentKind(instrument_kind)
    try:
        return _CAPABILITY_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"no capability profile is registered for instrument kind {normalized.value!r}"
        ) from exc


class InstrumentIdentityEvidence(BaseModel):
    """Versioned identity with a compatibility path for legacy checkpoints.

    Legacy ``symbol + name`` payloads remain parseable, but ``is_authoritative``
    stays false until venue, kind, currency, and provenance are supplied by an
    authoritative registry. No ticker-shape inference is performed here.
    """

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    symbol: str
    venue: str = ""
    instrument_kind: InstrumentKind = InstrumentKind.UNKNOWN
    currency: str = ""
    provenance: IdentityProvenance | None = None
    display_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_name(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "name" not in value:
            return value
        migrated = dict(value)
        legacy_name = migrated.pop("name")
        migrated.setdefault("display_name", legacy_name)
        return migrated

    @property
    def name(self) -> str | None:
        """Deprecated compatibility alias for ``display_name``."""
        return self.display_name

    @property
    def is_authoritative(self) -> bool:
        return bool(
            self.symbol.strip()
            and self.venue.strip()
            and self.instrument_kind is not InstrumentKind.UNKNOWN
            and self.currency.strip()
            and self.provenance is not None
            and bool(self.provenance.artifact_sha256)
        )


class MarketSnapshotEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
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
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
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


class MissingValuePolicy(str, Enum):
    FAIL = "fail"
    DROP = "drop"
    FORWARD_FILL = "forward_fill"


class CalculationDefinition(BaseModel):
    """Registered deterministic semantics for one derived canonical field."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    calculation_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    version: str = Field(min_length=1)
    input_fields: tuple[str, ...] = Field(min_length=1)
    input_frequency: str = Field(min_length=1)
    minimum_history_rows: int = Field(ge=1)
    warmup_rows: int = Field(ge=0)
    adjustment_basis: str = Field(min_length=1)
    missing_value_policy: MissingValuePolicy
    formula: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    output_field: str = Field(min_length=1)
    output_unit: str = Field(min_length=1)
    precision: int = Field(ge=0)

    @property
    def required_observations(self) -> int:
        return max(self.minimum_history_rows, self.warmup_rows + 1)


class CalculationLineage(BaseModel):
    """Exact authoritative inputs and implementation used for a derived fact."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    calculation_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    calculation_version: str = Field(min_length=1)
    input_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_snapshot_id: str = Field(min_length=1)
    effective_range_start: str = Field(min_length=1)
    effective_range_end: str = Field(min_length=1)
    observations_used: int = Field(ge=1)
    adjustment_basis: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_calculation_lineage(
    definition: CalculationDefinition,
    lineage: CalculationLineage,
) -> None:
    """Fail closed when recorded lineage cannot represent the definition."""
    if lineage.calculation_id != definition.calculation_id:
        raise ValueError("calculation lineage references a different definition")
    if lineage.calculation_version != definition.version:
        raise ValueError("calculation lineage references a different definition version")
    if lineage.implementation_version != definition.implementation_version:
        raise ValueError("calculation lineage implementation version does not match")
    if lineage.adjustment_basis != definition.adjustment_basis:
        raise ValueError("calculation lineage adjustment basis does not match")
    if lineage.observations_used < definition.required_observations:
        raise ValueError(
            "calculation lineage has insufficient history: "
            f"{lineage.observations_used} < {definition.required_observations}"
        )
    try:
        range_start = datetime.fromisoformat(lineage.effective_range_start)
        range_end = datetime.fromisoformat(lineage.effective_range_end)
    except ValueError as exc:
        raise ValueError("calculation lineage effective range is not ISO-8601") from exc
    if range_start > range_end:
        raise ValueError("calculation lineage effective range is reversed")


def stable_source_fact_id(
    *,
    source_ref: str,
    artifact_sha256: str,
    source_span_start: int,
    source_span_end: int,
    canonical_field: str = "",
    instrument_symbol: str = "",
    effective_date: str = "",
) -> str:
    """Derive a fact ID from source-bound semantics, never runtime/model IDs."""
    canonical_identity = "\0".join(
        (
            source_ref,
            artifact_sha256,
            str(source_span_start),
            str(source_span_end),
            canonical_field,
            instrument_symbol,
            effective_date,
        )
    )
    return f"fact:{sha256(canonical_identity.encode()).hexdigest()}"


class SourceFact(BaseModel):
    """Canonical observation bound to, but separate from, a raw source artifact.

    ``raw_text`` is retained as the legacy name for the exact cited excerpt; the
    complete provider payload exists only on :class:`SourceArtifact`.
    """

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    fact_kind: Literal["excerpt", "canonical"] = "excerpt"
    fact_id: str = Field(pattern=r"^fact:[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, pattern=r".*\S.*")
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(min_length=1, pattern=r".*\S.*")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_text: str
    source_span_start: int = Field(ge=0)
    source_span_end: int = Field(ge=0)
    normalized_numeric_tokens: tuple[str, ...] = ()
    calculation_ids: tuple[str, ...] = ()
    canonical_field: str = ""
    normalized_value: Decimal | str | int | bool | None = None
    unit: str = ""
    instrument_symbol: str = ""
    effective_date: str = ""
    calculation_lineage: CalculationLineage | None = None

    @model_validator(mode="after")
    def _validate_semantics(self) -> SourceFact:
        if not self.raw_text.strip():
            raise ValueError("Source Fact raw_text must be a nonblank exact excerpt")
        if (
            self.source_span_end <= self.source_span_start
            or self.source_span_end - self.source_span_start != len(self.raw_text)
        ):
            raise ValueError("Source Fact source span must match its exact excerpt")
        if self.fact_kind == "canonical":
            required_text = {
                "canonical_field": self.canonical_field,
                "unit": self.unit,
                "instrument_symbol": self.instrument_symbol,
                "effective_date": self.effective_date,
            }
            missing = tuple(name for name, value in required_text.items() if not value.strip())
            if missing:
                raise ValueError(
                    "canonical Source Fact requires nonblank " + ", ".join(missing)
                )
            if self.normalized_value is None:
                raise ValueError("canonical Source Fact requires normalized_value")
            try:
                datetime.fromisoformat(self.effective_date)
            except ValueError as exc:
                raise ValueError(
                    "canonical Source Fact effective_date must be ISO-8601"
                ) from exc
        return self


class SourceArtifact(BaseModel):
    """Exact immutable tool result whose digest is cited by Source Facts."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, pattern=r".*\S.*")
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(min_length=1, pattern=r".*\S.*")
    raw_text: str = Field(min_length=1, pattern=r".*\S.*")

    @model_validator(mode="after")
    def _validate_content_digest(self) -> SourceArtifact:
        expected = sha256(self.raw_text.encode("utf-8")).hexdigest()
        if self.artifact_sha256 != expected:
            raise ValueError("artifact_sha256 does not match exact UTF-8 raw_text")
        return self


class AcquisitionUnavailableReason(str, Enum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NOT_CONFIGURED = "not_configured"
    NO_DATA = "no_data"
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    INSUFFICIENT_HISTORY = "insufficient_history"
    PROVIDER_ERROR = "provider_error"
    REGISTRY_NOT_CONFIGURED = "registry_not_configured"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    IDENTITY_NOT_FOUND = "identity_not_found"
    INTEGRITY_FAILURE = "integrity_failure"
    CIRCUIT_OPEN = "circuit_open"


class SourceAcquisitionAvailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    outcome: Literal["available"] = "available"
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(default=0, ge=0)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    attempt: int = Field(ge=1)
    retrieved_at: str = Field(min_length=1)
    retryable: Literal[False] = False
    artifact: SourceArtifact

    @field_validator("retrieved_at")
    @classmethod
    def _validate_retrieved_at(cls, value: str) -> str:
        return _require_concrete_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_artifact_binding(self) -> SourceAcquisitionAvailable:
        if self.artifact.source_ref != self.source_ref:
            raise ValueError("available artifact source_ref does not match outcome")
        return self


class CalculationReadinessDiagnostic(BaseModel):
    """Closed, payload-free metadata for a rejected deterministic calculation."""

    model_config = _CLOSED_MODEL_CONFIG

    calculation_id: str = Field(min_length=1)
    required_observations: int = Field(ge=0)
    available_observations: int = Field(ge=0)
    input_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceAcquisitionUnavailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    outcome: Literal["unavailable"] = "unavailable"
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(default=0, ge=0)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    attempt: int = Field(ge=1)
    retrieved_at: str = Field(min_length=1)
    retryable: bool
    reason: AcquisitionUnavailableReason
    retry_after_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    calculation_readiness: CalculationReadinessDiagnostic | None = None

    @field_validator("retrieved_at")
    @classmethod
    def _validate_retrieved_at(cls, value: str) -> str:
        return _require_concrete_utc_timestamp(value)


SourceAcquisitionOutcome = Annotated[
    SourceAcquisitionAvailable | SourceAcquisitionUnavailable,
    Field(discriminator="outcome"),
]


def _canonicalize_source_acquisition_outcomes(
    outcomes: Iterable[SourceAcquisitionOutcome],
) -> tuple[SourceAcquisitionOutcome, ...]:
    by_attempt: dict[tuple[str, str, str, int], SourceAcquisitionOutcome] = {}
    for outcome in outcomes:
        identity = (
            outcome.provider,
            outcome.capability,
            outcome.source_ref,
            outcome.attempt,
        )
        existing = by_attempt.get(identity)
        if existing is not None and existing != outcome:
            raise ValueError(
                "source acquisition attempt was redefined for "
                f"{outcome.provider}:{outcome.capability}:{outcome.attempt}"
            )
        by_attempt[identity] = outcome
    return tuple(
        sorted(
            by_attempt.values(),
            key=lambda outcome: (
                outcome.capability,
                outcome.source_ref,
                outcome.provider_order,
                outcome.attempt,
                outcome.provider,
                outcome.retrieved_at,
                outcome.outcome,
            ),
        )
    )


class ToolExecutionEvidenceEnvelope(BaseModel):
    """Checkpoint-safe provenance emitted alongside one exact tool result."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = Field(min_length=1)
    selected_artifact: SourceArtifact | None = None

    @model_validator(mode="after")
    def _validate_local_bindings(self) -> ToolExecutionEvidenceEnvelope:
        available = tuple(
            outcome
            for outcome in self.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        )
        for outcome in self.acquisition_outcomes:
            if outcome.source_ref != self.source_ref:
                raise ValueError("acquisition outcome source_ref does not match envelope")
            if outcome.capability != self.capability:
                raise ValueError("acquisition outcome capability does not match envelope")
        if len(available) > 1:
            raise ValueError("envelope may select at most one available acquisition outcome")
        if not available:
            if self.selected_artifact is not None:
                raise ValueError("unavailable-only envelope cannot expose an artifact")
            return self
        if self.selected_artifact is None:
            raise ValueError("available envelope must expose its selected artifact")
        if available[0].artifact != self.selected_artifact:
            raise ValueError("selected artifact does not match available acquisition outcome")
        artifact = self.selected_artifact
        if artifact.tool_call_id != self.tool_call_id:
            raise ValueError("selected artifact tool_call_id does not match envelope")
        if artifact.tool_name != self.tool_name:
            raise ValueError("selected artifact tool_name does not match envelope")
        if artifact.source_ref != self.source_ref:
            raise ValueError("selected artifact source_ref does not match envelope")
        return self


def _unavailable_reason_for_text(content: str) -> AcquisitionUnavailableReason:
    normalized = content.casefold()
    if (
        "too many requests" in normalized
        or "rate limit" in normalized
        or re.search(r"\b(?:http\s*)?429\b", normalized)
    ):
        return AcquisitionUnavailableReason.RATE_LIMITED
    if (
        "timeout" in normalized
        or "timed out" in normalized
        or re.search(r"\b(?:http\s*)?408\b", normalized)
    ):
        return AcquisitionUnavailableReason.TIMEOUT
    if "not configured" in normalized or "missing configuration" in normalized:
        return AcquisitionUnavailableReason.NOT_CONFIGURED
    if (
        "unauthorized" in normalized
        or "authentication" in normalized
        or "forbidden" in normalized
        or re.search(r"\b(?:http\s*)?(?:401|403)\b", normalized)
    ):
        return AcquisitionUnavailableReason.AUTHENTICATION
    if "malformed" in normalized or "invalid response" in normalized:
        return AcquisitionUnavailableReason.MALFORMED_RESPONSE
    if "no data" in normalized or "unavailable" in normalized or "not_available" in normalized:
        return AcquisitionUnavailableReason.NO_DATA
    return AcquisitionUnavailableReason.PROVIDER_ERROR


def make_source_acquisition_outcome(
    *,
    provider: str,
    capability: str,
    attempt: int,
    retrieved_at: str,
    source_ref: str,
    tool_call_id: str,
    tool_name: str,
    content: str,
    status: str,
    retry_after_seconds: float | None = None,
) -> SourceAcquisitionOutcome:
    """Convert a provider attempt into evidence or a diagnostic, never both."""
    normalized_status = status.strip().casefold()
    availability_content = "\n".join(
        line
        for line in content.splitlines()
        if not line.lstrip().casefold().startswith("data_degraded:")
    )
    unavailable_marker = _source_quote_is_unavailable(availability_content) or any(
        marker in availability_content.casefold()
        for marker in (
            "too many requests",
            "rate limit",
            "data_unavailable",
            "timed out",
            "timeout",
            "unauthorized",
            "forbidden",
            "not configured",
            "missing configuration",
            "invalid response",
            "malformed response",
        )
    )
    unavailable_marker = unavailable_marker or bool(
        re.search(
            r"\b(?:http\s*)?(?:401|403|408|429|5\d{2})\b",
            availability_content,
            re.IGNORECASE,
        )
    )
    if normalized_status in {"error", "failed", "unavailable"} or unavailable_marker:
        reason = _unavailable_reason_for_text(content)
        status_match = re.search(r"\b([1-5]\d{2})\b", content)
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=capability,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            retryable=reason
            in {
                AcquisitionUnavailableReason.RATE_LIMITED,
                AcquisitionUnavailableReason.TIMEOUT,
                AcquisitionUnavailableReason.PROVIDER_ERROR,
            },
            reason=reason,
            retry_after_seconds=retry_after_seconds,
            http_status=int(status_match.group(1)) if status_match else None,
        )
    artifact_digest = sha256(content.encode()).hexdigest()
    return SourceAcquisitionAvailable(
        provider=provider,
        capability=capability,
        source_ref=source_ref,
        attempt=attempt,
        retrieved_at=retrieved_at,
        artifact=SourceArtifact(
            artifact_sha256=artifact_digest,
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw_text=content,
        ),
    )


def calculation_readiness_outcome(
    definition: CalculationDefinition,
    *,
    observations_available: int,
    adjustment_basis: str,
    input_artifact_sha256: str,
    provider: str,
    attempt: int,
    retrieved_at: str,
) -> SourceAcquisitionUnavailable | None:
    """Return a typed diagnostic instead of computing with invalid inputs."""
    readiness = CalculationReadinessDiagnostic(
        calculation_id=definition.calculation_id,
        required_observations=definition.required_observations,
        available_observations=observations_available,
        input_artifact_sha256=input_artifact_sha256,
    )
    source_ref = stable_acquisition_source_ref(
        "calculation",
        definition.calculation_id,
        definition.version,
    )
    if observations_available < definition.required_observations:
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=definition.output_field,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            # A calculation for a fixed historical snapshot cannot gain rows by
            # retrying the same acquisition within the run.
            retryable=False,
            reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY,
            calculation_readiness=readiness,
        )
    if adjustment_basis != definition.adjustment_basis:
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=definition.output_field,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            retryable=False,
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            calculation_readiness=readiness,
        )
    return None


class ClaimValidation(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    claim_id: str
    status: ClaimValidationStatus
    detail: str = ""
    fact_ids: tuple[str, ...] = ()


class AnalystEvidenceReport(BaseModel):
    """Typed final payload emitted by a tool-calling analyst."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    report_markdown: str
    material_claims: tuple[SubmittedMaterialClaim, ...] = ()


class EvidenceSource(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    source_id: str = Field(min_length=1, pattern=r".*\S.*")
    status: EvidenceStatus
    required: bool
    detail: str = ""


def _canonicalize_source_facts(
    facts: Iterable[SourceFact],
) -> tuple[SourceFact, ...]:
    first_seen: dict[str, SourceFact] = {}
    canonical: list[SourceFact] = []
    for fact in facts:
        existing = first_seen.get(fact.fact_id)
        if existing is None:
            first_seen[fact.fact_id] = fact
            canonical.append(fact)
        elif existing.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ) != fact.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ):
            raise ValueError(f"Source fact ID {fact.fact_id!r} was redefined.")
    return tuple(canonical)


class EvidenceState(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    instrument_identity: InstrumentIdentityEvidence | None = None
    market_snapshot: MarketSnapshotEvidence | None = None
    material_claims: tuple[MaterialClaim, ...] = ()
    source_facts: tuple[SourceFact, ...] = ()
    source_artifacts: tuple[SourceArtifact, ...] = ()
    claim_validations: tuple[ClaimValidation, ...] = ()
    sources: tuple[EvidenceSource, ...] = ()
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = ()

    @model_validator(mode="after")
    def _canonicalize_source_fact_collection(self) -> EvidenceState:
        canonical_facts = _canonicalize_source_facts(self.source_facts)
        if canonical_facts != self.source_facts:
            object.__setattr__(self, "source_facts", canonical_facts)
        return self

    @model_validator(mode="after")
    def _canonicalize_acquisition_outcomes(self) -> EvidenceState:
        canonical = _canonicalize_source_acquisition_outcomes(
            self.acquisition_outcomes
        )
        if canonical == self.acquisition_outcomes:
            return self
        return self.model_copy(update={"acquisition_outcomes": canonical})


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
    canonical = _canonicalize_source_facts((*current.source_facts, *facts))
    return current.model_copy(update={"source_facts": canonical})


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


def merge_source_acquisition_outcomes(
    evidence: EvidenceState | Mapping[str, Any] | None,
    outcomes: Iterable[SourceAcquisitionOutcome],
) -> EvidenceState:
    """Merge attempt outcomes idempotently without turning failures into evidence."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )

    canonical = _canonicalize_source_acquisition_outcomes(
        (*current.acquisition_outcomes, *outcomes)
    )
    return current.model_copy(update={"acquisition_outcomes": canonical})


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

    sources: list[EvidenceSource] = []
    acquisition_outcomes: list[SourceAcquisitionOutcome] = []
    available_messages_by_source: dict[str, tuple[Any, ...]] = {}
    selected_artifacts_by_source_call: dict[tuple[str, str], SourceArtifact] = {}
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
            frozenset({"get_verified_market_snapshot", "get_indicators"})
            if source_kind == "snapshot"
            else frozenset({source_kind})
        )

        def message_matches_source(
            message: Any,
            bound_tool_names: frozenset[str] = allowed_tool_names,
            bound_source_ref: str = source_ref,
        ) -> bool:
            message_tool_name = str(getattr(message, "name", ""))
            if message_tool_name in bound_tool_names:
                return True
            try:
                envelope = ToolExecutionEvidenceEnvelope.model_validate(
                    getattr(message, "artifact", None)
                )
            except ValidationError:
                return False
            return (
                envelope.source_ref == bound_source_ref
                and envelope.tool_call_id
                == str(getattr(message, "tool_call_id", ""))
                and envelope.tool_name == message_tool_name
            )

        matching_messages = tuple(
            message
            for tool_call_id in tool_call_ids_by_source[source_ref]
            for message in tool_messages.get(str(tool_call_id), ())
            if message_matches_source(message)
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
        envelopes: list[tuple[Any, ToolExecutionEvidenceEnvelope]] = []
        envelopes_by_call_id: dict[str, ToolExecutionEvidenceEnvelope] = {}
        envelope_error = False
        for message in sorted(
            matching_messages,
            key=lambda item: str(getattr(item, "tool_call_id", "")),
        ):
            try:
                envelope = ToolExecutionEvidenceEnvelope.model_validate(
                    getattr(message, "artifact", None)
                )
            except ValidationError:
                envelope_error = True
                break
            message_tool_call_id = str(getattr(message, "tool_call_id", ""))
            message_tool_name = str(getattr(message, "name", ""))
            expected_capability = (
                envelope.capability
                if source_ref.startswith("acq.v1:")
                else (
                    EvidenceCapability.MARKET_SNAPSHOT.value
                    if source_kind == "snapshot"
                    and message_tool_name == "get_verified_market_snapshot"
                    else (
                        message_tool_name if source_kind == "snapshot" else source_kind
                    )
                )
            )
            selected = envelope.selected_artifact
            previous = envelopes_by_call_id.get(message_tool_call_id)
            if (
                (previous is not None and previous != envelope)
                or envelope.tool_call_id != message_tool_call_id
                or envelope.tool_name != message_tool_name
                or envelope.source_ref != source_ref
                or envelope.capability != expected_capability
                or (
                    selected is not None
                    and selected.raw_text != str(getattr(message, "content", ""))
                )
            ):
                envelope_error = True
                break
            envelopes_by_call_id[message_tool_call_id] = envelope
            envelopes.append((message, envelope))
        if envelope_error or not envelopes:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="trusted acquisition metadata not exposed",
                )
            )
            continue
        attempt_outcomes = tuple(
            outcome
            for _, envelope in envelopes
            for outcome in envelope.acquisition_outcomes
        )
        acquisition_outcomes.extend(attempt_outcomes)
        available_messages = tuple(
            message
            for message, envelope in envelopes
            if envelope.selected_artifact is not None
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
        selected_artifacts_by_source_call.update(
            {
                (source_ref, envelope.tool_call_id): envelope.selected_artifact
                for _, envelope in envelopes
                if envelope.selected_artifact is not None
            }
        )
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
                artifact = selected_artifacts_by_source_call[(source_ref, tool_call_id)]
                artifact_digest = artifact.artifact_sha256
                artifact_key = (artifact_digest, tool_call_id, source_ref)
                artifacts[artifact_key] = artifact
                fact_id = stable_source_fact_id(
                    source_ref=source_ref,
                    artifact_sha256=artifact_digest,
                    source_span_start=span_start,
                    source_span_end=span_start + len(quote),
                )
                claim_facts.append(
                    SourceFact(
                        fact_id=fact_id,
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
        acquisition_outcomes=tuple(acquisition_outcomes),
    )


def build_inline_evidence_state(
    source_text_by_ref: Mapping[str, str | None],
    claims: Iterable[MaterialClaim],
    *,
    source_artifact_by_ref: Mapping[str, SourceArtifact] | None = None,
) -> EvidenceState:
    """Validate claims against pre-fetched source blocks kept in the prompt.

    ``source_artifact_by_ref`` binds a prompt-facing source ref to the exact
    acquired artifact that supplied its text, without minting alias provenance.
    """
    claim_list = tuple(claims)
    bound_artifacts = dict(source_artifact_by_ref or {})
    normalized_sources: dict[str, str | None] = {}
    acquisition_outcomes: list[SourceAcquisitionOutcome] = []
    for source_ref, text in source_text_by_ref.items():
        bound_artifact = bound_artifacts.get(source_ref)
        if bound_artifact is not None:
            normalized_sources[source_ref] = bound_artifact.raw_text
            continue
        content = text if text is not None else ""
        outcome = make_source_acquisition_outcome(
            provider=source_ref.partition(".")[2] or "inline",
            capability=source_ref,
            attempt=1,
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            source_ref=source_ref,
            tool_call_id=f"inline:{source_ref}",
            tool_name="inline_source",
            content=content,
            status="success" if content.strip() else "unavailable",
        )
        acquisition_outcomes.append(outcome)
        normalized_sources[source_ref] = (
            outcome.artifact.raw_text
            if isinstance(outcome, SourceAcquisitionAvailable)
            else None
        )
    for source_ref, artifact in bound_artifacts.items():
        normalized_sources.setdefault(source_ref, artifact.raw_text)
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
    artifacts: dict[tuple[str, str, str], SourceArtifact] = {
        (artifact.artifact_sha256, artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in bound_artifacts.values()
    }
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
            bound_artifact = bound_artifacts.get(source_ref)
            if bound_artifact is None:
                artifact_digest = sha256(content.encode()).hexdigest()
                artifact_source_ref = source_ref
                tool_call_id = f"inline:{source_ref}"
                tool_name = "inline_source"
                artifact_key = (artifact_digest, tool_call_id, artifact_source_ref)
                artifacts[artifact_key] = SourceArtifact(
                    artifact_sha256=artifact_digest,
                    source_ref=artifact_source_ref,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    raw_text=content,
                )
            else:
                artifact_digest = bound_artifact.artifact_sha256
                artifact_source_ref = bound_artifact.source_ref
                tool_call_id = bound_artifact.tool_call_id
                tool_name = bound_artifact.tool_name
            fact = SourceFact(
                fact_id=stable_source_fact_id(
                    source_ref=artifact_source_ref,
                    artifact_sha256=artifact_digest,
                    source_span_start=span_start,
                    source_span_end=span_start + len(quote),
                ),
                source_ref=artifact_source_ref,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
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
        acquisition_outcomes=tuple(acquisition_outcomes),
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
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    admitted: bool
    readiness: EvidenceReadiness
    diagnostics: tuple[str, ...] = ()


class EvidencePreflightResult(BaseModel):
    """Baseline evidence validation before any model-mediated analysis."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    passed: bool
    readiness: EvidenceReadiness
    blockers: tuple[str, ...] = ()


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
    identity: Mapping[str, Any],
    snapshot: Any | None,
) -> EvidenceState:
    """Convert acquired identity and snapshot data into checkpoint-safe evidence."""
    name = identity.get("company_name") or identity.get("name")
    raw_kind = str(
        identity.get("instrument_kind") or identity.get("quote_type") or "unknown"
    ).strip().casefold()
    kind_aliases = {
        "stock": InstrumentKind.EQUITY,
        "equity": InstrumentKind.EQUITY,
        "etf": InstrumentKind.FUND,
        "fund": InstrumentKind.FUND,
        "mutualfund": InstrumentKind.FUND,
        "index": InstrumentKind.INDEX,
        "bond": InstrumentKind.BOND,
        "cryptocurrency": InstrumentKind.CRYPTO,
        "crypto": InstrumentKind.CRYPTO,
    }
    provenance_value = identity.get("provenance")
    instrument_identity = None
    if identity:
        instrument_identity = InstrumentIdentityEvidence(
            symbol=str(identity.get("canonical_symbol") or symbol),
            venue=str(identity.get("venue") or identity.get("exchange") or ""),
            instrument_kind=kind_aliases.get(raw_kind, InstrumentKind.UNKNOWN),
            currency=str(identity.get("currency") or ""),
            provenance=provenance_value,
            display_name=str(name) if name else None,
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
    from tradingagents.dataflows.errors import NoMarketDataError
    from tradingagents.dataflows.instrument_identity import (
        IdentityRegistryAvailable,
        RegistryFailureReason,
        resolve_authoritative_instrument_identity,
    )
    from tradingagents.dataflows.market_snapshot import (
        get_active_market_snapshot_acquisition_record,
        get_authoritative_market_snapshot,
    )

    requested = datetime.strptime(requested_date, "%Y-%m-%d")
    start_date = (requested - relativedelta(years=5)).strftime("%Y-%m-%d")
    registry_result = resolve_authoritative_instrument_identity(symbol)
    identity: dict[str, Any] = {}
    acquired_at = datetime.now(timezone.utc).isoformat()

    if isinstance(registry_result, IdentityRegistryAvailable):
        canonical_symbol = registry_result.identity.canonical_symbol
        identity = registry_result.identity.as_mapping()
        identity["exchange"] = registry_result.identity.venue
        try:
            snapshot = get_authoritative_market_snapshot(
                canonical_symbol,
                start_date,
                requested_date,
                minimum_history_rows=1,
            )
        except NoMarketDataError:
            snapshot = None
        snapshot_record = get_active_market_snapshot_acquisition_record(
            canonical_symbol, requested_date
        )
        identity_source_ref = stable_acquisition_source_ref(
            "identity-registry",
            registry_result.registry_source_ref,
            registry_result.registry_sha256,
        )
        artifact = SourceArtifact(
            artifact_sha256=registry_result.registry_sha256,
            source_ref=identity_source_ref,
            tool_call_id="identity-registry",
            tool_name="instrument_identity_registry",
            raw_text=registry_result.raw_artifact,
        )
        identity_outcome: SourceAcquisitionOutcome = SourceAcquisitionAvailable(
            provider="instrument-identity-registry",
            capability="instrument_identity",
            source_ref=identity_source_ref,
            attempt=1,
            retrieved_at=acquired_at,
            artifact=artifact,
        )
        market_artifact = (
            snapshot_record.source_artifact
            if snapshot_record is not None
            else getattr(snapshot, "source_artifact", None)
        )
        artifacts = (artifact,) + (
            (market_artifact,) if market_artifact is not None else ()
        )
        market_outcomes = (
            snapshot_record.outcomes
            if snapshot_record is not None
            else tuple(getattr(snapshot, "acquisition_outcomes", ()))
        )
    else:
        # Identity failure is deterministic for this registry configuration.
        # Do not spend market-data calls on a run that preflight must reject.
        snapshot = None
        reason_map = {
            RegistryFailureReason.NOT_CONFIGURED: (
                AcquisitionUnavailableReason.REGISTRY_NOT_CONFIGURED
            ),
            RegistryFailureReason.NOT_FOUND: AcquisitionUnavailableReason.IDENTITY_NOT_FOUND,
            RegistryFailureReason.MALFORMED: (
                AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ),
            RegistryFailureReason.INTEGRITY_FAILURE: (
                AcquisitionUnavailableReason.INTEGRITY_FAILURE
            ),
            RegistryFailureReason.UNAVAILABLE: (
                AcquisitionUnavailableReason.REGISTRY_UNAVAILABLE
            ),
        }
        identity_outcome = SourceAcquisitionUnavailable(
            provider="instrument-identity-registry",
            capability="instrument_identity",
            source_ref=stable_acquisition_source_ref(
                "identity-registry",
                registry_result.source_ref,
                registry_result.reason.value,
            ),
            attempt=1,
            retrieved_at=acquired_at,
            retryable=False,
            reason=reason_map[registry_result.reason],
        )
        artifacts = ()
        market_outcomes = ()

    state = build_evidence_state(
        symbol=symbol,
        identity=identity,
        snapshot=snapshot,
    )
    return state.model_copy(
        update={
            "source_artifacts": artifacts,
            "acquisition_outcomes": (identity_outcome, *market_outcomes),
        }
    )


class AnalysisOutcomeReason(str, Enum):
    PREFLIGHT_BLOCKED = "preflight_blocked"
    ADMISSION_BLOCKED = "admission_blocked"
    DECISION_GATE_BLOCKED = "decision_gate_blocked"
    PORTFOLIO_GATE_BLOCKED = "portfolio_gate_blocked"
    SHADOW_MODE_BLOCKED = "shadow_mode_blocked"


class AnalysisDiagnosticCode(str, Enum):
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    SNAPSHOT_INVALID = "snapshot_invalid"
    HISTORY_INSUFFICIENT = "history_insufficient"
    REQUIRED_EVIDENCE_UNAVAILABLE = "required_evidence_unavailable"
    REQUIRED_EVIDENCE_CONFLICTED = "required_evidence_conflicted"
    DECISION_CONFIGURATION_INVALID = "decision_configuration_invalid"
    STRATEGY_RULE_INVALID = "strategy_rule_invalid"
    DIRECTION_SELECTION_INVALID = "direction_selection_invalid"
    DECISION_ASSERTION_INVALID = "decision_assertion_invalid"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    SHADOW_MODE = "shadow_mode"
    DETERMINISTIC_GATE_REJECTED = "deterministic_gate_rejected"


class AnalysisOutcome(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["2.0"] = ANALYSIS_OUTCOME_CONTRACT_VERSION
    readiness: EvidenceReadiness
    reason: AnalysisOutcomeReason
    diagnostic_codes: tuple[AnalysisDiagnosticCode, ...] = ()

    @field_validator("diagnostic_codes")
    @classmethod
    def _canonicalize_diagnostic_codes(
        cls,
        value: tuple[AnalysisDiagnosticCode, ...],
    ) -> tuple[AnalysisDiagnosticCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))


_ANALYSIS_OUTCOME_SUMMARIES = {
    AnalysisOutcomeReason.PREFLIGHT_BLOCKED: (
        "Analysis stopped before model-mediated work because required baseline "
        "evidence or deterministic decision configuration was not decision-ready."
    ),
    AnalysisOutcomeReason.ADMISSION_BLOCKED: (
        "Analysis stopped before thesis synthesis because required evidence and "
        "rule-backed assertions were not decision-ready."
    ),
    AnalysisOutcomeReason.DECISION_GATE_BLOCKED: (
        "Analysis completed without a Trading Decision because the final direction "
        "proposal did not satisfy the deterministic Decision Gate."
    ),
    AnalysisOutcomeReason.PORTFOLIO_GATE_BLOCKED: (
        "Analysis completed without a Trading Decision because the Portfolio Manager "
        "could not produce a decision-ready evidence chain."
    ),
    AnalysisOutcomeReason.SHADOW_MODE_BLOCKED: (
        "Analysis completed without a Trading Decision because shadow evidence mode "
        "is diagnostic-only."
    ),
}

_ANALYSIS_DIAGNOSTIC_LABELS = {
    AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE: (
        "Authoritative instrument identity was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE: (
        "The authoritative market snapshot was unavailable."
    ),
    AnalysisDiagnosticCode.SNAPSHOT_INVALID: (
        "The authoritative market snapshot failed deterministic validation."
    ),
    AnalysisDiagnosticCode.HISTORY_INSUFFICIENT: (
        "The authoritative snapshot did not contain enough usable history."
    ),
    AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE: (
        "A required evidence capability was unavailable or not applicable."
    ),
    AnalysisDiagnosticCode.REQUIRED_EVIDENCE_CONFLICTED: (
        "Required evidence contained an unresolved material conflict."
    ),
    AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID: (
        "Deterministic decision configuration was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.STRATEGY_RULE_INVALID: (
        "No valid registered Strategy Rule application could authorize direction."
    ),
    AnalysisDiagnosticCode.DIRECTION_SELECTION_INVALID: (
        "The direction selection was unavailable or incompatible with its context."
    ),
    AnalysisDiagnosticCode.DECISION_ASSERTION_INVALID: (
        "A decision assertion failed deterministic validation."
    ),
    AnalysisDiagnosticCode.STRUCTURED_OUTPUT_INVALID: (
        "Required structured portfolio output was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.SHADOW_MODE: (
        "Shadow evidence mode cannot publish a directional decision."
    ),
    AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED: (
        "A deterministic trust-boundary check rejected publication."
    ),
}


def analysis_diagnostic_codes(
    diagnostics: Iterable[str],
) -> tuple[AnalysisDiagnosticCode, ...]:
    """Collapse internal blocker details into a closed, publication-safe taxonomy."""

    codes: set[AnalysisDiagnosticCode] = set()
    for diagnostic in diagnostics:
        normalized = " ".join(str(diagnostic).replace("_", " ").casefold().split())
        if "shadow" in normalized:
            code = AnalysisDiagnosticCode.SHADOW_MODE
        elif "identity" in normalized:
            code = AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE
        elif "history" in normalized or " rows" in normalized:
            code = AnalysisDiagnosticCode.HISTORY_INSUFFICIENT
        elif "snapshot" in normalized and any(
            marker in normalized for marker in ("missing", "unavailable")
        ):
            code = AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE
        elif "snapshot" in normalized:
            code = AnalysisDiagnosticCode.SNAPSHOT_INVALID
        elif "conflict" in normalized:
            code = AnalysisDiagnosticCode.REQUIRED_EVIDENCE_CONFLICTED
        elif "required evidence" in normalized or "required capability" in normalized:
            code = AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE
        elif any(
            marker in normalized
            for marker in (
                "configuration",
                "not configured",
                "registry",
                "calculation",
                "resolver",
                "evaluator",
            )
        ):
            code = AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID
        elif "strategy rule" in normalized:
            code = AnalysisDiagnosticCode.STRATEGY_RULE_INVALID
        elif "direction" in normalized or "rating" in normalized:
            code = AnalysisDiagnosticCode.DIRECTION_SELECTION_INVALID
        elif "assertion" in normalized:
            code = AnalysisDiagnosticCode.DECISION_ASSERTION_INVALID
        elif any(
            marker in normalized
            for marker in ("structured", "portfolio manager", "revision", "selection")
        ):
            code = AnalysisDiagnosticCode.STRUCTURED_OUTPUT_INVALID
        else:
            code = AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED
        codes.add(code)
    return tuple(sorted(codes, key=lambda item: item.value))


def render_analysis_outcome(outcome: AnalysisOutcome) -> str:
    """Render a non-directional outcome without portfolio recommendation fields."""
    labels = {
        EvidenceReadiness.INSUFFICIENT: "Insufficient Evidence",
        EvidenceReadiness.CONFLICTED: "Conflicted Evidence",
    }
    lines = [
        f"**Analysis Outcome:** {labels[outcome.readiness]}",
        "",
        f"**Reason Code:** `{outcome.reason.value}`",
        "",
        _ANALYSIS_OUTCOME_SUMMARIES[outcome.reason],
        "",
        "No Trading Decision was issued.",
    ]
    if outcome.diagnostic_codes:
        lines.extend(["", "### Deterministic Blockers", ""])
        lines.extend(
            f"- `{code.value}`: {_ANALYSIS_DIAGNOSTIC_LABELS[code]}"
            for code in sorted(outcome.diagnostic_codes, key=lambda item: item.value)
        )
    return "\n".join(lines)


def analysis_outcome_publication(outcome: AnalysisOutcome) -> dict[str, Any]:
    """Publish the typed outcome and its sole deterministic prose rendering."""

    return {
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
    }


def evaluate_preflight_gate(
    evidence: EvidenceState | Mapping[str, Any] | None,
    *,
    minimum_history_rows: int = 1,
) -> EvidencePreflightResult:
    """Validate authoritative baseline evidence without requiring analyst output."""
    if minimum_history_rows < 1:
        raise ValueError("minimum_history_rows must be at least 1")
    state = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    blockers: list[str] = []
    identity = state.instrument_identity
    snapshot = state.market_snapshot
    profile = None

    if identity is None:
        blockers.append("authoritative instrument identity is missing")
    elif not identity.is_authoritative:
        blockers.append(
            "instrument identity requires canonical symbol, venue, kind, currency, "
            "and provenance"
        )
    else:
        try:
            profile = capability_profile_for(identity.instrument_kind)
        except ValueError as exc:
            blockers.append(str(exc))

    if profile is not None:
        unsupported_required = set(profile.required_capabilities) - {
            EvidenceCapability.MARKET_SNAPSHOT
        }
        blockers.extend(
            "unsupported required capability: " + capability.value
            for capability in sorted(unsupported_required, key=lambda item: item.value)
        )

    if snapshot is None:
        blockers.append("authoritative market snapshot is missing")
    else:
        if identity is not None and snapshot.symbol != identity.symbol:
            blockers.append("instrument identity and market snapshot symbols differ")
        unknown_adjustment = snapshot.adjustment_basis.strip().casefold() in {
            "",
            "unknown",
            "none",
            "n/a",
        }
        if unknown_adjustment:
            blockers.append("authoritative market snapshot adjustment basis is unknown")
        try:
            requested_date = datetime.fromisoformat(snapshot.requested_date)
            effective_date = datetime.fromisoformat(snapshot.effective_trading_date)
        except ValueError:
            blockers.append("authoritative market snapshot dates must be ISO-8601")
        else:
            if effective_date > requested_date:
                blockers.append(
                    "effective trading date cannot be later than the requested date"
                )
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot.frame_sha256):
            blockers.append("authoritative market snapshot frame digest is invalid")
        if not snapshot.snapshot_id.strip():
            blockers.append("authoritative market snapshot ID is missing")
        if snapshot.history_rows < minimum_history_rows:
            blockers.append(
                "authoritative market snapshot has "
                f"{snapshot.history_rows} rows; at least {minimum_history_rows} are required"
            )

    return EvidencePreflightResult(
        passed=not blockers,
        readiness=(
            EvidenceReadiness.DECISION_READY
            if not blockers
            else EvidenceReadiness.INSUFFICIENT
        ),
        blockers=tuple(blockers),
    )


def evaluate_admission_gate(
    evidence: EvidenceState,
    minimum_history_rows: int = 1,
) -> AdmissionGateResult:
    """Evaluate whether acquired evidence may proceed to thesis synthesis."""
    if evidence.instrument_identity is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostics=(
                "Required evidence missing: resolved instrument identity.",
            ),
        )

    if evidence.market_snapshot is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostics=(
                "Required evidence missing: Authoritative Market Snapshot.",
            ),
        )

    if not evidence.market_snapshot.adjustment_basis.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostics=(
                "Required evidence missing: known Adjustment Basis.",
            ),
        )

    if not evidence.market_snapshot.effective_trading_date.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
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
            diagnostics=(
                "Insufficient market history: "
                f"{evidence.market_snapshot.history_rows} rows available; "
                f"{required_history_rows} required{calculation_suffix}.",
            ),
        )

    required_not_applicable = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.NOT_APPLICABLE
    )
    if required_not_applicable:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostics=tuple(
                "Required evidence configuration invalid: "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''} "
                "is marked not applicable."
                for source in required_not_applicable
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
        diagnostics=diagnostics,
    )
