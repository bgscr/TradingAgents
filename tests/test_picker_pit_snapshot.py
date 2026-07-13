import json

import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITCoverageError, PITSchemaError
from tradingagents.picker.pit_models import Dataset, PartitionKey
from tradingagents.picker.snapshot import build_snapshot, build_snapshot_from_frames

AS_OF = "20260710"


def _open_dates(periods=60):
    return pd.bdate_range(end="2026-07-10", periods=periods).strftime("%Y%m%d").tolist()


def base_frames():
    return {
        "trade_cal": pd.DataFrame(
            {"cal_date": _open_dates(), "is_open": [1] * 60}
        ),
        "stock_basic": pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "600001.SH", "000002.SZ"],
                "name": ["平安银行", "未来退市样本", "停牌样本"],
                "list_date": ["19910403", "20000101", "20000101"],
                "delist_date": ["", "20270101", ""],
            }
        ),
        "namechange": pd.DataFrame(
            {
                "ts_code": ["600001.SH"],
                "name": ["ST未来"],
                "start_date": ["20260701"],
                "end_date": ["20260731"],
            }
        ),
        "daily": pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "600001.SH"],
                "trade_date": [AS_OF, AS_OF],
                "close": [10.0, 8.0],
                "amount_cny": [400_000_000.0, 500_000_000.0],
            }
        ),
        "daily_basic": pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "600001.SH"],
                "trade_date": [AS_OF, AS_OF],
                "free_float_market_cap_cny": [
                    2_000_000_000.0,
                    2_000_000_000.0,
                ],
            }
        ),
        "suspend_d": pd.DataFrame(
            {"ts_code": ["000002.SZ"], "suspend_date": [AS_OF]}
        ),
        "trailing_amounts": pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "600001.SH"],
                "avg_amount_20d_cny": [400_000_000.0, 500_000_000.0],
            }
        ),
    }


def test_snapshot_keeps_future_delisted_stock_but_marks_effective_st():
    snapshot = build_snapshot_from_frames(base_frames(), AS_OF)
    row = snapshot.universe.set_index("ts_code").loc["600001.SH"]

    assert row["name"] == "ST未来"
    assert row["is_st"]
    assert not row["eligible"]
    assert "st" in row["ineligibility_reasons"]


def test_explicit_suspension_counts_as_covered_but_ineligible():
    snapshot = build_snapshot_from_frames(base_frames(), AS_OF)
    row = snapshot.universe.set_index("ts_code").loc["000002.SZ"]

    assert row["is_suspended"]
    assert row["ineligibility_reasons"] == "suspended;missing_critical_data"
    assert snapshot.coverage == 1.0


def test_unexplained_missing_rows_fail_coverage():
    frames = base_frames()
    frames["suspend_d"] = frames["suspend_d"].iloc[0:0]

    with pytest.raises(PITCoverageError, match=r"coverage 66\.67%.*95\.00%"):
        build_snapshot_from_frames(frames, AS_OF)


def test_future_namechange_does_not_leak_current_name_or_mark_st():
    frames = base_frames()
    frames["stock_basic"].loc[1, "name"] = "ST当前泄漏名"
    frames["namechange"] = pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600001.SH"],
            "name": ["历史正常名", "ST未来名"],
            "start_date": ["20260101", "20260711"],
            "end_date": [AS_OF, ""],
        }
    )

    row = build_snapshot_from_frames(frames, AS_OF).universe.set_index("ts_code").loc[
        "600001.SH"
    ]
    assert row["name"] == "历史正常名"
    assert not row["is_st"]


def test_active_membership_uses_inclusive_listing_and_exclusive_delisting():
    frames = base_frames()
    frames["stock_basic"] = pd.DataFrame(
        {
            "ts_code": ["LISTED", "FUTURE", "DELISTED", "FUTURE_DELIST"],
            "name": ["A", "B", "C", "D"],
            "list_date": [AS_OF, "20260711", "20000101", "20000101"],
            "delist_date": ["", "", AS_OF, "20260711"],
        }
    )
    frames["daily"] = pd.DataFrame(
        {
            "ts_code": ["LISTED", "FUTURE_DELIST"],
            "trade_date": [AS_OF, AS_OF],
            "close": [10.0, 10.0],
            "amount_cny": [400_000_000.0, 400_000_000.0],
        }
    )
    frames["daily_basic"] = pd.DataFrame(
        {
            "ts_code": ["LISTED", "FUTURE_DELIST"],
            "trade_date": [AS_OF, AS_OF],
            "free_float_market_cap_cny": [500_000_000.0, 500_000_000.0],
        }
    )
    frames["trailing_amounts"] = pd.DataFrame(
        {
            "ts_code": ["LISTED", "FUTURE_DELIST"],
            "avg_amount_20d_cny": [400_000_000.0, 400_000_000.0],
        }
    )
    frames["namechange"] = frames["namechange"].iloc[0:0]
    frames["suspend_d"] = frames["suspend_d"].iloc[0:0]

    snapshot = build_snapshot_from_frames(frames, AS_OF)

    assert snapshot.universe["ts_code"].tolist() == ["FUTURE_DELIST", "LISTED"]
    assert snapshot.universe.set_index("ts_code").loc["LISTED", "listing_age_sessions"] == 1


