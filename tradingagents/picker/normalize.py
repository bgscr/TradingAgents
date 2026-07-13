from __future__ import annotations

import math
import re
from datetime import date, datetime

import pandas as pd

from .errors import PITSchemaError
from .pit_models import Dataset

_REQUIRED_COLUMNS = {
    Dataset.TRADE_CAL: (
        "exchange",
        "cal_date",
        "is_open",
        "pretrade_date",
    ),
    Dataset.STOCK_BASIC: (
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "market",
        "list_status",
        "list_date",
        "delist_date",
        "is_hs",
    ),
    Dataset.DAILY: (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ),
    Dataset.DAILY_BASIC: (
        "ts_code",
        "trade_date",
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    Dataset.NAMECHANGE: (
        "ts_code",
        "name",
        "start_date",
        "end_date",
        "ann_date",
        "change_reason",
    ),
    Dataset.SUSPEND_D: (
        "ts_code",
        "suspend_date",
        "resume_date",
        "ann_date",
        "suspend_reason",
        "reason_type",
    ),
}

_DATE_COLUMNS = {
    Dataset.TRADE_CAL: ("cal_date", "pretrade_date"),
    Dataset.STOCK_BASIC: ("list_date", "delist_date"),
    Dataset.DAILY: ("trade_date",),
    Dataset.DAILY_BASIC: ("trade_date",),
    Dataset.NAMECHANGE: ("start_date", "end_date", "ann_date"),
    Dataset.SUSPEND_D: ("suspend_date", "resume_date", "ann_date"),
}

_NUMERIC_COLUMNS = {
    Dataset.TRADE_CAL: ("is_open",),
    Dataset.STOCK_BASIC: (),
    Dataset.DAILY: (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ),
    Dataset.DAILY_BASIC: (
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    Dataset.NAMECHANGE: (),
    Dataset.SUSPEND_D: (),
}

_REQUIRED_VALUES = {
    Dataset.TRADE_CAL: ("exchange", "cal_date", "is_open"),
    Dataset.STOCK_BASIC: (
        "ts_code",
        "symbol",
        "name",
        "market",
        "list_status",
        "list_date",
    ),
    Dataset.DAILY: _REQUIRED_COLUMNS[Dataset.DAILY],
    Dataset.DAILY_BASIC: (
        "ts_code",
        "trade_date",
        "close",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    Dataset.NAMECHANGE: ("ts_code", "name", "start_date"),
    Dataset.SUSPEND_D: ("ts_code", "suspend_date"),
}

_NATURAL_KEYS = {
    Dataset.TRADE_CAL: ("exchange", "cal_date"),
    Dataset.STOCK_BASIC: ("ts_code",),
    Dataset.DAILY: ("ts_code", "trade_date"),
    Dataset.DAILY_BASIC: ("ts_code", "trade_date"),
    Dataset.NAMECHANGE: ("ts_code", "start_date"),
    Dataset.SUSPEND_D: ("ts_code", "suspend_date"),
}

_SORT_COLUMNS = {
    Dataset.TRADE_CAL: ("cal_date", "exchange"),
    Dataset.STOCK_BASIC: ("list_date", "ts_code"),
    Dataset.DAILY: ("trade_date", "ts_code"),
    Dataset.DAILY_BASIC: ("trade_date", "ts_code"),
    Dataset.NAMECHANGE: ("start_date", "ts_code"),
    Dataset.SUSPEND_D: ("suspend_date", "ts_code"),
}

_NONEMPTY_DATASETS = {
    Dataset.TRADE_CAL,
    Dataset.STOCK_BASIC,
    Dataset.DAILY,
    Dataset.DAILY_BASIC,
}

_DATE_ONLY_RE = re.compile(r"(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})")


def normalize_partition(dataset: Dataset, frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize one raw Phase 1 provider partition."""
    if not isinstance(dataset, Dataset):
        raise PITSchemaError(f"unknown PIT dataset: {dataset!r}")
    if not isinstance(frame, pd.DataFrame):
        raise PITSchemaError(
            f"{dataset.value} partition is {type(frame).__name__}, expected DataFrame"
        )

    if dataset is Dataset.SUSPEND_D and frame.empty:
        return pd.DataFrame(columns=_REQUIRED_COLUMNS[Dataset.SUSPEND_D])
    if dataset in _NONEMPTY_DATASETS and frame.empty:
        raise PITSchemaError(f"{dataset.value} partition is empty")

    missing_columns = [
        column
        for column in _REQUIRED_COLUMNS[dataset]
        if column not in frame.columns
    ]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise PITSchemaError(
            f"{dataset.value} partition is missing required columns: {missing}"
        )

    out = frame.copy()
    for column in _NUMERIC_COLUMNS[dataset]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in _DATE_COLUMNS[dataset]:
        out[column] = _normalize_date_column(dataset, column, out[column])

    _require_values(dataset, out)
    _reject_infinite_numbers(dataset, out)

    if dataset is Dataset.DAILY:
        out["amount_cny"] = out["amount"] * 1_000.0
    elif dataset is Dataset.DAILY_BASIC:
        _normalize_daily_basic(out)

    _reject_conflicting_duplicates(dataset, out)
    out = out.drop_duplicates(subset=list(_NATURAL_KEYS[dataset]), keep="first")
    return out.sort_values(
        list(_SORT_COLUMNS[dataset]), kind="mergesort"
    ).reset_index(drop=True)


def _normalize_date_column(
    dataset: Dataset, column: str, values: pd.Series
) -> pd.Series:
    normalized = []
    for index, value in values.items():
        try:
            normalized.append(_normalize_date_value(value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise PITSchemaError(
                f"{dataset.value} column {column} contains an invalid date "
                f"at row {index}: {value!r}"
            ) from exc
    return pd.Series(normalized, index=values.index, dtype="string")


def _normalize_date_value(value: object) -> object:
    if _is_missing(value):
        return pd.NA
    if isinstance(value, bool):
        raise ValueError("boolean is not a date")
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return f"{value.year:04d}{value.month:02d}{value.day:02d}"

    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("non-integral numeric date")
        text = str(int(value))
    else:
        text = str(value).strip()

    if re.fullmatch(r"\d{8}", text):
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    else:
        match = _DATE_ONLY_RE.fullmatch(text)
        if match is None:
            raise ValueError("date is not date-only")
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
    return date(year, month, day).strftime("%Y%m%d")


def _require_values(dataset: Dataset, frame: pd.DataFrame) -> None:
    for column in _REQUIRED_VALUES[dataset]:
        missing = frame[column].isna()
        if not pd.api.types.is_numeric_dtype(frame[column]):
            missing |= frame[column].map(
                lambda value: isinstance(value, str) and not value.strip(),
                na_action="ignore",
            ).fillna(False)
        if missing.any():
            rows = ", ".join(str(index) for index in frame.index[missing].tolist())
            raise PITSchemaError(
                f"{dataset.value} column {column} has missing required values "
                f"at rows: {rows}"
            )


def _reject_infinite_numbers(dataset: Dataset, frame: pd.DataFrame) -> None:
    for column in _NUMERIC_COLUMNS[dataset]:
        infinite = frame[column].isin([float("inf"), float("-inf")])
        if infinite.any():
            rows = ", ".join(str(index) for index in frame.index[infinite].tolist())
            raise PITSchemaError(
                f"{dataset.value} column {column} has non-finite values at rows: {rows}"
            )


def _normalize_daily_basic(out: pd.DataFrame) -> None:
    invalid_order = (
        (out["free_share"] < 0)
        | (out["free_share"] > out["float_share"])
        | (out["float_share"] > out["total_share"])
    )
    if invalid_order.any():
        raise PITSchemaError("daily_basic share count ordering is invalid")

    out["circ_market_cap_cny"] = out["circ_mv"] * 10_000.0
    out["free_float_market_cap_cny"] = (
        out["free_share"] * out["close"] * 10_000.0
    )
    derived_circ = out["float_share"] * out["close"] * 10_000.0
    tolerance = pd.concat(
        [
            pd.Series(10_000.0, index=out.index),
            out["circ_market_cap_cny"].abs() * 0.001,
        ],
        axis=1,
    ).max(axis=1)
    if ((derived_circ - out["circ_market_cap_cny"]).abs() > tolerance).any():
        raise PITSchemaError(
            "daily_basic circ_market_cap_cny does not reconcile with float_share and close"
        )


def _reject_conflicting_duplicates(dataset: Dataset, frame: pd.DataFrame) -> None:
    keys = list(_NATURAL_KEYS[dataset])
    duplicates = frame.loc[frame.duplicated(subset=keys, keep=False)]
    if duplicates.empty:
        return
    unique_rows = duplicates.drop_duplicates()
    if len(unique_rows) > len(unique_rows.drop_duplicates(subset=keys)):
        raise PITSchemaError(
            f"{dataset.value} partition contains conflicting rows for one natural key"
        )


def _is_missing(value: object) -> bool:
    missing = pd.isna(value)
    return bool(missing) if isinstance(missing, bool) else False
