"""Input and pre-consent behavior only; never start a billable analysis session."""
import builtins
import json
import runpy
import sys
from types import SimpleNamespace

import pytest

from tests.test_bounded_mainland_entry import ROOT, entry, synthetic_required_stores


@pytest.mark.parametrize('raw,expected', [
    ('600919.sh', '600919.SH'), (' 600919.SH ', '600919.SH'),
    ('\t600919.ss\t', '600919.SS'), (' 000001.sz ', '000001.SZ'),
])
def test_explicit_spelling_normalizes_without_importing_app_or_guessing_exchange(monkeypatch, raw, expected):
    module = entry()
    original = builtins.__import__

    def no_app(name, *args, **kwargs):
        if name == 'cli' or name.startswith('cli.') or name == 'tradingagents' or name.startswith('tradingagents.'):
            pytest.fail('argument normalization must not import packages that can read credentials')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_app)
    args = module.parse_arguments(['--ticker', raw, '--analysis-date', '2026-11-01'])
    assert args.ticker == expected
    assert args.analysis_date == '2026-11-01'
    assert 'instrument_identity' not in vars(args)


@pytest.mark.parametrize('raw', ['600919', '000001', '999999', '600919.BJ', '600919.XX',
                                '６００９１９.SH', '600919.ＳＨ', '600 919.SH',
                                '600919.ß', '600919.ſh',
                                '600919.S H', '600919.SH\x00', '600919.SH\n600000.SH'])
def test_explicit_unknown_ambiguous_and_malformed_inputs_still_reject(raw):
    module = entry()
    with pytest.raises(module.Stop, match='^invalid_explicit_ticker$'):
        module.parse_arguments(['--ticker', raw, '--analysis-date', '2026-11-01'])


@pytest.mark.parametrize('period,valid', [('2026-09-30', True), ('2026-11-01', True), ('2026-11-02', False)])
def test_supplemental_period_is_checked_against_explicit_analysis_day(period, valid):
    module = entry()
    args = ['--ticker', '600919.SH', '--analysis-date', '2026-11-01', '--supplemental-report-period', period]
    if valid:
        assert module.parse_arguments(args).supplemental_report_period == period
    else:
        with pytest.raises(module.Stop, match='^supplemental_period_after_analysis$'):
            module.parse_arguments(args)


def test_bad_period_stops_main_before_confirmation_files_or_credentials(monkeypatch):
    module = entry()
    monkeypatch.setattr(sys, 'argv', ['entry', '--ticker', '600919.SH', '--analysis-date', '2026-11-01',
                                      '--supplemental-report-period', '2026-11-02'])

    def forbidden(*args, **kwargs):
        pytest.fail('invalid selection must stop before preflight, confirmation or session work')

    for name in ['read_json', 'verify_release', 'reserve_session', 'read_user_credentials', 'Session']:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(builtins, 'input', forbidden)
    with pytest.raises(module.Stop, match='^supplemental_period_after_analysis$'):
        module.main()


