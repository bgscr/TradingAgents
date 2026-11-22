"""Synthetic public-entry/dispatch tests; never launch PowerShell or networking."""
from tests.test_bounded_mainland_entry import entry
import json
import copy
from datetime import date, datetime, timezone
from unittest.mock import Mock
import pytest

from tradingagents import powershell_fundflow as p, analysis_budget as b
from tradingagents.dataflows import financial_dispatch as fd, config as config_module, akshare_data


def identity():
    from tradingagents.evidence import InstrumentIdentityEvidence, IdentityProvenance, InstrumentKind
    return InstrumentIdentityEvidence(symbol='601328.SS', venue='XSHG', currency='CNY',
        instrument_kind=InstrumentKind.EQUITY, display_name='Synthetic bank',
        provenance=IdentityProvenance(provider='test-registry', source_ref='registry:test:v1',
            retrieved_at='2026-07-01T00:00:00Z', artifact_sha256='a' * 64))


def owner(tmp_path):
    exe = tmp_path / 'pwsh.exe'; exe.write_bytes(b'synthetic never executed')
    script = tmp_path / 'Get-PublicFundflow.ps1'; script.write_bytes(b'# synthetic never executed')
    return p.PowerShellFundflow(exe, script, '601328.SH', '2026-11-13')


def request():
    return fd.FinancialToolRequest(tool_name='get_fundamentals',
        statement_type=fd.FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=fd.FinancialReportingFrequency.NOT_APPLICABLE, as_of_date=date(2026, 11, 13),
        material_arguments={}, tool_call_id='tool-call-1', graph_message_id='message-1', process_id=101,
        dispatched_at=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc))


