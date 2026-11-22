"""Exercise pre-START checks with task-owned files; never open a real database."""
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_bounded_mainland_entry import entry, synthetic_required_stores

MARKET = 'data/market_history/market_history.sqlite3'
COORDINATOR = MARKET + '.provider-requests.sqlite3'


def isolated_main(monkeypatch, tmp_path, *, extra=()):
    module = entry()
    module.os = SimpleNamespace(environ={})
    monkeypatch.setitem(sys.modules, 'ctypes', SimpleNamespace(
        windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False))))
    monkeypatch.setattr(module, 'SOURCE', tmp_path / 'project')
    monkeypatch.setattr(sys, 'executable', str(module.SOURCE / '.venv/Scripts/python.exe'))
    monkeypatch.setattr(sys, 'argv', ['entry', '--ticker', '600919.SH', '--analysis-date', '2026-11-01', *extra])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(module, 'read_json', lambda path: {})
    monkeypatch.setattr(module, 'verify_release', lambda manifest: None)

    def forbidden(*args, **kwargs):
        pytest.fail('pre-START work must not reserve a session or read credentials')

    for name in ('reserve_session', 'read_user_credentials', 'Session'):
        monkeypatch.setattr(module, name, forbidden)
    return module


@pytest.mark.parametrize('state,reason', [
    ('neither', 'local_market_store_unavailable'),
    ('only_coordinator', 'local_market_store_unavailable'),
    ('only_market', 'local_coordinator_store_unavailable'),
    ('market_directory', 'local_market_store_unavailable'),
    ('coordinator_directory', 'local_coordinator_store_unavailable'),
])
def test_missing_required_store_stops_before_confirmation_or_credentials(monkeypatch, tmp_path, state, reason):
    module = isolated_main(monkeypatch, tmp_path)
    directory = module.SOURCE / 'data/market_history'
    directory.mkdir(parents=True)
    if state in {'only_market', 'coordinator_directory'}:
        (module.SOURCE / MARKET).write_bytes(b'synthetic')
    if state == 'only_coordinator':
        (module.SOURCE / COORDINATOR).write_bytes(b'synthetic')
    if state.endswith('_directory'):
        (module.SOURCE / (MARKET if state == 'market_directory' else COORDINATOR)).mkdir()

    def no_confirmation(*args):
        pytest.fail('known missing prerequisite must be detected before asking for START')

    monkeypatch.setattr(builtins, 'input', no_confirmation)
    with pytest.raises(module.Stop, match='^' + reason + '$'):
        module.main()
    assert not (module.SOURCE / 'reports').exists()
    hint = module.selection_error_hint(module.Stop(reason))
    assert (MARKET if reason == 'local_market_store_unavailable' else COORDINATOR) in hint
    assert 'empty' in hint and 'docs/bounded-mainland-entry.md' in hint


@pytest.mark.parametrize('cache_present', [False, True])
@pytest.mark.parametrize('period', [None, '2026-09-30'])
def test_start_preview_distinguishes_presence_cache_coverage_and_optional_period(
    monkeypatch, tmp_path, capsys, cache_present, period,
):
    extra = ['--supplemental-report-period', period] if period else []
    module = isolated_main(monkeypatch, tmp_path, extra=extra)
    synthetic_required_stores(module.SOURCE)
    cache = module.SOURCE / 'data/cache/current-financial-statements.sqlite3'
    if cache_present:
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'synthetic, deliberately unvalidated cache')

    def no_read(*args, **kwargs):
        pytest.fail('pre-START presence check must not open database, cache or credential contents')

    for name in ('read_bytes', 'read_text', 'open'):
        monkeypatch.setattr(Path, name, no_read)
    original_import = builtins.__import__

    def no_app(name, *args, **kwargs):
        if name in {'cli', 'tradingagents', 'sqlite3'} or name.startswith(('cli.', 'tradingagents.')):
            pytest.fail('pre-START presence check must not import the product or SQLite')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_app)
    prompts = []

    def decline(prompt):
        prompts.append(prompt)
        output = capsys.readouterr().out
        assert 'required files present' in output
        assert 'schema, identity and coverage are not yet verified' in output
        assert module.DESTINATION in output
        assert 'cache first' in output and str(module.FIXED['tushare_financial_cache_max_age_seconds']) in output
        assert 'Missing or expired coverage may require financial requests' in output
        assert 'not evidence of missing response fields' in output
        if period:
            assert period in output and 'binding is unavailable, this session stops' in output
            assert 'display only' in output and 'no extra acquisition' in output
        else:
            assert 'Supplemental report period:' not in output
        return 'NO'

    monkeypatch.setattr(builtins, 'input', decline)
    with pytest.raises(module.Stop, match='^not_confirmed$'):
        module.main()
    assert len(prompts) == 1


def test_check_only_does_not_inspect_local_data(monkeypatch, tmp_path, capsys):
    module = isolated_main(monkeypatch, tmp_path, extra=['--check-only'])

    def forbidden(*args, **kwargs):
        pytest.fail('check-only remains a source check, not local-data preflight')

    monkeypatch.setattr(module, 'check_required_local_stores', forbidden, raising=False)
    monkeypatch.setattr(builtins, 'input', forbidden)
    assert module.main() == 0
    assert '"runtime_metadata_checked": false' in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['permission', 'linked'])
def test_preconfirmation_file_refusal_propagates_without_retry(monkeypatch, tmp_path, failure):
    module = isolated_main(monkeypatch, tmp_path)
    calls = []

    def reject(path):
        calls.append(path)
        if failure == 'permission':
            raise PermissionError('synthetic denied')
        raise module.Stop('linked_path')

    monkeypatch.setattr(module, 'regular_path', reject)
    monkeypatch.setattr(builtins, 'input', lambda *args: pytest.fail('refusal must stop before START'))
    with pytest.raises(PermissionError if failure == 'permission' else module.Stop,
                       match='synthetic denied' if failure == 'permission' else '^linked_path$'):
        module.main()
    assert calls == [module.SOURCE / MARKET]


def test_early_required_paths_match_the_unchanged_session_storage_contract(monkeypatch, tmp_path):
    module = entry()
    session = module.Session(tmp_path / 'session', source=tmp_path / 'project')
    checked = []
    monkeypatch.setattr(module, 'regular_path', checked.append)
    module.check_required_local_stores(session.source)
    market = session.paths['market_history_database_path']
    assert checked == [market, market.with_name(market.name + '.provider-requests.sqlite3')]
    assert session.paths['data_cache_dir'] / 'current-financial-statements.sqlite3' not in checked
