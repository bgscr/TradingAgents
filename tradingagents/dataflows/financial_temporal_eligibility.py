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
PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION_V2 = (
    "provider-neutral-temporal-eligibility-v2"
)
MARKET_SESSION_ELIGIBILITY_BINDING_VERSION = "market-session-eligibility-binding-v1"
MARKET_SESSION_ELIGIBILITY_BINDING_VERSION_V2 = "market-session-eligibility-binding-v2"
FINANCIAL_ELIGIBILITY_ACQUISITION_WINDOW_VERSION = (
    "financial-eligibility-acquisition-window-v1"
)
FINANCIAL_ELIGIBILITY_TEMPORAL_TRANSACTION_BINDING_VERSION = (
    "financial-eligibility-temporal-transaction-binding-v1"
)
FINANCIAL_TEMPORAL_EVIDENCE_ACQUISITION_BINDING_VERSION = (
    "financial-temporal-evidence-acquisition-binding-v1"
)
FINANCIAL_METADATA_TEMPORAL_EVIDENCE_VERSION = "financial-metadata-temporal-evidence-v1"
FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION = (
    "financial-metadata-temporal-eligibility-decision-v1"
)
FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION_V2 = (
    "financial-metadata-temporal-eligibility-decision-v2"
)

_CALENDAR_IDENTITY_PREFIX = "market-session-eligibility-binding:v1:"
_CALENDAR_V2_IDENTITY_PREFIX = "market-session-eligibility-binding:v2:"
_ACQUISITION_WINDOW_IDENTITY_PREFIX = "financial-eligibility-acquisition-window:v1:"
_TEMPORAL_TRANSACTION_IDENTITY_PREFIX = (
    "financial-eligibility-temporal-transaction:v1:"
)
_TEMPORAL_EVIDENCE_ACQUISITION_IDENTITY_PREFIX = (
    "financial-temporal-evidence-acquisition:v1:"
)
_DECISION_IDENTITY_PREFIX = "financial-metadata-temporal-eligibility-decision:v1:"
_DECISION_V2_IDENTITY_PREFIX = "financial-metadata-temporal-eligibility-decision:v2:"
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CALENDAR_REVISION_ID_PATTERN = (
    r"^(?:market-session-calendar=sha256:|market-session-calendar:v1:)[0-9a-f]{64}$"
)
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


class TemporalEligibilityRejectionReasonV2(str, Enum):
    """Additive acquisition-window reasons; v1 vocabulary stays frozen."""

    ELIGIBILITY_EVALUATION_BEFORE_START = "eligibility_evaluation_before_start"
    OBSERVATION_BEFORE_ELIGIBILITY_START = "observation_before_eligibility_start"
    RETRIEVAL_BEFORE_ELIGIBILITY_START = "retrieval_before_eligibility_start"
    ACQUISITION_WINDOW_BINDING_MISMATCH = "acquisition_window_binding_mismatch"


class TemporalEvidenceAcquisitionOrigin(str, Enum):
    """Whether proof was acquired inside or before the active transaction."""

    FRESH = "fresh"
    RETAINED = "retained"


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
    calendar_revision_id: str = Field(pattern=_CALENDAR_REVISION_ID_PATTERN)
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


