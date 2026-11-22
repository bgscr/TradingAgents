"""Invented news only; compatibility without raw-body retention or live requests."""
import copy
import json
from unittest.mock import Mock

import pytest

from tradingagents import news_projection as projection
from tradingagents.dataflows import eastmoney_news as parser
from tradingagents.powershell_news import make_request

SENTINEL='authorization=Bearer_SYNTHETIC_DO_NOT_RETAIN'


def envelope(symbol='601328', day='2026-11-13'):
    return {'code':0,'msg':'OK','bizCode':'','bizMsg':'','extra':{},'hitsTotal':1,
        'searchId':'00000000-0000-4000-8000-000000000001',
        'result':{'cmsArticleWebOld':[{'date':day+' 12:00:00','code':'202611130000000001',
            'title':symbol+' synthetic news','content':'Synthetic description','mediaName':'Synthetic',
            'image':'','url':'http://bank.eastmoney.com/a/202611130000000001.html'}]}}


def wire(value,req=None):
    req=req or make_request('601328.SH','2026-11-13')
    return req.callback+'('+json.dumps(value,ensure_ascii=False)+')'


def validate(value):
    return projection.validate_response(make_request('601328.SH','2026-11-13'),wire(value))


@pytest.mark.parametrize('field',['extra','hitsTotal','searchId','bizCode','bizMsg'])
def test_optional_envelope_metadata_can_be_missing(field):
    data=envelope();del data[field]
    result=validate(data)
    assert result.status=='accepted' and len(result.rows)==1


@pytest.mark.parametrize('field',['image','url','mediaName','content'])
def test_optional_article_fields_can_be_missing_without_inventing_facts(field):
    data=envelope();del data['result']['cmsArticleWebOld'][0][field]
    result=validate(data)
    assert result.status=='accepted'
    if field=='content':assert result.rows[0][4]==''
    if field=='mediaName':assert result.rows[0][1]==''
    assert result.rows[0][5]=='https://finance.eastmoney.com/a/202611130000000001.html'


def test_unknown_fields_projected_out_before_diagnostics_and_evidence(monkeypatch,capsys):
    data=envelope();row=data['result']['cmsArticleWebOld'][0]
    data[SENTINEL]={'token':SENTINEL};data['extra']={'Cookie':SENTINEL};data['searchId']=SENTINEL
    data['hitsTotal']={'secret':SENTINEL};data['result']['extension']={'secret':SENTINEL}
    row['image']=SENTINEL;row['extension']={'token':SENTINEL}
    req=make_request('601328.SH','2026-11-13')
    projected,matched,status=parser.project_news_payload(wire(data).encode(),req.callback)
    assert matched and status=='projected_public_news_envelope'
    assert set(projected)=={'code','msg','result'}
    assert set(projected['result'])=={'cmsArticleWebOld'}
    assert set(projected['result']['cmsArticleWebOld'][0])=={'date','code','title','content','mediaName'}
    from tradingagents import run_diagnostics
    calls=[]
    monkeypatch.setattr(run_diagnostics,'record_news_response_shape',lambda value,**kw:calls.append(value))
    result=validate(data)
    assert result.status=='accepted'
    assert SENTINEL not in json.dumps([projected,result.rows,result.summary(),calls])+str(capsys.readouterr())
    # The raw-retaining default diagnostic must continue rejecting this body.
    assert parser.public_news_payload(wire(data).encode(),req.callback)[0]=='unknown_envelope_fields'


@pytest.mark.parametrize('field,value',[('code',False),('code','0'),('code',1),('msg','error'),
    ('bizCode','1'),('bizMsg','failure'),('status','OK'),('success',True),('error',None)])
def test_known_noncore_metadata_is_not_an_excuse_to_ignore_business_signals(field,value):
    data=envelope();data[field]=value
    result=validate(data)
    assert result.status=='business_status_unverified' and result.rows==()


@pytest.mark.parametrize('field,value',[('success',False),('error',SENTINEL),('status','failed')])
@pytest.mark.parametrize('where',['root','result'])
def test_explicit_failure_never_passes_or_echoes(field,value,where,capsys):
    data=envelope();target=data if where=='root' else data['result'];target[field]=value
    result=validate(data)
    assert result.status=='business_failure' and result.rows==()
    assert SENTINEL not in repr(result)+json.dumps(result.summary())+str(capsys.readouterr())


@pytest.mark.parametrize('field',['code','msg'])
def test_core_business_missing_cannot_mean_success(field):
    data=envelope();del data[field]
    assert validate(data).status=='business_status_unverified'


@pytest.mark.parametrize('field',['date','code','title'])
def test_article_core_missing_is_explicit(field):
    data=envelope();del data['result']['cmsArticleWebOld'][0][field]
    assert validate(data).status=='article_core_missing'


@pytest.mark.parametrize('value',[{},[] ,5,'https://untrusted.invalid/news',SENTINEL,None])
def test_provided_conflicting_article_source_is_not_ignored(value):
    data=envelope();data['result']['cmsArticleWebOld'][0]['url']=value
    assert validate(data).status=='article_source_invalid'


