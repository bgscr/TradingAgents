from __future__ import annotations

import logging
import re
import threading
from bisect import bisect_right
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceState,
    ProviderPhysicalAttemptEvidence,
    SourceAcquisitionAvailable,
    SourceAcquisitionOutcome,
    SourceArtifact,
    stable_acquisition_source_ref,
    stable_market_snapshot_id,
)
from tradingagents.market_history.coordinator import ProviderPhysicalAttemptEvent
from tradingagents.market_history.models import TradingStatus, TradingStatusProvenance
from tradingagents.market_history.snapshot_identity import (
    LEGACY_SNAPSHOT_ID_PATTERN,
    SNAPSHOT_V2_ID_PATTERN,
    CryptoProviderDatasetDescriptor,
    CryptoSnapshotIdentityMismatch,
    build_crypto_provider_dataset_descriptor,
    crypto_snapshot_identity_revision,
    crypto_snapshot_instrument_id,
    live_snapshot_v2_identity,
    validate_crypto_provider_dataset_descriptor,
)
from tradingagents.run_telemetry import RunTelemetryLedger

from .acquisition import AcquisitionController, AcquisitionFailure, AcquisitionRequest
from .config import get_config
from .errors import NoMarketDataError, VendorRateLimitError
from .stockstats_utils import MAX_OHLCV_STALE_DAYS
from .symbol_utils import resolve_china_a_symbol

if TYPE_CHECKING:
    from tradingagents.asset_configuration import RunAssetConfiguration

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotProvider:
    load: Callable[..., object]
    adjustment_basis: str
    history_load: Callable[..., object] | None = None
    reports_physical_requests: bool = False
    crypto_dataset: CryptoProviderDatasetDescriptor | None = None


@dataclass(frozen=True)
class QuarantinedSnapshot:
    provider: str
    reason: str
    row_indices: tuple[object, ...] = ()


class OHLCVValidationError(ValueError):
    def __init__(self, message: str, row_indices: tuple[object, ...] = ()):
        self.row_indices = row_indices
        super().__init__(message)


class AuthoritativeTradingStatusValidationError(ValueError):
    """A selected complete provider bundle lost or contradicted status evidence."""


@dataclass(frozen=True)
class AuthoritativeMarketSnapshot:
    symbol: str
    frame: pd.DataFrame
    provider: str
    retrieved_at: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    frame_sha256: str = ""
    snapshot_id: str = ""
    snapshot_id_version: str = ""
    pin_membership_digest: str | None = None
    snapshot_manifest_json: str | None = None
    quarantined: tuple[QuarantinedSnapshot, ...] = ()
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = ()
    source_artifact: SourceArtifact | None = None
    history_store_status: str = "live"
    history_store_diagnostic: str | None = None
    current_tradeability: str = "unknown"
    current_status_provenance: TradingStatusProvenance | None = None
    latest_traded_close: Decimal | None = None
    latest_traded_close_diagnostic: str | None = None
    carried_suspension_close: Decimal | None = None
    history_gap_dates: tuple[str, ...] = ()
    physical_attempt_events: tuple[ProviderPhysicalAttemptEvent, ...] = ()
    asset_configuration: RunAssetConfiguration | None = None
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None = None

    def __post_init__(self) -> None:
        configured_crypto = (
            self.asset_configuration is not None
            and self.asset_configuration.instrument_kind.value == "crypto"
        )
        computed_frame_digest = (
            _frame_sha256(self.frame)
            if configured_crypto or not self.frame_sha256
            else self.frame_sha256
        )
        if (
            configured_crypto
            and bool(self.frame_sha256)
            and self.frame_sha256 != computed_frame_digest
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot frame digest contradicts the selected frame"
            )
        frame_digest = self.frame_sha256 or computed_frame_digest
        identity = self.snapshot_id
        identity_version = self.snapshot_id_version
        membership_digest = self.pin_membership_digest
        manifest_json = self.snapshot_manifest_json
        if identity:
            if SNAPSHOT_V2_ID_PATTERN.fullmatch(identity) is not None:
                identity_version = identity_version or "v2"
                membership_digest = membership_digest or identity.rsplit(":", 1)[-1]
                if identity_version != "v2" or membership_digest != identity.rsplit(":", 1)[-1]:
                    raise ValueError("snapshot v2 identity metadata is inconsistent")
            elif LEGACY_SNAPSHOT_ID_PATTERN.fullmatch(identity) is not None:
                identity_version = identity_version or "v1"
                if identity_version != "v1" or membership_digest is not None:
                    raise ValueError("legacy snapshot identity metadata is inconsistent")
            else:
                raise ValueError("snapshot identity format is unsupported")
        else:
            live_identity = _live_snapshot_identity(self, frame_digest)
            identity = live_identity.snapshot_id
            identity_version = "v2"
            membership_digest = live_identity.membership_digest
            manifest_json = live_identity.manifest_json
        object.__setattr__(self, "frame_sha256", frame_digest)
        object.__setattr__(self, "snapshot_id", identity)
        object.__setattr__(self, "snapshot_id_version", identity_version)
        object.__setattr__(self, "pin_membership_digest", membership_digest)
        object.__setattr__(self, "snapshot_manifest_json", manifest_json)
        if self.current_tradeability not in {"unknown", "tradeable", "suspended"}:
            raise AuthoritativeTradingStatusValidationError(
                f"invalid current tradeability {self.current_tradeability!r}"
            )
        provenance = self.current_status_provenance
        if self.current_tradeability == "unknown":
            if (
                provenance is not None
                or self.latest_traded_close is not None
                or self.carried_suspension_close is not None
            ):
                raise AuthoritativeTradingStatusValidationError(
                    "unknown tradeability cannot carry authoritative status values"
                )
            return
        if provenance is None:
            raise AuthoritativeTradingStatusValidationError(
                "authoritative current tradeability requires status provenance"
            )
        expected_status = (
            TradingStatus.SUSPENDED
            if self.current_tradeability == "suspended"
            else TradingStatus.TRADED
        )
        if (
            provenance.provider != self.provider
            or provenance.session_date.isoformat() != self.effective_trading_date
            or provenance.status is not expected_status
        ):
            raise AuthoritativeTradingStatusValidationError(
                "authoritative status provenance contradicts the market snapshot"
            )
        if self.latest_traded_close is None:
            if not self.latest_traded_close_diagnostic:
                raise AuthoritativeTradingStatusValidationError(
                    "unavailable latest traded close requires a diagnostic"
                )
        elif self.latest_traded_close_diagnostic is not None:
            raise AuthoritativeTradingStatusValidationError(
                "available latest traded close cannot carry an unavailable diagnostic"
            )
        if self.frame.empty:
            raise AuthoritativeTradingStatusValidationError(
                "authoritative current tradeability requires a represented session"
            )
        latest_row = self.frame.iloc[-1]
        latest_close = Decimal(str(latest_row["Close"]))
        if self.current_tradeability == "suspended":
            prices = tuple(Decimal(str(latest_row[column])) for column in ("Open", "High", "Low", "Close"))
            if (
                self.carried_suspension_close is None
                or any(value != self.carried_suspension_close for value in prices)
                or Decimal(str(latest_row["Volume"])) != Decimal("0")
            ):
                raise AuthoritativeTradingStatusValidationError(
                    "authoritative suspension row contradicts its carried close or volume"
                )
        else:
            if self.carried_suspension_close is not None:
                raise AuthoritativeTradingStatusValidationError(
                    "tradeable status cannot carry a suspension close"
                )
            if self.latest_traded_close is None or latest_close != self.latest_traded_close:
                raise AuthoritativeTradingStatusValidationError(
                    "tradeable status contradicts the latest genuinely traded close"
                )


@dataclass(frozen=True)
class MarketSnapshotAcquisitionRecord:
    snapshot: AuthoritativeMarketSnapshot | None
    outcomes: tuple[SourceAcquisitionOutcome, ...]
    source_artifact: SourceArtifact | None


def _normalized_frame_text(frame: pd.DataFrame) -> str:
    from tradingagents.market_history.frames import normalized_frame_text

    return normalized_frame_text(frame)


