"""Real submission/acquisition and CLI artifact boundary, synthetic inputs only."""
import gzip
import json
from contextlib import nullcontext

import pytest

from tests.test_stock_data_evidence_binding import (
    evidence_for,
    snapshot_provider as _snapshot_provider,
    stock_messages,
)
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run


@pytest.fixture
def snapshot_provider(monkeypatch):
    return _snapshot_provider.__wrapped__(monkeypatch)


def capture(tmp_path):
    import cli.main as cli
    from cli.runtime_artifacts import RuntimeArtifactWriter
    artifacts = {'artifact_root': tmp_path / 'artifacts', 'log_file': tmp_path / 'log.txt'}
    writer = RuntimeArtifactWriter(artifact_root=artifacts['artifact_root'],
        log_paths=[artifacts['log_file']], metrics_path=tmp_path / 'metrics.json')
    artifacts['runtime_writer'] = writer
    scope = getattr(cli, '_run_diagnostic_scope', lambda a: nullcontext())(artifacts)
    return scope, writer, artifacts


def events(artifacts):
    return [json.loads(gzip.decompress(p.read_bytes())) for p in artifacts['artifact_root'].glob('sha256/*/*.json.gz')]


@pytest.mark.parametrize('value,status', [('11.0','supported'), ('99.99','unsupported')])
def test_actual_claim_validation_is_durably_recorded(snapshot_provider, tmp_path, value, status):
    scope, writer, artifacts = capture(tmp_path)
    try:
        with scope, authoritative_snapshot_run():
            messages = stock_messages()
            result = evidence_for(messages, statement='The latest close is ' + value + '.')
            if status == 'unsupported':
                assert any(e.get('kind') == 'claim_validation' for e in events(artifacts))
                assert 'artifact=sha256:' in artifacts['log_file'].read_text()
        assert result.claim_validations[0].status.value == status
    finally:
        writer.close()
    rows = [e for e in events(artifacts) if e.get('kind') == 'claim_validation']
    assert rows, 'complete entry lost claim diagnostic before canonical audit projection'
    row=rows[0]['payload']
    assert row['claim']['statement']['text'] == 'The latest close is ' + value + '.'
    assert row['validation']['status'] == status
    assert row['trusted_facts'][0]['artifact_sha256']
    assert row['numeric']['comparisons']
    assert 'artifact=sha256:' in artifacts['log_file'].read_text()


def test_actual_news_primary_and_cleanup_retained(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tradingagents.dataflows import akshare_data
    from tradingagents.dataflows.acquisition import AcquisitionFailure
    def close():
        raise OSError('Authorization: Bearer do-not-retain')
    monkeypatch.setattr(akshare_data.requests, 'get', lambda *a,**kw: SimpleNamespace(
        status_code=503, close=close))
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope, pytest.raises(AcquisitionFailure) as caught:
            akshare_data._stock_news_em('601328','2026-10-01','2026-10-16')
        assert caught.value.error_code == 'news_http_status' and caught.value.status_code == 503
    finally:
        writer.close()
    rows=[e['payload'] for e in events(artifacts) if e.get('kind')=='news_exception']
    assert {r['role'] for r in rows} == {'primary','cleanup'}, 'news failure details were discarded'
    assert {r['stage'] for r in rows} == {'http_status','response_close'}
    assert 'do-not-retain' not in json.dumps(rows)


@pytest.mark.parametrize('cleanup_retryable', [False, True])
def test_news_retention_failure_cannot_be_downgraded_by_cleanup(monkeypatch, cleanup_retryable):
    from types import SimpleNamespace

    from tradingagents.dataflows import akshare_data
    from tradingagents.dataflows.acquisition import (
        AcquisitionController,
        AcquisitionFailure,
        AcquisitionRequest,
    )
    from tradingagents.evidence import AcquisitionUnavailableReason
    from tradingagents.run_diagnostics import DiagnosticPersistenceError, diagnostic_scope

    calls, events_seen = [], []
    def close():
        if cleanup_retryable:
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.PROVIDER_ERROR, retryable=True)
        raise RuntimeError('cleanup-secret-not-retained')
    monkeypatch.setattr(akshare_data.requests, 'get', lambda *a,**kw: SimpleNamespace(status_code=503, close=close))
    def primary(request):
        calls.append('primary')
        return akshare_data._stock_news_em('601328', '2026-10-01', '2026-10-16')
    def fallback(request):
        calls.append('fallback')
        raise AssertionError('fallback must not run after failed retention')
    def sink(event):
        events_seen.append(event)
        if len(events_seen)==1:
            raise OSError('retention-secret-not-retained')
    with diagnostic_scope(sink), pytest.raises(DiagnosticPersistenceError, match='diagnostic_persistence_failed'):
        AcquisitionController(providers=(('akshare',primary),('yfinance',fallback))).acquire(
            AcquisitionRequest(tool_call_id='synthetic',tool_name='get_news',capability='get_news',source_ref='synthetic'),
            validator=lambda value:value, serializer=lambda value:value)
    assert calls==['primary']
    assert [event['payload']['role'] for event in events_seen]==['primary','cleanup']
    assert 'secret-not-retained' not in json.dumps(events_seen)


