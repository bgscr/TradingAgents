"""Explicit BaoStock market-history and issuer-lifecycle capabilities.

Ticket 08 exposes bounded adapter outputs only.  Ticket 10 owns production
routing, so this module never creates Source Facts or changes the Decision Gate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.factor_name_contracts import (
    ProviderArtifactIdentity,
    ProviderDatasetIdentity,
    ProviderRowArtifactIdentity,
)
from tradingagents.dataflows.financial_contracts import FinancialListingProvenance
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactRef,
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestInstrumentIdentity,
    ProviderSubrequestKey,
    ProviderSubrequestOutcome,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.models import (
    AdjustmentFactorObservation,
    InstrumentSpec,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    TradingStatus,
)
from tradingagents.market_history.suspension import normalize_mainland_session

BAOSTOCK_CAPABILITY_ADAPTER_VERSION = "baostock-market-capability-adapter-v1"
BAOSTOCK_CAPABILITY_NORMALIZER_VERSION = "baostock-market-capability-normalizer-v1"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_RAW_SCOPE = "raw_daily"
_FACTOR_SCOPE = "forward_adjustment_factors"
_STATUS_SCOPE = "session_status"
_LIFECYCLE_SCOPE = "issuer_lifecycle"
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CREDENTIAL_FIELD_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_CREDENTIAL_VALUE_MARKERS = (
    "api-key=",
    "api_key=",
    "apikey=",
    "authorization:",
    "authorization=",
    "bearer ",
    "credential=",
    "password=",
    "secret=",
    "token=",
)


class BaoStockCapabilityTransport(Protocol):
    """One physical transport method for each safe endpoint scope."""

    def query_raw_daily(self, **kwargs: object) -> pd.DataFrame: ...

    def query_forward_adjustment_factors(self, **kwargs: object) -> pd.DataFrame: ...

    def query_session_status(self, **kwargs: object) -> pd.DataFrame: ...

    def query_lifecycle(self, **kwargs: object) -> pd.DataFrame: ...


class BaoStockCapabilityIdentity(str, Enum):
    RAW_DAILY_OBSERVATIONS = "baostock_raw_daily_observations"
    FORWARD_ADJUSTMENT_FACTORS = "baostock_forward_adjustment_factors"
    SESSION_TRADING_STATUS = "baostock_session_trading_status"
    DATED_ST_STATUS = "baostock_dated_st_status"
    ISSUER_LIFECYCLE = "baostock_issuer_lifecycle"


class BaoStockCapabilityOutcomeKind(str, Enum):
    AVAILABLE = "available"
    PERMISSION_DENIED = "permission_denied"
    AUTHENTICATION_FAILURE = "authentication_failure"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    EMPTY_NO_DATA = "empty_no_data"
    MALFORMED_RESPONSE = "malformed_response"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    CONFLICTING_FACTOR = "conflicting_factor"
    CONTRADICTORY_STATUS = "contradictory_status"
    INCOMPLETE_STRICT_BUNDLE = "incomplete_strict_bundle"
    PROVIDER_ERROR = "provider_error"


class BaoStockCurrentTradeability(str, Enum):
    TRADEABLE = "tradeable"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class BaoStockStrictBundleState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class BaoStockListingStatus(str, Enum):
    LISTED = "listed"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class BaoStockRowRejectionReason(str, Enum):
    MALFORMED_RESPONSE = "malformed_response"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    CONFLICTING_FACTOR = "conflicting_factor"
    CONTRADICTORY_STATUS = "contradictory_status"


class BaoStockCapabilityOutcome(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    kind: BaoStockCapabilityOutcomeKind
    retryable: bool = False


class BaoStockRowRejection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    reason: BaoStockRowRejectionReason
    row_artifact_identity: str = Field(pattern=r"^provider-row-artifact:v1:[0-9a-f]{64}$")
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    effective_date: date | None = None


class _BaoStockCandidateBase(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    instrument_identity: InstrumentIdentityEvidence
    canonical_symbol: str
    provider_id: Literal["baostock"] = "baostock"
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    raw_artifact: ProviderSubrequestArtifactRef
    row_artifact_identities: tuple[str, ...] = Field(min_length=1)
    retrieved_at: datetime
    observed_at: datetime
    source_fact_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BaoStock candidate time must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_common(self):
        if self.canonical_symbol != self.instrument_identity.symbol.strip().upper():
            raise ValueError("BaoStock candidate contradicts Instrument Identity")
        if self.row_artifact_identities != tuple(sorted(set(self.row_artifact_identities))):
            raise ValueError("BaoStock row identities must be canonical and unique")
        return self


class BaoStockRawDailyObservationCandidate(_BaoStockCandidateBase):
    capability_identity: Literal[BaoStockCapabilityIdentity.RAW_DAILY_OBSERVATIONS] = (
        BaoStockCapabilityIdentity.RAW_DAILY_OBSERVATIONS
    )
    session_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    preclose: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    original_open: str
    original_high: str
    original_low: str
    original_close: str
    original_preclose: str
    original_volume: str
    original_amount: str

    @model_validator(mode="after")
    def _validate_values(self):
        values = (
            self.open,
            self.high,
            self.low,
            self.close,
            self.preclose,
            self.volume,
            self.amount,
        )
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("BaoStock raw values must be finite")
        if self.volume is not None and self.volume < 0:
            raise ValueError("BaoStock raw volume must not be negative")
        if all(value is not None for value in (self.open, self.high, self.low, self.close)):
            assert self.open is not None
            assert self.high is not None
            assert self.low is not None
            assert self.close is not None
            if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
                raise ValueError("BaoStock raw observation violates OHLC ordering")
        return self


class BaoStockForwardFactorCandidate(_BaoStockCandidateBase):
    capability_identity: Literal[BaoStockCapabilityIdentity.FORWARD_ADJUSTMENT_FACTORS] = (
        BaoStockCapabilityIdentity.FORWARD_ADJUSTMENT_FACTORS
    )
    effective_date: date
    factor: Decimal
    original_factor: str
    provenance_class: Literal[ProvenanceClass.RETROSPECTIVE_BACKFILL] = (
        ProvenanceClass.RETROSPECTIVE_BACKFILL
    )
    native_adjustment_factor_revision: Literal[True] = True
    strict_bundle_component_capable: Literal[True] = True
    provider_history_bundle_complete: Literal[False] = False

    @model_validator(mode="after")
    def _validate_factor(self):
        if not self.factor.is_finite() or self.factor <= 0:
            raise ValueError("BaoStock factor must be positive and finite")
        return self


class BaoStockSessionStatusCandidate(_BaoStockCandidateBase):
    capability_identity: Literal[BaoStockCapabilityIdentity.SESSION_TRADING_STATUS] = (
        BaoStockCapabilityIdentity.SESSION_TRADING_STATUS
    )
    st_capability_identity: Literal[BaoStockCapabilityIdentity.DATED_ST_STATUS] = (
        BaoStockCapabilityIdentity.DATED_ST_STATUS
    )
    session_date: date
    trading_status: TradingStatus | None
    is_st: bool | None
    official_carried_close: Decimal | None
    original_tradestatus: str
    original_is_st: str
    original_preclose: str
    trading_status_authoritative: bool
    st_status_authoritative: bool

    @model_validator(mode="after")
    def _validate_authority(self):
        if self.trading_status_authoritative != (self.trading_status is not None):
            raise ValueError("BaoStock trading-status authority is inconsistent")
        if self.st_status_authoritative != (self.is_st is not None):
            raise ValueError("BaoStock ST authority is inconsistent")
        if self.trading_status is TradingStatus.SUSPENDED and self.official_carried_close is None:
            raise ValueError("BaoStock suspension requires an official carried close")
        return self


class BaoStockLifecycleCandidate(_BaoStockCandidateBase):
    capability_identity: Literal[BaoStockCapabilityIdentity.ISSUER_LIFECYCLE] = (
        BaoStockCapabilityIdentity.ISSUER_LIFECYCLE
    )
    ipo_date: date | None
    listing_date: date | None
    out_date: date | None
    delisting_date: date | None
    listing_status: BaoStockListingStatus
    original_ipo_date: str
    original_out_date: str
    original_type: str
    original_status: str
    observed_name: str | None = None
    st_status_established: Literal[False] = False
    current_tradeability_established: Literal[False] = False

    def financial_listing_provenance(self) -> FinancialListingProvenance:
        if self.listing_date is None:
            raise ValueError("BaoStock lifecycle has no authoritative listing date")
        return FinancialListingProvenance(
            provider_id="baostock",
            source_ref=self.artifact_identity,
            observed_at=self.observed_at,
        )


class _BaoStockAdapterResultBase(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    endpoint_scope: str = Field(pattern=_SAFE_ID_PATTERN)
    request: ProviderSubrequestKey
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: ProviderArtifactIdentity | None = None
    row_artifacts: tuple[ProviderRowArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    capability_outcome: BaoStockCapabilityOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    source_facts_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_artifact_outcome(self):
        if (
            self.request.provider_id != "baostock"
            or self.request.capacity_scope != self.endpoint_scope
            or self.request.subrequest_key != self.subrequest_key
        ):
            raise ValueError("BaoStock result contradicts canonical request identity")
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("BaoStock artifact contradicts transport outcome")
        if not available and self.row_artifacts:
            raise ValueError("unavailable BaoStock result cannot expose row artifacts")
        if available:
            assert self.provider_artifact is not None
            assert self.artifact is not None
            if (
                self.artifact.subrequest_key != self.request.subrequest_key
                or self.artifact.raw_artifact != self.provider_artifact
                or self.artifact.dataset.provider_id != self.request.provider_id
                or self.artifact.dataset.endpoint_id != self.request.capacity_scope
                or self.artifact.qualification_profile != self.request.qualification_profile
                or self.artifact.normalizer_version != self.request.normalizer_version
            ):
                raise ValueError("BaoStock artifact contradicts canonical request binding")
            row_ids = {item.row_artifact_identity for item in self.row_artifacts}
            candidates = getattr(self, "candidates", ())
            rejections = getattr(self, "row_rejections", ())
            if not row_ids:
                if (
                    candidates
                    or rejections
                    or self.capability_outcome.kind
                    is not BaoStockCapabilityOutcomeKind.EMPTY_NO_DATA
                ):
                    raise ValueError("empty BaoStock artifact has a non-empty disposition")
                return self
            disposition_ids = {
                row_id for candidate in candidates for row_id in candidate.row_artifact_identities
            }
            disposition_ids.update(rejection.row_artifact_identity for rejection in rejections)
            if disposition_ids != row_ids:
                raise ValueError("BaoStock row artifact has no unique disposition")
            if any(
                item.response_artifact_identity != self.artifact.artifact_identity
                for item in self.row_artifacts
            ):
                raise ValueError("BaoStock row artifact contradicts response artifact")
        return self


class BaoStockRawDailyAdapterResult(_BaoStockAdapterResultBase):
    endpoint_scope: Literal["raw_daily"] = _RAW_SCOPE
    candidates: tuple[BaoStockRawDailyObservationCandidate, ...] = ()
    row_rejections: tuple[BaoStockRowRejection, ...] = ()


class BaoStockForwardFactorAdapterResult(_BaoStockAdapterResultBase):
    endpoint_scope: Literal["forward_adjustment_factors"] = _FACTOR_SCOPE
    candidates: tuple[BaoStockForwardFactorCandidate, ...] = ()
    row_rejections: tuple[BaoStockRowRejection, ...] = ()
    provider_history_bundle_complete: Literal[False] = False
    strict_replay_eligible: Literal[False] = False


class BaoStockSessionStatusAdapterResult(_BaoStockAdapterResultBase):
    endpoint_scope: Literal["session_status"] = _STATUS_SCOPE
    candidates: tuple[BaoStockSessionStatusCandidate, ...] = ()
    row_rejections: tuple[BaoStockRowRejection, ...] = ()
    current_tradeability: BaoStockCurrentTradeability = BaoStockCurrentTradeability.UNKNOWN


class BaoStockLifecycleAdapterResult(_BaoStockAdapterResultBase):
    endpoint_scope: Literal["issuer_lifecycle"] = _LIFECYCLE_SCOPE
    candidates: tuple[BaoStockLifecycleCandidate, ...] = ()
    row_rejections: tuple[BaoStockRowRejection, ...] = ()


@dataclass(frozen=True)
class BaoStockProviderHistoryBundleResult:
    strict_bundle_state: BaoStockStrictBundleState
    strict_outcome: BaoStockCapabilityOutcome
    raw: BaoStockRawDailyAdapterResult | None
    factors: BaoStockForwardFactorAdapterResult | None
    statuses: BaoStockSessionStatusAdapterResult | None
    provider_history_bundle_identity: str | None
    publication: ProviderHistoryBundlePublication | None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("BaoStock observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_value(value: object) -> object:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        value = value.item()
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool):
        return value
    raise TypeError("unsupported BaoStock provider value")


def _contains_credential_like_data(frame: pd.DataFrame) -> bool:
    if any(
        marker in str(column).casefold()
        for column in frame.columns
        for marker in _CREDENTIAL_FIELD_MARKERS
    ):
        return True
    return any(
        marker in value.casefold()
        for row in frame.itertuples(index=False, name=None)
        for value in row
        if isinstance(value, str)
        for marker in _CREDENTIAL_VALUE_MARKERS
    )


def _encode_artifact(
    *,
    frame: object,
    endpoint_scope: str,
    capability_identities: tuple[BaoStockCapabilityIdentity, ...],
    observed_at: datetime,
) -> bytes:
    if not isinstance(frame, pd.DataFrame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_response_not_frame",
        )
    if _contains_credential_like_data(frame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_response_unsafe",
        )
    columns = [str(column) for column in frame.columns]
    if not columns or len(columns) != len(set(columns)):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_columns_invalid",
        )
    try:
        rows = [
            {column: _json_value(value) for column, value in zip(columns, values, strict=True)}
            for values in frame.itertuples(index=False, name=None)
        ]
        payload = {
            "artifact_contract_version": BAOSTOCK_CAPABILITY_ADAPTER_VERSION,
            "provider_id": "baostock",
            "endpoint_scope": endpoint_scope,
            "dataset_id": f"baostock.{endpoint_scope}.v1",
            "schema_identity": f"baostock_{endpoint_scope}_v1",
            "capability_identities": [item.value for item in capability_identities],
            "retrieved_at": _utc(observed_at).isoformat().replace("+00:00", "Z"),
            "observed_at": _utc(observed_at).isoformat().replace("+00:00", "Z"),
            "columns": columns,
            "rows": rows,
        }
        return _canonical_json(payload).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_response_not_serializable",
        ) from None


def _decode_artifact(value: bytes, *, endpoint_scope: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("BaoStock capability artifact is malformed") from exc
    required = {
        "artifact_contract_version",
        "provider_id",
        "endpoint_scope",
        "dataset_id",
        "schema_identity",
        "capability_identities",
        "retrieved_at",
        "observed_at",
        "columns",
        "rows",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload["artifact_contract_version"] != BAOSTOCK_CAPABILITY_ADAPTER_VERSION
        or payload["provider_id"] != "baostock"
        or payload["endpoint_scope"] != endpoint_scope
        or not isinstance(payload["columns"], list)
        or not isinstance(payload["rows"], list)
        or any(
            not isinstance(row, dict) or set(row) != set(payload["columns"])
            for row in payload["rows"]
        )
    ):
        raise ValueError("BaoStock capability artifact is malformed")
    return payload


def _artifact_identity(
    *,
    key: ProviderSubrequestKey,
    provider_artifact: ProviderSubrequestArtifactRef,
    payload: Mapping[str, object],
) -> ProviderArtifactIdentity:
    dataset = ProviderDatasetIdentity.create(
        provider_id="baostock",
        endpoint_id=str(payload["endpoint_scope"]),
        dataset_id=str(payload["dataset_id"]),
        schema_identity=str(payload["schema_identity"]),
    )
    return ProviderArtifactIdentity.create(
        dataset=dataset,
        subrequest_key=key.subrequest_key,
        raw_artifact=provider_artifact,
        payload=payload["rows"],
        retrieved_at=datetime.fromisoformat(str(payload["retrieved_at"]).replace("Z", "+00:00")),
        observed_at=datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00")),
        qualification_profile=key.qualification_profile,
        normalizer_version=key.normalizer_version,
    )


def _row_artifacts(
    artifact: ProviderArtifactIdentity,
    payload: Mapping[str, object],
) -> tuple[ProviderRowArtifactIdentity, ...]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    counts: dict[str, int] = {}
    result: list[ProviderRowArtifactIdentity] = []
    for row in rows:
        canonical = _canonical_json(row)
        occurrence = counts.get(canonical, 0)
        counts[canonical] = occurrence + 1
        result.append(
            ProviderRowArtifactIdentity.create(
                response_artifact_identity=artifact.artifact_identity,
                row_payload_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
                duplicate_occurrence=occurrence,
            )
        )
    return tuple(result)


def _base_candidate(
    *,
    identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    row_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "instrument_identity": identity,
        "canonical_symbol": identity.symbol.strip().upper(),
        "dataset_identity": artifact.dataset.dataset_identity,
        "artifact_identity": artifact.artifact_identity,
        "raw_artifact": artifact.raw_artifact,
        "row_artifact_identities": tuple(sorted(row_ids)),
        "retrieved_at": artifact.retrieved_at,
        "observed_at": artifact.observed_at,
    }


def _rejection(
    *,
    reason: BaoStockRowRejectionReason,
    row: ProviderRowArtifactIdentity,
    artifact: ProviderArtifactIdentity,
    effective_date: date | None = None,
) -> BaoStockRowRejection:
    return BaoStockRowRejection(
        reason=reason,
        row_artifact_identity=row.row_artifact_identity,
        dataset_identity=artifact.dataset.dataset_identity,
        artifact_identity=artifact.artifact_identity,
        effective_date=effective_date,
    )


def _optional_decimal(value: object) -> tuple[Decimal | None, str]:
    original = "" if value is None else str(value).strip()
    if not original:
        return None, original
    try:
        parsed = Decimal(original)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid BaoStock decimal") from exc
    if not parsed.is_finite():
        raise ValueError("non-finite BaoStock decimal")
    return parsed, original


def _provider_date(value: object) -> date:
    return date.fromisoformat(str(value).strip())


def _transport_capability_outcome(
    outcome: ProviderSubrequestOutcome,
) -> BaoStockCapabilityOutcome:
    kind = {
        ProviderSubrequestOutcomeKind.AVAILABLE: BaoStockCapabilityOutcomeKind.AVAILABLE,
        ProviderSubrequestOutcomeKind.PERMISSION_DENIED: BaoStockCapabilityOutcomeKind.PERMISSION_DENIED,
        ProviderSubrequestOutcomeKind.AUTHENTICATION: BaoStockCapabilityOutcomeKind.AUTHENTICATION_FAILURE,
        ProviderSubrequestOutcomeKind.RATE_LIMITED: BaoStockCapabilityOutcomeKind.RATE_LIMITED,
        ProviderSubrequestOutcomeKind.TIMEOUT: BaoStockCapabilityOutcomeKind.TIMEOUT,
        ProviderSubrequestOutcomeKind.DISCONNECT: BaoStockCapabilityOutcomeKind.DISCONNECT,
        ProviderSubrequestOutcomeKind.EMPTY: BaoStockCapabilityOutcomeKind.EMPTY_NO_DATA,
        ProviderSubrequestOutcomeKind.MALFORMED: BaoStockCapabilityOutcomeKind.MALFORMED_RESPONSE,
    }.get(outcome.kind, BaoStockCapabilityOutcomeKind.PROVIDER_ERROR)
    return BaoStockCapabilityOutcome(kind=kind, retryable=outcome.retryable)


def _normalization_outcome(
    *,
    candidates: tuple[object, ...],
    rejections: tuple[BaoStockRowRejection, ...],
    special: BaoStockCapabilityOutcomeKind | None = None,
) -> BaoStockCapabilityOutcome:
    if special is not None:
        return BaoStockCapabilityOutcome(kind=special)
    if any(item.reason is BaoStockRowRejectionReason.INCOMPATIBLE_METADATA for item in rejections):
        return BaoStockCapabilityOutcome(kind=BaoStockCapabilityOutcomeKind.INCOMPATIBLE_METADATA)
    if rejections:
        return BaoStockCapabilityOutcome(kind=BaoStockCapabilityOutcomeKind.MALFORMED_RESPONSE)
    if candidates:
        return BaoStockCapabilityOutcome(kind=BaoStockCapabilityOutcomeKind.AVAILABLE)
    return BaoStockCapabilityOutcome(kind=BaoStockCapabilityOutcomeKind.EMPTY_NO_DATA)


class BaoStockCapabilityAdapter:
    """Coordinated BaoStock capabilities without production route activation."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        transport: BaoStockCapabilityTransport,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
    ) -> None:
        _validate_plan(routing_plan)
        self._plan = routing_plan
        self._cache = subrequest_cache
        self._transport = transport
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration

    def acquire_raw_daily(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> BaoStockRawDailyAdapterResult:
        key, symbol = self._key(
            instrument_identity=instrument_identity,
            endpoint_scope=_RAW_SCOPE,
            range_start=range_start,
            as_of_date=as_of_date,
            fields=(
                "amount",
                "close",
                "code",
                "date",
                "high",
                "low",
                "open",
                "preclose",
                "volume",
            ),
        )
        result = self._execute(
            key=key,
            operation="baostock_raw_daily",
            capabilities=(BaoStockCapabilityIdentity.RAW_DAILY_OBSERVATIONS,),
            request=lambda: self._transport.query_raw_daily(
                code=symbol,
                start_date=range_start.isoformat(),
                end_date=as_of_date.isoformat(),
            ),
        )
        if result.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return BaoStockRawDailyAdapterResult(
                request=key,
                subrequest_key=key.subrequest_key,
                outcome=result.outcome,
                capability_outcome=_transport_capability_outcome(result.outcome),
                sequence_id=result.sequence_id,
                attempt_events=result.attempt_events,
            )
        artifact, rows, payload = self._decoded_result(key, result)
        candidates, rejections = _raw_candidates(
            rows=rows,
            payload=payload,
            identity=instrument_identity,
            artifact=artifact,
            provider_code=symbol,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        return BaoStockRawDailyAdapterResult(
            request=key,
            subrequest_key=key.subrequest_key,
            provider_artifact=result.artifact,
            artifact=artifact,
            row_artifacts=rows,
            outcome=result.outcome,
            capability_outcome=_normalization_outcome(candidates=candidates, rejections=rejections),
            sequence_id=result.sequence_id,
            attempt_events=result.attempt_events,
            candidates=candidates,
            row_rejections=rejections,
        )

    def acquire_forward_adjustment_factors(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> BaoStockForwardFactorAdapterResult:
        key, symbol = self._key(
            instrument_identity=instrument_identity,
            endpoint_scope=_FACTOR_SCOPE,
            range_start=range_start,
            requested_range_start="all-history",
            as_of_date=as_of_date,
            fields=("code", "dividOperateDate", "foreAdjustFactor"),
        )
        result = self._execute(
            key=key,
            operation="baostock_forward_adjustment_factors",
            capabilities=(BaoStockCapabilityIdentity.FORWARD_ADJUSTMENT_FACTORS,),
            request=lambda: self._transport.query_forward_adjustment_factors(
                code=symbol,
                start_date=None,
                end_date=as_of_date.isoformat(),
            ),
        )
        if result.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return BaoStockForwardFactorAdapterResult(
                request=key,
                subrequest_key=key.subrequest_key,
                outcome=result.outcome,
                capability_outcome=_transport_capability_outcome(result.outcome),
                sequence_id=result.sequence_id,
                attempt_events=result.attempt_events,
            )
        artifact, rows, payload = self._decoded_result(key, result)
        candidates, rejections, conflict = _factor_candidates(
            rows=rows,
            payload=payload,
            identity=instrument_identity,
            artifact=artifact,
            provider_code=symbol,
            as_of_date=as_of_date,
        )
        return BaoStockForwardFactorAdapterResult(
            request=key,
            subrequest_key=key.subrequest_key,
            provider_artifact=result.artifact,
            artifact=artifact,
            row_artifacts=rows,
            outcome=result.outcome,
            capability_outcome=_normalization_outcome(
                candidates=candidates,
                rejections=rejections,
                special=(BaoStockCapabilityOutcomeKind.CONFLICTING_FACTOR if conflict else None),
            ),
            sequence_id=result.sequence_id,
            attempt_events=result.attempt_events,
            candidates=candidates,
            row_rejections=rejections,
        )

    def acquire_session_status(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> BaoStockSessionStatusAdapterResult:
        key, symbol = self._key(
            instrument_identity=instrument_identity,
            endpoint_scope=_STATUS_SCOPE,
            range_start=range_start,
            as_of_date=as_of_date,
            fields=("code", "date", "isST", "preclose", "tradestatus"),
        )
        result = self._execute(
            key=key,
            operation="baostock_session_status",
            capabilities=(
                BaoStockCapabilityIdentity.SESSION_TRADING_STATUS,
                BaoStockCapabilityIdentity.DATED_ST_STATUS,
            ),
            request=lambda: self._transport.query_session_status(
                code=symbol,
                start_date=range_start.isoformat(),
                end_date=as_of_date.isoformat(),
            ),
        )
        if result.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return BaoStockSessionStatusAdapterResult(
                request=key,
                subrequest_key=key.subrequest_key,
                outcome=result.outcome,
                capability_outcome=_transport_capability_outcome(result.outcome),
                sequence_id=result.sequence_id,
                attempt_events=result.attempt_events,
            )
        artifact, rows, payload = self._decoded_result(key, result)
        candidates, rejections, contradiction = _status_candidates(
            rows=rows,
            payload=payload,
            identity=instrument_identity,
            artifact=artifact,
            provider_code=symbol,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        capability_outcome = _normalization_outcome(
            candidates=candidates,
            rejections=rejections,
            special=(BaoStockCapabilityOutcomeKind.CONTRADICTORY_STATUS if contradiction else None),
        )
        tradeability = BaoStockCurrentTradeability.UNKNOWN
        if (
            candidates
            and capability_outcome.kind is BaoStockCapabilityOutcomeKind.AVAILABLE
            and candidates[-1].session_date == as_of_date
        ):
            latest = candidates[-1].trading_status
            if latest is TradingStatus.TRADED:
                tradeability = BaoStockCurrentTradeability.TRADEABLE
            elif latest is TradingStatus.SUSPENDED:
                tradeability = BaoStockCurrentTradeability.SUSPENDED
        return BaoStockSessionStatusAdapterResult(
            request=key,
            subrequest_key=key.subrequest_key,
            provider_artifact=result.artifact,
            artifact=artifact,
            row_artifacts=rows,
            outcome=result.outcome,
            capability_outcome=capability_outcome,
            sequence_id=result.sequence_id,
            attempt_events=result.attempt_events,
            candidates=candidates,
            row_rejections=rejections,
            current_tradeability=tradeability,
        )

    def acquire_lifecycle(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        as_of_date: date,
    ) -> BaoStockLifecycleAdapterResult:
        key, symbol = self._key(
            instrument_identity=instrument_identity,
            endpoint_scope=_LIFECYCLE_SCOPE,
            range_start=as_of_date,
            as_of_date=as_of_date,
            fields=("code", "code_name", "ipoDate", "outDate", "status", "type"),
        )
        result = self._execute(
            key=key,
            operation="baostock_issuer_lifecycle",
            capabilities=(BaoStockCapabilityIdentity.ISSUER_LIFECYCLE,),
            request=lambda: self._transport.query_lifecycle(code=symbol),
        )
        if result.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return BaoStockLifecycleAdapterResult(
                request=key,
                subrequest_key=key.subrequest_key,
                outcome=result.outcome,
                capability_outcome=_transport_capability_outcome(result.outcome),
                sequence_id=result.sequence_id,
                attempt_events=result.attempt_events,
            )
        artifact, rows, payload = self._decoded_result(key, result)
        candidates, rejections = _lifecycle_candidates(
            rows=rows,
            payload=payload,
            identity=instrument_identity,
            artifact=artifact,
            provider_code=symbol,
            as_of_date=as_of_date,
        )
        return BaoStockLifecycleAdapterResult(
            request=key,
            subrequest_key=key.subrequest_key,
            provider_artifact=result.artifact,
            artifact=artifact,
            row_artifacts=rows,
            outcome=result.outcome,
            capability_outcome=_normalization_outcome(candidates=candidates, rejections=rejections),
            sequence_id=result.sequence_id,
            attempt_events=result.attempt_events,
            candidates=candidates,
            row_rejections=rejections,
        )

    def acquire_provider_history_bundle(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> BaoStockProviderHistoryBundleResult:
        raw = self.acquire_raw_daily(
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        factors = self.acquire_forward_adjustment_factors(
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        statuses = self.acquire_session_status(
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        return assemble_baostock_history_bundle(
            instrument_identity=instrument_identity,
            as_of_date=as_of_date,
            raw=raw,
            factors=factors,
            statuses=statuses,
        )

    def _key(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        endpoint_scope: str,
        range_start: date,
        requested_range_start: str | None = None,
        as_of_date: date,
        fields: tuple[str, ...],
    ) -> tuple[ProviderSubrequestKey, str]:
        provider_code = _validate_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("BaoStock range must not end before it starts")
        upstream_id, _ = upstream_service_identity_for_provider("baostock")
        key = ProviderSubrequestKey.create(
            provider_id="baostock",
            upstream_service_id=upstream_id,
            account_scope="not-applicable",
            capacity_scope=endpoint_scope,
            instrument_identity=instrument_identity,
            requested_range_start=requested_range_start or range_start.isoformat(),
            requested_range_end=as_of_date.isoformat(),
            requested_fields=fields,
            as_of_date=as_of_date,
            qualification_profile=self._plan.qualification_profile,
            normalizer_version=BAOSTOCK_CAPABILITY_NORMALIZER_VERSION,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        )
        return key, provider_code

    def _execute(
        self,
        *,
        key: ProviderSubrequestKey,
        operation: str,
        capabilities: tuple[BaoStockCapabilityIdentity, ...],
        request: Callable[[], pd.DataFrame],
    ):
        def physical_request() -> bytes:
            try:
                frame = request()
            except PhysicalAttemptFailure:
                raise
            except Exception as exc:
                raise _sanitized_transport_failure(exc) from None
            return _encode_artifact(
                frame=frame,
                endpoint_scope=key.capacity_scope,
                capability_identities=capabilities,
                observed_at=_utc(self._now()),
            )

        return self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation=operation,
            media_type="application/json",
            physical_request=physical_request,
        )

    @staticmethod
    def _decoded_result(key, result):
        assert result.value is not None
        assert result.artifact is not None
        payload = _decode_artifact(result.value, endpoint_scope=key.capacity_scope)
        artifact = _artifact_identity(
            key=key,
            provider_artifact=result.artifact,
            payload=payload,
        )
        return artifact, _row_artifacts(artifact, payload), payload


def _raw_candidates(
    *,
    rows: tuple[ProviderRowArtifactIdentity, ...],
    payload: Mapping[str, object],
    identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    provider_code: str,
    range_start: date,
    as_of_date: date,
) -> tuple[
    tuple[BaoStockRawDailyObservationCandidate, ...],
    tuple[BaoStockRowRejection, ...],
]:
    payload_rows = payload["rows"]
    assert isinstance(payload_rows, list)
    parsed: list[tuple[date, dict[str, object], ProviderRowArtifactIdentity]] = []
    rejections: list[BaoStockRowRejection] = []
    for value, row_artifact in zip(payload_rows, rows, strict=True):
        assert isinstance(value, dict)
        try:
            session_date = _provider_date(value["date"])
            decimals = {
                field: _optional_decimal(value.get(field))
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "preclose",
                    "volume",
                    "amount",
                )
            }
            candidate = BaoStockRawDailyObservationCandidate(
                **_base_candidate(
                    identity=identity,
                    artifact=artifact,
                    row_ids=(row_artifact.row_artifact_identity,),
                ),
                session_date=session_date,
                **{field: item[0] for field, item in decimals.items()},
                **{f"original_{field}": item[1] for field, item in decimals.items()},
            )
        except (KeyError, TypeError, ValueError):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.MALFORMED_RESPONSE,
                    row=row_artifact,
                    artifact=artifact,
                )
            )
            continue
        if (
            value.get("code") != provider_code
            or session_date < range_start
            or session_date > as_of_date
        ):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=row_artifact,
                    artifact=artifact,
                    effective_date=session_date,
                )
            )
            continue
        parsed.append((session_date, candidate.model_dump(mode="json"), row_artifact))
    candidates: list[BaoStockRawDailyObservationCandidate] = []
    for session_date in sorted({item[0] for item in parsed}):
        dated = [item for item in parsed if item[0] == session_date]
        material = {
            _canonical_json(
                {
                    key: value
                    for key, value in item[1].items()
                    if key
                    not in {
                        "row_artifact_identities",
                        "raw_artifact",
                    }
                }
            )
            for item in dated
        }
        if len(material) != 1:
            rejections.extend(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=item[2],
                    artifact=artifact,
                    effective_date=session_date,
                )
                for item in dated
            )
            continue
        first = dated[0][1]
        first["row_artifact_identities"] = tuple(item[2].row_artifact_identity for item in dated)
        candidates.append(BaoStockRawDailyObservationCandidate.model_validate(first))
    return tuple(candidates), tuple(rejections)


def _factor_candidates(
    *,
    rows: tuple[ProviderRowArtifactIdentity, ...],
    payload: Mapping[str, object],
    identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    provider_code: str,
    as_of_date: date,
) -> tuple[
    tuple[BaoStockForwardFactorCandidate, ...],
    tuple[BaoStockRowRejection, ...],
    bool,
]:
    payload_rows = payload["rows"]
    assert isinstance(payload_rows, list)
    parsed: list[tuple[date, Decimal, str, ProviderRowArtifactIdentity]] = []
    rejections: list[BaoStockRowRejection] = []
    for value, row_artifact in zip(payload_rows, rows, strict=True):
        assert isinstance(value, dict)
        try:
            effective_date = _provider_date(value["dividOperateDate"])
            factor, original = _optional_decimal(value["foreAdjustFactor"])
            if factor is None or factor <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.MALFORMED_RESPONSE,
                    row=row_artifact,
                    artifact=artifact,
                )
            )
            continue
        if value.get("code") != provider_code or effective_date > as_of_date:
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=row_artifact,
                    artifact=artifact,
                    effective_date=effective_date,
                )
            )
            continue
        parsed.append((effective_date, factor, original, row_artifact))
    candidates: list[BaoStockForwardFactorCandidate] = []
    conflict = False
    for effective_date in sorted({item[0] for item in parsed}):
        dated = [item for item in parsed if item[0] == effective_date]
        if len({item[1] for item in dated}) != 1:
            conflict = True
            rejections.extend(
                _rejection(
                    reason=BaoStockRowRejectionReason.CONFLICTING_FACTOR,
                    row=item[3],
                    artifact=artifact,
                    effective_date=effective_date,
                )
                for item in dated
            )
            continue
        first = dated[0]
        candidates.append(
            BaoStockForwardFactorCandidate(
                **_base_candidate(
                    identity=identity,
                    artifact=artifact,
                    row_ids=tuple(item[3].row_artifact_identity for item in dated),
                ),
                effective_date=effective_date,
                factor=first[1],
                original_factor=first[2],
            )
        )
    return tuple(candidates), tuple(rejections), conflict