def _frame_sha256(frame: pd.DataFrame) -> str:
    return sha256(_normalized_frame_text(frame).encode("utf-8")).hexdigest()


def _snapshot_id(
    *,
    symbol: str,
    provider: str,
    adjustment_basis: str,
    requested_date: str,
    effective_trading_date: str,
    frame_sha256: str,
    history_rows: int,
) -> str:
    return stable_market_snapshot_id(
        symbol=symbol,
        provider=provider,
        adjustment_basis=adjustment_basis,
        requested_date=requested_date,
        effective_trading_date=effective_trading_date,
        frame_sha256=frame_sha256,
        history_rows=history_rows,
    )


def _live_snapshot_identity(
    snapshot: AuthoritativeMarketSnapshot,
    frame_digest: str,
):
    from tradingagents.market_history.coordinator import (
        upstream_service_identity_for_provider,
    )

    asset_configuration = snapshot.asset_configuration
    configured_crypto = (
        asset_configuration is not None
        and asset_configuration.instrument_kind.value == "crypto"
    )
    if configured_crypto:
        if not asset_configuration.matches_symbol(snapshot.symbol):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot symbol contradicts the run asset"
            )
        identity = asset_configuration.instrument_identity
        canonical_symbol = identity.symbol
        reference_market = identity.venue
        instrument_kind = identity.instrument_kind.value
        currency = identity.currency
        identity_revision = crypto_snapshot_identity_revision(asset_configuration)
        instrument_id = crypto_snapshot_instrument_id(asset_configuration)
    else:
        instrument = resolve_china_a_symbol(snapshot.symbol)
    if not configured_crypto and instrument is None:
        from tradingagents.dataflows.crypto_universe import is_crypto_pair_syntax

        if is_crypto_pair_syntax(snapshot.symbol):
            raise CryptoSnapshotIdentityMismatch(
                "authoritative RunAssetConfiguration is required"
            )
        canonical_symbol = snapshot.symbol.strip().upper()
        reference_market = "unresolved"
        instrument_kind = "unknown"
        currency = "unknown"
        identity_revision = "current-live-symbol-v1"
        instrument_id = _stable_live_identity(
            "instrument",
            canonical_symbol,
            reference_market,
            instrument_kind,
            currency,
        )
    elif not configured_crypto:
        canonical_symbol = instrument.yahoo_symbol
        reference_market = instrument.exchange
        instrument_kind = getattr(instrument, "instrument_kind", "equity")
        currency = "CNY"
        identity_revision = "mainland-routing-v1"
        instrument_id = _stable_live_identity(
            "instrument",
            canonical_symbol,
            reference_market,
            instrument_kind,
            currency,
        )
    upstream_service_id, _ = upstream_service_identity_for_provider(snapshot.provider)
    provenance = snapshot.current_status_provenance
    if configured_crypto:
        crypto_dataset = validate_crypto_provider_dataset_descriptor(
            snapshot.crypto_provider_dataset,
            provider_name=snapshot.provider,
            upstream_service_id=upstream_service_id,
        )
        provider_dataset_id = crypto_dataset.provider_dataset_id
    else:
        crypto_dataset = None
        provider_dataset_id = (
            provenance.provider_dataset_id
            if provenance is not None
            else _stable_live_identity(
                "provider-dataset",
                snapshot.provider,
                upstream_service_id,
                "mainland-current-adjusted-v1",
            )
        )
    if provenance is None:
        if configured_crypto:
            status_identity = _stable_live_identity(
                "crypto-continuous-market-status",
                provider_dataset_id,
                snapshot.effective_trading_date,
                frame_digest,
            )
            status_value = "not_applicable"
        else:
            status_identity = "unknown"
            status_value = "unknown"
    else:
        status_value = provenance.status.value
        status_identity = provenance.revision_id or _stable_live_identity(
            "trading-status",
            provenance.provider,
            provenance.provider_dataset_id,
            provenance.session_date.isoformat(),
            provenance.status.value,
            provenance.observed_at.astimezone(timezone.utc).isoformat(),
        )
    artifact_identity = (
        f"artifact:sha256:{snapshot.source_artifact.artifact_sha256}"
        if snapshot.source_artifact is not None
        else f"frame:sha256:{frame_digest}"
    )
    return live_snapshot_v2_identity(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        identity_revision=identity_revision,
        reference_market=reference_market,
        instrument_kind=instrument_kind,
        currency=currency,
        provider_dataset_id=provider_dataset_id,
        provider_name=snapshot.provider,
        upstream_service_id=upstream_service_id,
        requested_date=snapshot.requested_date,
        effective_trading_date=snapshot.effective_trading_date,
        adjustment_basis=snapshot.adjustment_basis,
        frame_digest=frame_digest,
        history_rows=len(snapshot.frame),
        derivation_version="provider-current-frame-v1",
        normalization_version="normalized-frame-csv-v1",
        accepted_artifact_identity=artifact_identity,
        authoritative_status_identity=status_identity,
        authoritative_status_value=status_value,
        provenance_class=snapshot.history_store_status,
        asset_configuration=asset_configuration,
        crypto_provider_dataset=crypto_dataset,
    )


def _stable_live_identity(namespace: str, *components: object) -> str:
    digest = sha256()
    for component in components:
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{namespace}=sha256:{digest.hexdigest()}"


@dataclass
class _AuthoritativeSnapshotRun:
    asset_configuration: RunAssetConfiguration | None = None
    snapshots: dict[tuple[str, str, str], AuthoritativeMarketSnapshot] = field(
        default_factory=dict
    )
    latest_snapshots: dict[tuple[str, str], AuthoritativeMarketSnapshot] = field(
        default_factory=dict
    )
    telemetry_ledger: RunTelemetryLedger = field(default_factory=RunTelemetryLedger)
    acquisition_controller: AcquisitionController = field(init=False)
    acquisition_records: dict[
        tuple[str, str, str], MarketSnapshotAcquisitionRecord
    ] = field(default_factory=dict)
    latest_acquisition_records: dict[
        tuple[str, str], MarketSnapshotAcquisitionRecord
    ] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    physical_attempt_events: list[ProviderPhysicalAttemptEvent] = field(
        default_factory=list
    )

    def __post_init__(self) -> None:
        self.acquisition_controller = AcquisitionController(
            providers=(),
            outcome_observer=self.telemetry_ledger.record_acquisition,
        )


_ACTIVE_SNAPSHOT_RUN: ContextVar[_AuthoritativeSnapshotRun | None] = ContextVar(
    "active_authoritative_snapshot_run",
    default=None,
)


@contextmanager
def authoritative_snapshot_run(
    *,
    asset_configuration: RunAssetConfiguration | None = None,
):
    """Reuse accepted market frames for the duration of one analysis run."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is not None:
        if asset_configuration is not None:
            if active.asset_configuration is None:
                active.asset_configuration = asset_configuration
            elif (
                asset_configuration.asset_configuration_signature
                != active.asset_configuration.asset_configuration_signature
            ):
                raise CryptoSnapshotIdentityMismatch(
                    "nested snapshot run contradicts the authoritative run asset"
                )
        yield active
        return

    run = _AuthoritativeSnapshotRun(asset_configuration=asset_configuration)
    token = _ACTIVE_SNAPSHOT_RUN.set(run)
    try:
        yield run
    finally:
        _ACTIVE_SNAPSHOT_RUN.reset(token)


def record_active_physical_attempt_events(
    events: tuple[ProviderPhysicalAttemptEvent, ...],
) -> None:
    """Append immutable coordinator events to the current run audit ledger."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None or not events:
        return
    with active.lock:
        by_key = {
            (event.sequence_id, event.attempt_index): event
            for event in active.physical_attempt_events
        }
        for event in events:
            key = (event.sequence_id, event.attempt_index)
            prior = by_key.get(key)
            if prior is not None:
                if prior != event:
                    raise RuntimeError(
                        "single-flight attempt identity has conflicting audit data"
                    )
                continue
            active.physical_attempt_events.append(event)
            by_key[key] = event


