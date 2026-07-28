from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.maintenance import (
    assess_history_gaps,
    decide_history_request,
    latest_completed_session,
)
from tradingagents.market_history.models import ReconstructedMarketSnapshot, SnapshotPurpose
from tradingagents.market_history.store import MarketHistoryStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentHistoryRead:
    attempted: bool
    loaded: ReconstructedMarketSnapshot | None = None
    retrieved_at: str | None = None
    diagnostic: str | None = None
    history_gap_dates: tuple[str, ...] = ()


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
    try:
        with MarketHistoryStore.open(config) as store:
            if not store.mainland_cutover_ready():
                return CurrentHistoryRead(True, diagnostic="mainland_gate_not_passed")
            try:
                bundle = store.latest_qualified_history_bundle(
                    instrument.yahoo_symbol,
                    end_date,
                )
            except KeyError:
                return CurrentHistoryRead(
                    True,
                    diagnostic="qualified_history_bundle_unavailable",
                )
            calendar = store.read_current_session_calendar("mainland-cn")
            requested_date = date.fromisoformat(end_date)
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
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )
            retained_dates = {
                value.date() for value in loaded.frame["Date"].dt.to_pydatetime()
            }
            decision = decide_history_request(
                calendar,
                as_of=as_of,
                retained_session_dates=retained_dates,
            )
            if decision.should_request:
                return CurrentHistoryRead(
                    True,
                    diagnostic=f"qualified_history_bundle_stale:{decision.reason}",
                )
            requested_start = date.fromisoformat(start_date)
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
            gap_dates = tuple(item.isoformat() for item in gaps.blocking_gaps)
            if gap_dates:
                return CurrentHistoryRead(
                    True,
                    diagnostic="history_gap:" + ",".join(gap_dates),
                    history_gap_dates=gap_dates,
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
