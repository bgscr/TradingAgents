"""Actual argparse/entry/credential/capture dispatch; only environment and HTTP are synthetic."""
import ctypes
import hashlib
import importlib
import io
import json
import sys
from pathlib import Path

import project_credentials
import pytest

from scripts import update_bank_identity as entry
from tests.test_bank_identity_acquisition import TOKEN, Time, authority
from tests.test_bank_identity_schema_diagnostics import fake_wire
from tests.test_bank_identity_snapshot import source_layout
from tradingagents.dataflows import bank_identity_acquisition as acquire

PROJECT = Path(__file__).resolve().parents[1]


def entry_context(tmp_path, monkeypatch, arguments, *, confirmation=None):
    source, _ = source_layout(tmp_path)
    authority(source)
    (source / '.env').write_text('TUSHARE_TOKEN=' + TOKEN + '\nDEEPSEEK_API_KEY=synthetic-never-selected\n',
                               encoding='utf-8')
    support = importlib.import_module('analyze_mainland_bounded')
    checks, confirmations = [], []
    # Synthetic installation layout. The actual release hashes are checked separately below.
    monkeypatch.setattr(support, 'verify_release', lambda manifest: checks.append(manifest))
    monkeypatch.setattr(entry, 'SOURCE', source)
    monkeypatch.setattr(entry.sys, 'executable', str(source / '.venv/Scripts/python.exe'))
    monkeypatch.setattr(entry.sys, 'argv', ['update_bank_identity.py', *arguments])
    monkeypatch.setattr(ctypes.windll.shell32, 'IsUserAnAdmin', lambda: 0)
    terminal = io.StringIO()
    monkeypatch.setattr(terminal, 'isatty', lambda: True)
    monkeypatch.setattr(entry.sys, 'stdin', terminal)
    def confirm(prompt):
        confirmations.append(prompt)
        return confirmation
    monkeypatch.setattr('builtins.input', confirm)
    clock = Time().bound()
    monkeypatch.setattr(acquire, 'CaptureClock', lambda: clock)
    return source, checks, confirmations


@pytest.mark.parametrize('mode,confirmation,expected_count', [
    ('--diagnose-schema', 'DIAGNOSE_BANK_SCHEMA', 1),
    ('--refresh', 'UPDATE_BANK_IDENTITIES', 6),
])
def test_explicit_mode_reaches_real_capture_with_selected_project_token(
        tmp_path, monkeypatch, capsys, mode, confirmation, expected_count):
    source, checks, prompts = entry_context(
        tmp_path, monkeypatch, [mode, '--use-project-env'], confirmation=confirmation)
    original, selected = project_credentials.read_project_credentials, []
    def read_selected(root, *, names):
        selected.append((root, names))
        return original(root, names=names)
    monkeypatch.setattr(project_credentials, 'read_project_credentials', read_selected)
    calls, _ = fake_wire(monkeypatch)
    assert entry.main() == 0
    assert checks and len(prompts) == 1 and confirmation in prompts[0]
    assert selected == [(source, ('TUSHARE_TOKEN',))]
    assert len(calls) == expected_count
    output = capsys.readouterr().out
    assert TOKEN not in output and 'synthetic-never-selected' not in output
    result = json.loads(output.splitlines()[-1])
    assert result['maximum_requests'] == expected_count
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert (root / 'READY.json').exists() is (mode == '--refresh')
    assert result['status'] == ('complete' if mode == '--refresh' else 'diagnostic_complete')