def _status_candidates(
    *,
    rows: tuple[ProviderRowArtifactIdentity, ...],
    payload: Mapping[str, object],
    identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    provider_code: str,
    range_start: date,
    as_of_date: date,
) -> tuple[
    tuple[BaoStockSessionStatusCandidate, ...],
    tuple[BaoStockRowRejection, ...],
    bool,
]:
    payload_rows = payload["rows"]
    assert isinstance(payload_rows, list)
    parsed: list[
        tuple[
            date,
            TradingStatus | None,
            bool | None,
            Decimal | None,
            str,
            str,
            str,
            ProviderRowArtifactIdentity,
        ]
    ] = []
    rejections: list[BaoStockRowRejection] = []
    for value, row_artifact in zip(payload_rows, rows, strict=True):
        assert isinstance(value, dict)
        try:
            session_date = _provider_date(value["date"])
            original_status = str(value.get("tradestatus") or "").strip()
            original_st = str(value.get("isST") or "").strip()
            if original_status not in {"", "0", "1"} or original_st not in {
                "",
                "0",
                "1",
            }:
                raise ValueError
            status = {
                "1": TradingStatus.TRADED,
                "0": TradingStatus.SUSPENDED,
                "": None,
            }[original_status]
            is_st = {"1": True, "0": False, "": None}[original_st]
            carried, original_preclose = _optional_decimal(value.get("preclose"))
            if status is TradingStatus.SUSPENDED and carried is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.MALFORMED_RESPONSE,
                    row=row_artifact,
                    artifact=artifact,
                )
            )
            continue
        if (
            value.get("code") != provider_code
            or session_date < range_start
            or session_date > as_of_date
        ):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=row_artifact,
                    artifact=artifact,
                    effective_date=session_date,
                )
            )
            continue
        parsed.append(
            (
                session_date,
                status,
                is_st,
                carried,
                original_status,
                original_st,
                original_preclose,
                row_artifact,
            )
        )
    candidates: list[BaoStockSessionStatusCandidate] = []
    contradiction = False
    for session_date in sorted({item[0] for item in parsed}):
        dated = [item for item in parsed if item[0] == session_date]
        if len({item[1:7] for item in dated}) != 1:
            contradiction = True
            rejections.extend(
                _rejection(
                    reason=BaoStockRowRejectionReason.CONTRADICTORY_STATUS,
                    row=item[7],
                    artifact=artifact,
                    effective_date=session_date,
                )
                for item in dated
            )
            continue
        first = dated[0]
        candidates.append(
            BaoStockSessionStatusCandidate(
                **_base_candidate(
                    identity=identity,
                    artifact=artifact,
                    row_ids=tuple(item[7].row_artifact_identity for item in dated),
                ),
                session_date=session_date,
                trading_status=first[1],
                is_st=first[2],
                official_carried_close=(first[3] if first[1] is TradingStatus.SUSPENDED else None),
                original_tradestatus=first[4],
                original_is_st=first[5],
                original_preclose=first[6],
                trading_status_authoritative=first[1] is not None,
                st_status_authoritative=first[2] is not None,
            )
        )
    return tuple(candidates), tuple(rejections), contradiction


