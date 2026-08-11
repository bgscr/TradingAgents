"""Closed provider-neutral temporal eligibility contracts and pure evaluator."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

from tradingagents.market_history import (
    MarketSessionStatus,
    StoredSessionCalendar,
    latest_completed_session,
    market_session_close,
    next_open_market_session,
)

PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION = "provider-neutral-temporal-eligibility-v1"
MARKET_SESSION_ELIGIBILITY_BINDING_VERSION = "market-session-eligibility-binding-v1"
FINANCIAL_METADATA_TEMPORAL_EVIDENCE_VERSION = "financial-metadata-temporal-evidence-v1"
FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION = (
    "financial-metadata-temporal-eligibility-decision-v1"
)

_CALENDAR_IDENTITY_PREFIX = "market-session-eligibility-binding:v1:"
_DECISION_IDENTITY_PREFIX = "financial-metadata-temporal-eligibility-decision:v1:"
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    revalidate_instances="always",
)


class TemporalEligibilityMode(str, Enum):
    CURRENT_ANALYSIS_ONLY = "current_analysis_only"
    STRICT_HISTORICAL_PIT = "strict_historical_pit"


class TemporalEvidenceClass(str, Enum):
    TUSHARE_STATEMENT_COMPANY_TYPE = "tushare_statement_company_type"
    AKSHARE_SINA_STATEMENT_COMPANY_TYPE = "akshare_sina_statement_company_type"
    YFINANCE_CURRENT_PROFILE_COMPANY_TYPE = "yfinance_current_profile_company_type"
    BAOSTOCK_ISSUER_LIFECYCLE_LISTING = "baostock_issuer_lifecycle_listing"


class EvidenceEffectiveAsOfSemantics(str, Enum):
    TUSHARE_STATEMENT_PERIOD_END = "tushare_statement_period_end"
    AKSHARE_SINA_STATEMENT_PERIOD_END = "akshare_sina_statement_period_end"
    BAOSTOCK_LIFECYCLE_LISTING_DATE = "baostock_lifecycle_listing_date"


class PublicationDateKind(str, Enum):
    ANN_DATE = "ann_date"
    F_ANN_DATE = "f_ann_date"


class ProviderPublicationDateSemantics(str, Enum):
    TUSHARE_STATEMENT_PUBLICATION_FIELDS = "tushare_statement_publication_fields"
    AKSHARE_SINA_STATEMENT_PUBLICATION_FIELDS = "akshare_sina_statement_publication_fields"
    BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS = "baostock_lifecycle_publication_fields"


class ExactRevisionAvailabilityScope(str, Enum):
    EXACT_IMMUTABLE_REVISION = "exact_immutable_revision"
    GENERIC_FILING = "generic_filing"


class TemporalEligibilityRejectionReason(str, Enum):
    REQUESTED_SESSION_NOT_OPEN_IN_PINNED_CALENDAR = "requested_session_not_open_in_pinned_calendar"
    CALENDAR_ELIGIBILITY_BINDING_MISMATCH = "calendar_eligibility_binding_mismatch"
    OBSERVATION_AFTER_RETRIEVAL = "observation_after_retrieval"
    EVIDENCE_EFFECTIVE_AS_OF_UNPROVEN = "evidence_effective_as_of_unproven"
    EVIDENCE_EFFECTIVE_AFTER_REQUESTED_SESSION = "evidence_effective_after_requested_session"
    ANN_DATE_MISSING = "ann_date_missing"
    ANN_DATE_AFTER_REQUESTED_SESSION = "ann_date_after_requested_session"
    RETRIEVAL_BEFORE_REQUESTED_SESSION_COMPLETED = "retrieval_before_requested_session_completed"
    RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION = "retrieval_after_eligibility_evaluation"
    REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER = (
        "requested_session_not_latest_completed_frontier"
    )
    F_ANN_DATE_MISSING_FOR_STRICT_PIT = "f_ann_date_missing_for_strict_pit"
    F_ANN_DATE_AFTER_REQUESTED_SESSION = "f_ann_date_after_requested_session"
    EXACT_REVISION_NOT_AVAILABLE_BY_STRICT_CUTOFF = "exact_revision_not_available_by_strict_cutoff"
    TEMPORAL_EVIDENCE_ARTIFACT_BINDING_MISMATCH = "temporal_evidence_artifact_binding_mismatch"
    UNKNOWN_TEMPORAL_ELIGIBILITY_MODE = "unknown_temporal_eligibility_mode"


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _identity(prefix: str, material: object) -> str:
    encoded = json.dumps(
        _canonical_value(material),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return prefix + sha256(encoded).hexdigest()


class TemporalEvidenceArtifactBinding(BaseModel):
    """Exact immutable artifact, revision, and row under evaluation."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["temporal-evidence-artifact-binding-v1"] = (
        "temporal-evidence-artifact-binding-v1"
    )
    artifact_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    revision_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    row_identity: str = Field(pattern=_SAFE_ID_PATTERN)


