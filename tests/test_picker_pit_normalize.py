from datetime import date

import pandas as pd
import pytest

from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.normalize import normalize_partition
from tradingagents.picker.pit_models import Dataset


def daily_basic_row(**overrides):
    row = {
        "ts_code": "000001.SZ",
        "trade_date": "20260710",
        "close": 10.0,
        "turnover_rate": 1.0,
        "turnover_rate_f": 2.0,
        "volume_ratio": 1.2,
        "pe": 8.0,
        "pe_ttm": 9.0,
        "pb": 1.0,
        "total_share": 120_000.0,
        "float_share": 100_000.0,
        "free_share": 40_000.0,
        "total_mv": 1_200_000.0,
        "circ_mv": 1_000_000.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def daily_row(**overrides):
    row = {
        "ts_code": "000001.SZ",
        "trade_date": "20260710",
        "open": 9.8,
        "high": 10.2,
        "low": 9.7,
        "close": 10.0,
        "pre_close": 9.9,
        "change": 0.1,
        "pct_chg": 1.01,
        "vol": 1_000.0,
        "amount": 123.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def dataset_row(dataset):
    rows = {
        Dataset.TRADE_CAL: {
            "exchange": "SSE",
            "cal_date": "20260710",
            "is_open": 1,
            "pretrade_date": "20260709",
        },
        Dataset.STOCK_BASIC: {
            "ts_code": "000001.SZ",
            "symbol": "000001",
            "name": "Ping An Bank",
            "area": "Shenzhen",
            "industry": "Banking",
            "market": "Main Board",
            "list_status": "L",
            "list_date": "19910403",
            "delist_date": None,
            "is_hs": "S",
        },
        Dataset.DAILY: daily_row().iloc[0].to_dict(),
        Dataset.DAILY_BASIC: daily_basic_row().iloc[0].to_dict(),
        Dataset.NAMECHANGE: {
            "ts_code": "000001.SZ",
            "name": "Ping An Bank",
            "start_date": "20090803",
            "end_date": None,
            "ann_date": "20090729",
            "change_reason": "rename",
        },
        Dataset.SUSPEND_D: {
            "ts_code": "000001.SZ",
            "suspend_date": "20260710",
            "resume_date": None,
            "ann_date": "20260710",
            "suspend_reason": "meeting",
            "reason_type": "major event",
        },
    }
    return pd.DataFrame([rows[dataset]])


def test_daily_basic_exposes_distinct_canonical_market_caps():
    out = normalize_partition(Dataset.DAILY_BASIC, daily_basic_row())
    assert out.loc[0, "circ_market_cap_cny"] == 10_000_000_000.0
    assert out.loc[0, "free_float_market_cap_cny"] == 4_000_000_000.0


@pytest.mark.parametrize(
    "field,value",
    [("free_share", 100_001.0), ("float_share", 120_001.0)],
)
def test_invalid_share_order_fails_closed(field, value):
    with pytest.raises(PITSchemaError, match="share count"):
        normalize_partition(Dataset.DAILY_BASIC, daily_basic_row(**{field: value}))


def test_circ_mv_reconciliation_uses_declared_units():
    with pytest.raises(PITSchemaError, match="circ_market_cap_cny"):
        normalize_partition(Dataset.DAILY_BASIC, daily_basic_row(circ_mv=800_000.0))


def test_daily_amount_is_converted_from_thousand_cny():
    raw = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20260710"],
            "open": [9.8],
            "high": [10.2],
            "low": [9.7],
            "close": [10.0],
            "pre_close": [9.9],
            "change": [0.1],
            "pct_chg": [1.01],
            "vol": [1_000.0],
            "amount": [123.0],
        }
    )
    out = normalize_partition(Dataset.DAILY, raw)
    assert out.loc[0, "amount_cny"] == 123_000.0


def test_empty_suspend_partition_is_valid():
    out = normalize_partition(Dataset.SUSPEND_D, pd.DataFrame())
    assert out.empty
    assert list(out.columns) == [
        "ts_code",
        "suspend_date",
        "resume_date",
        "ann_date",
        "suspend_reason",
        "reason_type",
    ]


@pytest.mark.parametrize("dataset", list(Dataset))
def test_all_datasets_require_the_declared_provider_columns(dataset):
    frame = dataset_row(dataset)
    missing = frame.columns[-1]
    with pytest.raises(PITSchemaError, match=f"{dataset.value}.*{missing}"):
        normalize_partition(dataset, frame.drop(columns=missing))