@pytest.mark.parametrize('statement,status', [('50日均线为4.29。','supported'),
    ('50日均线为4.30。','unsupported'), ('10日均线为4.29。','unsupported')])
def test_indicator_identity_and_exact_comparator_are_observed(tmp_path, statement, status):
    from tests.test_indicator_claim_metadata import evaluate
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            evidence=evaluate(statement,'2026-10-16,close_50_sma,4.2900')
    finally:
        writer.close()
    row=events(artifacts)[0]['payload']
    assert evidence.claim_validations[0].status.value == row['validation']['status'] == status
    assert row['numeric']['indicator_observation'] == {'indicator':'close_50_sma','date':'2026-10-16','value':'4.2900'}
    if statement.startswith('10'):
        assert row['numeric']['indicator_metadata_error']
    else:
        comparison=next(c for c in row['numeric']['comparisons'] if c['claim_token'].startswith('4.'))
        assert comparison['absolute_tolerance'] == '0.005'
        candidate=next(c for c in comparison['candidates'] if c['source_token']=='4.2900')
        assert candidate['within_numeric_tolerance'] is (status=='supported')


@pytest.mark.parametrize('secret', [
    'Authorization: Bearer forbidden-marker', '{"Authorization":"Basic forbidden-marker"}',
    '{"api_key":"123456789"}', "{'token': '123456789'}", 'Cookie: session=forbidden-marker',
    'https://example.invalid/path?secret=forbidden-marker', 'sk-forbidden-marker',
])
def test_text_and_numeric_derivatives_do_not_leak_known_secret_shapes(tmp_path, secret):
    from tests.test_indicator_claim_metadata import evaluate
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            evaluate(secret, '2026-10-16,close_50_sma,4.2900')
    finally:
        writer.close()
    serialized=json.dumps(events(artifacts))
    assert 'forbidden-marker' not in serialized and '123456789' not in serialized
    assert events(artifacts)[0]['payload']['numeric'] is None
    assert secret not in artifacts['log_file'].read_text()


def test_persistence_failure_never_spends_structured_repair():
    from unittest.mock import Mock

    from tradingagents.agents.utils.structured import invoke_required_structured
    from tradingagents.run_diagnostics import DiagnosticPersistenceError, diagnostic_scope
    def broken(event):
        raise ValueError('secret-not-logged')
    llm=Mock()
    llm.invoke.return_value={'valid': True}
    with diagnostic_scope(broken), pytest.raises(DiagnosticPersistenceError, match='diagnostic_persistence_failed'):
        invoke_required_structured(llm, 'synthetic prompt', 'Market Analyst')
    assert llm.invoke.call_count == 1