class EvidenceEffectiveAsOfDerivation(BaseModel):
    """Closed provider-origin derivation of material effective applicability."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["evidence-effective-as-of-derivation-v1"] = (
        "evidence-effective-as-of-derivation-v1"
    )
    semantics: EvidenceEffectiveAsOfSemantics
    source_field: Literal[
        "end_date",
        "period_end",
        "report_period",
        "listing_date",
        "ipoDate",
    ]
    artifact: TemporalEvidenceArtifactBinding

    def qualified_for(self, evidence_class: TemporalEvidenceClass) -> bool:
        allowed = {
            TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE: (
                EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
                frozenset({"end_date", "period_end"}),
            ),
            TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE: (
                EvidenceEffectiveAsOfSemantics.AKSHARE_SINA_STATEMENT_PERIOD_END,
                frozenset({"end_date", "period_end", "report_period"}),
            ),
            TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING: (
                EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE,
                frozenset({"listing_date", "ipoDate"}),
            ),
        }.get(evidence_class)
        return allowed is not None and (
            self.semantics is allowed[0] and self.source_field in allowed[1]
        )


class ProviderPublicationDateDerivation(BaseModel):
    """Closed provider-origin derivation for ann_date or f_ann_date."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["provider-publication-date-derivation-v1"] = (
        "provider-publication-date-derivation-v1"
    )
    date_kind: PublicationDateKind
    semantics: ProviderPublicationDateSemantics
    source_field: Literal[
        "ann_date",
        "f_ann_date",
        "公告日期",
        "publish_date",
        "首次公告日期",
        "首次披露日期",
        "announcement_date",
        "publication_date",
        "first_publication_date",
    ]
    artifact: TemporalEvidenceArtifactBinding

    def qualified_for(
        self,
        evidence_class: TemporalEvidenceClass,
        *,
        date_kind: PublicationDateKind,
    ) -> bool:
        allowed = {
            TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE: (
                ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS,
                {
                    PublicationDateKind.ANN_DATE: frozenset({"ann_date"}),
                    PublicationDateKind.F_ANN_DATE: frozenset({"f_ann_date"}),
                },
            ),
            TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE: (
                ProviderPublicationDateSemantics.AKSHARE_SINA_STATEMENT_PUBLICATION_FIELDS,
                {
                    PublicationDateKind.ANN_DATE: frozenset(
                        {"公告日期", "ann_date", "publish_date"}
                    ),
                    PublicationDateKind.F_ANN_DATE: frozenset(
                        {"首次公告日期", "首次披露日期", "f_ann_date"}
                    ),
                },
            ),
            TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING: (
                ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS,
                {
                    PublicationDateKind.ANN_DATE: frozenset(
                        {"announcement_date", "publication_date"}
                    ),
                    PublicationDateKind.F_ANN_DATE: frozenset({"first_publication_date"}),
                },
            ),
        }.get(evidence_class)
        return allowed is not None and (
            self.date_kind is date_kind
            and self.semantics is allowed[0]
            and self.source_field in allowed[1][date_kind]
        )


class ExactRevisionAvailabilityProof(BaseModel):
    """Availability proof that may be exact-revision or generic-filing scope."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["exact-revision-availability-proof-v1"] = (
        "exact-revision-availability-proof-v1"
    )
    exact_revision_authoritative_available_at: datetime
    authoritative: bool
    scope: ExactRevisionAvailabilityScope
    proof_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    artifact: TemporalEvidenceArtifactBinding

    @field_validator("exact_revision_authoritative_available_at")
    @classmethod
    def _validate_available_at(cls, value: datetime) -> datetime:
        return _aware(
            value,
            field_name="exact_revision_authoritative_available_at",
        )


class FinancialMetadataTemporalEvidence(BaseModel):
    """Provider-neutral immutable proof offered to the temporal evaluator."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-metadata-temporal-evidence-v1"] = (
        FINANCIAL_METADATA_TEMPORAL_EVIDENCE_VERSION
    )
    evidence_class: TemporalEvidenceClass
    artifact: TemporalEvidenceArtifactBinding
    evidence_effective_as_of: date | None = None
    evidence_effective_as_of_derivation: EvidenceEffectiveAsOfDerivation | None = None
    ann_date: date | None = None
    ann_date_derivation: ProviderPublicationDateDerivation | None = None
    f_ann_date: date | None = None
    f_ann_date_derivation: ProviderPublicationDateDerivation | None = None
    observed_at: datetime
    retrieved_at: datetime
    exact_revision_availability_proof: ExactRevisionAvailabilityProof | None = None
    retrieval_local_date: date | None = None

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime, info: Any) -> datetime:
        return _aware(value, field_name=info.field_name)