def _lifecycle_candidates(
    *,
    rows: tuple[ProviderRowArtifactIdentity, ...],
    payload: Mapping[str, object],
    identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    provider_code: str,
    as_of_date: date,
) -> tuple[
    tuple[BaoStockLifecycleCandidate, ...],
    tuple[BaoStockRowRejection, ...],
]:
    payload_rows = payload["rows"]
    assert isinstance(payload_rows, list)
    parsed: list[BaoStockLifecycleCandidate] = []
    rejections: list[BaoStockRowRejection] = []
    for value, row_artifact in zip(payload_rows, rows, strict=True):
        assert isinstance(value, dict)
        try:
            original_ipo = str(value.get("ipoDate") or "").strip()
            original_out = str(value.get("outDate") or "").strip()
            original_type = str(value.get("type") or "").strip()
            original_status = str(value.get("status") or "").strip()
            if value.get("code") != provider_code or original_type != "1":
                raise LookupError
            if original_status not in {"", "0", "1"}:
                raise ValueError
            ipo_date = date.fromisoformat(original_ipo) if original_ipo else None
            out_date = date.fromisoformat(original_out) if original_out else None
            if ipo_date is not None and ipo_date > as_of_date:
                raise ValueError
            if out_date is not None and out_date < (ipo_date or date.min):
                raise ValueError
            listing_status = {
                "1": BaoStockListingStatus.LISTED,
                "0": BaoStockListingStatus.DELISTED,
                "": BaoStockListingStatus.UNKNOWN,
            }[original_status]
            if listing_status is BaoStockListingStatus.DELISTED and out_date is None:
                raise ValueError
            parsed.append(
                BaoStockLifecycleCandidate(
                    **_base_candidate(
                        identity=identity,
                        artifact=artifact,
                        row_ids=(row_artifact.row_artifact_identity,),
                    ),
                    ipo_date=ipo_date,
                    listing_date=ipo_date,
                    out_date=out_date,
                    delisting_date=(
                        out_date if listing_status is BaoStockListingStatus.DELISTED else None
                    ),
                    listing_status=listing_status,
                    original_ipo_date=original_ipo,
                    original_out_date=original_out,
                    original_type=original_type,
                    original_status=original_status,
                    observed_name=(
                        str(value.get("code_name")).strip()
                        if value.get("code_name") is not None
                        else None
                    ),
                )
            )
        except LookupError:
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=row_artifact,
                    artifact=artifact,
                )
            )
        except (TypeError, ValueError):
            rejections.append(
                _rejection(
                    reason=BaoStockRowRejectionReason.MALFORMED_RESPONSE,
                    row=row_artifact,
                    artifact=artifact,
                )
            )
    if len(parsed) > 1:
        canonical = {
            _canonical_json(
                item.model_dump(
                    mode="json",
                    exclude={"row_artifact_identities", "raw_artifact"},
                )
            )
            for item in parsed
        }
        if len(canonical) != 1:
            rejections.extend(
                _rejection(
                    reason=BaoStockRowRejectionReason.INCOMPATIBLE_METADATA,
                    row=row,
                    artifact=artifact,
                )
                for row in rows
            )
            return (), tuple(rejections)
        first = parsed[0].model_dump()
        first["row_artifact_identities"] = tuple(
            item for candidate in parsed for item in candidate.row_artifact_identities
        )
        parsed = [BaoStockLifecycleCandidate.model_validate(first)]
    return tuple(parsed), tuple(rejections)