@pytest.mark.parametrize('statement,quote', [
    ('The value is 123456789.', '{"api_key":"123456789"}'),
    ('{"api_key":"123456789"}', 'The value is 123456789.'),
])
def test_sensitive_value_copied_between_claim_and_quote_is_fully_redacted(tmp_path, statement, quote):
    from tests.test_indicator_claim_metadata import evaluate
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            evaluate(statement, quote)
    finally:
        writer.close()
    rows=events(artifacts)
    assert rows and '123456789' not in json.dumps(rows)
    row=rows[0]['payload']
    assert row['claim']['statement']['text']==row['claim']['source_quote']['text']=='[REDACTED]'
    assert row['numeric'] is None and row['trusted_facts']==[]


@pytest.mark.parametrize('stage', ['provider_call','validation','serialization'])
def test_news_controller_records_exact_failure_stage_without_retry(tmp_path, stage):
    from tradingagents.dataflows.acquisition import AcquisitionController, AcquisitionRequest
    calls=[]
    def fail():
        raise KeyError('secret-not-logged')
    def provider(request):
        calls.append(request)
        return fail() if stage=='provider_call' else 'fixture'
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            result=AcquisitionController(providers=(('akshare',provider),)).acquire(
                AcquisitionRequest(tool_call_id='synthetic', tool_name='get_news',
                    capability='get_news',source_ref='synthetic'),
                validator=lambda x: fail() if stage=='validation' else x,
                serializer=lambda x: fail() if stage=='serialization' else x)
    finally:
        writer.close()
    row=events(artifacts)[0]['payload']
    assert row=={'error_type':'KeyError','error_code':None,'stage':stage,'role':'primary','provider':'akshare','http_status':None}
    assert len(calls)==1 and result.artifact is None and len(result.outcomes)==1
    assert 'secret-not-logged' not in json.dumps(events(artifacts))


@pytest.mark.parametrize('code,recorded', [('news_business_status_unverified','news_business_status_unverified'),
    ('news_schema_invalid','news_schema_invalid'), ('opaque_forbidden_marker','other')])
def test_news_fixed_parser_code_is_retained_without_arbitrary_provider_text(tmp_path, code, recorded):
    from tradingagents.dataflows.acquisition import AcquisitionFailure
    from tradingagents.evidence import AcquisitionUnavailableReason
    from tradingagents.run_diagnostics import record_news_exception
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            record_news_exception(AcquisitionFailure(reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
                error_code=code), stage='response_parse', status=200)
    finally:
        writer.close()
    row=events(artifacts)[0]['payload']
    assert row['error_code']==recorded and row['http_status']==200
    assert 'opaque_forbidden_marker' not in json.dumps(events(artifacts))


@pytest.mark.parametrize('case', ['timeout','connection','http401','http429','http503','parse','local','unknown'])
def test_news_safe_exception_allowlist(tmp_path, case):
    from types import SimpleNamespace

    import requests

    from tradingagents.run_diagnostics import record_news_exception
    class Unknown(Exception):
        @property
        def status_code(self):
            raise AssertionError('unknown exception attributes must not be accessed')
    exceptions={'timeout':requests.exceptions.ReadTimeout('secret-not-logged'),
        'connection':requests.exceptions.ConnectionError('secret-not-logged'),
        'parse':json.JSONDecodeError('secret-not-logged','secret-not-logged',0),
        'local':RuntimeError('secret-not-logged'),'unknown':Unknown('secret-not-logged')}
    error=exceptions.get(case)
    if case.startswith('http'):
        error=requests.exceptions.HTTPError('secret-not-logged',response=SimpleNamespace(
            status_code=int(case[4:]),headers={'Authorization':'secret-not-logged'},url='https://private.invalid'))
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            record_news_exception(error, stage='provider_call')
    finally:
        writer.close()
    row=events(artifacts)[0]['payload']
    assert row['error_type'] != 'other' if case!='unknown' else row['error_type']=='other'
    assert row['http_status'] == (int(case[4:]) if case.startswith('http') else None)
    assert 'secret-not-logged' not in json.dumps(events(artifacts))