class MarketSessionEligibilityBinding(BaseModel):
    """Content-derived decision facts from one pinned calendar revision."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["market-session-eligibility-binding-v1"] = (
        MARKET_SESSION_ELIGIBILITY_BINDING_VERSION
    )
    binding_identity: str = Field(pattern=r"^market-session-eligibility-binding:v1:[0-9a-f]{64}$")
    calendar_revision_id: str = Field(pattern=_SAFE_ID_PATTERN)
    reference_market: str = Field(pattern=_SAFE_ID_PATTERN)
    timezone_name: str = Field(min_length=1, max_length=64)
    requested_session: date
    requested_session_open: bool
    requested_session_close: datetime | None = None
    next_open_session: date | None = None
    next_session_close: datetime | None = None
    eligibility_evaluated_at: datetime
    latest_completed_session: date | None = None

    @field_validator(
        "requested_session_close",
        "next_session_close",
        "eligibility_evaluated_at",
    )
    @classmethod
    def _validate_timestamp(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        ZoneInfo(self.timezone_name)
        if self.requested_session_open != (self.requested_session_close is not None):
            raise ValueError("requested-session open status and close contradict")
        if (self.next_open_session is None) != (self.next_session_close is None):
            raise ValueError("next-session date and close must be supplied together")
        if self.next_open_session is not None and self.next_open_session <= self.requested_session:
            raise ValueError("next open session must follow requested session")
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"binding_identity"})

    def has_valid_identity(self) -> bool:
        return self.binding_identity == _identity(
            _CALENDAR_IDENTITY_PREFIX,
            self.identity_payload(),
        )


def build_market_session_eligibility_binding(
    calendar: StoredSessionCalendar,
    *,
    requested_session: date,
    eligibility_evaluated_at: datetime,
) -> MarketSessionEligibilityBinding:
    """Project one immutable eligibility binding from the calendar authority."""

    _aware(eligibility_evaluated_at, field_name="eligibility_evaluated_at")
    requested = next(
        (session for session in calendar.sessions if session.session_date == requested_session),
        None,
    )
    requested_open = requested is not None and requested.status is MarketSessionStatus.OPEN
    requested_close = (
        market_session_close(calendar, session=requested)
        if requested_open and requested is not None
        else None
    )
    next_session = next_open_market_session(calendar, after=requested_session)
    next_close = (
        market_session_close(calendar, session=next_session) if next_session is not None else None
    )
    payload: dict[str, object] = {
        "contract_version": MARKET_SESSION_ELIGIBILITY_BINDING_VERSION,
        "calendar_revision_id": calendar.calendar_revision_id,
        "reference_market": calendar.reference_market,
        "timezone_name": calendar.timezone_name,
        "requested_session": requested_session,
        "requested_session_open": requested_open,
        "requested_session_close": requested_close,
        "next_open_session": (next_session.session_date if next_session is not None else None),
        "next_session_close": next_close,
        "eligibility_evaluated_at": eligibility_evaluated_at,
        "latest_completed_session": latest_completed_session(
            calendar,
            as_of=eligibility_evaluated_at,
        ),
    }
    return MarketSessionEligibilityBinding(
        **payload,
        binding_identity=_identity(_CALENDAR_IDENTITY_PREFIX, payload),
    )


class FinancialMetadataTemporalEligibilityDecision(BaseModel):
    """Immutable accepted/rejected temporal decision and exact safe reasons."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-metadata-temporal-eligibility-decision-v1"] = (
        FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION
    )
    decision_identity: str = Field(
        pattern=(r"^financial-metadata-temporal-eligibility-decision:v1:[0-9a-f]{64}$")
    )
    temporal_policy_version: Literal["provider-neutral-temporal-eligibility-v1"] = (
        PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION
    )
    mode: str = Field(pattern=r"^[a-z][a-z0-9_]{0,78}$")
    immutable_artifact_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    immutable_revision_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    immutable_row_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    evidence_class: TemporalEvidenceClass
    evidence_effective_as_of: date | None = None
    evidence_effective_as_of_derivation: EvidenceEffectiveAsOfDerivation | None = None
    ann_date: date | None = None
    ann_date_derivation: ProviderPublicationDateDerivation | None = None
    f_ann_date: date | None = None
    f_ann_date_derivation: ProviderPublicationDateDerivation | None = None
    observed_at: datetime
    retrieved_at: datetime
    strict_cutoff: datetime | None = None
    exact_revision_authoritative_available_at: datetime | None = None
    exact_revision_availability_proof: ExactRevisionAvailabilityProof | None = None
    calendar_binding: MarketSessionEligibilityBinding
    result: Literal["accepted", "rejected"]
    reasons: tuple[TemporalEligibilityRejectionReason, ...]
    eligible_session: date | None = None

    @field_validator(
        "observed_at",
        "retrieved_at",
        "strict_cutoff",
        "exact_revision_authoritative_available_at",
    )
    @classmethod
    def _validate_timestamp(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware(value, field_name=info.field_name)

    @field_validator("reasons", mode="before")
    @classmethod
    def _canonical_reasons(cls, value: object) -> object:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return value
        parsed = tuple(TemporalEligibilityRejectionReason(item) for item in value)
        canonical = tuple(sorted(set(parsed), key=lambda reason: reason.value))
        if parsed != canonical:
            raise ValueError("temporal eligibility reasons must be sorted and unique")
        return parsed

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"decision_identity"})

    @model_validator(mode="after")
    def _validate_decision(self) -> Self:
        artifact = TemporalEvidenceArtifactBinding(
            artifact_identity=self.immutable_artifact_identity,
            revision_identity=self.immutable_revision_identity,
            row_identity=self.immutable_row_identity,
        )
        evidence = FinancialMetadataTemporalEvidence(
            evidence_class=self.evidence_class,
            artifact=artifact,
            evidence_effective_as_of=self.evidence_effective_as_of,
            evidence_effective_as_of_derivation=(self.evidence_effective_as_of_derivation),
            ann_date=self.ann_date,
            ann_date_derivation=self.ann_date_derivation,
            f_ann_date=self.f_ann_date,
            f_ann_date_derivation=self.f_ann_date_derivation,
            observed_at=self.observed_at,
            retrieved_at=self.retrieved_at,
            exact_revision_availability_proof=(self.exact_revision_availability_proof),
        )
        expected_reasons = _canonical_temporal_eligibility_reasons(
            mode_value=self.mode,
            evidence=evidence,
            calendar_binding=self.calendar_binding,
            strict_cutoff=self.strict_cutoff,
        )
        expected_result = "accepted" if not expected_reasons else "rejected"
        expected_eligible_session = (
            self.calendar_binding.requested_session if not expected_reasons else None
        )
        proof_available_at = (
            self.exact_revision_availability_proof.exact_revision_authoritative_available_at
            if self.exact_revision_availability_proof is not None
            else None
        )
        if (
            self.reasons != expected_reasons
            or self.result != expected_result
            or self.eligible_session != expected_eligible_session
            or self.exact_revision_authoritative_available_at != proof_available_at
        ):
            raise ValueError("temporal eligibility predicate mismatch")
        if self.decision_identity != _identity(
            _DECISION_IDENTITY_PREFIX,
            self.identity_payload(),
        ):
            raise ValueError("temporal eligibility decision identity mismatch")
        return self