def test_latest_effective_name_wins_and_blank_end_is_open_ended():
    frames = base_frames()
    frames["namechange"] = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "name": ["Old Name", "*st latest"],
            "start_date": ["20200101", "20260701"],
            "end_date": ["", ""],
        }
    )

    row = build_snapshot_from_frames(frames, AS_OF).universe.set_index("ts_code").loc[
        "000001.SZ"
    ]
    assert row["name"] == "*st latest"
    assert row["is_st"]


def test_only_same_day_market_rows_and_suspensions_are_used():
    frames = base_frames()
    frames["daily"].loc[0, "trade_date"] = "20260709"
    frames["suspend_d"] = pd.concat(
        [
            frames["suspend_d"],
            pd.DataFrame({"ts_code": ["000001.SZ"], "suspend_date": ["20260709"]}),
        ],
        ignore_index=True,
    )

    with pytest.raises(PITCoverageError, match="coverage"):
        build_snapshot_from_frames(frames, AS_OF)


def test_thresholds_and_reason_order_are_exact():
    frames = base_frames()
    dates = _open_dates()
    frames["trade_cal"] = pd.DataFrame(
        {"cal_date": dates, "is_open": [1] * len(dates)}
    )
    frames["stock_basic"] = pd.DataFrame(
        {
            "ts_code": ["PASS", "FAIL"],
            "name": ["normal", "ST failure"],
            "list_date": [dates[0], dates[1]],
            "delist_date": ["", ""],
        }
    )
    frames["namechange"] = frames["namechange"].iloc[0:0]
    frames["daily"] = pd.DataFrame(
        {
            "ts_code": ["PASS", "FAIL"],
            "trade_date": [AS_OF, AS_OF],
            "close": [3.000001, 3.0],
            "amount_cny": [300_000_001.0, 300_000_000.0],
        }
    )
    frames["daily_basic"] = pd.DataFrame(
        {
            "ts_code": ["PASS", "FAIL"],
            "trade_date": [AS_OF, AS_OF],
            "free_float_market_cap_cny": [500_000_000.0, 499_999_999.0],
        }
    )
    frames["trailing_amounts"] = pd.DataFrame(
        {
            "ts_code": ["PASS", "FAIL"],
            "avg_amount_20d_cny": [300_000_001.0, 300_000_000.0],
        }
    )
    frames["suspend_d"] = pd.DataFrame(
        {"ts_code": ["FAIL"], "suspend_date": [AS_OF]}
    )

    universe = build_snapshot_from_frames(frames, AS_OF).universe.set_index("ts_code")

    assert universe.loc["PASS", "listing_age_sessions"] == 60
    assert universe.loc["PASS", "eligible"]
    assert universe.loc["PASS", "ineligibility_reasons"] == ""
    assert universe.loc["FAIL", "listing_age_sessions"] == 59
    assert universe.loc["FAIL", "ineligibility_reasons"] == (
        "listing_age;st;price;free_float_market_cap;liquidity;suspended"
    )


def test_missing_mandatory_daily_basic_is_uncovered_and_fails_closed():
    frames = base_frames()
    frames["daily_basic"] = frames["daily_basic"].loc[
        frames["daily_basic"]["ts_code"] != "000001.SZ"
    ]

    with pytest.raises(PITCoverageError, match="coverage"):
        build_snapshot_from_frames(frames, AS_OF)


