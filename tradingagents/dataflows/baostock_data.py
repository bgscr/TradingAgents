from __future__ import annotations

import json
import logging
import threading
from bisect import bisect_right
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256

import baostock as bs
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.evidence import SourceArtifact, stable_acquisition_source_ref
from tradingagents.market_history.calendar import MarketSessionCalendarPublication
from tradingagents.market_history.coordinator import record_active_physical_attempt_io
from tradingagents.market_history.models import (
    ProviderHistoryBundlePublication,
    TradingStatusObservation,
    TradingStatusProvenance,
)

from .akshare_data import INDICATOR_DESCRIPTIONS
from .baostock_output import contain_baostock_output
from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError
from .market_snapshot import validate_ohlcv_frame
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol

logger = logging.getLogger(__name__)

FIELDS = "date,code,open,high,low,close,volume,amount"
CURRENT_SNAPSHOT_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
)
RAW_HISTORY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
)
_BAOSTOCK_SESSION_LOCK = threading.Lock()
_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class BaoStockSnapshotHistoryCandidate:
    frame: pd.DataFrame
    history_bundle: ProviderHistoryBundlePublication
    calendar: MarketSessionCalendarPublication
    current_tradeability: str
    current_status_provenance: TradingStatusProvenance
    latest_traded_close: Decimal | None
    latest_traded_close_diagnostic: str | None
    carried_suspension_close: Decimal | None


@dataclass(frozen=True)
class BaoStockCurrentSnapshotCandidate:
    frame: pd.DataFrame
    trading_statuses: tuple[TradingStatusObservation, ...]
    current_tradeability: str
    current_status_provenance: TradingStatusProvenance
    status_artifact: SourceArtifact
    latest_traded_close: Decimal | None
    latest_traded_close_diagnostic: str | None
    carried_suspension_close: Decimal | None


@contextmanager
def _session(physical_request=None, *, record_physical_io: bool = False):
    acquired = _BAOSTOCK_SESSION_LOCK.acquire(timeout=_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        message = (
            f"Timed out after {_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS:g}s "
            "waiting for the BaoStock session lock; another session may be hung."
        )
        logger.warning(message)
        raise TimeoutError(message)

    def login_request():
        with contain_baostock_output():
            if record_physical_io:
                record_active_physical_attempt_io()
            login = bs.login()
        if getattr(login, "error_code", "0") == "10001005":
            raise VendorRateLimitError(
                f"Baostock login capacity unavailable: {login.error_msg}",
                error_code="10001005",
            )
        if getattr(login, "error_code", "0") != "0":
            raise VendorNotConfiguredError(f"Baostock login failed: {login.error_msg}")
        return True

    def logout_request():
        with contain_baostock_output():
            if record_physical_io:
                record_active_physical_attempt_io()
            bs.logout()
        return True

    try:
        if physical_request is None:
            login_request()
        else:
            physical_request("baostock-login", login_request)
        primary_error = False
        try:
            yield
        except BaseException:
            primary_error = True
            raise
        finally:
            try:
                if physical_request is None:
                    logout_request()
                else:
                    physical_request("baostock-logout", logout_request)
            except Exception:
                if not primary_error:
                    raise
                logger.warning("BaoStock logout unavailable after failed acquisition")
    finally:
        _BAOSTOCK_SESSION_LOCK.release()


def _rows_to_frame(rows: list[list[str]], symbol: str, canonical: str) -> pd.DataFrame:
    if not rows:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no rows")

    frame = pd.DataFrame(
        rows,
        columns=["Date", "Code", "Open", "High", "Low", "Close", "Volume", "Amount"],
    )
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]]
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if frame.empty:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no usable rows")
    return frame.sort_values("Date")


