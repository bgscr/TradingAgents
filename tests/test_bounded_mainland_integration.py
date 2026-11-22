"""Actual entry/CLI/Graph and synthetic stores; replace only user input and external wire.

Fixtures are invented data, not evidence qualifying a real security. Native ReportStore
publication is deliberately not exercised at any previously denied filesystem path.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_entry():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('bounded_mainland_entry_test', ROOT / 'scripts/analyze_mainland_bounded.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_session(tmp_path, monkeypatch, *, ticker='601328.SS', session_root=None):
    import cli.main as cli
    from tests.test_analysis_budget_e2e import _clear_client_cache, _seed_calendar
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    module = load_entry()
    root = module.reserve_session(tmp_path / 'sessions') if session_root is None else session_root
    if session_root is not None:
        assert root.parent == ROOT / 'reports/bounded-runs'
        assert json.loads((root / 'session.json').read_text()) == {
            'contract': 'bounded-mainland-session-v1', 'run_id': root.name,
            'root': str(root), 'resume': False}
        assert (root / 'SYNTHETIC.txt').is_file()
    session = module.Session(root, source=ROOT, calendar_authorized=True)
    # Only replace filesystem fixture locations. Policy, readiness and actual SQLite are real.
    session.paths.update(data_cache_dir=tmp_path / 'cache', market_history_database_path=tmp_path / 'history.sqlite3',
                         market_history_payload_root=tmp_path / 'payloads', market_history_backup_root=tmp_path / 'backups')
    if session_root is not None:
        session.paths.update(checkpoint_data_dir=tmp_path / 'checkpoint-state',
                             results_dir=tmp_path / 'analysis',
                             memory_log_path=tmp_path / 'memory/trading_memory.md')
    session.paths['data_cache_dir'].mkdir()
    config = session.configure(DEFAULT_CONFIG)
    _seed_calendar(config)
    with MarketHistoryStore.open_provider_request_authority(MarketHistoryConfig.from_mapping(config)) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service = upstream_service_identity_for_provider('tushare')
        coordinator.register_upstream_service(upstream_id, service)
        coordinator.configure_operator_ceiling(upstream_service_id=upstream_id,
            minimum_interval=timedelta(seconds=1.5), allow_prewarming=False, configured_at=datetime.now(timezone.utc))
        coordinator.close()
    _clear_client_cache()
    for key, value in {'TUSHARE_TOKEN': 'public-synthetic-only', 'DEEPSEEK_API_KEY': 'public-synthetic-only',
        'TRADINGAGENTS_LLM_PROVIDER': 'deepseek', 'TRADINGAGENTS_QUICK_THINK_LLM': 'deepseek-flash',
        'TRADINGAGENTS_DEEP_THINK_LLM': 'deepseek-v4-pro', 'TRADINGAGENTS_LLM_BACKEND_URL': 'https://api.deepseek.com',
        'TRADINGAGENTS_OUTPUT_LANGUAGE': 'Chinese', 'TRADINGAGENTS_MAX_DEBATE_ROUNDS': '1',
        'TRADINGAGENTS_MAX_RISK_ROUNDS': '1'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, 'DEFAULT_CONFIG', config)
    monkeypatch.setattr(cli, 'get_ticker', lambda: ticker)
    monkeypatch.setattr(cli, 'get_analysis_date', lambda _ticker: '2026-10-16')
    monkeypatch.setattr(cli, 'select_china_a_enhancement_preset', lambda: 'basic')
    from cli.models import AnalystType
    monkeypatch.setattr(cli, 'select_analysts', lambda *_: [AnalystType(value) for value in ('market','social','news','fundamentals')])
    # Final report UI only. Automatic advisory draft publication remains native;
    # its retained PermissionError case is not included in subsequent invocations.
    monkeypatch.setattr(cli.typer, 'prompt', lambda *args, **kwargs: 'N')
    session.clock = module.SharedSessionClock()
    return module, session, cli, config


def install_wires(monkeypatch):
    # Preserve actual HTTPX/TLS pool construction; the older test helper replaces
    # it with a lifecycle-only pool and cannot exercise the current TLS boundary.
    from curl_cffi import AsyncCurl, Curl
    from urllib3.connectionpool import HTTPConnectionPool

    from tests.test_analysis_budget import OfflineNetworkAttempt, httpx_families
    from tests.test_analysis_budget_e2e import FakeWire, _days
    from tests.test_financial_acceptance_scope import sample_rows
    from tradingagents.analysis_budget import active_budget
    def denied(*args, **kwargs):
        raise OfflineNetworkAttempt('unmocked native network forbidden')
    monkeypatch.setattr(Curl, 'perform', denied)
    monkeypatch.setattr(AsyncCurl, 'add_handle', denied)
    wire = FakeWire()
    wire.actual_https = []
    wire.seeding = False
    wire.fail_market = False
    wire.loop_market = False
    wire.expire_session = None
    original_model = wire.httpx

    def model(transport, request, family):
        body = json.loads(request.content)
        assert body['reasoning_effort'] == 'high' and body['max_tokens'] == 65536
        reply = original_model(transport, request, family)
        if wire.loop_market:
            data = reply.json()
            data['choices'][0].update(finish_reason='tool_calls', message={'role':'assistant','content':'',
                'tool_calls':[{'id':f'loop-{len(wire.model_calls)}','type':'function',
                    'function':{'name':'get_stock_data','arguments':json.dumps({'symbol':'601328.SS',
                        'start_date':'2026-10-01','end_date':'2026-10-16'})}}]})
            return family.Response(200, request=request, json=data)
        if wire.expire_session is not None:
            wire.expire_session.deadline_monotonic = 0
            active_budget()._session_monotonic_deadline = 0
        if wire.fail_market and wire.phases['market_finalizer']:
            data = reply.json()
            data['choices'][0]['message']['content'] = '{}'
            return family.Response(200, request=request, json=data)
        return reply

    for family in httpx_families():
        monkeypatch.setattr(family.HTTPTransport, 'handle_request',
            lambda transport, request, family=family: model(transport, request, family))
    allowed = {'push2his.eastmoney.com', 'search-api-web.eastmoney.com', 'emappdata.eastmoney.com',
               'datacenter-web.eastmoney.com', 'datacenter.eastmoney.com'}

    def public(pool, method, url, **kwargs):
        assert pool.host in allowed, 'disabled source reached actual transport'
        return wire.urllib3(pool, method, url, **kwargs)

    monkeypatch.setattr(HTTPConnectionPool, 'urlopen', public)

    def request(connection, method, path, *, body, headers):
        assert connection.host == 'api.waditu.com' and method == 'POST'
        payload = json.loads(body)
        endpoint, params = payload['api_name'], payload['params']
        wire.actual_https.append(endpoint)
        assert endpoint in {'daily', 'adj_factor', 'suspend_d', 'trade_cal', 'balancesheet', 'income', 'cashflow'}
        if endpoint in {'balancesheet', 'income', 'cashflow'}:
            assert active_budget() is None, 'analysis attempted financial refresh'
            frame = sample_rows(endpoint, params['ts_code'])
            fields, rows = list(frame.columns), frame.values.tolist()
        else:
            days = [day for day in _days() if day.strftime('%Y%m%d') >= params.get('start_date', '00000000')
                    and day.strftime('%Y%m%d') <= params.get('end_date', '99999999')]
            if endpoint == 'trade_cal':
                fields = ['exchange', 'cal_date', 'is_open', 'pretrade_date']
                rows = [['SSE', day.strftime('%Y%m%d'), int(day.weekday() < 5), ''] for day in days]
            elif endpoint == 'suspend_d':
                fields, rows = ['ts_code', 'trade_date', 'suspend_timing', 'suspend_type'], []
            elif endpoint == 'adj_factor':
                fields = ['ts_code', 'trade_date', 'adj_factor']
                rows = [[params['ts_code'], day.strftime('%Y%m%d'), 1.0] for day in days if day.weekday() < 5]
            else:
                fields = ['ts_code','trade_date','open','high','low','close','pre_close','change','pct_chg','vol','amount']
                rows = [[params['ts_code'], day.strftime('%Y%m%d'), 10+i/100, 11+i/100, 9+i/100,
                         10+i/100, 10+i/100, 0, 0, 1000, 10000]
                        for i, day in enumerate(_days()) if day in days and day.weekday() < 5]
            offset, limit = params.get('offset', 0), params.get('limit', 1000000)
            rows = sorted(rows, key=lambda row: row[1], reverse=True)[offset:offset + limit]
        connection.fixture_response = json.dumps({'code': 0, 'data': {'fields': fields, 'items': rows}}).encode()

    def response(connection):
        return SimpleNamespace(status=200, getheader=lambda *_: None,
                               read=lambda maximum: connection.fixture_response[:maximum], close=lambda: None)
    monkeypatch.setattr(http.client.HTTPSConnection, 'request', request)
    monkeypatch.setattr(http.client.HTTPSConnection, 'getresponse', response)
    return wire


def test_real_menu_identity_calendar_and_coordinator_precheck(tmp_path, monkeypatch):
    module, session, cli, config = setup_session(tmp_path, monkeypatch)
    from tradingagents.analysis_budget import analysis_preparation_scope
    with analysis_preparation_scope(config):
        session.selection = cli.get_user_selections()
        module.validate_selection(session.selection)
    prepared = session.precheck(config)
    assert prepared['analysis_allowed_data_vendors'] == ['tushare', 'akshare']
    assert session.selection['ticker'] == '601328.SS'
    assert json.loads((session.root / 'precheck.json').read_text())['issues'] == []
    assert session.storage.cache.name == 'current-financial-statements.sqlite3'
    assert not session.storage.cache.exists()


CHILD = r'''
import sys, os, json, http.client, subprocess, getpass
from pathlib import Path
from types import SimpleNamespace
SOURCE, ALLOWED = Path(sys.argv[1]), Path(sys.argv[2])
args = sys.argv[3:]
sys.path[:0] = [str(SOURCE), str(SOURCE / 'scripts')]
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
def audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg'}:
        raise RuntimeError('offline all network forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        p = Path(os.fsdecode(args[0])).absolute()
        if p.name.casefold() in {'.env', '.netrc', '_netrc'}:
            raise RuntimeError('offline credential file forbidden')
        if p.name == 'tradingagents.local.json' and not p.is_relative_to(ALLOWED):
            raise RuntimeError('offline real profile forbidden')
        mode, flags = args[1] or '', args[2]
        if (any(c in mode for c in 'wax+') or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)):
            if str(p).casefold() != os.devnull.casefold() and not p.is_relative_to(ALLOWED):
                raise RuntimeError('offline outside write forbidden')
if '--worker' not in args:
    sys.addaudithook(audit)
import accept_financial_statements as finance
if '--worker' in args:
    original_install = finance.install
    def install(*args, **kwargs):
        result = original_install(*args, **kwargs)
        # Product self-check denials must reach its own audit first. The extra test
        # boundary is still present before application/transport and cannot permit I/O.
        sys.addaudithook(audit)
        return result
    finance.install = install
    getpass.getpass = lambda *_args, **_kwargs: 'public-synthetic-only'
    sys.stdin = SimpleNamespace(isatty=lambda: True)
    def request(connection, method, path, *, body, headers):
        payload = json.loads(body)
        assert connection.host == 'api.waditu.com' and method == 'POST'
        assert payload['api_name'] in {'balancesheet', 'income', 'cashflow'}
        from tests.test_financial_acceptance_scope import sample_rows
        frame = sample_rows(payload['api_name'], payload['params']['ts_code'])
        connection.fixture_body = json.dumps({'code':0, 'data': {'fields':list(frame.columns),
            'items':frame.values.tolist()}}).encode()
    def response(connection):
        return SimpleNamespace(status=200, getheader=lambda *_: None,
            read=lambda n: connection.fixture_body[:n], close=lambda: None)
    http.client.HTTPSConnection.request = request
    http.client.HTTPSConnection.getresponse = response
else:
    original_run = subprocess.run
    def run(command, **kwargs):
        assert command[4] == str(SOURCE / 'scripts/accept_financial_statements.py')
        assert '--worker' in command
        rewritten = [sys.executable, '-I', '-S', '-B', __file__, str(SOURCE), str(ALLOWED), *command[5:]]
        return original_run(rewritten, **kwargs)
    subprocess.run = run
sys.argv = [str(SOURCE / 'scripts/accept_financial_statements.py'), *args]
raise SystemExit(finance.main())
'''


STRICT_CHILD = r'''
import sys, os, json, http.client, subprocess
from pathlib import Path
from types import SimpleNamespace
SOURCE, ALLOWED, OWNED = (Path(v).absolute() for v in sys.argv[1:4])
args = sys.argv[4:]
assert OWNED.parent == SOURCE / 'reports/bounded-runs'
assert (OWNED / 'SYNTHETIC.txt').is_file()
REFRESH = OWNED / 'refresh'
assert args[args.index('--output') + 1] == str(REFRESH)
sys.path[:0] = [str(SOURCE), str(SOURCE / 'scripts')]
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
role = args[args.index('--worker') + 1] if '--worker' in args else 'coordinator'
def audit(event, values):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','socket.sendto','socket.sendmsg'}:
        if REFRESH.is_dir():
            with (REFRESH / ('synthetic-network-' + role + '.jsonl')).open('a') as h:
                h.write(json.dumps({'event':event,'blocked':True}) + '\n')
        raise RuntimeError('synthetic_network_denied')
    if event == 'open' and isinstance(values[0], (str, bytes, os.PathLike)):
        # subprocess.DEVNULL opens the Windows null device relative to cwd.
        # Admit only its exact spelling, never an arbitrary path ending in nul.
        if os.fsdecode(values[0]).casefold() == os.devnull.casefold():
            return
        path = Path(os.fsdecode(values[0])).absolute()
        if path.name.casefold() in {'.env','.netrc','_netrc'}:
            raise RuntimeError('synthetic_credentials_denied')
        if path.name == 'tradingagents.local.json' and not path.is_relative_to(OWNED):
            raise RuntimeError('real_profile_denied')
        mode, flags = values[1] or '', values[2]
        if any(c in mode for c in 'wax+') or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
            if str(path).casefold() != os.devnull.casefold() and not (
                    path.is_relative_to(ALLOWED) or path.is_relative_to(OWNED)):
                raise RuntimeError('synthetic_outside_write_denied')
if role == 'coordinator':
    sys.addaudithook(audit)
import accept_financial_statements as finance
if role != 'coordinator':
    original_install = finance.install
    def install(*args, **kwargs):
        result = original_install(*args, **kwargs)
        sys.addaudithook(audit)
        return result
    finance.install = install
    def request(connection, method, path, *, body, headers):
        payload = json.loads(body)
        assert connection.host == 'api.waditu.com' and method == 'POST'
        assert payload['api_name'] in {'balancesheet','income','cashflow'}
        from tests.test_financial_acceptance_scope import sample_rows
        frame = sample_rows(payload['api_name'], payload['params']['ts_code'])
        connection.fixture_body = json.dumps({'code':0,'data':{
            'fields':list(frame.columns),'items':frame.values.tolist()}}).encode()
    def response(connection):
        return SimpleNamespace(status=200,getheader=lambda *_:None,
            read=lambda n:connection.fixture_body[:n],close=lambda:None)
    http.client.HTTPSConnection.request = request
    http.client.HTTPSConnection.getresponse = response
else:
    original_popen = subprocess.Popen
    def popen(command, **kwargs):
        assert command[:4] == [sys.executable,'-I','-S','-B']
        assert command[4] == str(SOURCE / 'scripts/accept_financial_statements.py')
        assert '--worker' in command
        assert command[command.index('--output') + 1] == str(REFRESH)
        rewritten = [sys.executable,'-I','-S','-B',__file__,str(SOURCE),str(ALLOWED),str(OWNED),*command[5:]]
        kwargs['executable'] = sys.executable
        return original_popen(rewritten, **kwargs)
    subprocess.Popen = popen
sys.argv = [str(SOURCE / 'scripts/accept_financial_statements.py'),*args]
raise SystemExit(finance.main())
'''


def install_strict_finance_children(tmp_path, monkeypatch, module, owned_root, *, admit_command=None):
    """Current stdin-pipe transport; synthetic credentials, exact new owned root."""
    assert owned_root.parent == ROOT / 'reports/bounded-runs'
    assert (owned_root / 'SYNTHETIC.txt').is_file()
    child = tmp_path / 'offline_finance.py'
    with child.open('x', encoding='utf-8') as handle:
        handle.write(STRICT_CHILD)
    original_popen = module.subprocess.Popen

    def offline(command, **kwargs):
        assert command[:4] == [sys.executable, '-I', '-S', '-B']
        assert command[4] == str(ROOT / 'scripts/accept_financial_statements.py')
        assert '--authorize-live' in command and '--session-token-stdin' in command
        assert '--use-project-env' not in command
        assert command[command.index('--output') + 1] == str(owned_root / 'refresh')
        rewritten = [sys.executable, '-I', '-S', '-B', str(child), str(ROOT), str(tmp_path), str(owned_root), *command[5:]]
        kwargs.update(stdout=module.subprocess.DEVNULL, stderr=module.subprocess.DEVNULL,
                      executable=sys.executable)
        if admit_command is not None:
            admit_command(rewritten)
        return original_popen(rewritten, **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', offline)


def install_finance_children(tmp_path, monkeypatch, module):
    child = tmp_path / 'offline_finance.py'
    child.write_text(CHILD, encoding='utf-8')
    original_run = module.subprocess.run

    def offline(command, **kwargs):
        assert command[4] == str(ROOT / 'scripts/accept_financial_statements.py')
        assert '--authorize-live' in command  # Only synthetic wire, exercising the actual live branch.
        rewritten = [sys.executable, '-I', '-S', '-B', str(child), str(ROOT), str(tmp_path), *command[5:]]
        with (tmp_path / 'finance-console.log').open('a', encoding='utf-8') as console:
            return original_run(rewritten, **kwargs, stdout=console, stderr=console)
    monkeypatch.setattr(module.subprocess, 'run', offline)


def test_real_cross_process_finance_reuses_original_views_and_timestamps(tmp_path, monkeypatch):
    module, session, cli, config = setup_session(tmp_path, monkeypatch)
    session.selection = cli.get_user_selections()
    session.precheck(config)
    install_finance_children(tmp_path, monkeypatch, module)
    assert session.refresh() == [3, 0]
    live = json.loads((session.root / 'refresh/live.json').read_text())
    reuse = json.loads((session.root / 'refresh/reuse.json').read_text())
    assert live['pid'] != reuse['pid'] != os.getpid()
    assert live['views'] == reuse['views'] and len(live['views']) == 6
    assert reuse['access_during_application'] == {'network':0,'credentials':0,'subprocess':0,'outside_write':0}
    # A second independent ordinary session may use this still-valid cache without renewing it.
    second = module.Session(module.reserve_session(tmp_path / 'sessions'), source=ROOT)
    for key in ('data_cache_dir', 'market_history_database_path', 'market_history_payload_root', 'market_history_backup_root'):
        second.paths[key] = session.paths[key]
    second.clock = module.SharedSessionClock()
    second.selection = dict(session.selection)
    second.precheck(config)
    assert second.refresh() == [0, 0]
    assert json.loads((second.root / 'refresh/live.json').read_text())['views'] == live['views']


@pytest.mark.parametrize('failure', [None, 'critical', 'deadline', 'count'])
def test_complete_cli_graph_and_terminal_safety(tmp_path, monkeypatch, failure):
    module, session, cli, config = setup_session(tmp_path, monkeypatch)
    if failure == 'count':
        # A synthetic longer graph permits observing the unchanged 60-send ceiling;
        # the retained first trial hit the separate default 100-step graph limit first.
        config['max_recur_limit'] = 200
    install_finance_children(tmp_path, monkeypatch, module)
    wire = install_wires(monkeypatch)
    wire.fail_market = failure == 'critical'
    wire.loop_market = failure == 'count'
    wire.expire_session = session.clock if failure == 'deadline' else None
    from tradingagents.analysis_budget import AnalysisBudget
    from tradingagents.graph.conditional_logic import ConditionalLogic
    originals = (cli.get_user_selections, cli._build_run_config, AnalysisBudget.bind_checkpoint,
                 ConditionalLogic.should_continue_market)
    source_checks = []
    manifest = module.read_json(ROOT / 'config/bounded_mainland_release.json')
    def verify_sources():
        module.verify_release(manifest)
        source_checks.append(session.phase)
    result = session.execute(cli, config, verify_sources=verify_sources)
    (tmp_path / 'observed.json').write_text(json.dumps({'result':result, 'phases':dict(wire.phases),
        'model_count':len(wire.model_calls), 'https':wire.actual_https,
        'public_hosts':[host for host, *_ in wire.data_calls]}, indent=2), encoding='utf-8')
    assert result['financial_requests'] == [3, 0]
    assert source_checks == ['precheck', 'precheck', 'financial_preparation']
    assert originals == (cli.get_user_selections, cli._build_run_config, AnalysisBudget.bind_checkpoint,
                         ConditionalLogic.should_continue_market)
    assert not any(endpoint in {'balancesheet','income','cashflow'} for endpoint in wire.actual_https)
    if failure is None:
        assert result['status'] == 'completed', result
        assert result['terminal_outcome_kind'] == 'trading_decision'
        assert result['completion_is_not_decision_qualification'] is True
        assert wire.phases['research_plan'] == wire.phases['trader'] == 1
        assert all(wire.phases[name + '_finalizer'] for name in ('market','news','fundamentals'))
        assert {body['model'] for body in wire.model_calls} == {'deepseek-flash','deepseek-v4-pro'}
        budget = result['analysis_budget']
        assert budget['model_attempts'] == len(wire.model_calls)
        assert budget['admitted_attempts'] == len(wire.model_calls) + len(wire.actual_https) + len(wire.data_calls)
        assert result['total_external_attempts'] == 3 + result['calendar_requests'] + budget['admitted_attempts'] <= 60
        assert any(gap['reason'] == 'user_disabled' for gap in budget['data_gaps'])
    else:
        assert result['status'] == 'stopped'
        assert not wire.phases['research_plan'] and not wire.phases['trader']
        if failure == 'critical':
            stopped = json.loads((session.root / 'critical-stop.json').read_text())
            assert stopped['analyst'] == 'market' and stopped['subsequent_analysis_sends_allowed'] is False
        elif failure == 'deadline':
            assert len(wire.model_calls) == 1
        else:
            assert result['primary_error']['budget_reason'] == 'call_limit_exceeded'
            assert 3 + len(wire.model_calls) + len(wire.actual_https) + len(wire.data_calls) == 60
    assert all(body['max_tokens'] == 65536 and body['reasoning_effort'] == 'high' for body in wire.model_calls)
    assert result['resume'] is False


def test_production_graph_success_and_degraded_renderer_without_native_publication(tmp_path, monkeypatch):
    """Independent Graph/renderer coverage; does not replace the blocked CLI publish check."""
    module, session, cli, config = setup_session(tmp_path, monkeypatch)
    install_finance_children(tmp_path, monkeypatch, module)
    wire = install_wires(monkeypatch)
    session.selection = cli.get_user_selections()
    config = session.precheck(config)
    assert session.refresh() == [3,0]
    config = session.configure(config, selection=session.selection)
    from tests.test_analysis_budget_e2e import _close
    from tradingagents import reporting
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.statement_acceptance import _retain_shared_ceiling
    with _retain_shared_ceiling(session.storage), session.analysis_hooks(cli, config, {}):
        assembled = cli._build_run_config(session.selection, True, mainland_core_sources=True)
        graph = TradingAgentsGraph(config=assembled, instrument_symbol=session.selection['ticker'])
        try:
            state, _ = graph.propagate(session.selection['ticker'], session.selection['analysis_date'])
            assert state['admission_gate']['admitted'] is True
            assert state['terminal_outcome_kind'] == 'trading_decision', state.get('analysis_outcome')
            decision = reporting.TradingDecisionContract.model_validate(state['trading_decision'])
            terminal = reporting.TerminalContract.model_validate(state['terminal_contract'])
            evidence = reporting.EvidenceState.model_validate(state['evidence_state'])
            financial = reporting.TypeAdapter(reporting.FinancialDispatchAuditProjection).validate_python(
                state['financial_dispatch_audit_projection'])
            rendered = reporting.render_decision_report(decision, terminal, evidence, financial)
            rendered += '\n' + reporting.budget_data_gap_markdown(state)
            assert decision.decision_id in rendered and 'DATA_UNAVAILABLE' in rendered
            (tmp_path / 'renderer-only.md').write_text(rendered, encoding='utf-8')
            budget = graph.last_analysis_budget
            assert budget['admitted_attempts'] == len(wire.model_calls) + len(wire.actual_https) + len(wire.data_calls)
            assert budget['admitted_attempts'] <= 57 and budget['stop_reason'] == 'completed'
            assert wire.phases['research_plan'] == wire.phases['trader'] == 1
            assert {body['model'] for body in wire.model_calls} == {'deepseek-flash','deepseek-v4-pro'}
            (tmp_path / 'graph-only-result.json').write_text(json.dumps({'terminal':state['terminal_outcome_kind'],
                'budget':budget, 'phases':dict(wire.phases), 'real_security_qualified':False,
                'native_cli_publication_verified':False},indent=2),encoding='utf-8')
        finally:
            _close(graph)

def test_explicit_selection_matches_menu_shape_then_uses_real_precheck(tmp_path, monkeypatch):
    module, session, cli, config = setup_session(tmp_path, monkeypatch)
    from tradingagents.analysis_budget import analysis_preparation_scope
    with analysis_preparation_scope(config):
        expected = cli.get_user_selections()
        selected = module.build_explicit_selection('601328.SH', '2026-10-16', config)
        module.validate_selection(selected)
    assert selected == expected
    session.selection = selected
    prepared = session.precheck(config)
    assert prepared['financial_cache_only'] is True
    assert prepared['analysis_max_calls'] == 60
    assert prepared['llm_max_retries'] == 0
    assert prepared['deepseek_reasoning_effort'] == 'high'
    assert prepared['llm_max_output_tokens'] == 65536
    assert prepared['analysis_session_deadline_epoch'] <= session.clock.deadline_epoch
    assert prepared['analysis_session_monotonic_deadline'] == session.clock.deadline_monotonic
    assert json.loads((session.root / 'precheck.json').read_text())['issues'] == []


def test_explicit_selection_still_rejects_missing_snapshot_before_finance(tmp_path, monkeypatch):
    module, session, cli, config = setup_session(tmp_path, monkeypatch, ticker='600919.SS')
    session.bank_snapshot = '0' * 32 + ':' + '0' * 64
    def unexpected():
        pytest.fail('menu or finance entered after explicit selection / failed identity')
    monkeypatch.setattr(cli, 'get_user_selections', unexpected)
    monkeypatch.setattr(session, 'refresh', unexpected)
    value = session.execute(cli, config, verify_sources=lambda: None,
                            requested_selection=('600919.SH', '2026-11-01'))
    assert value['status'] == 'stopped' and value['phase'] == 'precheck'
    assert value['financial_requests'] is None and value['resume'] is False
    assert not (session.root / 'budget-binding.json').exists()
    assert not (session.root / 'configuration.json').exists()
    assert session.selection['ticker'] == '600919.SS'
    assert session.selection['analysis_date'] == '2026-11-01'