def test_coverage_threshold_is_inclusive_at_ninety_five_percent():
    frames = base_frames()
    codes = [f"S{i:02d}" for i in range(20)]
    frames["stock_basic"] = pd.DataFrame(
        {
            "ts_code": codes,
            "name": ["normal"] * 20,
            "list_date": ["20000101"] * 20,
            "delist_date": [""] * 20,
        }
    )
    covered = codes[:-1]
    frames["namechange"] = frames["namechange"].iloc[0:0]
    frames["daily"] = pd.DataFrame(
        {
            "ts_code": covered,
            "trade_date": [AS_OF] * 19,
            "close": [10.0] * 19,
            "amount_cny": [400_000_000.0] * 19,
        }
    )
    frames["daily_basic"] = pd.DataFrame(
        {
            "ts_code": covered,
            "trade_date": [AS_OF] * 19,
            "free_float_market_cap_cny": [500_000_000.0] * 19,
        }
    )
    frames["trailing_amounts"] = pd.DataFrame(
        {"ts_code": covered, "avg_amount_20d_cny": [400_000_000.0] * 19}
    )
    frames["suspend_d"] = frames["suspend_d"].iloc[0:0]

    snapshot = build_snapshot_from_frames(frames, AS_OF, minimum_coverage=0.95)
    assert snapshot.coverage == 0.95
    with pytest.raises(PITCoverageError):
        build_snapshot_from_frames(frames, AS_OF, minimum_coverage=0.950001)


def _store(cache, dataset, partition, frame):
    raw = json.dumps(frame.to_dict(orient="records"), ensure_ascii=False).encode()
    cache.store_complete(PartitionKey(dataset, partition), raw, frame, "1")


def _complete_cache(tmp_path, *, calendar_periods=60, skip=()):
    cache = PITCache(tmp_path)
    dates = _open_dates(calendar_periods)
    partitions = {
        (Dataset.TRADE_CAL, "2026"): pd.DataFrame(
            {"cal_date": dates, "is_open": [1] * len(dates)}
        ),
        (Dataset.STOCK_BASIC, "current"): pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "name": ["平安银行"],
                "list_date": [dates[0]],
                "delist_date": [""],
            }
        ),
        (Dataset.NAMECHANGE, "all"): pd.DataFrame(
            columns=["ts_code", "name", "start_date", "end_date"]
        ),
        (Dataset.DAILY_BASIC, AS_OF): pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [AS_OF],
                "free_float_market_cap_cny": [500_000_000.0],
            }
        ),
        (Dataset.SUSPEND_D, AS_OF): pd.DataFrame(
            columns=["ts_code", "suspend_date"]
        ),
    }
    trailing_dates = dates[-20:]
    for index, trade_date in enumerate(trailing_dates):
        partitions[(Dataset.DAILY, trade_date)] = pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [trade_date],
                "close": [10.0],
                "amount_cny": [310_000_000.0 + index * 1_000_000.0],
            }
        )
    for (dataset, partition), frame in partitions.items():
        if (dataset, partition) not in skip:
            _store(cache, dataset, partition, frame)
    return cache, dates


def test_cache_backed_snapshot_loads_history_and_computes_twenty_day_average(tmp_path):
    cache, dates = _complete_cache(tmp_path)

    snapshot = build_snapshot(cache, AS_OF)
    row = snapshot.universe.iloc[0]

    assert snapshot.as_of == AS_OF
    assert row["listing_age_sessions"] == 60
    assert row["amount_cny"] == 329_000_000.0
    assert row["avg_amount_20d_cny"] == pytest.approx(319_500_000.0)
    assert row["eligible"]
    assert dates[-1] == AS_OF


@pytest.mark.parametrize(
    ("dataset", "partition"),
    [
        (Dataset.STOCK_BASIC, "current"),
        (Dataset.NAMECHANGE, "all"),
        (Dataset.DAILY_BASIC, AS_OF),
        (Dataset.SUSPEND_D, AS_OF),
    ],
)
def test_cache_backed_snapshot_names_missing_required_partition(
    tmp_path, dataset, partition
):
    cache, _ = _complete_cache(tmp_path, skip={(dataset, partition)})

    with pytest.raises(PITSchemaError, match=rf"{dataset.value}/{partition}.*missing"):
        build_snapshot(cache, AS_OF)


def test_cache_backed_snapshot_names_missing_daily_lookback_and_requests_backfill(
    tmp_path,
):
    missing_date = _open_dates()[-7]
    cache, _ = _complete_cache(tmp_path, skip={(Dataset.DAILY, missing_date)})

    with pytest.raises(
        PITSchemaError,
        match=rf"daily/{missing_date}.*backfill an earlier start date",
    ):
        build_snapshot(cache, AS_OF)


def test_cache_backed_snapshot_requires_sixty_open_sessions_and_requests_backfill(
    tmp_path,
):
    cache, _ = _complete_cache(tmp_path, calendar_periods=59)

    with pytest.raises(
        PITSchemaError,
        match=r"trade_cal/2025.*60 open sessions.*backfill an earlier start date",
    ):
        build_snapshot(cache, AS_OF)
