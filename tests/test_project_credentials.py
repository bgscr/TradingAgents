from __future__ import annotations

import importlib.util
import inspect
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'synthetic-tushare-only'
KEY = 'synthetic-deepseek-only'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def project(tmp_path, text=None):
    root = tmp_path / 'project'
    root.mkdir()
    (root / '.env').write_text(text if text is not None else
        f'TUSHARE_TOKEN={TOKEN}\nDEEPSEEK_API_KEY={KEY}\n', encoding='utf-8')
    return root


def test_entry_project_mode_loads_only_two_keys_without_prompt(tmp_path, monkeypatch, capsys):
    entry = module('analyze_mainland_bounded')
    source = project(tmp_path, f'TUSHARE_TOKEN="{TOKEN}"\nDEEPSEEK_API_KEY=\'{KEY}\'\n'
                     'TRADINGAGENTS_ANALYSIS_MAX_CALLS=999\nOTHER_SECRET=not-loaded\n')
    monkeypatch.setattr(entry, 'SOURCE', source)
    monkeypatch.setenv('TUSHARE_TOKEN', 'inherited-wrong')
    monkeypatch.delenv('OTHER_SECRET', raising=False)
    monkeypatch.delenv('TRADINGAGENTS_ANALYSIS_MAX_CALLS', raising=False)
    import getpass
    monkeypatch.setattr(getpass, 'getpass', lambda *a, **k: pytest.fail('unexpected hidden prompt'))
    entry.read_user_credentials(use_project_env=True)
    assert os.environ['TUSHARE_TOKEN'] == TOKEN
    assert os.environ['DEEPSEEK_API_KEY'] == KEY
    assert 'OTHER_SECRET' not in os.environ
    assert 'TRADINGAGENTS_ANALYSIS_MAX_CALLS' not in os.environ
    assert TOKEN not in capsys.readouterr().out


def test_default_keeps_hidden_input_and_does_not_read_project_env(tmp_path, monkeypatch):
    entry = module('analyze_mainland_bounded')
    monkeypatch.setattr(entry, 'SOURCE', tmp_path / 'does-not-exist')
    monkeypatch.setattr(entry.sys.stdin, 'isatty', lambda: True)
    import getpass
    answers = iter([TOKEN, KEY])
    monkeypatch.setattr(getpass, 'getpass', lambda *a, **k: next(answers))
    entry.read_user_credentials()
    assert os.environ['TUSHARE_TOKEN'] == TOKEN and os.environ['DEEPSEEK_API_KEY'] == KEY


@pytest.mark.parametrize('text,reason', [
    ('OTHER_SECRET=ignored\n', 'project_env_missing_key'),
    ('TUSHARE_TOKEN=x\nTUSHARE_TOKEN=y\nDEEPSEEK_API_KEY=z\n', 'project_env_duplicate_key'),
    ('TUSHARE_TOKEN=\nDEEPSEEK_API_KEY=z\n', 'project_env_invalid_value'),
    ('TUSHARE_TOKEN="space token"\nDEEPSEEK_API_KEY=z\n', 'project_env_invalid_value'),
    ('TUSHARE_TOKEN=${OTHER_SECRET}\nDEEPSEEK_API_KEY=z\n', 'project_env_interpolation_forbidden'),
    ('TUSHARE_TOKEN="unterminated\nDEEPSEEK_API_KEY=z\n', 'project_env_syntax'),
])
def test_invalid_input_has_safe_reason_no_fallback(tmp_path, text, reason, capsys):
    helper = module('project_credentials')
    source = project(tmp_path, text)
    with pytest.raises(helper.ProjectCredentialError, match=reason) as error:
        helper.read_project_credentials(source)
    assert error.value.__cause__ is None
    assert str(error.value) == reason
    assert not capsys.readouterr().out and not capsys.readouterr().err


