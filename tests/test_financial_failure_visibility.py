"""Synthetic financial failures reach reports, never factual source catalogs."""
import copy
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.agents.analysts import submission as s
from tradingagents.dataflows.financial_dispatch import FinancialToolMessageAuditEnvelope
from tradingagents.evidence import (
    EvidenceSource, EvidenceState, MaterialClaim, SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable, SourceArtifact,
)

SYMBOL = '601328.SS'
DATE = '2026-10-16'
REF = 'financial-request:v1:' + 'a' * 64


def current(*, reason='empty_frame', required=False, duplicate=False):
    call = dict(name='get_balance_sheet', id='synthetic-financial', type='tool_call',
                args=dict(ticker=SYMBOL, curr_date=DATE, freq='quarterly'))
    outcome = SourceAcquisitionUnavailable(provider='synthetic', capability='company_financials',
        source_ref=REF, attempt=1, retrieved_at=DATE+'T00:00:00Z',
        reason=reason, error_code='synthetic_failure', retryable=False)
    envelope = FinancialToolMessageAuditEnvelope(tool_call_id=call['id'], tool_name=call['name'],
        request_ref=REF, disposition='executed', acquisition_outcomes=(outcome,))
    content = json.dumps(dict(capability='company_financials', reason=reason, request_ref=REF))
    message = ToolMessage(content=content, tool_call_id=call['id'], name=call['name'],
                          artifact=envelope.model_dump(mode='json'), status='error')
    messages = [AIMessage(content='', tool_calls=[call]), message]
    if duplicate:
        again = copy.deepcopy(call); again['id'] = 'synthetic-repeat'
        artifact = envelope.model_copy(update=dict(tool_call_id=again['id'], disposition='duplicate_suppressed'))
        messages += [AIMessage(content='', tool_calls=[again]), ToolMessage(content=content,
            tool_call_id=again['id'], name=again['name'], artifact=artifact.model_dump(mode='json'), status='error')]
    evidence = EvidenceState(sources=(EvidenceSource(source_id=REF, status='available', required=True),)
                             if required else ())
    return dict(company_of_interest=SYMBOL, trade_date=DATE, messages=messages,
                evidence_state=evidence.model_dump(mode='json'))


def submit(value, claims=()):
    result = s.AnalystSubmissionResult(message=AIMessage(content='synthetic complete'),
        report='合成辅助报告', claims=tuple(claims), submission_source=EvidenceSource(
            source_id='analyst.fundamentals.submission', status='available', required=True))
    return EvidenceState.model_validate(s.build_analyst_update(value, result, 'fundamentals_report')['evidence_state'])


@pytest.mark.parametrize('reason', ['empty_frame', 'disconnect'])
def test_uncited_financial_failure_reaches_evidence_and_report(reason):
    from tradingagents.reporting import _render_acquisition_outcomes
    value = current(reason=reason)
    before = copy.deepcopy(value)
    evidence = submit(value)
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.acquisition_outcomes[0].reason.value == reason
    assert next(x for x in evidence.sources if x.source_id == REF).status.value == 'unavailable'
    assert evidence.source_facts == evidence.source_artifacts == ()
    report = '\n'.join(_render_acquisition_outcomes(evidence))
    assert reason in report and 'company_financials' in report
    assert REF not in s.allowed_source_refs(value['messages'], SYMBOL, DATE)
    assert REF not in s.render_allowed_source_ref_catalog(value['messages'], SYMBOL, DATE)
    assert value == before


def test_duplicate_failure_is_one_outcome_and_one_unavailable_source():
    value = current(duplicate=True)
    evidence = submit(value)
    assert len(evidence.acquisition_outcomes) == 1
    assert len([x for x in evidence.sources if x.source_id == REF]) == 1
    assert len(s.source_ref_tool_call_ids(value['messages'], SYMBOL, DATE,
        include_unavailable_financial=True)[REF]) == 2
    assert s.allowed_source_refs(value['messages'], SYMBOL, DATE) == ()


@pytest.mark.parametrize('case', ['stock', 'date', 'call_id', 'tool_name', 'unknown_field',
                                 'missing_envelope', 'invented_digest'])
