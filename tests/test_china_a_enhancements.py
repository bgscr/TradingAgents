import pandas as pd
import pytest


@pytest.mark.unit
def test_basic_preset_returns_empty_for_china_a():
    from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements

    assert get_china_a_enhancements("600895.SS", "2026-06-30", "basic") == ""


@pytest.mark.unit
def test_non_china_symbol_returns_empty():
    from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements

    assert get_china_a_enhancements("AAPL", "2026-06-30", "all") == ""


@pytest.mark.unit
def test_flow_sentiment_snapshot_limits_records(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    fund_flow = pd.DataFrame(
        {
            "date": [f"2026-06-{day:02d}" for day in range(20, 31)],
            "close": [30 + day for day in range(11)],
            "pct_change": [1.0] * 11,
            "main_net_inflow": [1000 * day for day in range(11)],
            "main_net_ratio": [2.5] * 11,
        }
    )
    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", lambda stock, market: fund_flow)
    monkeypatch.setattr(enh.ak, "stock_lhb_detail_em", lambda start_date, end_date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_rank_latest_em",
        lambda symbol: pd.DataFrame({"rank": [287], "证券代码": ["600895"]}),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: pd.DataFrame({"title": ["光刻机", "张江"]}),
    )

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "## China A-share Enhancement Snapshot" in out
    assert "Preset: flow_sentiment" in out
    assert "stock_individual_fund_flow" in out
    assert out.count("Source:") <= 8
    assert "光刻机" in out


@pytest.mark.unit
def test_source_failure_degrades_without_raising(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_fund_flow",
        lambda stock, market: (_ for _ in ()).throw(ValueError("shape mismatch")),
    )
    monkeypatch.setattr(enh.ak, "stock_lhb_detail_em", lambda start_date, end_date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_rank_latest_em", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_keyword_em", lambda symbol: pd.DataFrame())

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "Source unavailable: stock_individual_fund_flow" in out
    assert "shape mismatch" in out


@pytest.mark.unit
def test_category_filter_omits_unrequested_sections(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_notice_report",
        lambda security, symbol, begin_date, end_date: pd.DataFrame(
            {"title": ["分红公告"], "date": ["2026-06-20"]}
        ),
    )
    monkeypatch.setattr(enh.ak, "stock_zh_a_disclosure_report_cninfo", lambda **kwargs: pd.DataFrame())

    out = enh.get_china_a_enhancements_for_categories(
        "600895.SS",
        "2026-06-30",
        "announcements",
        {"announcements"},
    )

    assert "Announcements and disclosures" in out
    assert "分红公告" in out
    assert "Fund flow and trading activity" not in out


@pytest.mark.unit
def test_cache_avoids_repeat_source_calls_within_ttl(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    calls = {"fund_flow": 0}

    def fund_flow(stock, market):
        calls["fund_flow"] += 1
        return pd.DataFrame(
            {
                "date": ["2026-06-30"],
                "close": [41.2],
                "pct_change": [1.3],
                "main_net_inflow": [1200000],
                "main_net_ratio": [4.2],
            }
        )

    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", fund_flow)
    monkeypatch.setattr(enh.ak, "stock_lhb_detail_em", lambda start_date, end_date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_rank_latest_em",
        lambda symbol: pd.DataFrame({"rank": [12]}),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: pd.DataFrame({"title": ["光刻机"]}),
    )

    first = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")
    second = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert first == second
    assert calls["fund_flow"] == 1
