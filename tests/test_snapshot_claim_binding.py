"""Synthetic snapshot and claim binding controls."""
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.synthetic_projection_data import snapshot_claim_case
from tradingagents.agents.analysts.submission import AnalystSubmissionResult, build_analyst_update
from tradingagents.evidence import (
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
)

FIXTURE = snapshot_claim_case()
TEXT = FIXTURE['snapshot_text']
REF = FIXTURE['claims'][0]['source_ref']


def messages_for(content=TEXT, *, name='get_verified_market_snapshot', call_updates=None,
                 extra=None, envelope=True):
    messages, calls = [], {}
    projections = [(name, content)] + (extra or [])
    for i, (tool, text) in enumerate(projections):
        call = {'name': tool, 'id': f'offline-{i}', 'type': 'tool_call',
                'args': {'symbol': '601328.SS', 'curr_date': '2026-10-16'}}
        if i == 0 and call_updates:
            call = {**call, **call_updates}
        artifact = SourceArtifact(artifact_sha256=sha256(text.encode()).hexdigest(),
            source_ref=REF, tool_call_id=f'offline-{i}', tool_name=tool, raw_text=text)
        capability = 'market_snapshot' if tool == 'get_verified_market_snapshot' else tool
        outcome = SourceAcquisitionAvailable(provider='offline-synthetic-projection', capability=capability,
            source_ref=REF, attempt=1, acquisition_id=f'offline-projection-{i}',
            retrieved_at='2026-11-01T02:08:15Z', artifact=artifact)
        bound = ToolExecutionEvidenceEnvelope(tool_call_id=f'offline-{i}', tool_name=tool,
            source_ref=REF, capability=capability, acquisition_outcomes=(outcome,), selected_artifact=artifact)
        messages.extend([AIMessage(content='', tool_calls=[call]),
            ToolMessage(name=tool, tool_call_id=f'offline-{i}', content=text,
                        artifact=bound.model_dump(mode='json') if envelope else None)])
        calls[f'offline-{i}'] = call
    return messages, calls


def evaluate(statement, quote, **kwargs):
    messages, calls = messages_for(**kwargs)
    claim = MaterialClaim(claim_id='market.offline', analyst='market', statement=statement,
                          source_refs=(REF,), source_quote=quote)
    return build_tool_evidence_state(messages, (claim,), tool_call_ids_by_source={REF: tuple(calls)},
                                    tool_calls_by_id=calls)


@pytest.mark.parametrize('claim_id', ['market.trend_ema10', 'market.trend_sma50',
                                     'market.trend_sma200', 'market.volume_latest'])
def test_synthetic_snapshot_metadata_is_bound_without_changing_the_claim(claim_id):
    c = next(c for c in FIXTURE['claims'] if c['claim_id'] == claim_id)
    evidence = evaluate(c['statement'], c['source_quote'])
    assert evidence.claim_validations[0].status.value == 'supported'
    assert evidence.material_claims[0].statement == c['statement']
    assert all(f.fact_kind == 'excerpt' and f.normalized_value is None
               and f.canonical_field == '' and f.calculation_lineage is None for f in evidence.source_facts)


def test_all_synthetic_claims_through_production_submission_keep_rsi_threshold_rejected():
    messages, _ = messages_for()
    claims = tuple(MaterialClaim(claim_id=c['claim_id'], analyst='market', statement=c['statement'],
                   source_quote=c['source_quote'], source_refs=(REF,)) for c in FIXTURE['claims'])
    result = AnalystSubmissionResult(message=AIMessage(content='Offline synthetic claims'), report='Offline',
        claims=claims, submission_source=EvidenceSource(source_id='analyst.market.submission',
        status=EvidenceStatus.AVAILABLE, required=True))
    update = build_analyst_update({'messages': messages, 'evidence_state': {},
        'company_of_interest': '601328.SS', 'trade_date': '2026-10-16'}, result, 'market_report')
    evidence = EvidenceState.model_validate(update['evidence_state'])
    failed = {v.claim_id: v for v in evidence.claim_validations if v.status.value != 'supported'}
    assert set(failed) == {'market.rsi_overbought'}
    assert failed['market.rsi_overbought'].detail.endswith('70')
    periods = {c.claim_id: c.minimum_history_rows for c in evidence.material_claims}
    assert [periods[k] for k in ('market.trend_ema10','market.trend_sma50','market.trend_sma200')] == [10,50,200]