def test_invalid_binding_or_plain_failure_text_cannot_mint_outcome(case):
    value = current()
    call = value['messages'][0].tool_calls[0]
    message = value['messages'][1]
    if case == 'stock': call['args']['ticker'] = '000001.SZ'
    if case == 'date': call['args']['curr_date'] = '2026-10-17'
    if case == 'call_id': message.artifact['tool_call_id'] = 'unpaired'
    if case == 'tool_name': message.artifact['tool_name'] = 'get_cashflow'
    if case == 'unknown_field': message.artifact['extra'] = 'SECRET_SENTINEL'
    if case == 'missing_envelope':
        message.artifact = None
        # An arbitrary provider string must never be parsed into a typed failure.
        message.content += ' SECRET_SENTINEL'
    if case == 'invented_digest': message.artifact['artifact_sha256'] = 'f' * 64
    evidence = submit(value)
    assert evidence.acquisition_outcomes == ()
    assert evidence.source_facts == evidence.source_artifacts == ()
    assert 'SECRET_SENTINEL' not in evidence.model_dump_json()


def test_failure_text_cannot_support_financial_claim():
    value = current()
    claim = MaterialClaim(claim_id='fundamentals.fabricated_zero', analyst='fundamentals',
        statement='Net profit is zero.', source_refs=(REF,), source_quote=value['messages'][1].content)
    evidence = submit(value, [claim])
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.source_facts == evidence.source_artifacts == ()
    assert evidence.claim_validations[0].status.value == 'unsupported'


def test_required_status_survives_failure_collection():
    evidence = submit(current(required=True))
    source = next(x for x in evidence.sources if x.source_id == REF)
    assert source.required is True and source.status.value == 'unavailable'


def test_available_financial_catalog_behavior_unchanged():
    from hashlib import sha256
    value = current()
    message = value['messages'][1]
    content = 'SYNTHETIC NOT OBSERVED DATA\nmetric,value\nTotal,123'
    artifact = SourceArtifact(tool_name=message.name, tool_call_id=message.tool_call_id,
        source_ref=REF, raw_text=content, artifact_sha256=sha256(content.encode()).hexdigest())
    outcome = SourceAcquisitionAvailable(provider='synthetic', capability='company_financials',
        source_ref=REF, attempt=1, retrieved_at=DATE+'T00:00:00Z', artifact=artifact)
    envelope = FinancialToolMessageAuditEnvelope(tool_call_id=message.tool_call_id,
        tool_name=message.name, request_ref=REF, disposition='executed',
        artifact_sha256=artifact.artifact_sha256, acquisition_outcomes=(outcome,))
    message.content = content; message.artifact = envelope.model_dump(mode='json'); message.status = 'success'
    assert s.allowed_source_refs(value['messages'], SYMBOL, DATE) == (REF,)
    evidence = submit(value)
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.acquisition_outcomes[0].outcome == 'available'


def test_failure_is_not_added_to_model_finalizer_catalog():
    value = current()
    prompt = s._finalization_prompt(analyst='fundamentals', ticker=SYMBOL, trade_date=DATE,
        draft='合成辅助草稿', messages=value['messages'])
    assert REF not in prompt and value['messages'][1].content not in prompt


@pytest.mark.parametrize('language', ['Chinese', 'English'])
def test_full_report_keeps_failure_visible_without_changing_decision(monkeypatch, language):
    from tests.test_multiyear_integration import combined_state
    from tests.test_supplemental_presentation import MemoryStore, export
    from tradingagents import reporting
    from tradingagents.terminal_contract import apply_terminal_contract
    value, _ = combined_state()
    failure = current(duplicate=True, reason='disconnect')
    for message in failure['messages']:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call['args'].update(ticker=value['company_of_interest'], curr_date=value['trade_date'])
    value['messages'] = failure['messages']
    decision_before = copy.deepcopy(value['trading_decision'])
    value['evidence_state'] = submit(value).model_dump(mode='json')
    def audit(state, *args, **kwargs):
        apply_terminal_contract(state, audit_digest='b' * 64)
        state['decision_audit_created_at'] = '2026-07-18T00:00:00Z'
    monkeypatch.setattr(reporting, 'ReportStore', MemoryStore)
    monkeypatch.setattr(reporting, 'write_immutable_decision_audit', audit)
    report = export(value, None, language)['complete_report.md'].decode()
    assert 'disconnect' in report and 'company_financials' in report
    assert value['trading_decision'] == decision_before
    assert len([x for x in value['evidence_state']['acquisition_outcomes'] if x['source_ref'] == REF]) == 1
