"""Public dispatch policy: optional endpoints never send when explicitly omitted."""
import copy

import pandas as pd
import pytest

from tests.test_analysis_budget import config as budget_config
from tradingagents.analysis_budget import analysis_budget_scope
from tradingagents.dataflows import akshare_data, config as config_module, interface
from tradingagents.default_config import DEFAULT_CONFIG


def selected(**overrides):
    return {
        **copy.deepcopy(DEFAULT_CONFIG),
        **budget_config(),
        "analysis_allowed_data_vendors": ["tushare", "akshare"],
        "analysis_skip_optional_fundamentals": True,
        "tool_vendors": {
            "get_fundamentals": "akshare",
            "get_balance_sheet": "tushare",
            "get_income_statement": "tushare",
            "get_cashflow": "tushare",
        },
        **overrides,
    }


def test_explicit_omission_reaches_registered_dispatch_with_zero_optional_calls(monkeypatch):
    settings = selected()
    monkeypatch.setattr(config_module, "_config", settings)
    optional_calls = []
    def forbidden(*args, **kwargs):
        optional_calls.append(1)
        raise AssertionError("disabled endpoint invoked")
    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", forbidden)
    monkeypatch.setattr(akshare_data.ak, "stock_financial_abstract", forbidden)
    monkeypatch.setattr(akshare_data, "_yahoo_supplemental_section", forbidden)
    monkeypatch.setattr(akshare_data.ak, "stock_individual_fund_flow", lambda **kwargs: pd.DataFrame({
        "日期": ["2026-10-16"], "主力净流入-净额": [1.0],
    }))
    with analysis_budget_scope(settings) as budget:
        text = interface.route_to_vendor("get_fundamentals", "601328.SH", "2026-10-16")
    assert optional_calls == []
    assert "optional_business_omitted" in text
    assert "optional_financial_summary_omitted" in text
    assert "主力净流入-净额" in text and "2026-10-16" in text
    assert budget.receipt()["admitted_attempts"] == 0  # Transport is synthetic here.
    assert {item["tool"] for item in budget.receipt()["data_gaps"]} >= {
        "get_fundamentals.business", "get_fundamentals.financial_summary",
    }

def test_news_wire_uses_safe_decoder_and_preserves_coverage_gap(monkeypatch):
    import json

    import requests

    from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run

    settings = selected(tool_vendors={"get_news": "akshare"})
    monkeypatch.setattr(config_module, "_config", settings)
    calls = []
    rows = [{
        "date": "2026-10-10 12:00:00", "code": "202610100001",
        "title": "601328.SH synthetic disclosure", "content": "Transport fixture.",
    }]
    def wire(adapter, request, **kwargs):
        calls.append((request.method, request.url))
        response = requests.Response()
        response.status_code = 200
        response._content = ("another_callback(" + json.dumps({
            "result": {"cmsArticleWebOld": rows},
        }) + ");").encode()
        response._content_consumed = True
        response.request = request
        response.url = request.url
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", wire)
    with analysis_budget_scope(settings) as budget, authoritative_snapshot_run():
        result = interface.route_to_vendor_acquired(
            "get_news", "601328.SH", "2026-10-09", "2026-10-16",
            tool_call_id="safe-wire", source_ref="safe-wire:601328", capability="get_news",
        )
    assert len(calls) == budget.receipt()["admitted_attempts"] == 1
    assert result.artifact is not None
    assert "601328.SH synthetic disclosure" in result.artifact.raw_text
    assert "news_window_coverage_unknown" in result.artifact.raw_text

@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_invalid_source_policy_is_rejected(value):
    from tradingagents.analysis_budget import validate_budget_config
    with pytest.raises(ValueError, match="analysis_skip_optional_fundamentals"):
        validate_budget_config(selected(analysis_skip_optional_fundamentals=value))


def test_omission_requires_budgeted_reporting():
    from tradingagents.analysis_budget import validate_budget_config
    with pytest.raises(ValueError, match="requires_analysis_budget"):
        validate_budget_config(selected(analysis_budget_enabled=False, financial_cache_only=False,
                                        analysis_allowed_data_vendors=None, analysis_disable_alpha_vantage=False))


