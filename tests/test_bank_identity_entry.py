"""Actual identity -> session -> finance config/cache paths, without real data."""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.diagnostic_runtime import SharedStatementStorage
from tests.test_bank_identity_acquisition import authority
from tests.test_bank_identity_snapshot import NOW, save_snapshot, snapshot, source_layout
from tests.test_bounded_mainland_entry import entry
from tests.test_financial_acceptance_scope import fake_https, sample_rows
from tradingagents import statement_acceptance as finance
from tradingagents.dataflows import bank_identity_snapshot as bank
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity


def setup(tmp_path, monkeypatch, *, near_expiry=False):
    source, run = source_layout(tmp_path)
    ref = save_snapshot(source, snapshot())
    moment = [NOW + timedelta(seconds=86390 if near_expiry else 10)]
    monkeypatch.setattr(bank, 'current_time', lambda now=None: moment[0] if now is None else now)
    module = entry()
    session = module.Session(run, source=source, bank_snapshot=ref)
    session.selection = {'ticker': '000123.SZ', 'analysis_date': '2026-10-16'}
    mono = [100.0]
    session.clock = module.SharedSessionClock(clock=lambda: mono[0], wall_clock=lambda: moment[0].timestamp())
    session.prepare_bank_identity()
    return source, run, session, moment, mono


def request_binding(source, run, session):
    from tradingagents.asset_configuration import resolve_run_asset_configuration
    asset = resolve_run_asset_configuration(session.selection['ticker'], config=session.configure({}))
    db = authority(source)
    state = db.stat()
    storage = {'market_database': str(source / 'data/market_history/market_history.sqlite3'),
        'coordinator_database': str(db), 'cache_database': str(source / 'data/cache/current-financial-statements.sqlite3'),
        'coordinator_file_id': [state.st_dev, state.st_ino]}
    binding = {'contract': 'shared-statement-acceptance-v1', 'account': 'synthetic-account',
        'destination': 'https://api.waditu.com/dataapi', 'as_of': '2026-10-16',
        'announcement_start': '2020-01-01', 'announcement_end': '2026-10-16',
        'normalizer_version': 'current-tushare-statements-v1',
        'instrument_identity_revision': ProviderSubrequestInstrumentIdentity.from_authoritative(asset.instrument_identity).identity_revision,
        'storage': storage, 'bank_identity': session.bank_identity}
    return binding, SharedStatementStorage(storage)


def test_entry_config_finance_acquire_reuse_share_new_bank_identity(tmp_path, monkeypatch):
    source, run, session, _, _ = setup(tmp_path, monkeypatch)
    binding, storage = request_binding(source, run, session)
    monkeypatch.setattr(finance, 'SOURCE_ROOT', source)
    calls = fake_https(monkeypatch, sample_rows)
    args = {'ticker': '000123.SZ', 'destination': 'https://api.waditu.com/dataapi', 'account': 'synthetic-account',
            'as_of': '2026-10-16', 'shared_binding': binding, 'shared_storage': storage}
    config, asset = finance.validate_request(run / 'refresh', **args)
    assert config['instrument_identity_registry_path'] == str(run / 'bank-identity-registry.json')
    assert ProviderSubrequestInstrumentIdentity.from_authoritative(asset.instrument_identity).identity_revision == binding['instrument_identity_revision']
    acquired = finance.acquire(run / 'refresh', token='public-synthetic', **args)
    reused = finance.reuse(run / 'refresh', **args)
    assert acquired['status'] == 'acquired' and acquired['physical_requests'] == len(calls) == 3
    assert reused['status'] == 'reused' and reused['physical_requests'] == reused['client_attempts'] == 0
    assert acquired['views'] == reused['views'] and len(reused['views']) == 6
    assert {c['params']['ts_code'] for c in calls} == {'000123.SZ'}
    configured = session.configure({'llm_max_output_tokens': 8192, 'analysis_max_calls': 99})
    assert configured['llm_max_output_tokens'] == 65536 and configured['deepseek_reasoning_effort'] == 'high'
    assert configured['analysis_max_calls'] == 60 and configured['llm_max_retries'] == 0
    assert configured['financial_cache_only'] is True


