"""Synthetic console and identity fixtures only; no transport or real stores."""
import io
from datetime import date, timedelta
from pathlib import Path

import pytest

from test_bounded_mainland_entry import entry
from test_bank_identity_snapshot import NOW, bank, source_layout, exact_snapshot, save_exact_snapshot


def guided(module, tmp_path, monkeypatch, extra=()):
    monkeypatch.setattr(module.sys, 'stdin', type('Terminal', (), {'isatty': lambda self: True})())
    executable = tmp_path / 'pwsh.exe'
    executable.write_bytes(b'synthetic executable identity only; never run')
    return module.parse_arguments(['--guided', '--guided-powershell-executable', str(executable), *extra])


@pytest.mark.parametrize('ticker,canonical', [('600919.sh', '600919.SH'), ('601398.SS','601398.SS'), ('000001.SZ','000001.SZ')])
def test_guided_selection_has_no_default_stock_and_clear_cutoff(tmp_path, monkeypatch, capsys, ticker, canonical):
    m=entry(); args=guided(m,tmp_path,monkeypatch)
    answers=iter([ticker, '', '', '', '/skip', ''])
    got=m.collect_guided_selection(args,reader=lambda _:next(answers),today=date(2026,11,15))
    assert got.ticker==canonical and got.analysis_date=='2026-11-14'
    assert got.margin_date is None and got.moneyflow and got.powershell_news and got.use_project_env
    assert '不代表交易日' in capsys.readouterr().out


@pytest.mark.parametrize('values,code', [(['','2026-11-13'],'invalid_explicit_ticker'),
    (['600919','2026-11-13'],'invalid_explicit_ticker'),(['600919.SH','2026-02-30'],'invalid_explicit_analysis_date'),
    (['600919.SH','9999-01-01'],'future_analysis_date'),(['/cancel'],'guided_cancelled'),
    (['x'*81],'guided_input_invalid')])
def test_invalid_selection_stops_without_identity_or_network(tmp_path,monkeypatch,values,code):
    m=entry();args=guided(m,tmp_path,monkeypatch);answers=iter(values)
    with pytest.raises(m.Stop,match=code):m.collect_guided_selection(args,reader=lambda _:next(answers))


@pytest.mark.parametrize('error',[EOFError,KeyboardInterrupt])
def test_eof_and_interrupt_are_closed_cancel(error):
    m=entry()
    def read(_):raise error()
    with pytest.raises(m.Stop,match='guided_cancelled'):m.guided_input('',reader=read)


def test_noninteractive_guided_rejected_but_explicit_parse_unchanged(tmp_path,monkeypatch):
    m=entry();args=guided(m,tmp_path,monkeypatch)
    monkeypatch.setattr(m.sys,'stdin',io.StringIO())
    with pytest.raises(m.Stop,match='guided_terminal_required'):
        m.collect_guided_selection(args,reader=lambda _:pytest.fail('must not read'))
    explicit=m.parse_arguments(['--ticker','600919.SH','--analysis-date','2026-11-13','--use-project-env'])
    assert not explicit.guided and not explicit.powershell_news


def test_explicit_guided_overrides_and_missing_runtime_no_requests_fallback(tmp_path,monkeypatch):
    m=entry();args=guided(m,tmp_path,monkeypatch, ['--ticker','600919.SH','--analysis-date','2026-11-13',
        '--moneyflow','--margin-date','2026-11-13','--use-project-env'])
    args.guided_powershell_executable=str(tmp_path/'absent'/'pwsh.exe')
    m.collect_guided_selection(args,reader=lambda _:pytest.fail('explicit settings must remain'))
    from tradingagents.powershell_news import PowerShellNews
    owner=PowerShellNews(args.powershell_news_executable, tmp_path/'Get-PublicNews.ps1', args.ticker,args.analysis_date)
    assert args.powershell_news and owner.summary()['transport']['status']=='runtime_missing'
    session=m.Session(tmp_path/'session',source=tmp_path,powershell_news_executable=args.powershell_news_executable)
    (tmp_path/'config').mkdir();(tmp_path/'config/instrument_identity_registry.sha256').write_text('a'*64)
    assert session.configure({})['bounded_powershell_news_enabled'] is True