@pytest.mark.parametrize('statement', [
    'Price is above the 10-day SMA of 7.36.', 'Price is above the 50-day EMA of 7.36.',
    'Price is above the 200-day SMA of 7.36.', 'Price is above the 50-day SMA of 50.',
    'Price is above the 50-day SMA of 7.37.', 'The 50-day SMA return is 50%.',
    'The 50-day SMA is 7.36 USD.', 'The 50-day SMA is 7.36 million.',
    'The 50-day SMA is 7.36 percent.', 'The 50-day SMA and 200-day SMA are 7.36.',
    'The RSI and 50-day SMA are 7.36.', 'The price is 50.',
    'The rsi and 50-day SMA are 7.36.',
])
def test_wrong_period_kind_value_units_or_multiple_indicators_stay_unsupported(statement):
    assert evaluate(statement, '| close_50_sma | 7.36 |').claim_validations[0].status.value == 'unsupported'


@pytest.mark.parametrize('quote', ['7.36', 'close_50_sma', '| close_50_sma | 7.36 |\n| close_200_sma | 6.85 |'])
def test_partial_or_multirow_quote_cannot_bind_indicator_identity(quote):
    assert evaluate('Price is above the 50-day SMA of 7.36.', quote).claim_validations[0].status.value == 'unsupported'


@pytest.mark.parametrize('damage', ['missing_date','duplicate_date','wrong_requested','future_row',
    'wrong_symbol','duplicate_section','duplicate_row','wrong_header','wrong_section','missing_snapshot_id'])
def test_ambiguous_or_mismatched_snapshot_layout_is_not_metadata_authority(damage):
    mutations = {
        'missing_date': ('- Latest trading row used: 2026-10-16', ''),
        'duplicate_date': ('- Latest trading row used: 2026-10-16', '- Latest trading row used: 2026-10-16\n- Latest trading row used: 2026-10-15'),
        'wrong_requested': ('- Requested analysis date: 2026-10-16', '- Requested analysis date: 2026-10-15'),
        'future_row': ('- Latest trading row used: 2026-10-16', '- Latest trading row used: 2026-10-17'),
        'wrong_symbol': ('snapshot for 601328.SS','snapshot for 600000.SS'),
        'duplicate_section': ('### Verified technical indicators (latest row)', '### Verified technical indicators (latest row)\n### Verified technical indicators (latest row)'),
        'duplicate_row': ('| close_50_sma | 7.36 |', '| close_50_sma | 7.36 |\n| close_50_sma | 9.99 |'),
        'wrong_header': ('| Indicator | Value |', '| Other | Value |'),
        'wrong_section': ('### Verified technical indicators (latest row)', '### Unrelated observations'),
        'missing_snapshot_id': ('Snapshot ID: '+REF, ''),
    }
    old,new=mutations[damage]
    assert old in TEXT
    assert evaluate('The 50-day SMA is 7.36.', '| close_50_sma | 7.36 |',
                    content=TEXT.replace(old,new)).claim_validations[0].status.value == 'unsupported'


@pytest.mark.parametrize('kwargs', [
    {'envelope':False}, {'name':'get_indicators'},
    {'call_updates':{'args':{'symbol':'600000.SS','curr_date':'2026-10-16'}}},
    {'call_updates':{'args':{'symbol':'601328.SS','curr_date':'2026-10-15'}}},
    {'extra':[('get_verified_market_snapshot', TEXT)]},
    {'extra':[('get_verified_market_snapshot', TEXT.replace('7.36','9.99'))]},
])
def test_call_and_unique_snapshot_projection_are_required(kwargs):
    assert evaluate('The 50-day SMA is 7.36.', '| close_50_sma | 7.36 |', **kwargs).claim_validations[0].status.value == 'unsupported'