def test_scope_cleanup_and_output_usage_no_raw_response(tmp_path):
    from unittest.mock import Mock

    from langchain_core.messages import AIMessage

    from tradingagents.agents.analysts.submission import process_analyst_response
    from tradingagents.agents.utils.structured import invoke_required_structured
    from tradingagents.run_diagnostics import emit
    response=AIMessage(content='raw-not-to-retain',response_metadata={'finish_reason':'length',
        'token_usage':{'completion_tokens':65536,'completion_tokens_details':{'reasoning_tokens':60000}}})
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            llm=Mock()
            result=process_analyst_response(response,finalizer=llm,analyst='market',ticker='601328.SS',
                trade_date='2026-10-16',messages=[])
            assert result.submission_source.detail=='output_limit' and llm.invoke.call_count==0
            llm.invoke.return_value={'raw':AIMessage(content='raw-not-to-retain',
                response_metadata={'finish_reason':'stop'}, usage_metadata={'input_tokens':1,'output_tokens':9,'total_tokens':10}),
                'parsed':{'valid':True},'parsing_error':None}
            result=invoke_required_structured(llm,'synthetic','Market Analyst')
            assert result.value=={'valid':True} and llm.invoke.call_count==1
        emit('analyst_output', {'must_not_capture':True})
    finally:
        writer.close()
    rows=events(artifacts)
    assert len(rows)==2 and all(e['kind']=='analyst_output' for e in rows)
    assert 'raw-not-to-retain' not in json.dumps(rows)
    assert {e['payload']['stage'] for e in rows}=={'initial_response','finalizer'}
    first=next(e['payload'] for e in rows if e['payload']['stage']=='initial_response')
    assert first['reasoning_tokens']==60000 and first['completion_tokens']==65536
    assert first['outcome']=='output_limit'


