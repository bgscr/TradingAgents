"""Qualified Tushare factor and issuer-name adapters.

These adapters expose Ticket 06 candidates only. Ticket 10 owns production
routing and selection; no adapter output is admitted as a Source Fact here.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
    TushareCapability,
)
from tradingagents.dataflows._tushare_statement_artifacts import (
    decode_provider_artifact,
    encode_provider_artifact,
    require_utc,
)
from tradingagents.dataflows.factor_name_contracts import (
    AdjustmentFactorCandidate,
    AdjustmentFactorRowRejection,
    FactorNameRejectionReason,
    IssuerNameEventCandidate,
    IssuerNameEventRowRejection,
    ProviderArtifactIdentity,
    ProviderDatasetIdentity,
    ProviderRowArtifactIdentity,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactRef,
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
    ProviderSubrequestOutcome,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
)

TUSHARE_ADJUSTMENT_FACTOR_ADAPTER_VERSION = "tushare-adjustment-factor-adapter-v1"
TUSHARE_NAME_EVENT_ADAPTER_VERSION = "tushare-name-event-adapter-v1"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_FACTOR_ENDPOINT = "adj_factor"
_FACTOR_ARTIFACT_METADATA = {
    "capability": "native_adjustment_factor_revision",
    "authority": "qualified_current_factor_fallback_only",
}
_NAME_ENDPOINT = "namechange"
_NAME_ARTIFACT_METADATA = {
    "capability": "issuer_name_event",
    "authority": "name_only_no_status_or_lifecycle",
}
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
    "tushare_token",
)


class TushareFactorNameSdkClient(Protocol):
    def adj_factor(self, **kwargs: object) -> pd.DataFrame: ...

    def namechange(self, **kwargs: object) -> pd.DataFrame: ...


class TushareFactorNameAdapterFailureReason(str, Enum):
    ADJUSTMENT_FACTORS_NOT_ENABLED = "tushare_adjustment_factors_not_enabled"
    ADJUSTMENT_FACTORS_PLAN_INCOHERENT = (
        "tushare_adjustment_factors_plan_incoherent"
    )
    NAME_EVENTS_NOT_ENABLED = "tushare_name_events_not_enabled"
    NAME_EVENTS_PLAN_INCOHERENT = "tushare_name_events_plan_incoherent"


class TushareFactorNameAdapterConfigurationError(ValueError):
    """Typed secret-free construction failure raised before provider work."""

    def __init__(self, reason: TushareFactorNameAdapterFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class TushareAdjustmentFactorAdapterResult(BaseModel):
    """Payload-free native factor candidates and immutable artifact references."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-adjustment-factor-adapter-v1"] = (
        TUSHARE_ADJUSTMENT_FACTOR_ADAPTER_VERSION
    )
    endpoint_id: Literal["adj_factor"] = _FACTOR_ENDPOINT
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: ProviderArtifactIdentity | None = None
    row_artifacts: tuple[ProviderRowArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    candidates: tuple[AdjustmentFactorCandidate, ...] = ()
    row_rejections: tuple[AdjustmentFactorRowRejection, ...] = ()
    raw_observation_history_established: Literal[False] = False
    session_trading_status_established: Literal[False] = False
    provider_history_bundle_complete: Literal[False] = False
    strict_replay_eligible: Literal[False] = False
    current_tradeability_established: Literal[False] = False
    source_facts_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_result(self) -> TushareAdjustmentFactorAdapterResult:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("Tushare factor artifact contradicts acquisition outcome")
        if not available and (self.row_artifacts or self.candidates or self.row_rejections):
            raise ValueError("unavailable Tushare factor cannot emit candidates")
        row_ids = {item.row_artifact_identity for item in self.row_artifacts}
        used_ids = {
            occurrence
            for candidate in self.candidates
            for occurrence in candidate.occurrence_artifact_identities
        }
        used_ids.update(item.row_artifact_identity for item in self.row_rejections)
        if available and (not row_ids or used_ids != row_ids):
            raise ValueError("Tushare factor row artifact has no disposition")
        return self


class TushareAdjustmentFactorAdapter:
    """Acquire one qualified native Tushare adjustment-factor history."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        sdk_client: TushareFactorNameSdkClient,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
    ) -> None:
        _validate_factor_plan(routing_plan)
        self._routing_plan = routing_plan
        self._cache = subrequest_cache
        self._sdk_client = sdk_client
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration

    def acquire_adjustment_factors(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareAdjustmentFactorAdapterResult:
        _validate_instrument_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("Tushare factor range must not end before it starts")
        key = _subrequest_key(
            routing_plan=self._routing_plan,
            endpoint_id=_FACTOR_ENDPOINT,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

        def physical_request() -> bytes:
            try:
                frame = self._sdk_client.adj_factor(
                    ts_code=_tushare_symbol(instrument_identity),
                    start_date=range_start.strftime("%Y%m%d"),
                    end_date=as_of_date.strftime("%Y%m%d"),
                )
            except Exception as exc:
                raise _sanitized_sdk_failure(exc) from None
            return _encoded_frame(
                frame=frame,
                endpoint_id=_FACTOR_ENDPOINT,
                now=self._now,
                artifact_contract_version=TUSHARE_ADJUSTMENT_FACTOR_ADAPTER_VERSION,
                adapter_metadata=_FACTOR_ARTIFACT_METADATA,
            )

        subrequest = self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation="tushare_adjustment_factor",
            media_type="application/json",
            physical_request=physical_request,
        )
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return TushareAdjustmentFactorAdapterResult(
                subrequest_key=key.subrequest_key,
                outcome=subrequest.outcome,
                sequence_id=subrequest.sequence_id,
                attempt_events=subrequest.attempt_events,
            )
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = decode_provider_artifact(
            subrequest.value,
            endpoint_id=_FACTOR_ENDPOINT,
            artifact_contract_version=TUSHARE_ADJUSTMENT_FACTOR_ADAPTER_VERSION,
            adapter_metadata=_FACTOR_ARTIFACT_METADATA,
        )
        artifact = _artifact_identity(
            key=key,
            provider_artifact=subrequest.artifact,
            payload=payload,
        )
        row_artifacts = _row_artifact_identities(
            response_artifact=artifact,
            payload=payload,
        )
        candidates, row_rejections = _factor_candidates(
            payload=payload,
            row_artifacts=row_artifacts,
            instrument_identity=instrument_identity,
            artifact=artifact,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        return TushareAdjustmentFactorAdapterResult(
            subrequest_key=key.subrequest_key,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            outcome=subrequest.outcome,
            sequence_id=subrequest.sequence_id,
            attempt_events=subrequest.attempt_events,
            candidates=candidates,
            row_rejections=row_rejections,
        )


class TushareNameEventAdapterResult(BaseModel):
    """Payload-free dated name candidates with no status authority."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-name-event-adapter-v1"] = (
        TUSHARE_NAME_EVENT_ADAPTER_VERSION
    )
    endpoint_id: Literal["namechange"] = _NAME_ENDPOINT
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: ProviderArtifactIdentity | None = None
    row_artifacts: tuple[ProviderRowArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    candidates: tuple[IssuerNameEventCandidate, ...] = ()
    row_rejections: tuple[IssuerNameEventRowRejection, ...] = ()
    st_status_established: Literal[False] = False
    suspension_status_established: Literal[False] = False
    current_tradeability_established: Literal[False] = False
    listing_status_established: Literal[False] = False
    delisting_status_established: Literal[False] = False
    source_facts_created: Literal[False] = False
    decision_gate_changed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_result(self) -> TushareNameEventAdapterResult:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("Tushare name artifact contradicts acquisition outcome")
        if not available and (self.row_artifacts or self.candidates or self.row_rejections):
            raise ValueError("unavailable Tushare name response cannot emit candidates")
        row_ids = {item.row_artifact_identity for item in self.row_artifacts}
        used_ids = {item.row_artifact_identity for item in self.candidates}
        used_ids.update(item.row_artifact_identity for item in self.row_rejections)
        if available and (not row_ids or used_ids != row_ids):
            raise ValueError("Tushare name row artifact has no disposition")
        return self


class TushareNameEventAdapter:
    """Acquire one qualified Tushare dated issuer-name history."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        sdk_client: TushareFactorNameSdkClient,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
    ) -> None:
        _validate_name_plan(routing_plan)
        self._routing_plan = routing_plan
        self._cache = subrequest_cache
        self._sdk_client = sdk_client
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration

    def acquire_name_events(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareNameEventAdapterResult:
        _validate_instrument_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("Tushare name-event range must not end before it starts")
        key = _subrequest_key(
            routing_plan=self._routing_plan,
            endpoint_id=_NAME_ENDPOINT,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

        def physical_request() -> bytes:
            try:
                frame = self._sdk_client.namechange(
                    ts_code=_tushare_symbol(instrument_identity),
                    start_date=range_start.strftime("%Y%m%d"),
                    end_date=as_of_date.strftime("%Y%m%d"),
                )
            except Exception as exc:
                raise _sanitized_sdk_failure(exc) from None
            return _encoded_frame(
                frame=frame,
                endpoint_id=_NAME_ENDPOINT,
                now=self._now,
                artifact_contract_version=TUSHARE_NAME_EVENT_ADAPTER_VERSION,
                adapter_metadata=_NAME_ARTIFACT_METADATA,
            )

        subrequest = self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation="tushare_name_event",
            media_type="application/json",
            physical_request=physical_request,
        )
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return TushareNameEventAdapterResult(
                subrequest_key=key.subrequest_key,
                outcome=subrequest.outcome,
                sequence_id=subrequest.sequence_id,
                attempt_events=subrequest.attempt_events,
            )
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = decode_provider_artifact(
            subrequest.value,
            endpoint_id=_NAME_ENDPOINT,
            artifact_contract_version=TUSHARE_NAME_EVENT_ADAPTER_VERSION,
            adapter_metadata=_NAME_ARTIFACT_METADATA,
        )
        artifact = _artifact_identity(
            key=key,
            provider_artifact=subrequest.artifact,
            payload=payload,
        )
        row_artifacts = _row_artifact_identities(
            response_artifact=artifact,
            payload=payload,
        )
        candidates, row_rejections = _name_candidates(
            payload=payload,
            row_artifacts=row_artifacts,
            instrument_identity=instrument_identity,
            artifact=artifact,
            range_start=range_start,
            as_of_date=as_of_date,
        )
        return TushareNameEventAdapterResult(
            subrequest_key=key.subrequest_key,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            outcome=subrequest.outcome,
            sequence_id=subrequest.sequence_id,
            attempt_events=subrequest.attempt_events,
            candidates=candidates,
            row_rejections=row_rejections,
        )


def _name_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: tuple[ProviderRowArtifactIdentity, ...],
    instrument_identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    range_start: date,
    as_of_date: date,
) -> tuple[
    tuple[IssuerNameEventCandidate, ...],
    tuple[IssuerNameEventRowRejection, ...],
]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    parsed: list[
        tuple[
            date,
            date | None,
            str,
            str | None,
            ProviderRowArtifactIdentity,
        ]
    ] = []
    rejections: list[IssuerNameEventRowRejection] = []
    for row, row_artifact in zip(rows, row_artifacts, strict=True):
        assert isinstance(row, dict)
        try:
            start_date = datetime.strptime(str(row["start_date"]), "%Y%m%d").date()
            raw_end = row["end_date"]
            end_date = (
                datetime.strptime(str(raw_end), "%Y%m%d").date()
                if raw_end is not None and str(raw_end).strip()
                else None
            )
        except (KeyError, TypeError, ValueError):
            rejections.append(
                IssuerNameEventRowRejection(
                    reason=FactorNameRejectionReason.INVALID_DATE_INTERVAL,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                )
            )
            continue
        name = str(row.get("name") or "").strip()
        raw_reason = row.get("change_reason")
        change_reason = (
            str(raw_reason).strip()
            if raw_reason is not None and str(raw_reason).strip()
            else None
        )
        if not name:
            rejections.append(
                IssuerNameEventRowRejection(
                    reason=FactorNameRejectionReason.MALFORMED_RESPONSE,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            continue
        if end_date is not None and end_date < start_date:
            rejections.append(
                IssuerNameEventRowRejection(
                    reason=FactorNameRejectionReason.INVALID_DATE_INTERVAL,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            continue
        if (
            row.get("ts_code") != _tushare_symbol(instrument_identity)
            or start_date > as_of_date
            or (end_date is not None and end_date < range_start)
        ):
            rejections.append(
                IssuerNameEventRowRejection(
                    reason=FactorNameRejectionReason.INCOMPATIBLE_METADATA,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            continue
        parsed.append((start_date, end_date, name, change_reason, row_artifact))

    by_exact_interval: dict[
        tuple[date, date | None],
        list[tuple[date, date | None, str, str | None, ProviderRowArtifactIdentity]],
    ] = {}
    for item in parsed:
        by_exact_interval.setdefault((item[0], item[1]), []).append(item)
    candidates: list[IssuerNameEventCandidate] = []
    for interval, interval_rows in sorted(
        by_exact_interval.items(),
        key=lambda item: (item[0][0], item[0][1] or date.max),
    ):
        if len({item[2] for item in interval_rows}) > 1:
            rejections.extend(
                IssuerNameEventRowRejection(
                    reason=FactorNameRejectionReason.INVALID_DATE_INTERVAL,
                    row_artifact_identity=item[4].row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    start_date=interval[0],
                    end_date=interval[1],
                )
                for item in interval_rows
            )
            continue
        candidates.extend(
            IssuerNameEventCandidate.create(
                instrument_identity=instrument_identity,
                start_date=item[0],
                end_date=item[1],
                name=item[2],
                change_reason=item[3],
                artifact=artifact,
                row_artifact_identity=item[4].row_artifact_identity,
            )
            for item in interval_rows
        )
    return (
        tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.start_date,
                    item.end_date or date.max,
                    item.name,
                    item.revision_identity,
                ),
            )
        ),
        tuple(
            sorted(
                rejections,
                key=lambda item: (
                    item.start_date or date.min,
                    item.end_date or date.max,
                    item.reason.value,
                    item.row_artifact_identity,
                ),
            )
        ),
    )


def _factor_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: tuple[ProviderRowArtifactIdentity, ...],
    instrument_identity: InstrumentIdentityEvidence,
    artifact: ProviderArtifactIdentity,
    range_start: date,
    as_of_date: date,
) -> tuple[
    tuple[AdjustmentFactorCandidate, ...],
    tuple[AdjustmentFactorRowRejection, ...],
]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    parsed: list[
        tuple[date, Decimal, str, Mapping[str, object], ProviderRowArtifactIdentity]
    ] = []
    rejections: list[AdjustmentFactorRowRejection] = []
    for row, row_artifact in zip(rows, row_artifacts, strict=True):
        assert isinstance(row, dict)
        try:
            trade_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
            original_factor = str(row["adj_factor"])
            factor = Decimal(original_factor)
            if not factor.is_finite() or factor <= 0:
                raise ValueError
        except (KeyError, InvalidOperation, TypeError, ValueError):
            rejections.append(
                AdjustmentFactorRowRejection(
                    reason=FactorNameRejectionReason.MALFORMED_RESPONSE,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                )
            )
            continue
        if (
            row.get("ts_code") != _tushare_symbol(instrument_identity)
            or trade_date < range_start
            or trade_date > as_of_date
        ):
            rejections.append(
                AdjustmentFactorRowRejection(
                    reason=FactorNameRejectionReason.INCOMPATIBLE_METADATA,
                    row_artifact_identity=row_artifact.row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    trade_date=trade_date,
                )
            )
            continue
        parsed.append((trade_date, factor, original_factor, row, row_artifact))

    by_date: dict[
        date,
        list[tuple[date, Decimal, str, Mapping[str, object], ProviderRowArtifactIdentity]],
    ] = {}
    for item in parsed:
        by_date.setdefault(item[0], []).append(item)
    candidates: list[AdjustmentFactorCandidate] = []
    for trade_date, dated_rows in sorted(by_date.items()):
        if len({item[1] for item in dated_rows}) > 1:
            rejections.extend(
                AdjustmentFactorRowRejection(
                    reason=FactorNameRejectionReason.CONFLICTING_DUPLICATE_FACTOR,
                    row_artifact_identity=item[4].row_artifact_identity,
                    dataset_identity=artifact.dataset.dataset_identity,
                    artifact_identity=artifact.artifact_identity,
                    trade_date=trade_date,
                )
                for item in dated_rows
            )
            continue
        exact_rows: dict[
            str,
            list[tuple[date, Decimal, str, Mapping[str, object], ProviderRowArtifactIdentity]],
        ] = {}
        for item in dated_rows:
            exact_rows.setdefault(item[4].row_payload_sha256, []).append(item)
        for duplicate_rows in exact_rows.values():
            first = duplicate_rows[0]
            candidates.append(
                AdjustmentFactorCandidate.create(
                    instrument_identity=instrument_identity,
                    trade_date=trade_date,
                    adj_factor=first[1],
                    original_adj_factor=first[2],
                    artifact=artifact,
                    occurrence_artifact_identities=tuple(
                        item[4].row_artifact_identity for item in duplicate_rows
                    ),
                )
            )
    return (
        tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.trade_date,
                    item.original_adj_factor,
                    item.revision_identity,
                ),
            )
        ),
        tuple(
            sorted(
                rejections,
                key=lambda item: (
                    item.trade_date or date.min,
                    item.row_artifact_identity,
                ),
            )
        ),
    )


def _subrequest_key(
    *,
    routing_plan: MainlandCapabilityRoutingPlan,
    endpoint_id: str,
    instrument_identity: InstrumentIdentityEvidence,
    range_start: date,
    as_of_date: date,
) -> ProviderSubrequestKey:
    upstream_service_id, _ = upstream_service_identity_for_provider(
        "tushare",
        account_scope=routing_plan.account_scope_label,
    )
    return ProviderSubrequestKey.create(
        provider_id="tushare",
        upstream_service_id=upstream_service_id,
        account_scope=routing_plan.account_scope_label,
        capacity_scope=endpoint_id,
        instrument_identity=instrument_identity,
        requested_range_start=range_start.isoformat(),
        requested_range_end=as_of_date.isoformat(),
        requested_fields=("all_fields",),
        as_of_date=as_of_date,
        qualification_profile=routing_plan.qualification_profile,
        normalizer_version=routing_plan.normalizer_version,
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _encoded_frame(
    *,
    frame: object,
    endpoint_id: str,
    now: Callable[[], datetime],
    artifact_contract_version: str,
    adapter_metadata: Mapping[str, object],
) -> bytes:
    if not isinstance(frame, pd.DataFrame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="tushare_response_not_frame",
        )
    if frame.empty:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
            retryable=False,
            error_code="tushare_empty_frame",
        )
    if _contains_credential_like_data(frame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="tushare_response_unsafe",
        )
    observed_at = require_utc(now())
    try:
        return encode_provider_artifact(
            endpoint_id=endpoint_id,
            frame=frame,
            retrieved_at=observed_at,
            observed_at=observed_at,
            artifact_contract_version=artifact_contract_version,
            adapter_metadata=adapter_metadata,
        )
    except (TypeError, ValueError, OverflowError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="tushare_response_not_serializable",
        ) from None


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
        for raw_value in row
        if isinstance(raw_value, str)
        for value in (raw_value,)
        for marker in _CREDENTIAL_VALUE_MARKERS
    )


def _artifact_identity(
    *,
    key: ProviderSubrequestKey,
    provider_artifact: ProviderSubrequestArtifactRef,
    payload: Mapping[str, object],
) -> ProviderArtifactIdentity:
    dataset = ProviderDatasetIdentity.create(
        provider_id="tushare",
        endpoint_id=str(payload["endpoint_id"]),
        dataset_id=str(payload["dataset_id"]),
        schema_identity=str(payload["schema_identity"]),
    )
    return ProviderArtifactIdentity.create(
        dataset=dataset,
        subrequest_key=key.subrequest_key,
        raw_artifact=provider_artifact,
        payload=payload["rows"],
        retrieved_at=datetime.fromisoformat(
            str(payload["retrieved_at"]).replace("Z", "+00:00")
        ),
        observed_at=datetime.fromisoformat(
            str(payload["observed_at"]).replace("Z", "+00:00")
        ),
        qualification_profile=key.qualification_profile,
        normalizer_version=key.normalizer_version,
    )


def _row_artifact_identities(
    *,
    response_artifact: ProviderArtifactIdentity,
    payload: Mapping[str, object],
) -> tuple[ProviderRowArtifactIdentity, ...]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    duplicate_counts: dict[str, int] = {}
    identities: list[ProviderRowArtifactIdentity] = []
    for row in rows:
        canonical_row = json.dumps(
            row,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        occurrence = duplicate_counts.get(canonical_row, 0)
        duplicate_counts[canonical_row] = occurrence + 1
        identities.append(
            ProviderRowArtifactIdentity.create(
                response_artifact_identity=response_artifact.artifact_identity,
                row_payload_sha256=sha256(canonical_row.encode("utf-8")).hexdigest(),
                duplicate_occurrence=occurrence,
            )
        )
    return tuple(identities)


def _validate_factor_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if (
        plan.mode
        not in {
            MainlandCapabilityRoutingMode.QUALIFIED_V1,
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
        }
        or TushareCapability.ADJUSTMENT_FACTORS
        not in plan.enabled_tushare_capabilities
    ):
        raise TushareFactorNameAdapterConfigurationError(
            TushareFactorNameAdapterFailureReason.ADJUSTMENT_FACTORS_NOT_ENABLED
        )
    endpoint_ids = {item.endpoint_id for item in plan.endpoint_pacing_identities}
    route = plan.route_for(MainlandCapability.ADJUSTMENT_FACTORS)
    if route != ("baostock", "tushare", "akshare", "yfinance_derived") or (
        _FACTOR_ENDPOINT not in endpoint_ids
    ):
        raise TushareFactorNameAdapterConfigurationError(
            TushareFactorNameAdapterFailureReason.ADJUSTMENT_FACTORS_PLAN_INCOHERENT
        )


def _validate_name_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if (
        plan.mode
        not in {
            MainlandCapabilityRoutingMode.QUALIFIED_V1,
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
        }
        or TushareCapability.NAME_EVENTS not in plan.enabled_tushare_capabilities
    ):
        raise TushareFactorNameAdapterConfigurationError(
            TushareFactorNameAdapterFailureReason.NAME_EVENTS_NOT_ENABLED
        )
    endpoint_ids = {item.endpoint_id for item in plan.endpoint_pacing_identities}
    route = plan.route_for(MainlandCapability.NAME_EVENTS)
    if route != ("tushare", "akshare") or _NAME_ENDPOINT not in endpoint_ids:
        raise TushareFactorNameAdapterConfigurationError(
            TushareFactorNameAdapterFailureReason.NAME_EVENTS_PLAN_INCOHERENT
        )


def _validate_instrument_identity(identity: InstrumentIdentityEvidence) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("Tushare factors require a mainland Equity identity")


def _tushare_symbol(identity: InstrumentIdentityEvidence) -> str:
    symbol = identity.symbol.strip().upper()
    if symbol.endswith(".SS"):
        return symbol[:-3] + ".SH"
    if symbol.endswith(".SZ"):
        return symbol
    raise ValueError("Tushare symbol is not a canonical mainland symbol")


def _sanitized_sdk_failure(exc: Exception) -> PhysicalAttemptFailure:
    type_name = type(exc).__name__.casefold()
    message = str(exc)[:512].casefold()
    retry_after = _retry_after_seconds(exc)
    if isinstance(exc, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code="tushare_timeout",
        )
    if any(marker in message for marker in ("频率", "每分钟", "rate limit", "too many")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
            error_code="tushare_rate_limited",
            rate_limit_scope=_sdk_rate_limit_scope(exc),
        )
    if any(
        marker in message
        for marker in ("没有接口", "权限", "permission", "access denied")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="tushare_permission_denied",
        )
    if any(marker in message for marker in ("认证", "auth", "无效token", "invalid token")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="tushare_authentication",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code="tushare_provider_error",
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


def _sdk_rate_limit_scope(exc: Exception) -> RateLimitScope:
    value = getattr(exc, "rate_limit_scope", None)
    if value is RateLimitScope.UPSTREAM:
        return RateLimitScope.UPSTREAM
    if isinstance(value, str) and value.strip().casefold() in {
        "account",
        "global",
        "upstream",
    }:
        return RateLimitScope.UPSTREAM
    return RateLimitScope.CAPACITY


__all__ = [
    "TUSHARE_ADJUSTMENT_FACTOR_ADAPTER_VERSION",
    "TUSHARE_NAME_EVENT_ADAPTER_VERSION",
    "TushareAdjustmentFactorAdapter",
    "TushareAdjustmentFactorAdapterResult",
    "TushareFactorNameAdapterConfigurationError",
    "TushareFactorNameAdapterFailureReason",
    "TushareFactorNameSdkClient",
    "TushareNameEventAdapter",
    "TushareNameEventAdapterResult",
]