def runtime(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(market_history_mode='shadow',
        market_history_database_path=str(tmp_path / 'history/market_history.sqlite3'),
        market_history_payload_root=str(tmp_path / 'history/payloads'),
        market_history_backup_root=str(tmp_path / 'history/backups'))
    config.update(bounded_powershell_fundflow_enabled=True, analysis_skip_optional_fundamentals=True,
                  data_usage_mode='personal_research')
    config['tool_vendors'] = {key: 'akshare' for key in ('get_fundamentals', 'get_balance_sheet', 'get_cashflow', 'get_income_statement')}
    return config


def test_formal_dispatch_identity_does_not_reuse_legacy_transport_key(tmp_path, monkeypatch):
    config = runtime(tmp_path)
    monkeypatch.setattr(config_module, '_config', config)
    with p.powershell_fundflow_scope(owner(tmp_path)):
        new = fd.FinancialToolDispatcher.from_configured_vendors(instrument_identity=identity(), config=config)
        legacy = fd.FinancialToolDispatcher.from_configured_vendors(instrument_identity=identity(),
            config={**config, 'bounded_powershell_fundflow_enabled': False})
        assert new.canonical_request_key(request()).request_key != legacy.canonical_request_key(request()).request_key


def test_explicit_fundflow_option_preserves_other_sources_and_selection():
    args = entry().parse_arguments([
        '--ticker', '601328.SH', '--analysis-date', '2026-11-13',
        '--powershell-fundflow', '--powershell-fundflow-executable', 'C:/synthetic/pwsh.exe',
        '--moneyflow',
    ])
    assert args.powershell_fundflow and args.moneyflow
    assert not args.powershell_news
    assert args.ticker == '601328.SH' and args.analysis_date == '2026-11-13'


def test_formal_session_pins_only_selected_fundflow_transport(tmp_path):
    session = entry().Session(tmp_path, powershell_fundflow_executable='C:/synthetic/pwsh.exe')
    assert session.configure({})['bounded_powershell_fundflow_enabled'] is True
    assert session.configure({})['bounded_powershell_news_enabled'] is False
    assert entry().Session(tmp_path).configure({})['bounded_powershell_fundflow_enabled'] is False


def line(day, amount='987654321', main='3'):
    return ','.join([day, amount, '2', '3', '4', '5', main, '-2', '1', '1', '2', '999999', '1.5', '0', '0'])


def payload(rows=None, **changes):
    obj = {'rc': 0, 'data': {'code': '601328', 'market': 1,
        'klines': rows if rows is not None else [line('2026-11-13')]}}
    obj.update(changes)
    return json.dumps(obj).encode()


def test_projection_sorts_filters_future_deduplicates_and_excludes_unknown_units():
    source = payload([line('2026-11-14'), line('2026-11-13'), line('2026-11-06'),
                      line('2026-11-05'), line('2026-11-06')], token='SECRET_SENTINEL')
    result = p.project_response(source, '601328.SH', '2026-11-06', '2026-11-13')
    assert result['counts'] == {'returned': 5, 'duplicate': 1, 'older': 1, 'future': 1, 'accepted': 2}
    assert result['rows'] == [['2026-11-06', '3', '-2', '1', '1', '2', '1.5'],
                              ['2026-11-13', '3', '-2', '1', '1', '2', '1.5']]
    for value in ('987654321', '999999', 'SECRET_SENTINEL', '2026-11-14'):
        assert value not in json.dumps(result)


@pytest.mark.parametrize('case', ['rc', 'bool', 'stock', 'market', 'missing', 'null', 'depth',
    'duplicate_key', 'nonfinite', 'oversize', 'conflict', 'bad_date', 'bad_numeric', 'percent', 'malicious'])
def test_response_rejections_are_closed_and_do_not_leak(case):
    content = payload()
    if case == 'rc': content = payload(rc=1, msg='SECRET_SENTINEL')
    if case == 'bool': content = payload(rc=False)
    if case in ('stock', 'market', 'missing', 'null'):
        obj = json.loads(content)
        if case == 'stock': obj['data']['code'] = '600919'
        if case == 'market': obj['data']['market'] = 0
        if case == 'missing': obj['data'].pop('klines')
        if case == 'null': obj['data']['klines'] = None
        content = json.dumps(obj).encode()
    if case == 'depth': content = b'[' * 30 + b']' * 30
    if case == 'duplicate_key': content = b'{"rc":0,"rc":0}'
    if case == 'nonfinite': content = payload(extra=float('nan'))
    if case == 'oversize': content = b'X' * (p.MAX_BYTES + 1)
    if case == 'conflict': content = payload([line('2026-11-13'), line('2026-11-13', amount='1')])
    if case == 'bad_date': content = payload([line('2026-11-99')])
    if case == 'bad_numeric': content = payload([line('2026-11-13', amount='NaN')])
    if case == 'percent': content = payload([line('2026-11-13', main='101')])
    if case == 'malicious': content = payload([line('2026-11-13', main='SECRET_SENTINEL')])
    with pytest.raises(p.Rejected) as caught:
        p.project_response(content, '601328.SH', '2026-11-06', '2026-11-13')
    assert 'SECRET_SENTINEL' not in str(caught.value)


@pytest.mark.parametrize('ticker,secid', [('601328.SH', '1.601328'), ('600919.SS', '1.600919'), ('000001.SZ','0.000001')])
def test_fixed_url_binds_exchange_and_prevents_arbitrary_input(ticker, secid):
    assert 'secid=' + secid + '&' in p.request_url(ticker, '1764599530176')
    with pytest.raises(p.FundflowStop): p.request_url(ticker + ';SECRET_SENTINEL')


def test_actual_dispatch_coordinator_uses_one_ps_send_no_legacy_or_statement_facts(tmp_path, monkeypatch):
    config = runtime(tmp_path)
    monkeypatch.setattr(config_module, '_config', config)
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'], skip_optional_fundamentals=True)
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    source = owner(tmp_path)
    send = Mock(return_value=(payload(), {'status': 'ok', 'http_status': 200, 'process_closed': True}))
    monkeypatch.setattr(p, 'run_child', send)
    legacy = Mock(side_effect=AssertionError('legacy request forbidden'))
    monkeypatch.setattr(akshare_data.ak, 'stock_individual_fund_flow', legacy)
    with p.powershell_fundflow_scope(source):
        dispatcher = fd.FinancialToolDispatcher.from_configured_vendors(instrument_identity=identity(), config=config)
        result = dispatcher.dispatch(request())
        repeated = dispatcher.dispatch(request().model_copy(update={'tool_call_id': 'second'}))
    assert result.artifact is not None and result.value == repeated.value
    assert send.call_count == 1 and legacy.call_count == 0
    assert budget.receipt()['data_attempts'] == 1 and budget.receipt()['model_attempts'] == 0
    assert '净占比%' in result.value and 'PowerShell' in result.value
    assert all(x not in result.value for x in ('987654321', '999999', 'Structured Monetary Source Facts', 'net_mf_amount'))
    assert source.value['amounts_and_prices'] == 'excluded_unit_unverified'
    # Public retention boundary: real coordinator must never encode raw bytes.
    import sqlite3, base64
    db = str(config['market_history_database_path']) + '.provider-requests.sqlite3'
    with sqlite3.connect(db) as connection:
        retained = '\n'.join(str(row[0]) for row in connection.execute(
            'SELECT result_payload FROM provider_request_sequences WHERE result_payload IS NOT NULL'))
    assert base64.b64encode(payload()).decode() not in retained
    assert '987654321' not in retained and '999999' not in retained


@pytest.mark.parametrize('failure', [False, True])
def test_one_owner_concurrent_consumers_share_success_or_failure(tmp_path, monkeypatch, failure):
    from concurrent.futures import ThreadPoolExecutor
    from tradingagents.dataflows.acquisition import AcquisitionFailure
    source = owner(tmp_path); source.bind_identity(identity())
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    send = Mock(return_value=(None if failure else payload(), {'status': 'request_failed' if failure else 'ok'}))
    monkeypatch.setattr(p, 'run_child', send)
    physical = Mock(side_effect=lambda operation, function: function())
    def call(_):
        try: return source.acquire('601328.SS', '2026-11-13', physical_request=physical)
        except AcquisitionFailure as e: return e.error_code
    with ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(call, range(3)))
    assert values[0] == values[1] == values[2]
    assert send.call_count == physical.call_count == budget.receipt()['data_attempts'] == 1


