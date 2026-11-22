"""Synthetic window semantics and real ToolNode dispatch; no live transports."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langgraph.prebuilt import ToolNode

from tradingagents import analysis_budget as b, moneyflow as mf, powershell_news as p
from tradingagents.agents.utils.news_data_tools import get_news
from tests.test_powershell_news import owned, body
from tests.test_bounded_moneyflow import state, response, observed, budget


def test_wider_window_does_not_claim_missing_context(tmp_path, monkeypatch):
    source = owned(tmp_path)
    account = b.AnalysisBudget(1, 60, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: account)
    send = Mock(return_value=(body(source.request), {'status': 'ok'}))
    monkeypatch.setattr(p, 'run_child', send)
    with p.powershell_news_scope(source):
        args = dict(ticker=source.request.ticker, end_date=source.request.end,
                    tool_call_id='synthetic-wide', state={})
        text, artifact = get_news.func(start_date='2026-10-23', **args)
        assert artifact['acquisition_outcomes'][0]['error_code'] == 'powershell_news_query_window_mismatch'
        assert '2026-11-06' in text and 'context_missing' not in text
        assert send.call_count == 0
        text, artifact = get_news.func(start_date=source.request.start, **args)
        assert artifact['selected_artifact'] is not None
    assert send.call_count == account._counts['data'] == 1


def test_each_flow_total_has_its_own_window(monkeypatch, budget):
    _, obs = observed(monkeypatch, budget, response([
        ['600895.SH', '20261020', 100], ['600895.SH', '20261106', 2],
        ['600895.SH', '20261113', 3]]))
    current = {**state(), 'moneyflow_observation': obs}
    before = copy.deepcopy(current)
    text = mf.moneyflow_markdown(current)
    assert '| 全采集窗口 | 2026-10-15 → 2026-11-13 | 2026-10-20 → 2026-11-13 | 3 | 105 |' in text
    assert '| 情绪回看窗口 | 2026-11-06 → 2026-11-13 | 2026-11-06 → 2026-11-13 | 2 | 5 |' in text
    assert '万元人民币' in text and obs['normalized_sha256'] in text
    assert current == before


def test_real_toolnode_first_concurrent_and_repeated_access(tmp_path, monkeypatch):
    from tradingagents.dataflows import config
    from tradingagents.agents.utils.agent_states import AgentState
    from langgraph.graph import StateGraph, START, END
    source = owned(tmp_path)
    account = b.AnalysisBudget(1, 60, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: account)
    monkeypatch.setitem(config._config, 'bounded_powershell_news_enabled', True)
    send = Mock(return_value=(body(source.request), {'status': 'ok'}))
    monkeypatch.setattr(p, 'run_child', send)
    graph = StateGraph(AgentState)
    graph.add_node('news_tools', ToolNode([get_news], handle_tool_errors=False))
    graph.add_edge(START, 'news_tools'); graph.add_edge('news_tools', END)
    tool = graph.compile()
    def dispatch():
        calls = [dict(name='get_news', id='synthetic-' + str(i), args=dict(
            ticker='600919.SS', start_date=source.request.start, end_date=source.request.end)) for i in range(2)]
        return tool.invoke({**state('600919.SS'), 'messages': [AIMessage(content='', tool_calls=calls)]})['messages'][-2:]
    with p.powershell_news_scope(source):
        for _ in range(2):
            messages = dispatch()
            assert all(m.artifact['selected_artifact'] is not None for m in messages)
            assert all(m.artifact['acquisition_outcomes'][0]['provider'] == 'eastmoney_powershell' for m in messages)
    missing = dispatch()
    assert all(m.artifact['acquisition_outcomes'][0]['error_code'] == 'powershell_news_context_missing' for m in missing)
    assert p.active_powershell_news() is None
    assert send.call_count == account._counts['data'] == 1


@pytest.mark.parametrize('case,code', [
    ('wrong_query_stock', 'query_binding_mismatch'), ('wrong_identity', 'query_binding_mismatch'),
    ('future', 'query_window_mismatch'), ('reversed', 'request_invalid'), ('bad_date', 'request_invalid')])
def test_tool_rejects_invalid_query_without_substituting_identity(tmp_path, monkeypatch, case, code):
    source = owned(tmp_path)
    account = b.AnalysisBudget(1, 60, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: account)
    send = Mock(side_effect=AssertionError('unexpected send'))
    monkeypatch.setattr(p, 'run_child', send)
    args = dict(ticker='600919.SS', start_date=source.request.start, end_date=source.request.end,
                tool_call_id='synthetic-invalid', state=state('600919.SS'))
    if case == 'wrong_query_stock': args['ticker'] = '000001.SZ'
    if case == 'wrong_identity': args['state'] = state('000001.SZ')
    if case == 'future': args['end_date'] = '2026-11-14'
    if case == 'reversed': args['start_date'] = '2026-11-14'
    if case == 'bad_date': args['start_date'] = 'SECRET_SENTINEL'
    with p.powershell_news_scope(source):
        text, envelope = get_news.func(**args)
    assert envelope['selected_artifact'] is None
    assert envelope['acquisition_outcomes'][0]['error_code'] == 'powershell_news_' + code
    assert 'SECRET_SENTINEL' not in text
    send.assert_not_called()
    assert account._counts['data'] == 0


def test_news_prompt_has_bound_window_and_scope_does_not_leak(tmp_path, monkeypatch):
    from langchain_core.runnables import RunnableLambda
    from tradingagents.agents.analysts.news_analyst import create_news_analyst
    source = owned(tmp_path)
    captured = []
    def fake(value):
        captured.append(value.to_messages()[0].content)
        return AIMessage(content='', tool_calls=[dict(name='get_news', id='synthetic-news', args={
            'ticker': '600919.SS', 'start_date': '2026-11-06', 'end_date': '2026-11-13'})])
    llm = Mock(); llm.bind_tools.return_value = RunnableLambda(fake)
    node = create_news_analyst(llm)
    value = {**state('600919.SS'), 'messages': []}
    before = copy.deepcopy(value)
    with p.powershell_news_scope(source):
        node(value)
        assert '2026-11-06 至 2026-11-13' in captured[-1]
        assert '不得替换' in p.powershell_news_window_instruction('000001.SZ', source.request.end)
    assert p.powershell_news_window_instruction('600919.SS', source.request.end) == ''
    assert value == before


@pytest.mark.parametrize('recent,expected', [([], '— | 0 | 不可用'),
    ([['600895.SH', '20261113', 0]], '2026-11-13 → 2026-11-13 | 1 | 0'),
    ([['600895.SH', '20261113', -3]], '2026-11-13 → 2026-11-13 | 1 | -3')])
def test_missing_window_is_not_zero_or_borrowed_total(monkeypatch, budget, recent, expected):
    _, obs = observed(monkeypatch, budget, response([['600895.SH', '20261020', 100]] + recent))
    text = mf.moneyflow_markdown({**state(), 'moneyflow_observation': obs})
    line = next(x for x in text.splitlines() if x.startswith('| 情绪回看窗口'))
    assert expected in line and '100' not in line


@pytest.mark.parametrize('case', ['digest', 'stock', 'unit', 'future', 'duplicate', 'nonfinite'])
def test_aggregate_never_promotes_invalid_observation(monkeypatch, budget, case):
    _, obs = observed(monkeypatch, budget)
    current = {**state(), 'moneyflow_observation': copy.deepcopy(obs)}
    value = current['moneyflow_observation']
    if case == 'stock': current['company_of_interest'] = '000001.SZ'
    if case == 'unit': value['unit'] = 'SECRET_SENTINEL'
    if case == 'future': value['rows'][-1][0] = '2026-11-14'
    if case == 'duplicate': value['rows'].append(value['rows'][-1][:])
    if case == 'nonfinite': value['rows'][-1][1] = 'NaN'
    if case != 'digest':
        # Prove date/value guards still matter even with a self-consistent digest.
        binding = mf._binding(state(), '600895.SH', '2026-11-13')
        value['projected_data_sha256'] = mf._projection_digest('moneyflow', binding, value['rows'])
        value['normalized_sha256'] = mf._digest({k: v for k, v in value.items() if k != 'normalized_sha256'})
    else: value['normalized_sha256'] = '0' * 64
    text = mf.moneyflow_markdown(current, include_rows=False)
    assert text == '资金流增强：材料绑定无效，未采用。'


@pytest.mark.parametrize('language', ['Chinese', 'English'])
def test_downstream_nodes_receive_same_table_not_prior_prose_totals(monkeypatch, budget, language):
    from tradingagents.dataflows import config
    from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
    from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
    monkeypatch.setitem(config._config, 'output_language', language)
    _, obs = observed(monkeypatch, budget, response([
        ['600895.SH', '20261020', 100], ['600895.SH', '20261113', 5]]))
    value = {**state(), 'moneyflow_observation': obs, 'market_report': '', 'news_report': '',
             'fundamentals_report': '', 'sentiment_report': '合成错误声称：短窗口合计 105',
             'investment_debate_state': {'count': 0}, 'risk_debate_state': {'count': 0},
             'trader_investment_plan': '辅助意见'}
    before = copy.deepcopy(value)
    expected = mf.moneyflow_markdown(value, language=language, include_rows=False)
    assert '2026-11-13 → 2026-11-13 | 1 | 5 |' in expected
    assert '| Date |' not in expected
    llm = Mock(); llm.invoke.return_value = SimpleNamespace(content='合成辅助回应')
    for factory in (create_bull_researcher, create_neutral_debator):
        factory(llm)(value)
        assert expected in llm.invoke.call_args.args[0]
        assert value['sentiment_report'] in llm.invoke.call_args.args[0]
    assert value == before


@pytest.mark.parametrize('language', ['Chinese', 'English'])
def test_report_labels_totals_unverified_without_rewriting_model_text(language):
    from tradingagents.report_presentation import advisory_markdown
    body = '合成错误声称：短窗口合计 105'
    plain = advisory_markdown('synthetic', body, language)
    marked = advisory_markdown('synthetic', body, language, flow_windows_unverified=True)
    assert plain.endswith(body + '\n') and marked.endswith(body + '\n')
    note = '窗口与合计未逐句核验' if language == 'Chinese' else 'not checked claim-by-claim'
    assert note in marked and note not in plain


@pytest.mark.parametrize('language', ['Chinese', 'English'])
def test_full_report_keeps_rule_decision_and_unverified_prose_separate(monkeypatch, budget, language):
    from datetime import datetime, timezone
    from tests.test_multiyear_integration import combined_state
    from tests.test_supplemental_presentation import MemoryStore, export
    from tradingagents import reporting
    from tradingagents.terminal_contract import apply_terminal_contract
    value, _ = combined_state()
    def collect(config, binding):
        rows = [[binding['start'], '100'], [binding['as_of'], '5']]
        return rows, mf._projection_digest('moneyflow', binding, rows), datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mf, '_coordinated', collect)
    value['moneyflow_observation'] = mf._Run({}, value['company_of_interest'], value['trade_date']).acquire(value)
    content = '合成错误声称：短窗口合计 105'
    value['investment_debate_state'] = {'bull_history': content}
    def audit(s, *args, **kwargs):
        apply_terminal_contract(s, audit_digest='b' * 64)
        s['decision_audit_created_at'] = '2026-07-18T00:00:00Z'
    audit(value)
    monkeypatch.setattr(reporting, 'ReportStore', MemoryStore)
    monkeypatch.setattr(reporting, 'write_immutable_decision_audit', audit)
    before = copy.deepcopy(value)
    enhanced = export(copy.deepcopy(value), None, language)
    plain = copy.deepcopy(value); plain.pop('moneyflow_observation')
    baseline = export(plain, None, language)
    text = enhanced['complete_report.md'].decode()
    note = '窗口与合计未逐句核验' if language == 'Chinese' else 'not checked claim-by-claim'
    assert note in enhanced['2_research/bull.md'].decode() and note in text
    assert content in text and '| 1 | 5 |' in text and '| 2 | 105 |' in text
    assert enhanced['5_portfolio/decision.md'] == baseline['5_portfolio/decision.md']
    assert value == before