@pytest.mark.parametrize('answer', ['', 'NO', 'start', 'START ', None])
def test_confirmation_shows_normalized_symbol_and_never_treats_input_as_authorization(
    monkeypatch, tmp_path, capsys, answer,
):
    module = entry()
    # Controlled preflight/platform observations only. Do not alter the real
    # environment, load .env, invoke native admin checks, or grant a real session.
    module.os = SimpleNamespace(environ={})
    monkeypatch.setitem(sys.modules, 'ctypes', SimpleNamespace(
        windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False))))
    monkeypatch.setattr(module, 'SOURCE', tmp_path / 'project')
    synthetic_required_stores(module.SOURCE)
    monkeypatch.setattr(sys, 'executable', str(module.SOURCE / '.venv/Scripts/python.exe'))
    monkeypatch.setattr(sys, 'argv', ['entry', '--ticker', ' 600919.sh ', '--analysis-date', '2026-11-01'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(module, 'read_json', lambda path: {})
    monkeypatch.setattr(module, 'verify_release', lambda manifest: None)
    prompts = []

    def decline(prompt):
        prompts.append(prompt)
        assert 'START' in prompt
        text = capsys.readouterr().out
        assert '600919.SS' in text and '600919.SH' in text and '2026-11-01' in text
        if answer is None:
            raise EOFError
        return answer

    def forbidden(*args, **kwargs):
        pytest.fail('unconfirmed selection must not reserve, load credentials or start a session')

    monkeypatch.setattr(builtins, 'input', decline)
    for name in ['reserve_session', 'read_user_credentials', 'Session']:
        monkeypatch.setattr(module, name, forbidden)
    with pytest.raises(EOFError if answer is None else module.Stop):
        module.main()
    assert len(prompts) == 1


@pytest.mark.parametrize('args,reason,help_fragment', [
    (['--ticker', '600919'], 'selection_arguments_must_be_paired', '--analysis-date'),
    (['--ticker', '000001', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker', '.SZ'),
    (['--ticker', 'SYNTHETIC_PRIVATE', '--analysis-date', '2026-11-01'], 'invalid_explicit_ticker', '.SH'),
    (['--ticker', '600919.SH', '--analysis-date', '2026-02-30'], 'invalid_explicit_analysis_date', 'YYYY-MM-DD'),
    (['--ticker', '600919.SH', '--analysis-date', '9999-01-01'], 'future_analysis_date', 'Shanghai'),
    (['--supplemental-report-period', '2026-02-30'], 'invalid_supplemental_report_period', 'YYYY-MM-DD'),
    (['--ticker', '600919.SH', '--analysis-date', '2026-11-01', '--supplemental-report-period', '2026-11-02'],
     'supplemental_period_after_analysis', '--supplemental-report-period'),
])
def test_actual_cli_invalid_input_has_safe_hint_without_changing_json_contract(monkeypatch, capsys, args, reason, help_fragment):
    def no_prompt(*args, **kwargs):
        pytest.fail('invalid arguments must not request START or credentials')

    monkeypatch.setattr(builtins, 'input', no_prompt)
    monkeypatch.setattr(sys, 'argv', ['entry', *args])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT / 'scripts/analyze_mainland_bounded.py'), run_name='__main__')
    assert stopped.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {'status': 'stopped', 'exception_type': 'Stop', 'reason': reason}
    assert help_fragment in output.err
    assert 'SYNTHETIC_PRIVATE' not in output.out + output.err


def test_input_hints_ignore_unknown_and_upstream_errors():
    module = entry()

    class UpstreamStop(module.Stop):
        pass

    assert module.selection_error_hint(module.Stop('SYNTHETIC_PRIVATE')) is None
    assert module.selection_error_hint(ValueError('invalid_explicit_ticker')) is None
    assert module.selection_error_hint(UpstreamStop('invalid_explicit_ticker')) is None


@pytest.mark.parametrize('value,valid', [
    ('600919.SH', True), (' 600919.sh ', True), ('000001.SZ', True), ('AAPL', True),
    ('BRK.B', True), ('0700.HK', True), ('BTC-USD', True), ('EURUSD=X', True), ('GC=F', True),
    ('^GSPC', True), ('', True), ('江苏银行', False), ('６００９１９.SH', False),
    ('ＡＡＰＬ', False), ('АAPL', False), ('600919.ＳＨ', False), ('A/B', False),
    ('600919.SH\n600000.SH', False),
])
def test_menu_charset_matches_supported_symbol_alphabet(value, valid):
    from cli.utils import is_valid_ticker_input

    assert is_valid_ticker_input(value) is valid


@pytest.mark.parametrize('raw,expected', [('600919', '600919.SS'), (' 600919.sh ', '600919.SS'),
                                        ('000001.SZ', '000001.SZ'), ('BTCUSD', 'BTC-USD'), ('', 'SPY')])
def test_menu_shows_the_resolved_symbol_before_next_step(monkeypatch, raw, expected):
    from cli import utils

    printed = []

    def prompt(*args, **kwargs):
        assert kwargs['validate'](raw) is True
        return SimpleNamespace(ask=lambda: raw)

    monkeypatch.setattr(utils.questionary, 'text', prompt)
    monkeypatch.setattr(utils.console, 'print', lambda *args, **kwargs: printed.append(' '.join(map(str, args))))
    assert utils.get_ticker() == expected
    assert expected in '\n'.join(printed)


def test_menu_selection_cancellation_remains_terminal(monkeypatch):
    from cli import utils

    monkeypatch.setattr(utils.questionary, 'text', lambda *args, **kwargs: SimpleNamespace(ask=lambda: None))
    with pytest.raises(SystemExit) as stopped:
        utils.get_ticker()
    assert stopped.value.code == 1
