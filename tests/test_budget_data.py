"""Real routed vendors and Requests, with only the wire replaced offline."""
import io
import json
from copy import deepcopy

import pytest
from urllib3.response import HTTPResponse

from tests.test_analysis_budget import config, install_network_denial
from tradingagents.analysis_budget import (
    AnalysisBudgetStopped,
    active_budget,
    analysis_budget_scope,
)
from tradingagents.dataflows import config as runtime
from tradingagents.dataflows.interface import route_to_vendor


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    install_network_denial(monkeypatch)
    monkeypatch.setattr(runtime, '_config', deepcopy(runtime.get_config()))
    monkeypatch.setenv('ALPHA_VANTAGE_API_KEY', 'offline-dummy')
    monkeypatch.setenv('FRED_API_KEY', 'offline-dummy')
    monkeypatch.setenv('NO_PROXY', '*')
    runtime.set_config({'tool_vendors': {
        'get_news': 'alpha_vantage', 'get_global_news': 'alpha_vantage',
        'get_insider_transactions': 'alpha_vantage',
        'get_macro_indicators': 'fred', 'get_prediction_markets': 'polymarket'}})


def fake_wire(monkeypatch, callback=None):
    from urllib3.connectionpool import HTTPConnectionPool
    sends = []

    def send(pool, method, url, **kwargs):
        sends.append((pool.host, method, url))
        if callback:
            value = callback(pool, method, url, len(sends))
            if value is not None:
                return value
        if pool.host == 'api.stlouisfed.org':
            value = {'observations': [{'date': '2026-10-01', 'value': '3'}]} if '/observations?' in url else {
                'seriess': [{'title': 'Synthetic CPI', 'units': 'Index', 'frequency': 'Monthly'}]}
        elif pool.host == 'gamma-api.polymarket.com':
            value = {'events': []}
        else:
            assert pool.host == 'www.alphavantage.co'
            value = {'feed': [], 'data': []}
        return HTTPResponse(body=io.BytesIO(json.dumps(value).encode()), status=200,
                            preload_content=False, headers={'content-type': 'application/json'})

    monkeypatch.setattr(HTTPConnectionPool, 'urlopen', send)
    return sends


def global_news():
    return route_to_vendor('get_global_news', '2026-10-16')


def test_all_new_tools_use_real_implementations_and_physical_counter(monkeypatch):
    sends = fake_wire(monkeypatch)
    with analysis_budget_scope(config(analysis_max_calls=5)) as budget:
        assert 'feed' in global_news()
        assert 'data' in route_to_vendor('get_insider_transactions', '601328.SH')
        assert 'Synthetic CPI' in route_to_vendor('get_macro_indicators', 'cpi', '2026-10-16')
        assert 'No open prediction markets' in route_to_vendor('get_prediction_markets', 'synthetic')
    assert budget.receipt()['data_attempts'] == len(sends) == 5
    assert budget.receipt()['model_attempts'] == 0


def test_multi_request_macro_stops_before_second_send(monkeypatch):
    sends = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match='call_limit_exceeded'), analysis_budget_scope(config(analysis_max_calls=1)) as budget:
        route_to_vendor('get_macro_indicators', 'cpi', '2026-10-16')
    assert len(sends) == budget.receipt()['data_attempts'] == 1


@pytest.mark.parametrize('host,expected', [('www.alphavantage.co', 2), ('outside.invalid', 1)])
def test_redirects_count_each_send_and_cannot_escape_destination(monkeypatch, host, expected):
    def redirect(pool, method, url, number):
        if number == 1:
            return HTTPResponse(body=io.BytesIO(b''), status=302, preload_content=False,
                                headers={'location': 'https://' + host + '/query?redirected=1'})
    sends = fake_wire(monkeypatch, redirect)
    if expected == 1:
        with pytest.raises(AnalysisBudgetStopped, match='uncovered_data_destination'), analysis_budget_scope(config()) as budget:
            global_news()
    else:
        with analysis_budget_scope(config()) as budget:
            global_news()
    assert budget.receipt()['data_attempts'] == len(sends) == expected


@pytest.mark.parametrize('stop', ['deadline', 'cancel'])
def test_late_result_and_cancellation_propagate_and_release_scope(monkeypatch, stop):
    def interrupt(pool, method, url, number):
        if stop == 'cancel':
            raise KeyboardInterrupt('synthetic cancellation')
        active_budget()._started -= 1000
    sends = fake_wire(monkeypatch, interrupt)
    error = KeyboardInterrupt if stop == 'cancel' else AnalysisBudgetStopped
    with pytest.raises(error), analysis_budget_scope(config()) as budget:
        global_news()
    assert budget.receipt()['data_attempts'] == len(sends) == 1
    assert active_budget() is None
    fake_wire(monkeypatch)
    with analysis_budget_scope(config()) as fresh:
        global_news()
    assert fresh.receipt()['data_attempts'] == 1


