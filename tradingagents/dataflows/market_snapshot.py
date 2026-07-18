from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .config import get_config
from .errors import NoMarketDataError
from .stockstats_utils import MAX_OHLCV_STALE_DAYS
from .symbol_utils import resolve_china_a_symbol


@dataclass(frozen=True)
class SnapshotProvider:
    load: Callable[[str, str, str], pd.DataFrame]
    adjustment_basis: str


@dataclass(frozen=True)
class QuarantinedSnapshot:
    provider: str
    reason: str
    row_indices: tuple[object, ...] = ()


class OHLCVValidationError(ValueError):
    def __init__(self, message: str, row_indices: tuple[object, ...] = ()):
        self.row_indices = row_indices
        super().__init__(message)


@dataclass(frozen=True)
class AuthoritativeMarketSnapshot:
    symbol: str
    frame: pd.DataFrame
    provider: str
    retrieved_at: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    quarantined: tuple[QuarantinedSnapshot, ...] = ()


@dataclass
class _AuthoritativeSnapshotRun:
    snapshots: dict[tuple[str, str, str], AuthoritativeMarketSnapshot] = field(
        default_factory=dict
    )
    lock: threading.Lock = field(default_factory=threading.Lock)


_ACTIVE_SNAPSHOT_RUN: ContextVar[_AuthoritativeSnapshotRun | None] = ContextVar(
    "active_authoritative_snapshot_run",
    default=None,
)


@contextmanager
def authoritative_snapshot_run():
    """Reuse accepted market frames for the duration of one analysis run."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is not None:
        yield
        return

    token = _ACTIVE_SNAPSHOT_RUN.set(_AuthoritativeSnapshotRun())
    try:
        yield
    finally:
        _ACTIVE_SNAPSHOT_RUN.reset(token)


def _load_akshare(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from .akshare_data import load_ohlcv_range

    return load_ohlcv_range(symbol, start_date, end_date)


def _load_baostock(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from .baostock_data import load_ohlcv_range

    return load_ohlcv_range(symbol, start_date, end_date)


def _load_yfinance(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from .y_finance import load_ohlcv_range

    return load_ohlcv_range(symbol, start_date, end_date)


SNAPSHOT_PROVIDERS: dict[str, SnapshotProvider] = {
    "akshare": SnapshotProvider(_load_akshare, "qfq"),
    "baostock": SnapshotProvider(_load_baostock, "qfq"),
    "yfinance": SnapshotProvider(_load_yfinance, "auto_adjusted"),
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


def _acquire_authoritative_market_snapshot(
    symbol: str, start_date: str, end_date: str
) -> AuthoritativeMarketSnapshot:
    quarantined = []
    for provider_name in _provider_chain(symbol):
        provider = SNAPSHOT_PROVIDERS.get(provider_name)
        if provider is None:
            continue
        try:
            frame = validate_ohlcv_frame(
                provider.load(symbol, start_date, end_date), end_date, start_date
            )
        except Exception as exc:  # noqa: BLE001 - invalid providers are quarantined
            row_indices = getattr(exc, "row_indices", ())
            quarantined.append(
                QuarantinedSnapshot(provider_name, str(exc), tuple(row_indices))
            )
            continue
        effective_date = frame["Date"].max().strftime("%Y-%m-%d")
        return AuthoritativeMarketSnapshot(
            symbol=symbol,
            frame=frame,
            provider=provider_name,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            adjustment_basis=provider.adjustment_basis,
            requested_date=end_date,
            effective_trading_date=effective_date,
            quarantined=tuple(quarantined),
        )
    detail = "; ".join(f"{item.provider}: {item.reason}" for item in quarantined)
    raise NoMarketDataError(symbol, symbol, detail or "no configured snapshot provider")


def get_authoritative_market_snapshot(
    symbol: str, start_date: str, end_date: str
) -> AuthoritativeMarketSnapshot:
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None:
        return _acquire_authoritative_market_snapshot(symbol, start_date, end_date)

    key = (symbol, start_date, end_date)
    with active.lock:
        snapshot = active.snapshots.get(key)
        if snapshot is None:
            snapshot = _acquire_authoritative_market_snapshot(
                symbol,
                start_date,
                end_date,
            )
            active.snapshots[key] = snapshot
    return replace(snapshot, frame=snapshot.frame.copy(deep=True))


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
    snapshot = get_authoritative_market_snapshot(symbol, history_start, curr_date)
    stock_frame = wrap(snapshot.frame.copy())
    stock_frame["Date"] = pd.to_datetime(stock_frame["Date"]).dt.strftime("%Y-%m-%d")
    stock_frame[indicator]
    values = {
        row["Date"]: "N/A" if pd.isna(row[indicator]) else str(row[indicator])
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
        f"# Total records: {len(out)}",
    ]
    for rejected in snapshot.quarantined:
        lines.append(f"# Quarantined provider: {rejected.provider}; {rejected.reason}")
    return "\n".join(lines) + "\n\n" + out.to_csv(index=False)