@pytest.mark.parametrize(
    "dataset",
    [Dataset.TRADE_CAL, Dataset.STOCK_BASIC, Dataset.DAILY, Dataset.DAILY_BASIC],
)
def test_open_date_and_static_contracts_reject_empty_partitions(dataset):
    with pytest.raises(PITSchemaError, match=f"{dataset.value}.*empty"):
        normalize_partition(dataset, dataset_row(dataset).iloc[0:0])


def test_dates_are_normalized_without_timezone_conversion():
    raw = dataset_row(Dataset.NAMECHANGE)
    raw["start_date"] = pd.Series([date(2009, 8, 3)], dtype=object)
    raw.loc[0, "ann_date"] = "2009-07-29"

    out = normalize_partition(Dataset.NAMECHANGE, raw)

    assert out.loc[0, "start_date"] == "20090803"
    assert out.loc[0, "ann_date"] == "20090729"


def test_unparseable_date_fails_closed():
    raw = daily_row(trade_date="2026-02-30")
    with pytest.raises(PITSchemaError, match="trade_date"):
        normalize_partition(Dataset.DAILY, raw)


def test_numeric_columns_are_coerced_and_raw_provenance_columns_are_retained():
    raw = daily_basic_row(
        close="10",
        total_share="120000",
        float_share="100000",
        free_share="40000",
        total_mv="1200000",
        circ_mv="1000000",
    )

    out = normalize_partition(Dataset.DAILY_BASIC, raw)

    assert out.loc[0, "circ_mv"] == 1_000_000.0
    assert out.loc[0, "free_share"] == 40_000.0
    assert out.loc[0, "circ_market_cap_cny"] == 10_000_000_000.0
    assert out.loc[0, "free_float_market_cap_cny"] == 4_000_000_000.0


@pytest.mark.parametrize(
    ("dataset", "field"),
    [
        (Dataset.TRADE_CAL, "is_open"),
        (Dataset.STOCK_BASIC, "ts_code"),
        (Dataset.DAILY, "amount"),
        (Dataset.DAILY_BASIC, "free_share"),
        (Dataset.NAMECHANGE, "start_date"),
        (Dataset.SUSPEND_D, "suspend_date"),
    ],
)
def test_missing_required_values_fail_closed(dataset, field):
    raw = dataset_row(dataset)
    raw.loc[0, field] = None
    with pytest.raises(PITSchemaError, match=field):
        normalize_partition(dataset, raw)


@pytest.mark.parametrize("dataset", list(Dataset))
def test_all_datasets_deduplicate_natural_keys_and_sort_stably(dataset):
    first = dataset_row(dataset)
    second = dataset_row(dataset)
    if dataset is Dataset.TRADE_CAL:
        second["cal_date"] = "20260709"
        second["pretrade_date"] = "20260708"
    elif dataset is Dataset.STOCK_BASIC:
        second["ts_code"] = "000000.SZ"
        second["symbol"] = "000000"
        second["list_date"] = "19900101"
    elif dataset in {Dataset.DAILY, Dataset.DAILY_BASIC}:
        second["ts_code"] = "000000.SZ"
    else:
        second["ts_code"] = "000000.SZ"
    raw = pd.concat([first, second, first], ignore_index=True)

    out = normalize_partition(dataset, raw)

    assert len(out) == 2
    assert list(out.index) == [0, 1]
    if "ts_code" in out:
        assert out.loc[0, "ts_code"] == "000000.SZ"
    else:
        assert out.loc[0, "cal_date"] == "20260709"


def test_conflicting_rows_for_one_natural_key_fail_closed():
    raw = pd.concat([daily_row(), daily_row(close=11.0)], ignore_index=True)
    with pytest.raises(PITSchemaError, match="conflicting.*natural key"):
        normalize_partition(Dataset.DAILY, raw)


def test_normalization_preserves_rows_and_extra_raw_provenance():
    raw = pd.concat(
        [
            daily_row(ts_code="000002.SZ", provider_note="second"),
            daily_row(ts_code="000001.SZ", provider_note="first"),
        ],
        ignore_index=True,
    )

    out = normalize_partition(Dataset.DAILY, raw)

    assert out["ts_code"].tolist() == ["000001.SZ", "000002.SZ"]
    assert out["provider_note"].tolist() == ["first", "second"]
    assert out["amount"].tolist() == [123.0, 123.0]
