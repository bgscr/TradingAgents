"""Unsigned proposal construction only; no native execution or real claim."""

from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest

from tradingagents.policy_qualified_contract import (
    BytePin,
    Candidate,
    EvidenceRef,
    ReviewedPolicyPackage,
    content_identity,
)
from tradingagents.windows_sandbox_acceptance import build_ticket06_command_ledger


@pytest.fixture
def materializer():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "scripts/policy_owner/materialize_policy_package.py"
        )
    )


def inputs():
    pin = BytePin.of(b"EXPLICIT SYNTHETIC MATERIALIZER METADATA; NO ACQUISITION")
    phases = {}
    evidence = []
    for name in (
        "historical-before.json",
        "historical-after.json",
        "capture_historical.py",
        "procedure-discovery.json",
        "qualification-blocked-condition-v1.json",
        "historical-delta-detail-v1.json",
    ):
        identity = content_identity("test-materializer-reference:v1", {"name": name})
        evidence.append(
            {
                "path": "D:\\TEST-METADATA-ONLY\\" + name,
                "declared_identity": identity,
                **pin.model_dump(mode="json"),
            }
        )
        if name.startswith("historical-") and name.endswith(("before.json", "after.json")):
            phases[name] = identity
    retained = {
        "evidence": evidence,
        "collection_payloads": [
            {
                "phase": phase,
                "identity": phases["historical-" + phase + ".json"],
                "elapsed_seconds": duration,
            }
            for phase, duration in (("before", 1.0), ("after", 2.0))
        ],
        "candidate": {"commit": "1" * 40, "tree": "2" * 40, "parent": "3" * 40},
        "historical_root": {
            "root": "D:\\TEST-METADATA-ONLY\\build",
            "before_identity": phases["historical-before.json"],
            "after_identity": phases["historical-after.json"],
        },
        "saved_verification": {"changed_rows": 2571},
    }
    ledger = build_ticket06_command_ledger(
        "windows-sandbox-environment-policy:v1:sha256:8428ed3a9e8435cace5541ded703613d97ac25df2e08a163b94fb6d90e18a81f"
    ).model_dump(mode="json")
    procedure = {
        "iso04_host_timeout_seconds": 14400,
        "ticket06_command_ledger_identity": ledger["ledger_identity"],
    }
    return pin, retained, ledger, procedure


def test_budget_proposal_keeps_command_limits_separate_from_new_grant(materializer):
    _, _, ledger, procedure = inputs()
    value = materializer["budget_proposal"](ledger, procedure)
    assert value["sum_individual_timeout_seconds"] == 69600
    assert value["total_remaining_budget_seconds"] == 75600
    assert value["historical_outer_timeout_seconds_reference_only"] == 14400
    assert value["execution_authorized"] is False and value["effective_utc"] is None
    assert value["status"] == "PROPOSED_REQUIRES_FRESH_EXACT_HUMAN_GRANT"
    altered = copy.deepcopy(ledger)
    altered["entries"][0]["argv"] = ["unapproved-command"]
    with pytest.raises(ValueError):
        materializer["budget_proposal"](altered, procedure)
    with pytest.raises(ValueError):
        materializer["budget_proposal"](ledger, {**procedure, "iso04_host_timeout_seconds": 14401})


def test_exact_unsigned_policy_rebinds_successor_without_rewriting_history(materializer):
    pin, retained, ledger, procedure = inputs()
    registry = materializer["original_registry"](retained, b"TEST predecessor manifest")
    budgets = materializer["budget_proposal"](ledger, procedure)
    candidate = Candidate(commit="a" * 40, tree="b" * 40, parent="c" * 40)
    arguments = dict.fromkeys(
        (
            "predecessor_pin",
            "scope_pin",
            "authority_pin",
            "procedure_pin",
            "coordinator_pin",
            "consumer_pin",
            "measurement_pin",
        ),
        pin,
    )
    scope, plan, policy = materializer["policy_objects"](
        candidate, retained, registry, budgets=budgets, **arguments
    )
    assert scope.original_candidate.commit == "1" * 40 and scope.candidate == candidate
    assert (
        scope.raw_before,
        scope.raw_after,
        scope.historical_equal,
        scope.historical_cause,
        scope.prior_outcome,
    ) == (0x10000010, 0x10, False, "UNRESOLVED", "BLOCKED")
    assert scope.before.collection_utc is None and scope.after.transition_utc is None
    assert plan.claim_window_days == 7 and plan.terminal_window_days == 30
    assert policy.failure_consumes_attempt and policy.new_discrepancy_is_terminal
    package = ReviewedPolicyPackage.seal(
        contract_version="reviewed-policy-contract-package-v1",
        policy=EvidenceRef(
            identity=policy.acceptance_policy_identity, pin=BytePin.of(policy.to_bytes())
        ),
        scope_evidence_manifest_pin=pin,
        contract_source_pin=pin,
    )
    assert ReviewedPolicyPackage.read(package.to_bytes()) == package
    _, new_plan, new_policy = materializer["policy_objects"](
        Candidate(commit="d" * 40, tree="e" * 40, parent="c" * 40),
        retained,
        registry,
        budgets=budgets,
        **arguments,
    )
    assert new_plan.lineage_plan_identity != plan.lineage_plan_identity
    assert new_policy.acceptance_policy_identity != policy.acceptance_policy_identity
    assert new_policy.scope.before == scope.before
    assert "effective_utc" not in policy.model_dump(mode="json")


def test_registry_rejects_ambiguous_or_mismatched_original_provenance(materializer):
    _, retained, _, _ = inputs()
    duplicated = copy.deepcopy(retained)
    duplicated["evidence"].append(duplicated["evidence"][0])
    with pytest.raises(ValueError, match="absent or ambiguous"):
        materializer["original_registry"](duplicated, b"TEST")
    changed = copy.deepcopy(retained)
    changed["collection_payloads"][0]["identity"] = changed["collection_payloads"][1]["identity"]
    with pytest.raises(ValueError, match="phase/duration"):
        materializer["original_registry"](changed, b"TEST")


def test_source_package_preserves_checkout_bytes_and_rejects_actual_source_drift(materializer):
    bind = materializer["source_checkout_binding"]
    canonical = b"line one\nline two\n"
    physical = b"line one\r\nline two\r\n"
    assert bind(canonical, canonical)["checkout_form"] == "IDENTICAL"
    result = bind(physical, canonical)
    assert result["checkout_form"] == "UTF8_CRLF_CHECKOUT"
    assert result["git_blob_pin"] == BytePin.of(canonical).model_dump(mode="json")
    assert BytePin.of(physical) != BytePin.of(canonical)
    with pytest.raises(ValueError, match="differs"):
        bind(b"CHANGED\r\n", canonical)
