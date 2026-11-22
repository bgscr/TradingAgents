"""Actual submission -> gate -> existing CLI artifact sink; no real calls."""
import json

import pytest

from tests.test_evidence_notes import LIMIT, note, state, submit
from tests.test_run_diagnostic_retention import capture, events
from tradingagents.evidence import evaluate_admission_gate
from tradingagents.run_diagnostics import DiagnosticPersistenceError, diagnostic_scope


@pytest.mark.parametrize('case,reason,admitted', [
    ('bound','source_bound_quote',True), ('missing','quote_not_found',False),
    ('duplicate','quote_ambiguous',False), ('unknown_source','source_not_validated',False),
])
def test_limitation_binding_and_specific_gate_source_are_durable(tmp_path, case, reason, admitted):
    current = state(content=LIMIT+'\n'+LIMIT if case=='duplicate' else None)
    change = {'source_quote':'a fabricated gap'} if case=='missing' else (
        {'source_ref':'not-in-the-catalog'} if case=='unknown_source' else {})
    scope, writer, artifacts = capture(tmp_path)
    try:
        with scope:
            evidence, _ = submit(current, notes=[note(**change)])
            gate = evaluate_admission_gate(evidence)
            assert gate.admitted is admitted
            # Failures must be durable synchronously; success uses the existing queue.
            if admitted:
                writer.flush()
            rows = events(artifacts)
            bindings = [e['payload'] for e in rows if e.get('kind')=='evidence_note_binding']
            assert len(bindings)==1
            row=bindings[0]
            assert row['reason_class']==reason
            assert row['stage']=='tool_quote_binding'
            assert row['note']['source_quote']['text']==note(**change)['source_quote']
            assert row['binding_status']==('source_bound_quote' if admitted else 'unverified')
            assert row['financial_fact_created'] is False and row['decision_qualified'] is False
            gates=[e['payload'] for e in rows if e.get('kind')=='admission_gate']
            assert gates[-1]['admitted'] is admitted
            if not admitted:
                source=next(s for s in gates[-1]['blocking_sources']
                            if s['source_id']['text']=='analyst.fundamentals.submission')
                assert source['required'] is True and source['status']=='unavailable'
                assert source['reason_class']=='limitation_quote_unbound'
            assert not evidence.material_claims and not evidence.source_facts
    finally:
        writer.close()


@pytest.mark.parametrize('field,secret', [
    ('statement','Authorization: Bearer forbidden-marker'),
    ('source_quote','Cookie: forbidden-marker'),
    ('source_ref','https://private.invalid/?token=forbidden-marker'),
    ('statement','api_key=123456789'),
])
def test_note_associated_fields_and_gate_ids_are_redacted(tmp_path, field, secret):
    values=note(**{field:secret})
    if field=='statement':
        values['source_quote']='forbidden-marker 123456789'
    scope, writer, artifacts=capture(tmp_path)
    try:
        with scope:
            evidence,_=submit(state(),notes=[values])
            evaluate_admission_gate(evidence)
    finally:
        writer.close()
    rows=events(artifacts)
    binding=next(e['payload'] for e in rows if e.get('kind')=='evidence_note_binding')
    assert binding['detail_status']=='sensitive_text_redacted'
    serialized=json.dumps(rows)
    assert 'forbidden-marker' not in serialized and '123456789' not in serialized
    assert 'private.invalid' not in serialized


@pytest.mark.parametrize('kind',['evidence_note_binding','admission_gate'])
def test_diagnostic_write_failure_is_terminal_and_never_disappears(kind):
    def broken(event):
        if event['kind']==kind:
            raise OSError('secret error body must never be stored')
    with diagnostic_scope(broken), pytest.raises(DiagnosticPersistenceError,match='diagnostic_persistence_failed'):
        evidence,_=submit(state(),notes=[note(source_quote='missing')])
        evaluate_admission_gate(evidence)


def test_unknown_source_detail_is_not_copied_to_gate_diagnostic():
    from tradingagents.evidence import EvidenceSource, EvidenceStatus
    evidence,_=submit(state(),notes=[])
    evidence=evidence.model_copy(update={'sources':(*evidence.sources,
        EvidenceSource(source_id='Authorization: Bearer forbidden-marker',required=True,
                       status=EvidenceStatus.UNAVAILABLE,detail='secret-original-body'))})
    rows=[]
    with diagnostic_scope(rows.append):
        gate=evaluate_admission_gate(evidence)
    assert not gate.admitted
    assert any(e['kind']=='admission_gate' for e in rows)
    assert 'forbidden-marker' not in json.dumps(rows) and 'secret-original-body' not in json.dumps(rows)


@pytest.mark.parametrize('earlier,code', [
    ('identity', 'identity_unavailable'), ('snapshot', 'snapshot_unavailable'),
    ('history', 'history_insufficient'),
])
def test_gate_diagnostics_do_not_attribute_earlier_failures_to_unexamined_sources(earlier, code):
    evidence, _ = submit(state(), notes=[note(source_quote='missing')])
    if earlier == 'identity':
        evidence = evidence.model_copy(update={'instrument_identity': None})
    elif earlier == 'snapshot':
        evidence = evidence.model_copy(update={'market_snapshot': None})
    else:
        evidence = evidence.model_copy(update={'market_snapshot':
            evidence.market_snapshot.model_copy(update={'history_rows': 0})})
    rows = []
    with diagnostic_scope(rows.append):
        gate = evaluate_admission_gate(evidence)
    assert code in [c.value for c in gate.diagnostic_codes]
    record = next(e['payload'] for e in rows if e['kind'] == 'admission_gate')
    assert record['blocking_sources'] == []
    assert record['blocking_source_count'] == 0


@pytest.mark.parametrize('first_status,expected_code', [
    ('not_applicable', 'decision_configuration_invalid'),
    ('conflicted', 'required_evidence_conflicted'),
])
def test_gate_only_attributes_sources_in_the_returned_failure_branch(first_status, expected_code):
    from tradingagents.evidence import EvidenceSource, EvidenceStatus

    evidence, _ = submit(state(), notes=[note(source_quote='missing')])
    evidence = evidence.model_copy(update={'sources': (*evidence.sources,
        EvidenceSource(source_id='offline-prior-source', required=True,
                       status=EvidenceStatus(first_status)))})
    rows = []
    with diagnostic_scope(rows.append):
        gate = evaluate_admission_gate(evidence)
    assert expected_code in [c.value for c in gate.diagnostic_codes]
    record = next(e['payload'] for e in rows if e['kind'] == 'admission_gate')
    assert [(s['source_id']['text'], s['status']) for s in record['blocking_sources']] == [
        ('offline-prior-source', first_status)]
