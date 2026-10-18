"""Offline composition checks: actual config, CLI, graph, catalogs and routers."""
import json
from copy import deepcopy

import pytest

from tradingagents.budget_preflight import (
    ANALYST_TOOLS,
    BudgetRouteConfigurationError,
    inspect_budget_routes,
    validate_budget_routes,
)
from tradingagents.dataflows import config as runtime
from tradingagents.default_config import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.setattr(runtime, '_config', deepcopy(DEFAULT_CONFIG))


def settings():
    result = deepcopy(DEFAULT_CONFIG)
    result.update(analysis_budget_enabled=True, analysis_max_calls=30,
                  analysis_timeout_seconds=600, llm_provider='deepseek',
                  market_history_mode='current', checkpoint_enabled=False,
                  mainland_capability_routing_mode='legacy', financial_cache_only=True)
    result['market_data_vendors'] = {'cn_a': {
        'core_stock_apis': 'akshare', 'news_data': 'akshare', 'fundamental_data': 'akshare'}}
    result['tool_vendors'] = dict.fromkeys(('get_balance_sheet', 'get_income_statement', 'get_cashflow'), 'tushare')
    return result


def test_all_four_reports_every_uncovered_capability_without_spend():
    config = settings()
    report = inspect_budget_routes(config, symbol='601328.SH')
    actual = {(item.location, item.provider, item.reason) for item in report.issues}
    assert actual == {
        ('news.get_global_news', 'yfinance', 'uncovered_data_vendor'),
        ('news.get_insider_transactions', 'akshare', 'placeholder_implementation'),
    }
    assert dict(report.chains)['market_snapshot'] == ('akshare',)
    assert 'verified_mainland_calendar_window_or_uncovered_baostock_acquisition' in report.dependencies
    with pytest.raises(BudgetRouteConfigurationError) as failure:
        validate_budget_routes(config, symbol='601328.SH')
    assert failure.value.report == report
    assert 'readiness_not_checked=' in str(failure.value)


@pytest.mark.parametrize('chain', ['tushare,akshare', 'baostock,akshare', 'akshare,yfinance', 'default'])
def test_every_snapshot_fallback_is_checked_and_matches_actual_resolver(chain):
    from tradingagents.dataflows.market_snapshot import _provider_chain
    config = settings()
    config['market_data_vendors']['cn_a']['core_stock_apis'] = chain
    config['data_vendors']['core_stock_apis'] = 'default'
    config['tool_vendors']['get_stock_data'] = 'akshare'
    runtime.set_config(config)
    report = inspect_budget_routes(runtime.get_config(), symbol='601328.SH', selected_analysts=('market',))
    assert dict(report.chains)['market_snapshot'] == tuple(_provider_chain('601328.SH'))
    assert report.issues
    assert all(item.location == 'market_snapshot' for item in report.issues)


def test_profile_env_and_cli_merge_rejected_before_clients(tmp_path, monkeypatch):
    import cli.main as cli
    import tradingagents.default_config as defaults
    import tradingagents.graph.trading_graph as graph
    from tradingagents.market_history.store import MarketHistoryStore
    monkeypatch.setenv('TRADINGAGENTS_CN_A_CORE_STOCK_APIS', 'akshare')
    monkeypatch.setattr(defaults, '_PROJECT_RUNTIME_ROOT', str(tmp_path))
    (tmp_path / 'tradingagents.local.json').write_text(json.dumps({
        'market_data_vendors': {'cn_a': {'core_stock_apis': 'tushare,baostock'}}}), encoding='utf-8')
    config = defaults._apply_local_analysis_overrides(defaults._apply_env_overrides(settings()))
    config.update(market_history_database_path=str(tmp_path / 'history.sqlite3'),
                  data_cache_dir=str(tmp_path / 'cache'))
    monkeypatch.setattr(cli, 'DEFAULT_CONFIG', config)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append('unexpected side effect')
        raise AssertionError('preflight must reject first')

    monkeypatch.setattr(graph, 'create_llm_client', forbidden)
    monkeypatch.setattr(MarketHistoryStore, 'open', forbidden)
    selections = {'ticker': '601328.SH', 'analysis_date': '2026-10-16',
                  'research_depth': 1, 'shallow_thinker': 'deepseek-flash',
                  'deep_thinker': 'deepseek-v4-pro', 'backend_url': 'https://api.deepseek.com',
                  'llm_provider': 'DeepSeek', 'analysts': list(ANALYST_TOOLS)}
    with pytest.raises(BudgetRouteConfigurationError) as cli_error:
        cli._build_run_config(selections, False)
    with pytest.raises(BudgetRouteConfigurationError) as graph_error:
        graph.TradingAgentsGraph(config=config, instrument_symbol='601328.SH')
    assert cli_error.value.report == graph_error.value.report
    assert dict(cli_error.value.report.chains)['market_snapshot'] == ('tushare', 'baostock')
    assert not calls