@pytest.mark.parametrize('kind', ['stock', 'date', 'cancel', 'budget', 'permission', 'http403', 'source', 'identity'])
def test_critical_failures_stop_without_fallback_or_additional_send(tmp_path, monkeypatch, kind):
    source = owner(tmp_path); source.bind_identity(identity())
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    send = Mock(return_value=(payload(), {'status': 'ok'}))
    monkeypatch.setattr(p, 'run_child', send)
    if kind == 'budget': budget._counts['data'] = 1
    if kind == 'cancel': monkeypatch.setattr(budget, 'check', Mock(side_effect=b.AnalysisBudgetStopped('cancelled')))
    if kind == 'permission': send.side_effect = PermissionError('SECRET_SENTINEL')
    if kind == 'http403': send.return_value = (None, {'status': 'http_error', 'http_status': 403})
    if kind == 'source': source.script.write_bytes(b'changed')
    if kind == 'identity': source.identity_sha = None
    with pytest.raises((p.FundflowStop, b.AnalysisBudgetStopped)):
        source.acquire('600919.SH' if kind == 'stock' else '601328.SH',
            '2026-11-12' if kind == 'date' else '2026-11-13', physical_request=lambda op, fn: fn())
    assert send.call_count == (1 if kind in ('permission', 'http403') else 0)


def observed(tmp_path, monkeypatch):
    source = owner(tmp_path); source.bind_identity(identity())
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    monkeypatch.setattr(p, 'run_child', Mock(return_value=(payload(), {'status': 'ok'})))
    source.acquire('601328.SH', '2026-11-13', physical_request=lambda op, fn: fn())
    return source.value


