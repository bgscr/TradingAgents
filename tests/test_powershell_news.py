"""Fake child transport; no PowerShell process or network is launched."""
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents import analysis_budget as b
from tradingagents import powershell_news as p
from tradingagents import news_projection as m


def body(req, count=1):
    rows = [{'date': req.end + ' 12:00:00', 'code': 'synth' + str(i), 'title': req.ticker[:6] + ' 合成',
             'content': '测试数据', 'mediaName': '合成媒体'} for i in range(count)]
    return (req.callback + '(' + json.dumps({'result': {'cmsArticleWebOld': rows}}) + ')').encode()


def wire(payload, **changes):
    obj = dict(status='ok', http_status=200, body=base64.b64encode(payload).decode(),
               cleanup='closed', request_attempted=True)
    obj.update(changes)
    return json.dumps(obj).encode()


class FakeChild:
    def __init__(self, stdout=b'', stderr=b'', returncode=0):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.killed = False
    def poll(self): return self.returncode
    def kill(self): self.killed = True; self.returncode = -9
    def wait(self, timeout=None): return self.returncode


def owned(tmp_path):
    exe = tmp_path / 'pwsh.exe'; exe.write_bytes(b'fake executable never launched')
    script = tmp_path / 'Get-PublicNews.ps1'; script.write_bytes(b'# fake never launched')
    return p.PowerShellNews(exe,script,'600919.SH','2026-11-13')


@pytest.mark.parametrize('count', [0,1,3])
def test_child_exact_argument_list_env_ipc_and_collection_count(tmp_path, monkeypatch, count):
    request = p.make_request('600919.SH','2026-11-13')
    content = body(request,count)
    child = FakeChild(wire(content))
    launch = Mock(return_value=child)
    monkeypatch.setenv('TUSHARE_TOKEN','SECRET_SENTINEL')
    monkeypatch.setenv('HTTP_PROXY','SECRET_SENTINEL')
    result, meta = p.run_child(tmp_path/'pwsh.exe',tmp_path/'Get-PublicNews.ps1',request.url,
                               remaining=30,check=lambda: None,launch=launch)
    args, kwargs = launch.call_args
    assert args[0] == [str(tmp_path/'pwsh.exe'),'-NoLogo','-NoProfile','-NonInteractive','-File',str(tmp_path/'Get-PublicNews.ps1')]
    assert kwargs['shell'] is False and 'TUSHARE_TOKEN' not in kwargs['env'] and 'HTTP_PROXY' not in kwargs['env']
    assert kwargs['env']['PSModulePath'] == str(tmp_path/'Modules')
    assert result == content and meta['http_status'] == 200 and meta['process_closed']
    projection = m.validate_response(request, result.decode())
    assert projection.status == ('accepted' if count else 'no_usable_articles')
    assert len(projection.rows) == count
    assert all(pipe.closed for pipe in (child.stdin,child.stdout,child.stderr))
    assert 'SECRET_SENTINEL' not in json.dumps(meta)


@pytest.mark.parametrize('mode', ['stderr','oversize','wrong_status','close_failed','bad_base64','unknown','duplicate','bad_utf8','nonzero'])
def test_unsafe_child_results_never_escape_or_retry(tmp_path, mode):
    content = wire(b'CALLBACK_SECRET_SENTINEL')
    err = b''; rc = 0
    if mode == 'stderr': err = b'Token=SECRET_SENTINEL'
    if mode == 'oversize': content = b'x' * (p.MAX_IPC + 1)
    if mode == 'wrong_status': content = wire(b'SECRET_SENTINEL',http_status=403)
    if mode == 'close_failed': content = wire(b'SECRET_SENTINEL',cleanup='close_failed')
    if mode == 'bad_base64': content = wire(b'',body='not-base64!SECRET_SENTINEL')
    if mode == 'unknown': content = b'{"token":"SECRET_SENTINEL"}'
    if mode == 'duplicate': content = b'{"status":"ok","status":"SECRET_SENTINEL"}'
    if mode == 'bad_utf8': content = b'\xff'
    if mode == 'nonzero': rc=1
    child = FakeChild(content,err,rc); launch = Mock(return_value=child)
    result,meta=p.run_child(tmp_path/'pwsh.exe',tmp_path/'Get-PublicNews.ps1','https://example.invalid',
                           remaining=30,check=lambda:None,launch=launch)
    assert result is None and launch.call_count == 1
    assert 'SECRET_SENTINEL' not in json.dumps(meta) and meta['process_closed']
    assert meta['status'] != 'ok'


