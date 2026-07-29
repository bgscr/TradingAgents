import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated
from uuid import uuid4

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFRateLimitError

from .config import get_config
from .errors import VendorRateLimitError
from .symbol_utils import NoMarketDataError, normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
MAX_OHLCV_STALE_DAYS = 10


class _YahooAdditionalRequestBlocked(Exception):
    def __init__(self, response) -> None:
        self.response = response
        super().__init__("yfinance attempted more than one network request per permit")


class _YahooSessionAttemptGuard:
    """Allow one low-level session call for each coordinator physical attempt."""

    def __init__(self, session) -> None:
        self.session = session
        self._remaining = 0
        self._consumed = False
        self._last_response = None
        for method_name in ("get", "post"):
            original = getattr(session, method_name)

            def guarded(*args, _original=original, **kwargs):
                if self._remaining < 1:
                    raise _YahooAdditionalRequestBlocked(self._last_response)
                self._remaining -= 1
                self._consumed = True
                from tradingagents.market_history.coordinator import (
                    record_active_physical_attempt_io,
                )

                record_active_physical_attempt_io()
                response = _original(*args, **kwargs)
                self._last_response = response
                return response

            setattr(session, method_name, guarded)

    def begin_attempt(self) -> None:
        self._remaining = 1
        self._consumed = False
        self._last_response = None

    def end_attempt(self) -> None:
        self._remaining = 0
        self._consumed = False
        self._last_response = None

    def mark_injected_transport_attempt(self) -> None:
        if self._remaining < 1:
            raise _YahooAdditionalRequestBlocked(self._last_response)
        self._remaining -= 1
        self._consumed = True
        from tradingagents.market_history.coordinator import (
            record_active_physical_attempt_io,
        )

        record_active_physical_attempt_io()

    @property
    def consumed(self) -> bool:
        return self._consumed

    @property
    def permit_active(self) -> bool:
        return self._remaining == 1 and not self._consumed


_YAHOO_SESSION_GUARD_LOCK = threading.Lock()
_YAHOO_SESSION_GUARD: _YahooSessionAttemptGuard | None = None


def _yahoo_session_guard() -> _YahooSessionAttemptGuard:
    global _YAHOO_SESSION_GUARD
    with _YAHOO_SESSION_GUARD_LOCK:
        if _YAHOO_SESSION_GUARD is None:
            from yfinance._http import new_session

            _YAHOO_SESSION_GUARD = _YahooSessionAttemptGuard(new_session())
        return _YAHOO_SESSION_GUARD


def _activate_yahoo_session_guard(guard: _YahooSessionAttemptGuard) -> None:
    from yfinance.config import YfConfig
    from yfinance.data import YfData

    YfConfig.network.retries = 0
    YfData(session=guard.session)


def _retry_after_from_response(response) -> float | None:
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    raw_value = headers.get("Retry-After") if headers is not None else None
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw_value))
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            return None
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


def _blocked_request_failure(exc: _YahooAdditionalRequestBlocked):
    from tradingagents.market_history import PhysicalAttemptFailure, PhysicalAttemptOutcome

    status_code = getattr(exc.response, "status_code", None)
    if status_code == 429:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            status_code=429,
            error_code="YAHOO_HTTP_429",
            retry_after_seconds=_retry_after_from_response(exc.response),
        )
    if status_code in {401, 403}:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            status_code=int(status_code),
            error_code=f"YAHOO_HTTP_{status_code}",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=status_code is None or int(status_code) < 400,
        status_code=int(status_code) if status_code is not None else None,
        error_code="YAHOO_ADDITIONAL_NETWORK_REQUEST_BLOCKED",
    )


def _physical_attempt_failure_from_yahoo_exception(exc: Exception):
    from tradingagents.market_history import (
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
    )

    if isinstance(exc, PhysicalAttemptFailure):
        return exc
    if isinstance(exc, _YahooAdditionalRequestBlocked):
        return _blocked_request_failure(exc)
    if isinstance(exc, (YFRateLimitError, VendorRateLimitError)):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            status_code=getattr(exc, "status_code", 429) or 429,
            error_code=getattr(exc, "error_code", None) or "YAHOO_HTTP_429",
            retry_after_seconds=getattr(exc, "retry_after_seconds", None),
        )
    if isinstance(exc, PermissionError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
        )
    if isinstance(exc, TimeoutError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
        )
    if isinstance(exc, (ConnectionError, OSError)):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
    )


def _execute_guarded_yahoo_attempt(
    func,
    *,
    session_guard: _YahooSessionAttemptGuard,
    injected_transport: bool,
    result_validator=None,
):
    """Run one Yahoo call under one permit with shared typing and cleanup."""
    from tradingagents.market_history import PhysicalAttemptFailure, PhysicalAttemptNotMade

    session_guard.begin_attempt()
    try:
        _activate_yahoo_session_guard(session_guard)
        if injected_transport:
            session_guard.mark_injected_transport_attempt()
        try:
            value = func()
            if result_validator is not None:
                value = result_validator(value)
        except PhysicalAttemptFailure:
            raise
        except Exception as exc:
            raise _physical_attempt_failure_from_yahoo_exception(exc) from exc
        if not session_guard.consumed:
            return PhysicalAttemptNotMade(value)
        return value
    finally:
        session_guard.end_attempt()