def test_fresh_process_recomputes_bound_snapshot_and_reopens_cache(tmp_path, monkeypatch):
    source, run, session, moment, _ = setup(tmp_path, monkeypatch)
    binding, storage = request_binding(source, run, session)
    monkeypatch.setattr(finance, 'SOURCE_ROOT', source)
    calls = fake_https(monkeypatch, sample_rows)
    first = finance.acquire(run / 'refresh', ticker='000123.SZ', as_of='2026-10-16',
        account='synthetic-account', destination='https://api.waditu.com/dataapi', token='public-synthetic',
        shared_binding=binding, shared_storage=storage)
    assert first['status'] == 'acquired' and len(calls) == 3
    handoff = run / 'synthetic-child-input.json'
    handoff.write_text(json.dumps({'binding': binding, 'time': bank.utc_text(moment[0])}))
    (source / 'tradingagents.local.json').write_text('{}')
    code = r'''
import json, os, sys
from pathlib import Path
from datetime import datetime
source, root, candidate = map(Path, sys.argv[1:])
for key in tuple(os.environ):
    if key.startswith('TRADINGAGENTS_') or any(x in key.upper() for x in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD')):
        os.environ.pop(key, None)
os.environ.update(PYTHON_DOTENV_DISABLED='1', TRADINGAGENTS_PROJECT_ROOT=str(source),
    TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE='0', LANGCHAIN_TRACING_V2='false',
    LANGCHAIN_TRACING='false', LANGSMITH_TRACING='false', OTEL_SDK_DISABLED='true')
counts = {'external_network': 0, 'credential_reads': 0}
def audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.sendto'}:
        address = args[0] if event == 'socket.getaddrinfo' else args[1]
        host = address[0] if isinstance(address, tuple) else address
        if host not in {'127.0.0.1', '::1', 'localhost'}:
            counts['external_network'] += 1
            raise RuntimeError('synthetic child external network forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        caller = sys._getframe(1)
        if (os.name == 'nt' and os.fsdecode(args[0]).casefold() == os.devnull.casefold()
                and args[2] in {os.O_RDWR, os.O_RDWR | getattr(os, 'O_NOINHERIT', 0)}
                and caller.f_code.co_name == '_get_devnull'
                and Path(caller.f_code.co_filename) == Path(sys.base_prefix) / 'Lib/subprocess.py'):
            return  # Standard read-only OS-version initialization; not a filesystem write.
        path = Path(os.fsdecode(args[0])).absolute()
        if path.name in {'.env', '.env.enterprise', '.netrc', '_netrc'}:
            counts['credential_reads'] += 1
            raise RuntimeError('credential file forbidden')
        if path.name == 'tradingagents.local.json' and not path.is_relative_to(source):
            raise RuntimeError('outside synthetic profile')
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) and not path.is_relative_to(source):
            raise RuntimeError('outside synthetic write')
    if event == 'sqlite3.connect':
        value = str(args[0])
        if value.startswith('file:'):
            from urllib.parse import unquote, urlsplit
            value = unquote(urlsplit(value).path).lstrip('/')
        if value != ':memory:' and not Path(value).absolute().is_relative_to(source):
            raise RuntimeError('outside synthetic DB')
sys.addaudithook(audit)
sys.path[:0] = [str(candidate), str(candidate / 'scripts')]
from tradingagents import statement_acceptance as finance
from tradingagents.dataflows import bank_identity_snapshot as bank
from scripts.diagnostic_runtime import SharedStatementStorage
value = json.loads((root / 'synthetic-child-input.json').read_text())
finance.SOURCE_ROOT = source  # synthetic installation layout, not a different identity algorithm
bank.current_time = lambda now=None: datetime.fromisoformat(value['time'].replace('Z','+00:00'))
result = finance.reuse(root / 'refresh', ticker='000123.SZ', as_of='2026-10-16',
    account='synthetic-account', destination='https://api.waditu.com/dataapi',
    shared_binding=value['binding'], shared_storage=SharedStatementStorage(value['binding']['storage']))
(root / 'synthetic-child-result.json').write_text(json.dumps({'result': result, 'counts': counts, 'pid': os.getpid()}))
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(source), str(run),
                             str(Path(__file__).resolve().parents[1])],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    second = json.loads((run / 'synthetic-child-result.json').read_text())
    assert second['pid'] != os.getpid() and second['counts'] == {'external_network': 0, 'credential_reads': 0}
    assert second['result']['status'] == 'reused'
    assert second['result']['physical_requests'] == second['result']['client_attempts'] == 0
    assert second['result']['views'] == first['views'] and len(first['views']) == 6


@pytest.mark.parametrize('mutation', ['hash', 'expiry', 'symbol', 'session', 'path', 'missing', 'ticker', 'as_of'])
def test_worker_rederives_rejects_forged_parent_before_financial_wire(tmp_path, monkeypatch, mutation):
    source, run, session, _, _ = setup(tmp_path, monkeypatch)
    binding, storage = request_binding(source, run, session)
    monkeypatch.setattr(finance, 'SOURCE_ROOT', source)
    calls = fake_https(monkeypatch, sample_rows)
    args = {'ticker': '000123.SZ', 'as_of': '2026-10-16'}
    if mutation == 'hash':
        binding['bank_identity']['registry_sha256'] = '0' * 64
    elif mutation == 'expiry':
        binding['bank_identity']['expires_at'] = bank.utc_text(NOW + timedelta(days=10))
    elif mutation == 'symbol':
        binding['bank_identity']['symbol'] = '600123.SS'
    elif mutation == 'session':
        binding['bank_identity']['session_id'] = 'd' * 32
    elif mutation == 'path':
        binding['bank_identity']['registry_path'] = 'untrusted'
    elif mutation == 'missing':
        binding.pop('bank_identity')
    elif mutation == 'ticker':
        args['ticker'] = '000123.SH'
    else:
        args['as_of'] = '2026-10-15'
    with pytest.raises((ValueError, RuntimeError)):
        finance.validate_request(run / 'refresh', **args, destination='https://api.waditu.com/dataapi',
                                 account='synthetic-account', shared_binding=binding, shared_storage=storage)
    assert calls == []


def test_snapshot_expiry_shortens_both_clocks_and_never_renews(tmp_path, monkeypatch):
    source, run, session, moment, mono = setup(tmp_path, monkeypatch, near_expiry=True)
    assert session.clock.remaining() == 10
    deadline = session.clock.deadline_monotonic
    session.clock.shorten_to(session.clock.deadline_epoch + 1000)
    assert session.clock.deadline_monotonic == deadline and session.clock.remaining() == 10
    moment[0] -= timedelta(hours=1)  # wall rollback does not restore monotonic time
    mono[0] += 10
    with pytest.raises(RuntimeError, match='session_deadline_exceeded'):
        session.check_bank_identity()
    moment[0] = NOW + timedelta(days=1)
    with pytest.raises(bank.BankIdentityError, match='bank_snapshot_expired'):
        session.check_bank_identity()


def test_shared_binding_parser_preserves_closed_bank_identity_for_worker_validation(tmp_path, monkeypatch):
    from scripts.accept_financial_statements import load_shared_binding
    source, run, session, _, _ = setup(tmp_path, monkeypatch)
    binding, _ = request_binding(source, run, session)
    path = run / 'shared-binding.json'
    path.write_text(json.dumps(binding))
    assert load_shared_binding(path, ticker='000123.SZ', as_of='2026-10-16',
        account='synthetic-account', destination='https://api.waditu.com/dataapi') == binding
    binding['extra'] = 'not allowed'
    path.write_text(json.dumps(binding))
    with pytest.raises(ValueError):
        load_shared_binding(path, ticker='000123.SZ', as_of='2026-10-16',
            account='synthetic-account', destination='https://api.waditu.com/dataapi')



def test_exact_identity_entry_scopes_confirmation_and_selected_credential(tmp_path, monkeypatch, capsys):
    from scripts import update_bank_identity as update_entry
    from tests.test_bank_identity_acquisition import wire
    from tests.test_bank_identity_diagnostic_entry import entry_context
    source, checks, prompts = entry_context(tmp_path, monkeypatch,
        ['--refresh-security', '600919.SS', '--use-project-env'],
        confirmation='UPDATE_BANK_IDENTITY:600919.SH')
    calls, _, _ = wire(monkeypatch, exact_symbol='600919.SH')
    assert update_entry.main() == 0
    output = capsys.readouterr().out
    from tests.test_bank_identity_acquisition import TOKEN
    assert TOKEN not in output and 'synthetic-never-selected' not in output
    result = json.loads(output.splitlines()[-1])
    assert checks and len(prompts) == len(calls) == 1
    assert 'UPDATE_BANK_IDENTITY:600919.SH' in prompts[0]
    assert result['status'] == 'exact_positive' and result['maximum_requests'] == 1
    assert (source / 'data/instrument_identity/stock_basic' / result['snapshot_id'] / 'READY.json').is_file()


@pytest.mark.parametrize('symbol,confirmation', [
    ('600919.SH', 'UPDATE_BANK_IDENTITIES'), ('600919.SH', 'UPDATE_BANK_IDENTITY:000123.SZ'),
    ('600919', 'UPDATE_BANK_IDENTITY:600919'), ('600919.BJ', 'UPDATE_BANK_IDENTITY:600919.BJ'),
])
def test_exact_entry_invalid_identity_or_confirmation_precedes_credentials(tmp_path, monkeypatch, symbol, confirmation):
    import project_credentials

    from scripts import update_bank_identity as update_entry
    from tests.test_bank_identity_acquisition import wire
    from tests.test_bank_identity_diagnostic_entry import entry_context
    source, _, _ = entry_context(tmp_path, monkeypatch,
        ['--refresh-security', symbol, '--use-project-env'], confirmation=confirmation)
    monkeypatch.setattr(project_credentials, 'read_project_credentials',
                        lambda *args, **kwargs: pytest.fail('credential read before bound confirmation'))
    calls, _, _ = wire(monkeypatch, exact_symbol='600919.SH')
    with pytest.raises((RuntimeError, bank.BankIdentityError)):
        update_entry.main()
    assert not calls and not (source / 'data/instrument_identity').exists()


@pytest.mark.parametrize('other', ['--refresh', '--diagnose-schema'])
def test_exact_entry_cannot_combine_capture_modes(monkeypatch, other):
    from scripts import update_bank_identity as update_entry
    monkeypatch.setattr(update_entry.sys, 'argv', ['update_bank_identity.py', '--refresh-security', '600919.SH', other])
    with pytest.raises(SystemExit) as stopped:
        update_entry.main()
    assert stopped.value.code == 2


@pytest.mark.parametrize('symbol', ['600919.SH', '000123.SZ'])
def test_exact_identity_reaches_financial_acquire_and_zero_network_reuse(tmp_path, monkeypatch, symbol):
    from scripts import analyze_mainland_bounded as bounded_entry
    from tests.test_bank_identity_snapshot import exact_snapshot, save_exact_snapshot
    source, run = source_layout(tmp_path)
    moment = NOW + timedelta(seconds=10)
    ref = save_exact_snapshot(source, exact_snapshot(symbol))
    monkeypatch.setattr(bank, 'current_time', lambda now=None: moment if now is None else now)
    session = bounded_entry.Session(run, source=source, bank_snapshot=ref)
    session.selection = {'ticker': symbol, 'analysis_date': '2026-10-16'}
    session.clock = bounded_entry.SharedSessionClock(clock=lambda: 1.0, wall_clock=lambda: moment.timestamp())
    session.prepare_bank_identity()
    config = session.configure({})
    assert config['financial_cache_only'] and config['analysis_max_calls'] == 60
    assert config['llm_max_output_tokens'] == 65536 and config['deepseek_reasoning_effort'] == 'high'
    # The same production statement validation entry used in each native worker.
    from tradingagents import statement_acceptance
    monkeypatch.setattr(statement_acceptance, 'SOURCE_ROOT', source)
    shared, storage = request_binding(source, run, session)
    shared['as_of'] = '2026-10-16'
    args = {'ticker': symbol, 'destination': shared['destination'], 'account': shared['account'],
            'as_of': shared['as_of'], 'shared_binding': shared, 'shared_storage': storage}
    validated, _ = statement_acceptance.validate_request(run / 'refresh', **args)
    assert validated['instrument_identity_registry_sha256'] == session.bank_identity['registry_sha256']
    calls = fake_https(monkeypatch, sample_rows)
    acquired = statement_acceptance.acquire(run / 'refresh', token='public-synthetic', **args)
    reused = statement_acceptance.reuse(run / 'refresh', **args)
    assert acquired['status'] == 'acquired' and acquired['physical_requests'] == len(calls) == 3
    assert reused['status'] == 'reused' and reused['physical_requests'] == reused['client_attempts'] == 0
    assert acquired['views'] == reused['views'] and len(reused['views']) == 6
    assert {call['params']['ts_code'] for call in calls} == {symbol}
