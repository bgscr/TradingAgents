from __future__ import annotations

from numbers import Real

import pandas as pd

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITCoverageError, PITSchemaError
from tradingagents.picker.pit_dates import parse_yyyymmdd
from tradingagents.picker.pit_models import Dataset, PartitionKey, PITSnapshot

_MIN_LISTING_SESSIONS = 60
_MIN_CLOSE_CNY = 3.0
_MIN_FREE_FLOAT_MARKET_CAP_CNY = 500_000_000.0
_MIN_AVG_AMOUNT_20D_CNY = 300_000_000.0
_OUTPUT_COLUMNS = [
    "ts_code",
    "name",
    "list_date",
    "delist_date",
    "close",
    "amount_cny",
    "avg_amount_20d_cny",
    "free_float_market_cap_cny",
    "is_st",
    "is_suspended",
    "listing_age_sessions",
    "eligible",
    "ineligibility_reasons",
]
_REQUIRED_COLUMNS = {
    "trade_cal": ("cal_date", "is_open"),
    "stock_basic": ("ts_code", "name", "list_date", "delist_date"),
    "namechange": ("ts_code", "name", "start_date", "end_date"),
    "daily": ("ts_code", "trade_date", "close", "amount_cny"),
    "daily_basic": (
        "ts_code",
        "trade_date",
        "free_float_market_cap_cny",
    ),
    "suspend_d": ("ts_code", "suspend_date"),
    "trailing_amounts": ("ts_code", "avg_amount_20d_cny"),
}


def build_snapshot_from_frames(
    frames: dict[str, pd.DataFrame],
    as_of: str,
    minimum_coverage: float = 0.95,
) -> PITSnapshot:
    """Build one point-in-time universe from already normalized local frames."""
    as_of = parse_yyyymmdd(as_of, "as_of").strftime("%Y%m%d")
    minimum_coverage = _validated_coverage(minimum_coverage)
    _validate_frames(frames)

    basic = frames["stock_basic"].copy()
    basic["list_date"] = basic["list_date"].astype("string")
    basic["delist_date"] = basic["delist_date"].fillna("").astype("string")
    active = basic.loc[
        (basic["list_date"] <= as_of)
        & ((basic["delist_date"].str.strip() == "") | (basic["delist_date"] > as_of))
    ].copy()
    active = active.sort_values("ts_code", kind="mergesort").reset_index(drop=True)

    effective_names = _effective_names(frames["namechange"], as_of)
    active = active.merge(effective_names, on="ts_code", how="left", validate="one_to_one")
    active["name"] = active["effective_name"].fillna(active["name"])
    active = active.drop(columns="effective_name")
    active["is_st"] = active["name"].astype("string").str.contains(
        "ST", case=False, regex=False, na=False
    )

    daily = _same_day(frames["daily"], "trade_date", as_of).copy()
    daily["_has_daily"] = True
    daily_basic = _same_day(frames["daily_basic"], "trade_date", as_of).copy()
    daily_basic["_has_daily_basic"] = True
    trailing = frames["trailing_amounts"].copy()
    trailing["_has_trailing"] = True
    suspended_codes = set(
        _same_day(frames["suspend_d"], "suspend_date", as_of)["ts_code"].astype(str)
    )

    active = active.merge(
        daily[["ts_code", "close", "amount_cny", "_has_daily"]],
        on="ts_code",
        how="left",
        validate="one_to_one",
    )
    active = active.merge(
        daily_basic[
            ["ts_code", "free_float_market_cap_cny", "_has_daily_basic"]
        ],
        on="ts_code",
        how="left",
        validate="one_to_one",
    )
    active = active.merge(
        trailing[["ts_code", "avg_amount_20d_cny", "_has_trailing"]],
        on="ts_code",
        how="left",
        validate="one_to_one",
    )
    active["is_suspended"] = active["ts_code"].astype(str).isin(suspended_codes)
    active["listing_age_sessions"] = _listing_ages(
        active["list_date"], frames["trade_cal"], as_of
    )

    mandatory_columns = [
        "close",
        "amount_cny",
        "avg_amount_20d_cny",
        "free_float_market_cap_cny",
    ]
    missing_critical = active[mandatory_columns].isna().any(axis=1)
    active["ineligibility_reasons"] = [
        _ineligibility_reasons(row, missing)
        for (_, row), missing in zip(active.iterrows(), missing_critical, strict=True)
    ]
    active["eligible"] = active["ineligibility_reasons"].eq("")

    has_complete_market_data = (
        active["_has_daily"].fillna(False)
        & active["_has_daily_basic"].fillna(False)
        & active["_has_trailing"].fillna(False)
        & ~missing_critical
    )
    covered = active["is_suspended"] | has_complete_market_data
    coverage = float(covered.mean()) if len(active) else 1.0
    if coverage < minimum_coverage:
        raise PITCoverageError(
            f"snapshot coverage {coverage:.2%} is below required {minimum_coverage:.2%}"
        )

    universe = active[_OUTPUT_COLUMNS].reset_index(drop=True)
    return PITSnapshot(as_of=as_of, universe=universe, coverage=coverage)