def assemble_baostock_history_bundle(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    as_of_date: date,
    raw: BaoStockRawDailyAdapterResult | None,
    factors: BaoStockForwardFactorAdapterResult | None,
    statuses: BaoStockSessionStatusAdapterResult | None,
) -> BaoStockProviderHistoryBundleResult:
    """Atomically bind exact BaoStock raw/factor/status membership."""

    incomplete = BaoStockProviderHistoryBundleResult(
        strict_bundle_state=BaoStockStrictBundleState.INCOMPLETE,
        strict_outcome=BaoStockCapabilityOutcome(
            kind=BaoStockCapabilityOutcomeKind.INCOMPLETE_STRICT_BUNDLE
        ),
        raw=raw,
        factors=factors,
        statuses=statuses,
        provider_history_bundle_identity=None,
        publication=None,
    )
    if (
        not isinstance(raw, BaoStockRawDailyAdapterResult)
        or not isinstance(factors, BaoStockForwardFactorAdapterResult)
        or not isinstance(statuses, BaoStockSessionStatusAdapterResult)
        or any(
            item.capability_outcome.kind is not BaoStockCapabilityOutcomeKind.AVAILABLE
            for item in (raw, factors, statuses)
        )
        or not raw.candidates
        or not factors.candidates
        or not statuses.candidates
        or raw.artifact is None
        or factors.artifact is None
        or statuses.artifact is None
        or raw.provider_artifact is None
        or factors.provider_artifact is None
        or statuses.provider_artifact is None
        or raw.row_rejections
        or factors.row_rejections
        or statuses.row_rejections
    ):
        return incomplete

    components = (raw, factors, statuses)
    expected_scopes = (_RAW_SCOPE, _FACTOR_SCOPE, _STATUS_SCOPE)
    try:
        expected_identity = ProviderSubrequestInstrumentIdentity.from_authoritative(
            instrument_identity
        )
    except ValueError:
        return incomplete
    requests = tuple(item.request for item in components)
    if (
        any(request.instrument_identity != expected_identity for request in requests)
        or any(
            request.capacity_scope != scope
            or item.endpoint_scope != scope
            or item.artifact is None
            or item.provider_artifact is None
            or item.artifact.dataset.provider_id != "baostock"
            or item.artifact.dataset.endpoint_id != scope
            or item.artifact.subrequest_key != request.subrequest_key
            or item.artifact.raw_artifact != item.provider_artifact
            for item, request, scope in zip(
                components,
                requests,
                expected_scopes,
                strict=True,
            )
        )
        or len(
            {
                (
                    request.upstream_service_id,
                    request.account_scope,
                    request.qualification_profile,
                    request.normalizer_version,
                    request.data_usage_mode,
                )
                for request in requests
            }
        )
        != 1
        or raw.request.requested_range_start
        != statuses.request.requested_range_start
        or raw.request.requested_range_end != statuses.request.requested_range_end
        or factors.request.requested_range_start != "all-history"
        or any(request.requested_range_end != as_of_date.isoformat() for request in requests)
        or any(request.as_of_date != as_of_date for request in requests)
    ):
        return incomplete

    for item in components:
        assert item.artifact is not None
        assert item.provider_artifact is not None
        if any(
            candidate.instrument_identity != instrument_identity
            or candidate.canonical_symbol != expected_identity.canonical_symbol
            or candidate.dataset_identity != item.artifact.dataset.dataset_identity
            or candidate.artifact_identity != item.artifact.artifact_identity
            or candidate.raw_artifact != item.provider_artifact
            or candidate.retrieved_at != item.artifact.retrieved_at
            or candidate.observed_at != item.artifact.observed_at
            for candidate in item.candidates
        ):
            return incomplete

    observation_dates = tuple(item.session_date for item in raw.candidates)
    status_dates = tuple(item.session_date for item in statuses.candidates)
    if (
        observation_dates != tuple(sorted(set(observation_dates)))
        or status_dates != observation_dates
        or any(item.trading_status is None for item in statuses.candidates)
    ):
        return incomplete
    factor_dates = tuple(item.effective_date for item in factors.candidates)
    if factor_dates != tuple(sorted(set(factor_dates))) or factor_dates[0] > observation_dates[0]:
        return incomplete

    observations = []
    trading_statuses = []
    for raw_item, status_item in zip(raw.candidates, statuses.candidates, strict=True):
        assert status_item.trading_status is not None
        try:
            normalized = normalize_mainland_session(
                session_date=raw_item.session_date,
                open_value=raw_item.open,
                high_value=raw_item.high,
                low_value=raw_item.low,
                close_value=raw_item.close,
                volume=raw_item.volume,
                authoritative_status=status_item.trading_status,
                official_carried_close=status_item.official_carried_close,
            )
        except (TypeError, ValueError):
            return incomplete
        observations.append(normalized.observation)
        trading_statuses.append(normalized.status)
    factor_observations = tuple(
        AdjustmentFactorObservation(item.effective_date, item.factor) for item in factors.candidates
    )

    resolved = resolve_mainland_instrument(instrument_identity.symbol)
    if resolved is None or resolved.instrument_kind != "equity":
        return incomplete
    upstream_id, service_name = upstream_service_identity_for_provider("baostock")
    if requests[0].upstream_service_id != upstream_id:
        return incomplete
    retrieval_cutoff = max(item.artifact.retrieved_at for item in components)
    observed_at = max(item.artifact.observed_at for item in components)
    bundle_dataset_id = _history_identity(
        "provider-dataset",
        upstream_id,
        "mainland-raw-status-factors-v1",
    )
    identity_payload = {
        "contract_version": "baostock-provider-history-bundle-v1",
        "provider_id": "baostock",
        "provider_dataset_id": bundle_dataset_id,
        "instrument_symbol": instrument_identity.symbol.strip().upper(),
        "as_of_date": as_of_date.isoformat(),
        "raw_subrequest_key": raw.request.subrequest_key,
        "factor_subrequest_key": factors.request.subrequest_key,
        "status_subrequest_key": statuses.request.subrequest_key,
        "raw_provider_artifact": raw.provider_artifact.model_dump(mode="json"),
        "factor_provider_artifact": factors.provider_artifact.model_dump(mode="json"),
        "status_provider_artifact": statuses.provider_artifact.model_dump(mode="json"),
        "raw_artifact_identity": raw.artifact.artifact_identity,
        "factor_artifact_identity": factors.artifact.artifact_identity,
        "status_artifact_identity": statuses.artifact.artifact_identity,
        "observation_dates": [item.isoformat() for item in observation_dates],
        "factor_dates": [item.isoformat() for item in factor_dates],
    }
    bundle_identity = f"baostock-provider-history-bundle:v1:{_digest(identity_payload)}"
    binding_manifest = {
        **identity_payload,
        "provider_history_bundle_identity": bundle_identity,
    }
    publication = ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id=upstream_id,
            upstream_service_name=service_name,
            provider_dataset_id=bundle_dataset_id,
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id=_history_identity(
                "instrument",
                resolved.yahoo_symbol,
                resolved.exchange,
                resolved.instrument_kind,
                "CNY",
            ),
            canonical_symbol=resolved.yahoo_symbol,
            reference_market="mainland-cn",
            instrument_kind=resolved.instrument_kind,
            currency="CNY",
            identity_revision="mainland-routing-v1",
        ),
        requested_as_of=as_of_date,
        retrieval_cutoff=retrieval_cutoff,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=_canonical_json(binding_manifest).encode("utf-8"),
        observations=tuple(observations),
        trading_statuses=tuple(trading_statuses),
        adjustment_factors=factor_observations,
    )
    return BaoStockProviderHistoryBundleResult(
        strict_bundle_state=BaoStockStrictBundleState.COMPLETE,
        strict_outcome=BaoStockCapabilityOutcome(kind=BaoStockCapabilityOutcomeKind.AVAILABLE),
        raw=raw,
        factors=factors,
        statuses=statuses,
        provider_history_bundle_identity=bundle_identity,
        publication=publication,
    )


