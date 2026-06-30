import pandas as pd
import pytest

from tradingagents.dataflows import china_sentiment


@pytest.mark.unit
def test_get_china_a_local_sentiment_formats_local_sources(monkeypatch):
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_hot_rank_detail_em",
        lambda symbol: pd.DataFrame({
            "\u65f6\u95f4": ["2026-06-26", "2026-06-29"],
            "\u6392\u540d": [36, 83],
            "\u8bc1\u5238\u4ee3\u7801": ["SH601138", "SH601138"],
            "\u65b0\u664b\u7c89\u4e1d": [0.2007, 0.1701],
            "\u94c1\u6746\u7c89\u4e1d": [0.7993, 0.8299],
        }),
    )
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_comment_em",
        lambda: pd.DataFrame({
            "\u4ee3\u7801": ["601138", "600000"],
            "\u540d\u79f0": ["\u5de5\u4e1a\u5bcc\u8054", "\u6d66\u53d1\u94f6\u884c"],
            "\u6700\u65b0\u4ef7": [71.75, 10.0],
            "\u673a\u6784\u53c2\u4e0e\u5ea6": [0.4867, 0.1],
            "\u7efc\u5408\u5f97\u5206": [77.32, 20.0],
            "\u5173\u6ce8\u6307\u6570": [92.8, 5.0],
            "\u4ea4\u6613\u65e5": ["2026-06-29", "2026-06-29"],
        }),
    )
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_hsgt_individual_em",
        lambda symbol: pd.DataFrame({
            "\u6301\u80a1\u65e5\u671f": ["2026-06-26", "2026-06-29"],
            "\u5f53\u65e5\u6536\u76d8\u4ef7": [70.22, 69.61],
            "\u6301\u80a1\u6570\u91cf": [1000, 1200],
            "\u6301\u80a1\u5e02\u503c": [70220.0, 83532.0],
            "\u4eca\u65e5\u589e\u6301\u80a1\u6570": [-100, 200],
            "\u4eca\u65e5\u589e\u6301\u8d44\u91d1": [-7022.0, 13922.0],
        }),
    )

    out = china_sentiment.get_china_a_local_sentiment(
        "601138.SH", "2026-06-22", "2026-06-29"
    )

    assert "# China A-share local sentiment for 601138.SS" in out
    assert "Source: AKShare/Eastmoney" in out
    assert "Eastmoney popularity trend" in out
    assert "2026-06-29,83,SH601138,0.1701,0.8299" in out
    assert "Eastmoney stock comment" in out
    assert "\u5de5\u4e1a\u5bcc\u8054" in out
    assert "Northbound holdings" in out
    assert "2026-06-29,69.61,1200,83532.0,200,13922.0" in out


@pytest.mark.unit
def test_get_china_a_local_sentiment_degrades_optional_endpoint(monkeypatch):
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_hot_rank_detail_em",
        lambda symbol: pd.DataFrame({
            "\u65f6\u95f4": ["2026-06-29"],
            "\u6392\u540d": [83],
            "\u8bc1\u5238\u4ee3\u7801": ["SH601138"],
        }),
    )
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_comment_em",
        lambda: (_ for _ in ()).throw(ValueError("shape mismatch")),
    )
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_hsgt_individual_em",
        lambda symbol: pd.DataFrame(),
    )

    out = china_sentiment.get_china_a_local_sentiment(
        "601138.SS", "2026-06-22", "2026-06-29"
    )

    assert "Eastmoney popularity trend" in out
    assert "DATA_DEGRADED: AKShare stock_comment_em unavailable" in out
    assert "DATA_DEGRADED: AKShare stock_hsgt_individual_em returned no rows." in out
    assert "Do not fabricate degraded or missing China local sentiment values." in out


@pytest.mark.unit
def test_get_china_a_local_sentiment_marks_non_china_as_not_applicable():
    out = china_sentiment.get_china_a_local_sentiment(
        "AAPL", "2026-06-22", "2026-06-29"
    )

    assert "not applicable" in out
    assert "China A-share symbols only" in out