class FinancialEligibilityTemporalTransactionBinding(BaseModel):
    """Run-owned start binding for one open fresh-acquisition transaction."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-temporal-transaction-binding-v1"
    ] = FINANCIAL_ELIGIBILITY_TEMPORAL_TRANSACTION_BINDING_VERSION
    transaction_identity: str = Field(
        pattern=r"^financial-eligibility-temporal-transaction:v1:[0-9a-f]{64}$"
    )
    canonical_run_id: str = Field(pattern=r"^run:[0-9a-f]{64}$")
    routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    mode: TemporalEligibilityMode
    requested_session: date
    eligibility_started_at: datetime
    calendar_binding: MarketSessionEligibilityBinding
    strict_cutoff: datetime | None = None

    @field_validator("eligibility_started_at", "strict_cutoff")
    @classmethod
    def _validate_transaction_time(
        cls,
        value: datetime | None,
        info: Any,
    ) -> datetime | None:
        return None if value is None else _aware(value, field_name=info.field_name)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"transaction_identity"})

    @model_validator(mode="after")
    def _validate_transaction_binding(self) -> Self:
        strict = self.mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
        if (
            not self.calendar_binding.has_valid_identity()
            or self.requested_session != self.calendar_binding.requested_session
            or self.eligibility_started_at
            != self.calendar_binding.eligibility_evaluated_at
            or strict != (self.strict_cutoff is not None)
            or self.transaction_identity
            != _identity(
                _TEMPORAL_TRANSACTION_IDENTITY_PREFIX,
                self.identity_payload(),
            )
        ):
            raise ValueError("temporal transaction binding is inconsistent")
        return self


def build_financial_eligibility_temporal_transaction_binding(
    *,
    canonical_run_id: str,
    routing_plan_signature: str,
    mode: TemporalEligibilityMode,
    requested_session: date,
    eligibility_started_at: datetime,
    calendar_binding: MarketSessionEligibilityBinding,
    strict_cutoff: datetime | None = None,
) -> FinancialEligibilityTemporalTransactionBinding:
    payload = {
        "contract_version": (
            FINANCIAL_ELIGIBILITY_TEMPORAL_TRANSACTION_BINDING_VERSION
        ),
        "canonical_run_id": canonical_run_id,
        "routing_plan_signature": routing_plan_signature,
        "mode": mode,
        "requested_session": requested_session,
        "eligibility_started_at": eligibility_started_at,
        "calendar_binding": calendar_binding,
        "strict_cutoff": strict_cutoff,
    }
    return FinancialEligibilityTemporalTransactionBinding(
        **payload,
        transaction_identity=_identity(
            _TEMPORAL_TRANSACTION_IDENTITY_PREFIX,
            payload,
        ),
    )


class FinancialTemporalEvidenceAcquisitionBinding(BaseModel):
    """Proof staged under one active transaction with explicit origin."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-temporal-evidence-acquisition-binding-v1"
    ] = FINANCIAL_TEMPORAL_EVIDENCE_ACQUISITION_BINDING_VERSION
    binding_identity: str = Field(
        pattern=r"^financial-temporal-evidence-acquisition:v1:[0-9a-f]{64}$"
    )
    transaction_identity: str = Field(
        pattern=r"^financial-eligibility-temporal-transaction:v1:[0-9a-f]{64}$"
    )
    evidence_origin: TemporalEvidenceAcquisitionOrigin
    evidence: FinancialMetadataTemporalEvidence

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"binding_identity"})

    @model_validator(mode="after")
    def _validate_acquisition_binding(self) -> Self:
        if self.binding_identity != _identity(
            _TEMPORAL_EVIDENCE_ACQUISITION_IDENTITY_PREFIX,
            self.identity_payload(),
        ):
            raise ValueError("temporal evidence acquisition identity mismatch")
        return self


def bind_financial_temporal_evidence_to_transaction(
    *,
    transaction_identity: str,
    evidence_origin: TemporalEvidenceAcquisitionOrigin,
    evidence: FinancialMetadataTemporalEvidence,
) -> FinancialTemporalEvidenceAcquisitionBinding:
    payload = {
        "contract_version": FINANCIAL_TEMPORAL_EVIDENCE_ACQUISITION_BINDING_VERSION,
        "transaction_identity": transaction_identity,
        "evidence_origin": evidence_origin,
        "evidence": evidence,
    }
    return FinancialTemporalEvidenceAcquisitionBinding(
        **payload,
        binding_identity=_identity(
            _TEMPORAL_EVIDENCE_ACQUISITION_IDENTITY_PREFIX,
            payload,
        ),
    )


