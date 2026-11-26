"""Ordinary single-stock entry with synthetic UI/transport and owned local fixtures."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def entry():
    spec = importlib.util.spec_from_file_location('bounded_entry', ROOT / 'scripts/analyze_mainland_bounded.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_invocations_have_fresh_identity_and_preserve_stopped_history(tmp_path):
    module = entry()
    first = module.reserve_session(tmp_path / 'bounded-runs')
    (first / 'analysis01.result.json').write_text(json.dumps({'status': 'stopped', 'model_attempts': 2}))
    before = (first / 'analysis01.result.json').read_bytes()
    second = module.reserve_session(tmp_path / 'bounded-runs')
    assert first != second
    assert (first / 'analysis01.result.json').read_bytes() == before
    assert not (second / 'checkpoint-state').exists()
    assert not (second / 'analysis01.result.json').exists()


def test_collision_never_renews_or_removes_an_existing_session(tmp_path, monkeypatch):
    module = entry()
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: type('Id', (), {'hex': 'a' * 32})())
    first = module.reserve_session(tmp_path / 'bounded-runs')
    (first / 'consumed.json').write_text('{"used":true}')
    with pytest.raises(FileExistsError):
        module.reserve_session(tmp_path / 'bounded-runs')
    assert (first / 'consumed.json').read_text() == '{"used":true}'


def test_shared_clock_charges_finance_and_rejects_expiry_without_renewal():
    module = entry()
    now = [100.0, 1000.0]
    clock = module.SharedSessionClock(clock=lambda: now[0], wall_clock=lambda: now[1])
    now[:] = [280.0, 1180.0]
    assert clock.remaining() == 1020
    now[:] = [1300.0, 999.0]  # wall rollback must not restore the spent time
    with pytest.raises(module.Stop, match='session_deadline_exceeded'):
        clock.remaining()


def test_selection_retains_user_stock_and_date_but_rejects_changed_scope():
    module = entry()
    selection = {
        'ticker': '600895.SS', 'asset_type': 'stock', 'analysis_date': '2026-10-15',
        'research_depth': 1, 'llm_provider': 'deepseek',
        'backend_url': 'https://api.deepseek.com', 'shallow_thinker': 'deepseek-flash',
        'deep_thinker': 'deepseek-v4-pro', 'output_language': 'Chinese',
        'china_a_enhancement_preset': 'basic',
        'analysts': [type('A', (), {'value': value})()
                     for value in ('market', 'social', 'news', 'fundamentals')],
    }
    module.validate_selection(selection)
    selection['ticker'] = 'AAPL'
    with pytest.raises(module.Stop, match='mainland_equity_required'):
        module.validate_selection(selection)


def test_release_drift_fails_before_menu_or_credentials(tmp_path):
    module = entry()
    source = tmp_path / 'installed'
    source.mkdir()
    dep = source / 'file.py'
    dep.write_text('reviewed')
    import hashlib
    manifest = {'contract': 'bounded-mainland-release-v1', 'deployment_root': str(source),
                'files': [{'root': 'project', 'relative': 'file.py',
                           'sha256': hashlib.sha256(dep.read_bytes()).hexdigest()}]}
    module.verify_release(manifest, source=source, python_root=tmp_path)
    dep.write_text('unknown edit')
    with pytest.raises(module.Stop, match='bound_source_changed'):
        module.verify_release(manifest, source=source, python_root=tmp_path)
    with pytest.raises(module.Stop, match='installation_root_mismatch'):
        module.verify_release(manifest, source=tmp_path, python_root=tmp_path)


def test_release_reads_only_bound_runtime_files_and_rejects_missing_dependency(tmp_path, monkeypatch):
    import hashlib
    module = entry()
    source = tmp_path / 'installed'
    source.mkdir()
    dep = source / 'runtime.py'
    dep.write_text('reviewed')
    manifest = {'contract': 'bounded-mainland-release-v1', 'deployment_root': str(source),
                'files': [{'root': 'project', 'relative': 'runtime.py',
                           'sha256': hashlib.sha256(dep.read_bytes()).hexdigest()}]}
    original = Path.read_bytes
    def exact_only(path):
        assert path == dep  # Unbound tests/output trees must not be enumerated or read.
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', exact_only)
    module.verify_release(manifest, source=source, python_root=tmp_path)
    manifest['files'][0]['relative'] = 'absent.py'
    with pytest.raises(module.Stop, match='missing_bound_file'):
        module.verify_release(manifest, source=source, python_root=tmp_path)


@pytest.mark.parametrize('relative', ['../outside.py', '.env'])
def test_release_rejects_escape_or_secret_before_read(tmp_path, monkeypatch, relative):
    module = entry()
    manifest = {'contract': 'bounded-mainland-release-v1', 'deployment_root': str(tmp_path),
                'files': [{'root': 'project', 'relative': relative, 'sha256': '0' * 64}]}
    def no_read(path):
        pytest.fail('invalid dependency must not be read')
    monkeypatch.setattr(Path, 'read_bytes', no_read)
    with pytest.raises(module.Stop, match='invalid_bound_path'):
        module.verify_release(manifest, source=tmp_path, python_root=tmp_path)


def test_installed_release_contains_no_test_output_or_consumed_packet_dependencies():
    manifest = json.loads((ROOT / 'config/bounded_mainland_release.json').read_text())
    assert manifest['old_authorization_reused'] is False
    for item in manifest['files']:
        relative = item['relative'].casefold()
        assert not relative.startswith(('tests/', 'reports/', 'data/'))
        assert 'pytest-temp' not in relative and 'prepared' not in relative


def test_effective_config_is_session_scoped_after_profile_merge(tmp_path):
    module = entry()
    session = module.Session(module.reserve_session(tmp_path / 'sessions'), source=ROOT)
    original = {'analysis_max_calls': 999, 'llm_max_output_tokens': 8192,
                'deepseek_reasoning_effort': 'none', 'llm_max_retries': 4,
                'results_dir': 'old', 'checkpoint_data_dir': 'old'}
    value = session.configure(original)
    assert original['analysis_max_calls'] == 999
    assert value['analysis_max_calls'] == 60 and value['llm_max_retries'] == 0
    assert value['deepseek_reasoning_effort'] == 'high' and value['llm_max_output_tokens'] == 65536
    assert value['analysis_timeout_seconds'] == 1200
    assert Path(value['checkpoint_data_dir']).is_relative_to(session.root)
    assert Path(value['data_cache_dir']) == ROOT / 'data/cache'


def test_finance_only_accepts_precise_owned_fresh_report_child(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    import accept_financial_statements as finance
    module = entry()
    source = tmp_path / 'project'
    root = module.reserve_session(source / 'reports/bounded-runs')
    assert finance.output_path_allowed(root / 'refresh', source)
    assert not finance.output_path_allowed(root / 'other', source)
    assert not finance.output_path_allowed(source / 'data/refresh', source)
    (root / 'refresh').mkdir()
    assert not finance.output_path_allowed(root / 'refresh', source)


def test_forged_or_missing_session_record_cannot_open_source_write_exception(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    import accept_financial_statements as finance
    root = tmp_path / 'project/reports/bounded-runs' / ('a' * 32)
    root.mkdir(parents=True)
    assert not finance.output_path_allowed(root / 'refresh', tmp_path / 'project')
    (root / 'session.json').write_text('{"contract":"wrong"}')
    assert not finance.output_path_allowed(root / 'refresh', tmp_path / 'project')


def test_finance_output_cannot_traverse_from_external_path_back_into_source(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    import accept_financial_statements as finance
    source = tmp_path / 'project'
    source.mkdir()
    assert not finance.output_path_allowed(tmp_path / 'external' / '..' / 'project/data/refresh', source)


@pytest.mark.parametrize('suffix', ['.', ' '])
@pytest.mark.skipif(os.name != 'nt', reason='Windows filesystem alias semantics')
def test_finance_output_rejects_win32_aliases_of_source(tmp_path, suffix):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    import accept_financial_statements as finance
    source = tmp_path / 'project'
    source.mkdir()
    alias = tmp_path / ('project' + suffix)
    assert alias.samefile(source)  # Actual Windows lookup, no writes through the alias.
    assert not finance.output_path_allowed(alias / 'data/fresh', source)


def test_selection_uses_mainland_calendar_day_not_computer_timezone(monkeypatch):
    from datetime import date, datetime
    module = entry()
    class LocalDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 1)
    class MainlandNow(datetime):
        @classmethod
        def now(cls, tz=None):
            assert str(tz) == 'Asia/Shanghai'
            return cls(2026, 11, 2, 1, tzinfo=tz)
    monkeypatch.setattr(module, 'date', LocalDate)
    monkeypatch.setattr(module, 'datetime', MainlandNow, raising=False)
    selection = {'ticker':'601328.SS', 'asset_type':'stock', 'analysis_date':'2026-11-02',
                 'research_depth':1, 'llm_provider':'deepseek', 'backend_url':'https://api.deepseek.com',
                 'shallow_thinker':'deepseek-flash', 'deep_thinker':'deepseek-v4-pro',
                 'output_language':'Chinese', 'china_a_enhancement_preset':'basic',
                 'analysts':[type('A', (), {'value':x})() for x in ('market','social','news','fundamentals')]}
    module.validate_selection(selection)
    selection['analysis_date'] = '2026-11-03'
    with pytest.raises(module.Stop, match='future_analysis_date'):
        module.validate_selection(selection)


def test_untrusted_exception_text_is_not_written_to_lifecycle_receipt(tmp_path):
    from types import SimpleNamespace
    module = entry()
    session = module.Session(module.reserve_session(tmp_path / 'sessions'), source=ROOT)
    def fail():
        raise RuntimeError('Authorization: Bearer synthetic-do-not-record; token=synthetic-value')
    cli = SimpleNamespace(get_user_selections=fail)
    value = session.execute(cli, {'analysis_budget_enabled': False}, verify_sources=lambda: None)
    encoded = (session.root / 'result.json').read_text()
    assert value['status'] == 'stopped' and value['exception_type'] == 'RuntimeError'
    assert 'Authorization' not in encoded and 'synthetic-do-not-record' not in encoded
    assert 'synthetic-value' not in encoded


def test_required_diagnostic_write_failure_is_propagated(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = entry()
    session = module.Session(module.reserve_session(tmp_path / 'sessions'), source=ROOT)
    def fail():
        raise ValueError('synthetic')
    def denied(*args, **kwargs):
        raise OSError('synthetic receipt unavailable')
    monkeypatch.setattr(module, 'write_once', denied)
    with pytest.raises(OSError, match='synthetic receipt unavailable'):
        session.execute(SimpleNamespace(get_user_selections=fail), {'analysis_budget_enabled': False},
                        verify_sources=lambda: None)


def test_explicit_arguments_preserve_requested_stock_and_date():
    module = entry()
    args = module.parse_arguments(['--ticker', '600919.SH', '--analysis-date', '2026-11-01'])
    assert args.ticker == '600919.SH'
    assert args.analysis_date == '2026-11-01'


@pytest.mark.parametrize('is_tty, explicit, project_env, reason', [
    (False, True, True, 'not_confirmed'),
    (False, True, False, 'interactive_terminal_required'),
    (False, False, True, 'interactive_terminal_required'),
    (False, False, False, 'interactive_terminal_required'),
    (True, True, False, 'not_confirmed'),
    (True, False, True, 'not_confirmed'),
])
def test_redirected_input_requires_explicit_selection_and_project_env(
        tmp_path, monkeypatch, is_tty, explicit, project_env, reason):
    import ctypes
    from types import SimpleNamespace

    module = entry()
    monkeypatch.setattr(module, 'SOURCE', tmp_path / 'project')
    monkeypatch.setattr(module.sys, 'executable', str(module.SOURCE / '.venv/Scripts/python.exe'))
    arguments = ['entry']
    if explicit:
        arguments += ['--ticker', '600919.SH', '--analysis-date', '2026-11-01']
    if project_env:
        arguments += ['--use-project-env']
    monkeypatch.setattr(module.sys, 'argv', arguments)
    monkeypatch.setattr(module.sys, 'stdin', SimpleNamespace(isatty=lambda: is_tty))
    monkeypatch.setattr(ctypes.windll.shell32, 'IsUserAnAdmin', lambda: False)
    monkeypatch.setattr(module, 'read_json', lambda _path: {})
    monkeypatch.setattr(module, 'verify_release', lambda _manifest: None)
    prompts = []
    def decline(prompt):
        prompts.append(prompt)
        return 'NO'
    monkeypatch.setattr('builtins.input', decline)
    def forbidden(*args, **kwargs):
        pytest.fail('rejected input or START must not create state or read credentials')
    monkeypatch.setattr(module, 'reserve_session', forbidden)
    monkeypatch.setattr(module, 'read_user_credentials', forbidden)
    with pytest.raises(module.Stop, match='^' + reason + '$'):
        module.main()
    assert bool(prompts) == (reason == 'not_confirmed')


@pytest.mark.parametrize('answer', ['', 'start', 'NO', None])
def test_explicit_mode_still_requires_exact_start_before_state_or_credentials(tmp_path, monkeypatch, answer):
    import ctypes
    from types import SimpleNamespace

    module = entry()
    monkeypatch.setattr(module, 'SOURCE', tmp_path / 'project')
    monkeypatch.setattr(module.sys, 'executable', str(module.SOURCE / '.venv/Scripts/python.exe'))
    monkeypatch.setattr(module.sys, 'argv', ['entry', '--ticker', '600919.SH', '--analysis-date', '2026-11-01'])
    monkeypatch.setattr(module.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ctypes.windll.shell32, 'IsUserAnAdmin', lambda: False)
    monkeypatch.setattr(module, 'read_json', lambda _path: {})
    monkeypatch.setattr(module, 'verify_release', lambda _manifest: None)
    def answer_start(_prompt):
        if answer is None:
            raise EOFError
        return answer
    monkeypatch.setattr('builtins.input', answer_start)
    def forbidden(*args, **kwargs):
        pytest.fail('unconfirmed session must not reserve state or read credentials')
    monkeypatch.setattr(module, 'reserve_session', forbidden)
    monkeypatch.setattr(module, 'read_user_credentials', forbidden)
    with pytest.raises(EOFError if answer is None else module.Stop):
        module.main()

def test_explicit_selection_uses_real_analysts_and_never_constructs_questionary(monkeypatch):
    import questionary

    from cli.models import AnalystType
    module = entry()
    def forbidden(*args, **kwargs):
        pytest.fail('explicit selection must not construct a screen-buffer menu')
    monkeypatch.setattr(questionary, 'text', forbidden)
    selected = module.build_explicit_selection('600919.SH', '2026-11-01', {
        'google_thinking_level': None, 'openai_reasoning_effort': None,
        'anthropic_effort': None,
    })
    module.validate_selection(selected)
    assert selected['ticker'] == '600919.SS'
    assert selected['analysis_date'] == '2026-11-01'
    assert selected['analysts'] == [AnalystType.MARKET, AnalystType.SOCIAL,
                                    AnalystType.NEWS, AnalystType.FUNDAMENTALS]
    assert selected['deepseek_reasoning_effort'] == 'high'
    assert selected['research_depth'] == 1 and selected['output_language'] == 'Chinese'

@pytest.mark.parametrize('arguments, reason', [
    (['--ticker', '600919.SH'], 'selection_arguments_must_be_paired'),
    (['--analysis-date', '2026-11-01'], 'selection_arguments_must_be_paired'),
    (['--ticker', '', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker'),
    (['--ticker', '600919.SH', '--analysis-date', ''], 'invalid_explicit_analysis_date'),
    (['--ticker', '600919', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker'),
    (['--ticker', '600919.BJ', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker'),
    (['--ticker', '600919.SH ', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker'),
    (['--ticker', '６００９１９.SH', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker'),
    (['--ticker', '600919.SH', '--analysis-date', '2026-2-1'], 'invalid_explicit_analysis_date'),
    (['--ticker', '600919.SH', '--analysis-date', '2026-02-30'], 'invalid_explicit_analysis_date'),
    (['--ticker', '600919.SH', '--analysis-date', '9999-01-01'], 'future_analysis_date'),
])
def test_explicit_arguments_reject_incomplete_or_invalid_scope(arguments, reason):
    module = entry()
    with pytest.raises(module.Stop, match=reason):
        module.parse_arguments(arguments)


def test_no_arguments_preserve_menu_and_check_only_never_fills_a_selection():
    module = entry()
    args = module.parse_arguments([])
    assert args.ticker is None and args.analysis_date is None
    checked = module.parse_arguments(['--check-only', '--ticker', '600919.SS',
                                     '--analysis-date', '2026-11-01'])
    assert checked.check_only and checked.analysis_date == '2026-11-01'
    with pytest.raises(module.Stop, match='selection_arguments_must_be_paired'):
        module.parse_arguments(['--check-only', '--ticker', '600919.SH'])


@pytest.mark.parametrize('ticker, expected', [('600919.SH','600919.SS'), ('600919.SS','600919.SS'),
                                              ('000001.SZ','000001.SZ')])
def test_explicit_selection_preserves_exchange_alias_without_claiming_identity(ticker, expected):
    module = entry()
    value = module.build_explicit_selection(ticker, '2026-11-01', {})
    module.validate_selection(value)
    assert value['ticker'] == expected
    assert 'instrument_identity' not in value
    assert 'bank_identity' not in value