def load_ohlcv_range(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")

    with _session():
        if before_physical_request is not None:
            before_physical_request("adjusted-history")
        with contain_baostock_output():
            rs = bs.query_history_k_data_plus(
                instrument.baostock_code,
                FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            if getattr(rs, "error_code", "0") != "0":
                raise NoMarketDataError(symbol, instrument.yahoo_symbol, rs.error_msg)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

    frame = _rows_to_frame(rows, symbol, instrument.yahoo_symbol)
    frame = validate_ohlcv_frame(frame, end_date)
    _assert_ohlcv_not_stale(frame, end_date, symbol, instrument.yahoo_symbol)
    return frame


def load_current_snapshot(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
) -> BaoStockCurrentSnapshotCandidate:
    """Acquire adjusted OHLCV and explicit trade status in one BaoStock request."""
    from tradingagents.market_history.coordinator import (
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.models import TradingStatus
    from tradingagents.market_history.suspension import normalize_mainland_session

    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(
            symbol,
            symbol,
            "BaoStock supports China A-share symbols only",
        )
    with _session():
        if before_physical_request is not None:
            before_physical_request("adjusted-history-status")
        with contain_baostock_output():
            result = bs.query_history_k_data_plus(
                instrument.baostock_code,
                CURRENT_SNAPSHOT_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            raw_rows = _named_query_rows(result, "current adjusted history status")
    if not raw_rows:
        raise NoMarketDataError(
            symbol,
            instrument.yahoo_symbol,
            "BaoStock returned no current adjusted history rows",
        )

    invalid_date_rows: list[int] = []
    parsed_session_dates: list[date | None] = []
    for index, row in enumerate(raw_rows):
        try:
            parsed_session_dates.append(date.fromisoformat(row["date"]))
        except (KeyError, ValueError):
            invalid_date_rows.append(index)
            parsed_session_dates.append(None)
    if invalid_date_rows:
        rows = ", ".join(str(index) for index in invalid_date_rows)
        raise ValueError(f"invalid Date at row(s) {rows}")

    normalized_sessions = []
    for row, session_date in zip(raw_rows, parsed_session_dates, strict=True):
        assert session_date is not None
        status_value = row.get("tradestatus", "").strip()
        if status_value == "1":
            status = TradingStatus.TRADED
        elif status_value == "0":
            status = TradingStatus.SUSPENDED
        else:
            raise ValueError(
                "BaoStock returned a missing or unknown explicit trading status"
            )
        normalized_sessions.append(
            normalize_mainland_session(
                session_date=session_date,
                open_value=_optional_decimal(row.get("open")),
                high_value=_optional_decimal(row.get("high")),
                low_value=_optional_decimal(row.get("low")),
                close_value=_optional_decimal(row.get("close")),
                volume=_optional_decimal(row.get("volume")),
                authoritative_status=status,
                official_carried_close=(
                    _optional_decimal(row.get("preclose"))
                    if status is TradingStatus.SUSPENDED
                    else None
                ),
            )
        )
    normalized_sessions.sort(key=lambda item: item.observation.session_date)
    session_dates = tuple(
        item.observation.session_date for item in normalized_sessions
    )
    if len(session_dates) != len(set(session_dates)):
        raise NoMarketDataError(
            symbol,
            instrument.yahoo_symbol,
            "BaoStock returned duplicate current status sessions",
        )

    frame = pd.DataFrame(
        (
            {
                "Date": pd.Timestamp(item.observation.session_date),
                "Open": float(item.observation.open),
                "High": float(item.observation.high),
                "Low": float(item.observation.low),
                "Close": float(item.observation.close),
                "Volume": float(item.observation.volume),
            }
            for item in normalized_sessions
        ),
        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
    )
    observed_at = datetime.now(timezone.utc)
    upstream_service_id, _ = upstream_service_identity_for_provider("baostock")
    provider_dataset_id = _history_identity(
        "provider-dataset",
        upstream_service_id,
        "mainland-current-adjusted-status-v1",
    )
    raw_text = json.dumps(
        {
            "adjustment_basis": "qfq",
            "as_of_date": end_date,
            "fields": CURRENT_SNAPSHOT_FIELDS.split(","),
            "provider": "baostock",
            "requested_start": start_date,
            "rows": raw_rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    artifact_digest = sha256(raw_text.encode("utf-8")).hexdigest()
    source_ref = stable_acquisition_source_ref(
        "baostock-current-status",
        instrument.yahoo_symbol,
        start_date,
        end_date,
        artifact_digest,
    )
    status_artifact = SourceArtifact(
        artifact_sha256=artifact_digest,
        source_ref=source_ref,
        tool_call_id=source_ref,
        tool_name="baostock_current_adjusted_status_v1",
        raw_text=raw_text,
    )
    latest = normalized_sessions[-1]
    latest_status = latest.status
    latest_traded_close = next(
        (
            item.observation.close
            for item in reversed(normalized_sessions)
            if item.status.status is TradingStatus.TRADED
        ),
        None,
    )
    provenance = TradingStatusProvenance(
        provider="baostock",
        provider_dataset_id=provider_dataset_id,
        session_date=latest_status.session_date,
        status=latest_status.status,
        observed_at=observed_at,
        revision_id=f"artifact:sha256:{artifact_digest}",
    )
    return BaoStockCurrentSnapshotCandidate(
        frame=frame,
        trading_statuses=tuple(item.status for item in normalized_sessions),
        current_tradeability=(
            "suspended"
            if latest_status.status is TradingStatus.SUSPENDED
            else "tradeable"
        ),
        current_status_provenance=provenance,
        status_artifact=status_artifact,
        latest_traded_close=latest_traded_close,
        latest_traded_close_diagnostic=(
            None
            if latest_traded_close is not None
            else "no_genuinely_traded_close_in_retained_history"
        ),
        carried_suspension_close=(
            latest.observation.close
            if latest_status.status is TradingStatus.SUSPENDED
            else None
        ),
    )


def load_snapshot_with_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    before_physical_request: Callable[[str], None] | None = None,
    physical_request: Callable[[str, Callable[[], object]], object] | None = None,
    physical_workflow: Callable | None = None,
    adjustment_as_of: str | None = None,
) -> BaoStockSnapshotHistoryCandidate:
    """Acquire one complete BaoStock raw/status/factor/calendar workflow."""
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(
            symbol,
            symbol,
            "BaoStock supports China A-share symbols only",
        )
    observed_at = None
    if physical_workflow is not None:
        if physical_request is not None or before_physical_request is not None:
            raise ValueError("BaoStock history accepts only one physical workflow executor")

        def workflow(execute_operation):
            def query(operation, call):
                def request():
                    with contain_baostock_output():
                        record_active_physical_attempt_io()
                        return _named_query_rows(call(), operation)

                return execute_operation(operation, request)

            # One leader owns this session and consumes every SDK cursor before
            # logout. Followers receive only codec-safe rows and the leader time.
            with _session(
                physical_request=execute_operation, record_physical_io=True
            ):
                raw_rows = query("raw-history", lambda: bs.query_history_k_data_plus(
                    instrument.baostock_code, RAW_HISTORY_FIELDS,
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="3"))
                factor_rows = query("adjustment-factors", lambda: bs.query_adjust_factor(
                    instrument.baostock_code, end_date=adjustment_as_of or end_date))
                calendar_rows = query("session-calendar", lambda: bs.query_trade_dates(
                    start_date=start_date, end_date=end_date))
            return {
                "raw_rows": raw_rows,
                "factor_rows": factor_rows,
                "calendar_rows": calendar_rows,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }

        payload = physical_workflow(workflow)
        if not isinstance(payload, dict) or set(payload) != {
            "raw_rows", "factor_rows", "calendar_rows", "observed_at"
        }:
            raise ValueError("invalid BaoStock history workflow result")
        observed_at = datetime.fromisoformat(payload["observed_at"])
        if observed_at.tzinfo is None:
            raise ValueError("BaoStock history observation time must be timezone-aware")
        raw_rows = payload["raw_rows"]
        factor_rows = payload["factor_rows"]
        calendar_rows = payload["calendar_rows"]
    elif physical_request is not None:
        # Keep query and row consumption inside the physical attempt. SDK result
        # objects are session-bound and must never enter the persisted handoff.
        def query(operation, call):
            def request():
                with contain_baostock_output():
                    return _named_query_rows(call(), operation)
            return physical_request(operation, request)

        with _session(physical_request=physical_request):
            raw_rows = query("raw-history", lambda: bs.query_history_k_data_plus(
                instrument.baostock_code, RAW_HISTORY_FIELDS, start_date=start_date,
                end_date=end_date, frequency="d", adjustflag="3"))
            factor_rows = query("adjustment-factors", lambda: bs.query_adjust_factor(
                instrument.baostock_code, end_date=adjustment_as_of or end_date))
            calendar_rows = query("session-calendar", lambda: bs.query_trade_dates(
                start_date=start_date, end_date=end_date))
    else:
        raw_rows, factor_rows, calendar_rows = _load_history_rows_legacy(
            instrument, start_date, end_date, before_physical_request)

    return _history_candidate(
        symbol, instrument, end_date, raw_rows, factor_rows, calendar_rows,
        observed_at=observed_at,
    )


def _load_history_rows_legacy(instrument, start_date, end_date, before_physical_request):
    with _session():
        if before_physical_request is not None:
            before_physical_request("raw-history")
        with contain_baostock_output():
            raw_result = bs.query_history_k_data_plus(
                instrument.baostock_code,
                RAW_HISTORY_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",
            )
        if before_physical_request is not None:
            before_physical_request("adjustment-factors")
        with contain_baostock_output():
            factor_result = bs.query_adjust_factor(
                instrument.baostock_code,
                end_date=end_date,
            )
        if before_physical_request is not None:
            before_physical_request("session-calendar")
        with contain_baostock_output():
            calendar_result = bs.query_trade_dates(
                start_date=start_date,
                end_date=end_date,
            )

        with contain_baostock_output():
            raw_rows = _named_query_rows(raw_result, "raw history")
            factor_rows = _named_query_rows(factor_result, "adjustment factors")
            calendar_rows = _named_query_rows(calendar_result, "session calendar")
    return raw_rows, factor_rows, calendar_rows


def _history_candidate(
    symbol, instrument, end_date, raw_rows, factor_rows, calendar_rows,
    *, observed_at: datetime | None = None,
):
    from tradingagents.market_history.calendar import (
        MarketSession,
        MarketSessionCalendarPublication,
        MarketSessionStatus,
    )
    from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
    from tradingagents.market_history.models import (
        AdjustmentFactorObservation,
        InstrumentSpec,
        ProvenanceClass,
        ProviderDatasetSpec,
        ProviderHistoryBundlePublication,
        TradingStatus,
    )
    from tradingagents.market_history.suspension import normalize_mainland_session

    normalized_sessions = []
    for row in raw_rows:
        session_date = date.fromisoformat(row["date"])
        status_value = row.get("tradestatus", "").strip()
        if status_value == "1":
            status = TradingStatus.TRADED
        elif status_value == "0":
            status = TradingStatus.SUSPENDED
        else:
            raise NoMarketDataError(
                symbol,
                instrument.yahoo_symbol,
                f"BaoStock returned unknown trading status {status_value!r}",
            )
        normalized_sessions.append(
            normalize_mainland_session(
                session_date=session_date,
                open_value=_optional_decimal(row.get("open")),
                high_value=_optional_decimal(row.get("high")),
                low_value=_optional_decimal(row.get("low")),
                close_value=_optional_decimal(row.get("close")),
                volume=_optional_decimal(row.get("volume")),
                authoritative_status=status,
                official_carried_close=(
                    _optional_decimal(row.get("preclose"))
                    if status is TradingStatus.SUSPENDED
                    else None
                ),
            )
        )
    if not normalized_sessions:
        raise NoMarketDataError(
            symbol,
            instrument.yahoo_symbol,
            "BaoStock returned no raw history rows",
        )

    factors_by_date: dict[date, Decimal] = {}
    for row in factor_rows:
        effective_date = date.fromisoformat(row["dividOperateDate"])
        factor = _required_decimal(row.get("foreAdjustFactor"), "foreAdjustFactor")
        prior = factors_by_date.get(effective_date)
        if prior is not None and prior != factor:
            raise NoMarketDataError(
                symbol,
                instrument.yahoo_symbol,
                f"BaoStock returned conflicting factors for {effective_date}",
            )
        factors_by_date[effective_date] = factor
    factors = tuple(
        AdjustmentFactorObservation(day, factors_by_date[day])
        for day in sorted(factors_by_date)
    )
    if not factors or factors[0].effective_date > normalized_sessions[0].observation.session_date:
        raise NoMarketDataError(
            symbol,
            instrument.yahoo_symbol,
            "BaoStock returned incomplete adjustment-factor history",
        )

    factor_dates = tuple(item.effective_date for item in factors)
    derived_rows = []
    latest_traded_close: Decimal | None = None
    latest_adjusted_close: Decimal | None = None
    for normalized in normalized_sessions:
        observation = normalized.observation
        factor_index = bisect_right(factor_dates, observation.session_date) - 1
        if factor_index < 0:
            raise NoMarketDataError(
                symbol,
                instrument.yahoo_symbol,
                f"BaoStock has no effective factor for {observation.session_date}",
            )
        factor = factors[factor_index].factor
        latest_adjusted_close = observation.close * factor
        if normalized.status.status is TradingStatus.TRADED:
            latest_traded_close = observation.close * factor
        derived_rows.append(
            {
                "Date": pd.Timestamp(observation.session_date),
                "Open": float(observation.open * factor),
                "High": float(observation.high * factor),
                "Low": float(observation.low * factor),
                "Close": float(observation.close * factor),
                "Volume": float(observation.volume),
            }
        )
    frame = pd.DataFrame(
        derived_rows,
        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
    )

    calendar_sessions = tuple(
        MarketSession(
            session_date=date.fromisoformat(row["calendar_date"]),
            status=(
                MarketSessionStatus.OPEN
                if row.get("is_trading_day", "").strip() == "1"
                else MarketSessionStatus.CLOSED
            ),
        )
        for row in calendar_rows
    )
    if not calendar_sessions:
        raise NoMarketDataError(
            symbol,
            instrument.yahoo_symbol,
            "BaoStock returned no session calendar rows",
        )

    if observed_at is None:
        observed_at = datetime.now(timezone.utc)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "baostock"
    )
    provider = ProviderDatasetSpec(
        upstream_service_id=upstream_service_id,
        upstream_service_name=service_name,
        provider_dataset_id=_history_identity(
            "provider-dataset",
            upstream_service_id,
            "mainland-raw-status-factors-v1",
        ),
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    raw_payload = json.dumps(
        {
            "calendar": calendar_rows,
            "factors": factor_rows,
            "raw_history": raw_rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    history_bundle = ProviderHistoryBundlePublication(
        provider=provider,
        instrument=InstrumentSpec(
            instrument_id=_history_identity(
                "instrument",
                instrument.yahoo_symbol,
                instrument.exchange,
                getattr(instrument, "instrument_kind", "equity"),
                "CNY",
            ),
            canonical_symbol=instrument.yahoo_symbol,
            reference_market="mainland-cn",
            instrument_kind=getattr(instrument, "instrument_kind", "equity"),
            currency="CNY",
            identity_revision="mainland-routing-v1",
        ),
        requested_as_of=date.fromisoformat(end_date),
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=raw_payload,
        observations=tuple(item.observation for item in normalized_sessions),
        trading_statuses=tuple(item.status for item in normalized_sessions),
        adjustment_factors=factors,
    )
    latest_status = normalized_sessions[-1].status
    current_status_provenance = TradingStatusProvenance(
        provider=provider.provider_name,
        provider_dataset_id=provider.provider_dataset_id,
        session_date=latest_status.session_date,
        status=latest_status.status,
        observed_at=observed_at,
    )
    calendar = MarketSessionCalendarPublication(
        provider=ProviderDatasetSpec(
            upstream_service_id=upstream_service_id,
            upstream_service_name=service_name,
            provider_dataset_id=_history_identity(
                "provider-dataset",
                upstream_service_id,
                "mainland-session-calendar-v1",
            ),
            provider_name="baostock",
            dataset_name="mainland-session-calendar-v1",
            adjustment_methodology="not-applicable",
            strict_history_qualified=False,
        ),
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=raw_payload,
        sessions=calendar_sessions,
    )
    return BaoStockSnapshotHistoryCandidate(
        frame=frame,
        history_bundle=history_bundle,
        calendar=calendar,
        current_tradeability=(
            "suspended"
            if latest_status.status is TradingStatus.SUSPENDED
            else "tradeable"
        ),
        current_status_provenance=current_status_provenance,
        latest_traded_close=latest_traded_close,
        latest_traded_close_diagnostic=(
            None
            if latest_traded_close is not None
            else "no_genuinely_traded_close_in_retained_history"
        ),
        carried_suspension_close=(
            latest_adjusted_close
            if latest_status.status is TradingStatus.SUSPENDED
            else None
        ),
    )


def _named_query_rows(result, label: str) -> list[dict[str, str]]:
    if getattr(result, "error_code", "0") != "0":
        raise NoMarketDataError("baostock", "baostock", f"{label}: {result.error_msg}")
    fields = tuple(str(item) for item in getattr(result, "fields", ()))
    if not fields:
        raise NoMarketDataError("baostock", "baostock", f"{label}: missing fields")
    rows = []
    while result.next():
        values = result.get_row_data()
        if len(values) != len(fields):
            raise NoMarketDataError(
                "baostock",
                "baostock",
                f"{label}: row width does not match fields",
            )
        rows.append(dict(zip(fields, (str(value) for value in values), strict=True)))
    return rows


def _optional_decimal(value: object) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _required_decimal(text, "market value")


def _required_decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"BaoStock returned invalid {label}") from exc
    if not result.is_finite():
        raise ValueError(f"BaoStock returned non-finite {label}")
    return result


def _history_identity(namespace: str, *components: object) -> str:
    digest = sha256()
    for component in (namespace, *components):
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{namespace}=sha256:{digest.hexdigest()}"


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")
    frame = load_ohlcv_range(symbol, start_date, end_date)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    header = f"# Stock data for {instrument.yahoo_symbol} from {start_date} to {end_date}\n"
    header += "# Primary source: Baostock query_history_k_data_plus\n"
    header += "# Fallback source used: Baostock\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


@lru_cache(maxsize=64)
def _load_ohlcv_cached(symbol: str, curr_date: str, years: int) -> pd.DataFrame:
    curr = pd.to_datetime(curr_date)
    start = (curr - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")

    with _session(), contain_baostock_output():
        rs = bs.query_history_k_data_plus(
            instrument.baostock_code,
            FIELDS,
            start_date=start,
            end_date=curr.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise NoMarketDataError(symbol, instrument.yahoo_symbol, rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

    frame = _rows_to_frame(rows, symbol, instrument.yahoo_symbol)
    filtered = frame[frame["Date"] <= curr].copy()
    _assert_ohlcv_not_stale(filtered, curr_date, symbol, instrument.yahoo_symbol)
    return filtered


def load_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    return _load_ohlcv_cached(symbol, curr_date, years).copy()


def _indicator_values(symbol: str, indicator: str, curr_date: str) -> dict[str, str]:
    data = load_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df[indicator]
    values = {}
    for _, row in df.iterrows():
        value = row[indicator]
        values[row["Date"]] = "N/A" if pd.isna(value) else str(value)
    return values


def get_stock_stats_indicators_window(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int,
) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)
    values = _indicator_values(symbol, indicator, curr_date)
    lines = []
    current = curr_date_dt
    while current >= before:
        date_str = current.strftime("%Y-%m-%d")
        lines.append(
            f"{date_str}: {values.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}"
        )
        current = current - relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\nSource: Baostock query_history_k_data_plus\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )


def get_fundamentals(
    ticker: str,
    curr_date: str | None = None,
    *,
    _acquired: bool = False,
) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        raise NoMarketDataError(ticker, ticker, "Baostock supports China A-share symbols only")
    if _acquired:
        raise NoMarketDataError(
            ticker,
            instrument.yahoo_symbol,
            "Baostock has no dispatcher-valid financial observations",
        )
    return (
        f"# Company Fundamentals for {instrument.yahoo_symbol}\n"
        "# Primary source: Baostock\n"
        "Baostock fallback fundamentals are limited in phase 1. "
        "Use AKShare and Yahoo supplemental fundamentals when available."
    )
