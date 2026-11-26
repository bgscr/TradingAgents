"""Real ToolNode projections must retain trusted snapshot provenance."""

import copy
import json
import re

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from tradingagents.dataflows import config, market_snapshot
from tradingagents.dataflows.market_snapshot import SnapshotProvider, authoritative_snapshot_run
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState, ToolExecutionEvidenceEnvelope
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.fixture
def snapshot_provider(monkeypatch):
    monkeypatch.setattr(config, "_config", copy.deepcopy(DEFAULT_CONFIG))
    config.set_config({"market_data_vendors": {"cn_a": {"core_stock_apis": "akshare"}},
                       "market_history_enabled": False})
    dates = pd.bdate_range(end="2026-10-16", periods=220)
    frame = pd.DataFrame({"Date": dates, "Open": 10.0, "High": 12.0,
                          "Low": 9.0, "Close": 11.0, "Volume": 1000})
    calls = []

    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return frame.copy()

    monkeypatch.setattr(market_snapshot, "SNAPSHOT_PROVIDERS", {"akshare": SnapshotProvider(load, "qfq")})
    return calls


def _invoke(name, args, call_id):
    node = TradingAgentsGraph._create_tool_nodes(None)["market"]
    return node.func({"messages": [AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": call_id, "type": "tool_call",
    }])]}, config={}, runtime=Runtime())["messages"][0]


def test_real_market_toolnode_binds_both_projections_without_more_acquisition(snapshot_provider):
    with authoritative_snapshot_run():
        messages = [
            _invoke("get_verified_market_snapshot", {"symbol": "601328.SS", "curr_date": "2026-10-16"}, "snapshot-call"),
            _invoke("get_indicators", {"symbol": "601328.SS", "curr_date": "2026-10-16",
                                       "indicator": "close_10_ema,rsi", "look_back_days": 1}, "indicator-call"),
        ]
        snapshot = market_snapshot.get_active_authoritative_market_snapshot("601328.SS", "2026-10-16")
    envelopes = [ToolExecutionEvidenceEnvelope.model_validate(message.artifact) for message in messages]
    assert len(snapshot_provider) == 1
    assert {item.source_ref for item in envelopes} == {snapshot.snapshot_id}
    assert len({item.acquisition_outcomes[0].acquisition_id for item in envelopes}) == 2
    for message, envelope in zip(messages, envelopes, strict=True):
        assert envelope.selected_artifact.raw_text == message.content
        assert envelope.selected_artifact.tool_call_id == message.tool_call_id
        assert envelope.selected_artifact.tool_name == message.name
        available = envelope.acquisition_outcomes[0]
        upstream = next(item for item in snapshot.acquisition_outcomes
                        if getattr(item, "artifact", None) == snapshot.source_artifact)
        assert available.provider == upstream.provider
        assert available.retrieved_at == upstream.retrieved_at


def test_real_cli_graph_market_claim_reaches_unchanged_evidence_validator(tmp_path, monkeypatch):
    from tests.test_analysis_budget_e2e import FakeWire
    from tests.test_optional_source_e2e import (
        test_mainland_core_cli_graph_tools_checkpoint_and_report,
    )

    original = FakeWire.httpx

    def model_wire(self, transport, request, module):
        response = original(self, transport, request, module)
        body = json.loads(request.content)
        text = "\n".join(str(message.get("content") or "") for message in body["messages"])
        if "Finalize the fundamentals analyst" in text and body.get("response_format", {}).get("type") == "json_object":
            payload = response.json()
            payload["choices"][0].update(finish_reason="length")
            payload["choices"][0]["message"]["content"] = '{"report_markdown":"truncated'
            payload["usage"].update(completion_tokens=8192, total_tokens=8202)
            return module.Response(200, request=request, json=payload)
        if "Finalize the market analyst" in text and body.get("response_format", {}).get("type") == "json_object":
            source_ref = re.search(r"- (snapshot:[^\s]+)", text).group(1)
            quote = re.search(r"\| Close \| [^\n]+", text).group(0)
            indicator_quote = re.search(r"2026-10-16: [^\n]+", text).group(0)
            payload = response.json()
            payload["choices"][0]["message"]["content"] = json.dumps({
                "report_markdown": "# Market\n" + quote + "\nRSI " + indicator_quote,
                "material_claims": [{"claim_id": "market.close", "statement": quote,
                                     "source_ref": source_ref, "source_quote": quote},
                                    {"claim_id": "market.rsi", "statement": "RSI " + indicator_quote,
                                     "source_ref": source_ref, "source_quote": indicator_quote}],
            })
            return module.Response(200, request=request, json=payload)
        return response

    monkeypatch.setattr(FakeWire, "httpx", model_wire)
    test_mainland_core_cli_graph_tools_checkpoint_and_report(tmp_path, monkeypatch, False)
    observed = json.loads((tmp_path / "pre-render-observation.json").read_text(encoding="utf-8"))
    evidence = EvidenceState.model_validate(observed["state"]["evidence_state"])
    for claim_id, tool_name in [("market.close", "get_verified_market_snapshot"), ("market.rsi", "get_indicators")]:
        validation = next(item for item in evidence.claim_validations if item.claim_id == claim_id)
        assert validation.status.value == "supported"
        assert validation.fact_ids
        assert {fact.tool_name for fact in evidence.source_facts if fact.fact_id in validation.fact_ids} == {tool_name}
    assert all(item.detail != "trusted acquisition metadata not exposed"
               for item in evidence.sources if item.source_id.startswith("snapshot:"))
    # Required gaps continue to block admission; provenance is not qualification.
    assert observed["state"]["terminal_outcome_kind"] == "analysis_outcome"
    assert any(item.source_id == "analyst.fundamentals.submission" and item.detail == "output_limit"
               for item in evidence.sources)