class MarketSessionEligibilityBindingV2(BaseModel):
    """Start and terminal frontiers from one pinned calendar revision."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["market-session-eligibility-binding-v2"] = (
        MARKET_SESSION_ELIGIBILITY_BINDING_VERSION_V2
    )
    binding_identity: str = Field(
        pattern=r"^market-session-eligibility-binding:v2:[0-9a-f]{64}$"
    )
    started: MarketSessionEligibilityBinding
    evaluated: MarketSessionEligibilityBinding

    @model_validator(mode="after")
    def _validate_window_frontiers(self) -> Self:
        if (
            not self.started.has_valid_identity()
            or not self.evaluated.has_valid_identity()
            or self.started.calendar_revision_id
            != self.evaluated.calendar_revision_id
            or self.started.reference_market != self.evaluated.reference_market
            or self.started.timezone_name != self.evaluated.timezone_name
            or self.started.requested_session != self.evaluated.requested_session
            or self.started.requested_session_open
            != self.evaluated.requested_session_open
            or self.started.requested_session_close
            != self.evaluated.requested_session_close
            or self.started.next_open_session != self.evaluated.next_open_session
            or self.started.next_session_close != self.evaluated.next_session_close
            or self.started.eligibility_evaluated_at
            > self.evaluated.eligibility_evaluated_at
        ):
            raise ValueError("market-session eligibility window binding is invalid")
        if self.binding_identity != _identity(
            _CALENDAR_V2_IDENTITY_PREFIX,
            self.identity_payload(),
        ):
            raise ValueError("market-session eligibility v2 identity mismatch")
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"binding_identity"})


def build_market_session_eligibility_binding_v2(
    calendar: StoredSessionCalendar,
    *,
    requested_session: date,
    eligibility_started_at: datetime,
    eligibility_evaluated_at: datetime,
) -> MarketSessionEligibilityBindingV2:
    """Bind the pre-I/O and terminal frontiers to one calendar revision."""

    started = build_market_session_eligibility_binding(
        calendar,
        requested_session=requested_session,
        eligibility_evaluated_at=eligibility_started_at,
    )
    evaluated = build_market_session_eligibility_binding(
        calendar,
        requested_session=requested_session,
        eligibility_evaluated_at=eligibility_evaluated_at,
    )
    payload = {
        "contract_version": MARKET_SESSION_ELIGIBILITY_BINDING_VERSION_V2,
        "started": started,
        "evaluated": evaluated,
    }
    return MarketSessionEligibilityBindingV2(
        **payload,
        binding_identity=_identity(_CALENDAR_V2_IDENTITY_PREFIX, payload),
    )


class FinancialEligibilityAcquisitionWindow(BaseModel):
    """One fresh-metadata interval sealed before context binding."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-eligibility-acquisition-window-v1"] = (
        FINANCIAL_ELIGIBILITY_ACQUISITION_WINDOW_VERSION
    )
    window_identity: str = Field(
        pattern=r"^financial-eligibility-acquisition-window:v1:[0-9a-f]{64}$"
    )
    transaction_binding: FinancialEligibilityTemporalTransactionBinding
    calendar_binding: MarketSessionEligibilityBindingV2

    @model_validator(mode="after")
    def _validate_window_identity(self) -> Self:
        if (
            self.transaction_binding.calendar_binding
            != self.calendar_binding.started
            or self.transaction_binding.requested_session
            != self.calendar_binding.evaluated.requested_session
            or self.window_identity
            != _identity(
                _ACQUISITION_WINDOW_IDENTITY_PREFIX,
                self.identity_payload(),
            )
        ):
            raise ValueError("eligibility acquisition window identity mismatch")
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"window_identity"})

    @property
    def eligibility_started_at(self) -> datetime:
        return self.calendar_binding.started.eligibility_evaluated_at

    @property
    def eligibility_evaluated_at(self) -> datetime:
        return self.calendar_binding.evaluated.eligibility_evaluated_at

    @property
    def calendar_revision_id(self) -> str:
        return self.calendar_binding.evaluated.calendar_revision_id

    @property
    def requested_session(self) -> date:
        return self.calendar_binding.evaluated.requested_session

    @property
    def started_latest_completed_session(self) -> date | None:
        return self.calendar_binding.started.latest_completed_session

    @property
    def evaluated_latest_completed_session(self) -> date | None:
        return self.calendar_binding.evaluated.latest_completed_session


