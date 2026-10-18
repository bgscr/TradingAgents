"""Current-analysis Tushare raw/status/factor bundles for one mainland equity.

This adapter does not confer strict-history qualification (ADR-0034). Daily
prices exclude suspended sessions, and suspend_d supplies no official carried
close. Missing open-session bars therefore fail closed rather than being filled.
Tushare points are an access tier, not a consumable request balance.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256

import pandas as pd

from tradingagents.market_history.calendar import (
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
)
from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
from tradingagents.market_history.maintenance import latest_completed_session
from tradingagents.market_history.models import (
    AdjustmentFactorObservation,
    InstrumentSpec,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    RawMarketObservation,
    TradingStatus,
    TradingStatusObservation,
    TradingStatusProvenance,
)
from tradingagents.picker.errors import (
    PITError,
    TushareNotConfiguredError,
    TushareRateLimitError,
)
from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.tushare_provider import RequestExecutor, TushareProvider

from .errors import NoMarketDataError
from .symbol_utils import MainlandInstrument, resolve_mainland_instrument

NORMALIZER_VERSION = "tushare-market-history-normalizer-v1"
ADJUSTMENT_METHODOLOGY = "tushare-adj-factor-divide-end-anchor-qfq-v1"
DATASET_NAME = "mainland-daily-suspend-d-adj-factor-v1"
CALENDAR_DATASET_NAME = "tushare-mainland-trade-cal-v1"


@dataclass(frozen=True)
class TushareSnapshotHistoryCandidate:
    frame: pd.DataFrame
    history_bundle: ProviderHistoryBundlePublication
    calendar: MarketSessionCalendarPublication
    current_tradeability: str
    current_status_provenance: TradingStatusProvenance
    latest_traded_close: Decimal | None
    latest_traded_close_diagnostic: str | None
    carried_suspension_close: Decimal | None
    bundle_revision_id: str | None = None
    history_requested_ranges: tuple[tuple[str, str], ...] = ()
    history_local_rows: int = 0


def history_binding(symbol: str) -> tuple[ProviderDatasetSpec, InstrumentSpec]:
    """Return the exact dataset/instrument binding without constructing a client."""
    instrument = _instrument(symbol)
    upstream_id, upstream_name = upstream_service_identity_for_provider("tushare")
    provider = ProviderDatasetSpec(
        upstream_service_id=upstream_id,
        upstream_service_name=upstream_name,
        provider_dataset_id=_identity(
            "provider-dataset", upstream_id, DATASET_NAME,
            ADJUSTMENT_METHODOLOGY, NORMALIZER_VERSION,
        ),
        provider_name="tushare",
        dataset_name=DATASET_NAME,
        adjustment_methodology=ADJUSTMENT_METHODOLOGY,
        strict_history_qualified=False,
    )
    spec = InstrumentSpec(
        instrument_id=_identity(
            "instrument", instrument.yahoo_symbol, instrument.exchange, "equity", "CNY"
        ),
        canonical_symbol=instrument.yahoo_symbol,
        reference_market="mainland-cn",
        instrument_kind="equity",
        currency="CNY",
        identity_revision="mainland-routing-v1",
    )
    return provider, spec


def load_snapshot_with_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
    provider: TushareProvider | None = None,
    request_executor: RequestExecutor | None = None,
    adjustment_as_of: str | None = None,
) -> TushareSnapshotHistoryCandidate:
    """Acquire one atomic current-analysis candidate; perform no persistence.

    Every physical page goes through the supplied request executor and/or
    coordinator callback. There are no retries or permission probes here.
    Factor acquisition deliberately omits start_date: a revision changes the
    qfq anchor for *all* retained history, including rows before the raw refresh.
    The caller must reject retained dates absent from this complete factor set.
    """
    instrument = _instrument(symbol)
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise ValueError("Tushare history start must not follow end")
    binding, instrument_spec = history_binding(symbol)
    ts_code = instrument.yahoo_symbol.replace(".SS", ".SH")
    exchange = "SSE" if instrument.exchange == "shanghai" else "SZSE"

    def execute(endpoint: str, request: Callable[[], object]) -> object:
        if before_physical_request is not None:
            before_physical_request(endpoint)
        return request_executor(endpoint, request) if request_executor else request()

    try:
        source = (
            provider
            if provider is not None
            else TushareProvider.create(PITConfig.from_env())
        )
        common = {"ts_code": ts_code, "end_date": end.strftime("%Y%m%d")}
        range_args = {**common, "start_date": start.strftime("%Y%m%d")}
        raw = source.fetch_range("daily", **range_args, request_executor=execute)
        factor_frame = source.fetch_range(
            "adj_factor", ts_code=ts_code,
            end_date=(adjustment_as_of or end_date).replace("-", ""), request_executor=execute
        )
        suspension_frame = source.fetch_range(
            "suspend_d", **range_args, request_executor=execute
        )
        calendar_frame = source.fetch_range(
            "trade_cal", exchange=exchange, start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"), request_executor=execute,
        )
    except (TushareNotConfiguredError, TushareRateLimitError):
        raise
    except PITError:
        raise NoMarketDataError(
            symbol, ts_code, "Tushare could not acquire the complete history bundle"
        ) from None

    try:
        return _candidate(
            raw, factor_frame, suspension_frame, calendar_frame,
            binding=binding, instrument=instrument_spec, ts_code=ts_code,
            exchange=exchange, start=start, end=end,
            factor_end=date.fromisoformat(adjustment_as_of or end_date),
        )
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise NoMarketDataError(symbol, ts_code, f"Tushare history unavailable: {exc}") from None


def _candidate(
    raw: pd.DataFrame,
    factor_frame: pd.DataFrame,
    suspension_frame: pd.DataFrame,
    calendar_frame: pd.DataFrame,
    *,
    binding: ProviderDatasetSpec,
    instrument: InstrumentSpec,
    ts_code: str,
    exchange: str,
    start: date,
    end: date,
    factor_end: date | None = None,
    observed_at: datetime | None = None,
) -> TushareSnapshotHistoryCandidate:
    raw_rows = _rows(
        raw, ("ts_code", "trade_date", "open", "high", "low", "close", "vol")
    )
    factor_rows = _rows(factor_frame, ("ts_code", "trade_date", "adj_factor"))
    suspension_rows = _rows(
        suspension_frame, ("ts_code", "trade_date", "suspend_type", "suspend_timing"),
        allow_empty=True,
    )
    calendar_rows = _rows(calendar_frame, ("exchange", "cal_date", "is_open"))
    calendar_by_day: dict[date, MarketSessionStatus] = {}
    for row in calendar_rows:
        day = _day(row["cal_date"])
        if row["exchange"] != exchange or not start <= day <= end:
            raise ValueError("calendar identity/range mismatch")
        flag = _decimal(row["is_open"])
        if flag not in (Decimal(0), Decimal(1)):
            raise ValueError("unknown calendar session status")
        status = MarketSessionStatus.OPEN if flag == 1 else MarketSessionStatus.CLOSED
        if day in calendar_by_day and calendar_by_day[day] is not status:
            raise ValueError("conflicting calendar session status")
        calendar_by_day[day] = status
    expected_days = {start + timedelta(days=n) for n in range((end - start).days + 1)}
    if set(calendar_by_day) != expected_days:
        raise ValueError("incomplete session calendar")
    observed_at = observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("Tushare observation time must be timezone aware")
    calendar = MarketSessionCalendarPublication(
        provider=ProviderDatasetSpec(
            upstream_service_id=binding.upstream_service_id,
            upstream_service_name=binding.upstream_service_name,
            provider_dataset_id=_identity(
                "provider-dataset", binding.upstream_service_id,
                CALENDAR_DATASET_NAME, "not-applicable", NORMALIZER_VERSION, exchange,
            ),
            provider_name="tushare", dataset_name=CALENDAR_DATASET_NAME,
            adjustment_methodology="not-applicable", strict_history_qualified=False,
        ),
        reference_market="mainland-cn", timezone_name="Asia/Shanghai",
        observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=json.dumps(calendar_rows, sort_keys=True).encode("utf-8"),
        sessions=tuple(
            MarketSession(day, calendar_by_day[day]) for day in sorted(calendar_by_day)
        ),
    )
    completed = latest_completed_session(calendar, as_of=observed_at)
    open_days = {
        day for day, status in calendar_by_day.items()
        if status is MarketSessionStatus.OPEN and completed is not None and day <= completed
    }

    full_day_suspensions: set[date] = set()
    for row in suspension_rows:
        day = _security_day(row, ts_code, start, end)
        if row["suspend_type"] not in {"S", "R"}:
            raise ValueError("unknown suspension type")
        if row["suspend_type"] == "S" and not row["suspend_timing"]:
            full_day_suspensions.add(day)

    observations_by_day: dict[date, RawMarketObservation] = {}
    for row in raw_rows:
        day = _security_day(row, ts_code, start, end)
        if day not in open_days or day in full_day_suspensions:
            raise ValueError("daily bar conflicts with calendar or full-day suspension")
        prices = tuple(_decimal(row[name]) for name in ("open", "high", "low", "close"))
        volume = _decimal(row["vol"]) * 100  # Tushare lots -> canonical shares.
        if any(value <= 0 for value in prices) or volume <= 0:
            raise ValueError("daily bar does not prove a positive-price traded session")
        observation = RawMarketObservation(day, *prices, volume)
        if day in observations_by_day and observations_by_day[day] != observation:
            raise ValueError("conflicting daily bars")
        observations_by_day[day] = observation
    if set(observations_by_day) != open_days:
        # Even a confirmed suspension cannot supply the missing official close.
        raise ValueError("open-session history gap; no official suspension carried close")
    if not observations_by_day:
        raise ValueError("no traded sessions in requested range")

    cumulative_factors: dict[date, Decimal] = {}
    for row in factor_rows:
        day = _security_day(row, ts_code, date.min, factor_end or end)
        factor = _decimal(row["adj_factor"])
        if factor <= 0:
            raise ValueError("adjustment factor must be positive")
        if day in cumulative_factors and cumulative_factors[day] != factor:
            raise ValueError("conflicting adjustment factors")
        cumulative_factors[day] = factor
    if not open_days.issubset(cumulative_factors):
        raise ValueError("missing exact-date adjustment factors")
    # An intraday factor may be published before that session's daily bar.
    # Derive the completed view using its own final session, never that future anchor.
    anchor_day = max(observations_by_day)
    anchor_factor = cumulative_factors[anchor_day]
    factors = tuple(
        AdjustmentFactorObservation(day, cumulative_factors[day] / anchor_factor)
        for day in sorted(cumulative_factors)
    )
    observations = tuple(
        observations_by_day[day] for day in sorted(observations_by_day)
    )
    statuses = tuple(
        TradingStatusObservation(row.session_date, TradingStatus.TRADED, volume=row.volume)
        for row in observations
    )
    multipliers = {row.effective_date: row.factor for row in factors}
    frame = pd.DataFrame([
        {
            "Date": pd.Timestamp(row.session_date),
            "Open": float(row.open * multipliers[row.session_date]),
            "High": float(row.high * multipliers[row.session_date]),
            "Low": float(row.low * multipliers[row.session_date]),
            "Close": float(row.close * multipliers[row.session_date]),
            "Volume": float(row.volume),
        }
        for row in observations
    ])
    raw_payload = json.dumps(
        {
            "normalizer_version": NORMALIZER_VERSION,
            "adjustment_methodology": ADJUSTMENT_METHODOLOGY,
            "factor_scope": "all-history-through-requested-end",
            "qfq_anchor_date": anchor_day.isoformat(),
            "qfq_anchor_factor": str(anchor_factor),
            "raw_history": raw_rows,
            "adjustment_factors": factor_rows,
            "suspensions": suspension_rows,
            "calendar": calendar_rows,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    publication = ProviderHistoryBundlePublication(
        provider=binding, instrument=instrument, requested_as_of=end,
        retrieval_cutoff=observed_at, observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=raw_payload, observations=observations,
        trading_statuses=statuses, adjustment_factors=factors,
    )
    calendar = replace(calendar, raw_payload=raw_payload)
    return TushareSnapshotHistoryCandidate(
        frame=frame, history_bundle=publication, calendar=calendar,
        current_tradeability="tradeable",
        current_status_provenance=TradingStatusProvenance(
            provider="tushare", provider_dataset_id=binding.provider_dataset_id,
            session_date=observations[-1].session_date, status=TradingStatus.TRADED,
            observed_at=observed_at,
        ),
        latest_traded_close=(
            observations[-1].close * multipliers[observations[-1].session_date]
        ),
        latest_traded_close_diagnostic=None, carried_suspension_close=None,
    )


def load_calendar(symbol, start_date, end_date, *, provider=None, request_executor=None):
    """Acquire calendar membership without making an instrument-price request."""
    security = _instrument(symbol)
    exchange = "SSE" if security.exchange == "shanghai" else "SZSE"
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise ValueError("invalid CURRENT calendar range")
    source = provider if provider is not None else TushareProvider.create(PITConfig.from_env())
    page_dates = {}

    def check_rows(rows, seen):
        for row in rows:
            day = _day(row["cal_date"])
            flag = _decimal(row["is_open"])
            if row["exchange"] != exchange or not start <= day <= end or flag not in (0, 1) or day in seen:
                raise ValueError("invalid CURRENT calendar membership")
            seen[day] = str(int(flag))

    # Check physical pages before the provider's generic concatenation deduplicates
    # them. Duplicate dates (including identical rows across pages) are not evidence.
    def execute(endpoint, request):
        def checked():
            frame = request()
            rows = _rows(frame, ("exchange", "cal_date", "is_open"), allow_empty=True)
            check_rows(rows, page_dates)
            return frame
        return request_executor(endpoint, checked) if request_executor else checked()

    frame = source.fetch_range("trade_cal", exchange=exchange,
        start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""),
        request_executor=execute)
    rows = _rows(frame, ("exchange", "cal_date", "is_open"))
    result = {}
    check_rows(rows, result)
    if set(result) != {start + timedelta(days=index) for index in range((end-start).days+1)}:
        raise ValueError("CURRENT calendar acquisition incomplete")
    binding, _ = history_binding(symbol)
    return MarketSessionCalendarPublication(
        provider=ProviderDatasetSpec(binding.upstream_service_id, binding.upstream_service_name,
            _identity("provider-dataset", binding.upstream_service_id, CALENDAR_DATASET_NAME,
                      "not-applicable", NORMALIZER_VERSION, exchange),
            "tushare", CALENDAR_DATASET_NAME, "not-applicable", False),
        reference_market="mainland-cn", timezone_name="Asia/Shanghai",
        observed_at=datetime.now(timezone.utc), provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=json.dumps({"calendar": rows, "normalizer_version": NORMALIZER_VERSION,
                               "adjustment_methodology": ADJUSTMENT_METHODOLOGY}, sort_keys=True).encode(),
        sessions=tuple(MarketSession(day, MarketSessionStatus.OPEN if flag == "1" else MarketSessionStatus.CLOSED)
                       for day, flag in sorted(result.items())))


def _instrument(symbol: str) -> MainlandInstrument:
    instrument = resolve_mainland_instrument(symbol)
    if (
        instrument is None or instrument.instrument_kind != "equity"
        or instrument.akshare_code.startswith(("200", "900"))
    ):
        raise NoMarketDataError(
            symbol, symbol, "Tushare history requires a mainland CNY equity"
        )
    return instrument


def _rows(
    frame: pd.DataFrame, fields: tuple[str, ...], *, allow_empty: bool = False,
) -> list[dict[str, str | None]]:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("endpoint response must be a DataFrame")
    if frame.empty:
        if allow_empty:
            return []
        raise ValueError("required endpoint returned no rows")
    if not set(fields).issubset(frame.columns) or not frame.columns.is_unique:
        raise ValueError("endpoint response has missing or duplicate fields")
    return [
        {str(key): None if pd.isna(value) else str(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _day(value: object) -> date:
    text = str(value)
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        raise ValueError("invalid provider date")
    return datetime.strptime(text, "%Y%m%d").date()


def _security_day(row: dict[str, str | None], ts_code: str, start: date, end: date) -> date:
    if row["ts_code"] != ts_code:
        raise ValueError("endpoint security identity mismatch")
    day = _day(row["trade_date"])
    if not start <= day <= end:
        raise ValueError("endpoint date outside requested range")
    return day


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid provider numeric value") from None
    if not result.is_finite():
        raise ValueError("non-finite provider numeric value")
    return result


def _identity(namespace: str, *components: object) -> str:
    digest = sha256()
    for component in (namespace, *components):
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{namespace}=sha256:{digest.hexdigest()}"