@pytest.mark.parametrize('field', ['counts', 'provider', 'received_at', 'extra', 'ratio', 'identity'])
def test_rehashed_untrusted_state_cannot_smuggle_metadata_or_semantics(tmp_path, monkeypatch, field):
    value = observed(tmp_path, monkeypatch)
    if field == 'counts': value['counts']['returned'] = 'SECRET_SENTINEL'
    if field == 'provider': value['provider'] = 'fabricated'
    if field == 'received_at': value['received_at'] = 'SECRET_SENTINEL'
    if field == 'extra': value['token'] = 'SECRET_SENTINEL'
    if field == 'ratio': value['rows'][0][1] = '200'
    if field == 'identity': value['identity_sha256'] = 'SECRET_SENTINEL'
    value['normalized_sha256'] = p._digest({k: v for k, v in value.items() if k != 'normalized_sha256'})
    text = p.render_observation(value, '601328.SH', '2026-11-13')
    assert text == '东方财富个股资金流：绑定无效，未采用。'
    assert 'SECRET_SENTINEL' not in text


@pytest.mark.parametrize('meta', [{'cleanup': 'close_failed'}, {'status': 'response_not_closed'}])
def test_response_close_failure_is_critical(tmp_path, monkeypatch, meta):
    source = owner(tmp_path); source.bind_identity(identity())
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    monkeypatch.setattr(p, 'run_child', Mock(return_value=(None, meta)))
    with pytest.raises(p.FundflowStop):
        source.acquire('601328.SH', '2026-11-13', physical_request=lambda op, fn: fn())


def test_existing_context_without_feature_still_works():
    from tradingagents.agents.utils.agent_utils import get_instrument_context_from_state
    assert '601328' in get_instrument_context_from_state({'company_of_interest': '601328.SH'})


def test_first_tool_call_in_empty_context_uses_captured_run_owner(tmp_path, monkeypatch):
    from contextvars import Context
    from langchain_core.messages import AIMessage
    from tradingagents.asset_configuration import RunAssetConfiguration
    from tradingagents.evidence import EvidenceState, capability_profile_for
    from tradingagents.strategy_registry import DEFAULT_DECISION_HORIZON, market_return_calculation_id
    from tradingagents.graph.financial_tools import FinancialDispatchToolNode
    config = runtime(tmp_path)
    monkeypatch.setattr(config_module, '_config', config)
    ident = identity()
    asset = RunAssetConfiguration(instrument_identity=ident, instrument_kind=ident.instrument_kind,
        registry_id='synthetic', registry_digest='a' * 64, reference_market='XSHG',
        capability_profile=capability_profile_for(ident.instrument_kind), observation_calendar_kind='market_sessions',
        calculation_id=market_return_calculation_id('qfq', ident.instrument_kind),
        strategy_rule_ids=tuple('market.return_20d.' + x for x in ('buy', 'hold', 'sell')),
        horizon=DEFAULT_DECISION_HORIZON, observations_required=21, adjustment_basis='qfq',
        registry_source_ref='synthetic:registry', registry_artifact='synthetic')
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'], skip_optional_fundamentals=True)
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    send = Mock(return_value=(payload(), {'status': 'ok'})); monkeypatch.setattr(p, 'run_child', send)
    source = owner(tmp_path)
    with p.powershell_fundflow_scope(source):
        node = FinancialDispatchToolNode(config=config)
    state = {'company_of_interest': '601328.SS', 'trade_date': '2026-11-13',
        'asset_configuration': asset.model_dump(mode='json'),
        'evidence_state': EvidenceState(instrument_identity=asset.instrument_identity).model_dump(mode='json'),
        'messages': [AIMessage(content='', tool_calls=[{'name': 'get_fundamentals', 'args': {
            'ticker': '601328.SS', 'curr_date': '2026-11-13'}, 'id': 'flow-1', 'type': 'tool_call'}])]}
    result = Context().run(node, state)
    assert send.call_count == 1 and result['eastmoney_fundflow_observation']['counts']['accepted'] == 1
    assert '净占比%' in result['messages'][0].content