def test_timeout_cancel_and_permission_keep_first_and_close(tmp_path):
    class Cancel(BaseException): pass
    child=FakeChild(returncode=None); calls=[0]
    def check():
        calls[0]+=1
        if calls[0]==2: raise Cancel()
    with pytest.raises(Cancel):
        p.run_child(tmp_path/'pwsh.exe',tmp_path/'Get-PublicNews.ps1','public',remaining=30,
                    check=check,launch=lambda *a,**k:child)
    assert child.killed and all(x.closed for x in (child.stdin,child.stdout,child.stderr))
    child=FakeChild(returncode=None); clock=iter([0,30])
    result,meta=p.run_child(tmp_path/'pwsh.exe',tmp_path/'Get-PublicNews.ps1','public',remaining=30,
                           check=lambda:None,launch=lambda *a,**k:child,clock=lambda:next(clock))
    assert result is None and meta['status']=='timeout' and child.killed
    with pytest.raises(p.PowerShellNewsStop):
        p.run_child(tmp_path/'pwsh.exe',tmp_path/'Get-PublicNews.ps1','public',remaining=30,
                    check=lambda:None,launch=Mock(side_effect=PermissionError('SECRET_SENTINEL')))


def test_owner_retains_interruption_and_cleanup_metadata(tmp_path, monkeypatch):
    source=owned(tmp_path)
    budget=b.AnalysisBudget(1,60)
    monkeypatch.setattr(b,'active_budget',lambda:budget)
    def interrupted(*args,metadata,**kwargs):
        metadata.update(status='interrupted',process_started=True,attempted=None,process_closed=True)
        raise b.AnalysisBudgetStopped('cancelled')
    monkeypatch.setattr(p,'run_child',interrupted)
    with pytest.raises(b.AnalysisBudgetStopped):
        source.acquire('600919.SS',source.request.start,source.request.end,
            tool_call_id='stop',source_ref='news:synthetic',capability='get_news')
    assert source.summary()['transport']=={'status':'interrupted','attempted':None,'process_started':True,'process_closed':True}
    assert budget._counts['data']==1 and source.attempted


@pytest.mark.parametrize('status,reason', [('powershell_timeout','timeout'),('callback_mismatch','malformed_response'),
    ('context_missing','not_configured'),('powershell_runtime_missing','not_configured'),
    ('powershell_http_error','provider_error'),('powershell_ipc_invalid','malformed_response'),
    ('no_usable_articles','no_data')])
def test_failures_remain_typed_and_named(status,reason):
    req=p.make_request('600919.SH','2026-11-13')
    result=m.acquire_validated_news(m.unavailable(req,status),req.ticker,req.start,req.end,
        tool_call_id='classify',source_ref='news:synthetic',capability='get_news',provider_name=p.PROVIDER)
    assert result.outcomes[0].reason.value==reason
    assert result.outcomes[0].error_code=='powershell_news_'+status
    assert result.artifact is None