def test_report_lists_only_fixed_missing_data_categories():
    from tradingagents.reporting import budget_data_gap_markdown
    gaps = [
        {"provider": "akshare", "tool": "get_fundamentals.business", "reason": "optional_business_omitted"},
        {"provider": "akshare", "tool": "get_fundamentals.financial_summary", "reason": "optional_financial_summary_omitted"},
        {"provider": "akshare", "tool": "get_news.coverage", "reason": "news_window_coverage_unknown"},
        {"provider": "akshare", "tool": "get_news", "reason": "news_unavailable"},
        {"provider": "untrusted", "tool": "get_news", "reason": "do-not-render-arbitrary-values"},
    ]
    text = budget_data_gap_markdown({"analysis_budget": {"data_gaps": gaps}}, "Chinese")
    for item in gaps[:-1]:
        assert item["reason"] in text
    assert "do-not-render-arbitrary-values" not in text
    assert "不是来源事实" in text


@pytest.mark.parametrize("function", ["get_fundamentals", "get_china_a_identity"])
def test_omission_cannot_run_outside_budget(function, monkeypatch):
    from tradingagents.analysis_budget import AnalysisBudgetStopped

    monkeypatch.setattr(config_module, "_config", selected())
    with pytest.raises(AnalysisBudgetStopped, match="optional_sources_require_budget"):
        getattr(akshare_data, function)("601328.SH")


def test_direct_router_cannot_fallback_after_missing_budget(monkeypatch):
    from tradingagents.analysis_budget import AnalysisBudgetStopped

    settings = selected(tool_vendors={"get_fundamentals": "akshare,yfinance"})
    monkeypatch.setattr(config_module, "_config", settings)
    monkeypatch.setitem(interface.VENDOR_METHODS["get_fundamentals"], "yfinance",
                        lambda *args, **kwargs: pytest.fail("policy bypass via fallback"))
    with pytest.raises(AnalysisBudgetStopped, match="optional_sources_require_budget"):
        interface.route_to_vendor("get_fundamentals", "601328.SH", "2026-10-16")


def test_identity_enrichment_respects_optional_omission(monkeypatch):
    settings = selected()
    monkeypatch.setattr(config_module, "_config", settings)
    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", lambda **kwargs: pytest.fail("disabled THS invoked"))
    with analysis_budget_scope(settings) as budget:
        identity = akshare_data.get_china_a_identity("601328.SH")
    assert identity == {"exchange": "shanghai"}
    assert budget.receipt()["admitted_attempts"] == 0
    assert budget.receipt()["data_gaps"] == [{"tool": "get_fundamentals.business", "provider": "akshare", "reason": "optional_business_omitted"}]


def test_environment_profile_and_cli_preserve_explicit_policy(tmp_path, monkeypatch):
    import json

    import cli.main as cli
    from tradingagents import default_config

    monkeypatch.setenv("TRADINGAGENTS_ANALYSIS_SKIP_OPTIONAL_FUNDAMENTALS", "true")
    configured = default_config._apply_env_overrides(copy.deepcopy(DEFAULT_CONFIG))
    assert configured["analysis_skip_optional_fundamentals"] is True
    profile = tmp_path / "tradingagents.local.json"
    profile.write_text(json.dumps({"analysis_skip_optional_fundamentals": False}), encoding="utf-8")
    monkeypatch.setattr(default_config, "_PROJECT_RUNTIME_ROOT", str(tmp_path))
    configured = default_config._apply_local_analysis_overrides(configured)
    assert configured["analysis_skip_optional_fundamentals"] is False
    configured["analysis_budget_enabled"] = False
    configured["financial_cache_only"] = False
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", configured)
    assembled = cli._build_run_config({
        "ticker": "601328.SH", "analysis_date": "2026-10-16", "research_depth": 1,
        "shallow_thinker": "deepseek-flash", "deep_thinker": "deepseek-v4-pro",
        "backend_url": "https://api.deepseek.com", "llm_provider": "DeepSeek",
    }, False)
    assert assembled["analysis_skip_optional_fundamentals"] is False
    profile.write_text(json.dumps({"analysis_skip_optional_fundamentals": "false"}), encoding="utf-8")
    with pytest.raises(ValueError, match="optional fundamentals"):
        default_config._apply_local_analysis_overrides(configured)