def test_retained_reference_revalidated_into_real_session_without_writes_during_discovery(tmp_path):
    source,run=source_layout(tmp_path);ref=save_exact_snapshot(source,exact_snapshot())
    before={p.relative_to(source):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert bank.find_current_reference(source,'600919.SS','2026-10-30',now=NOW)==(ref,'retained_snapshot')
    assert before=={p.relative_to(source):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    binding=bank.prepare_overlay(source,run,'600919.SH','2026-10-30',ref,now=NOW)
    assert bank.validate_binding(source,run,binding,'600919.SH','2026-10-30',now=NOW)


@pytest.mark.parametrize('case',['missing','expired','wrong_stock','base'])
def test_reference_discovery_no_guess_no_request(tmp_path,case):
    source,_=source_layout(tmp_path)
    if case!='missing':save_exact_snapshot(source,exact_snapshot())
    ticker='601398.SH' if case=='wrong_stock' else '600895.SH' if case=='base' else '600919.SH'
    now=NOW+timedelta(days=1) if case=='expired' else NOW
    assert bank.find_current_reference(source,ticker,'2026-10-30',now=now)==(None,'base_registry' if case=='base' else 'refresh_required')


@pytest.mark.parametrize('case',['corrupt','future','listing_date'])
def test_corrupt_future_and_bad_period_not_refresh_fallback(tmp_path,case):
    source,_=source_layout(tmp_path);ref=save_exact_snapshot(source,exact_snapshot())
    if case=='corrupt':(source/'data/instrument_identity/stock_basic'/ref.split(':')[0]/'snapshot.json').write_text('{}')
    with pytest.raises(bank.BankIdentityError):
        bank.find_current_reference(source,'600919.SH','1999-01-01' if case=='listing_date' else '2026-10-30',
                                    now=NOW-timedelta(seconds=1) if case=='future' else NOW)


@pytest.mark.parametrize('outcome',['success','failure','cancel','wrong_stock'])
def test_one_exact_refresh_before_analysis_and_strict_return(tmp_path,monkeypatch,outcome):
    m=entry();args=guided(m,tmp_path,monkeypatch,['--ticker','600919.SH','--analysis-date','2026-10-30','--use-project-env'])
    source,_=source_layout(tmp_path);calls=[]
    monkeypatch.setattr(bank,'current_time',lambda now=None: NOW)
    def refresh(argv,**kwargs):
        calls.append((argv,kwargs))
        if outcome=='cancel':raise m.Stop('not_confirmed')
        if outcome=='failure':return {'status':'stopped'}
        ref=save_exact_snapshot(source,exact_snapshot('601398.SH' if outcome=='wrong_stock' else '600919.SH'))
        return {'status':'exact_positive','snapshot_ref':ref,'physical_attempts':1}
    clock=m.SharedSessionClock()
    if outcome=='success':
        assert m.prepare_guided_identity(args,source=source,refresh=refresh,clock=clock)==1
        assert args.bank_snapshot
        assert m.prepare_guided_identity(args,source=source,refresh=lambda *a,**k:pytest.fail('no second update'))==0
    else:
        with pytest.raises((m.Stop,bank.BankIdentityError)):m.prepare_guided_identity(args,source=source,refresh=refresh,clock=clock)
        assert args.bank_snapshot is None
    assert calls==[(['--refresh-security','600919.SH','--use-project-env'],{'return_result':True,'session_clock':clock})]


def test_explicit_wrong_reference_not_replaced(tmp_path,monkeypatch):
    m=entry();args=guided(m,tmp_path,monkeypatch,['--ticker','600919.SH','--analysis-date','2026-10-30'])
    source,_=source_layout(tmp_path);args.bank_snapshot=save_exact_snapshot(source,exact_snapshot('601398.SH'))
    monkeypatch.setattr(bank,'current_time',lambda now=None:NOW)
    with pytest.raises(bank.BankIdentityError):
        m.prepare_guided_identity(args,source=source,refresh=lambda *a,**k:pytest.fail('no replacement'))


@pytest.mark.parametrize('kind',['link','denied'])
@pytest.mark.parametrize('location',['parent','ready'])
def test_ready_link_or_permission_denial_is_not_missing(tmp_path,monkeypatch,kind,location):
    import stat
    from types import SimpleNamespace
    source,_=source_layout(tmp_path);ref=save_exact_snapshot(source,exact_snapshot())
    ready=source/'data/instrument_identity/stock_basic'/ref.split(':')[0]/'READY.json'
    target=ready if location=='ready' else ready.parent.parent
    original=Path.lstat
    def inspect(path,*args,**kwargs):
        if path==target:
            if kind=='denied':raise PermissionError('synthetic-secret-must-not-be-echoed')
            return SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',inspect)
    with pytest.raises((bank.BankIdentityError,PermissionError)):
        bank.find_current_reference(source,'600919.SH','2026-10-30',now=NOW)


def test_guided_start_cancel_does_not_create_session_or_read_credentials(tmp_path,monkeypatch,capsys):
    import ctypes
    from types import SimpleNamespace
    m=entry();args=guided(m,tmp_path,monkeypatch,['--ticker','600919.SH','--analysis-date','2026-11-13',
        '--moneyflow','--margin-date','2026-11-13','--use-project-env'])
    monkeypatch.setattr(m,'parse_arguments',lambda:args)
    monkeypatch.setattr(m,'read_json',lambda _: {})
    monkeypatch.setattr(m,'verify_release',lambda _:None)
    monkeypatch.setattr(ctypes.windll.shell32,'IsUserAnAdmin',lambda:False)
    monkeypatch.setattr(m,'check_required_local_stores',lambda _:None)
    monkeypatch.setattr(m,'prepare_guided_identity',lambda _,**kwargs:0)
    # Synthetic pre-START gate test only, never execute a mocked interpreter.
    m.sys=SimpleNamespace(flags=m.sys.flags,dont_write_bytecode=True,
        executable=str(m.SOURCE/'.venv/Scripts/python.exe'),stdin=m.sys.stdin,path=list(m.sys.path))
    m.os=SimpleNamespace(environ={})
    monkeypatch.setattr(m,'reserve_session',lambda _:pytest.fail('must not start'))
    monkeypatch.setattr('builtins.input',lambda _:'CANCEL')
    with pytest.raises(m.Stop,match='not_confirmed'):m.main()
    output=capsys.readouterr().out
    assert '1200s' in output and '<=60' in output and '<=3' in output and '600919' in output