@pytest.mark.parametrize('allowed', [True, False])
def test_real_news_failure_fallback_and_terminal_stop(monkeypatch, allowed):
    runtime.set_config({'tool_vendors': {'get_news': 'alpha_vantage,akshare'}})
    def fail_alpha(pool, method, url, number):
        if pool.host == 'www.alphavantage.co':
            return HTTPResponse(body=io.BytesIO(b'{"Note":"rate limit"}'), status=200,
                                preload_content=False)
        assert pool.host == 'search-api-web.eastmoney.com'
        return HTTPResponse(body=io.BytesIO(('jQuery35101792940631092459_1764599530165(' + json.dumps({"result": {"cmsArticleWebOld": [{
            "date": "2026-10-14 12:00:00", "mediaName": "Synthetic", "code": "synthetic",
            "title": "Synthetic news", "content": "Synthetic", "image": ""}]}}) + ')').encode()),
                            status=200, preload_content=False)
    sends = fake_wire(monkeypatch, fail_alpha)
    if allowed:
        with analysis_budget_scope(config()) as budget:
            route_to_vendor('get_news', '601328.SH', '2026-10-01', '2026-10-16')
    else:
        with pytest.raises(AnalysisBudgetStopped, match='call_limit_exceeded'), analysis_budget_scope(config(analysis_max_calls=1)) as budget:
            route_to_vendor('get_news', '601328.SH', '2026-10-01', '2026-10-16')
    assert budget.receipt()['data_attempts'] == len(sends) == (2 if allowed else 1)


def test_unreviewed_implementation_does_not_inherit_vendor_authority(monkeypatch):
    from tradingagents.dataflows.interface import VENDOR_METHODS
    sends = fake_wire(monkeypatch)
    monkeypatch.setitem(VENDOR_METHODS['get_global_news'], 'alpha_vantage', lambda *a: 'unreviewed')
    with pytest.raises(AnalysisBudgetStopped, match='uncovered_data_vendor'), analysis_budget_scope(config()) as budget:
        global_news()
    assert not sends and budget.receipt()['data_attempts'] == 0


@pytest.mark.parametrize('enabled', [False, True])
def test_global_news_tool_omitted_parameters_use_configured_defaults(monkeypatch, enabled):
    from urllib.parse import parse_qs, urlsplit

    from tradingagents.agents.utils.news_data_tools import get_global_news, get_insider_transactions
    runtime.set_config({'global_news_lookback_days': 3, 'global_news_article_limit': 11})
    sends = fake_wire(monkeypatch)
    with analysis_budget_scope(config(analysis_budget_enabled=enabled)):
        get_global_news.invoke({'curr_date': '2026-10-16'})
        get_insider_transactions.invoke({'ticker': '601328.SH'})
    query = parse_qs(urlsplit(sends[0][2]).query)
    assert query['time_from'] == ['20261013T0000']
    assert query['limit'] == ['11']
    assert len(sends) == 2


@pytest.mark.parametrize('excess', ['data', 'model'])
def test_thirtieth_physical_send_succeeds_and_thirty_first_never_sends(monkeypatch, excess):
    from tests.test_analysis_budget import fake_httpx, model_client

    models, data = fake_httpx(monkeypatch), fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match='call_limit_exceeded'), analysis_budget_scope(
        config(analysis_max_calls=30)
    ) as budget, model_client() as client:
        for _ in range(15):
            assert 'feed' in global_news()
            assert client.post('https://fake.invalid/model').status_code == 200
        assert (len(models), len(data)) == (15, 15)
        assert budget.receipt()['admitted_attempts'] == 30
        if excess == 'data':
            global_news()
        else:
            client.post('https://fake.invalid/excess')
    assert (len(models), len(data)) == (15, 15)
    assert budget.receipt()['admitted_attempts'] == 30
    assert budget.receipt()['stop_reason'] == 'call_limit_exceeded'
    assert active_budget() is None


def invoke_news_node(name):
    from tradingagents.agents.utils.news_data_tools import get_global_news, get_insider_transactions

    tools = {tool.name: tool for tool in (get_global_news, get_insider_transactions)}
    args = {'ticker': '601328.SH'} if name == 'get_insider_transactions' else {'curr_date': '2026-10-16'}
    return invoke_tool_graph(tools[name], args)


def invoke_tool_graph(tool, args):
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    graph = StateGraph(MessagesState)
    graph.add_node('tools', ToolNode([tool]))
    graph.add_edge(START, 'tools')
    graph.add_edge('tools', END)
    result = graph.compile().invoke({'messages': [AIMessage(content='', tool_calls=[{
        'name': tool.name, 'args': args, 'id': 'synthetic-tool-call', 'type': 'tool_call',
    }])]})
    return result['messages'][-1].content


