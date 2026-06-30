import pandas as pd
import pytest

from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.errors import NoMarketDataError


def _hist_frame():
    return pd.DataFrame(
        {
            "\u65e5\u671f": pd.to_datetime(["2026-06-27", "2026-06-29"]).date,
            "\u80a1\u7968\u4ee3\u7801": ["601138", "601138"],
            "\u5f00\u76d8": [68.0, 69.3],
            "\u6536\u76d8": [70.0, 69.61],
            "\u6700\u9ad8": [71.0, 71.46],
            "\u6700\u4f4e": [67.5, 66.5],
            "\u6210\u4ea4\u91cf": [1000, 1964970],
            "\u6210\u4ea4\u989d": [70000.0, 13558013854.0],
            "\u6da8\u8dcc\u5e45": [1.2, -0.87],
            "\u6362\u624b\u7387": [0.5, 0.99],
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


@pytest.mark.unit
def test_indicator_reuses_cached_akshare_ohlcv(monkeypatch):
    cache = getattr(akshare_data, "_load_ohlcv_cached", None)
    if cache is not None:
        cache.cache_clear()
    calls = 0

    def fake_hist(**kwargs):
        nonlocal calls
        calls += 1
        return _hist_frame()

    monkeypatch.setattr(akshare_data.ak, "stock_zh_a_hist", fake_hist)

    akshare_data.get_stock_stats_indicators_window(
        "601138.SH", "close_10_ema", "2026-06-29", 1
    )
    akshare_data.get_stock_stats_indicators_window(
        "601138.SH", "rsi", "2026-06-29", 1
    )

    assert calls == 1


@pytest.mark.unit
def test_get_news_filters_to_requested_window(monkeypatch):
    news = pd.DataFrame({
        "\u5173\u952e\u8bcd": ["601138", "601138"],
        "\u65b0\u95fb\u6807\u9898": ["inside window", "outside window"],
        "\u65b0\u95fb\u5185\u5bb9": ["kept body", "old body"],
        "\u53d1\u5e03\u65f6\u95f4": ["2026-06-26 16:30:06", "2026-05-01 09:00:00"],
        "\u6587\u7ae0\u6765\u6e90": ["source a", "source b"],
        "\u65b0\u95fb\u94fe\u63a5": ["https://example.test/1", "https://example.test/2"],
    })
    monkeypatch.setattr(akshare_data.ak, "stock_news_em", lambda symbol: news)

    out = akshare_data.get_news("601138.SH", "2026-06-20", "2026-06-29")

    assert "## 601138.SS News" in out
    assert "inside window" in out
    assert "kept body" in out
    assert "outside window" not in out


@pytest.mark.unit
def test_get_news_excludes_next_day_midnight(monkeypatch):
    news = pd.DataFrame({
        "\u5173\u952e\u8bcd": ["601138", "601138"],
        "\u65b0\u95fb\u6807\u9898": ["inside window", "next day midnight"],
        "\u65b0\u95fb\u5185\u5bb9": ["kept body", "future body"],
        "\u53d1\u5e03\u65f6\u95f4": ["2026-06-29 23:59:59", "2026-06-30 00:00:00"],
        "\u6587\u7ae0\u6765\u6e90": ["source a", "source b"],
        "\u65b0\u95fb\u94fe\u63a5": ["https://example.test/1", "https://example.test/2"],
    })
    monkeypatch.setattr(akshare_data.ak, "stock_news_em", lambda symbol: news)

    out = akshare_data.get_news("601138.SH", "2026-06-20", "2026-06-29")

    assert "inside window" in out
    assert "next day midnight" not in out


@pytest.mark.unit
def test_get_fundamentals_degrades_failed_optional_endpoint(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zyjs_ths",
        lambda symbol: pd.DataFrame({
            "\u80a1\u7968\u4ee3\u7801": ["601138"],
            "\u4e3b\u8425\u4e1a\u52a1": ["test business"],
            "\u4ea7\u54c1\u7c7b\u578b": ["3C product type"],
            "\u4ea7\u54c1\u540d\u79f0": ["3C product name"],
            "\u7ecf\u8425\u8303\u56f4": ["test scope"],
        }),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        lambda symbol: pd.DataFrame({
            "\u9009\u9879": ["common indicators"],
            "\u6307\u6807": ["net profit"],
            "20260331": ["100"],
            "20251231": ["90"],
        }),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        lambda stock, market: (_ for _ in ()).throw(ValueError("shape mismatch")),
    )
    monkeypatch.setattr(
        akshare_data,
        "_get_yfinance_fundamentals",
        lambda ticker, curr_date: "Name: Foxconn Industrial Internet Co., Ltd.",
    )

    out = akshare_data.get_fundamentals("601138.SS", "2026-06-29")

    assert "# Company Fundamentals for 601138.SS" in out
    assert "Primary source: AKShare" in out
    assert "test business" in out
    assert "net profit" in out
    assert "Yahoo Supplemental Profile" in out
    assert "Foxconn Industrial Internet" in out
    assert "DATA_DEGRADED: AKShare stock_individual_fund_flow unavailable" in out


@pytest.mark.unit
def test_get_fundamentals_degrades_yahoo_supplemental_error(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zyjs_ths",
        lambda symbol: pd.DataFrame({"\u4e3b\u8425\u4e1a\u52a1": ["test business"]}),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        lambda symbol: pd.DataFrame({"\u9009\u9879": ["common"], "\u6307\u6807": ["net profit"], "20260331": ["100"]}),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        lambda stock, market: pd.DataFrame({"\u65e5\u671f": ["2026-06-29"], "\u6536\u76d8\u4ef7": [69.61]}),
    )
    monkeypatch.setattr(
        akshare_data,
        "_get_yfinance_fundamentals",
        lambda ticker, curr_date: "Error retrieving fundamentals for 601138.SS: timeout",
    )

    out = akshare_data.get_fundamentals("601138.SS", "2026-06-29")

    assert "Yahoo Supplemental Profile" not in out
    assert "DATA_DEGRADED: Yahoo supplemental fundamentals unavailable" in out


@pytest.mark.unit
def test_financial_abstract_missing_label_columns_degrades():
    data = pd.DataFrame({"20260331": ["100"], "20251231": ["90"]})

    lines, errors = akshare_data._financial_abstract_section_from_frame(data)

    assert lines == []
    assert errors == [
        "DATA_DEGRADED: AKShare stock_financial_abstract missing label columns: \u9009\u9879, \u6307\u6807."
    ]
