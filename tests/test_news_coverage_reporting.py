"""News coverage through the real tool/route and report composition, entirely offline."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tests.test_analysis_budget import config, install_network_denial
from tests.test_news_observed_envelope import envelope, wire
from tests.test_reporting import _outcome_state
from tradingagents import reporting
from tradingagents.agents.utils.news_data_tools import get_news
from tradingagents.analysis_budget import analysis_budget_scope
from tradingagents.dataflows import akshare_data, config as runtime
from tradingagents.evidence import (
    EvidenceState,
    SourceAcquisitionUnavailable,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
)
from tradingagents.report_presentation import evidence_diagnostic_markdown
from tradingagents.terminal_contract import apply_terminal_contract


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    install_network_denial(monkeypatch)
    monkeypatch.setattr(runtime, '_config', deepcopy(runtime.get_config()))
    runtime.set_config({'tool_vendors': {'get_news': 'akshare'}})


def acquire(monkeypatch, scenario, enabled):
    calls, closed = [], []
    payload = envelope()
    if scenario == 'outside_window':
        payload['result']['cmsArticleWebOld'][0]['date'] = '2026-11-08 00:00:00'
    elif scenario == 'wrong_stock':
        payload['result']['cmsArticleWebOld'][0]['title'] = '600895 synthetic news'
    elif scenario == 'unknown_status':
        payload['msg'] = 'UNKNOWN'

    def get(url, *, params, allow_redirects, timeout, **kwargs):
        assert allow_redirects is False and timeout == 15
        calls.append(url)
        if scenario == 'timeout':
            raise TimeoutError('SYNTHETIC-PRIVATE-ERROR')
        body = wire(payload, params['cb'])
        return SimpleNamespace(status_code=200, content=body, text=body.decode(), headers={},
                               close=lambda: closed.append(True))

    monkeypatch.setattr(akshare_data.requests, 'get', get)
    graph = StateGraph(MessagesState)
    graph.add_node('tools', ToolNode([get_news]))
    graph.add_edge(START, 'tools')
    graph.add_edge('tools', END)
    call = {'name': 'get_news', 'args': {'ticker': '600919.SH', 'start_date': '2026-10-31',
                                       'end_date': '2026-11-07'},
            'id': 'synthetic-news-call', 'type': 'tool_call'}
    with analysis_budget_scope(config(analysis_budget_enabled=enabled)) as budget:
        result = graph.compile().invoke({'messages': [AIMessage(content='', tool_calls=[call])]})
    message = result['messages'][-1]
    bound = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
    evidence = build_tool_evidence_state(
        [message], [], tool_call_ids_by_source={bound.source_ref: (call['id'],)},
        tool_calls_by_id={call['id']: call}, include_catalog_sources=True,
    )
    assert len(calls) == 1
    assert len(closed) == (0 if scenario == 'timeout' else 1)
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.source_facts == ()
    assert 'SYNTHETIC-PRIVATE-ERROR' not in message.content
    return message, bound, evidence, budget.receipt() if budget else None


def compose_report(monkeypatch, evidence, receipt, language, tmp_path):
    """Real document assembly, with audit persistence and final disk publication replaced.

    No native ReportStore handles are opened; this is not a native write test.
    """
    state = _outcome_state()
    state.update(company_of_interest='600919.SH', trade_date='2026-11-07',
                 evidence_state=evidence.model_dump(mode='json'))
    if receipt is not None:
        state['analysis_budget'] = receipt
    outcome_before = deepcopy(state['analysis_outcome_contract'])

    def fake_audit(final_state, directory, *, publication):
        apply_terminal_contract(final_state, audit_digest='a' * 64)
        final_state['decision_audit_created_at'] = '2026-11-14T00:00:00Z'

    monkeypatch.setattr(reporting, 'write_immutable_decision_audit', fake_audit)
    monkeypatch.setattr(reporting, 'publish_report_export',
                        lambda publication, identity, language, documents, stages: documents)
    documents = reporting._write_report_tree(
        state, '600919.SH', SimpleNamespace(root=tmp_path), language)
    assert state['analysis_outcome_contract'] == outcome_before
    assert EvidenceState.model_validate(state['evidence_state']) == evidence
    return documents


@pytest.mark.parametrize('enabled', [False, True], ids=['budget-off', 'budget-on'])
@pytest.mark.parametrize('language', ['English', 'Chinese'])
@pytest.mark.parametrize('scenario,code,phrase', [
    ('success', 'news_window_coverage_unknown', ('full historical coverage', '完整历史覆盖')),
    ('outside_window', 'news_page_no_eligible_articles', ('this response page', '本响应页')),
    ('wrong_stock', 'news_page_no_eligible_articles', ('this response page', '本响应页')),
    ('timeout', 'news_unavailable', ('unavailable', '不可用')),
    ('unknown_status', 'news_unavailable', ('unavailable', '不可用')),
])
def test_news_outcomes_reach_tool_and_complete_report(
    monkeypatch, tmp_path, enabled, language, scenario, code, phrase,
):
    message, bound, evidence, receipt = acquire(monkeypatch, scenario, enabled)
    assert code in message.content
    if scenario == 'success':
        assert bound.selected_artifact is not None
        assert bound.selected_artifact.raw_text == message.content
        assert evidence.sources[0].status.value == 'available'
        assert 'DATA_UNAVAILABLE' not in message.content
    else:
        assert bound.selected_artifact is None
        assert not evidence.source_artifacts
        assert evidence.sources[0].status.value == 'unavailable'
        if scenario in {'outside_window', 'wrong_stock'}:
            assert bound.acquisition_outcomes[0].error_code == 'news_no_usable_articles'
            assert bound.acquisition_outcomes[0].reason.value == 'no_data'
        else:
            assert 'news_page_no_eligible_articles' not in message.content
    documents = compose_report(monkeypatch, evidence, receipt, language, tmp_path)
    for name in ['complete_report.md', '5_portfolio/analysis_outcome.md']:
        assert code in documents[name]
        assert phrase[int(language == 'Chinese')] in documents[name]
        assert ('不是来源事实' if language == 'Chinese' else 'not source facts') in documents[name]
        assert 'SYNTHETIC-PRIVATE-ERROR' not in documents[name]
        if scenario == 'success':
            assert 'DATA_UNAVAILABLE' not in documents[name]
    if enabled and scenario == 'success':
        assert 'DATA_GAP: `get_news.coverage`' in documents['complete_report.md']
        assert receipt['data_gaps'] == [{'tool': 'get_news.coverage', 'provider': 'akshare',
                                         'reason': 'news_window_coverage_unknown'}]


@pytest.mark.parametrize('provider,capability,reason,error_code,expected', [
    ('akshare', 'get_news', 'no_data', 'news_no_usable_articles', 'news_page_no_eligible_articles'),
    ('akshare', 'get_news', 'no_data', None, 'news_unavailable'),
    ('akshare', 'get_news', 'provider_error', 'news_no_usable_articles', 'news_unavailable'),
    ('akshare', 'get_news', 'no_data', 'private_unknown_code', 'news_unavailable'),
    ('yfinance', 'get_news', 'no_data', 'news_no_usable_articles', None),
    ('akshare', 'get_global_news', 'no_data', 'news_no_usable_articles', None),
])
def test_presentation_requires_exact_typed_reason_not_arbitrary_error_text(
    provider, capability, reason, error_code, expected,
):
    outcome = SourceAcquisitionUnavailable(
        provider=provider, capability=capability, source_ref='private_source_ref', attempt=1,
        retrieved_at='2026-11-14T00:00:00Z', retryable=False, reason=reason, error_code=error_code,
    )
    evidence = EvidenceState(acquisition_outcomes=(outcome,))
    for language in ['English', 'Chinese']:
        text = evidence_diagnostic_markdown(evidence, language)
        assert 'private_' not in text
        if expected:
            assert expected in text
            if expected == 'news_unavailable':
                assert 'news_page_no_eligible_articles' not in text
        else:
            assert text == ''


def test_old_evidence_without_acquisition_metadata_does_not_invent_coverage():
    assert evidence_diagnostic_markdown(EvidenceState(), 'Chinese') == ''


@pytest.mark.parametrize('language', ['English', 'Chinese'])
def test_failed_attempt_does_not_hide_later_success_or_change_availability(monkeypatch, language):
    _, bound, evidence, _ = acquire(monkeypatch, 'success', False)
    failed = SourceAcquisitionUnavailable(
        provider='akshare', capability='get_news', source_ref=bound.source_ref,
        acquisition_id='earlier-synthetic-call', attempt=1,
        retrieved_at='2026-11-14T00:00:00Z', retryable=False,
        reason='no_data', error_code='news_no_usable_articles',
    )
    # This is retained synthetic history, not another acquisition/retry.
    combined = EvidenceState.model_validate({
        **evidence.model_dump(mode='python'),
        'acquisition_outcomes': (failed, *evidence.acquisition_outcomes),
    })
    before = combined.model_dump(mode='json')
    text = evidence_diagnostic_markdown(combined, language)
    assert text.count('news_page_no_eligible_articles') == 1
    assert text.count('news_window_coverage_unknown') == 1
    assert ('this response page' if language == 'English' else '本响应页') in text
    assert 'DATA_UNAVAILABLE' not in text
    assert combined.sources[0].status.value == 'available'
    assert combined.model_dump(mode='json') == before


def test_existing_budget_unavailability_and_unknown_gap_filter_are_preserved():
    state = {'analysis_budget': {'data_gaps': [
        {'tool': 'get_news', 'provider': 'akshare', 'reason': 'news_unavailable'},
        {'tool': 'get_news.coverage', 'provider': 'private', 'reason': 'private'},
    ]}}
    text = reporting.budget_data_gap_markdown(state)
    assert 'DATA_UNAVAILABLE: `get_news`' in text
    assert 'private' not in text
