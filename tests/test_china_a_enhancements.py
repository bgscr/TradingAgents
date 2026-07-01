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
        lambda symbol: pd.DataFrame({"rank": [287], "code": ["600895"]}),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: pd.DataFrame({"title": ["chipmaking", "zhangjiang"]}),
    )

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "## China A-share Enhancement Snapshot" in out
    assert "Preset: flow_sentiment" in out
    assert "stock_individual_fund_flow" in out
    assert out.count("Source:") <= 8


@pytest.mark.unit
def test_flow_sentiment_snapshot_caps_total_source_labeled_lines(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_fund_flow",
        lambda stock, market: pd.DataFrame(
            {
                "date": [f"2026-06-{day:02d}" for day in range(26, 31)],
                "close": [40, 41, 42, 43, 44],
                "pct_change": [1, 1, 1, 1, 1],
                "main_net_inflow": [10, 11, 12, 13, 14],
                "main_net_ratio": [2, 2, 2, 2, 2],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_lhb_detail_em",
        lambda start_date, end_date: pd.DataFrame(
            {
                "code": ["600895", "600895", "600895"],
                "date": ["2026-06-28", "2026-06-29", "2026-06-30"],
                "reason": ["reason-1", "reason-2", "reason-3"],
                "net_buy": ["1", "2", "3"],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_margin_detail_sse",
        lambda date: pd.DataFrame(
            {
                "code": ["600895", "600895"],
                "financing_balance": ["100", "101"],
                "securities_lending_balance": ["10", "11"],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_rank_latest_em",
        lambda symbol: pd.DataFrame({"rank": [12]}),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: pd.DataFrame({"title": ["chipmaking", "zhangjiang", "semiconductor"]}),
    )

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")
    source_lines = [
        line
        for line in out.splitlines()
        if line.startswith("- Source:") or line.startswith("- Source unavailable:")
    ]

    assert len(source_lines) <= enh.MAX_SOURCE_LINES
    assert any("stock_individual_fund_flow" in line for line in source_lines)


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
            {"title": ["dividend announcement"], "date": ["2026-06-20"]}
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
    assert "dividend announcement" in out
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
        lambda symbol: pd.DataFrame({"title": ["chipmaking"]}),
    )

    first = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")
    second = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert first == second
    assert calls["fund_flow"] == 1


@pytest.mark.unit
def test_industry_policy_uses_ticker_specific_profile_fields(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    def individual_info(symbol):
        industry = {
            "600895": "Semiconductor",
            "000333": "Home Appliance",
        }[symbol]
        return pd.DataFrame({"item": ["industry"], "value": [industry]})

    monkeypatch.setattr(enh.ak, "stock_individual_info_em", individual_info, raising=False)
    monkeypatch.setattr(
        enh.ak,
        "stock_sector_fund_flow_rank",
        lambda indicator: pd.DataFrame(
            {
                "sector": ["Semiconductor", "Home Appliance"],
                "pct_change": ["1.2", "-0.4"],
                "net_inflow": ["1200", "-300"],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_info_global_em",
        lambda: pd.DataFrame(
            {
                "title": [
                    "Semiconductor policy support expands",
                    "Home Appliance subsidy update",
                ],
                "source": ["policy desk", "policy desk"],
            }
        ),
    )

    semiconductor = enh.get_china_a_enhancements(
        "600895.SS", "2026-06-30", "industry_policy"
    )
    appliance = enh.get_china_a_enhancements(
        "000333.SZ", "2026-06-30", "industry_policy"
    )

    assert "stock_code=600895; industry=Semiconductor" in semiconductor
    assert "sector=Semiconductor" in semiconductor
    assert "sector=Home Appliance" not in semiconductor
    assert "stock_code=000333; industry=Home Appliance" in appliance
    assert "sector=Home Appliance" in appliance
    assert "sector=Semiconductor" not in appliance


@pytest.mark.unit
def test_industry_policy_does_not_emit_generic_rows_when_profile_fails(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_info_em",
        lambda symbol: (_ for _ in ()).throw(ValueError("profile offline")),
        raising=False,
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_sector_fund_flow_rank",
        lambda indicator: pd.DataFrame(
            {"sector": ["Generic Sector"], "pct_change": ["1.2"], "net_inflow": ["1200"]}
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_info_global_em",
        lambda: pd.DataFrame(
            {"title": ["Generic market policy headline"], "source": ["policy desk"]}
        ),
    )

    out = enh.get_china_a_enhancements(
        "600895.SS", "2026-06-30", "industry_policy"
    )

    assert "Source unavailable: stock_individual_info_em" in out
    assert "no ticker-specific industry/concept fields available" in out
    assert "Generic Sector" not in out
    assert "Generic market policy headline" not in out


@pytest.mark.unit
def test_industry_policy_does_not_emit_generic_rows_when_profile_has_no_fields(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_info_em",
        lambda symbol: pd.DataFrame({"item": ["name"], "value": ["Ticker Name"]}),
        raising=False,
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_sector_fund_flow_rank",
        lambda indicator: pd.DataFrame(
            {"sector": ["Generic Sector"], "pct_change": ["1.2"], "net_inflow": ["1200"]}
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_info_global_em",
        lambda: pd.DataFrame(
            {"title": ["Generic market policy headline"], "source": ["policy desk"]}
        ),
    )

    out = enh.get_china_a_enhancements(
        "600895.SS", "2026-06-30", "industry_policy"
    )

    assert "stock_code=600895; no industry/concept fields returned" in out
    assert "no ticker-specific industry/concept fields available" in out
    assert "Generic Sector" not in out
    assert "Generic market policy headline" not in out


@pytest.mark.unit
def test_all_preset_preserves_source_lines_for_each_requested_category(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(
        enh.ak,
        "stock_individual_fund_flow",
        lambda stock, market: pd.DataFrame(
            {
                "date": [f"2026-06-{day:02d}" for day in range(26, 31)],
                "close": [40, 41, 42, 43, 44],
                "pct_change": [1, 1, 1, 1, 1],
                "main_net_inflow": [10, 11, 12, 13, 14],
                "main_net_ratio": [2, 2, 2, 2, 2],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_lhb_detail_em",
        lambda start_date, end_date: pd.DataFrame(
            {
                "code": ["600895", "600895", "600895"],
                "date": ["2026-06-28", "2026-06-29", "2026-06-30"],
                "reason": ["reason-1", "reason-2", "reason-3"],
                "net_buy": ["1", "2", "3"],
            }
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_margin_detail_sse",
        lambda date: pd.DataFrame(
            {
                "code": ["600895", "600895"],
                "financing_balance": ["100", "101"],
                "securities_lending_balance": ["10", "11"],
            }
        ),
    )
    monkeypatch.setattr(enh.ak, "stock_hot_rank_latest_em", lambda symbol: pd.DataFrame({"rank": [12]}))
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: pd.DataFrame({"title": ["chipmaking", "zhangjiang"]}),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_individual_notice_report",
        lambda security, symbol, begin_date, end_date: pd.DataFrame(
            {"title": ["dividend announcement"], "date": ["2026-06-20"]}
        ),
    )
    monkeypatch.setattr(enh.ak, "stock_zh_a_disclosure_report_cninfo", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        enh.ak,
        "stock_individual_info_em",
        lambda symbol: pd.DataFrame({"item": ["industry"], "value": ["Semiconductor"]}),
        raising=False,
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_sector_fund_flow_rank",
        lambda indicator: pd.DataFrame(
            {"sector": ["Semiconductor"], "pct_change": ["1.2"], "net_inflow": ["1200"]}
        ),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_info_global_em",
        lambda: pd.DataFrame(
            {"title": ["Semiconductor policy support expands"], "source": ["policy desk"]}
        ),
    )

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "all")

    assert "stock_individual_fund_flow" in out
    assert "stock_individual_notice_report" in out
    assert "stock_individual_info_em" in out