def yf_retry(
    func,
    *,
    request_key: str,
    operation: str,
    injected_transport: bool = False,
    result_validator=None,
):
    """Execute Yahoo I/O under the one shared coordinator retry budget."""
    from tradingagents.market_history import (
        MarketHistoryConfig,
        MarketHistoryStore,
        PhysicalAttemptBudgetExhausted,
        PhysicalAttemptOutcome,
        ProviderRequestCoordinator,
        RequestPriority,
        upstream_service_identity_for_provider,
    )

    history_config = MarketHistoryConfig.from_mapping(get_config())
    session_guard = _yahoo_session_guard()
    with MarketHistoryStore.open_provider_request_authority(history_config) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            "yfinance"
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)

        def physical_attempt(_attempt_index: int):
            return _execute_guarded_yahoo_attempt(
                func,
                session_guard=session_guard,
                injected_transport=injected_transport,
                result_validator=result_validator,
            )

        try:
            result = coordinator.execute_retry_sequence(
                request_key=f"yahoo:{operation}:{request_key}",
                upstream_service_id=upstream_service_id,
                owner_id=f"yahoo:{threading.get_ident()}:{uuid4().hex}",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: datetime.now(timezone.utc),
                sleep=time.sleep,
                lease_duration=timedelta(minutes=2),
                max_physical_attempts=history_config.yahoo_max_physical_attempts,
                operation=operation,
                physical_attempt=physical_attempt,
                cooldown_scope="all",
                record_at_physical_io=True,
            )
        except PhysicalAttemptBudgetExhausted as exc:
            _record_active_yahoo_attempts(exc.attempt_events)
            if exc.failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED:
                raise VendorRateLimitError(
                    status_code=exc.failure.status_code,
                    error_code=exc.failure.error_code,
                    retry_after_seconds=exc.failure.retry_after_seconds,
                ) from exc
            raise exc.failure from exc
        _record_active_yahoo_attempts(result.attempt_events)
        return result.value


def yf_acquire_once(
    func,
    *,
    request_key: str,
    operation: str,
    injected_transport: bool = False,
    result_validator=None,
):
    """Execute one Yahoo attempt; the caller owns any retry sequence."""

    from tradingagents.market_history import (
        MarketHistoryConfig,
        MarketHistoryStore,
        PhysicalAttemptBudgetExhausted,
        ProviderRequestCoordinator,
        RequestPriority,
        upstream_service_identity_for_provider,
    )

    history_config = MarketHistoryConfig.from_mapping(get_config())
    session_guard = _yahoo_session_guard()
    with MarketHistoryStore.open_provider_request_authority(history_config) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            "yfinance"
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)

        def physical_attempt(_attempt_index: int):
            return _execute_guarded_yahoo_attempt(
                func,
                session_guard=session_guard,
                injected_transport=injected_transport,
                result_validator=result_validator,
            )

        try:
            result = coordinator.execute_retry_sequence(
                request_key=f"yahoo:{operation}:{request_key}",
                upstream_service_id=upstream_service_id,
                owner_id=f"yahoo:{threading.get_ident()}:{uuid4().hex}",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: datetime.now(timezone.utc),
                sleep=time.sleep,
                lease_duration=timedelta(minutes=2),
                max_physical_attempts=1,
                operation=operation,
                physical_attempt=physical_attempt,
                cooldown_scope="all",
                record_at_physical_io=True,
            )
        except PhysicalAttemptBudgetExhausted as exc:
            _record_active_yahoo_attempts(exc.attempt_events)
            raise exc.failure from exc
        _record_active_yahoo_attempts(result.attempt_events)
        return result.value


def _record_active_yahoo_attempts(events) -> None:
    """Attach completed attempts to the current run without a module cycle."""
    try:
        from .market_snapshot import record_active_physical_attempt_events
    except ImportError:
        return
    record_active_physical_attempt_events(events)


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    curr_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than curr_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # Cache uses a fixed window (5y to today) so one file per symbol.
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when curr_date is the current day (#986). Look-ahead is still prevented by
    # the curr_date filter below.
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        from tradingagents.market_history import (
            PhysicalAttemptFailure,
            PhysicalAttemptOutcome,
        )

        def validate_download(value):
            if not isinstance(value, pd.DataFrame):
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                )
            normalized = _ensure_date_column(value.reset_index())
            if normalized.empty:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                    retryable=True,
                )
            if "Close" not in normalized.columns:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                )
            return normalized

        try:
            downloaded = yf_retry(
                lambda: yf.download(
                    canonical,
                    start=start_str,
                    end=end_str,
                    multi_level_index=False,
                    progress=False,
                    auto_adjust=True,
                ),
                request_key=f"download:{canonical}:{start_str}:{end_str}",
                operation="market-snapshot",
                injected_transport=(
                    not type(yf.download).__module__.startswith("yfinance")
                ),
                result_validator=validate_download,
            )
        except PhysicalAttemptFailure as exc:
            if exc.outcome in {
                PhysicalAttemptOutcome.EMPTY_FRAME,
                PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            }:
                raise NoMarketDataError(
                    symbol,
                    canonical,
                    "Yahoo Finance returned no usable rows",
                ) from exc
            raise
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    # Reject a stale frame (latest row far older than curr_date) rather than
    # feeding year-old prices into indicators (#1021).
    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
