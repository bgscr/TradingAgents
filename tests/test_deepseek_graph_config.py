"""Real CLI/config/Graph/client serialization with synthetic HTTP responses only."""
import asyncio
import copy
import json
import sqlite3
import threading
from contextlib import contextmanager

import httpx
import pytest

from cli import main as cli
from tests.test_deepseek_high_budget import budget, complete, response, state
from tradingagents import analysis_budget as ab, default_config
from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients import create_llm_client


def settings(**changes):
    config = {'llm_provider': 'deepseek', 'backend_url': 'https://api.deepseek.com',
        'quick_think_llm': 'deepseek-flash', 'deep_think_llm': 'deepseek-v4-pro',
        'deepseek_reasoning_effort': 'high', 'llm_max_output_tokens': 65536,
        'llm_max_retries': 0, 'analysis_budget_enabled': False,
        'analysis_max_calls': 60, 'analysis_timeout_seconds': 600,
        'checkpoint_enabled': False, 'financial_cache_only': False,
        'market_history_mode': 'current', 'mainland_capability_routing_mode': 'legacy'}
    config.update(changes)
    return config


def cli_config(monkeypatch, **changes):
    config = settings(**changes)
    monkeypatch.setattr(cli, 'DEFAULT_CONFIG', config)
    selections = {'ticker': '601328.SH', 'asset_type': 'stock', 'analysis_date': '2026-10-16',
        'analysts': [], 'research_depth': 1, 'llm_provider': 'deepseek',
        'backend_url': 'https://api.deepseek.com', 'shallow_thinker': 'deepseek-flash',
        'deep_thinker': 'deepseek-v4-pro', 'output_language': 'Chinese'}
    return cli._build_run_config(selections, checkpoint=False)


def graph_kwargs(config):
    # Execute the production config conversion without constructing a Graph,
    # opening stores/checkpoints or resolving real instruments.
    graph = object.__new__(TradingAgentsGraph)
    graph.config = config
    return graph._get_provider_kwargs()


@contextmanager
def wire_client(monkeypatch, model, config, replies):
    calls = []
    def wire(transport, request):
        assert request.method == 'POST' and str(request.url) == 'https://api.deepseek.com/chat/completions'
        calls.append(json.loads(request.content))
        assert len(calls) <= len(replies)
        reply = copy.deepcopy(replies[len(calls)-1])
        if isinstance(reply, Exception):
            raise reply
        reply['model'] = model
        return httpx.Response(200, json=reply)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', wire)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'public-synthetic-placeholder')
    sync = httpx.Client(trust_env=False, follow_redirects=False)
    async_client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        llm = create_llm_client(provider='deepseek', model=model,
            base_url='https://api.deepseek.com', api_key='public-synthetic-placeholder',
            http_client=sync, http_async_client=async_client, **graph_kwargs(config)).get_llm()
        yield llm, calls
    finally:
        sync.close()
        asyncio.run(async_client.aclose())


def test_explicit_environment_maps_and_default_is_unset(monkeypatch):
    assert default_config.DEFAULT_CONFIG['deepseek_reasoning_effort'] is None
    monkeypatch.setenv('TRADINGAGENTS_DEEPSEEK_REASONING_EFFORT', 'high')
    monkeypatch.setenv('TRADINGAGENTS_LLM_MAX_OUTPUT_TOKENS', '65536')
    config = default_config._apply_env_overrides({'deepseek_reasoning_effort': None, 'llm_max_output_tokens': None})
    assert config['deepseek_reasoning_effort'] == 'high'
    assert config['llm_max_output_tokens'] == '65536'


def test_cli_to_graph_preserves_explicit_config_without_new_prompt(monkeypatch):
    config = cli_config(monkeypatch)
    assert graph_kwargs(config) == {'reasoning_effort': 'high', 'max_tokens': 65536, 'max_retries': 0}
    assert config['analysis_max_calls'] == 60 and config['analysis_timeout_seconds'] == 600