def refresh_active_evidence_physical_attempts(
    evidence: EvidenceState,
) -> EvidenceState:
    """Merge the complete run attempt ledger into evidence before publication."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None:
        return evidence
    with active.lock:
        events = tuple(active.physical_attempt_events)
    projected = tuple(
        ProviderPhysicalAttemptEvidence(
            attempt_event_id=event.attempt_event_id,
            sequence_id=event.sequence_id,
            request_key=event.request_key,
            upstream_service_id=event.upstream_service_id,
            upstream_service_name=event.upstream_service_name,
            attempt_index=event.attempt_index,
            attempted_at=event.attempted_at.isoformat(),
            pacing_event=event.pacing_event,
            pacing_wait_seconds=event.pacing_wait_seconds,
            outcome=event.outcome.value,
            retryable=event.retryable,
            status_code=event.status_code,
            error_code=event.error_code,
            retry_after_seconds=event.retry_after_seconds,
            cooldown_changed=event.cooldown_changed,
            cooldown_until=(
                event.cooldown_until.isoformat()
                if event.cooldown_until is not None
                else None
            ),
            final_physical_attempt_count=event.final_physical_attempt_count,
        )
        for event in events
    )
    payload = evidence.model_dump(mode="python")
    payload["physical_attempt_events"] = projected
    payload["physical_attempt_count"] = len(projected)
    if evidence.market_snapshot is not None:
        snapshot_payload = evidence.market_snapshot.model_dump(mode="python")
        snapshot_payload["physical_attempt_events"] = projected
        snapshot_payload["physical_attempt_count"] = len(projected)
        payload["market_snapshot"] = snapshot_payload
    return EvidenceState.model_validate(payload)


def get_active_acquisition_controller() -> AcquisitionController | None:
    """Return the controller owned by the active analysis run, when present."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    return None if active is None else active.acquisition_controller