def test_preview_matches_actual_merge_without_mutating():
    runtime.set_config(settings())
    runtime.set_config({'tool_vendors': {'get_news': 'baostock'}})
    before = runtime.get_config()
    partial = {'analysis_budget_enabled': True, 'data_vendors': {'macro_data': 'fred'}}
    predicted = runtime.preview_config(partial)
    assert runtime.get_config() == before
    runtime.set_config(partial)
    assert runtime.get_config() == predicted
    report = inspect_budget_routes(predicted, selected_analysts=('social',), symbol='601328.SH')
    assert any(i.location == 'social.get_news' and i.reason == 'provider_not_implemented'
               for i in report.issues)


@pytest.mark.parametrize('value', [None, 42, '', 'akshare,,baostock', 'secret-token-value'])
def test_malformed_chains_are_safe_diagnostics(value):
    config = settings()
    config['market_data_vendors']['cn_a']['core_stock_apis'] = value
    with pytest.raises(BudgetRouteConfigurationError) as error:
        validate_budget_routes(config, selected_analysts=('market',), symbol='601328.SH')
    assert 'secret-token-value' not in str(error.value)


def test_akshare_names_do_not_create_missing_implementations():
    config = settings()
    config['tool_vendors'].update(dict.fromkeys(ANALYST_TOOLS['news'], 'akshare'))
    report = inspect_budget_routes(config, selected_analysts=('news',), symbol='601328.SH')
    assert {i.location for i in report.issues} == {
        'news.get_global_news', 'news.get_insider_transactions',
        'news.get_macro_indicators', 'news.get_prediction_markets'}


def test_real_tool_nodes_match_inventory():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = settings()
    nodes = graph._create_tool_nodes()
    for analyst in ('market', 'social', 'news'):
        assert set(nodes[analyst].tools_by_name) == set(ANALYST_TOOLS[analyst])


def test_non_mainland_social_prefetch_is_reported():
    report = inspect_budget_routes(settings(), selected_analysts=('social',), symbol='AAPL')
    assert any(item.reason == 'non_mainland_sentiment_transport_unverified' for item in report.issues)


def test_run_boundary_checks_active_process_routes_not_a_predicted_merge(monkeypatch, tmp_path):
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = settings()
    graph.selected_analysts = ('market', 'social')
    runtime.set_config(settings())
    runtime.set_config({'tool_vendors': {'get_news': 'yfinance'},
                        'market_history_database_path': str(tmp_path / 'history.sqlite3')})
    with pytest.raises(BudgetRouteConfigurationError) as error:
        graph.resolve_evidence_state('601328.SH', '2026-10-16')
    assert any(i.location == 'social.get_news' and i.provider == 'yfinance'
               for i in error.value.report.issues)


def test_retained_financial_routes_are_checked_separately():
    active = settings()
    active['tool_vendors']['get_fundamentals'] = 'yfinance'
    report = inspect_budget_routes(active, selected_analysts=('fundamentals',),
                                   symbol='601328.SH', financial_config=settings())
    assert not report.issues
    retained = settings()
    retained['tool_vendors']['get_fundamentals'] = 'yfinance'
    report = inspect_budget_routes(settings(), selected_analysts=('fundamentals',),
                                   symbol='601328.SH', financial_config=retained)
    assert any(i.location == 'fundamentals.get_fundamentals' for i in report.issues)