def test_percentage_projection_reaches_prompt_and_complete_report_without_changing_rating(tmp_path, monkeypatch):
    import copy
    from tests.test_multiyear_integration import combined_state
    from tests.test_supplemental_presentation import MemoryStore, export
    from tradingagents import reporting
    from tradingagents.evidence import EvidenceState
    from tradingagents.agents.utils.agent_utils import get_instrument_context_from_state
    from tradingagents.terminal_contract import apply_terminal_contract
    state, evidence = combined_state()
    source = owner(tmp_path)
    source = p.PowerShellFundflow(source.executable, source.script, state['company_of_interest'], state['trade_date'])
    source.bind_identity(evidence.instrument_identity)
    obj = json.loads(payload([line(state['trade_date'])])); obj['data']['code'] = source.symbol[:6]
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    send = Mock(return_value=(json.dumps(obj).encode(), {'status': 'ok'})); monkeypatch.setattr(p, 'run_child', send)
    source.acquire(source.symbol, source.end, physical_request=lambda op, fn: fn())
    state['eastmoney_fundflow_observation'] = source.value
    prompt = get_instrument_context_from_state(state)
    assert '净占比%' in prompt and '987654321' not in prompt and '999999' not in prompt
    def audit(value, *args, **kwargs):
        apply_terminal_contract(value, audit_digest='b' * 64)
        value['decision_audit_created_at'] = '2026-07-18T00:00:00Z'
    monkeypatch.setattr(reporting, 'ReportStore', MemoryStore)
    monkeypatch.setattr(reporting, 'write_immutable_decision_audit', audit)
    full = export(copy.deepcopy(state), None, 'Chinese')
    plain = copy.deepcopy(state); plain.pop('eastmoney_fundflow_observation')
    baseline = export(plain, None, 'Chinese')
    assert full['1_analysts/eastmoney_fundflow.md'] in full['complete_report.md']
    assert full['5_portfolio/decision.md'] == baseline['5_portfolio/decision.md']
    assert '987654321' not in full['complete_report.md'].decode()
    assert send.call_count == 1 and EvidenceState.model_validate(state['evidence_state']) == evidence


@pytest.mark.parametrize('status', [429, 503])
def test_rate_limit_keeps_typed_coordinator_failure_no_retry(tmp_path, monkeypatch, status):
    from tradingagents.market_history import PhysicalAttemptFailure, PhysicalAttemptOutcome
    from tradingagents.dataflows.acquisition import AcquisitionFailure
    source = owner(tmp_path); source.bind_identity(identity())
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b, 'active_budget', lambda: budget)
    send = Mock(return_value=(None, {'status': 'http_error', 'http_status': status, 'cleanup': 'closed'}))
    monkeypatch.setattr(p, 'run_child', send)
    seen = []
    def physical(op, fn):
        try: return fn()
        except PhysicalAttemptFailure as failure:
            seen.append(failure)
            raise
    with pytest.raises(AcquisitionFailure):
        source.acquire('601328.SH', '2026-11-13', physical_request=physical)
    assert len(seen) == 1 and seen[0].outcome is PhysicalAttemptOutcome.RATE_LIMITED
    assert seen[0].retry_after_seconds == 300 and seen[0].retryable is False
    assert send.call_count == budget.receipt()['data_attempts'] == 1


@pytest.mark.parametrize('rows,expected', [([], 0), ([line('2026-11-14')], 0),
    ([line('2026-11-10'), line('2026-11-12'), line('2026-11-13')], 3)])
def test_empty_and_multirecord_windows_remain_explicit(rows, expected):
    result = p.project_response(payload(rows), '601328.SH', '2026-11-06', '2026-11-13')
    assert len(result['rows']) == expected and result['counts']['accepted'] == expected


def test_enabled_without_owner_does_not_silently_run_legacy(tmp_path):
    with pytest.raises(p.FundflowStop):
        fd.FinancialToolDispatcher.from_configured_vendors(instrument_identity=identity(), config=runtime(tmp_path))


