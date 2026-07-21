from __future__ import annotations

import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    SourceAcquisitionOutcome,
    SourceArtifact,
    stable_acquisition_source_ref,
)

from .acquisition import AcquisitionController, AcquisitionFailure, AcquisitionRequest
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
    frame_sha256: str = ""
    snapshot_id: str = ""
    quarantined: tuple[QuarantinedSnapshot, ...] = ()
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = ()
    source_artifact: SourceArtifact | None = None

    def __post_init__(self) -> None:
        frame_digest = self.frame_sha256 or _frame_sha256(self.frame)
        identity = self.snapshot_id or _snapshot_id(
            symbol=self.symbol,
            provider=self.provider,
            adjustment_basis=self.adjustment_basis,
            requested_date=self.requested_date,
            effective_trading_date=self.effective_trading_date,
            frame_sha256=frame_digest,
            history_rows=len(self.frame),
        )
        object.__setattr__(self, "frame_sha256", frame_digest)
        object.__setattr__(self, "snapshot_id", identity)


@dataclass(frozen=True)
class MarketSnapshotAcquisitionRecord:
    snapshot: AuthoritativeMarketSnapshot | None
    outcomes: tuple[SourceAcquisitionOutcome, ...]
    source_artifact: SourceArtifact | None


def _normalized_frame_text(frame: pd.DataFrame) -> str:
    canonical = frame.copy()
    if "Date" in canonical:
        canonical["Date"] = pd.to_datetime(canonical["Date"]).dt.strftime("%Y-%m-%d")
    return canonical.to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.17g",
    )


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
    identity = "\0".join(
        (
            symbol.strip().upper(),
            provider,
            adjustment_basis,
            requested_date,
            effective_trading_date,
            frame_sha256,
            str(history_rows),
        )
    )
    return f"snapshot:{sha256(identity.encode('utf-8')).hexdigest()}"


@dataclass
class _AuthoritativeSnapshotRun:
    snapshots: dict[tuple[str, str, str], AuthoritativeMarketSnapshot] = field(
        default_factory=dict
    )
    latest_snapshots: dict[tuple[str, str], AuthoritativeMarketSnapshot] = field(
        default_factory=dict
    )
    acquisition_controller: AcquisitionController = field(
        default_factory=lambda: AcquisitionController(providers=())
    )
    acquisition_records: dict[
        tuple[str, str, str], MarketSnapshotAcquisitionRecord
    ] = field(default_factory=dict)
    latest_acquisition_records: dict[
        tuple[str, str], MarketSnapshotAcquisitionRecord
    ] = field(default_factory=dict)
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


def get_active_acquisition_controller() -> AcquisitionController | None:
    """Return the controller owned by the active analysis run, when present."""
    active = _ACTIVE_SNAPSHOT_RUN.get()
    return None if active is None else active.acquisition_controller


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


def _acquire_authoritative_market_snapshot(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_history_rows: int = 1,
) -> AuthoritativeMarketSnapshot:
    if minimum_history_rows < 1:
        raise ValueError("minimum_history_rows must be positive")
    active = _ACTIVE_SNAPSHOT_RUN.get()
    controller = (
        active.acquisition_controller
        if active is not None
        else AcquisitionController(providers=())
    )
    provider_chain = _provider_chain(symbol)
    validation_quarantine: dict[str, QuarantinedSnapshot] = {}
    validated_frames: dict[str, pd.DataFrame] = {}

    def load_and_validate(
        provider_name: str, provider: SnapshotProvider
    ) -> pd.DataFrame:
        try:
            raw_frame = provider.load(symbol, start_date, end_date)
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
        try:
            validated = validate_ohlcv_frame(raw_frame, end_date, start_date)
            validated_frames[provider_name] = validated
            return validated
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
        )
    detail = "; ".join(f"{item.provider}: {item.reason}" for item in quarantined)
    raise NoMarketDataError(symbol, symbol, detail or "no configured snapshot provider")


def get_authoritative_market_snapshot(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_history_rows: int = 1,
) -> AuthoritativeMarketSnapshot:
    active = _ACTIVE_SNAPSHOT_RUN.get()
    if active is None:
        return _acquire_authoritative_market_snapshot(
            symbol,
            start_date,
            end_date,
            minimum_history_rows=minimum_history_rows,
        )

    key = (symbol, start_date, end_date)
    with active.lock:
        snapshot = active.snapshots.get(key)
        if snapshot is None or len(snapshot.frame) < minimum_history_rows:
            snapshot = _acquire_authoritative_market_snapshot(
                symbol,
                start_date,
                end_date,
                minimum_history_rows=minimum_history_rows,
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
        return replace(snapshot, frame=snapshot.frame.copy(deep=True))


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
