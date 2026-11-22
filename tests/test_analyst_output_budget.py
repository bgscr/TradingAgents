"""Synthetic external model responses through the real analyst completion boundary."""
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from tradingagents.agents.analysts.submission import process_analyst_response
from tradingagents.agents.utils.structured import invoke_required_structured


@pytest.mark.parametrize('kind', ['report', 'data', 'prose'])
def test_initial_truncated_analyst_message_never_submits_or_spends_finalizer(kind):
    calls = []
    if kind == 'report':
        calls = [{'name': 'AnalystEvidenceReport', 'id': 'partial', 'type': 'tool_call',
                  'args': {'report_markdown': 'Apparently valid partial report.', 'material_claims': []}}]
    elif kind == 'data':
        calls = [{'name': 'get_income_statement', 'id': 'partial', 'type': 'tool_call',
                  'args': {'ticker': '601328.SS', 'curr_date': '2026-10-16'}}]
    response = AIMessage(content='PARTIAL_PRIVATE_OUTPUT', tool_calls=calls,
                         response_metadata={'finish_reason': 'length'})
    finalizer = MagicMock()
    result = process_analyst_response(response, finalizer=finalizer, analyst='fundamentals',
                                     ticker='601328.SS', trade_date='2026-10-16', messages=[])
    assert result.submission_source is not None
    assert result.submission_source.status.value == 'unavailable'
    assert result.submission_source.detail == 'output_limit'
    assert result.claims == () and result.message.tool_calls == []
    assert 'PARTIAL_PRIVATE_OUTPUT' not in result.report
    finalizer.invoke.assert_not_called()


def test_sdk_length_diagnostic_reports_available_reasoning_count_without_text(caplog):
    completion = ChatCompletion(id='synthetic', object='chat.completion', created=0,
        model='deepseek-flash', choices=[{'index': 0, 'finish_reason': 'length', 'message': {
            'role': 'assistant', 'content': 'PRIVATE_OUTPUT', 'reasoning_content': 'PRIVATE_REASONING'}}],
        usage={'prompt_tokens': 100, 'completion_tokens': 8192, 'total_tokens': 8292,
               'completion_tokens_details': {'reasoning_tokens': 7600}})
    finalizer = MagicMock()
    finalizer.invoke.side_effect = LengthFinishReasonError(completion=completion)
    result = invoke_required_structured(finalizer, 'synthetic', 'Fundamentals Analyst')
    assert result.reason == 'output_limit' and result.attempts == 1 and result.value is None
    finalizer.invoke.assert_called_once()
    assert 'completion_tokens=8192' in caplog.text
    assert 'reasoning_tokens=7600' in caplog.text
    assert 'PRIVATE_OUTPUT' not in caplog.text and 'PRIVATE_REASONING' not in caplog.text


def test_fundamentals_and_finalizer_request_compact_evidence_not_shorter_data_window():
    from langchain_core.runnables import RunnableLambda

    from tests.test_structured_agents import _assert_unbound_financial_commentary, _tool_exchange
    from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
    from tradingagents.evidence import AnalystEvidenceReport, EvidenceState, SubmittedMaterialClaim

    prompts = []
    structured = MagicMock()
    structured.invoke.return_value = AnalystEvidenceReport(
        report_markdown='Verified source. Disclosure gap retained.',
        material_claims=(SubmittedMaterialClaim(claim_id='fundamentals.cash',
            statement='Cash was 55.', source_quote='Cash was 55.',
            source_ref='get_income_statement:NVDA:2026-01-15'),))
    llm = MagicMock()

    def respond(prompt):
        prompts.append(prompt.to_string())
        return AIMessage(content='Unvalidated draft; disclosure gap retained.')

    llm.bind_tools.return_value = RunnableLambda(respond)
    llm.with_structured_output.return_value = structured
    result = create_fundamentals_analyst(llm)({
        'company_of_interest': 'NVDA', 'trade_date': '2026-01-15', 'asset_type': 'stock',
        'messages': _tool_exchange('get_income_statement', {'ticker': 'NVDA', 'curr_date': '2026-01-15'},
                                  'Cash was 55. Disclosure gap retained.', 'source-call'),
        'evidence_state': EvidenceState().model_dump(mode='json'),
    })
    assert 'full requested financial window' in prompts[0]
    assert 'past week' not in prompts[0] and 'as much detail as possible' not in prompts[0]
    finalization = structured.invoke.call_args.args[0]
    for prompt in [prompts[0], finalization]:
        assert 'configured output budget' in prompt
        assert 'every material data gap' in prompt
        assert 'complete historical coverage' in prompt
    assert 'Cash was 55. Disclosure gap retained.' in finalization
    _assert_unbound_financial_commentary(
        result['fundamentals_report'], 'Verified source. Disclosure gap retained.')
    assert result['messages'][0].content == 'Verified source. Disclosure gap retained.'
    evidence = EvidenceState.model_validate(result['evidence_state'])
    assert evidence.material_claims[0].source_quote == 'Cash was 55.'
    structured.invoke.assert_called_once()