def test_formal_hooks_keep_first_error_on_receipt_failure(tmp_path,monkeypatch):
    from tests.test_news_projection import entry
    module=entry()
    source=tmp_path/'source'; (source/'scripts').mkdir(parents=True)
    (source/'scripts/Get-PublicNews.ps1').write_text('# synthetic not executed')
    exe=tmp_path/'pwsh.exe';exe.write_bytes(b'synthetic not executed')
    session=module.Session(tmp_path,source=source,powershell_news_executable=str(exe))
    session.selection={'ticker':'600919.SH','analysis_date':'2026-11-13'}
    cli=SimpleNamespace(DEFAULT_CONFIG={},get_user_selections=lambda:None,
        _build_run_config=lambda:{},_prepare_run_artifacts=lambda:{})
    monkeypatch.setattr(module,'write_once',Mock(side_effect=OSError('SECRET_SENTINEL')))
    with pytest.raises(b.AnalysisBudgetStopped) as caught:
        with session.analysis_hooks(cli,{},{}):
            assert p.active_powershell_news() is not None
            raise b.AnalysisBudgetStopped('cancelled')
    assert caught.value.reason=='cancelled' and session._powershell_receipt_failed
    assert p.active_powershell_news() is None


@pytest.mark.parametrize('failure', [False,True])
def test_one_attempt_shared_real_budget_cached_across_calls_and_threads(tmp_path, monkeypatch, failure):
    source=owned(tmp_path)
    budget=b.AnalysisBudget(1,60,allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b,'active_budget',lambda:budget)
    run=Mock(return_value=(None if failure else body(source.request), {'status':'request_failed' if failure else 'ok'}))
    monkeypatch.setattr(p,'run_child',run)
    def call(i):
        return source.acquire('600919.SS',source.request.start,source.request.end,
            tool_call_id='call'+str(i),source_ref='news:synthetic',capability='get_news')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(call,range(2)))
    assert run.call_count==1 and budget._counts=={'model':0,'data':1}
    assert all((r.artifact is None)==failure for r in results)
    assert all(r.provider=='eastmoney_powershell' for r in results) if not failure else True
    assert 'body' not in source.summary() and 'user_browser_paste' not in json.dumps(source.summary())


@pytest.mark.parametrize('case', ['expired','used','cancelled','wrong_stock','future','invalid_date','changed_script','missing'])
def test_no_send_when_not_admitted_or_binding_invalid(tmp_path, monkeypatch, case):
    source=owned(tmp_path)
    now=[0.0]; budget=b.AnalysisBudget(1,60,clock=lambda:now[0],allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b,'active_budget',lambda:budget)
    run=Mock(side_effect=AssertionError('must not launch'));monkeypatch.setattr(p,'run_child',run)
    ticker, start, end='600919.SS',source.request.start,source.request.end
    if case=='expired':now[0]=70
    if case=='used':budget.admit('data')
    if case=='cancelled':
        try:budget.stop('cancelled')
        except b.AnalysisBudgetStopped:pass
    if case=='wrong_stock':ticker='000001.SZ'
    if case=='future':end='2026-11-14'
    if case=='invalid_date':start='2026-11-0X'
    if case=='changed_script':source.script.write_text('# changed after binding')
    if case=='missing':source=p.PowerShellNews(tmp_path/'missing'/'pwsh.exe',source.script,ticker,end)
    args=dict(tool_call_id='reject',source_ref='news:synthetic',capability='get_news')
    if case in {'expired','used','cancelled','changed_script'}:
        with pytest.raises((b.AnalysisBudgetStopped,p.PowerShellNewsStop)):source.acquire(ticker,start,end,**args)
    else:assert source.acquire(ticker,start,end,**args).artifact is None
    run.assert_not_called()


def test_powershell_route_and_report_are_explicit_no_requests_fallback(tmp_path,monkeypatch):
    from tradingagents.dataflows import interface,config
    from tradingagents.evidence import EvidenceState,merge_source_acquisition_outcomes
    from tradingagents.reporting import _render_acquisition_outcomes
    source=owned(tmp_path)
    monkeypatch.setattr(b,'active_budget',lambda:b.AnalysisBudget(1,60))
    monkeypatch.setattr(p,'run_child',lambda *a,**k:(body(source.request),{'status':'ok'}))
    monkeypatch.setattr(config,'_config',{'bounded_powershell_news_enabled':True})
    monkeypatch.setattr(interface,'get_vendor',Mock(side_effect=AssertionError('no Requests')))
    with p.powershell_news_scope(source):
        result=interface.route_to_vendor_acquired('get_news','600919.SS',source.request.start,source.request.end,
            tool_call_id='ps-tool',source_ref='news:synthetic',capability='get_news')
    assert result.provider=='eastmoney_powershell' and 'PowerShell' in result.value
    assert '用户浏览器粘贴' not in result.value
    report='\n'.join(_render_acquisition_outcomes(merge_source_acquisition_outcomes(EvidenceState(),result.outcomes)))
    assert 'PowerShell 新闻传输' in report and '历史覆盖未知' in report
    absent=interface.route_to_vendor_acquired('get_news','600919.SS',source.request.start,source.request.end,
            tool_call_id='ps-absent',source_ref='news:synthetic',capability='get_news')
    assert absent.artifact is None