@pytest.mark.parametrize('name', ['get_global_news', 'get_insider_transactions'])
@pytest.mark.parametrize('reason', ['not_configured', 'user_disabled'])
def test_alpha_gap_through_real_toolnode_never_sends_or_falls_back(monkeypatch, name, reason):
    from tradingagents.dataflows import alpha_vantage_common as av
    from tradingagents.dataflows.interface import VENDOR_METHODS
    from tradingagents.reporting import budget_data_gap_markdown

    sends = fake_wire(monkeypatch)
    # The surrounding autouse fixture supplies only a fixed synthetic key.
    if reason == 'not_configured':
        monkeypatch.delenv('ALPHA_VANTAGE_API_KEY')
    else:
        def forbidden_key_read():
            pytest.fail('user-disabled Alpha must not inspect even a synthetic key')
        monkeypatch.setattr(av, 'get_api_key', forbidden_key_read)
    monkeypatch.setitem(VENDOR_METHODS[name], 'yfinance', lambda *a, **k: pytest.fail('cross-provider fallback'))
    runtime.set_config({'tool_vendors': {name: 'alpha_vantage,yfinance'}})
    with analysis_budget_scope(config(analysis_disable_alpha_vantage=reason == 'user_disabled')) as budget:
        content = invoke_news_node(name)
        assert 'DATA_UNAVAILABLE' in content and f'reason={reason}' in content
        # Repeated model requests still cannot spend or duplicate receipt gaps.
        assert invoke_news_node(name) == content
    assert sends == []
    receipt = budget.receipt()
    assert receipt['admitted_attempts'] == receipt['data_attempts'] == receipt['model_attempts'] == 0
    assert receipt['data_gaps'] == [{'tool': name, 'provider': 'alpha_vantage', 'reason': reason}]
    for language in ('English', 'Chinese'):
        rendered = budget_data_gap_markdown({'analysis_budget': receipt}, language)
        assert name in rendered and reason in rendered and 'DATA_UNAVAILABLE' in rendered
    assert active_budget() is None


@pytest.mark.parametrize('name', ['get_global_news', 'get_insider_transactions'])
def test_budget_off_alpha_missing_still_raises_existing_error(monkeypatch, name):
    from tradingagents.dataflows.alpha_vantage_common import AlphaVantageNotConfiguredError

    sends = fake_wire(monkeypatch)
    monkeypatch.delenv('ALPHA_VANTAGE_API_KEY')
    with pytest.raises(AlphaVantageNotConfiguredError), analysis_budget_scope(config(analysis_budget_enabled=False)):
        invoke_news_node(name)
    assert sends == [] and active_budget() is None


@pytest.mark.parametrize('enabled', [False, True])
def test_synthetic_missing_fred_key_remains_optional_zero_send(monkeypatch, enabled):
    from tradingagents.agents.utils.agent_utils import get_macro_indicators

    monkeypatch.delenv('FRED_API_KEY')
    sends = fake_wire(monkeypatch)
    with analysis_budget_scope(config(analysis_budget_enabled=enabled)) as budget:
        content = invoke_tool_graph(get_macro_indicators, {'indicator': 'cpi', 'curr_date': '2026-10-16'})
        assert 'DATA_UNAVAILABLE' in content
        assert 'macro_data' in content
    assert not sends
    if enabled:
        assert budget.receipt()['admitted_attempts'] == 0


@pytest.mark.parametrize('notice,reason', [
    ('Our API rate limit is 25 requests per day; upgrade to premium.', 'rate_limited'),
    ('This is a premium endpoint requiring a subscription.', 'permission_denied'),
    ('The parameter apikey is invalid or missing.', 'authentication_failed'),
])
def test_remote_alpha_rejections_are_not_missing_keys_or_empty_news(monkeypatch, notice, reason):
    from tradingagents.dataflows.interface import VENDOR_METHODS

    def reject(pool, method, url, number):
        return HTTPResponse(body=io.BytesIO(json.dumps({'Information': notice}).encode()),
                            status=200, preload_content=False)
    sends = fake_wire(monkeypatch, reject)
    monkeypatch.setitem(VENDOR_METHODS['get_global_news'], 'yfinance', lambda *a, **k: pytest.fail('fallback'))
    runtime.set_config({'tool_vendors': {'get_global_news': 'alpha_vantage,yfinance'}})
    with analysis_budget_scope(config()) as budget:
        content = invoke_news_node('get_global_news')
    assert f'reason={reason}' in content and 'reason=not_configured' not in content
    assert 'DATA_UNAVAILABLE' in content
    assert budget.receipt()['data_attempts'] == len(sends) == 1


def test_explicit_disable_rejects_other_alpha_capabilities_before_send(monkeypatch):
    sends = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match='disabled_data_vendor'), analysis_budget_scope(
        config(analysis_disable_alpha_vantage=True)
    ) as budget:
        route_to_vendor('get_news', '601328.SH', '2026-10-01', '2026-10-16')
    assert not sends and budget.receipt()['admitted_attempts'] == 0



@pytest.mark.parametrize('status,reason', [(401, 'authentication_failed'), (403, 'permission_denied'), (429, 'rate_limited')])
def test_http_alpha_rejection_has_accurate_gap_and_one_count(monkeypatch, status, reason):
    def reject(pool, method, url, number):
        return HTTPResponse(body=io.BytesIO(b'synthetic remote denial'), status=status, preload_content=False)
    sends = fake_wire(monkeypatch, reject)
    with analysis_budget_scope(config()) as budget:
        content = invoke_news_node('get_insider_transactions')
    assert f'reason={reason}' in content
    assert budget.receipt()['data_attempts'] == len(sends) == 1