def build_financial_eligibility_acquisition_window(
    calendar: StoredSessionCalendar,
    *,
    transaction_binding: FinancialEligibilityTemporalTransactionBinding,
    requested_session: date,
    eligibility_started_at: datetime,
    eligibility_evaluated_at: datetime,
) -> FinancialEligibilityAcquisitionWindow:
    calendar_binding = build_market_session_eligibility_binding_v2(
        calendar,
        requested_session=requested_session,
        eligibility_started_at=eligibility_started_at,
        eligibility_evaluated_at=eligibility_evaluated_at,
    )
    payload = {
        "contract_version": FINANCIAL_ELIGIBILITY_ACQUISITION_WINDOW_VERSION,
        "transaction_binding": transaction_binding,
        "calendar_binding": calendar_binding,
    }
    return FinancialEligibilityAcquisitionWindow(
        **payload,
        window_identity=_identity(_ACQUISITION_WINDOW_IDENTITY_PREFIX, payload),
    )


class FinancialMetadataTemporalEligibilityDecision(BaseModel):
    """Immutable accepted/rejected temporal decision and exact safe reasons."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-metadata-temporal-eligibility-decision-v1",
        "financial-metadata-temporal-eligibility-decision-v2",
    ] = FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION
    decision_identity: str = Field(
        pattern=(r"^financial-metadata-temporal-eligibility-decision:v[12]:[0-9a-f]{64}$")
    )
    temporal_policy_version: Literal[
        "provider-neutral-temporal-eligibility-v1",
        "provider-neutral-temporal-eligibility-v2",
    ] = PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION
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
    evidence_origin: TemporalEvidenceAcquisitionOrigin | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    evidence_acquisition_binding: (
        FinancialTemporalEvidenceAcquisitionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    acquisition_window: FinancialEligibilityAcquisitionWindow | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    result: Literal["accepted", "rejected"]
    reasons: tuple[
        TemporalEligibilityRejectionReason | TemporalEligibilityRejectionReasonV2,
        ...,
    ]
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
        def parse_reason(item: object):
            try:
                return TemporalEligibilityRejectionReason(item)
            except ValueError:
                return TemporalEligibilityRejectionReasonV2(item)

        parsed = tuple(parse_reason(item) for item in value)
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
        v2 = (
            self.contract_version
            == FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION_V2
        )
        if (
            v2
            != (
                self.temporal_policy_version
                == PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION_V2
            )
            or v2 != (self.acquisition_window is not None)
            or v2 != (self.evidence_origin is not None)
            or v2 != (self.evidence_acquisition_binding is not None)
        ):
            raise ValueError("temporal decision version bindings are inconsistent")
        if self.acquisition_window is not None and (
            self.calendar_binding
            != self.acquisition_window.calendar_binding.evaluated
        ):
            raise ValueError("temporal decision terminal binding mismatch")
        if self.acquisition_window is not None and (
            self.mode != self.acquisition_window.transaction_binding.mode.value
            or self.strict_cutoff
            != self.acquisition_window.transaction_binding.strict_cutoff
        ):
            raise ValueError("temporal decision active transaction binding mismatch")
        if self.evidence_acquisition_binding is not None and (
            self.acquisition_window is None
            or self.evidence_origin
            is not self.evidence_acquisition_binding.evidence_origin
            or self.evidence_acquisition_binding.transaction_identity
            != self.acquisition_window.transaction_binding.transaction_identity
        ):
            raise ValueError("temporal decision active transaction binding mismatch")
        if self.evidence_acquisition_binding is not None:
            bound_evidence = self.evidence_acquisition_binding.evidence
            eligibility_exclusions = {
                "retrieval_local_date",
                "exact_revision_availability_proof",
            }
            if (
                evidence.model_dump(
                    mode="python",
                    exclude=eligibility_exclusions,
                )
                != bound_evidence.model_dump(
                    mode="python",
                    exclude=eligibility_exclusions,
                )
                or (
                    self.mode
                    == TemporalEligibilityMode.STRICT_HISTORICAL_PIT.value
                    and evidence.exact_revision_availability_proof
                    != bound_evidence.exact_revision_availability_proof
                )
            ):
                raise ValueError(
                    "temporal decision staged evidence binding mismatch"
                )
        expected_reasons = (
            _canonical_temporal_eligibility_reasons_v2(
                mode_value=self.mode,
                evidence=evidence,
                acquisition_window=self.acquisition_window,
                evidence_origin=self.evidence_origin,
                strict_cutoff=self.strict_cutoff,
            )
            if self.acquisition_window is not None
            else _canonical_temporal_eligibility_reasons(
                mode_value=self.mode,
                evidence=evidence,
                calendar_binding=self.calendar_binding,
                strict_cutoff=self.strict_cutoff,
            )
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
            (
                _DECISION_V2_IDENTITY_PREFIX
                if v2
                else _DECISION_IDENTITY_PREFIX
            ),
            self.identity_payload(),
        ):
            raise ValueError("temporal eligibility decision identity mismatch")
        return self


class FinancialTemporalEligibilityCheckpointProjection(BaseModel):
    """Complete safe temporal decision state copied into replay contracts."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-temporal-binding-v1",
        "financial-eligibility-temporal-binding-v2",
    ] = "financial-eligibility-temporal-binding-v1"
    capability_routing_plan_version: Literal["1.3", "1.4"] = "1.3"
    temporal_policy_version: Literal[
        "provider-neutral-temporal-eligibility-v1",
        "provider-neutral-temporal-eligibility-v2",
    ] = PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION
    calendar_eligibility_binding_version: Literal[
        "market-session-eligibility-binding-v1",
        "market-session-eligibility-binding-v2",
    ] = MARKET_SESSION_ELIGIBILITY_BINDING_VERSION
    temporal_decision_version: Literal[
        "financial-metadata-temporal-eligibility-decision-v1",
        "financial-metadata-temporal-eligibility-decision-v2",
    ] = FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION
    requested_session: date
    temporal_mode: TemporalEligibilityMode
    eligibility_evaluated_at: datetime
    calendar_binding: MarketSessionEligibilityBinding
    latest_completed_session: date | None = None
    company_type_temporal_decision: FinancialMetadataTemporalEligibilityDecision
    listing_temporal_decision: FinancialMetadataTemporalEligibilityDecision
    acquisition_window: FinancialEligibilityAcquisitionWindow | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    strict_cutoff: datetime | None = None

    @field_validator("eligibility_evaluated_at", "strict_cutoff")
    @classmethod
    def _validate_checkpoint_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return None if value is None else _aware(value, field_name="temporal time")

    @model_validator(mode="after")
    def _validate_checkpoint_projection(self) -> Self:
        windowed = self.contract_version == "financial-eligibility-temporal-binding-v2"
        expected_versions = (
            (
                "1.4",
                PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION_V2,
                MARKET_SESSION_ELIGIBILITY_BINDING_VERSION_V2,
                FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION_V2,
            )
            if windowed
            else (
                "1.3",
                PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION,
                MARKET_SESSION_ELIGIBILITY_BINDING_VERSION,
                FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION,
            )
        )
        decisions = (
            self.company_type_temporal_decision,
            self.listing_temporal_decision,
        )
        if (
            (
                self.capability_routing_plan_version,
                self.temporal_policy_version,
                self.calendar_eligibility_binding_version,
                self.temporal_decision_version,
            )
            != expected_versions
            or windowed != (self.acquisition_window is not None)
            or (
                self.acquisition_window is not None
                and (
                    self.calendar_binding
                    != self.acquisition_window.calendar_binding.evaluated
                    or any(
                        decision.acquisition_window != self.acquisition_window
                        for decision in decisions
                    )
                )
            )
            or not self.calendar_binding.has_valid_identity()
            or self.requested_session != self.calendar_binding.requested_session
            or self.eligibility_evaluated_at
            != self.calendar_binding.eligibility_evaluated_at
            or self.latest_completed_session
            != self.calendar_binding.latest_completed_session
            or any(
                decision.mode != self.temporal_mode.value
                or decision.calendar_binding != self.calendar_binding
                or decision.strict_cutoff != self.strict_cutoff
                for decision in decisions
            )
            or self.company_type_temporal_decision.evidence_class
            not in {
                TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
                TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE,
            }
            or self.listing_temporal_decision.evidence_class
            is not TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING
            or (
                self.temporal_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
            )
            != (self.strict_cutoff is not None)
        ):
            raise ValueError("temporal checkpoint projection is inconsistent")
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