def test_missing_fixed_file_does_not_search_parents_or_enterprise(tmp_path):
    helper = module('project_credentials')
    source = tmp_path / 'project'
    source.mkdir()
    (tmp_path / '.env').write_text(f'TUSHARE_TOKEN={TOKEN}\nDEEPSEEK_API_KEY={KEY}\n')
    (source / '.env.enterprise').write_text(f'TUSHARE_TOKEN={TOKEN}\nDEEPSEEK_API_KEY={KEY}\n')
    with pytest.raises(helper.ProjectCredentialError, match='project_env_unreadable'):
        helper.read_project_credentials(source)


def test_parser_accepts_dotenv_quotes_export_comments_without_loading_other_variables(tmp_path):
    helper = module('project_credentials')
    source = project(tmp_path, f'export TUSHARE_TOKEN="{TOKEN}" # retained\n'
                     f"DEEPSEEK_API_KEY='{KEY}'\nOTHER=x\n")
    assert helper.read_project_credentials(source) == {'TUSHARE_TOKEN': TOKEN, 'DEEPSEEK_API_KEY': KEY}


def test_finance_requests_only_tushare_and_does_not_require_model_key(tmp_path):
    helper = module('project_credentials')
    source = project(tmp_path, f'TUSHARE_TOKEN={TOKEN}\nDEEPSEEK_API_KEY=\n')
    assert helper.read_project_credentials(source, names=('TUSHARE_TOKEN',)) == {'TUSHARE_TOKEN': TOKEN}
    with pytest.raises(helper.ProjectCredentialError, match='project_env_key_scope'):
        helper.read_project_credentials(source, names=('OTHER_SECRET',))


def test_native_hardlink_rejected_before_secret_read(tmp_path):
    helper = module('project_credentials')
    source = project(tmp_path)
    os.link(source / '.env', tmp_path / 'linked-secret')
    with pytest.raises(helper.ProjectCredentialError, match='project_env_linked_path'):
        helper.read_project_credentials(source)


def test_reparse_ancestor_rejected_before_open(tmp_path, monkeypatch):
    from types import SimpleNamespace
    helper = module('project_credentials')
    source = project(tmp_path)
    original = Path.lstat
    def reparse(path):
        value = original(path)
        if path == source:
            return SimpleNamespace(st_file_attributes=0x400, st_mode=value.st_mode, st_nlink=1)
        return value
    monkeypatch.setattr(Path, 'lstat', reparse)
    with pytest.raises(helper.ProjectCredentialError, match='project_env_linked_path'):
        helper.read_project_credentials(source)


def test_file_changed_between_validation_and_open_is_rejected(tmp_path, monkeypatch):
    helper = module('project_credentials')
    source = project(tmp_path)
    original = os.open
    def swapped(path, flags, *args, **kwargs):
        if Path(path) == source / '.env':
            (source / '.env').rename(source / 'original-preserved')
            (source / '.env').write_text('TUSHARE_TOKEN=changed\nDEEPSEEK_API_KEY=changed\n')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(helper.os, 'open', swapped)
    with pytest.raises(helper.ProjectCredentialError, match='project_env_changed'):
        helper.read_project_credentials(source)


def test_oversize_and_directory_are_rejected(tmp_path):
    helper = module('project_credentials')
    source = project(tmp_path, 'X' * 65537)
    with pytest.raises(helper.ProjectCredentialError, match='project_env_size'):
        helper.read_project_credentials(source)
    other = tmp_path / 'other'
    other.mkdir()
    (other / '.env').mkdir()
    with pytest.raises(helper.ProjectCredentialError, match='project_env_not_regular'):
        helper.read_project_credentials(other)


def test_child_command_records_only_mode_and_never_credentials(tmp_path, monkeypatch):
    entry = module('analyze_mainland_bounded')
    root = entry.reserve_session(tmp_path / 'sessions')
    session = entry.Session(root, source=ROOT, use_project_env=True)
    session.selection = {'ticker': '601328.SS', 'analysis_date': '2026-10-16'}
    session.clock = entry.SharedSessionClock()
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        assert '--use-project-env' in command
        assert 'TUSHARE_TOKEN' not in kwargs['env'] and 'DEEPSEEK_API_KEY' not in kwargs['env']
        assert TOKEN not in json.dumps(command) and KEY not in json.dumps(command)
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(entry.subprocess, 'run', run)
    monkeypatch.setattr(session, 'verify_refresh', lambda: {'passed': True})
    assert session.refresh() == {'passed': True}
    assert len(commands) == 1