def get_active_run_telemetry() -> RunTelemetryLedger | None:
    """Return the canonical telemetry ledger owned by the active run."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    return None if active is None else active.telemetry_ledger


def _load_akshare(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from .akshare_data import load_ohlcv_range

    return load_ohlcv_range(symbol, start_date, end_date)


def _load_baostock(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    from .baostock_data import load_ohlcv_range

    return load_ohlcv_range(
        symbol,
        start_date,
        end_date,
        before_physical_request=before_physical_request,
    )


def _load_baostock_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
) -> object:
    from .baostock_data import load_snapshot_with_history

    return load_snapshot_with_history(
        symbol,
        start_date,
        end_date,
        before_physical_request=before_physical_request,
    )


def _load_yfinance(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from .y_finance import load_ohlcv_range

    return load_ohlcv_range(symbol, start_date, end_date)


SNAPSHOT_PROVIDERS: dict[str, SnapshotProvider] = {
    "akshare": SnapshotProvider(_load_akshare, "qfq"),
    "baostock": SnapshotProvider(
        _load_baostock,
        "qfq",
        history_load=_load_baostock_history,
        reports_physical_requests=True,
    ),
    "yfinance": SnapshotProvider(_load_yfinance, "auto_adjusted"),
}


def _crypto_dataset_for_provider(
    provider_name: str,
    provider: SnapshotProvider,
) -> CryptoProviderDatasetDescriptor | None:
    if provider.crypto_dataset is not None:
        return provider.crypto_dataset
    if provider_name != "yfinance":
        return None
    from tradingagents.market_history.coordinator import (
        upstream_service_identity_for_provider,
    )

    upstream_service_id, _ = upstream_service_identity_for_provider(provider_name)
    return build_crypto_provider_dataset_descriptor(
        provider_name=provider_name,
        upstream_service_id=upstream_service_id,
        dataset_family="crypto",
        dataset_name="yahoo-ccc-daily-ohlcv",
        dataset_version="1.0",
        dataset_revision="yfinance-download-auto-adjusted-v1",
        reference_market="CCC",
        tags=("crypto", "ccc", "ohlcv", "daily"),
    )


def _coordinator_now() -> datetime:
    return datetime.now(timezone.utc)


def _coordinator_sleep(seconds: float) -> None:
    from time import sleep

    sleep(seconds)


def _load_through_provider_coordinator(
    provider_name: str,
    provider: SnapshotProvider,
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    attempt_observer: Callable[[tuple[ProviderPhysicalAttemptEvent, ...]], None]
    | None = None,
) -> object:
    from datetime import timedelta

    from tradingagents.market_history.config import (
        DataUsageMode,
        MarketHistoryConfig,
        MarketHistoryMode,
    )
    from tradingagents.market_history.coordinator import (
        LeaseDisposition,
        ProviderRequestCoordinator,
        RequestPriority,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    try:
        history_config = MarketHistoryConfig.from_mapping(get_config())
    except Exception as exc:
        logger.warning(
            "Provider request coordinator configuration failed for %s: %s",
            provider_name,
            exc,
        )
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.NOT_CONFIGURED
        ) from exc
    if history_config.data_usage_mode is DataUsageMode.PRODUCTION:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.USAGE_NOT_ENTITLED
        )
    try:
        store = MarketHistoryStore.open_provider_request_authority(history_config)
    except Exception as primary_exc:
        logger.warning(
            "Provider request coordinator unavailable for %s: %s",
            provider_name,
            primary_exc,
        )
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.UPSTREAM_BUSY
        ) from primary_exc
    with store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            provider_name
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)
        history_start_date = start_date
        history_end_date = end_date
        if (
            history_config.mode is MarketHistoryMode.AUTHORITATIVE
            and provider.history_load is not None
        ):
            instrument = resolve_china_a_symbol(symbol)
            if instrument is not None:
                from tradingagents.market_history.maintenance import (
                    plan_incremental_refresh,
                )
                from tradingagents.market_history.models import SnapshotPurpose

                try:
                    with MarketHistoryStore.open(history_config) as history_store:
                        latest_bundle = history_store.latest_qualified_history_bundle(
                            instrument.yahoo_symbol,
                            end_date,
                        )
                        retained = history_store.reconstruct_snapshot(
                            latest_bundle.bundle_revision_id,
                            requested_date=date.fromisoformat(end_date),
                            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
                        )
                        calendar = history_store.read_current_session_calendar(
                            "mainland-cn"
                        )
                except Exception:
                    pass
                else:
                    retained_dates = {
                        value.date()
                        for value in retained.frame["Date"].dt.to_pydatetime()
                    }
                    refresh_plan = plan_incremental_refresh(
                        sessions=calendar.sessions,
                        as_of_date=date.fromisoformat(end_date),
                        retained_session_dates=retained_dates,
                        seeded=True,
                    )
                    if refresh_plan.requested_start is not None:
                        history_start_date = refresh_plan.requested_start.isoformat()
                    if refresh_plan.requested_end is not None:
                        history_end_date = refresh_plan.requested_end.isoformat()
        now = _coordinator_now()
        lease_duration = timedelta(minutes=2)
        request_key = stable_acquisition_source_ref(
            "provider-request",
            provider_name,
            symbol,
            history_start_date,
            history_end_date,
            provider.adjustment_basis,
        )
        owner_id = f"snapshot:{threading.get_ident()}:{uuid4().hex}"
        if provider_name == "yfinance":
            from tradingagents.dataflows.stockstats_utils import (
                _execute_guarded_yahoo_attempt,
                _yahoo_session_guard,
            )
            from tradingagents.dataflows.y_finance import (
                _validated_yahoo_history_frame,
            )
            from tradingagents.market_history.coordinator import (
                PhysicalAttemptBudgetExhausted,
                PhysicalAttemptOutcome,
            )

            yahoo_session_guard = _yahoo_session_guard()

            def validate_yahoo_result(value: object) -> object:
                raw_frame = getattr(value, "frame", value)
                _validated_yahoo_history_frame(
                    raw_frame,
                    history_end_date,
                    history_start_date,
                )
                return value

            def yahoo_physical_attempt(_attempt_index: int) -> object:
                return _execute_guarded_yahoo_attempt(
                    lambda: provider.load(symbol, start_date, end_date),
                    session_guard=yahoo_session_guard,
                    injected_transport=provider.load is not _load_yfinance,
                    result_validator=validate_yahoo_result,
                )

            try:
                result = coordinator.execute_retry_sequence(
                    request_key=request_key,
                    upstream_service_id=upstream_service_id,
                    owner_id=owner_id,
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=_coordinator_now,
                    sleep=_coordinator_sleep,
                    lease_duration=lease_duration,
                    max_physical_attempts=(
                        history_config.yahoo_max_physical_attempts
                    ),
                    operation="market-snapshot",
                    physical_attempt=yahoo_physical_attempt,
                    cooldown_scope="all",
                    record_at_physical_io=True,
                )
                if attempt_observer is not None:
                    attempt_observer(result.attempt_events)
                return result.value
            except PhysicalAttemptBudgetExhausted as exc:
                if attempt_observer is not None:
                    attempt_observer(exc.attempt_events)
                reason_by_outcome = {
                    PhysicalAttemptOutcome.RATE_LIMITED: (
                        AcquisitionUnavailableReason.RATE_LIMITED
                    ),
                    PhysicalAttemptOutcome.TIMEOUT: AcquisitionUnavailableReason.TIMEOUT,
                    PhysicalAttemptOutcome.DISCONNECT: (
                        AcquisitionUnavailableReason.DISCONNECT
                    ),
                    PhysicalAttemptOutcome.EMPTY_FRAME: (
                        AcquisitionUnavailableReason.EMPTY_FRAME
                    ),
                    PhysicalAttemptOutcome.AUTHENTICATION: (
                        AcquisitionUnavailableReason.AUTHENTICATION
                    ),
                    PhysicalAttemptOutcome.MALFORMED_RESPONSE: (
                        AcquisitionUnavailableReason.MALFORMED_RESPONSE
                    ),
                    PhysicalAttemptOutcome.PROVIDER_ERROR: (
                        AcquisitionUnavailableReason.PROVIDER_ERROR
                    ),
                    PhysicalAttemptOutcome.UPSTREAM_BUSY: (
                        AcquisitionUnavailableReason.UPSTREAM_BUSY
                    ),
                }
                raise AcquisitionFailure(
                    reason=reason_by_outcome[exc.failure.outcome],
                    status_code=exc.failure.status_code,
                    retry_after_seconds=exc.failure.retry_after_seconds,
                ) from exc
        decision = coordinator.acquire(
            request_key=request_key,
            upstream_service_id=upstream_service_id,
            owner_id=owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=lease_duration,
            cooldown_scope="market-snapshot",
        )
        if decision.disposition is not LeaseDisposition.ACQUIRED:
            coordinator.cancel_queued_request(
                request_key=request_key,
                upstream_service_id=upstream_service_id,
            )
        if decision.disposition is LeaseDisposition.COOLDOWN:
            retry_after = (
                max(0.0, (decision.cooldown_until - now).total_seconds())
                if decision.cooldown_until is not None
                else None
            )
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.RATE_LIMITED,
                retry_after_seconds=retry_after,
            )
        if decision.disposition is LeaseDisposition.PACING:
            retry_after = (
                max(0.0, (decision.cooldown_until - now).total_seconds())
                if decision.cooldown_until is not None
                else None
            )
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.UPSTREAM_BUSY,
                retry_after_seconds=retry_after,
            )
        if decision.disposition is not LeaseDisposition.ACQUIRED:
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.UPSTREAM_BUSY)
        physical_attempt_index = 0
        current_lease = decision.lease

        def before_physical_request(operation: str) -> None:
            nonlocal current_lease, physical_attempt_index
            physical_attempt_index += 1
            attempted_at = _coordinator_now()
            earliest_at = coordinator.earliest_physical_attempt_at(
                current_lease,
                requested_at=attempted_at,
            )
            while earliest_at > attempted_at:
                renewed = coordinator.renew(
                    current_lease,
                    now=attempted_at,
                    lease_duration=lease_duration,
                )
                if renewed is None:
                    raise AcquisitionFailure(
                        reason=AcquisitionUnavailableReason.UPSTREAM_BUSY
                    )
                current_lease = renewed
                wait_seconds = min(
                    60.0,
                    (earliest_at - attempted_at).total_seconds(),
                )
                _coordinator_sleep(wait_seconds)
                attempted_at = max(
                    _coordinator_now(),
                    attempted_at + timedelta(seconds=wait_seconds),
                )
            renewed = coordinator.renew(
                current_lease,
                now=attempted_at,
                lease_duration=lease_duration,
            )
            if renewed is None:
                raise AcquisitionFailure(reason=AcquisitionUnavailableReason.UPSTREAM_BUSY)
            current_lease = renewed
            coordinator.record_physical_attempt(
                current_lease,
                occurred_at=attempted_at,
                attempt_key=f"{physical_attempt_index}:{operation}",
            )

        def load_provider(
            loader: Callable[..., object],
            loader_start_date: str,
            loader_end_date: str,
            operation: str,
        ) -> object:
            if provider.reports_physical_requests:
                return loader(
                    symbol,
                    loader_start_date,
                    loader_end_date,
                    before_physical_request=before_physical_request,
                )
            before_physical_request(operation)
            return loader(symbol, loader_start_date, loader_end_date)

        try:
            if (
                history_config.mode is MarketHistoryMode.AUTHORITATIVE
                and provider.history_load is not None
            ):
                try:
                    return load_provider(
                        provider.history_load,
                        history_start_date,
                        history_end_date,
                        "history-workflow",
                    )
                except (NoMarketDataError, ValueError, AttributeError) as exc:
                    logger.warning(
                        "Complete history bundle unavailable for %s via %s; "
                        "using its independently validated current frame: %s",
                        symbol,
                        provider_name,
                        exc,
                    )
            return load_provider(
                provider.load,
                start_date,
                end_date,
                "current-frame",
            )
        except VendorRateLimitError as exc:
            retry_after = (
                300.0
                if exc.retry_after_seconds is None
                else max(0.0, float(exc.retry_after_seconds))
            )
            if retry_after > 0:
                coordinator.record_rate_limit(
                    upstream_service_id=upstream_service_id,
                    cooldown_scope="market-snapshot",
                    observed_at=now,
                    retry_after=timedelta(seconds=retry_after),
                    provider_code=(
                        exc.error_code
                        or str(exc.status_code or "provider_capacity")
                    ),
                )
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.RATE_LIMITED,
                status_code=exc.status_code,
                retry_after_seconds=retry_after,
            ) from exc
        finally:
            coordinator.release(current_lease)


_INDICATOR_MINIMUM_HISTORY = {
    "macd": 26,
    "macds": 35,
    "macdh": 35,
    "rsi": 14,
    "boll": 20,
    "boll_ub": 20,
    "boll_lb": 20,
    "atr": 14,
    "vwma": 14,
}


def _provider_chain(symbol: str) -> list[str]:
    config = get_config()
    instrument = resolve_china_a_symbol(symbol)
    configured = "default"
    if instrument is not None:
        configured = (
            config.get("market_data_vendors", {})
            .get("cn_a", {})
            .get("core_stock_apis", "default")
        )
    if configured == "default":
        configured = config.get("data_vendors", {}).get("core_stock_apis", "default")
    if configured == "default":
        return list(SNAPSHOT_PROVIDERS)
    return [name.strip() for name in configured.split(",") if name.strip()]


def validate_ohlcv_frame(
    frame: pd.DataFrame, requested_date: str, start_date: str | None = None
) -> pd.DataFrame:
    required = ("Date", "Open", "High", "Low", "Close", "Volume")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")
    validated = frame.copy()
    validated["Date"] = pd.to_datetime(validated["Date"], errors="coerce")
    invalid_dates = validated["Date"].isna()
    if invalid_dates.any():
        rows = ", ".join(str(index) for index in validated.index[invalid_dates])
        raise ValueError(f"invalid Date at row(s) {rows}")
    requested = pd.Timestamp(requested_date).normalize()
    validated = validated[validated["Date"] <= requested]
    if start_date is not None:
        validated = validated[validated["Date"] >= pd.Timestamp(start_date).normalize()]
    if validated.empty:
        raise ValueError("no rows within the requested date range")
    duplicate_dates = validated["Date"].duplicated(keep=False)
    if duplicate_dates.any():
        date = validated.loc[duplicate_dates, "Date"].iloc[0].strftime("%Y-%m-%d")
        rows = ", ".join(str(index) for index in validated.index[duplicate_dates])
        raise ValueError(f"duplicate trading date {date} at row(s) {rows}")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        validated[column] = pd.to_numeric(validated[column], errors="coerce")
        non_numeric = validated[column].isna()
        if non_numeric.any():
            rows = ", ".join(str(index) for index in validated.index[non_numeric])
            raise ValueError(f"non-numeric {column} at row(s) {rows}")
        non_finite = ~np.isfinite(validated[column])
        if non_finite.any():
            rows = ", ".join(str(index) for index in validated.index[non_finite])
            raise ValueError(f"non-finite {column} at row(s) {rows}")
    negative_volume = validated["Volume"] < 0
    if negative_volume.any():
        rows = ", ".join(str(index) for index in validated.index[negative_volume])
        raise ValueError(f"negative Volume at row(s) {rows}")
    invalid = ~(
        (validated["Low"] <= validated["Open"])
        & (validated["Open"] <= validated["High"])
        & (validated["Low"] <= validated["Close"])
        & (validated["Close"] <= validated["High"])
    )
    if invalid.any():
        invalid_indices = tuple(validated.index[invalid])
        rows = ", ".join(str(index) for index in invalid_indices)
        raise OHLCVValidationError(
            "OHLC invariant failed at row(s) "
            f"{rows}: expected Low <= Open <= High and Low <= Close <= High",
            invalid_indices,
        )
    latest = validated["Date"].max().normalize()
    stale_days = (requested - latest).days
    if stale_days > MAX_OHLCV_STALE_DAYS:
        raise ValueError(
            f"latest row is {latest.strftime('%Y-%m-%d')}, {stale_days} days before "
            f"requested date {requested.strftime('%Y-%m-%d')} (stale)"
        )
    return validated.sort_values("Date").reset_index(drop=True)


def _authoritative_mainland_calendar(symbol: str):
    from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
    from tradingagents.market_history.store import MarketHistoryStore

    if resolve_china_a_symbol(symbol) is None:
        return None
    try:
        config = MarketHistoryConfig.from_mapping(get_config())
    except (KeyError, TypeError, ValueError):
        return None
    if config.mode is not MarketHistoryMode.AUTHORITATIVE:
        return None
    try:
        with MarketHistoryStore.open(config) as store:
            return store.read_current_session_calendar("mainland-cn")
    except Exception:
        return None


def _blocking_live_history_gaps(
    frame: pd.DataFrame,
    calendar,
    *,
    start_date: str,
    end_date: str,
    minimum_history_rows: int,
) -> tuple[str, ...]:
    from tradingagents.market_history.current import _analysis_as_of
    from tradingagents.market_history.maintenance import (
        assess_history_gaps,
        latest_completed_session,
    )

    requested_start = date.fromisoformat(start_date)
    requested_end = date.fromisoformat(end_date)
    expected_session = latest_completed_session(
        calendar,
        as_of=_analysis_as_of(
            requested_end,
            timezone_name=calendar.timezone_name,
        ),
    )
    if expected_session is None:
        return ()
    retained_dates = {
        value.date() for value in frame["Date"].dt.to_pydatetime()
    }
    eligible_dates = sorted(
        item
        for item in retained_dates
        if requested_start <= item <= expected_session
    )
    required_start = (
        eligible_dates[-minimum_history_rows]
        if len(eligible_dates) >= minimum_history_rows
        else requested_start
    )
    gaps = assess_history_gaps(
        sessions=calendar.sessions,
        observed_session_dates=retained_dates,
        suspension_session_dates=set(),
        lifecycle_start=min(retained_dates),
        lifecycle_end=None,
        required_start=required_start,
        required_end=expected_session,
    )
    return tuple(item.isoformat() for item in gaps.blocking_gaps)


def _validated_candidate_status(
    candidate: object,
    frame: pd.DataFrame,
    *,
    provider_name: str,
    requested_date: str,
) -> tuple[
    str,
    TradingStatusProvenance,
    Decimal | None,
    str | None,
    Decimal | None,
]:
    try:
        bundle = candidate.history_bundle
        statuses = tuple(bundle.trading_statuses)
        observations = tuple(bundle.observations)
        factors = tuple(bundle.adjustment_factors)
        current_tradeability = str(candidate.current_tradeability)
        provenance = candidate.current_status_provenance
        latest_traded_close = candidate.latest_traded_close
        latest_traded_close_diagnostic = candidate.latest_traded_close_diagnostic
        carried_suspension_close = candidate.carried_suspension_close
        calendar_sessions = tuple(candidate.calendar.sessions)
    except AttributeError as exc:
        raise AuthoritativeTradingStatusValidationError(
            "selected complete provider candidate dropped authoritative status evidence"
        ) from exc
    if not statuses or not observations or not factors:
        raise AuthoritativeTradingStatusValidationError(
            "selected complete provider candidate has incomplete status-bearing history"
        )
    frame_dates = tuple(value.date() for value in frame["Date"].dt.to_pydatetime())
    status_dates = tuple(item.session_date for item in statuses)
    observation_dates = tuple(item.session_date for item in observations)
    if status_dates != observation_dates or status_dates != frame_dates:
        raise AuthoritativeTradingStatusValidationError(
            "authoritative status membership contradicts the selected provider frame"
        )
    latest_status = statuses[-1]
    applicable_open_sessions = tuple(
        item.session_date
        for item in calendar_sessions
        if getattr(item.status, "value", item.status) == "open"
        and item.session_date <= date.fromisoformat(requested_date)
    )
    if (
        not applicable_open_sessions
        or max(applicable_open_sessions) != latest_status.session_date
    ):
        raise AuthoritativeTradingStatusValidationError(
            "authoritative status does not cover the latest applicable mainland session"
        )
    expected_tradeability = (
        "suspended"
        if latest_status.status is TradingStatus.SUSPENDED
        else "tradeable"
    )
    if current_tradeability != expected_tradeability:
        raise AuthoritativeTradingStatusValidationError(
            "selected provider tradeability contradicts its latest authoritative status"
        )
    if not isinstance(provenance, TradingStatusProvenance):
        raise AuthoritativeTradingStatusValidationError(
            "selected provider status provenance has an invalid type"
        )
    if (
        provenance.provider != provider_name
        or provenance.provider != bundle.provider.provider_name
        or provenance.provider_dataset_id != bundle.provider.provider_dataset_id
        or provenance.session_date != latest_status.session_date
        or provenance.status is not latest_status.status
        or provenance.observed_at != bundle.observed_at
        or provenance.revision_id is not None
    ):
        raise AuthoritativeTradingStatusValidationError(
            "selected provider status provenance contradicts its history bundle"
        )

    factor_dates = tuple(item.effective_date for item in factors)
    observations_by_date = {item.session_date: item for item in observations}
    expected_latest_traded_close: Decimal | None = None
    latest_factor: Decimal | None = None
    for status in statuses:
        factor_index = bisect_right(factor_dates, status.session_date) - 1
        if factor_index < 0:
            raise AuthoritativeTradingStatusValidationError(
                f"authoritative status has no adjustment factor for {status.session_date}"
            )
        factor = factors[factor_index].factor
        if status.status is TradingStatus.TRADED:
            expected_latest_traded_close = (
                observations_by_date[status.session_date].close * factor
            )
        if status is latest_status:
            latest_factor = factor
    if latest_traded_close != expected_latest_traded_close:
        raise AuthoritativeTradingStatusValidationError(
            "latest genuinely traded close contradicts authoritative status history"
        )
    expected_diagnostic = (
        None
        if expected_latest_traded_close is not None
        else "no_genuinely_traded_close_in_retained_history"
    )
    if latest_traded_close_diagnostic != expected_diagnostic:
        raise AuthoritativeTradingStatusValidationError(
            "latest genuinely traded close diagnostic contradicts retained history"
        )
    assert latest_factor is not None
    latest_observation = observations[-1]
    expected_current_close = (
        latest_status.official_carried_close
        if latest_status.status is TradingStatus.SUSPENDED
        else latest_observation.close
    )
    if expected_current_close is None:
        raise AuthoritativeTradingStatusValidationError(
            "authoritative suspension is missing its official carried close"
        )
    expected_carried_suspension_close = (
        expected_current_close * latest_factor
        if latest_status.status is TradingStatus.SUSPENDED
        else None
    )
    if carried_suspension_close != expected_carried_suspension_close:
        raise AuthoritativeTradingStatusValidationError(
            "carried suspension close contradicts authoritative status history"
        )
    latest_frame_row = frame.iloc[-1]
    if (
        float(latest_frame_row["Close"]) != float(expected_current_close * latest_factor)
        or (
            latest_status.status is TradingStatus.SUSPENDED
            and float(latest_frame_row["Volume"]) != 0.0
        )
    ):
        raise AuthoritativeTradingStatusValidationError(
            "selected provider frame contradicts its latest authoritative status"
        )
    return (
        current_tradeability,
        provenance,
        latest_traded_close,
        latest_traded_close_diagnostic,
        carried_suspension_close,
    )


def _resolve_snapshot_asset_configuration(
    symbol: str,
    *,
    explicit: RunAssetConfiguration | None,
    active: RunAssetConfiguration | None,
) -> RunAssetConfiguration | None:
    from tradingagents.dataflows.crypto_universe import is_crypto_pair_syntax

    if (
        explicit is not None
        and active is not None
        and explicit.asset_configuration_signature
        != active.asset_configuration_signature
    ):
        raise CryptoSnapshotIdentityMismatch(
            "snapshot publication received contradictory run assets"
        )
    configuration = explicit or active
    if not is_crypto_pair_syntax(symbol) and (
        configuration is None
        or configuration.instrument_kind.value != "crypto"
    ):
        return configuration
    if configuration is None:
        from tradingagents.asset_configuration import (
            RunAssetConfigurationError,
            resolve_run_asset_configuration,
        )
        from tradingagents.dataflows.config import get_config

        try:
            configuration = resolve_run_asset_configuration(
                symbol,
                config=get_config(),
            )
        except RunAssetConfigurationError as exc:
            raise CryptoSnapshotIdentityMismatch(
                "authoritative RunAssetConfiguration is required"
            ) from exc
    identity = configuration.instrument_identity
    provenance = identity.provenance
    if (
        configuration.asset_configuration_version != "1.0"
        or configuration.instrument_kind.value != "crypto"
        or not configuration.matches_symbol(symbol)
        or not identity.is_authoritative
        or provenance is None
        or provenance.artifact_sha256 != configuration.registry_digest
        or not configuration.registry_id.strip()
        or re.fullmatch(r"[0-9a-f]{64}", configuration.registry_digest) is None
        or identity.venue != "CCC"
        or identity.instrument_kind.value != "crypto"
        or identity.currency != "USD"
        or configuration.reference_market != "CCC"
        or configuration.capability_profile.profile_id != "crypto.v1"
        or configuration.capability_profile.contract_version != "1.0"
        or configuration.capability_profile.instrument_kind.value != "crypto"
        or configuration.observation_calendar_kind.value != "consecutive_daily"
        or configuration.adjustment_basis != "auto_adjusted"
    ):
        raise CryptoSnapshotIdentityMismatch(
            "crypto snapshot RunAssetConfiguration is contradictory"
        )
    return configuration


def _acquire_authoritative_market_snapshot(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_history_rows: int = 1,
    asset_configuration: RunAssetConfiguration | None = None,
) -> AuthoritativeMarketSnapshot:
    if minimum_history_rows < 1:
        raise ValueError("minimum_history_rows must be positive")
    active = _ACTIVE_SNAPSHOT_RUN.get()
    asset_configuration = _resolve_snapshot_asset_configuration(
        symbol,
        explicit=asset_configuration,
        active=(active.asset_configuration if active is not None else None),
    )
    from tradingagents.market_history.current import (
        try_read_authoritative_mainland_frame,
    )

    history_read = try_read_authoritative_mainland_frame(
        symbol,
        start_date,
        end_date,
        minimum_history_rows=minimum_history_rows,
    )
    if history_read.loaded is not None:
        stored = history_read.loaded
        frame = stored.frame.copy(deep=True)
        source_ref = stable_acquisition_source_ref(
            "market-history-bundle",
            stored.bundle_revision_id,
            start_date,
            end_date,
        )
        raw_text = _normalized_frame_text(frame)
        frame_digest = _frame_sha256(frame)
        artifact = SourceArtifact(
            artifact_sha256=frame_digest,
            source_ref=source_ref,
            tool_call_id=source_ref,
            tool_name="authoritative_market_snapshot_history_bundle_v1",
            raw_text=raw_text,
        )
        available = SourceAcquisitionAvailable(
            provider=stored.provider,
            provider_order=0,
            capability="market_snapshot",
            source_ref=source_ref,
            attempt=1,
            retrieved_at=history_read.retrieved_at or datetime.now(timezone.utc).isoformat(),
            artifact=artifact,
        )
        snapshot = AuthoritativeMarketSnapshot(
            symbol=symbol,
            frame=frame,
            provider=stored.provider,
            retrieved_at=available.retrieved_at,
            adjustment_basis=stored.adjustment_basis,
            requested_date=stored.requested_date,
            effective_trading_date=stored.effective_trading_date,
            frame_sha256=frame_digest,
            snapshot_id=stored.snapshot_id,
            snapshot_id_version=stored.snapshot_id_version,
            pin_membership_digest=stored.pin_membership_digest,
            snapshot_manifest_json=stored.manifest_json,
            acquisition_outcomes=(available,),
            source_artifact=artifact,
            history_store_status="stored",
            current_tradeability=stored.current_tradeability,
            current_status_provenance=stored.current_status_provenance,
            latest_traded_close=stored.latest_traded_close,
            latest_traded_close_diagnostic=stored.latest_traded_close_diagnostic,
            carried_suspension_close=stored.carried_suspension_close,
            asset_configuration=asset_configuration,
        )
        if active is not None:
            record = MarketSnapshotAcquisitionRecord(
                snapshot=snapshot,
                outcomes=(available,),
                source_artifact=artifact,
            )
            active.acquisition_records[(symbol, start_date, end_date)] = record
            active.latest_acquisition_records[
                (symbol.strip().upper(), str(end_date))
            ] = record
        return snapshot
    live_gap_calendar = _authoritative_mainland_calendar(symbol)
    controller = (
        active.acquisition_controller
        if active is not None
        else AcquisitionController(providers=())
    )
    provider_chain = _provider_chain(symbol)
    validation_quarantine: dict[str, QuarantinedSnapshot] = {}
    validated_frames: dict[str, pd.DataFrame] = {}
    history_candidates: dict[str, object] = {}
    crypto_provider_datasets: dict[str, CryptoProviderDatasetDescriptor] = {}
    physical_attempt_events: list[ProviderPhysicalAttemptEvent] = []

    def observe_physical_attempts(
        events: tuple[ProviderPhysicalAttemptEvent, ...],
    ) -> None:
        physical_attempt_events.extend(events)
        record_active_physical_attempt_events(events)

    def load_and_validate(
        provider_name: str, provider: SnapshotProvider
    ) -> pd.DataFrame:
        try:
            raw_candidate = _load_through_provider_coordinator(
                provider_name,
                provider,
                symbol,
                start_date,
                end_date,
                attempt_observer=observe_physical_attempts,
            )
        except (NoMarketDataError, ValueError) as exc:
            validation_quarantine[provider_name] = QuarantinedSnapshot(
                provider_name,
                str(exc),
                tuple(getattr(exc, "row_indices", ())),
            )
            reason = (
                AcquisitionUnavailableReason.NO_DATA
                if isinstance(exc, NoMarketDataError)
                else AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
            raise AcquisitionFailure(reason=reason) from exc
        raw_frame = getattr(raw_candidate, "frame", raw_candidate)
        if (
            hasattr(raw_candidate, "history_bundle")
            and hasattr(raw_candidate, "calendar")
        ):
            history_candidates[provider_name] = raw_candidate
        try:
            validated = validate_ohlcv_frame(raw_frame, end_date, start_date)
            blocking_gap_dates = (
                _blocking_live_history_gaps(
                    validated,
                    live_gap_calendar,
                    start_date=start_date,
                    end_date=end_date,
                    minimum_history_rows=minimum_history_rows,
                )
                if live_gap_calendar is not None
                else ()
            )
            if blocking_gap_dates:
                reason = "history gap: " + ",".join(blocking_gap_dates)
                validation_quarantine[provider_name] = QuarantinedSnapshot(
                    provider_name,
                    reason,
                )
                raise AcquisitionFailure(
                    reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
                )
            validated_frames[provider_name] = validated
            return validated
        except AcquisitionFailure:
            raise
        except ValueError as exc:
            validation_quarantine[provider_name] = QuarantinedSnapshot(
                provider_name,
                str(exc),
                tuple(getattr(exc, "row_indices", ())),
            )
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ) from exc

    provider_callables = []
    for provider_name in provider_chain:
        provider = SNAPSHOT_PROVIDERS.get(provider_name)
        if provider is None:
            def not_configured(_request, provider_name=provider_name):
                raise AcquisitionFailure(
                    reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
                )

            provider_callables.append((provider_name, not_configured))
        else:
            if (
                asset_configuration is not None
                and asset_configuration.instrument_kind.value == "crypto"
            ):
                if provider.adjustment_basis != asset_configuration.adjustment_basis:
                    raise CryptoSnapshotIdentityMismatch(
                        "crypto provider Adjustment Basis contradicts the run asset"
                    )
                from tradingagents.market_history.coordinator import (
                    upstream_service_identity_for_provider,
                )

                upstream_service_id, _ = upstream_service_identity_for_provider(
                    provider_name
                )
                crypto_provider_datasets[provider_name] = (
                    validate_crypto_provider_dataset_descriptor(
                        _crypto_dataset_for_provider(provider_name, provider),
                        provider_name=provider_name,
                        upstream_service_id=upstream_service_id,
                    )
                )
            provider_callables.append(
                (
                    provider_name,
                    lambda _request, provider_name=provider_name, provider=provider: (
                        load_and_validate(provider_name, provider)
                    ),
                )
            )
    providers = tuple(provider_callables)
    source_ref = stable_acquisition_source_ref(
        "market-snapshot",
        symbol,
        start_date,
        end_date,
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="market_snapshot",
            source_ref=source_ref,
            tool_call_id=source_ref,
            tool_name="authoritative_market_snapshot_normalized_frame_v1",
        ),
        providers=providers,
        validator=lambda value: value,
        serializer=_normalized_frame_text,
        accept_candidate=lambda frame: len(frame) >= minimum_history_rows,
        fallback_candidate_index=lambda frames: max(
            range(len(frames)), key=lambda index: len(frames[index])
        ),
    )
    if result.value is not None and result.artifact is not None:
        provider_name = result.provider
        assert provider_name is not None
        provider = SNAPSHOT_PROVIDERS[provider_name]
        frame = result.value
        effective_date = frame["Date"].max().strftime("%Y-%m-%d")
        quarantined_items: list[QuarantinedSnapshot] = []
        for outcome in result.outcomes:
            if outcome.outcome == "unavailable":
                quarantined_items.append(
                    validation_quarantine.get(
                        outcome.provider,
                        QuarantinedSnapshot(outcome.provider, outcome.reason.value),
                    )
                )
            elif outcome.provider != provider_name:
                candidate = validated_frames[outcome.provider]
                if len(candidate) < minimum_history_rows:
                    quarantined_items.append(
                        QuarantinedSnapshot(
                            outcome.provider,
                            "insufficient history: "
                            f"{len(candidate)} rows available; "
                            f"{minimum_history_rows} required",
                        )
                    )
        quarantined = tuple(quarantined_items)
        history_candidate = history_candidates.get(provider_name)
        if history_candidate is None:
            status_values = ("unknown", None, None, None, None)
        else:
            status_values = _validated_candidate_status(
                history_candidate,
                frame,
                provider_name=provider_name,
                requested_date=end_date,
            )
        snapshot = AuthoritativeMarketSnapshot(
            symbol=symbol,
            frame=frame,
            provider=provider_name,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            adjustment_basis=provider.adjustment_basis,
            requested_date=end_date,
            effective_trading_date=effective_date,
            quarantined=quarantined,
            acquisition_outcomes=result.outcomes,
            source_artifact=result.artifact,
            history_store_status=("degraded" if history_read.attempted else "live"),
            history_store_diagnostic=history_read.diagnostic,
            physical_attempt_events=tuple(physical_attempt_events),
            current_tradeability=status_values[0],
            current_status_provenance=status_values[1],
            latest_traded_close=status_values[2],
            latest_traded_close_diagnostic=status_values[3],
            carried_suspension_close=status_values[4],
            history_gap_dates=(),
            asset_configuration=asset_configuration,
            crypto_provider_dataset=crypto_provider_datasets.get(provider_name),
        )
        from tradingagents.market_history.shadow import (
            persist_mainland_history_candidate,
            persist_mainland_provider_frame,
        )

        persistence_diagnostics: list[str] = []
        if history_candidate is not None:
            history_write = persist_mainland_history_candidate(history_candidate)
            if history_write.attempted and not history_write.persisted:
                persistence_diagnostics.append(
                    history_write.diagnostic or "history_bundle_write_failed"
                )
        write_result = persist_mainland_provider_frame(
            symbol=symbol,
            provider=provider_name,
            adjustment_basis=provider.adjustment_basis,
            requested_start=start_date,
            requested_end=end_date,
            effective_trading_date=effective_date,
            retrieved_at=snapshot.retrieved_at,
            snapshot_id=snapshot.snapshot_id,
            frame_digest=snapshot.frame_sha256,
            history_rows=len(frame),
            normalized_frame_text=_normalized_frame_text(frame),
        )
        if write_result.attempted and not write_result.persisted:
            persistence_diagnostics.append(
                write_result.diagnostic or "shadow_write_failed"
            )
        if persistence_diagnostics:
            diagnostics = tuple(
                item
                for item in (
                    snapshot.history_store_diagnostic,
                    *persistence_diagnostics,
                )
                if item
            )
            snapshot = replace(
                snapshot,
                history_store_status="degraded",
                history_store_diagnostic=";".join(diagnostics),
            )
        if active is not None:
            record = MarketSnapshotAcquisitionRecord(
                snapshot=snapshot,
                outcomes=result.outcomes,
                source_artifact=result.artifact,
            )
            key = (symbol, start_date, end_date)
            active.acquisition_records[key] = record
            latest_key = (symbol.strip().upper(), str(end_date))
            latest = active.latest_acquisition_records.get(latest_key)
            if (
                latest is None
                or latest.snapshot is None
                or len(snapshot.frame) > len(latest.snapshot.frame)
            ):
                active.latest_acquisition_records[latest_key] = record
        return snapshot
    quarantined = [
        validation_quarantine.get(
            outcome.provider,
            QuarantinedSnapshot(outcome.provider, outcome.reason.value),
        )
        for outcome in result.outcomes
        if outcome.outcome == "unavailable"
    ]
    insufficient_candidates = []
    if active is not None:
        record = MarketSnapshotAcquisitionRecord(
            snapshot=None,
            outcomes=result.outcomes,
            source_artifact=None,
        )
        active.acquisition_records[(symbol, start_date, end_date)] = record
        latest_key = (symbol.strip().upper(), str(end_date))
        active.latest_acquisition_records.setdefault(latest_key, record)
    if insufficient_candidates:
        # Preserve the best valid frame so admission can report the actual row
        # count when no provider meets the requirement. Exclude the selected
        # provider from the quarantine list because it remains authoritative.
        provider_name, provider, frame, selected_rejection = max(
            insufficient_candidates,
            key=lambda candidate: len(candidate[2]),
        )
        effective_date = frame["Date"].max().strftime("%Y-%m-%d")
        return AuthoritativeMarketSnapshot(
            symbol=symbol,
            frame=frame,
            provider=provider_name,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            adjustment_basis=provider.adjustment_basis,
            requested_date=end_date,
            effective_trading_date=effective_date,
            quarantined=tuple(
                item for item in quarantined if item is not selected_rejection
            ),
            asset_configuration=asset_configuration,
            crypto_provider_dataset=crypto_provider_datasets.get(provider_name),
        )
    detail = "; ".join(f"{item.provider}: {item.reason}" for item in quarantined)
    raise NoMarketDataError(symbol, symbol, detail or "no configured snapshot provider")


def get_authoritative_market_snapshot(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_history_rows: int = 1,
    asset_configuration: RunAssetConfiguration | None = None,
) -> AuthoritativeMarketSnapshot:
    active = _ACTIVE_SNAPSHOT_RUN.get()
    asset_configuration = _resolve_snapshot_asset_configuration(
        symbol,
        explicit=asset_configuration,
        active=(active.asset_configuration if active is not None else None),
    )
    if active is None:
        return _acquire_authoritative_market_snapshot(
            symbol,
            start_date,
            end_date,
            minimum_history_rows=minimum_history_rows,
            asset_configuration=asset_configuration,
        )

    key = (symbol, start_date, end_date)
    with active.lock:
        if (
            asset_configuration is not None
            and asset_configuration.instrument_kind.value == "crypto"
        ):
            if active.asset_configuration is None:
                active.asset_configuration = asset_configuration
            elif (
                active.asset_configuration.asset_configuration_signature
                != asset_configuration.asset_configuration_signature
            ):
                raise CryptoSnapshotIdentityMismatch(
                    "snapshot run contradicts the authoritative crypto asset"
                )
        snapshot = active.snapshots.get(key)
        if (
            snapshot is not None
            and asset_configuration is not None
            and asset_configuration.instrument_kind.value == "crypto"
            and (
                snapshot.asset_configuration is None
                or snapshot.asset_configuration.asset_configuration_signature
                != asset_configuration.asset_configuration_signature
            )
        ):
            raise CryptoSnapshotIdentityMismatch(
                "cached crypto snapshot contradicts the authoritative run asset"
            )
        if snapshot is None or len(snapshot.frame) < minimum_history_rows:
            snapshot = _acquire_authoritative_market_snapshot(
                symbol,
                start_date,
                end_date,
                minimum_history_rows=minimum_history_rows,
                asset_configuration=asset_configuration,
            )
            active.snapshots[key] = snapshot
        latest_key = (symbol.strip().upper(), str(end_date))
        latest = active.latest_snapshots.get(latest_key)
        if latest is None or len(snapshot.frame) > len(latest.frame):
            active.latest_snapshots[latest_key] = snapshot
    return replace(snapshot, frame=snapshot.frame.copy(deep=True))


def get_active_authoritative_market_snapshot(
    symbol: str,
    end_date: str,
) -> AuthoritativeMarketSnapshot | None:
    """Return the newest run-scoped snapshot so shared evidence can follow it."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None:
        return None
    with active.lock:
        snapshot = active.latest_snapshots.get(
            (symbol.strip().upper(), str(end_date))
        )
        if snapshot is None:
            return None
        return replace(
            snapshot,
            frame=snapshot.frame.copy(deep=True),
            physical_attempt_events=tuple(active.physical_attempt_events),
        )