def _canonical_temporal_eligibility_reasons_v2(
    *,
    mode_value: str,
    evidence: FinancialMetadataTemporalEvidence,
    acquisition_window: FinancialEligibilityAcquisitionWindow,
    evidence_origin: TemporalEvidenceAcquisitionOrigin,
    strict_cutoff: datetime | None,
) -> tuple[
    TemporalEligibilityRejectionReason | TemporalEligibilityRejectionReasonV2,
    ...,
]:
    terminal_binding = acquisition_window.calendar_binding.evaluated
    reasons = set(
        _canonical_temporal_eligibility_reasons(
            mode_value=mode_value,
            evidence=evidence,
            calendar_binding=terminal_binding,
            strict_cutoff=strict_cutoff,
        )
    )
    if (
        acquisition_window.eligibility_evaluated_at
        < acquisition_window.eligibility_started_at
    ):
        reasons.add(
            TemporalEligibilityRejectionReasonV2.ELIGIBILITY_EVALUATION_BEFORE_START
        )
    if acquisition_window.calendar_binding.started.requested_session != (
        terminal_binding.requested_session
    ) or acquisition_window.calendar_binding.started.calendar_revision_id != (
        terminal_binding.calendar_revision_id
    ):
        reasons.add(
            TemporalEligibilityRejectionReasonV2.ACQUISITION_WINDOW_BINDING_MISMATCH
        )
    if evidence.retrieved_at > acquisition_window.eligibility_evaluated_at:
        reasons.add(
            TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION
        )
    if (
        evidence_origin is TemporalEvidenceAcquisitionOrigin.FRESH
        and evidence.observed_at < acquisition_window.eligibility_started_at
    ):
        reasons.add(
            TemporalEligibilityRejectionReasonV2.OBSERVATION_BEFORE_ELIGIBILITY_START
        )
    if (
        evidence_origin is TemporalEvidenceAcquisitionOrigin.FRESH
        and evidence.retrieved_at < acquisition_window.eligibility_started_at
    ):
        reasons.add(
            TemporalEligibilityRejectionReasonV2.RETRIEVAL_BEFORE_ELIGIBILITY_START
        )
    if (
        mode_value == TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY.value
        and acquisition_window.started_latest_completed_session
        != terminal_binding.requested_session
    ):
        reasons.add(
            TemporalEligibilityRejectionReason.REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER
        )
    return tuple(sorted(reasons, key=lambda reason: reason.value))