def test_script_closed_cmdlet_contract_and_entry_selection():
    from tests.test_news_projection import entry
    root=Path(__file__).resolve().parents[1]
    script=(root/'scripts/Get-PublicNews.ps1').read_text()
    assert 'Microsoft.PowerShell.Utility\\Invoke-WebRequest -CommandType Cmdlet' in script
    for token in ('-MaximumRedirection 0','-MaximumRetryCount 0','-SkipHttpErrorCheck','RawContentStream.Length -le 1048576','ImplementingType.Assembly.Location'):
        assert token in script
    assert 'Invoke-Expression' not in script and 'OutFile' not in script and '-Credential' not in script
    module=entry()
    args=['--powershell-news','--powershell-news-executable',str(root/'pwsh.exe'),'--ticker','600919.SH','--analysis-date','2026-11-13']
    assert module.parse_arguments(args).powershell_news
    with pytest.raises(SystemExit):module.parse_arguments(args+['--manual-news'])
    with pytest.raises(module.Stop,match='invalid_powershell_executable'):
        module.parse_arguments([v.replace('pwsh.exe','evil.cmd') for v in args])
    assert not hasattr(module.parse_arguments([]), 'manual_news')
    wrapper=(root/'scripts/Start-MainlandAnalysis.ps1').read_text()
    assert '[switch]$ManualNews' not in wrapper and '[switch]$PowerShellNews' in wrapper
    assert "(Join-Path $PSHOME 'pwsh.exe')" in wrapper
    assert 'collect(make_request' not in (root/'scripts/analyze_mainland_bounded.py').read_text()


def test_actual_tool_envelope_and_report_keep_powershell_source(tmp_path, monkeypatch):
    from tradingagents.agents.utils.news_data_tools import get_news
    from tradingagents.dataflows import interface
    from tradingagents.evidence import EvidenceState, ToolExecutionEvidenceEnvelope, merge_source_acquisition_outcomes
    from tradingagents.reporting import _render_acquisition_outcomes
    source=owned(tmp_path)
    budget=b.AnalysisBudget(1,60,allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b,'active_budget',lambda:budget)
    run=Mock(return_value=(body(source.request),{'status':'ok'}))
    monkeypatch.setattr(p,'run_child',run)
    monkeypatch.setattr(interface,'get_vendor',Mock(side_effect=AssertionError('no fallback')))
    with p.powershell_news_scope(source):
        text,envelope=get_news.func(ticker=source.request.ticker,start_date=source.request.start,
            end_date=source.request.end,tool_call_id='ps-tool-envelope',state={})
        legacy=interface.route_to_vendor('get_news',source.request.ticker,source.request.start,source.request.end)
    assert 'PowerShell' in text and '测试数据' in text and '测试数据' in legacy
    assert '用户浏览器粘贴' not in text and source.request.callback not in text
    typed=ToolExecutionEvidenceEnvelope.model_validate(envelope)
    assert typed.selected_artifact.raw_text==text
    evidence=merge_source_acquisition_outcomes(EvidenceState(),typed.acquisition_outcomes)
    report='\n'.join(_render_acquisition_outcomes(evidence))
    assert 'PowerShell 新闻传输' in report and '历史覆盖未知' in report
    assert '测试数据' not in report and 'user_browser_paste' not in report
    assert budget._counts=={'model':0,'data':1} and run.call_count==1
