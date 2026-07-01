import pandas as pd
import requests

import ak_pick_a_stock as picker


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