def qualify_financial_metadata_temporal_evidence_for_acquisition(
    *,
    mode: TemporalEligibilityMode,
    evidence: FinancialMetadataTemporalEvidence,
    started_calendar_binding: MarketSessionEligibilityBinding,
    evidence_origin: TemporalEvidenceAcquisitionOrigin = (
        TemporalEvidenceAcquisitionOrigin.FRESH
    ),
    strict_cutoff: datetime | None = None,
) -> tuple[
    TemporalEligibilityRejectionReason | TemporalEligibilityRejectionReasonV2,
    ...,
]:
    """Qualify provider proof while deferring only the terminal upper bound."""

    reasons = list(
        _canonical_temporal_eligibility_reasons(
            mode_value=mode.value,
            evidence=evidence,
            calendar_binding=started_calendar_binding,
            strict_cutoff=strict_cutoff,
        )
    )
    if mode is TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY:
        reasons = [
            reason
            for reason in reasons
            if reason
            is not TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION
        ]
    if (
        evidence_origin is TemporalEvidenceAcquisitionOrigin.FRESH
        and evidence.observed_at < started_calendar_binding.eligibility_evaluated_at
    ):
        reasons.append(
            TemporalEligibilityRejectionReasonV2.OBSERVATION_BEFORE_ELIGIBILITY_START
        )
    if (
        evidence_origin is TemporalEvidenceAcquisitionOrigin.FRESH
        and evidence.retrieved_at < started_calendar_binding.eligibility_evaluated_at
    ):
        reasons.append(
            TemporalEligibilityRejectionReasonV2.RETRIEVAL_BEFORE_ELIGIBILITY_START
        )
    return tuple(sorted(set(reasons), key=lambda reason: reason.value))