def _artifact_bindings_match(
    evidence: FinancialMetadataTemporalEvidence,
    *,
    include_exact_revision_proof: bool,
) -> bool:
    bindings = (
        evidence.evidence_effective_as_of_derivation.artifact
        if evidence.evidence_effective_as_of_derivation is not None
        else None,
        (
            evidence.ann_date_derivation.artifact
            if evidence.ann_date_derivation is not None
            else None
        ),
        (
            evidence.f_ann_date_derivation.artifact
            if evidence.f_ann_date_derivation is not None
            else None
        ),
        evidence.exact_revision_availability_proof.artifact
        if (include_exact_revision_proof and evidence.exact_revision_availability_proof is not None)
        else None,
    )
    return all(binding is None or binding == evidence.artifact for binding in bindings)


def _safe_mode(value: TemporalEligibilityMode | str) -> str:
    raw = value.value if isinstance(value, TemporalEligibilityMode) else str(value)
    if raw in {item.value for item in TemporalEligibilityMode}:
        return raw
    return "unknown_sha256_" + sha256(raw.encode("utf-8")).hexdigest()


def _canonical_temporal_eligibility_reasons(
    *,
    mode_value: str,
    evidence: FinancialMetadataTemporalEvidence,
    calendar_binding: MarketSessionEligibilityBinding,
    strict_cutoff: datetime | None,
) -> tuple[TemporalEligibilityRejectionReason, ...]:
    known_mode = (
        TemporalEligibilityMode(mode_value)
        if mode_value in {item.value for item in TemporalEligibilityMode}
        else None
    )
    reasons: set[TemporalEligibilityRejectionReason] = set()
    if known_mode is None:
        reasons.add(TemporalEligibilityRejectionReason.UNKNOWN_TEMPORAL_ELIGIBILITY_MODE)
    if not calendar_binding.has_valid_identity():
        reasons.add(TemporalEligibilityRejectionReason.CALENDAR_ELIGIBILITY_BINDING_MISMATCH)
    if evidence.observed_at > evidence.retrieved_at:
        reasons.add(TemporalEligibilityRejectionReason.OBSERVATION_AFTER_RETRIEVAL)
    derivation = evidence.evidence_effective_as_of_derivation
    if (
        evidence.evidence_effective_as_of is None
        or derivation is None
        or not derivation.qualified_for(evidence.evidence_class)
    ):
        reasons.add(TemporalEligibilityRejectionReason.EVIDENCE_EFFECTIVE_AS_OF_UNPROVEN)
    elif evidence.evidence_effective_as_of > calendar_binding.requested_session:
        reasons.add(TemporalEligibilityRejectionReason.EVIDENCE_EFFECTIVE_AFTER_REQUESTED_SESSION)
    if evidence.ann_date is None:
        reasons.add(TemporalEligibilityRejectionReason.ANN_DATE_MISSING)
    elif evidence.ann_date > calendar_binding.requested_session:
        reasons.add(TemporalEligibilityRejectionReason.ANN_DATE_AFTER_REQUESTED_SESSION)
    if (
        not _artifact_bindings_match(
            evidence,
            include_exact_revision_proof=(
                known_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
            ),
        )
        or (
            evidence.ann_date is not None
            and (
                evidence.ann_date_derivation is None
                or not evidence.ann_date_derivation.qualified_for(
                    evidence.evidence_class,
                    date_kind=PublicationDateKind.ANN_DATE,
                )
            )
        )
        or (
            evidence.f_ann_date is not None
            and (
                evidence.f_ann_date_derivation is None
                or not evidence.f_ann_date_derivation.qualified_for(
                    evidence.evidence_class,
                    date_kind=PublicationDateKind.F_ANN_DATE,
                )
            )
        )
        or (evidence.ann_date is None and evidence.ann_date_derivation is not None)
        or (evidence.f_ann_date is None and evidence.f_ann_date_derivation is not None)
    ):
        reasons.add(TemporalEligibilityRejectionReason.TEMPORAL_EVIDENCE_ARTIFACT_BINDING_MISMATCH)

    if known_mode is TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY:
        if not calendar_binding.requested_session_open:
            reasons.add(
                TemporalEligibilityRejectionReason.REQUESTED_SESSION_NOT_OPEN_IN_PINNED_CALENDAR
            )
        close = calendar_binding.requested_session_close
        if close is not None and evidence.retrieved_at < close:
            reasons.add(
                TemporalEligibilityRejectionReason.RETRIEVAL_BEFORE_REQUESTED_SESSION_COMPLETED
            )
        if evidence.retrieved_at > calendar_binding.eligibility_evaluated_at:
            reasons.add(TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION)
        if calendar_binding.latest_completed_session != calendar_binding.requested_session:
            reasons.add(
                TemporalEligibilityRejectionReason.REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER
            )
    elif known_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT:
        if evidence.f_ann_date is None:
            reasons.add(TemporalEligibilityRejectionReason.F_ANN_DATE_MISSING_FOR_STRICT_PIT)
        elif evidence.f_ann_date > calendar_binding.requested_session:
            reasons.add(TemporalEligibilityRejectionReason.F_ANN_DATE_AFTER_REQUESTED_SESSION)
        direct = strict_cutoff is not None and (
            evidence.observed_at <= strict_cutoff and evidence.retrieved_at <= strict_cutoff
        )
        exact_proof = evidence.exact_revision_availability_proof
        exact = exact_proof is not None and (
            strict_cutoff is not None
            and exact_proof.authoritative
            and exact_proof.scope is ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION
            and exact_proof.artifact == evidence.artifact
            and exact_proof.exact_revision_authoritative_available_at <= strict_cutoff
        )
        if not (direct or exact):
            reasons.add(
                TemporalEligibilityRejectionReason.EXACT_REVISION_NOT_AVAILABLE_BY_STRICT_CUTOFF
            )

    return tuple(sorted(reasons, key=lambda reason: reason.value))


