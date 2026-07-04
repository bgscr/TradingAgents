import pandas as pd
import pytest
import requests

import ak_pick_a_stock as picker


@pytest.fixture(autouse=True)
def disable_live_history_fetches(monkeypatch):
    def empty_history(*args, **kwargs):
        return pd.DataFrame()

    monkeypatch.setattr(picker.ak, "stock_zh_a_hist", empty_history)
    monkeypatch.setattr(picker.ak, "stock_zh_a_daily", empty_history)
    monkeypatch.setattr(picker.ak, "stock_zh_a_hist_tx", empty_history)
    monkeypatch.setattr(picker.ak, "index_zh_a_hist", empty_history)
    monkeypatch.setattr(picker.ak, "stock_zh_index_daily_tx", empty_history)
    monkeypatch.setattr(picker.ak, "stock_zh_index_daily", empty_history)


def _history_from_closes(closes: list[float], amount: float = 500_000_000) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "日期": dates.strftime("%Y-%m-%d"),
            "收盘": closes,
            "成交额": amount,
        }
    )


def test_prepare_candidates_filters_missing_and_extreme_valuation_when_available():
    pe = picker.DYNAMIC_PE_COL
    data = pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "ValidCo",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 900_000_000,
                pe: 25.0,
            },
            {
                "代码": "600002",
                "名称": "MissingPE",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 950_000_000,
                pe: None,
            },
            {
                "代码": "600003",
                "名称": "ExpensiveCo",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 1_000_000_000,
                pe: 120.0,
            },
        ]
    )

    result = picker.prepare_candidates(data, "unit_source")

    assert list(result["代码"]) == ["600001"]
    assert list(result[picker.VALUATION_STATUS_COL]) == ["ok"]
    assert list(result[picker.CANDIDATE_WARNING_COL]) == [""]


def test_main_marks_degraded_candidates_when_spot_source_lacks_valuation(
    monkeypatch, tmp_path, capsys
):
    def spot_without_pe():
        return pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1700,
                    "涨跌幅": 2.0,
                    "成交额": 1_200_000_000,
                },
                {
                    "代码": "sz000001",
                    "名称": "平安银行",
                    "最新价": 10,
                    "涨跌幅": -1.0,
                    "成交额": 500_000_000,
                },
            ]
        )

    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", spot_without_pe)
    monkeypatch.chdir(tmp_path)

    assert picker.main() == 0

    captured = capsys.readouterr()
    assert "valuation data unavailable from stock_zh_a_spot_em" in captured.out

    result = pd.read_csv(tmp_path / "ak_candidates.csv")
    assert set(result[picker.VALUATION_STATUS_COL]) == {"missing_source"}
    assert set(result[picker.CANDIDATE_WARNING_COL]) == {
        "valuation data unavailable from stock_zh_a_spot_em"
    }


def test_main_falls_back_to_sina_when_eastmoney_spot_fails(monkeypatch, tmp_path):
    def fail_eastmoney():
        raise requests.ConnectionError("remote closed")

    fallback_calls = []

    def sina_spot():
        fallback_calls.append(True)
        return pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1700,
                    "涨跌幅": 2.0,
                    "成交额": 1_200_000_000,
                },
                {
                    "代码": "sz000001",
                    "名称": "平安银行",
                    "最新价": 10,
                    "涨跌幅": -1.0,
                    "成交额": 500_000_000,
                },
            ]
        )

    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_a_spot", sina_spot)
    monkeypatch.chdir(tmp_path)

    assert picker.main() == 0

    result = pd.read_csv(tmp_path / "ak_candidates.csv")
    assert fallback_calls == [True]
    assert "换手率" in result.columns
    assert "市盈率-动态" in result.columns
    assert list(result["代码"].astype(str).str.zfill(6)) == ["600519", "000001"]
    assert list(result["tradingagents_ticker"]) == ["600519.SS", "000001.SZ"]


def test_main_prints_fallback_reason_and_uses_neutral_candidate_naming(
    monkeypatch, tmp_path, capsys
):
    def fail_eastmoney():
        raise requests.ConnectionError("remote closed")

    def sina_spot():
        return pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1700,
                    "涨跌幅": 2.0,
                    "成交额": 1_200_000_000,
                },
            ]
        )

    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_a_spot", sina_spot)
    monkeypatch.chdir(tmp_path)

    assert picker.main() == 0

    captured = capsys.readouterr()
    assert "fallback reason: stock_zh_a_spot_em: ConnectionError: remote closed" in captured.out
    assert "候选标的" in captured.out
    assert "候选股票" not in captured.out