def test_midrun_source_policy_change_stops_before_dispatch(monkeypatch):
    from tradingagents.analysis_budget import AnalysisBudgetStopped

    configured = selected()
    monkeypatch.setattr(config_module, "_config", configured)
    with pytest.raises(AnalysisBudgetStopped, match="source_policy_changed"), analysis_budget_scope(configured) as budget:
        monkeypatch.setattr(config_module, "_config", {**configured, "analysis_skip_optional_fundamentals": False})
        with pytest.raises(AnalysisBudgetStopped, match="source_policy_changed"):
            interface.route_to_vendor("get_fundamentals", "601328.SH", "2026-10-16")
    assert budget.receipt()["admitted_attempts"] == 0
    assert budget.receipt()["stop_reason"] == "source_policy_changed"


def test_source_policy_cannot_change_by_checkpoint_reopen(tmp_path):
    from tests.test_checkpoint_location import read_ledger
    from tradingagents.analysis_budget import (
        AnalysisBudget,
        AnalysisBudgetStopped,
        checkpoint_budget_binding,
    )
    from tradingagents.graph.checkpointer import get_checkpointer

    first = selected(checkpoint_enabled=True, checkpoint_data_dir=str(tmp_path / "state"))
    changed = {**first, "analysis_skip_optional_fundamentals": False}
    binding = checkpoint_budget_binding(first, "unchanged-graph")
    assert binding != checkpoint_budget_binding(changed, "unchanged-graph")
    with get_checkpointer(first["checkpoint_data_dir"], "601328.SS") as saver:
        initial = AnalysisBudget(60, 600, skip_optional_fundamentals=True, wall_clock=lambda: 1000)
        initial.bind_checkpoint(saver.conn, saver.lock, "same-thread", binding, has_checkpoint=False)
        initial.admit("data")
        initial.record_interruption()
        before = read_ledger(saver)
    with get_checkpointer(first["checkpoint_data_dir"], "601328.SS") as saver:
        altered = AnalysisBudget(60, 600, wall_clock=lambda: 1010)
        with pytest.raises(AnalysisBudgetStopped, match="checkpoint_budget_mismatch"):
            altered.bind_checkpoint(saver.conn, saver.lock, "same-thread",
                                    checkpoint_budget_binding(changed, "unchanged-graph"), has_checkpoint=False)
        assert read_ledger(saver) == before
        resumed = AnalysisBudget(60, 600, skip_optional_fundamentals=True, wall_clock=lambda: 1010)
        resumed.bind_checkpoint(saver.conn, saver.lock, "same-thread", binding, has_checkpoint=False)
        assert resumed.receipt()["admitted_attempts"] == 1
        assert read_ledger(saver)[1]["deadline"] == 1600


def test_remaining_source_redirect_is_terminal_not_optional(monkeypatch):
    import requests

    from tradingagents.analysis_budget import AnalysisBudgetStopped

    settings = selected()
    monkeypatch.setattr(config_module, "_config", settings)
    sends = []
    def wire(adapter, request, **kwargs):
        sends.append(request.url)
        response = requests.Response()
        response.status_code = 301
        response._content = b""
        response._content_consumed = True
        response.request = request
        response.url = request.url
        response.headers["Location"] = "https://unapproved.invalid/"
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", wire)
    with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"), analysis_budget_scope(settings) as budget:
        with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"):
            interface.route_to_vendor("get_fundamentals", "601328.SH", "2026-10-16")
        with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"):
            budget.check()
    assert len(sends) == budget.receipt()["admitted_attempts"] == 1
    assert "unapproved.invalid" not in sends[0]