def test_real_menu_selection_retains_explicit_effort(monkeypatch):
    from tests.test_analysis_budget import test_cli_menu_announcements_contract

    # Reuse the existing synthetic UI/announcement fixture, not a mocked menu.
    monkeypatch.setattr(cli, 'DEFAULT_CONFIG', {**cli.DEFAULT_CONFIG, 'deepseek_reasoning_effort': 'high'})
    original = cli.get_user_selections
    selected = []

    def capture():
        result = original()
        selected.append(result)
        return result

    monkeypatch.setattr(cli, 'get_user_selections', capture)
    test_cli_menu_announcements_contract(monkeypatch, enabled=True, cancel=False)
    assert len(selected) == 1 and selected[0]['deepseek_reasoning_effort'] == 'high'


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
@pytest.mark.parametrize('factory', [create_market_analyst, create_fundamentals_analyst])
def test_cli_graph_initial_and_finalizer_actual_wire(monkeypatch, model, factory):
    config = cli_config(monkeypatch)
    with wire_client(monkeypatch, model, config, [response(), complete()]) as (llm, calls):
        with budget() as counter:
            factory(llm)(state())
            assert counter.receipt()['model_attempts'] == 2
            assert counter.receipt()['data_attempts'] == 0
        assert len(calls) == 2
        assert all(p['model'] == model and p['reasoning_effort'] == 'high' and p['max_tokens'] == 65536 for p in calls)
        assert all('max_completion_tokens' not in p and 'thinking' not in p for p in calls)
        assert 'tools' in calls[0] and calls[1]['response_format'] == {'type': 'json_object'}
        assert llm.root_client.max_retries == llm.max_retries == 0


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
def test_default_none_does_not_send_explicit_effort(monkeypatch, model):
    config = cli_config(monkeypatch, deepseek_reasoning_effort=None, llm_max_output_tokens=8192)
    with wire_client(monkeypatch, model, config, [response(), complete()]) as (llm, calls):
        with budget(cap=8192):
            create_market_analyst(llm)(state())
        assert all('reasoning_effort' not in p and p['max_tokens'] == 8192 for p in calls)


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
def test_json_format_repair_counts_physical_attempt_and_preserves_high(monkeypatch, model):
    with wire_client(monkeypatch, model, settings(), [response(), response('{"bad":true}'), complete()]) as (llm, calls):
        with budget() as counter:
            create_market_analyst(llm)(state())
            assert counter.receipt()['model_attempts'] == 3
        assert len(calls) == 3 and all(p['reasoning_effort'] == 'high' for p in calls)


@pytest.mark.parametrize('extra', [{'thinking': {'type': 'disabled'}}, {'reasoning_effort': 'none'}])
def test_extra_body_cannot_override_explicit_effort(monkeypatch, extra):
    with wire_client(monkeypatch, 'deepseek-v4-pro', settings(), []) as (llm, calls):
        llm.extra_body = extra
        with budget() as counter, pytest.raises(ValueError, match='reasoning'):
            create_market_analyst(llm)(state())
        assert calls == [] and counter.receipt()['model_attempts'] == 0


@pytest.mark.parametrize('override', [None, 'none', 'low'])
def test_per_call_cannot_clear_or_change_bound_high(monkeypatch, override):
    with wire_client(monkeypatch, 'deepseek-v4-pro', settings(), []) as (llm, calls):
        with budget() as counter, pytest.raises(ValueError, match='reasoning'):
            llm.invoke(state()['messages'], reasoning_effort=override)
        assert calls == [] and counter.receipt()['model_attempts'] == 0