@pytest.mark.parametrize('analyst', ['market', 'news', 'fundamentals'])
def test_actual_analyst_bindings_and_prompts_are_covered(analyst):
    from importlib import import_module

    from langchain_core.runnables import RunnableLambda
    module = import_module('tradingagents.agents.analysts.' + analyst + '_analyst')
    bound, prompts = [], []

    class Captured(BaseException):
        pass

    def capture(prompt):
        prompts.append(prompt.to_messages()[0].content)
        raise Captured()

    class LLM:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(capture)

        def bind_tools(self, tools, **kwargs):
            bound.extend(tool.name for tool in tools if hasattr(tool, 'name'))
            return RunnableLambda(capture)

    node = getattr(module, 'create_' + analyst + '_analyst')(LLM())
    with pytest.raises(Captured):
        node({'trade_date': '2026-10-16', 'company_of_interest': '601328.SH',
              'messages': [], 'asset_type': 'stock'})
    expected = set(ANALYST_TOOLS[analyst])
    if analyst == 'news':
        expected.remove('get_insider_transactions')  # executable legacy ToolNode entry
    assert set(bound) == expected
    assert all(name in prompts[0] for name in bound)


def test_disabled_config_preserves_legacy_routes_and_does_not_validate():
    config = settings()
    config['analysis_budget_enabled'] = False
    config['market_data_vendors'] = None
    assert validate_budget_routes(config, symbol='601328.SH').issues == ()


def test_terminal_guard_fallback_and_exact_budget_boundary(monkeypatch):
    from tradingagents.analysis_budget import (
        AnalysisBudget,
        AnalysisBudgetStopped,
        analysis_budget_scope,
    )
    from tradingagents.dataflows import interface
    config = settings()
    config['tool_vendors']['get_news'] = 'akshare,yfinance'
    runtime.set_config(config)
    calls = []

    def first(*args, **kwargs):
        calls.append('akshare')
        raise ValueError('synthetic failure')

    monkeypatch.setitem(interface.VENDOR_METHODS['get_news'], 'akshare', first)
    monkeypatch.setitem(interface.VENDOR_METHODS['get_news'], 'yfinance', lambda *a: calls.append('yfinance'))
    with (
        pytest.raises(AnalysisBudgetStopped, match='uncovered_data_vendor'),
        analysis_budget_scope(config) as budget,
    ):
        interface.route_to_vendor('get_news', '601328.SH', '2026-10-01', '2026-10-16')
    assert budget.receipt()['admitted_attempts'] == 0
    assert calls == ['akshare']
    clock = [0.0]
    budget = AnalysisBudget(30, 600, clock=lambda: clock[0])
    for _ in range(30):
        budget.admit('data')
    with pytest.raises(AnalysisBudgetStopped, match='call_limit_exceeded'):
        budget.admit('model')
    assert budget.receipt()['admitted_attempts'] == 30
    budget = AnalysisBudget(30, 600, clock=lambda: clock[0])
    clock[0] = 600
    with pytest.raises(AnalysisBudgetStopped, match='deadline_exceeded'):
        budget.admit('data')
    clock[0] = 0
    with pytest.raises(AnalysisBudgetStopped, match='deadline_exceeded'):
        budget.check()


def test_explicit_counted_news_choices_preserve_all_four_analysts():
    config = settings()
    config['tool_vendors'].update(get_global_news='alpha_vantage', get_insider_transactions='alpha_vantage')
    report = validate_budget_routes(config, symbol='601328.SH')
    assert not report.issues
    assert dict(report.chains)['news.get_macro_indicators'] == ('fred',)
    assert dict(report.chains)['news.get_prediction_markets'] == ('polymarket',)


def test_config_routes_and_context_failures_are_reported_together(tmp_path, monkeypatch):
    import cli.main as cli

    config = settings()
    config.update(analysis_max_calls=0, analysis_timeout_seconds=-1, checkpoint_enabled=True,
                  tool_vendors=None, market_history_database_path=str(tmp_path / 'absent.sqlite3'),
                  data_cache_dir=str(tmp_path / 'cache'))
    monkeypatch.setattr(cli, 'DEFAULT_CONFIG', config)
    with pytest.raises(BudgetRouteConfigurationError) as captured:
        cli._build_run_config({'ticker': '601328.SH', 'analysis_date': '2026-10-16',
            'research_depth': 1, 'shallow_thinker': 'deepseek-flash', 'deep_thinker': 'deepseek-v4-pro',
            'backend_url': 'https://api.deepseek.com', 'llm_provider': 'DeepSeek',
            'analysts': list(ANALYST_TOOLS)}, None)
    locations = {item.location for item in captured.value.report.issues}
    assert {'config.analysis_max_calls', 'config.analysis_timeout_seconds', 'config.checkpoint_enabled',
            'config.tool_vendors', 'news.get_global_news', 'news.get_insider_transactions'} <= locations
    assert any(item.location == 'calendar' for item in captured.value.readiness_report.issues)


