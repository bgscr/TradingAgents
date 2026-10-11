from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.maintenance import (
    assess_history_gaps,
    decide_history_request,
    latest_completed_session,
)
from tradingagents.market_history.models import (
    InstrumentSpec,
    ProviderDatasetSpec,
    ReconstructedMarketSnapshot,
    SnapshotPurpose,
)
from tradingagents.market_history.store import MarketHistoryStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentHistoryRead:
    attempted: bool
    loaded: ReconstructedMarketSnapshot | None = None
    retrieved_at: str | None = None
    diagnostic: str | None = None
    history_gap_dates: tuple[str, ...] = ()
    retained: ReconstructedMarketSnapshot | None = None
    refresh_due: bool = False


def baostock_history_binding(symbol: str) -> tuple[ProviderDatasetSpec, InstrumentSpec]:
    """Expected adapter identity without importing the network SDK."""
    instrument = resolve_mainland_instrument(symbol)
    if instrument is None:
        raise ValueError("mainland instrument required")

    def identity(namespace, *components):
        digest = sha256()
        for component in (namespace, *components):
            encoded = str(component).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return f"{namespace}=sha256:{digest.hexdigest()}"

    from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
    upstream, service = upstream_service_identity_for_provider("baostock")
    return (
        ProviderDatasetSpec(upstream, service,
            identity("provider-dataset", upstream, "mainland-raw-status-factors-v1"),
            "baostock", "mainland-raw-status-factors-v1", "baostock-fore-factor-v1", True),
        InstrumentSpec(identity("instrument", instrument.yahoo_symbol,
            instrument.exchange, getattr(instrument, "instrument_kind", "equity"), "CNY"),
            instrument.yahoo_symbol, "mainland-cn",
            getattr(instrument, "instrument_kind", "equity"), "CNY", "mainland-routing-v1"),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _analysis_as_of(
    requested_date: date,
    *,
    timezone_name: str,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    current = _now().astimezone(zone)
    if requested_date >= current.date():
        return current
    return datetime.combine(requested_date, time.max, tzinfo=zone)


def try_read_authoritative_mainland_frame(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_history_rows: int,
    provider: ProviderDatasetSpec | None = None,
    instrument_identity: InstrumentSpec | None = None,
    listed_on: date | None = None,
) -> CurrentHistoryRead:
    try:
        config = MarketHistoryConfig.from_mapping(get_config())
    except (KeyError, TypeError, ValueError) as exc:
        return CurrentHistoryRead(True, diagnostic=f"configuration_invalid:{exc}")
    if config.mode is not MarketHistoryMode.AUTHORITATIVE:
        return CurrentHistoryRead(False)
    instrument = resolve_mainland_instrument(symbol)
    if instrument is None:
        return CurrentHistoryRead(False)
    if provider is not None and not provider.strict_history_qualified:
        return CurrentHistoryRead(True, diagnostic="provider_history_not_qualified")
    try:
        with MarketHistoryStore.open(config) as store:
            if not store.mainland_cutover_ready():
                return CurrentHistoryRead(True, diagnostic="mainland_gate_not_passed")
            try:
                bundle = store.latest_qualified_history_bundle(
                    instrument.yahoo_symbol,
                    end_date,
                    provider=provider,
                    instrument=instrument_identity,
                )
            except KeyError:
                return CurrentHistoryRead(
                    True,
                    diagnostic="qualified_history_bundle_unavailable",
                )
            requested_date = date.fromisoformat(end_date)
            requested_start = max(date.fromisoformat(start_date), listed_on or date.min)
            from tradingagents.market_history.calendar_discovery import (
                read_mainland_calendar_window,
            )
            calendar = (read_mainland_calendar_window(store, requested_start, requested_date)
                        or store.read_current_session_calendar("mainland-cn"))
            if requested_date < requested_start:
                return CurrentHistoryRead(True, diagnostic="invalid_history_window")
            calendar_dates = {item.session_date for item in calendar.sessions}
            calendar_incomplete = any(
                requested_start + timedelta(days=index) not in calendar_dates
                for index in range((requested_date - requested_start).days + 1)
            )
            as_of = _analysis_as_of(
                requested_date,
                timezone_name=calendar.timezone_name,
            )
            expected_session = latest_completed_session(calendar, as_of=as_of)
            if expected_session is None:
                return CurrentHistoryRead(
                    True,
                    diagnostic="no_completed_market_session",
                )
            loaded = store.reconstruct_snapshot(
                bundle.bundle_revision_id,
                requested_date=requested_date,
                requested_start=requested_start,
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )
            retained_dates = {
                value.date() for value in loaded.frame["Date"].dt.to_pydatetime()
            }
            watermark = None
            if provider is not None and instrument_identity is not None:
                with suppress(KeyError):
                    watermark = store.current_publication_watermark(
                        instrument_identity.instrument_id,
                        provider.provider_dataset_id,
                        expected_session,
                    )
                # Publication cooldown is wall-clock state, even when the user
                # requests an older as-of date. Never make it permanent.
                if (watermark is not None and watermark.cooldown_until is not None
                        and watermark.cooldown_until <= _now()):
                    watermark = None
            decision = decide_history_request(
                calendar,
                as_of=as_of,
                retained_session_dates=retained_dates,
                watermark=watermark,
            )
            if decision.reason == "publication_cooldown":
                return CurrentHistoryRead(True, diagnostic="publication_cooldown",
                                          retained=loaded)
            if calendar_incomplete:
                return CurrentHistoryRead(True, diagnostic="calendar_range_incomplete",
                    retained=loaded, refresh_due=requested_date > max(calendar_dates))
            required_start = requested_start
            gaps = assess_history_gaps(
                sessions=calendar.sessions,
                observed_session_dates=retained_dates,
                suspension_session_dates=set(),
                lifecycle_start=requested_start,
                lifecycle_end=None,
                required_start=required_start,
                required_end=expected_session,
            )
            gap_dates = tuple(item.isoformat() for item in gaps.blocking_gaps)
            if gap_dates:
                return CurrentHistoryRead(
                    True,
                    diagnostic="history_gap:" + ",".join(gap_dates),
                    history_gap_dates=gap_dates,
                    retained=loaded,
                    refresh_due=decision.should_request,
                )
            in_range_rows = sum(
                required_start <= item <= expected_session for item in retained_dates
            )
            if in_range_rows < minimum_history_rows:
                return CurrentHistoryRead(
                    True,
                    diagnostic=(
                        f"qualified_history_insufficient:{in_range_rows}/"
                        f"{minimum_history_rows}"
                    ),
                    retained=loaded,
                    refresh_due=decision.should_request,
                )
    except Exception as exc:
        logger.warning(
            "Authoritative market history read degraded for %s: %s",
            instrument.yahoo_symbol,
            exc,
        )
        return CurrentHistoryRead(True, diagnostic=f"history_store_read_failed:{exc}")
    return CurrentHistoryRead(
        True,
        loaded=loaded,
        retrieved_at=bundle.observed_at,
    )