def test_compatible_explicit_thinking_enabled_retains_high(monkeypatch):
    with wire_client(monkeypatch, 'deepseek-v4-pro', settings(), [response()]) as (llm, calls):
        supplied = {'thinking': {'type': 'enabled'}}
        before = copy.deepcopy(supplied)
        with budget():
            llm.invoke(state()['messages'], extra_body=supplied)
        assert supplied == before
        assert calls[0]['reasoning_effort'] == 'high'
        assert calls[0]['thinking'] == {'type': 'enabled'}


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
@pytest.mark.parametrize('stage', ['initial', 'finalizer'])
def test_length_stops_without_further_format_attempt(monkeypatch, model, stage):
    truncated = response('incomplete', finish='length', completion=65536, reasoning=64000)
    replies = [truncated] if stage == 'initial' else [response(), truncated]
    with wire_client(monkeypatch, model, settings(), replies) as (llm, calls):
        with budget() as counter:
            result = create_market_analyst(llm)(state())
            assert counter.receipt()['model_attempts'] == len(replies)
        assert len(calls) == len(replies)
        diagnostic = result['messages'][-1].response_metadata['analyst_submission_diagnostic']
        assert diagnostic['finish_reason'] == 'length'
        assert diagnostic['stage'] == ('initial_response' if stage == 'initial' else 'finalizer')
        assert diagnostic['completion_tokens'] == 65536 and diagnostic['reasoning_tokens'] == 64000
        assert 'incomplete' not in result['market_report']
        assert all(p['reasoning_effort'] == 'high' and p['max_tokens'] == 65536 for p in calls)


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
def test_sdk_timeout_is_one_attempt_and_no_finalizer(monkeypatch, model):
    from openai import APITimeoutError

    with wire_client(monkeypatch, model, settings(), [httpx.ReadTimeout('synthetic')]) as (llm, calls):
        with budget() as counter, pytest.raises(APITimeoutError):
            create_market_analyst(llm)(state())
        assert len(calls) == 1 and counter.receipt()['model_attempts'] == 1


def test_explicit_effort_on_unverified_deepseek_model_is_rejected(monkeypatch):
    with (
        pytest.raises(ValueError, match='requires Flash or V4 Pro'),
        wire_client(monkeypatch, 'deepseek-reasoner', settings(), []),
    ):
        pytest.fail('explicit effort must not be silently dropped')


@pytest.mark.parametrize('effort', ['', 'HIGH', 'medium', True, 1, []])
def test_invalid_config_effort_is_rejected_before_client(effort):
    with pytest.raises(ValueError, match='deepseek_reasoning_effort'):
        ab.validate_budget_config(settings(deepseek_reasoning_effort=effort))


def test_explicit_effort_requires_deepseek_provider():
    with pytest.raises(ValueError, match='deepseek_reasoning_effort'):
        ab.validate_budget_config(settings(llm_provider='openai'))


def test_unspecified_effort_keeps_old_checkpoint_binding():
    config = settings()
    del config['deepseek_reasoning_effort']
    assert ab.checkpoint_budget_binding(config, 'fixed-graph') == ab.checkpoint_budget_binding(
        {**config, 'deepseek_reasoning_effort': None}, 'fixed-graph')


def test_changed_effort_cannot_resume_or_reset_synthetic_checkpoint_budget():
    config = settings()
    binding = ab.checkpoint_budget_binding(config, 'fixed-graph')
    changed = ab.checkpoint_budget_binding({**config, 'deepseek_reasoning_effort': 'low'}, 'fixed-graph')
    assert changed != binding
    conn = sqlite3.connect(':memory:')
    lock = threading.RLock()
    try:
        original = ab.AnalysisBudget(60, 600, max_output_tokens=65536)
        original.bind_checkpoint(conn, lock, 'synthetic-thread', binding, has_checkpoint=False)
        original.admit('model')
        original.record_interruption()
        before = conn.execute('SELECT policy,state FROM analysis_budget_v1 WHERE thread=?', ('synthetic-thread',)).fetchone()
        with pytest.raises(ab.AnalysisBudgetStopped, match='checkpoint_budget_mismatch'):
            ab.AnalysisBudget(60, 600, max_output_tokens=65536).bind_checkpoint(
                conn, lock, 'synthetic-thread', changed, has_checkpoint=True)
        assert conn.execute('SELECT policy,state FROM analysis_budget_v1 WHERE thread=?', ('synthetic-thread',)).fetchone() == before
        resumed = ab.AnalysisBudget(60, 600, max_output_tokens=65536)
        resumed.bind_checkpoint(conn, lock, 'synthetic-thread', binding, has_checkpoint=True)
        assert resumed.receipt()['model_attempts'] == 1
        assert resumed._deadline == original._deadline
    finally:
        conn.close()