def test_run_boundary_uses_retained_budget_settings_but_active_routes(tmp_path):
    from tradingagents.budget_readiness import BudgetReadinessError
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = settings()
    graph.selected_analysts = ('market',)
    active = settings()
    active.update(llm_provider='openai', analysis_max_calls=0,
                  market_history_database_path=str(tmp_path / 'missing.sqlite3'))
    runtime.set_config(active)
    with pytest.raises(BudgetReadinessError, match='calendar'):
        graph.resolve_evidence_state('601328.SH', '2026-10-16')
    # Global budget/client flags are irrelevant, but an uncovered active route
    # remains an error even though the graph's retained route was AKShare.
    runtime.set_config({'market_data_vendors': {'cn_a': {'core_stock_apis': 'baostock'}}})
    with pytest.raises(BudgetRouteConfigurationError) as captured:
        graph.resolve_evidence_state('601328.SH', '2026-10-16')
    assert any(item.provider == 'baostock' for item in captured.value.report.issues)
    assert not any(item.location.startswith('config.') for item in captured.value.report.issues)


def test_oversized_timeout_has_fixed_safe_diagnostic():
    from tradingagents.analysis_budget import AnalysisBudget, inspect_budget_config

    config = settings()
    config['analysis_timeout_seconds'] = 10 ** 400
    assert ('analysis_timeout_seconds', 'must_be_positive_finite') in inspect_budget_config(config)
    with pytest.raises(ValueError, match='positive and finite'):
        AnalysisBudget(30, 10 ** 400)


def test_credential_requirements_are_labels_without_reading_environment(tmp_path, monkeypatch):
    import os

    from tradingagents.budget_preflight import validate_budget_preflight
    from tradingagents.budget_readiness import BudgetReadinessError

    config = settings()
    config['tool_vendors'].update(get_global_news='alpha_vantage', get_insider_transactions='alpha_vantage')
    config.update(market_history_database_path=str(tmp_path / 'missing.sqlite3'),
                  data_cache_dir=str(tmp_path / 'cache'))

    class NoCredentialRead(dict):
        def get(self, key, default=None):
            assert key not in {'ALPHA_VANTAGE_API_KEY', 'FRED_API_KEY'}
            return super().get(key, default)

    monkeypatch.setattr(os, 'environ', NoCredentialRead())
    with pytest.raises(BudgetReadinessError) as captured:
        validate_budget_preflight(config, ticker='601328.SH', as_of_date='2026-10-16')
    assert {(item.location, item.status) for item in captured.value.report.checked
            if item.location.startswith('credential.')} == {
        ('credential.ALPHA_VANTAGE_API_KEY', 'presence_and_service_not_checked'),
        ('credential.FRED_API_KEY', 'presence_and_service_not_checked')}



def test_explicit_alpha_disable_env_and_preflight_require_compatible_routes(monkeypatch):
    from tradingagents import default_config as defaults
    from tradingagents.analysis_budget import inspect_budget_config

    monkeypatch.setenv('TRADINGAGENTS_ANALYSIS_DISABLE_ALPHA_VANTAGE', 'true')
    config = defaults._apply_env_overrides(settings())
    assert config['analysis_disable_alpha_vantage'] is True
    config['tool_vendors'].update(get_global_news='alpha_vantage', get_insider_transactions='alpha_vantage')
    assert not inspect_budget_routes(config, symbol='601328.SH').issues
    config['tool_vendors']['get_news'] = 'alpha_vantage,akshare'
    report = inspect_budget_routes(config, symbol='601328.SH')
    assert {item.location for item in report.issues if item.reason == 'disabled_provider_requires_explicit_alternative'} == {
        'social.get_news', 'news.get_news'}
    assert ('analysis_disable_alpha_vantage', 'requires_analysis_budget') in inspect_budget_config({
        'analysis_disable_alpha_vantage': True})
    assert ('analysis_disable_alpha_vantage', 'must_be_boolean') in inspect_budget_config({
        'analysis_disable_alpha_vantage': 'true'})