def test_prepare_candidates_uses_historical_factors_to_rank_candidates(monkeypatch):
    pe = picker.DYNAMIC_PE_COL
    data = pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "MomentumLeader",
                "最新价": 13.0,
                "涨跌幅": 0.5,
                "成交额": 550_000_000,
                "换手率": 2.0,
                pe: 20.0,
            },
            {
                "代码": "600002",
                "名称": "SnapshotLeader",
                "最新价": 10.0,
                "涨跌幅": 2.0,
                "成交额": 1_200_000_000,
                "换手率": 2.0,
                pe: 20.0,
            },
        ]
    )

    histories = {
        "600001": _history_from_closes([10.0] * 40 + [10.5] * 10 + [11.0] * 10 + [13.0] * 5),
        "600002": _history_from_closes([12.0] * 40 + [11.0] * 10 + [10.5] * 10 + [10.0] * 5),
    }
    benchmark_history = _history_from_closes([10.0] * 40 + [10.2] * 10 + [10.4] * 10 + [10.5] * 5)

    monkeypatch.setattr(picker, "load_stock_history", lambda code: histories[code], raising=False)
    monkeypatch.setattr(picker, "load_benchmark_history", lambda: benchmark_history, raising=False)

    result = picker.prepare_candidates(data, "unit_source")

    assert result.iloc[0]["代码"] == "600001"
    assert result.iloc[0]["momentum_20d"] > result.iloc[1]["momentum_20d"]
    assert result.iloc[0]["relative_strength_20d"] > 0
    for col in [
        "momentum_20d",
        "momentum_60d",
        "volatility_20d",
        "ma_trend_20_60",
        "avg_amount_20d",
        "relative_strength_20d",
    ]:
        assert col in result.columns


def test_load_stock_history_falls_back_when_eastmoney_history_fails(monkeypatch):
    calls = []

    def fail_eastmoney(**kwargs):
        calls.append(("eastmoney", kwargs["symbol"]))
        raise requests.ConnectionError("eastmoney history closed")

    def sina_daily(**kwargs):
        calls.append(("sina", kwargs["symbol"]))
        return _history_from_closes([10.0] * 65)

    monkeypatch.setattr(picker.ak, "stock_zh_a_hist", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_a_daily", sina_daily)

    result = picker.load_stock_history("300604")

    assert calls == [("eastmoney", "300604"), ("sina", "sz300604")]
    assert not result.empty


def test_load_stock_history_prefers_eastmoney_source_when_available(monkeypatch):
    calls = []

    def eastmoney_history(**kwargs):
        calls.append("eastmoney")
        return _history_from_closes([10.0] * 65)

    def sina_daily(**kwargs):
        calls.append("sina")
        return _history_from_closes([10.0] * 65)

    monkeypatch.setattr(picker.ak, "stock_zh_a_hist", eastmoney_history)
    monkeypatch.setattr(picker.ak, "stock_zh_a_daily", sina_daily)

    result = picker.load_stock_history("300604")

    assert calls == ["eastmoney"]
    assert not result.empty


def test_load_benchmark_history_falls_back_when_eastmoney_index_fails(monkeypatch):
    calls = []

    def fail_eastmoney(**kwargs):
        calls.append(("eastmoney", kwargs["symbol"]))
        raise requests.ConnectionError("eastmoney index closed")

    def tx_index(**kwargs):
        calls.append(("tx", kwargs["symbol"]))
        return _history_from_closes([10.0] * 65)

    monkeypatch.setattr(picker.ak, "index_zh_a_hist", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_index_daily_tx", tx_index)

    result = picker.load_benchmark_history()

    assert calls == [("eastmoney", "000300"), ("tx", "sh000300")]
    assert not result.empty


def test_load_benchmark_history_prefers_eastmoney_source_when_available(monkeypatch):
    calls = []

    def eastmoney_index(**kwargs):
        calls.append("eastmoney")
        return _history_from_closes([10.0] * 65)

    def tx_index(**kwargs):
        calls.append("tx")
        return _history_from_closes([10.0] * 65)

    monkeypatch.setattr(picker.ak, "index_zh_a_hist", eastmoney_index)
    monkeypatch.setattr(picker.ak, "stock_zh_index_daily_tx", tx_index)

    result = picker.load_benchmark_history()

    assert calls == ["eastmoney"]
    assert not result.empty


def test_main_reports_clear_failure_when_all_spot_sources_fail(monkeypatch, tmp_path, capsys):
    def fail_source():
        raise requests.ConnectionError("remote closed")

    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", fail_source)
    monkeypatch.setattr(picker.ak, "stock_zh_a_spot", fail_source)
    monkeypatch.chdir(tmp_path)

    assert picker.main() == 1

    captured = capsys.readouterr()
    assert "Unable to fetch A-share spot data" in captured.out
    assert "stock_zh_a_spot_em" in captured.out
    assert "stock_zh_a_spot" in captured.out
    assert not (tmp_path / "ak_candidates.csv").exists()
