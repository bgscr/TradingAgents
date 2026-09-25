"""Local Windows owner registration and decision inputs, separate from results.

These are closed data contracts. Only the registered native owner may authenticate
and persist them. Neither parsing nor a caller-supplied public key grants a role.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from tradingagents.policy_qualified_contract import (
    AcceptancePolicy,
    BytePin,
    Candidate,
    Closed,
    EvidenceRef,
    Identity,
    OriginalAcquisition,
    ReviewedPolicyPackage,
    Sealed,
    StageBudget,
    Text,
    utc,
)

Sid = Annotated[str, Field(pattern=r"^S-1-[0-9]+(?:-[0-9]+)+$")]
Instance = Annotated[str, Field(pattern=r"^(?:operational|test)-[0-9]{8}-[0-9]{3}$")]


class OriginalAcquisitionRegistry(Sealed):
    namespace = "policy-owner-original-acquisition-registry:v1"
    contract_version: Literal["policy-owner-original-acquisition-registry-v1"]
    source_package_manifest_pin: BytePin
    original_blocker: EvidenceRef
    before: OriginalAcquisition
    after: OriginalAcquisition
    complete_delta_ledger: EvidenceRef
    acquisition_registration_mode: Literal["EXACT_RETAINED_ORIGINAL_PAIR_ONLY"]
    newly_acquired_observations_eligible: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _pair(self) -> Self:
        if (self.before.phase, self.after.phase) != ("before", "after") or (
            self.before.observation_id == self.after.observation_id
            or self.before.original_path == self.after.original_path
        ):
            raise ValueError("the immutable registry requires the distinct original pair")
        return self

    def validate_policy(self, policy: AcceptancePolicy) -> None:
        scope = AcceptancePolicy.read(policy).scope
        if (
            scope.before != self.before
            or scope.after != self.after
            or scope.original_blocker != self.original_blocker
            or scope.complete_delta_ledger != self.complete_delta_ledger
        ):
            raise ValueError("policy does not bind the registered original acquisitions")


class StorageBinding(Sealed):
    namespace = "policy-owner-storage-binding:v1"
    contract_version: Literal["policy-owner-storage-binding-v1"]
    instance: Instance
    operational: bool
    service_name: Text
    service_sid: Sid
    operator_sid: Sid
    owner_root: Text
    owner_root_object_id: Text
    owner_root_descriptor_pin: BytePin
    original_registry_pin: BytePin
    ledger_path: Text
    ledger_object_id: Text
    owner_state_path: Text
    owner_state_object_id: Text
    ledger_identity: Identity
    native_owner_pin: BytePin
    trusted_worker_manifest_pin: BytePin
    administrators_and_system_are_trusted: Literal[True]
    candidate_has_storage_write_access: Literal[False]
    registration_is_approval: Literal[False]
    execution_authorized: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _storage(self) -> Self:
        from tradingagents.runtime_protection import _windows_path

        root, ledger = _windows_path(self.owner_root), _windows_path(self.ledger_path)
        if ledger != root + "\\state\\lineage.sqlite3":
            raise ValueError("ledger is outside the exact registered owner state directory")
        if _windows_path(self.owner_state_path) != root + "\\state\\owner.sqlite3":
            raise ValueError("owner decision database is outside registered state")
        if self.operational != self.instance.startswith("operational-"):
            raise ValueError("test and operational owner registrations cannot be exchanged")
        if self.service_name != "TA.PolicyOwner." + self.instance:
            raise ValueError("service name differs from its exact instance")
        if not self.service_sid.startswith("S-1-5-80-") or self.service_sid == self.operator_sid:
            raise ValueError("a distinct Windows service SID is required")
        return self


class RoleProposal(Sealed):
    """Proposed local project authority; enrollment requires a real human proof.

    No field claims power to waive an unknown third-party rule. The prospective
    replacement applies only to this explicitly adopted project campaign.
    """

    namespace = "policy-owner-project-role-proposal:v1"
    contract_version: Literal["policy-owner-project-role-proposal-v1"]
    instance: Instance
    operator_sid: Sid
    observed_account_name: Text
    role: Literal["PERSONAL_RESEARCH_CAMPAIGN_ISSUING_OPERATOR"]
    authority_basis: Text
    prospective_project_policy_scope: Text
    campaign_specification_scope: Text
    project_contract_scope: Text
    third_party_or_higher_policy_authority_claimed: Literal[False]
    exact_package_approved: Literal[False]
    qualification_execution_authorized: Literal[False]
    live_execution_authorized: Literal[False]
    identity: Identity


class QualificationExecutionProposal(Closed):
    selection: Literal["AUTHORIZE_BOUNDED_QUALIFICATION"]
    candidate: Candidate
    lineage_plan_identity: Identity
    stage_budgets: tuple[StageBudget, ...]
    finalization_maximum_seconds: int = Field(gt=0, le=30 * 86400)
    maximum_lineages: Literal[1]
    maximum_claims: Literal[1]
    effective_utc: Text
    expires_utc: Text
    ticket07_or_live_authorized: Literal[False]

    @model_validator(mode="after")
    def _period(self) -> Self:
        if utc(self.expires_utc) <= utc(self.effective_utc):
            raise ValueError("execution period is empty")
        return self


class PolicyDecisionProposal(Sealed):
    """The body a human reviews before Windows Hello signs its owner challenge."""

    namespace = "policy-owner-exact-decision-proposal:v1"
    contract_version: Literal["policy-owner-exact-decision-proposal-v1"]
    instance: Instance
    registered_role_identity: Identity
    storage_binding_identity: Identity
    reviewed_package: ReviewedPolicyPackage
    policy: AcceptancePolicy
    prospective_replacement_scope: Text
    campaign_specification_adoption: Text
    project_contract_adoption: Text
    execution: QualificationExecutionProposal
    selection: Literal["APPROVE", "REJECT"]
    historical_cause: Literal["UNRESOLVED"]
    historical_equal: Literal[False]
    historical_outcome: Literal["BLOCKED"]
    result_execution_authorized: Literal[False]
    ticket07_or_live_authorized: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _exact(self) -> Self:
        policy, plan = self.policy, self.policy.lineage_plan
        if (
            self.reviewed_package.policy.identity != policy.acceptance_policy_identity
            or self.reviewed_package.policy.pin != BytePin.of(policy.to_bytes())
            or self.reviewed_package.scope_evidence_manifest_pin
            != policy.scope_evidence_manifest_pin
            or self.reviewed_package.contract_source_pin != plan.consumer_contract_pin
            or self.execution.candidate != policy.scope.candidate
            or self.execution.lineage_plan_identity != plan.lineage_plan_identity
            or self.execution.stage_budgets != plan.stage_budgets
            or self.execution.finalization_maximum_seconds != plan.finalization_maximum_seconds
        ):
            raise ValueError("exact policy/package/qualification execution proposal mismatch")
        return self


class ArtifactRegistration(Closed):
    reference: EvidenceRef
    storage_name: Annotated[str, Field(pattern=r"^[0-9a-f]{64}\.blob$")]

    @model_validator(mode="after")
    def _name(self) -> Self:
        if self.storage_name != self.reference.pin.sha256 + ".blob":
            raise ValueError("artifact storage name does not match exact bytes")
        return self


def evidence_intake_entries(document: dict) -> tuple[dict, ...]:
    """Closed, nonrecursive intake batches are authenticated as one exact body."""
    if not isinstance(document, dict):
        raise ValueError("evidence intake must be an object")
    version = document.get("contract_version")
    if version == "policy-owner-evidence-intake-batch-v1":
        if (
            set(document) != {"contract_version", "entries", "execution_authorized"}
            or document["execution_authorized"] is not False
        ):
            raise ValueError("invalid evidence intake batch")
        entries = document["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
            raise ValueError("evidence intake batch count exceeds its bound")
    else:
        entries = [document]
    keys = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"contract_version", "kind", "key", "value"}
            or entry["contract_version"] != "policy-owner-evidence-intake-v1"
        ):
            raise ValueError("invalid closed evidence intake entry")
        if (
            not isinstance(entry["kind"], str)
            or not isinstance(entry["key"], str)
            or not entry["key"]
            or not isinstance(entry["value"], dict)
        ):
            raise ValueError("evidence intake key and value types are invalid")
        key = entry["kind"], entry["key"]
        if key in keys:
            raise ValueError("duplicate key within authenticated evidence intake")
        keys.add(key)
    return tuple(entries)