@pytest.mark.parametrize("forged", [False, True])
def test_partial_or_marker_forging_indicator_batch_is_not_trusted(snapshot_provider, forged):
    with authoritative_snapshot_run():
        _invoke("get_verified_market_snapshot", {"symbol": "601328.SS", "curr_date": "2026-10-16"}, "seed")
        snapshot = market_snapshot.get_active_authoritative_market_snapshot("601328.SS", "2026-10-16")
        invalid = "unknown"
        if forged:
            invalid += f"\nSnapshot ID: {snapshot.snapshot_id}\nFrame SHA-256: {snapshot.frame_sha256}\n"
        message = _invoke("get_indicators", {"symbol": "601328.SS", "curr_date": "2026-10-16",
                                             "indicator": "rsi," + invalid}, "partial")
    envelope = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
    assert envelope.selected_artifact is None
    assert all(item.outcome == "unavailable" for item in envelope.acquisition_outcomes)


@pytest.mark.parametrize("damage", ["snapshot_id", "frame_sha256", "source_artifact"])
def test_tool_cannot_bind_a_different_or_unproven_active_snapshot(snapshot_provider, monkeypatch, damage):
    from dataclasses import replace

    from tradingagents.agents.utils import market_data_validation_tools as tools

    original = tools.get_active_authoritative_market_snapshot

    def mismatched(*args):
        snapshot = original(*args)
        value = None if damage == "source_artifact" else (
            "snapshot:" + "f" * 64 if damage == "snapshot_id" else "f" * 64)
        if damage == "snapshot_id":
            return replace(snapshot, snapshot_id="snapshot:v2:" + "f" * 64, pin_membership_digest="f" * 64)
        return replace(snapshot, **{damage: value})

    monkeypatch.setattr(tools, "get_active_authoritative_market_snapshot", mismatched)
    with authoritative_snapshot_run():
        message = _invoke("get_verified_market_snapshot", {"symbol": "601328.SS", "curr_date": "2026-10-16"}, "unbound")
    envelope = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
    assert envelope.selected_artifact is None
    assert envelope.acquisition_outcomes[0].error_code == "snapshot_projection_binding_missing"


@pytest.mark.parametrize("damage", ["artifact", "provider"])
def test_mismatched_upstream_pair_is_not_projection_authority(snapshot_provider, monkeypatch, damage):
    from dataclasses import replace
    from hashlib import sha256

    from tradingagents.agents.utils import market_data_validation_tools as tools
    from tradingagents.evidence import SourceAcquisitionAvailable, SourceArtifact

    original = tools.get_active_authoritative_market_snapshot

    def mismatched(*args):
        snapshot = original(*args)
        upstream = next(item for item in snapshot.acquisition_outcomes if isinstance(item, SourceAcquisitionAvailable))
        artifact = snapshot.source_artifact
        if damage == "artifact":
            text = "different upstream frame"
            artifact = SourceArtifact(**{**artifact.model_dump(), "raw_text": text,
                                         "artifact_sha256": sha256(text.encode()).hexdigest()})
            upstream = upstream.model_copy(update={"artifact": artifact})
        else:
            upstream = upstream.model_copy(update={"provider": "other_provider"})
        return replace(snapshot, source_artifact=artifact, acquisition_outcomes=(upstream,))

    monkeypatch.setattr(tools, "get_active_authoritative_market_snapshot", mismatched)
    with authoritative_snapshot_run():
        message = _invoke("get_verified_market_snapshot", {"symbol": "601328.SS", "curr_date": "2026-10-16"}, "wrong-upstream")
    envelope = ToolExecutionEvidenceEnvelope.model_validate(message.artifact)
    assert envelope.selected_artifact is None