def build_snapshot(
    cache: PITCache,
    as_of: str,
    minimum_coverage: float = 0.95,
) -> PITSnapshot:
    """Load verified local cache partitions and reconstruct one PIT snapshot."""
    as_of = parse_yyyymmdd(as_of, "as_of").strftime("%Y%m%d")
    minimum_coverage = _validated_coverage(minimum_coverage)

    calendars, open_dates = _load_calendar_history(cache, as_of)
    trailing_dates = open_dates[-20:]
    daily_by_date = {
        trade_date: _load_required(
            cache,
            PartitionKey(Dataset.DAILY, trade_date),
            backfill=True,
            requirement="20 daily lookback sessions",
        )
        for trade_date in trailing_dates
    }
    if as_of not in daily_by_date:
        daily_by_date[as_of] = _load_required(
            cache, PartitionKey(Dataset.DAILY, as_of)
        )

    daily_history = pd.concat(
        [
            frame.loc[frame["trade_date"].astype(str) == trade_date]
            for trade_date, frame in daily_by_date.items()
            if trade_date in trailing_dates
        ],
        ignore_index=True,
    )
    trailing_amounts = (
        daily_history.groupby("ts_code", as_index=False, sort=True)["amount_cny"]
        .mean()
        .rename(columns={"amount_cny": "avg_amount_20d_cny"})
    )

    frames = {
        "trade_cal": pd.concat(calendars, ignore_index=True),
        "stock_basic": _load_required(
            cache, PartitionKey(Dataset.STOCK_BASIC, "current")
        ),
        "namechange": _load_required(
            cache, PartitionKey(Dataset.NAMECHANGE, "all")
        ),
        "daily": daily_by_date[as_of],
        "daily_basic": _load_required(
            cache, PartitionKey(Dataset.DAILY_BASIC, as_of)
        ),
        "suspend_d": _load_required(
            cache, PartitionKey(Dataset.SUSPEND_D, as_of)
        ),
        "trailing_amounts": trailing_amounts,
    }
    return build_snapshot_from_frames(frames, as_of, minimum_coverage)


def _validated_coverage(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("minimum_coverage must be between 0 and 1")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError("minimum_coverage must be between 0 and 1")
    return result


def _validate_frames(frames: dict[str, pd.DataFrame]) -> None:
    if not isinstance(frames, dict):
        raise PITSchemaError("snapshot frames must be a mapping of normalized DataFrames")
    for name, columns in _REQUIRED_COLUMNS.items():
        frame = frames.get(name)
        if not isinstance(frame, pd.DataFrame):
            raise PITSchemaError(f"snapshot frame {name} is missing")
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise PITSchemaError(
                f"snapshot frame {name} is missing required columns: {', '.join(missing)}"
            )


def _effective_names(namechange: pd.DataFrame, as_of: str) -> pd.DataFrame:
    names = namechange.copy()
    names["start_date"] = names["start_date"].astype("string")
    names["end_date"] = names["end_date"].fillna("").astype("string")
    effective = names.loc[
        (names["start_date"] <= as_of)
        & ((names["end_date"].str.strip() == "") | (names["end_date"] >= as_of))
    ]
    latest = effective.sort_values("start_date", kind="mergesort").drop_duplicates(
        "ts_code", keep="last"
    )
    return latest[["ts_code", "name"]].rename(columns={"name": "effective_name"})


def _same_day(frame: pd.DataFrame, date_column: str, as_of: str) -> pd.DataFrame:
    return frame.loc[frame[date_column].astype(str) == as_of]


def _listing_ages(
    list_dates: pd.Series, trade_cal: pd.DataFrame, as_of: str
) -> pd.Series:
    open_dates = (
        trade_cal.loc[
            (pd.to_numeric(trade_cal["is_open"], errors="coerce") == 1)
            & (trade_cal["cal_date"].astype(str) <= as_of),
            "cal_date",
        ]
        .astype(str)
        .drop_duplicates()
        .sort_values()
    )
    return list_dates.astype(str).map(lambda listed: int((open_dates >= listed).sum()))


def _ineligibility_reasons(row: pd.Series, missing_critical: bool) -> str:
    reasons = []
    if row["listing_age_sessions"] < _MIN_LISTING_SESSIONS:
        reasons.append("listing_age")
    if row["is_st"]:
        reasons.append("st")
    if pd.notna(row["close"]) and row["close"] <= _MIN_CLOSE_CNY:
        reasons.append("price")
    if (
        pd.notna(row["free_float_market_cap_cny"])
        and row["free_float_market_cap_cny"] < _MIN_FREE_FLOAT_MARKET_CAP_CNY
    ):
        reasons.append("free_float_market_cap")
    if (
        pd.notna(row["avg_amount_20d_cny"])
        and row["avg_amount_20d_cny"] <= _MIN_AVG_AMOUNT_20D_CNY
    ):
        reasons.append("liquidity")
    if row["is_suspended"]:
        reasons.append("suspended")
    if missing_critical:
        reasons.append("missing_critical_data")
    return ";".join(reasons)


def _load_calendar_history(
    cache: PITCache, as_of: str
) -> tuple[list[pd.DataFrame], list[str]]:
    calendars = []
    year = int(as_of[:4])
    open_dates: set[str] = set()
    while len(open_dates) < _MIN_LISTING_SESSIONS:
        key = PartitionKey(Dataset.TRADE_CAL, str(year))
        calendar = _load_required(
            cache,
            key,
            backfill=True,
            requirement="60 open sessions",
        )
        calendars.append(calendar)
        open_dates.update(
            calendar.loc[
                (pd.to_numeric(calendar["is_open"], errors="coerce") == 1)
                & (calendar["cal_date"].astype(str) <= as_of),
                "cal_date",
            ].astype(str)
        )
        year -= 1
    return calendars, sorted(open_dates)


def _load_required(
    cache: PITCache,
    key: PartitionKey,
    *,
    backfill: bool = False,
    requirement: str | None = None,
) -> pd.DataFrame:
    try:
        return cache.load_frame(key)
    except PITSchemaError as exc:
        message = f"required cached partition {key.storage_key} is unavailable: {exc}"
        if backfill:
            message += (
                f"; {requirement or 'required history'} is unavailable, "
                "backfill an earlier start date"
            )
        raise PITSchemaError(message) from exc