def test_manual_hidden_mode_retained(tmp_path, monkeypatch, capsys):
    _, _, _ = entry_context(tmp_path, monkeypatch, ['--diagnose-schema'],
                            confirmation='DIAGNOSE_BANK_SCHEMA')
    monkeypatch.setattr(project_credentials, 'read_project_credentials',
                        lambda *args, **kwargs: pytest.fail('project credentials not selected'))
    prompts = []
    monkeypatch.setattr(entry.getpass, 'getpass', lambda prompt: prompts.append(prompt) or TOKEN)
    calls, _ = fake_wire(monkeypatch)
    assert entry.main() == 0 and len(prompts) == len(calls) == 1
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize('arguments,confirmation', [
    (['--diagnose-schema', '--use-project-env'], 'UPDATE_BANK_IDENTITIES'),
    (['--refresh', '--use-project-env'], 'DIAGNOSE_BANK_SCHEMA'),
])
def test_confirmation_is_mode_bound_before_any_credential_or_send(tmp_path, monkeypatch, arguments, confirmation):
    source, _, _ = entry_context(tmp_path, monkeypatch, arguments, confirmation=confirmation)
    monkeypatch.setattr(project_credentials, 'read_project_credentials',
                        lambda *args, **kwargs: pytest.fail('credential read before valid confirmation'))
    calls, _ = fake_wire(monkeypatch)
    with pytest.raises(RuntimeError, match='not_confirmed'):
        entry.main()
    assert calls == [] and not (source / 'data/instrument_identity').exists()


def test_no_action_is_preflight_only(tmp_path, monkeypatch, capsys):
    source, checks, prompts = entry_context(tmp_path, monkeypatch, ['--use-project-env'])
    monkeypatch.setattr(project_credentials, 'read_project_credentials',
                        lambda *args, **kwargs: pytest.fail('preflight must not read credentials'))
    calls, _ = fake_wire(monkeypatch)
    assert entry.main() == 0 and checks and not prompts and not calls
    assert json.loads(capsys.readouterr().out)['acquisition'] is False
    assert not (source / 'data/instrument_identity').exists()


def test_modes_cannot_be_combined(monkeypatch):
    monkeypatch.setattr(entry.sys, 'argv', ['update_bank_identity.py', '--refresh', '--diagnose-schema'])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 2


@pytest.mark.installed_release
def test_deployment_binding_covers_changed_production_sources():
    support = importlib.import_module('analyze_mainland_bounded')
    release = json.loads((PROJECT / 'config/bounded_mainland_release.json').read_bytes())
    assert Path(release['deployment_root']).resolve() == PROJECT
    # Verify the stored root and actual bytes without rebasing the manifest.
    # Tests may share an installed interpreter; this does not qualify the
    # production launcher's separate requirement for PROJECT/.venv.
    support.verify_release(release, source=PROJECT,
                           python_root=Path(sys.executable).resolve().parents[1])


@pytest.mark.parametrize('mutation,reason', [
    ('none', None),
    ('root', 'installation_root_mismatch'),
    ('source_bytes', 'bound_source_changed'),
    ('dependency_bytes', 'bound_source_changed'),
    ('parent_path', 'invalid_bound_path'),
    ('absolute_path', 'invalid_bound_path'),
    ('duplicate', 'duplicate_bound_path'),
    ('missing', 'missing_bound_file'),
])
def test_release_binding_rejects_synthetic_drift(tmp_path, mutation, reason):
    support = importlib.import_module('analyze_mainland_bounded')
    source, dependency = tmp_path / 'source', tmp_path / 'dependency'
    source.mkdir()
    dependency.mkdir()
    content = b'synthetic reviewed bytes\n'
    for root in (source, dependency):
        (root / 'bound.py').write_bytes(content)
    manifest = {
        'contract': 'bounded-mainland-release-v1', 'deployment_root': str(source),
        'files': [
            {'root': kind, 'relative': 'bound.py',
             'sha256': hashlib.sha256(content).hexdigest()}
            for kind in ('project', 'python')
        ],
    }
    if mutation == 'root':
        manifest['deployment_root'] = str(dependency)
    elif mutation in {'source_bytes', 'dependency_bytes'}:
        root = source if mutation == 'source_bytes' else dependency
        (root / 'bound.py').write_bytes(b'synthetic changed bytes\n')
    elif mutation == 'parent_path':
        manifest['files'][0]['relative'] = '../bound.py'
    elif mutation == 'absolute_path':
        manifest['files'][0]['relative'] = str(source / 'bound.py')
    elif mutation == 'duplicate':
        manifest['files'].append(dict(manifest['files'][0]))
    elif mutation == 'missing':
        manifest['files'][0]['relative'] = 'missing.py'
    if reason is None:
        support.verify_release(manifest, source=source, python_root=dependency)
    else:
        with pytest.raises(support.Stop, match=f'^{reason}$'):
            support.verify_release(manifest, source=source, python_root=dependency)
