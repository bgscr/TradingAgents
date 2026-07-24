"""Checkpoint and run-isolation coverage for admitted evidence bindings."""

import pytest
from pydantic import ValidationError

from tradingagents.decision_policy import (
    AdmittedEvidenceBinding,
    EvidenceIntegrityStatus,
)
from tradingagents.evidence import EvidenceState
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph


def test_admitted_evidence_binding_current_schema_json_round_trip():
    binding = AdmittedEvidenceBinding.create(
        run_id="run:" + "1" * 64,
        evidence_semantic_digest="evidence:" + "a" * 64,
        context_id="context:" + "b" * 64,
        registry_digest="registry:" + "c" * 64,
        calculation_registry_digest="calculations:" + "d" * 64,
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )

    restored = AdmittedEvidenceBinding.model_validate_json(binding.model_dump_json())

    assert restored == binding


def test_initial_graph_state_carries_run_identity_and_nullable_binding():
    state = Propagator().create_initial_state(
        "600895.SS",
        "2026-07-19",
        run_id="run:" + "2" * 64,
    )

    assert state["run_id"] == "run:" + "2" * 64
    assert state["admitted_evidence_binding"] is None


def test_old_or_open_admission_binding_schema_is_rejected():
    payload = AdmittedEvidenceBinding.create(
        run_id="run:" + "3" * 64,
        evidence_semantic_digest="evidence:" + "a" * 64,
        context_id="context:" + "b" * 64,
        registry_digest="registry:" + "c" * 64,
        calculation_registry_digest="calculations:" + "d" * 64,
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    ).model_dump(mode="json")

    with pytest.raises(ValidationError):
        AdmittedEvidenceBinding.model_validate(
            {**payload, "contract_version": "0.9"}
        )
    with pytest.raises(ValidationError):
        AdmittedEvidenceBinding.model_validate(
            {key: value for key, value in payload.items() if key != "binding_digest"}
        )
    with pytest.raises(ValidationError):
        AdmittedEvidenceBinding.model_validate({**payload, "legacy_trust": True})


def test_binding_and_public_initial_state_reject_noncanonical_run_ids():
    binding_fields = {
        "evidence_semantic_digest": "evidence:" + "a" * 64,
        "context_id": "context:" + "b" * 64,
        "registry_digest": "registry:" + "c" * 64,
        "calculation_registry_digest": "calculations:" + "d" * 64,
        "integrity_status": EvidenceIntegrityStatus.DECISION_READY,
    }
    with pytest.raises(ValidationError):
        AdmittedEvidenceBinding.create(run_id="run:not-canonical", **binding_fields)

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {}
    graph.propagator = Propagator()
    graph.resolve_instrument_context = lambda *args, **kwargs: ""
    with pytest.raises(ValueError, match="canonical run ID"):
        graph.create_initial_state(
            "600895.SS",
            "2026-07-19",
            evidence_state=EvidenceState(),
            run_id="run:not-canonical",
        )