def get_active_market_snapshot_acquisition_record(
    symbol: str,
    end_date: str,
) -> MarketSnapshotAcquisitionRecord | None:
    """Return the immutable latest run acquisition record, including failures."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None:
        return None
    with active.lock:
        return active.latest_acquisition_records.get(
            (symbol.strip().upper(), str(end_date))
        )


def _minimum_history_for_indicator(indicator: str) -> int:
    moving_average = re.fullmatch(r"close_(\d+)_(?:sma|ema)", indicator)
    if moving_average:
        return int(moving_average.group(1))
    return _INDICATOR_MINIMUM_HISTORY.get(indicator, 1)


def build_authoritative_indicator_window(
    symbol: str, indicator: str, curr_date: str, look_back_days: int
) -> str:
    from .akshare_data import INDICATOR_DESCRIPTIONS

    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: "
            f"{list(INDICATOR_DESCRIPTIONS)}"
        )
    current = datetime.strptime(curr_date, "%Y-%m-%d")
    history_start = (current - relativedelta(years=5)).strftime("%Y-%m-%d")
    minimum_history = _minimum_history_for_indicator(indicator)
    snapshot = get_authoritative_market_snapshot(
        symbol,
        history_start,
        curr_date,
        minimum_history_rows=minimum_history,
    )
    stock_frame = wrap(snapshot.frame.copy())
    stock_frame["Date"] = pd.to_datetime(stock_frame["Date"]).dt.strftime("%Y-%m-%d")
    stock_frame[indicator]
    if minimum_history > 1:
        warmup_indices = stock_frame.index[: minimum_history - 1]
        stock_frame.loc[warmup_indices, indicator] = np.nan
    insufficient_history = (
        "N/A: insufficient history "
        f"({len(stock_frame)} rows available; {minimum_history} required)"
    )
    values = {
        row["Date"]: (
            insufficient_history
            if pd.isna(row[indicator]) and len(stock_frame) < minimum_history
            else ("N/A" if pd.isna(row[indicator]) else str(row[indicator]))
        )
        for _, row in stock_frame.iterrows()
    }
    before = current - relativedelta(days=look_back_days)
    lines = []
    cursor = current
    while cursor >= before:
        date = cursor.strftime("%Y-%m-%d")
        lines.append(
            f"{date}: {values.get(date, 'N/A: Not a trading day (weekend or holiday)')}"
        )
        cursor -= relativedelta(days=1)
    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + f"\n\nSource: Authoritative Market Snapshot\n"
        f"Provider: {snapshot.provider}\n"
        f"Adjustment basis: {snapshot.adjustment_basis}\n"
        f"Effective trading date: {snapshot.effective_trading_date}\n\n"
        f"Frame SHA-256: {snapshot.frame_sha256}\n"
        f"Snapshot ID: {snapshot.snapshot_id}\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )


def render_authoritative_market_data(
    symbol: str, start_date: str, end_date: str
) -> str:
    end = datetime.strptime(end_date, "%Y-%m-%d")
    history_start = (end - relativedelta(years=5)).strftime("%Y-%m-%d")
    snapshot = get_authoritative_market_snapshot(symbol, history_start, end_date)
    requested_start = pd.Timestamp(start_date).normalize()
    out = snapshot.frame[snapshot.frame["Date"] >= requested_start].copy()
    if out.empty:
        raise NoMarketDataError(
            symbol, symbol, f"no accepted rows on or after requested start {start_date}"
        )
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    lines = [
        f"# Authoritative Market Snapshot for {symbol} from {start_date} to {end_date}",
        f"# Provider: {snapshot.provider}",
        f"# Adjustment basis: {snapshot.adjustment_basis}",
        f"# Requested date: {snapshot.requested_date}",
        f"# Effective trading date: {snapshot.effective_trading_date}",
        f"# Retrieved at: {snapshot.retrieved_at}",
        f"# Frame SHA-256: {snapshot.frame_sha256}",
        f"# Snapshot ID: {snapshot.snapshot_id}",
        f"# Total records: {len(out)}",
    ]
    for rejected in snapshot.quarantined:
        lines.append(f"# Quarantined provider: {rejected.provider}; {rejected.reason}")
    return "\n".join(lines) + "\n\n" + out.to_csv(index=False)
