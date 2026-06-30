import pandas as pd
import pytest

from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.errors import NoMarketDataError


def _hist_frame():
    return pd.DataFrame(
        {
            "日期": pd.to_datetime(["2026-06-27", "2026-06-29"]).date,
            "股票代码": ["601138", "601138"],
            "开盘": [68.0, 69.3],
            "收盘": [70.0, 69.61],
            "最高": [71.0, 71.46],
            "最低": [67.5, 66.5],
            "成交量": [1000, 1964970],
            "成交额": [70000.0, 13558013854.0],
            "涨跌幅": [1.2, -0.87],
            "换手率": [0.5, 0.99],
        }
    )


@pytest.mark.unit
def test_get_stock_data_formats_akshare_ohlcv(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: _hist_frame(),
    )

    out = akshare_data.get_stock_data("601138.SH", "2026-06-01", "2026-06-29")

    assert "# Stock data for 601138.SS" in out
    assert "# Primary source: AKShare stock_zh_a_hist" in out
    assert "Date,Open,High,Low,Close,Volume,Amount" in out
    assert "2026-06-29,69.3,71.46,66.5,69.61,1964970,13558013854.0" in out


@pytest.mark.unit
def test_get_stock_data_rejects_non_china_symbol():
    with pytest.raises(NoMarketDataError):
        akshare_data.get_stock_data("AAPL", "2026-06-01", "2026-06-29")


@pytest.mark.unit
def test_get_stock_data_empty_frame_raises_no_market_data(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: pd.DataFrame(),
    )

    with pytest.raises(NoMarketDataError) as exc:
        akshare_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    assert "AKShare returned no rows" in str(exc.value)


@pytest.mark.unit
def test_indicator_uses_akshare_ohlcv(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: _hist_frame(),
    )

    out = akshare_data.get_stock_stats_indicators_window(
        "601138.SH", "close_10_ema", "2026-06-29", 1
    )

    assert "## close_10_ema values" in out
    assert "2026-06-29:" in out
    assert "AKShare stock_zh_a_hist" in out