def test_power_shell_mode_is_explicit_and_default_remains_hidden():
    text = (ROOT / 'scripts/Start-MainlandAnalysis.ps1').read_text()
    assert '[switch]$UseProjectEnv' in text
    assert "if ($UseProjectEnv) { $entryArguments += '--use-project-env' }" in text
    entry = module('analyze_mainland_bounded')
    assert inspect.signature(entry.Session).parameters['use_project_env'].default is False


def test_credential_error_receipt_never_retains_untrusted_values(tmp_path):
    from types import SimpleNamespace
    entry = module('analyze_mainland_bounded')
    session = entry.Session(entry.reserve_session(tmp_path / 'runs'), source=ROOT)
    def fail():
        raise ValueError(TOKEN + KEY)
    session.execute(SimpleNamespace(get_user_selections=fail), {}, verify_sources=lambda: None)
    body = (session.root / 'result.json').read_text()
    assert TOKEN not in body and KEY not in body


def test_safe_error_codes_never_expose_arbitrary_exception_arguments():
    helper = module('project_credentials')
    assert helper.credential_failure_code(helper.ProjectCredentialError('project_env_missing_key')) == 'project_env_missing_key'
    assert helper.credential_failure_code(helper.ProjectCredentialError(TOKEN)) is None
    assert helper.credential_failure_code(ValueError(TOKEN)) is None


def valid_initialization_receipt(finance, *, project_env=False, role='live'):
    zero = dict.fromkeys(('network', 'credentials', 'subprocess', 'outside_write'), 0)
    return {'initialization_policy': {
        'contract': finance.AcceptanceInitializationProbe.POLICY, 'role': role, 'phase': 'complete',
        'implementation_sha256': finance.AcceptanceInitializationProbe.SOURCE_SHA256,
        'version_sha256': finance.AcceptanceInitializationProbe.VERSION_SHA256,
        'probe_finished': True, 'unexpected_denials': 0, 'first_unexpected_denial': None,
        'expected_denials': dict.fromkeys(('socket', 'environment', 'file'), 1 if role == 'reuse' else 0),
        'counts': {'function_calls': 1, 'socket_creations': 1, 'allowed_bind_attempts': 1, 'closed_sockets': 1}},
        'import_access': {**zero, **({'project_env_reads': 0} if project_env else {})},
        'guard_counts': {**zero, **({'project_env_reads': 1} if project_env else {})}}


def test_financial_receipt_verifier_explicitly_binds_credential_mode_and_counts():
    finance = module('accept_financial_statements')
    live = valid_initialization_receipt(finance, project_env=True)
    assert finance._initialization_verified(live, 'live', project_env=True)
    assert not finance._initialization_verified(live, 'live')
    assert not finance._initialization_verified(live, 'reuse', project_env=True)
    assert finance._initialization_verified(valid_initialization_receipt(finance), 'live')
    assert finance._initialization_verified(valid_initialization_receipt(finance, role='reuse'), 'reuse')
    for location, key, value in [('import_access', 'project_env_reads', 1),
                                 ('guard_counts', 'project_env_reads', 0),
                                 ('guard_counts', 'project_env_reads', 2),
                                 ('guard_counts', 'project_env_reads', True),
                                 ('guard_counts', 'credentials', 1),
                                 ('guard_counts', 'unknown', 0)]:
        changed = valid_initialization_receipt(finance, project_env=True)
        changed[location][key] = value
        assert not finance._initialization_verified(changed, 'live', project_env=True)


