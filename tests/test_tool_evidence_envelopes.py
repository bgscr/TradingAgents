"""Public contract tests for checkpoint-safe tool evidence envelopes."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    ClaimValidationStatus,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
)


def _available_envelope(
    *, tool_call_id: str, content: str, tool_name: str = "evidence_probe"
) -> dict[str, object]:
    source_ref = "get_company_news:600895.SS:2026-07-19"
    artifact = SourceArtifact(
        artifact_sha256=sha256(content.encode("utf-8")).hexdigest(),
        source_ref=source_ref,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        raw_text=content,
    )
    return ToolExecutionEvidenceEnvelope(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        source_ref=source_ref,
        capability="get_company_news",
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="fixture-provider",
                capability="get_company_news",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-19T12:00:00+00:00",
                artifact=artifact,
            ),
        ),
        selected_artifact=artifact,
    ).model_dump(mode="json")


@pytest.mark.unit
def test_real_tool_node_preserves_json_envelope_and_correlates_reversed_calls_by_id():
    content_by_id = {"call-a": "Alpha fact: 10", "call-b": "Beta fact: 20"}

    @tool(response_format="content_and_artifact")
    def evidence_probe(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId()],
    ) -> tuple[str, dict[str, object]]:
        """Return deterministic evidence for a test query."""
        content = content_by_id[tool_call_id]
        return content, _available_envelope(tool_call_id=tool_call_id, content=content)

    calls = [
        {"name": "evidence_probe", "args": {"query": "beta"}, "id": "call-b", "type": "tool_call"},
        {"name": "evidence_probe", "args": {"query": "alpha"}, "id": "call-a", "type": "tool_call"},
    ]
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([evidence_probe]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    result = graph.invoke(
        {"messages": [AIMessage(content="", tool_calls=calls)]}
    )

    messages = result["messages"][1:]
    assert all(isinstance(message, ToolMessage) for message in messages)
    by_id = {message.tool_call_id: message for message in messages}
    assert tuple(by_id) == ("call-b", "call-a")
    for call_id, expected_content in content_by_id.items():
        message = by_id[call_id]
        assert message.content == expected_content
        checkpoint_message = ToolMessage.model_validate_json(message.model_dump_json())
        assert checkpoint_message.artifact == message.artifact
        envelope = ToolExecutionEvidenceEnvelope.model_validate(
            json.loads(json.dumps(checkpoint_message.artifact))
        )
        assert envelope.tool_call_id == call_id == message.tool_call_id
        assert envelope.selected_artifact is not None
        assert envelope.selected_artifact.raw_text == expected_content


@pytest.mark.unit
def test_envelope_rejects_unknown_fields_and_cross_bound_artifacts():
    payload = _available_envelope(tool_call_id="call-a", content="Alpha fact: 10")
    payload["unexpected"] = "not allowed"
    with pytest.raises(ValidationError):
        ToolExecutionEvidenceEnvelope.model_validate(payload)


@pytest.mark.unit
def test_ingestion_trusts_only_envelope_order_and_selected_artifact():
    content = "PE Ratio (TTM): 61.77551"
    payload = _available_envelope(
        tool_call_id="call-a", content=content, tool_name="get_company_news"
    )
    payload["acquisition_outcomes"].insert(
        0,
        SourceAcquisitionUnavailable(
            provider="primary",
            capability="get_company_news",
            source_ref="get_company_news:600895.SS:2026-07-19",
            attempt=1,
            retrieved_at="2026-07-19T11:59:00+00:00",
            retryable=True,
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            retry_after_seconds=2,
        ).model_dump(mode="json"),
    )
    payload["acquisition_outcomes"][1]["provider"] = "secondary"
    message = ToolMessage(
        content=content,
        tool_call_id="call-a",
        name="get_company_news",
        artifact=payload,
    )
    claim = MaterialClaim(
        claim_id="fundamentals.pe",
        analyst="fundamentals",
        statement="The PE ratio is 61.77551.",
        source_refs=("get_company_news:600895.SS:2026-07-19",),
        source_quote=content,
    )

    evidence = build_tool_evidence_state(
        (message,),
        (claim,),
        tool_call_ids_by_source={
            "get_company_news:600895.SS:2026-07-19": ("call-a",)
        },
    )

    assert [outcome.provider for outcome in evidence.acquisition_outcomes] == [
        "primary",
        "secondary",
    ]
    assert evidence.source_artifacts[0].raw_text == content
    assert evidence.claim_validations[0].status is ClaimValidationStatus.SUPPORTED


@pytest.mark.unit
def test_legacy_success_message_without_envelope_exposes_no_trusted_evidence():
    content = "PE Ratio (TTM): 61.77551"
    source_ref = "get_company_news:600895.SS:2026-07-19"
    message = ToolMessage(
        content=content,
        tool_call_id="legacy-call",
        name="get_company_news",
    )
    claim = MaterialClaim(
        claim_id="legacy.pe",
        analyst="fundamentals",
        statement="The PE ratio is 61.77551.",
        source_refs=(source_ref,),
        source_quote=content,
    )

    evidence = build_tool_evidence_state(
        (message,),
        (claim,),
        tool_call_ids_by_source={source_ref: ("legacy-call",)},
    )

    assert evidence.sources[0].status.value == "unavailable"
    assert evidence.sources[0].detail == "trusted acquisition metadata not exposed"
    assert evidence.acquisition_outcomes == ()
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert evidence.claim_validations[0].status is ClaimValidationStatus.UNSUPPORTED


@pytest.mark.unit
def test_conflicting_envelopes_for_one_tool_call_fail_closed():
    source_ref = "get_company_news:600895.SS:2026-07-19"
    messages = tuple(
        ToolMessage(
            content=content,
            tool_call_id="call-a",
            name="get_company_news",
            artifact=_available_envelope(
                tool_call_id="call-a",
                content=content,
                tool_name="get_company_news",
            ),
        )
        for content in ("Alpha fact: 10", "Conflicting fact: 99")
    )
    claim = MaterialClaim(
        claim_id="news.alpha",
        analyst="news",
        statement="Alpha fact: 10",
        source_refs=(source_ref,),
        source_quote="Alpha fact: 10",
    )

    evidence = build_tool_evidence_state(
        messages,
        (claim,),
        tool_call_ids_by_source={source_ref: ("call-a",)},
    )

    assert evidence.sources[0].status.value == "unavailable"
    assert evidence.acquisition_outcomes == ()
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()


@pytest.mark.unit
def test_unavailable_only_envelope_retains_diagnostic_but_exposes_no_evidence():
    source_ref = "get_company_news:600895.SS:2026-07-19"
    outcome = SourceAcquisitionUnavailable(
        provider="primary",
        capability="get_company_news",
        source_ref=source_ref,
        attempt=1,
        retrieved_at="2026-07-19T12:00:00+00:00",
        retryable=True,
        reason=AcquisitionUnavailableReason.RATE_LIMITED,
        retry_after_seconds=3,
    )
    envelope = ToolExecutionEvidenceEnvelope(
        tool_call_id="call-a",
        tool_name="get_company_news",
        source_ref=source_ref,
        capability="get_company_news",
        acquisition_outcomes=(outcome,),
    ).model_dump(mode="json")
    message = ToolMessage(
        content="DATA_UNAVAILABLE",
        tool_call_id="call-a",
        name="get_company_news",
        artifact=envelope,
    )
    claim = MaterialClaim(
        claim_id="news.alpha",
        analyst="news",
        statement="Alpha fact: 10",
        source_refs=(source_ref,),
        source_quote="Alpha fact: 10",
    )

    evidence = build_tool_evidence_state(
        (message,),
        (claim,),
        tool_call_ids_by_source={source_ref: ("call-a",)},
    )

    assert evidence.sources[0].status.value == "unavailable"
    assert evidence.acquisition_outcomes == (outcome,)
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()


@pytest.mark.unit
def test_runtime_tool_call_id_changes_only_operational_evidence_metadata():
    source_ref = "get_company_news:600895.SS:2026-07-19"
    content = "Alpha fact: 10"
    claim = MaterialClaim(
        claim_id="news.alpha",
        analyst="news",
        statement=content,
        source_refs=(source_ref,),
        source_quote=content,
    )

    def ingest(call_id: str):
        message = ToolMessage(
            content=content,
            tool_call_id=call_id,
            name="get_company_news",
            artifact=_available_envelope(
                tool_call_id=call_id,
                content=content,
                tool_name="get_company_news",
            ),
        )
        return build_tool_evidence_state(
            (message,),
            (claim,),
            tool_call_ids_by_source={source_ref: (call_id,)},
        )

    first = ingest("runtime-a")
    second = ingest("runtime-b")

    assert first.material_claims[0].fact_ids == second.material_claims[0].fact_ids
    assert first.source_facts[0].fact_id == second.source_facts[0].fact_id
    assert first.source_facts[0].tool_call_id != second.source_facts[0].tool_call_id


@pytest.mark.unit
def test_multi_message_ingestion_is_canonical_by_tool_call_id_not_input_order():
    source_ref = "get_company_news:600895.SS:2026-07-19"

    def message(call_id: str, provider: str) -> ToolMessage:
        content = f"{provider} fact"
        envelope = _available_envelope(
            tool_call_id=call_id,
            content=content,
            tool_name="get_company_news",
        )
        envelope["acquisition_outcomes"][0]["provider"] = provider
        return ToolMessage(
            content=content,
            tool_call_id=call_id,
            name="get_company_news",
            artifact=envelope,
        )

    evidence = build_tool_evidence_state(
        (message("call-b", "provider-b"), message("call-a", "provider-a")),
        (),
        tool_call_ids_by_source={source_ref: ("call-b", "call-a")},
    )

    # With no claims there is intentionally no requested source to ingest.
    assert evidence.acquisition_outcomes == ()

    claim = MaterialClaim(
        claim_id="news.alpha",
        analyst="news",
        statement="provider-a fact",
        source_refs=(source_ref,),
        source_quote="provider-a fact",
    )
    evidence = build_tool_evidence_state(
        (message("call-b", "provider-b"), message("call-a", "provider-a")),
        (claim,),
        tool_call_ids_by_source={source_ref: ("call-b", "call-a")},
    )
    assert [item.provider for item in evidence.acquisition_outcomes] == [
        "provider-a",
        "provider-b",
    ]

    payload = _available_envelope(tool_call_id="call-a", content="Alpha fact: 10")
    payload["selected_artifact"]["tool_call_id"] = "forged"
    with pytest.raises(ValidationError):
        ToolExecutionEvidenceEnvelope.model_validate(payload)