def _validate_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) != (
        "akshare",
        "baostock",
        "yfinance",
    ):
        raise ValueError("BaoStock capability adapter requires the unchanged daily route")
    if plan.route_for(MainlandCapability.ADJUSTMENT_FACTORS)[0] != "baostock":
        raise ValueError("BaoStock must remain first for adjustment factors")
    if plan.route_for(MainlandCapability.SUSPENSION_STATUS) != ("baostock",):
        raise ValueError("BaoStock must remain authoritative for session status")
    if plan.route_for(MainlandCapability.ISSUER_LIFECYCLE) != ("baostock",):
        raise ValueError("BaoStock must remain authoritative for issuer lifecycle")


def _validate_identity(identity: InstrumentIdentityEvidence) -> str:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("BaoStock capabilities require a mainland Equity identity")
    resolved = resolve_mainland_instrument(identity.symbol)
    if resolved is None or resolved.instrument_kind != "equity":
        raise ValueError("BaoStock Instrument Identity is not provider-compatible")
    return resolved.baostock_code


def _history_identity(namespace: str, *components: object) -> str:
    digest = sha256()
    for component in (namespace, *components):
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{namespace}=sha256:{digest.hexdigest()}"


def _sanitized_transport_failure(exc: Exception) -> PhysicalAttemptFailure:
    type_name = type(exc).__name__.casefold()
    message = str(exc)[:512].casefold()
    provider_error_code = str(getattr(exc, "error_code", "") or "").strip().casefold()
    retry_after = _retry_after_seconds(exc)
    if isinstance(exc, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code="baostock_timeout",
        )
    if provider_error_code == "10001005" or any(
        marker in message for marker in ("10001005", "rate limit", "too many", "capacity")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
            error_code="baostock_rate_limited",
            rate_limit_scope=(
                RateLimitScope.UPSTREAM
                if provider_error_code == "10001005"
                or "10001005" in message
                or _upstream_rate_limit(exc)
                else RateLimitScope.CAPACITY
            ),
        )
    if any(marker in message for marker in ("permission", "access denied", "权限")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="baostock_permission_denied",
        )
    if any(marker in message for marker in ("authentication", "auth", "login failed", "认证")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="baostock_authentication",
        )
    if any(
        marker in message
        for marker in ("disconnect", "connection reset", "connection aborted", "socket")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
            error_code="baostock_disconnect",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code="baostock_provider_error",
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if value is None:
        value = getattr(exc, "retry_after", None)
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _upstream_rate_limit(exc: Exception) -> bool:
    value = getattr(exc, "rate_limit_scope", None)
    return value is RateLimitScope.UPSTREAM or (
        isinstance(value, str) and value.strip().casefold() in {"account", "global", "upstream"}
    )


__all__ = [
    "BAOSTOCK_CAPABILITY_ADAPTER_VERSION",
    "BAOSTOCK_CAPABILITY_NORMALIZER_VERSION",
    "BaoStockCapabilityAdapter",
    "BaoStockCapabilityIdentity",
    "BaoStockCapabilityOutcome",
    "BaoStockCapabilityOutcomeKind",
    "BaoStockCapabilityTransport",
    "BaoStockCurrentTradeability",
    "BaoStockForwardFactorAdapterResult",
    "BaoStockForwardFactorCandidate",
    "BaoStockLifecycleAdapterResult",
    "BaoStockLifecycleCandidate",
    "BaoStockListingStatus",
    "BaoStockProviderHistoryBundleResult",
    "BaoStockRawDailyAdapterResult",
    "BaoStockRawDailyObservationCandidate",
    "BaoStockRowRejection",
    "BaoStockRowRejectionReason",
    "BaoStockSessionStatusAdapterResult",
    "BaoStockSessionStatusCandidate",
    "BaoStockStrictBundleState",
    "assemble_baostock_history_bundle",
]