def test_financial_parser_rejects_other_destination_before_file_or_worker(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    finance = module('accept_financial_statements')
    source = tmp_path / 'source'
    monkeypatch.setattr(finance, 'SOURCE', source)
    monkeypatch.setattr(finance.sys, 'flags', SimpleNamespace(isolated=True, no_site=True))
    monkeypatch.setattr(finance.sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(finance.sys, 'argv', ['finance', '--output', str(source / 'reports/bounded-runs/a/refresh'),
                        '--destination', 'https://api.tushare.pro', '--account', 'synthetic',
                        '--ticker', '601328.SH', '--as-of', '2026-10-16',
                        '--shared-binding', str(tmp_path / 'never-read.json'),
                        '--authorize-live', '--use-project-env'])
    with pytest.raises(SystemExit) as error:
        finance.main()
    assert error.value.code == 2
    assert 'project env mode requires' in capsys.readouterr().err
    assert not source.exists()


def test_financial_worker_rejects_forged_destination_before_guard_or_secret(tmp_path, monkeypatch):
    from types import SimpleNamespace
    finance = module('accept_financial_statements')
    contract = {'sources': finance.identities(), 'live': True, 'credential_mode': 'project_env',
                'request': {'destination': 'https://api.tushare.pro', 'shared_binding': {'storage': {}}}}
    (tmp_path / 'contract.json').write_text(json.dumps(contract))
    monkeypatch.setattr(finance, 'SharedStatementStorage', lambda value: object())
    monkeypatch.setattr(finance, 'install', lambda *a, **k: pytest.fail('guard must not start for wrong destination'))
    assert finance.worker(SimpleNamespace(output=tmp_path, worker='live')) == 1
    result = json.loads((tmp_path / 'live.json').read_text())
    assert result['physical_requests'] == 0 and result['status'] == 'stopped'
    assert result['failure']['error_code'] == 'acceptance_initialization_failed'


def test_real_finance_coordinator_consumes_project_mode_receipts_without_relaxing_reuse(tmp_path, monkeypatch):
    from types import SimpleNamespace
    finance = module('accept_financial_statements')
    entry = module('analyze_mainland_bounded')
    source = tmp_path / 'synthetic-project'
    root = entry.reserve_session(source / 'reports/bounded-runs')
    output = root / 'refresh'
    monkeypatch.setattr(finance, 'SOURCE', source)
    original_allowed = finance.output_path_allowed
    monkeypatch.setattr(finance, 'output_path_allowed', lambda path: original_allowed(path, source))
    monkeypatch.setattr(finance, 'identities', lambda: {'synthetic_layout': 'fixed'})
    monkeypatch.setattr(finance, 'load_shared_binding', lambda *a, **k: {'synthetic': True})
    monkeypatch.setattr(finance.sys, 'flags', SimpleNamespace(isolated=True, no_site=True))
    monkeypatch.setattr(finance.sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(finance.sys, 'argv', ['finance', '--output', str(output),
                        '--destination', finance.PROJECT_ENV_DESTINATION, '--account', 'synthetic',
                        '--ticker', '601328.SH', '--as-of', '2026-10-16',
                        '--shared-binding', str(root / 'synthetic-binding.json'),
                        '--authorize-live', '--authorize-initialization-probes', '--use-project-env'])
    calls = []
    def transport(command, **kwargs):
        role = command[-1]
        calls.append(role)
        assert 'TUSHARE_TOKEN' not in kwargs['env'] and 'DEEPSEEK_API_KEY' not in kwargs['env']
        result = valid_initialization_receipt(finance, project_env=role == 'live', role=role)
        result.update(status='acquired' if role == 'live' else 'reused', pid=1 if role == 'live' else 2,
                      canonical_symbol='601328.SS', physical_requests=0, client_attempts=0,
                      views={str(i): {'checked_at': 'synthetic-fixed-time'} for i in range(6)},
                      guard_probes=dict.fromkeys(('socket', 'environment', 'file'), 'blocked'),
                      access_during_application=dict.fromkeys(('network', 'credentials', 'subprocess', 'outside_write'), 0))
        (output / (role + '.json')).write_text(json.dumps(result))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(finance.subprocess, 'run', transport)
    assert finance.main() == 0
    assert calls == ['live', 'reuse']
    assert json.loads((output / 'contract.json').read_text())['credential_mode'] == 'project_env'
    assert json.loads((output / 'result.json').read_text())['physical_requests'] == [0, 0]