def test_rejected_before_selection_has_unknown_not_zero_exclusions():
    data=envelope();data['result']['cmsArticleWebOld'][0]['url']=None
    result=validate(data)
    assert result.summary()['excluded_counts']=={} and result.summary()['accepted_count'] is None
    assert result.summary()['actual_date_min'] is None and result.summary()['actual_date_max'] is None
    data=envelope(symbol='000001')
    result=validate(data)
    assert result.status=='no_usable_articles' and result.summary()['excluded_counts']['wrong_stock']==1


@pytest.mark.parametrize('kind',['callback','javascript','duplicate','deep','oversize','passport','null','nonlist','too_many','nonfinite'])
def test_protocol_structure_and_other_collection_remain_closed(kind):
    req=make_request('601328.SH','2026-11-13');data=envelope();text=None
    if kind=='callback':text=wire(data).replace(req.callback,'other',1)
    if kind=='javascript':text=wire(data)+';alert(1)'
    if kind=='duplicate':text=wire(data).replace('"code": 0','"code": 0, "code": 0',1)
    if kind=='deep':
        node={};data['extra']=node
        for _ in range(40):node['x']={};node=node['x']
    if kind=='oversize':text='x'*(projection.MAX_TEXT_CHARACTERS+1)
    if kind=='passport':data['result']={'passportWeb':[]}
    if kind=='null':data['result']['cmsArticleWebOld']=None
    if kind=='nonlist':data['result']['cmsArticleWebOld']={}
    if kind=='too_many':data['result']['cmsArticleWebOld']*=11
    if kind=='nonfinite':text=wire(data).replace('"extra": {}','"extra": NaN')
    expected={'callback':'callback_mismatch','javascript':'trailing_syntax','duplicate':'duplicate_json_key',
        'deep':'structure_limit','oversize':'input_limit','passport':'articles_missing','null':'articles_null',
        'nonlist':'articles_not_list','too_many':'article_limit','nonfinite':'nonfinite_json_number'}
    result=projection.validate_response(req,text or wire(data))
    assert result.status==expected[kind] and result.rows==()


def test_unordered_dates_stock_and_empty_collection_keep_semantics():
    data=envelope();data['result']['cmsArticleWebOld']=[envelope(symbol,day)['result']['cmsArticleWebOld'][0]
        for symbol,day in [('601328','2026-11-10'),('601328','2026-11-14'),('600919','2026-11-13'),
                           ('601328','2026-11-13'),('601328','2026-11-05')]]
    result=validate(data)
    assert result.status=='accepted' and len(result.rows)==2
    assert dict(result.excluded)=={'malformed':0,'undated':0,'wrong_stock':1,'outside_window':2}
    data['result']['cmsArticleWebOld']=[]
    assert validate(data).status=='no_usable_articles'


@pytest.mark.parametrize('day',['2026-11-13','2026-11-14'])
def test_sensitive_retained_fields_checked_before_date_exclusion(day):
    data=envelope(day=day);data['result']['cmsArticleWebOld'][0]['content']='auth<em>orization</em>=Bearer_SYNTHETIC'
    assert validate(data).status=='sensitive_article_text'


@pytest.mark.parametrize('symbol',['600919','601328'])
def test_existing_observed_core_projects_identically(symbol):
    data=envelope(symbol=symbol);req=make_request(symbol+'.SH','2026-11-13')
    baseline=parser.parse_news_page(wire(data,req).encode(),expected_callback=req.callback,ticker=req.ticker,
        start_date=req.start,end_date=req.end)
    result=projection.validate_response(req,wire(data,req))
    assert result.status=='accepted'
    assert [dict(zip(projection._ROW_FIELDS,row)) for row in result.rows]==list(baseline.rows)


def test_real_tool_evidence_report_and_budget_discard_extensions(tmp_path,monkeypatch):
    from tests.test_powershell_news import owned
    from tradingagents import analysis_budget as b, powershell_news as ps
    from tradingagents.agents.utils.news_data_tools import get_news
    from tradingagents.evidence import ToolExecutionEvidenceEnvelope,EvidenceState,merge_source_acquisition_outcomes
    from tradingagents.reporting import _render_acquisition_outcomes
    owner=owned(tmp_path);data=envelope(symbol=owner.request.ticker[:6]);data['extra']={'token':SENTINEL}
    budget=b.AnalysisBudget(1,30,allowed_data_vendors=['akshare'])
    monkeypatch.setattr(b,'active_budget',lambda:budget)
    send=Mock(return_value=(wire(data,owner.request).encode(),{'status':'ok'}))
    monkeypatch.setattr(ps,'run_child',send)
    with ps.powershell_news_scope(owner):
        outputs=[get_news.func(ticker=owner.request.ticker,start_date=owner.request.start,end_date=owner.request.end,
            tool_call_id='projected-'+str(i),state={}) for i in range(2)]
    assert send.call_count==1 and budget._counts=={'data':1,'model':0}
    for content,artifact in outputs:
        typed=ToolExecutionEvidenceEnvelope.model_validate(artifact)
        assert typed.selected_artifact is not None and 'PowerShell' in content
        evidence=merge_source_acquisition_outcomes(EvidenceState(),typed.acquisition_outcomes)
        report='\n'.join(_render_acquisition_outcomes(evidence))
        assert 'PowerShell' in report and SENTINEL not in content+json.dumps(artifact)+report
