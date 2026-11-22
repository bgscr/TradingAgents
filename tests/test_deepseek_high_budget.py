"""Explicit Flash high reasoning and output cap through actual HTTP serialization."""
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from langchain_core.messages import HumanMessage

from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.analysis_budget import AnalysisBudgetStopped, analysis_budget_scope
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.evidence import EvidenceState
from tradingagents.llm_clients.openai_client import OpenAIClient


def response(content='Synthetic draft', *, finish='stop', completion=71, reasoning=0):
    return {'id': 'synthetic', 'object': 'chat.completion', 'created': 0,
        'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': finish,
            'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 120, 'completion_tokens': completion,
            'total_tokens': 120 + completion,
            'completion_tokens_details': {'reasoning_tokens': reasoning}}}


def complete():
    return response(json.dumps({'report_markdown': 'Synthetic data only; no investment decision.',
                                'material_claims': []}))


def state():
    return {'company_of_interest': '601328.SS', 'trade_date': '2026-10-16',
        'asset_type': 'stock', 'messages': [HumanMessage(content='Synthetic test; retain gaps.')],
        'evidence_state': EvidenceState().model_dump(mode='json')}


@contextmanager
def client(monkeypatch, replies, **overrides):
    calls = []

    def wire(transport, request):
        calls.append(json.loads(request.content))
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert len(calls) <= len(replies), 'unexpected physical send'
        reply = replies[len(calls)-1]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', wire)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'public-synthetic-test-placeholder')
    sync = httpx.Client(trust_env=False, follow_redirects=False)
    async_client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    settings = {'model': 'deepseek-flash', 'provider': 'deepseek',
        'base_url': 'https://api.deepseek.com', 'api_key': 'public-synthetic-test-placeholder',
        'max_retries': 0, 'max_tokens': 65536, 'reasoning_effort': 'high',
        'http_client': sync, 'http_async_client': async_client}
    settings.update(overrides)
    llm = None
    try:
        llm = OpenAIClient(**settings).get_llm()
        yield llm, calls
    finally:
        sync.close()
        asyncio.run(async_client.aclose())


@contextmanager
def budget(max_calls=3, cap=65536):
    config = {'llm_provider': 'deepseek', 'analysis_budget_enabled': True,
        'analysis_max_calls': max_calls, 'analysis_timeout_seconds': 600,
        'llm_max_output_tokens': cap, 'checkpoint_enabled': False,
        'market_history_mode': 'current', 'mainland_capability_routing_mode': 'legacy'}
    with authoritative_snapshot_run(), analysis_budget_scope(config) as counter:
        yield counter


def test_explicit_high_and_65536_reach_initial_and_json_wire(monkeypatch):
    with client(monkeypatch, [response(), response('{"bad":true}'), complete()]) as (llm, calls):
        with budget() as counter:
            result = create_market_analyst(llm)(state())
            assert counter.receipt()['model_attempts'] == 3
            assert counter.receipt()['data_attempts'] == 0
        assert len(calls) == 3
        assert all(body['max_tokens'] == 65536 and body['reasoning_effort'] == 'high' for body in calls)
        assert all('thinking' not in body for body in calls)
        assert 'tools' in calls[0]
        assert all(body['response_format'] == {'type': 'json_object'} for body in calls[1:])
        assert llm.max_retries == llm.root_client.max_retries == 0
        assert llm.max_tokens == 65536
        assert result['market_report'] == 'Synthetic data only; no investment decision.'


@pytest.mark.parametrize('cap', [8192, 65536])
def test_cap_is_explicit_and_not_a_mode_switch(monkeypatch, cap):
    with client(monkeypatch, [response(), complete()], max_tokens=cap) as (llm, calls):
        with budget(cap=cap):
            create_market_analyst(llm)(state())
        assert all(body['max_tokens'] == cap and body['reasoning_effort'] == 'high' for body in calls)


def test_unspecified_effort_preserves_provider_default(monkeypatch):
    with client(monkeypatch, [response(), complete()], reasoning_effort=None, max_tokens=8192) as (llm, calls):
        with budget(cap=8192):
            create_market_analyst(llm)(state())
        assert all('reasoning_effort' not in body and 'thinking' not in body for body in calls)
        assert all(body['max_tokens'] == 8192 for body in calls)


@pytest.mark.parametrize('effort', ['medium', 'HIGH', '', True, 42, []])
def test_invalid_explicit_effort_is_not_silently_dropped(monkeypatch, effort):
    with pytest.raises(ValueError, match='Invalid DeepSeek Flash reasoning_effort'), client(
            monkeypatch, [], reasoning_effort=effort):
        pytest.fail('invalid client was constructed')


@pytest.mark.parametrize('stage', ['initial_response', 'finalizer'])
def test_length_still_stops_at_new_cap_and_preserves_actual_usage(monkeypatch, stage):
    replies = [] if stage == 'initial_response' else [response()]
    replies.append(response('PARTIAL_NOT_REPORT', finish='length', completion=65533, reasoning=65000))
    with client(monkeypatch, replies) as (llm, calls):
        with budget() as counter:
            result = create_market_analyst(llm)(state())
            assert counter.receipt()['model_attempts'] == len(replies)
        diagnostic = result['messages'][-1].response_metadata['analyst_submission_diagnostic']
        assert diagnostic['stage'] == stage and diagnostic['finish_reason'] == 'length'
        assert diagnostic['completion_tokens'] == 65533 and diagnostic['reasoning_tokens'] == 65000
        assert len(calls) == len(replies) and 'PARTIAL_NOT_REPORT' not in result['market_report']


def test_budget_exhaustion_keeps_request_count_and_stops_before_finalizer_send(monkeypatch):
    with client(monkeypatch, [response()]) as (llm, calls):
        with pytest.raises(AnalysisBudgetStopped) as error, budget(max_calls=1) as counter:
            create_market_analyst(llm)(state())
        assert error.value.reason == 'call_limit_exceeded'
        assert counter.receipt()['model_attempts'] == 1 and len(calls) == 1


def test_old_smaller_budget_rejects_new_request_before_send(monkeypatch):
    with client(monkeypatch, []) as (llm, calls):
        with pytest.raises(AnalysisBudgetStopped) as error, budget(cap=8192) as counter:
            create_market_analyst(llm)(state())
        assert error.value.reason == 'model_output_limit_unbound'
        assert counter.receipt()['model_attempts'] == 0 and calls == []


def test_transport_failure_still_has_zero_sdk_retries(monkeypatch):
    from openai import APITimeoutError

    with client(monkeypatch, [httpx.ReadTimeout('synthetic')]) as (llm, calls):
        with budget() as counter, pytest.raises(APITimeoutError):
            create_market_analyst(llm)(state())
        assert counter.receipt()['model_attempts'] == 1 and len(calls) == 1


def test_extra_body_cannot_increase_new_output_limit(monkeypatch):
    with client(monkeypatch, []) as (llm, calls):
        llm.extra_body = {'max_tokens': 65537}
        with budget() as counter, pytest.raises(ValueError, match='output limit'):
            create_market_analyst(llm)(state())
        assert counter.receipt()['model_attempts'] == 0 and calls == []


def test_graph_config_conversion_keeps_explicit_cap_and_retry_zero():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {'llm_provider': 'deepseek', 'llm_max_output_tokens': '65536', 'llm_max_retries': 0}
    kwargs = graph._get_provider_kwargs()
    assert kwargs['max_tokens'] == 65536 and kwargs['max_retries'] == 0
    assert 'reasoning_effort' not in kwargs  # Normal Graph default behavior remains unchanged.