def test_actual_cli_graph_retains_diagnostics_at_publication_boundary(tmp_path, monkeypatch):
    """Real CLI/Graph/ToolNode/budget/gates; only wires/UI/publication substituted."""
    import http.client
    import inspect
    import re
    from pathlib import Path
    from types import SimpleNamespace

    import cli.main as cli
    from tests.test_analysis_budget import httpx_families
    from tests.test_analysis_budget_e2e import FakeWire, prepare
    from tests.test_optional_source_e2e import (
        _install_core_wire,
        _retain_model_transport_settings,
    )

    native=http.client.HTTPSConnection
    signatures={transport:inspect.signature(transport.__init__) for module in httpx_families()
                for transport in (module.HTTPTransport,module.AsyncHTTPTransport)}
    settings,wire,cache,before=prepare(tmp_path,monkeypatch)
    _retain_model_transport_settings(monkeypatch,signatures)
    _install_core_wire(monkeypatch,wire,native,news_available=False)
    home=tmp_path/'synthetic-home'
    home.mkdir()
    monkeypatch.setenv('USERPROFILE',str(home))
    monkeypatch.setenv('TRADINGAGENTS_CACHE_DIR',str(tmp_path/'cache'))
    settings.update(analysis_skip_optional_fundamentals=True,analysis_max_calls=60,
        llm_max_output_tokens=65536,deepseek_reasoning_effort='high',llm_max_retries=0,
        checkpoint_data_dir=str(tmp_path/'checkpoint-state'),checkpoint_enabled=True)
    monkeypatch.setattr(cli,'DEFAULT_CONFIG',settings)
    selection={'ticker':'601328.SS','asset_type':'stock','analysis_date':'2026-10-16','research_depth':1,
        'shallow_thinker':'deepseek-flash','deep_thinker':'deepseek-v4-pro','backend_url':'https://api.deepseek.com',
        'llm_provider':'deepseek','analysts':[SimpleNamespace(value=k) for k in ('market','social','news','fundamentals')],
        'china_a_enhancement_preset':'basic','output_language':'Chinese'}
    monkeypatch.setattr(cli,'get_user_selections',lambda:selection)
    monkeypatch.setattr(cli.typer,'prompt',lambda *a,**kw:'N')
    class Display:
        def __getattr__(self,name):
            return lambda *a,**kw:None
    monkeypatch.setattr(cli,'create_run_display',lambda *a,**kw:Display())
    states=[]
    class OfflinePublicationBoundary(BaseException):
        pass
    def publish(state,ticker,path,**kw):
        # Stop before the distinct protected native publisher. Do not replace
        # its security implementation, claim publication success, or retry it.
        states.append(state)
        raise OfflinePublicationBoundary()
    monkeypatch.setattr(cli,'save_report_to_disk',publish)
    import tradingagents.report_exports as report_exports
    # Native protected-directory publication is explicitly not exercised. The
    # prior WinError 5 remains BLOCKED; no retry/ACL change/access to that target.
    monkeypatch.setattr(report_exports,'write_advisory_draft',lambda root,name,text:Path(root)/name)
    original=FakeWire.httpx
    def wire_response(self,transport,request,module):
        response=original(self,transport,request,module)
        body=json.loads(request.content)
        assert body.get('max_tokens',body.get('max_completion_tokens'))==65536
        assert body['reasoning_effort']=='high'
        text='\n'.join(str(m.get('content') or '') for m in body['messages'])
        if 'Finalize the market analyst' in text and body.get('response_format',{}).get('type')=='json_object':
            quote=re.search(r'(?m)^2026-10-16,[^\r\n]+',text).group(0)
            ref=next(x for x in re.findall(r'(?m)^- ([^\s]+)',text) if x.startswith('snapshot:'))
            payload=response.json()
            payload['choices'][0]['message']['content']=json.dumps({'report_markdown':'Synthetic unmatched close.',
                'material_claims':[{'claim_id':'market.retained_wrong_close','statement':'The close is 99999999.',
                'source_ref':ref,'source_quote':quote}]})
            return module.Response(200,request=request,json=payload)
        return response
    monkeypatch.setattr(FakeWire,'httpx',wire_response)
    with pytest.raises(OfflinePublicationBoundary):
        cli.run_analysis(checkpoint=True,mainland_core_sources=True)
    assert states
    from tradingagents.evidence import (
        DraftThesis,
        EvidenceState,
        decision_ready_material_claims,
        evaluate_decision_gate,
    )
    evidence = EvidenceState.model_validate(states[-1]['evidence_state'])
    rejected = next(v for v in evidence.claim_validations if v.claim_id == 'market.retained_wrong_close')
    assert rejected.status.value == 'unsupported'
    assert 'market.retained_wrong_close' not in {c.claim_id for c in decision_ready_material_claims(evidence)}
    # Existing policy can still use independent canonical facts. This rejected
    # prose must not become a directional premise simply because it was logged.
    gate = evaluate_decision_gate(DraftThesis(rating='BUY', narrative='Synthetic invalid premise.',
        material_claim_ids=('market.retained_wrong_close',)), evidence)
    assert not gate.permitted and any('unsupported' in d for d in gate.diagnostics)
    all_events=events({'artifact_root':Path(settings['results_dir'])/'runtime_artifacts'})
    claims=[e['payload'] for e in all_events if isinstance(e,dict) and e.get('kind')=='claim_validation']
    row=next(c for c in claims if c['claim']['claim_id']['text']=='market.retained_wrong_close')
    assert row['validation']['status']=='unsupported'
    assert row['numeric']['unsupported_tokens']==['99999999']
    outputs=[e['payload'] for e in all_events if isinstance(e,dict) and e.get('kind')=='analyst_output']
    assert any(e['analyst']=='market' and e['stage']=='finalizer' for e in outputs)
    news=[e for e in all_events if isinstance(e,dict) and e.get('kind')=='news_exception']
    assert news
    assert states[-1]['analysis_budget']['admitted_attempts']<=60
    assert len(wire.financial_calls)==3  # Synthetic fixture seeding only; no refresh during analysis.