def evaluate_financial_metadata_temporal_eligibility(
    *,
    mode: TemporalEligibilityMode | str,
    evidence: FinancialMetadataTemporalEvidence,
    calendar_binding: MarketSessionEligibilityBinding,
    strict_cutoff: datetime | None = None,
) -> FinancialMetadataTemporalEligibilityDecision:
    """Evaluate immutable proof with no provider, clock, calendar, or model I/O."""

    mode_value = _safe_mode(mode)
    known_mode = (
        TemporalEligibilityMode(mode_value)
        if mode_value in {item.value for item in TemporalEligibilityMode}
        else None
    )
    effective_strict_cutoff = (
        strict_cutoff if known_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT else None
    )
    effective_exact_proof = (
        evidence.exact_revision_availability_proof
        if known_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
        else None
    )
    if effective_strict_cutoff is not None:
        _aware(effective_strict_cutoff, field_name="strict_cutoff")
    canonical_reasons = _canonical_temporal_eligibility_reasons(
        mode_value=mode_value,
        evidence=evidence,
        calendar_binding=calendar_binding,
        strict_cutoff=effective_strict_cutoff,
    )
    accepted = not canonical_reasons
    proof_available_at = (
        effective_exact_proof.exact_revision_authoritative_available_at
        if effective_exact_proof is not None
        else None
    )
    payload: dict[str, object] = {
        "contract_version": (FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION),
        "temporal_policy_version": PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION,
        "mode": mode_value,
        "immutable_artifact_identity": evidence.artifact.artifact_identity,
        "immutable_revision_identity": evidence.artifact.revision_identity,
        "immutable_row_identity": evidence.artifact.row_identity,
        "evidence_class": evidence.evidence_class,
        "evidence_effective_as_of": evidence.evidence_effective_as_of,
        "evidence_effective_as_of_derivation": (evidence.evidence_effective_as_of_derivation),
        "ann_date": evidence.ann_date,
        "ann_date_derivation": evidence.ann_date_derivation,
        "f_ann_date": evidence.f_ann_date,
        "f_ann_date_derivation": evidence.f_ann_date_derivation,
        "observed_at": evidence.observed_at,
        "retrieved_at": evidence.retrieved_at,
        "strict_cutoff": effective_strict_cutoff,
        "exact_revision_authoritative_available_at": proof_available_at,
        "exact_revision_availability_proof": effective_exact_proof,
        "calendar_binding": calendar_binding,
        "result": "accepted" if accepted else "rejected",
        "reasons": canonical_reasons,
        "eligible_session": (calendar_binding.requested_session if accepted else None),
    }
    return FinancialMetadataTemporalEligibilityDecision(
        **payload,
        decision_identity=_identity(_DECISION_IDENTITY_PREFIX, payload),
    )


__all__ = [
    "FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION",
    "FINANCIAL_METADATA_TEMPORAL_EVIDENCE_VERSION",
    "MARKET_SESSION_ELIGIBILITY_BINDING_VERSION",
    "PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION",
    "EvidenceEffectiveAsOfDerivation",
    "EvidenceEffectiveAsOfSemantics",
    "ExactRevisionAvailabilityProof",
    "ExactRevisionAvailabilityScope",
    "FinancialMetadataTemporalEligibilityDecision",
    "FinancialMetadataTemporalEvidence",
    "MarketSessionEligibilityBinding",
    "ProviderPublicationDateDerivation",
    "ProviderPublicationDateSemantics",
    "PublicationDateKind",
    "TemporalEligibilityMode",
    "TemporalEligibilityRejectionReason",
    "TemporalEvidenceArtifactBinding",
    "TemporalEvidenceClass",
    "build_market_session_eligibility_binding",
    "evaluate_financial_metadata_temporal_eligibility",
]