def test_formal_scope_saves_safe_receipt_without_starting_child(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = entry()
    source = tmp_path / 'source'; (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/Get-PublicFundflow.ps1').write_text('# synthetic')
    exe = tmp_path / 'pwsh.exe'; exe.write_bytes(b'synthetic')
    session = module.Session(tmp_path, source=source, powershell_fundflow_executable=str(exe))
    session.selection = {'ticker': '601328.SH', 'analysis_date': '2026-11-13'}
    cli = SimpleNamespace(DEFAULT_CONFIG={}, get_user_selections=lambda: None,
                         _build_run_config=lambda: {}, _prepare_run_artifacts=lambda: {}, _write_run_reports=Mock())
    write = Mock(); monkeypatch.setattr(module, 'write_once', write)
    with session.analysis_hooks(cli, {}, {}):
        assert p.configured_owner({'bounded_powershell_fundflow_enabled': True,
                                  'analysis_skip_optional_fundamentals': True}) is not None
    assert write.call_args.args[1] == 'powershell-fundflow.json'
    assert write.call_args.args[2]['transport']['status'] == 'not_attempted'
    assert write.call_args.args[2]['raw_body_retained'] is False


@pytest.mark.parametrize("body, expected", [(payload(), "available_partial"), (None, "transport_unavailable")])
def test_bounded_fundflow_waits_for_published_report(tmp_path, monkeypatch, body, expected):
    from types import SimpleNamespace
    from threading import Event

    files = owner(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / files.script.name).write_bytes(files.script.read_bytes())
    session = entry().Session(tmp_path, source=tmp_path, powershell_fundflow_executable=files.executable)
    session.selection = {"ticker": "601328.SH", "analysis_date": "2026-11-13"}
    published = Event()
    report = tmp_path / "export" / "complete_report.md"

    def publish(*args):
        report.parent.mkdir()
        report.write_bytes(b"synthetic basic report")
        published.set()
        return report

    def child(*args, **kwargs):
        assert published.is_set() and report.read_bytes() == b"synthetic basic report"
        return body, {"status": "ok" if body else "transport_unavailable", "process_closed": True}

    cli = SimpleNamespace(DEFAULT_CONFIG={}, get_user_selections=Mock(), _build_run_config=Mock(),
                          _prepare_run_artifacts=Mock(), _write_run_reports=publish)
    cfg = runtime(tmp_path)
    budget = b.AnalysisBudget(1, 30, allowed_data_vendors=["akshare"], skip_optional_fundamentals=True)
    monkeypatch.setattr(b, "active_budget", lambda: budget)
    monkeypatch.setattr(config_module, "_config", cfg)
    send = Mock(side_effect=child)
    monkeypatch.setattr(p, "run_child", send)
    with session.analysis_hooks(cli, cfg, {}):
        dispatcher = fd.FinancialToolDispatcher.from_configured_vendors(instrument_identity=identity(), config=cfg)
        assert dispatcher.dispatch(request()).artifact is None
        send.assert_not_called()
        assert cli._write_run_reports({"terminal_outcome_kind": "trading_decision"}, "601328.SS", {}) == report
    receipt = json.loads((tmp_path / "powershell-fundflow.json").read_text())
    assert receipt["basic_export_id"] == report.parent.name and receipt["observation"]["status"] == expected
    assert report.read_bytes() == b"synthetic basic report" and cli._write_run_reports is publish
    assert send.call_count == budget.receipt()["data_attempts"] == 1


def test_bound_observation_cannot_be_reused_for_other_identity(tmp_path, monkeypatch):
    from tradingagents.evidence import EvidenceState
    observation = observed(tmp_path, monkeypatch)
    changed = identity().model_copy(update={'provenance': identity().provenance.model_copy(update={'artifact_sha256': 'b' * 64})})
    text = p.fundflow_markdown({'company_of_interest': '601328.SH', 'trade_date': '2026-11-13',
        'evidence_state': EvidenceState(instrument_identity=changed).model_dump(mode='json'),
        'eastmoney_fundflow_observation': observation})
    assert text == '东方财富个股资金流：绑定无效，未采用。'