@pytest.mark.parametrize('statement', [
    'Volume was 289,163,320 shares on 2026-10-15.',
    'Volume was 289,163,320 shares on 2026-16-10.',
    'Volume was 16 shares on 2026-10-16.',
    'Volume was 2026 shares on 2026-10-16.',
    'Volume was 289,163,320 shares on 2026-10-16 and 2026-10-15.',
    'Volume was 2026-10-16.',
    'Volume on 2026-10-16.',
    'Volume was 2026-10-16 and 289163320.',
    'Volume was 289163320; the amount was 2026-10-16.',
])
def test_date_binding_never_supplies_a_wrong_date_or_a_numeric_observation(statement):
    assert evaluate(statement,'| Volume | 289163320.00 |').claim_validations[0].status.value == 'unsupported'


@pytest.mark.parametrize('statement', [
    'Volume was 289163320 on 2026-10-16.',
    'As of 2026-10-16, volume was 289163320.',
    'On 2026-10-16, volume was 289163320.',
])
def test_explicit_observation_date_keeps_a_separate_correct_value(statement):
    assert evaluate(statement, '| Volume | 289163320.00 |').claim_validations[0].status.value == 'supported'


def test_dated_indicator_name_does_not_supply_a_missing_value_equal_to_its_period():
    content = TEXT.replace('| close_50_sma | 7.36 |', '| close_50_sma | 50.00 |')
    assert evaluate('The 50-day SMA on 2026-10-16.', '| close_50_sma | 50.00 |',
                    content=content).claim_validations[0].status.value == 'unsupported'
    assert evaluate('The 50-day SMA is 50.00 on 2026-10-16.', '| close_50_sma | 50.00 |',
                    content=content).claim_validations[0].status.value == 'supported'


@pytest.mark.parametrize('value', ['2026', '10', '16'])
def test_new_english_snapshot_support_does_not_borrow_get_indicators_date(value):
    from tests.test_indicator_claim_metadata import evaluate as indicator_evaluate

    evidence = indicator_evaluate(f'The 50-day SMA is {value}.', '2026-10-16: 7.36',
        content='## close_50_sma values from 2026-10-01 to 2026-10-16:\n\n2026-10-16: 7.36\n')
    assert evidence.claim_validations[0].status.value == 'unsupported'


def test_rsi_observation_is_separate_from_an_unregistered_interpretation():
    assert evaluate('RSI is 74.58.','| rsi | 74.58 |').claim_validations[0].status.value == 'supported'
    assert evaluate('RSI is above 70.','| rsi | 74.58 |').claim_validations[0].status.value == 'unsupported'


def test_actual_companion_projection_does_not_make_one_snapshot_row_ambiguous():
    text = FIXTURE['indicators_text']
    evidence = evaluate('The 50-day SMA is 7.36.', '| close_50_sma | 7.36 |',
                        extra=[('get_indicators',text)])
    assert evidence.claim_validations[0].status.value == 'supported'


@pytest.mark.parametrize('content', [
    TEXT.replace('- Latest trading row used: 2026-10-16',
                 '- Latest trading row used: 2026-10-16\n- Latest trading row used: unknown'),
    TEXT.replace('- Latest trading row used: 2026-10-16', '')+'\n- Latest trading row used: 2026-10-16',
])
def test_date_context_cannot_be_malformed_duplicated_or_hoisted_from_a_footer(content):
    assert evaluate('Volume was 289,163,320 on 2026-10-16.', '| Volume | 289163320.00 |',
                    content=content).claim_validations[0].status.value == 'unsupported'


def test_production_market_prompt_separates_observed_rsi_from_rule_commentary():
    from langchain_core.runnables import RunnableLambda

    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    prompts=[]
    def respond(prompt):
        prompts.append(prompt.to_messages())
        return AIMessage(content='', tool_calls=[{'name': 'get_stock_data', 'id': 'offline',
            'type': 'tool_call', 'args': {'symbol': '601328.SS', 'start_date': '2026-01-01', 'end_date': '2026-10-16'}}])
    class Client:
        def with_structured_output(self,*args,**kwargs):
            return RunnableLambda(respond)
        def bind_tools(self,*args,**kwargs):
            return RunnableLambda(respond)
    result=create_market_analyst(Client())({'messages': [], 'company_of_interest': '601328.SS', 'trade_date': '2026-10-16'})
    assert result['market_report']=='' and result['messages'][0].tool_calls
    text='\n'.join(str(m.content) for m in prompts[0])
    assert 'not numbers measured by the source' in text
    assert 'do not combine an unregistered threshold' in text