def _evaluate_financial_metadata_temporal_eligibility(
    *,
    mode: TemporalEligibilityMode | str,
    evidence: FinancialMetadataTemporalEvidence,
    calendar_binding: MarketSessionEligibilityBinding,
    strict_cutoff: datetime | None = None,
    acquisition_window: FinancialEligibilityAcquisitionWindow | None = None,
    evidence_origin: TemporalEvidenceAcquisitionOrigin | None = None,
    evidence_acquisition_binding: (
        FinancialTemporalEvidenceAcquisitionBinding | None
    ) = None,
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
    if acquisition_window is not None and (
        calendar_binding != acquisition_window.calendar_binding.evaluated
    ):
        raise ValueError("terminal calendar binding contradicts acquisition window")
    if (acquisition_window is not None) != (evidence_origin is not None) or (
        acquisition_window is not None
    ) != (evidence_acquisition_binding is not None):
        raise ValueError("terminal evidence acquisition binding is incomplete")
    canonical_reasons = (
        _canonical_temporal_eligibility_reasons_v2(
            mode_value=mode_value,
            evidence=evidence,
            acquisition_window=acquisition_window,
            evidence_origin=evidence_origin,
            strict_cutoff=effective_strict_cutoff,
        )
        if acquisition_window is not None
        else _canonical_temporal_eligibility_reasons(
            mode_value=mode_value,
            evidence=evidence,
            calendar_binding=calendar_binding,
            strict_cutoff=effective_strict_cutoff,
        )
    )
    accepted = not canonical_reasons
    proof_available_at = (
        effective_exact_proof.exact_revision_authoritative_available_at
        if effective_exact_proof is not None
        else None
    )
    payload: dict[str, object] = {
        "contract_version": (
            FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION_V2
            if acquisition_window is not None
            else FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION
        ),
        "temporal_policy_version": (
            PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION_V2
            if acquisition_window is not None
            else PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION
        ),
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
    if acquisition_window is not None:
        payload["acquisition_window"] = acquisition_window
        payload["evidence_origin"] = evidence_origin
        payload["evidence_acquisition_binding"] = evidence_acquisition_binding
    return FinancialMetadataTemporalEligibilityDecision(
        **payload,
        decision_identity=_identity(
            (
                _DECISION_V2_IDENTITY_PREFIX
                if acquisition_window is not None
                else _DECISION_IDENTITY_PREFIX
            ),
            payload,
        ),
    )


def evaluate_financial_metadata_temporal_eligibility(
    *,
    mode: TemporalEligibilityMode | str,
    evidence: FinancialMetadataTemporalEvidence,
    calendar_binding: MarketSessionEligibilityBinding,
    strict_cutoff: datetime | None = None,
) -> FinancialMetadataTemporalEligibilityDecision:
    """Evaluate the frozen v1 temporal policy without I/O or clock access."""

    return _evaluate_financial_metadata_temporal_eligibility(
        mode=mode,
        evidence=evidence,
        calendar_binding=calendar_binding,
        strict_cutoff=strict_cutoff,
    )


def evaluate_financial_metadata_temporal_eligibility_v2(
    *,
    mode: TemporalEligibilityMode | str,
    evidence: FinancialMetadataTemporalEvidence,
    acquisition_window: FinancialEligibilityAcquisitionWindow,
    evidence_origin: TemporalEvidenceAcquisitionOrigin | str = (
        TemporalEvidenceAcquisitionOrigin.FRESH
    ),
    evidence_acquisition_binding: (
        FinancialTemporalEvidenceAcquisitionBinding | None
    ) = None,
    strict_cutoff: datetime | None = None,
) -> FinancialMetadataTemporalEligibilityDecision:
    """Evaluate proof at the terminal instant of one acquisition window."""

    parsed_origin = TemporalEvidenceAcquisitionOrigin(evidence_origin)
    binding = evidence_acquisition_binding or (
        bind_financial_temporal_evidence_to_transaction(
            transaction_identity=(
                acquisition_window.transaction_binding.transaction_identity
            ),
            evidence_origin=parsed_origin,
            evidence=evidence,
        )
    )
    return _evaluate_financial_metadata_temporal_eligibility(
        mode=mode,
        evidence=evidence,
        calendar_binding=acquisition_window.calendar_binding.evaluated,
        strict_cutoff=strict_cutoff,
        acquisition_window=acquisition_window,
        evidence_origin=parsed_origin,
        evidence_acquisition_binding=binding,
    )


__all__ = [
    "FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION",
    "FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION_V2",
    "FINANCIAL_METADATA_TEMPORAL_EVIDENCE_VERSION",
    "FINANCIAL_ELIGIBILITY_ACQUISITION_WINDOW_VERSION",
    "FINANCIAL_ELIGIBILITY_TEMPORAL_TRANSACTION_BINDING_VERSION",
    "FINANCIAL_TEMPORAL_EVIDENCE_ACQUISITION_BINDING_VERSION",
    "MARKET_SESSION_ELIGIBILITY_BINDING_VERSION",
    "MARKET_SESSION_ELIGIBILITY_BINDING_VERSION_V2",
    "PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION",
    "PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION_V2",
    "EvidenceEffectiveAsOfDerivation",
    "EvidenceEffectiveAsOfSemantics",
    "ExactRevisionAvailabilityProof",
    "ExactRevisionAvailabilityScope",
    "FinancialMetadataTemporalEligibilityDecision",
    "FinancialMetadataTemporalEvidence",
    "FinancialEligibilityAcquisitionWindow",
    "FinancialEligibilityTemporalTransactionBinding",
    "FinancialTemporalEvidenceAcquisitionBinding",
    "FinancialTemporalEligibilityCheckpointProjection",
    "MarketSessionEligibilityBinding",
    "MarketSessionEligibilityBindingV2",
    "ProviderPublicationDateDerivation",
    "ProviderPublicationDateSemantics",
    "PublicationDateKind",
    "TemporalEligibilityMode",
    "TemporalEligibilityRejectionReason",
    "TemporalEligibilityRejectionReasonV2",
    "TemporalEvidenceAcquisitionOrigin",
    "TemporalEvidenceArtifactBinding",
    "TemporalEvidenceClass",
    "bind_financial_temporal_evidence_to_transaction",
    "build_financial_eligibility_temporal_transaction_binding",
    "build_market_session_eligibility_binding",
    "build_market_session_eligibility_binding_v2",
    "build_financial_eligibility_acquisition_window",
    "evaluate_financial_metadata_temporal_eligibility",
    "evaluate_financial_metadata_temporal_eligibility_v2",
    "qualify_financial_metadata_temporal_evidence_for_acquisition",
]
