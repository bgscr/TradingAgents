"""Pure synthetic partial-news admission and formal wiring; no live requests."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents import news_projection as p, powershell_news as ps, analysis_budget as budget_module
from tradingagents.dataflows import eastmoney_news as parser
from tests.test_news_projected_envelope import envelope, wire, SENTINEL
from tests.test_powershell_news import owned
from tests.test_news_projection import entry


def row(n=1, symbol='601328', day='2026-11-13', **extra):
    item = envelope(symbol, day)['result']['cmsArticleWebOld'][0]
    item['code'] = f'20261113{n:010d}'
    item['url'] = f'http://bank.eastmoney.com/a/{item["code"]}.html'
    item.update(extra)
    return item


def data(rows):
    value = envelope()
    value['result']['cmsArticleWebOld'] = rows
    return value


def project(rows, *, isolate=True):
    req = ps.make_request('601328.SH', '2026-11-13')
    return p.validate_response(req, wire(data(rows), req), isolate_invalid_sources=isolate)


def mixed(symbol='601328'):
    good = row(symbol=symbol)
    missing = row(5, symbol); del missing['url']
    return [good, row(2, symbol, url='http://outside.invalid/a/2.html', title='DISCARDED_ARTICLE'),
            row(3, '600919' if symbol != '600919' else '601328'), row(4, symbol, '2026-11-14'),
            copy.deepcopy(good), missing]


def test_mixed_exact_counts_default_strict_and_no_discarded_text(monkeypatch):
    from tradingagents import run_diagnostics
    shapes=[]
    monkeypatch.setattr(run_diagnostics,'record_news_response_shape',lambda value,**kw:shapes.append(value))
    rows = mixed()
    assert project(rows, isolate=False).status == 'article_source_invalid'
    result = project(rows)
    assert result.status == 'accepted' and len(result.rows) == 1
    assert result.summary()['selection_counts'] == dict(returned=6, source_rejected=2,
        malformed=0, undated=0, outside_window=1, wrong_stock=1, duplicate=1, accepted=1)
    assert result.summary()['page_status'] == 'partial_source_exclusion'
    assert 'DISCARDED_ARTICLE' not in repr(result.rows)+json.dumps(result.summary())
    assert shapes[-1] == {'result':{'cmsArticleWebOld':[None]*6}}


@pytest.mark.parametrize('url', [None, {}, [], 1, '', 'javascript:UNTRUSTED_ACTION()',
    'ftp://bank.eastmoney.com/a/202611130000000001.html',
    'https://bank.eastmoney.com/a/202611130000000001.html',
    'http://bank.eastmoney.com.evil.invalid/a/202611130000000001.html',
    'http://bank.eastmoney.com:80/a/202611130000000001.html',
    'http://bank.eastmoney.com/a/202611130000000002.html'])
def test_source_only_refusal_isolates_whole_article(url):
    result = project([row(title='DISCARDED_ARTICLE', url=url), row(2)])
    assert result.status == 'accepted' and result.source_selection.source_rejected == 1
    assert len(result.rows) == 1 and result.rows[0][2].endswith('2')
    assert 'DISCARDED_ARTICLE' not in json.dumps(result.rows)


@pytest.mark.parametrize('url', ['http://name:password@bank.eastmoney.com/a/1.html',
    ' http://name:pwd@bank.eastmoney.com/a/1.html',
    '\x00http://name:pwd@bank.eastmoney.com/a/1.html',
    '\thttp://name:pwd@bank.eastmoney.com/a/1.html',
    'http&#58;//name:pwd&#64;bank.eastmoney.com/a/1.html',
    '//name:pwd@bank.eastmoney.com/a/1.html',
    'ftp://name@bank.eastmoney.com/a/1.html', 'http://bank.eastmoney.com/a/1.html?token='+SENTINEL,
    'javascript:'+SENTINEL, {'token':'synthetic'}, {'extra':[SENTINEL]}])
def test_sensitive_source_blocks_entire_page_without_echo(url, capsys):
    result = project([row(), row(2, url=url)])
    assert result.status == 'sensitive_article_source' and not result.rows
    assert result.source_selection is None and result.summary()['selection_counts'] is None
    assert SENTINEL not in repr(result)+json.dumps(result.summary())+str(capsys.readouterr())


@pytest.mark.parametrize('case', ['callback', 'status', 'missing_status', 'collection', 'sensitive_valid', 'sensitive_excluded', 'duplicate_keys', 'oversize'])
def test_global_checks_never_become_partial_success(case):
    req = ps.make_request('601328.SH', '2026-11-13')
    value = data([row(), row(2, url='http://outside.invalid/x')])
    if case == 'status': value['code'] = 1
    if case == 'missing_status': value = {'result':value['result']}
    if case == 'collection': value['result'] = {'passportWeb': []}
    if case.startswith('sensitive'):
        value['result']['cmsArticleWebOld'][case == 'sensitive_excluded']['content'] = SENTINEL
    body = wire(value, req)
    if case == 'callback': body = body.replace(req.callback, 'wrong', 1)
    if case == 'duplicate_keys': body = body.replace('"code": 0', '"code": 0, "code": 0', 1)
    if case == 'oversize': body = 'x'*(p.MAX_TEXT_CHARACTERS+1)
    result = p.validate_response(req, body, isolate_invalid_sources=True)
    assert result.status != 'accepted' and not result.rows and result.source_selection is None
    assert result.summary()['accepted_count'] is None


def test_all_invalid_empty_and_optional_url_semantics():
    result = project([row(url='http://outside.invalid/x')])
    assert result.status == 'no_usable_articles' and result.summary()['accepted_count'] == 0
    assert result.source_selection.returned == result.source_selection.source_rejected == 1
    assert '新闻不可用' in p.render_source_selection(result)
    assert '部分条目可用' not in p.render_source_selection(result)
    assert project([]).source_selection.returned == 0
    item = row(); del item['url']
    assert project([item], isolate=False).status == 'accepted'
    assert project([item]).source_selection.source_rejected == 1
    assert '未知' not in p.render_source_selection(result).split('接口本页记录')[0]
    excluded = project([row(url='http://outside.invalid/x'),row(2, day='2026-11-14')])
    assert excluded.status == 'no_usable_articles'
    assert '新闻不可用' in p.render_source_selection(excluded)
    assert '部分条目可用' not in p.render_source_selection(excluded)


@pytest.mark.parametrize('extra', [{'content':'different synthetic content'},
    {'date':'2026-11-14 12:00:00'}, {'title':'600919 different stock'}])
def test_conflicting_duplicate_is_global_failure(extra):
    result = project([row(), row(**extra)])
    assert result.status == 'conflicting_article_duplicate' and result.source_selection is None
    assert not result.rows


def test_completed_counts_reject_forged_combinations():
    with pytest.raises(ValueError):
        parser.NewsSelectionCounts(1, 2, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        parser.NewsSelectionCounts(True, 0, 0, 0, 0, 0, 0, 1)
    value = project([row()])
    from dataclasses import replace
    with pytest.raises(ValueError): replace(value, rows=())
    with pytest.raises(ValueError): replace(value, status='article_source_invalid')


def test_one_real_tool_boundary_reused_concurrently_only_admitted_text(tmp_path, monkeypatch):
    from tradingagents.agents.utils.news_data_tools import get_news
    from tradingagents.evidence import ToolExecutionEvidenceEnvelope
    original = owned(tmp_path)
    owner = ps.PowerShellNews(original.executable, original.script, '600919.SH', '2026-11-13', isolate_invalid_sources=True)
    send = Mock(return_value=(wire(data(mixed('600919')), owner.request).encode(), {'status':'ok', 'cleanup':'closed'}))
    monkeypatch.setattr(ps, 'run_child', send)
    budget = budget_module.AnalysisBudget(1,30,allowed_data_vendors=['akshare'])
    monkeypatch.setattr(budget_module,'active_budget',lambda:budget)
    def call(i):
        with ps.powershell_news_scope(owner):
            return get_news.func(ticker=owner.request.ticker,start_date=owner.request.start,end_date=owner.request.end,
                tool_call_id=f'isolation-{i}',state={})
    with ThreadPoolExecutor(max_workers=2) as pool: outputs = list(pool.map(call, range(2)))
    assert send.call_count == 1 and budget._counts == {'data':1,'model':0}
    for content, raw in outputs:
        artifact = ToolExecutionEvidenceEnvelope.model_validate(raw).selected_artifact
        assert artifact is not None and artifact.raw_text == content
        assert '来源排除 2 条' in content and '窗口外 1 条' in content
        assert 'DISCARDED_ARTICLE' not in content+json.dumps(raw)
        assert 'outside.invalid' not in content+json.dumps(raw)


def test_formal_arguments_and_scope_propagate_without_send(tmp_path, monkeypatch):
    module = entry(); executable = str(tmp_path/'pwsh.exe')
    args = ['--ticker','601328.SH','--analysis-date','2026-11-13','--powershell-news',
            '--powershell-news-executable',executable,'--news-source-isolation']
    assert module.parse_arguments(args).news_source_isolation
    assert not module.parse_arguments(args[:-1]).news_source_isolation
    with pytest.raises(module.Stop): module.parse_arguments(['--news-source-isolation'])
    assert module.parse_arguments(['--guided','--news-source-isolation']).news_source_isolation
    root = Path(__file__).resolve().parents[1]
    wrapper = (root/'scripts/Start-MainlandAnalysis.ps1').read_text()
    assert '[switch]$NewsSourceIsolation' in wrapper and "'--news-source-isolation'" in wrapper
    original = owned(tmp_path)
    source = tmp_path/'source'
    (source/'scripts').mkdir(parents=True)
    (source/'scripts/Get-PublicNews.ps1').write_bytes(original.script.read_bytes())
    session = module.Session(tmp_path, source=source, powershell_news_executable=str(original.executable), news_source_isolation=True)
    session.selection = {'ticker':'601328.SH','analysis_date':'2026-11-13'}
    save = Mock()
    cli = SimpleNamespace(DEFAULT_CONFIG={},get_user_selections=lambda:None,
        _build_run_config=lambda:{},_prepare_run_artifacts=lambda:{},save_report_to_disk=save)
    with session.analysis_hooks(cli,{},{}):
        owner = ps.active_powershell_news()
        assert owner.isolate_invalid_sources and not owner.attempted
        owner.value = project(mixed())
        cli.save_report_to_disk({},'601328.SH',tmp_path/'report')
        assert save.call_args.kwargs['news_source_selection'] is owner.value
        with pytest.raises(module.Stop): cli.save_report_to_disk({},'601328.SH',tmp_path,news_source_selection=owner.value)
    assert ps.active_powershell_news() is None and cli.save_report_to_disk is save


@pytest.mark.parametrize('case', ['mixed', 'all_invalid', 'global'])
def test_actual_report_renderer_receives_only_fixed_counts(tmp_path, monkeypatch, case):
    from tradingagents import reporting
    from tests.test_announcement_report import report_state
    from tests.test_supplemental_presentation import MemoryStore
    value = project(mixed() if case=='mixed' else [row(url='http://outside.invalid/x')])
    if case=='global': value = project([row(content=SENTINEL)])
    state = report_state('601328.SH')
    MemoryStore.opened = []
    monkeypatch.setattr(reporting, 'ReportStore', MemoryStore)
    monkeypatch.setattr(reporting, 'write_immutable_decision_audit', lambda *a,**k:None)
    reporting.write_report_tree(state, '601328.SH', tmp_path, output_language='Chinese', news_source_selection=value)
    files = MemoryStore.opened[-1].files
    complete = next(text for name,text in files.items() if name.endswith('/complete_report.md'))
    if isinstance(complete,bytes): complete=complete.decode()
    assert '新闻来源与部分覆盖' in complete and '601328.SS' in complete
    assert 'DISCARDED_ARTICLE' not in complete and SENTINEL not in complete and 'outside.invalid' not in complete
    if case=='mixed': assert '来源排除 2 条' in complete and '最终准入 1 条' in complete
    if case=='all_invalid':
        assert '最终准入 0 条' in complete and '新闻不可用' in complete
        assert '部分条目可用' not in complete
    if case=='global': assert '数量未知' in complete
    with pytest.raises(ValueError): p.render_source_selection_report(value,'600919.SH','2026-11-13')
    with pytest.raises(ValueError): p.render_source_selection_report(value,'601328.SH','2026-11-14')
