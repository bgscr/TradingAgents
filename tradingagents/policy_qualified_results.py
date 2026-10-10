"""Owner-backed admission, finalization, receipt and downstream verification.

This module has no installed authority resolver and no native runner. Its trust
dependencies must come from the authenticated readiness/coordinator owner, never
from a serialized result, environment flag, or result-supplied callback. The
owner authenticates people and acquisition provenance; hashes only bind bytes.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from tradingagents.policy_coverage_models import read_model
from tradingagents.policy_coverage_verification import coverage_operation, reset_coverage_proof
from tradingagents.policy_qualification_lifecycle import LineageLedger
from tradingagents.policy_qualified_contract import (
    AcceptanceBinding,
    AcceptancePolicy,
    BoundEvidence,
    BytePin,
    Candidate,
    DownstreamEnvelope,
    EvidenceInventoryEntry,
    EvidenceInventoryRoot,
    EvidenceRef,
    ExecutionEvidence,
    HistoricalClaim,
    IndependentReviewRecord,
    LineageAllocation,
    LineageClaim,
    OperatorApproval,
    OriginalAcquisition,
    PolicyQualifiedExecutionResult,
    PostResultAcceptanceReceipt,
    PreResultClosureEvidence,
    PreResultEvidenceInventory,
    QualifiedBaseline,
    RawComparison,
    ResultBindingCore,
    ReviewedPolicyPackage,
    StageBudget,
    canonical_bytes,
    content_identity,
    strict_json,
    utc,
)


@dataclass(frozen=True, slots=True)
class AuthorityResolution:
    """An authenticated owner's fresh response; no parser accepts this as proof.

    The resolver must verify issuer entitlement over each attached authority,
    including the unavailable originating rule OR an entitled prospective
    replacement, the campaign admission rule, and the project specification.
    Merely finding documents with matching hashes cannot produce this response.
    """

    approval_bytes: bytes
    authenticated_principal: str
    authenticated_channel_session: str
    decision_artifact_bytes: bytes
    issuer_authority_bytes: bytes
    reviewed_package_manifest_bytes: bytes
    originating_policy_disposition: Literal[
        "PERMITTED_BY_ORIGINATING_AUTHORITY", "PERMITTED_BY_PROSPECTIVE_REPLACEMENT"
    ]
    originating_or_replacement_authority: EvidenceRef
    campaign_replacement_authority: EvidenceRef
    project_contract_authority: EvidenceRef
    qualification_execution_authority: EvidenceRef
    execution_candidate: Candidate
    execution_lineage_plan_identity: str
    execution_effective_utc: str
    execution_expires_utc: str
    execution_stage_budgets: tuple[StageBudget, ...]
    execution_finalization_maximum_seconds: int
    approved_policy_identity: str
    current_candidate: Candidate
    current_lineage_plan_identity: str
    current_production_inventory_identity: str
    ledger_id: str
    ledger_path: Path
    state: Literal["ACTIVE", "REVOKED", "UNKNOWN"]
    state_reference: str
    now_utc: str


@dataclass(frozen=True, slots=True)
class ObservationResolution:
    observation_id: str
    artifact: EvidenceRef
    measurement_contract_pin: BytePin
    lineage_identity: str | None
    lineage_plan_identity: str
    allocation_identity: str
    stage: str
    phase: Literal["before", "after"]
    origin: Literal["NEW_QUALIFICATION"]
    observed_at_utc: str


class TrustedPolicyOwner(Protocol):
    """Injected by the trusted process owner, never selected by document content.

    Read operations must return stable bytes from retained, immutable records.
    Original acquisition and executed-stage registration are independent of byte
    equality. A new capture/copy cannot be registered as the original acquisition.
    ``resolve`` must freshly authenticate the human decision and current authority,
    revocation, source, execution scope, clock and durable ledger registration.
    """

    def resolve(self, approval_identity: str) -> AuthorityResolution: ...
    def read_artifact(self, reference: EvidenceRef) -> bytes: ...
    def read_pinned_file(self, pin: BytePin) -> bytes: ...
    def original_acquisition(self, observation_id: str) -> OriginalAcquisition: ...
    def canonical_allocation(self, lineage_plan_identity: str) -> LineageAllocation: ...
    def observation(self, observation_id: str) -> ObservationResolution: ...
    def executed_stage(self, lineage_identity: str, kind: str) -> bytes: ...
    def review_principal(self, reference: EvidenceRef) -> str: ...
    def guest_export_root(self, lineage_identity: str) -> Path: ...
    def source_input_root(self, lineage_identity: str) -> Path: ...
    def runtime_bundle_root(self, lineage_identity: str) -> Path: ...


def confirm_policy_decision_challenge(
    exact_policy_identity: str, *, input_fn: Callable[[str], str]
) -> None:
    """Reuse protected handoff's exact-identity then literal YES interaction.

    This only checks the human interaction, exactly as the existing handoff does
    before contacting its owner. It returns no approval, receipt or principal.
    The authenticated channel owner must retain and authenticate the decision.
    """
    if not exact_policy_identity.startswith("historical-state-acceptance-policy:v1:sha256:"):
        raise ValueError("an exact reviewed policy identity is required")
    if (
        input_fn("Enter the exact reviewed acceptance policy identity: ").strip()
        != exact_policy_identity
    ):
        raise ValueError("policy identity confirmation failed")
    if input_fn("Enter YES to record your policy decision: ").strip() != "YES":
        raise ValueError("explicit human policy decision was not confirmed")


def reference(value: BaseModel, identity_field: str = "identity") -> EvidenceRef:
    raw = canonical_bytes(value.model_dump(mode="json")) + b"\n"
    return EvidenceRef(identity=getattr(value, identity_field), pin=BytePin.of(raw))


def acceptance_binding(
    policy: AcceptancePolicy, approval: OperatorApproval, claim: LineageClaim
) -> AcceptanceBinding:
    return AcceptanceBinding(
        acceptance_policy_identity=policy.acceptance_policy_identity,
        exception_identity=policy.exception_identity,
        approval_identity=approval.approval_identity,
        lineage_plan_identity=policy.lineage_plan.lineage_plan_identity,
        lineage_identity=claim.lineage_identity,
        original_blocker_identity=policy.scope.original_blocker.identity,
        scope_evidence_manifest_pin=policy.scope_evidence_manifest_pin,
    )


def _inventory_entries(
    roots: tuple[EvidenceInventoryRoot, ...],
) -> tuple[EvidenceInventoryEntry, ...]:
    """Read every file/directory in the fixed scope; no filename exclusions."""
    from tradingagents.runtime_protection import _windows_path

    def members(path: Path) -> list[Path]:
        found = []

        def walk(directory: Path) -> None:
            # Path.rglob may suppress inaccessible-directory errors; a complete
            # evidence inventory must propagate them and fail closed instead.
            with os.scandir(directory) as stream:
                children = sorted(stream, key=lambda item: item.name.casefold())
            for child in children:
                item = Path(child.path)
                found.append(item)
                metadata = item.lstat()
                if (
                    stat.S_ISDIR(metadata.st_mode)
                    and not getattr(metadata, "st_file_attributes", 0) & 0x400
                ):
                    walk(item)

        walk(path)
        return sorted(found, key=lambda item: item.relative_to(path).as_posix().casefold())

    entries = []
    for root in roots:
        path = Path(_windows_path(root.path))
        root_stat = path.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or getattr(root_stat, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("evidence inventory root must be an ordinary retained directory")
        paths = members(path)
        names = [item.relative_to(path).as_posix() for item in paths]
        for item, relative in zip(paths, names, strict=True):
            before = item.lstat()
            if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
                raise ValueError("evidence inventory refuses reparse/link entries")
            if stat.S_ISDIR(before.st_mode):
                entries.append(
                    EvidenceInventoryEntry(
                        root_role=root.role, relative_path=relative, kind="directory", pin=None
                    )
                )
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("evidence inventory contains an unsupported object")
            digest = sha256()
            count = 0
            with item.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    count += len(chunk)
            after = item.lstat()

            def metadata(value):
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                    getattr(value, "st_file_attributes", 0),
                )

            if metadata(before) != metadata(after) or count != before.st_size:
                raise ValueError("evidence inventory file changed during read")
            entries.append(
                EvidenceInventoryEntry(
                    root_role=root.role,
                    relative_path=relative,
                    kind="file",
                    pin=BytePin(byte_length=count, sha256=digest.hexdigest()),
                )
            )
        if names != [item.relative_to(path).as_posix() for item in members(path)]:
            raise ValueError("evidence inventory membership changed during read")
        if path.lstat().st_mtime_ns != root_stat.st_mtime_ns:
            raise ValueError("evidence inventory root changed during read")
    return tuple(entries)


def capture_pre_result_evidence_inventory(
    *,
    binding: AcceptanceBinding,
    candidate: Candidate,
    roots: tuple[EvidenceInventoryRoot, ...],
    captured_at_utc: str,
) -> PreResultEvidenceInventory:
    """Read-only capture; publish the returned inventory OUTSIDE its closed roots.

    Capture grants no admission or result authority. The verifier checks these
    exact owner/claim roots, coverage and post-review ordering again before use.
    """
    return PreResultEvidenceInventory.seal(
        contract_version="policy-qualified-pre-result-inventory-v1",
        binding=binding,
        candidate=candidate,
        roots=roots,
        entries=_inventory_entries(roots),
        captured_at_utc=captured_at_utc,
    )


class PolicyQualifiedVerifier:
    """One verification implementation shared by producer and every consumer."""

    def __init__(
        self, *, owner: TrustedPolicyOwner | None = None, ledger: LineageLedger | None = None
    ) -> None:
        self._owner = owner
        self._ledger = ledger

    @property
    def owner(self) -> TrustedPolicyOwner:
        if self._owner is None:
            raise ValueError("activation requires an authenticated policy/qualification owner")
        return self._owner

    @property
    def ledger(self) -> LineageLedger:
        if self._ledger is None:
            raise ValueError("activation requires the registered durable lineage ledger")
        return self._ledger

    def _bytes(self, ref: EvidenceRef) -> bytes:
        ref = EvidenceRef.read(ref)
        raw = self.owner.read_artifact(ref)
        if not isinstance(raw, bytes):
            raise ValueError("owner did not resolve stable artifact bytes")
        ref.pin.check(raw)
        return raw

    def _model(
        self, ref: EvidenceRef, model: type[BaseModel], field: str = "identity"
    ) -> BaseModel:
        raw = self._bytes(ref)
        from tradingagents.policy_coverage_models import model_for

        parsed = model_for(model, raw).model_validate_json(canonical_bytes(strict_json(raw)))
        if getattr(parsed, field) != ref.identity:
            raise ValueError("artifact semantic identity is not its bound identity")
        return parsed

    @staticmethod
    def _allocated_root(path: Path, claim: LineageClaim, stage_index: int) -> Path:
        resolved = path.resolve(strict=True)
        allocation = Path(claim.stages[stage_index].root).resolve(strict=True)
        if not resolved.is_relative_to(allocation):
            raise ValueError("qualification input is outside its exact claimed stage root")
        return path

    def _guest_export_root(self, claim: LineageClaim) -> Path:
        return self._allocated_root(self.owner.guest_export_root(claim.lineage_identity), claim, 3)

    def _saved(self, ref: EvidenceRef, namespace: str) -> dict:
        obj = strict_json(self._bytes(ref))
        if not isinstance(obj, dict) or "identity" not in obj:
            raise ValueError("saved artifact has no closed content identity")
        expected = content_identity(namespace, {k: v for k, v in obj.items() if k != "identity"})
        if obj["identity"] != ref.identity or obj["identity"] != expected:
            raise ValueError("saved content identity mismatch")
        return obj

    def _authority(
        self, policy: AcceptancePolicy, approval: OperatorApproval
    ) -> AuthorityResolution:
        from tradingagents.policy_coverage_models import require_policy_pair

        require_policy_pair(policy, approval)
        resolved = self.owner.resolve(approval.approval_identity)
        if not isinstance(resolved, AuthorityResolution):
            raise ValueError("unknown authentication state")
        authenticated = read_model(OperatorApproval, resolved.approval_bytes)
        if authenticated != approval or not resolved.state_reference:
            raise ValueError("unapproved, revoked, unauthenticated or unresolved decision")
        if (
            resolved.ledger_id != self.ledger.ledger_id
            or resolved.ledger_path.resolve(strict=True) != self.ledger.path
        ):
            raise ValueError("approval is not registered to this durable ledger")
        self.ledger.require_not_invalidated(
            approval.approval_identity, approval.lineage_plan_identity
        )
        if resolved.state == "REVOKED":
            self.ledger.invalidate(
                approval.approval_identity,
                approval.lineage_plan_identity,
                reason="REVOKED",
                at=resolved.now_utc,
            )
        if resolved.state != "ACTIVE":
            raise ValueError("unapproved, revoked, unauthenticated or unresolved decision")
        if resolved.originating_policy_disposition not in {
            "PERMITTED_BY_ORIGINATING_AUTHORITY",
            "PERMITTED_BY_PROSPECTIVE_REPLACEMENT",
        }:
            raise ValueError("originating/replacement-policy authority is missing")
        if (
            resolved.authenticated_principal != approval.issuer_principal
            or resolved.authenticated_channel_session != approval.channel_session_reference
        ):
            raise ValueError("decision principal/channel authentication mismatch")
        approval.decision_artifact_pin.check(resolved.decision_artifact_bytes)
        approval.issuer_authority_pin.check(resolved.issuer_authority_bytes)
        approval.reviewed_package_manifest_pin.check(resolved.reviewed_package_manifest_bytes)
        package = ReviewedPolicyPackage.read(resolved.reviewed_package_manifest_bytes)
        if (
            package.policy.identity != policy.acceptance_policy_identity
            or package.policy.pin != BytePin.of(policy.to_bytes())
            or package.scope_evidence_manifest_pin != policy.scope_evidence_manifest_pin
            or package.contract_source_pin != policy.lineage_plan.consumer_contract_pin
        ):
            raise ValueError("completed reviewed package does not bind this exact policy/contract")
        for pin in (
            policy.predecessor_proposal_pin,
            policy.scope_evidence_manifest_pin,
            policy.authority_source_contract_pin,
            policy.lineage_plan.procedure_pin,
            policy.lineage_plan.coordinator_pin,
            policy.lineage_plan.consumer_contract_pin,
            policy.lineage_plan.measurement_contract_pin,
            policy.scope.before.collector_pin,
            policy.scope.before.procedure_pin,
            policy.scope.after.collector_pin,
            policy.scope.after.procedure_pin,
        ):
            pin.check(self.owner.read_pinned_file(pin))
        # These are independently authenticated authority records, not self-asserted
        # permissions in the approval. Also recheck their exact retained bytes.
        for authority in (
            resolved.originating_or_replacement_authority,
            resolved.campaign_replacement_authority,
            resolved.project_contract_authority,
            resolved.qualification_execution_authority,
        ):
            self._bytes(authority)
        if (
            approval.acceptance_policy_identity != policy.acceptance_policy_identity
            or approval.exception_identity != policy.exception_identity
            or approval.lineage_plan_identity != policy.lineage_plan.lineage_plan_identity
            or approval.candidate != policy.scope.candidate
            or approval.scope_evidence_manifest_pin != policy.scope_evidence_manifest_pin
            or resolved.approved_policy_identity != policy.acceptance_policy_identity
            or Candidate.read(resolved.current_candidate) != policy.scope.candidate
            or resolved.current_lineage_plan_identity != policy.lineage_plan.lineage_plan_identity
            or Candidate.read(resolved.execution_candidate) != policy.scope.candidate
            or resolved.execution_lineage_plan_identity != policy.lineage_plan.lineage_plan_identity
            or tuple(StageBudget.read(x) for x in resolved.execution_stage_budgets)
            != policy.lineage_plan.stage_budgets
            or type(resolved.execution_finalization_maximum_seconds) is not int
            or resolved.execution_finalization_maximum_seconds
            != policy.lineage_plan.finalization_maximum_seconds
        ):
            raise ValueError(
                "approval, current source, policy or execution lineage binding mismatch"
            )
        now = utc(resolved.now_utc)
        if now >= utc(approval.expires_utc):
            self.ledger.invalidate(
                approval.approval_identity,
                approval.lineage_plan_identity,
                reason="EXPIRED",
                at=resolved.now_utc,
            )
        if not utc(approval.effective_utc) <= now < utc(approval.expires_utc):
            raise ValueError("approval not effective or terminal period expired")
        if not utc(resolved.execution_effective_utc) <= now < utc(resolved.execution_expires_utc):
            raise ValueError("separate qualification execution authority is not active")
        return resolved

    @staticmethod
    def _budget_fit(
        policy: AcceptancePolicy,
        approval: OperatorApproval,
        resolved: AuthorityResolution,
        next_stage: int,
    ) -> None:
        remaining = policy.lineage_plan.finalization_maximum_seconds + sum(
            item.maximum_execution_seconds + item.maximum_cleanup_seconds
            for item in policy.lineage_plan.stage_budgets[next_stage:]
        )
        deadline = min(utc(approval.expires_utc), utc(resolved.execution_expires_utc))
        if utc(resolved.now_utc) + timedelta(seconds=remaining) >= deadline:
            raise ValueError("remaining canonical stage, cleanup and closure budgets do not fit")

    def _historical(self, policy: AcceptancePolicy) -> dict:
        scope = policy.scope
        values = []
        for acquisition in (scope.before, scope.after):
            registered = OriginalAcquisition.read(
                self.owner.original_acquisition(acquisition.observation_id)
            )
            if registered != acquisition:
                raise ValueError("not the exact retained original acquisition")
            obj = self._saved(acquisition.artifact, "crash-protection-historical-inventory:v1")
            if (
                obj.get("phase") != acquisition.phase
                or obj.get("elapsed_seconds") != acquisition.collection_elapsed_seconds
            ):
                raise ValueError("original phase/duration provenance mismatch")
            self._roots(obj)
            values.append(obj)
        blocker = self._saved(
            scope.original_blocker, "crash-protection-source-requalification-blocked:v1"
        )
        original_candidate = blocker.get("candidate", {})
        historical = blocker.get("historical_inventory", {})
        difference = blocker.get("difference", {})
        if (
            blocker.get("status") != "BLOCKED"
            or {k: original_candidate.get(k) for k in ("commit", "tree", "parent")}
            != scope.original_candidate.model_dump(mode="json")
            or historical.get("equal") is not False
            or historical.get("changed_roots") != [scope.root]
            or difference.get("root") != scope.root
            or difference.get("changed_directory_count") != scope.changed_rows
            or difference.get("changed_field") != scope.changed_field
            or difference.get("before_hex") != f"0x{scope.raw_before:08x}"
            or difference.get("after_hex") != f"0x{scope.raw_after:08x}"
            or difference.get("changed_bit") != f"0x{scope.raw_before ^ scope.raw_after:08x}"
        ):
            raise ValueError("original blocker/candidate/raw scope binding mismatch")
        artifacts = blocker.get("artifacts", [])
        for acquisition in (scope.before, scope.after):
            matches = [x for x in artifacts if x.get("path") == acquisition.original_path]
            if (
                len(matches) != 1
                or BytePin.read({k: matches[0].get(k) for k in ("byte_length", "sha256")})
                != acquisition.artifact.pin
            ):
                raise ValueError(
                    "original blocker does not pin this acquisition's exact file bytes"
                )
        if not any(
            {k: x.get(k) for k in ("byte_length", "sha256")}
            == scope.complete_delta_ledger.pin.model_dump(mode="json")
            for x in artifacts
        ):
            raise ValueError("original blocker does not pin the complete delta ledger")
        before, after = values
        if {
            k: v
            for k, v in before.items()
            if k not in {"identity", "phase", "elapsed_seconds", "roots"}
        } != {
            k: v
            for k, v in after.items()
            if k not in {"identity", "phase", "elapsed_seconds", "roots", "equal", "changed_roots"}
        } or before.get("no_acl_write_or_recovery") is not True:
            raise ValueError("original acquisition metadata scope mismatch")
        old, new = self._roots(before), self._roots(after)
        if tuple(old) != tuple(new) or after.get("equal") is not False:
            raise ValueError("historical root scope/equality mismatch")
        changes = []
        for root, previous in old.items():
            current = new[root]
            if previous == current:
                continue
            if root != scope.root or (
                previous["inventory_identity"] != scope.before_root_identity
                or current["inventory_identity"] != scope.after_root_identity
            ):
                raise ValueError("additional historical root/field scope")
            if {
                k: v for k, v in previous.items() if k not in {"entries", "inventory_identity"}
            } != {k: v for k, v in current.items() if k not in {"entries", "inventory_identity"}}:
                raise ValueError("historical root metadata differs")
            a = {(x["path"], x["kind"]): x for x in previous["entries"]}
            b = {(x["path"], x["kind"]): x for x in current["entries"]}
            rows = []
            for key in sorted(set(a) | set(b)):
                x, y = a.get(key), b.get(key)
                if x == y:
                    continue
                if x is None or y is None or key[1] != "directory":
                    raise ValueError("additional historical path/kind scope")
                fields = sorted(
                    k for k in set(x) | set(y) if k not in x or k not in y or x[k] != y[k]
                )
                if fields != [scope.changed_field] or (
                    type(x.get("attributes")) is not int
                    or type(y.get("attributes")) is not int
                    or x["attributes"] != scope.raw_before
                    or y["attributes"] != scope.raw_after
                ):
                    raise ValueError("additional historical field/bit scope")
                rows.append(
                    {
                        "relative_path": key[0],
                        "kind": key[1],
                        "before": x,
                        "after": y,
                        "changed_fields": fields,
                    }
                )
            changes.append(
                {
                    "root": root,
                    "before_identity": previous["inventory_identity"],
                    "after_identity": current["inventory_identity"],
                    "changes": rows,
                }
            )
        ledger = self._saved(
            scope.complete_delta_ledger, "crash-protection-historical-delta-detail:v1"
        )
        if (
            len(changes) != 1
            or len(changes[0]["changes"]) != scope.changed_rows
            or after.get("changed_roots") != [scope.root]
            or ledger.get("roots") != changes
            or ledger.get("status") != "DIFFERENCE_REQUIRES_CLASSIFICATION"
            or ledger.get("source_modified") is not False
            or ledger.get("iso02_started") is not False
            or ledger.get("no_repair_performed") is not True
        ):
            raise ValueError("complete historical delta ledger mismatch")
        return after

    @staticmethod
    def _roots(obj: dict) -> dict:
        roots = obj.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("raw comparison lacks a complete inventory")
        mapped = {}
        for root in roots:
            if not isinstance(root, dict) or root.get("path") in mapped:
                raise ValueError("duplicate/invalid historical root")
            entries = root.get("entries")
            if not isinstance(entries, list) or len(
                {(x["path"], x["kind"]) for x in entries}
            ) != len(entries):
                raise ValueError("duplicate/invalid historical inventory row")
            if root.get("inventory_identity") != content_identity(
                "crash-protection-historical-root-inventory:v1", entries
            ):
                raise ValueError("raw root content identity mismatch")
            mapped[root["path"]] = root
        return mapped

    def _comparison(
        self,
        comparison: RawComparison,
        *,
        policy: AcceptancePolicy,
        claim: LineageClaim | LineageAllocation,
        stage: str,
        bridge: bool = False,
    ) -> None:
        comparison = read_model(RawComparison, comparison)
        approved, authority = self._claim_authority(policy, claim)
        if not bridge and not isinstance(claim, LineageClaim):
            raise ValueError("new stage comparisons require a consumed lineage")
        if comparison.measurement_contract_pin != policy.lineage_plan.measurement_contract_pin:
            raise ValueError("raw measurements are not comparable to the approved contract")
        for phase in ("before", "after"):
            observation_id = getattr(comparison, phase + "_observation")
            artifact = getattr(comparison, phase)
            if bridge and phase == "before":
                if (
                    observation_id != policy.scope.after.observation_id
                    or artifact != policy.scope.after.artifact
                ):
                    raise ValueError(
                        "bridge does not start at the retained original after inventory"
                    )
                continue
            resolved = self.owner.observation(observation_id)
            expected_phase = "before" if bridge else phase
            if not isinstance(resolved, ObservationResolution) or (
                resolved.origin != "NEW_QUALIFICATION"
                or resolved.observation_id != observation_id
                or EvidenceRef.read(resolved.artifact) != artifact
                or resolved.measurement_contract_pin != comparison.measurement_contract_pin
                or resolved.lineage_identity != (None if bridge else claim.lineage_identity)
                or resolved.lineage_plan_identity != policy.lineage_plan.lineage_plan_identity
                or resolved.allocation_identity != claim.allocation_identity
                or resolved.stage != stage
                or resolved.phase != expected_phase
                or observation_id
                in {policy.scope.before.observation_id, policy.scope.after.observation_id}
            ):
                raise ValueError("raw acquisition lineage/provenance is unresolved")
            observed = utc(resolved.observed_at_utc)
            earliest = utc(approved.effective_utc) if bridge else utc(claim.claimed_at_utc)
            latest = (
                utc(claim.claimed_at_utc)
                if bridge and isinstance(claim, LineageClaim)
                else utc(authority.now_utc)
            )
            if not earliest <= observed <= latest:
                raise ValueError("new observation lies outside its approval/lineage window")
        from tradingagents.policy_coverage_models import CoveragePolicy, CoverageRawComparison

        if isinstance(policy, CoveragePolicy):
            from tradingagents.policy_coverage_verification import verify_coverage_comparison

            verify_coverage_comparison(self, comparison, policy=policy, claim=claim, bridge=bridge)
            return
        if isinstance(comparison, CoverageRawComparison):
            raise ValueError("old policy cannot adopt coverage comparison semantics")
        a = self._saved(comparison.before, "crash-protection-historical-inventory:v1")
        b = self._saved(comparison.after, "crash-protection-historical-inventory:v1")
        if (
            a.get("phase") != ("after" if bridge else "before")
            or b.get("phase") != ("before" if bridge else "after")
            or (not bridge and (b.get("equal") is not True or b.get("changed_roots") != []))
        ):
            raise ValueError("new comparison acquisition phase/equality is contradictory")
        if self._roots(a) != self._roots(b):
            raise ValueError("new raw discrepancy; historical policy cannot waive it")

    def eligibility(self, policy: object, approval: object) -> AuthorityResolution:
        reset_coverage_proof(self)
        policy, approval = (
            read_model(AcceptancePolicy, policy),
            read_model(OperatorApproval, approval),
        )
        self._authority(policy, approval)
        from tradingagents.windows_sandbox_acceptance import (
            TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
            TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        )

        if (
            policy.lineage_plan.ledger_identity != TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
            or policy.lineage_plan.result_schema_identity
            != TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY
        ):
            raise ValueError("approved ledger/schema is not the current canonical contract")
        self._historical(policy)
        return self._authority(policy, approval)

    @coverage_operation
    def claim(
        self, policy: object, approval: object, allocation: object, *, new_start_bridge: object
    ) -> LineageClaim:
        policy, approval, allocation = (
            read_model(AcceptancePolicy, policy),
            read_model(OperatorApproval, approval),
            LineageAllocation.read(allocation),
        )
        resolved = self.eligibility(policy, approval)
        self._allocation_binding(policy, approval, allocation)
        self._budget_fit(policy, approval, resolved, 0)
        self._comparison(
            read_model(RawComparison, new_start_bridge),
            policy=policy,
            claim=allocation,
            stage="ISO02",
            bridge=True,
        )
        # Mint the claim only after evidence verification, with a fresh owner
        # clock. The pre-claim bridge binds allocation/plan, never a future hash.
        resolved = self._authority(policy, approval)
        if utc(resolved.now_utc) >= utc(approval.claim_by_utc):
            self.ledger.expire_unclaimed(
                approval.approval_identity, approval.lineage_plan_identity, at=resolved.now_utc
            )
            raise ValueError("claim clock/window expired")
        self._budget_fit(policy, approval, resolved, 0)
        payload = allocation.model_dump(mode="json")
        from tradingagents.policy_coverage_models import CoverageClaim, CoveragePolicy

        claim_model = CoverageClaim if isinstance(policy, CoveragePolicy) else LineageClaim
        payload["contract_version"] = (
            "historical-state-qualification-lineage-v2"
            if isinstance(policy, CoveragePolicy)
            else "historical-state-qualification-lineage-v1"
        )
        if isinstance(policy, CoveragePolicy):
            payload["coverage"] = read_model(RawComparison, new_start_bridge).coverage
        claim = claim_model.seal(
            **payload,
            claimed_at_utc=resolved.now_utc,
            state="CLAIMED_ATTEMPT_CONSUMED",
            attempt_ordinal=1,
        )
        # Reserved before ISO02 and before any external stage allocation/launch.
        self.ledger.claim(claim)
        return claim

    def _allocation_binding(
        self, policy: AcceptancePolicy, approval: OperatorApproval, allocation: LineageAllocation
    ) -> None:
        if (
            allocation.approval_identity != approval.approval_identity
            or allocation.acceptance_policy_identity != policy.acceptance_policy_identity
            or allocation.exception_identity != policy.exception_identity
            or allocation.lineage_plan != policy.lineage_plan
            or allocation.candidate != policy.scope.candidate
        ):
            raise ValueError("claim candidate/policy/plan/clock mismatch")
        expected = LineageAllocation.read(
            self.owner.canonical_allocation(policy.lineage_plan.lineage_plan_identity)
        )
        if expected != allocation:
            raise ValueError("claim is not the owner's exact canonical one-lineage allocation")

    def _claim_binding(
        self, policy: AcceptancePolicy, approval: OperatorApproval, claim: LineageClaim
    ) -> None:
        from tradingagents.policy_coverage_models import CoverageClaim, CoveragePolicy

        if isinstance(policy, CoveragePolicy) != isinstance(claim, CoverageClaim):
            raise ValueError("claim does not bind the policy's coverage contract version")
        self._allocation_binding(policy, approval, claim.to_allocation())
        if (
            not utc(approval.effective_utc)
            <= utc(claim.claimed_at_utc)
            < utc(approval.claim_by_utc)
        ):
            raise ValueError("claim clock/window mismatch")

    def _claim_authority(
        self, policy: AcceptancePolicy, claim: LineageAllocation
    ) -> tuple[OperatorApproval, AuthorityResolution]:
        response = self.owner.resolve(claim.approval_identity)
        if not isinstance(response, AuthorityResolution):
            raise ValueError("unknown current authority")
        approved = read_model(OperatorApproval, response.approval_bytes)
        if approved.approval_identity != claim.approval_identity:
            raise ValueError("current authority approval binding mismatch")
        return approved, self._authority(policy, approved)

    @coverage_operation
    def begin_stage(
        self,
        policy: object,
        approval: object,
        claim: object,
        *,
        stage: str,
        new_start_bridge: object,
    ) -> str:
        policy, approval, claim = (
            read_model(AcceptancePolicy, policy),
            read_model(OperatorApproval, approval),
            read_model(LineageClaim, claim),
        )
        try:
            resolved = self.eligibility(policy, approval)
            self._claim_binding(policy, approval, claim)
            index = policy.lineage_plan.stage_slots.index(stage)
            self._budget_fit(policy, approval, resolved, index)
            self._revalidate_completed(policy, claim)
            self._comparison(
                read_model(RawComparison, new_start_bridge),
                policy=policy,
                claim=claim,
                stage="ISO02",
                bridge=True,
            )
            resolved = self._authority(policy, approval)
            self._claim_binding(policy, approval, claim)
            self._budget_fit(policy, approval, resolved, index)
            return self.ledger.transition(claim, kind="START:" + stage, at=resolved.now_utc)
        except (ValueError, OSError):
            self._fail_existing(claim)
            raise

    @coverage_operation
    def complete_stage(
        self,
        policy: object,
        approval: object,
        claim: object,
        *,
        stage: str,
        evidence: BoundEvidence,
    ) -> str:
        policy, approval, claim = (
            read_model(AcceptancePolicy, policy),
            read_model(OperatorApproval, approval),
            read_model(LineageClaim, claim),
        )
        try:
            resolved = self.eligibility(policy, approval)
            self._claim_binding(policy, approval, claim)
            index = policy.lineage_plan.stage_slots.index(stage)
            self._budget_fit(policy, approval, resolved, index + 1)
            validated = self._revalidate_completed(policy, claim)
            node = read_model(BoundEvidence, evidence)
            if (
                node.binding != acceptance_binding(policy, approval, claim)
                or node.candidate != claim.candidate
                or node.kind
                != (
                    {"REVIEWS": "REVIEW_SPEC", "CLOSURE_HANDOFF": "PRE_RESULT_CLOSURE"}.get(
                        stage, stage
                    )
                )
                or utc(node.recorded_at_utc) > utc(resolved.now_utc)
            ):
                raise ValueError("stage completion binding mismatch")
            self._registered_stage(node, claim)
            self._stage_payload(node, policy=policy, claim=claim, _validated=validated)
            resolved = self._authority(policy, approval)
            self._claim_binding(policy, approval, claim)
            self._budget_fit(policy, approval, resolved, index + 1)
            started = self.ledger.require_active(claim)[-1]
            budget = policy.lineage_plan.stage_budgets[index]
            if (
                started["kind"] != "START:" + stage
                or not utc(started["recorded_at"])
                <= utc(node.recorded_at_utc)
                <= utc(resolved.now_utc)
                or utc(resolved.now_utc) - utc(started["recorded_at"])
                > timedelta(
                    seconds=budget.maximum_execution_seconds + budget.maximum_cleanup_seconds
                )
            ):
                raise ValueError("stage completion exceeded its canonical execution/cleanup budget")
            return self.ledger.transition(
                claim, kind="COMPLETE:" + stage, at=resolved.now_utc, evidence=reference(node)
            )
        except (ValueError, OSError):
            self._fail_existing(claim)
            raise

    def _fail_existing(self, claim: LineageClaim) -> None:
        # A failed check can never free a reservation. Preserve prior terminal
        # outcomes; missing/conflicting ledgers remain failures, never recreation.
        try:
            events = self.ledger.require_active(claim)
            at = events[-1]["recorded_at"]
            try:
                current = self.owner.resolve(claim.approval_identity)
                if isinstance(current, AuthorityResolution) and utc(current.now_utc) >= utc(at):
                    at = current.now_utc
            except (ValueError, OSError):
                pass
            self.ledger.transition(claim, kind="FAILED", at=at)
        except (ValueError, OSError):
            pass

    def _registered_stage(self, node: BoundEvidence, claim: LineageClaim) -> None:
        raw = self.owner.executed_stage(claim.lineage_identity, node.kind)
        if read_model(BoundEvidence, raw) != node or BytePin.of(raw) != reference(node).pin:
            raise ValueError("stage is not the trusted coordinator's immutable execution record")

    def _owned_node(self, kind: str, claim: LineageClaim) -> BoundEvidence:
        node = read_model(BoundEvidence, self.owner.executed_stage(claim.lineage_identity, kind))
        if node.kind != kind:
            raise ValueError("coordinator stage record has the wrong kind")
        return node

    def _source_runtime(self, claim: LineageClaim) -> tuple[BaseModel, BaseModel]:
        from tradingagents.windows_sandbox_runner import (
            ReviewedSourceIdentity,
            RuntimeBundleInputContract,
        )

        iso02 = self._owned_node("ISO02", claim)
        return (
            self._model(iso02.artifacts[0], ReviewedSourceIdentity, "source_input_identity"),
            self._model(
                iso02.artifacts[1], RuntimeBundleInputContract, "runtime_bundle_contract_identity"
            ),
        )

    @staticmethod
    def _static_source(binding: BaseModel, source: BaseModel, runtime: BaseModel) -> None:
        expected = {
            "source_commit_id": source.commit_id,
            "source_tree_hash": source.tree_hash,
            "source_input_identity": source.source_input_identity,
            "source_manifest_identity": source.source_manifest_identity,
            "source_archive_sha256": source.archive_sha256,
            "runtime_bundle_manifest_sha256": runtime.bundle_manifest_sha256,
            "runtime_bundle_contract_identity": runtime.runtime_bundle_contract_identity,
            "runtime_bundle_identity": runtime.bundle_manifest_identity,
        }
        if any(getattr(binding, key) != value for key, value in expected.items()):
            raise ValueError("canonical stage source/runtime binding mismatch")

    def _iso03_bindings(
        self,
        report: BaseModel,
        before: BaseModel,
        after: BaseModel,
        policy: AcceptancePolicy,
        claim: LineageClaim,
    ) -> None:
        from tradingagents.windows_sandbox_acceptance import build_ticket06_control_environment
        from tradingagents.windows_sandbox_runner import (
            build_environment_policy,
            build_guest_layout,
        )

        source, runtime = self._source_runtime(claim)
        static = report.static_bindings
        if static is None or report.guest_result is None:
            raise ValueError("ISO03 canonical static/guest evidence is missing")
        self._static_source(static, source, runtime)
        self._static_source(report.guest_result, source, runtime)
        guest_static = report.guest_result.model_dump(mode="json")
        if any(guest_static[key] != value for key, value in static.model_dump(mode="json").items()):
            raise ValueError("ISO03 guest/static binding mismatch")
        environment = build_ticket06_control_environment(
            build_environment_policy(
                run_live_environment=source.run_live_environment,
                layout=build_guest_layout(),
            ).variables
        )
        structure = static.acceptance_runner_structure
        harness_module = next(
            (
                item
                for item in report.host_harness.files
                if item.relative_path == "tradingagents/windows_sandbox_qualification.py"
            ),
            None,
        )
        if (
            report.implementation_base_commit_id != claim.candidate.commit
            or report.candidate_tree_hash != claim.candidate.tree
            or structure.command_ledger_identity != policy.lineage_plan.ledger_identity
            or structure.result_schema_identity != policy.lineage_plan.result_schema_identity
            or structure.environment_policy_identity != environment.policy_identity
            or report.runner_input_identity != structure.runner_input_identity
            or structure.host_harness_manifest_identity != report.host_harness.manifest_identity
            or structure.boundary_runner_identity != static.runner_identity
            or structure.wsb_config_identity != static.wsb_config_identity
            or structure.mapping_manifest_identity != static.mapping_manifest_identity
            or harness_module is None
            or structure.host_acceptance_module_sha256 != harness_module.sha256
            or report.platform_qualification.platform_report_identity
            != report.platform_report.report_identity
            or static.platform_qualification_identity
            != report.platform_qualification.qualification_identity
            or static.host_before_inventory_identity != before.inventory_identity
            or report.semantic_before != before.semantic_inventory
            or report.semantic_after != after.semantic_inventory
            or before.immutable_verifier_identity != static.immutable_verifier_identity
            or before.protected_root_manifest_identity != static.protected_root_manifest_identity
        ):
            raise ValueError("ISO03 source/runner/harness/environment/host binding mismatch")

    def _revalidate_completed(self, policy: AcceptancePolicy, claim: LineageClaim) -> set[str]:
        validated: set[str] = set()
        for event in self.ledger.require_active(claim):
            if not event["kind"].startswith("COMPLETE:"):
                continue
            stage = event["kind"].removeprefix("COMPLETE:")
            kind = {"REVIEWS": "REVIEW_SPEC", "CLOSURE_HANDOFF": "PRE_RESULT_CLOSURE"}.get(
                stage, stage
            )
            node = read_model(
                BoundEvidence, self.owner.executed_stage(claim.lineage_identity, kind)
            )
            if reference(node) != EvidenceRef.read(event["payload"]):
                raise ValueError("completed stage evidence changed after consumption")
            self._stage_payload(node, policy=policy, claim=claim, _validated=validated)
        return validated

    def _stage_payload(
        self,
        node: BoundEvidence,
        *,
        policy: AcceptancePolicy,
        claim: LineageClaim,
        _validated: set[str] | None = None,
    ) -> None:
        """Reject stage failure at completion, before the next stage can start."""
        from tradingagents.windows_sandbox_acceptance import (
            Ticket06AcceptanceResult,
            Ticket06CompletionMarker,
            validate_ticket06_acceptance_export,
        )
        from tradingagents.windows_sandbox_qualification import (
            ISO03HostQualificationReport,
            ISO04Ticket06AcceptanceReport,
            ProtectedHostInventory,
            compare_protected_host_inventories,
        )
        from tradingagents.windows_sandbox_runner import (
            ReviewedSourceIdentity,
            RuntimeBundleInputContract,
        )

        validated = set() if _validated is None else _validated
        from tradingagents.policy_coverage_verification import require_record_coverage

        require_record_coverage(policy, claim, node)
        if node.identity in validated:
            return
        self._claim_authority(policy, claim)
        expected_kinds = {
            "ISO02": (),
            "ISO03": ("ISO02",),
            "QCF17": ("ISO03",),
            "ISO04_TICKET06": ("QCF17",),
            "REVIEW_STANDARDS": ("ISO02", "ISO03", "QCF17", "ISO04_TICKET06"),
            "REVIEW_SPEC": ("ISO02", "ISO03", "QCF17", "ISO04_TICKET06"),
            "PRE_RESULT_CLOSURE": ("REVIEW_STANDARDS", "REVIEW_SPEC"),
        }
        predecessors = tuple(self._owned_node(kind, claim) for kind in expected_kinds[node.kind])
        if (
            node.predecessors != tuple(reference(x) for x in predecessors)
            or node.candidate != claim.candidate
            or node.binding.acceptance_policy_identity != policy.acceptance_policy_identity
            or node.binding.exception_identity != policy.exception_identity
            or node.binding.approval_identity != claim.approval_identity
            or node.binding.lineage_plan_identity != claim.lineage_plan.lineage_plan_identity
            or node.binding.lineage_identity != claim.lineage_identity
            or node.binding.original_blocker_identity != policy.scope.original_blocker.identity
            or node.binding.scope_evidence_manifest_pin != policy.scope_evidence_manifest_pin
            or utc(node.recorded_at_utc) < utc(claim.claimed_at_utc)
        ):
            raise ValueError("stage predecessor/source/acceptance binding mismatch")
        self._registered_stage(node, claim)
        for previous in predecessors:
            if previous.binding != node.binding or utc(previous.recorded_at_utc) > utc(
                node.recorded_at_utc
            ):
                raise ValueError("stage predecessor binding/order mismatch")
            self._stage_payload(previous, policy=policy, claim=claim, _validated=validated)

        counts = {
            "ISO02": 2,
            "ISO03": 5,
            "QCF17": 1,
            "ISO04_TICKET06": 6,
            "REVIEW_STANDARDS": 1,
            "REVIEW_SPEC": 1,
            "PRE_RESULT_CLOSURE": 1,
        }
        if len(node.artifacts) != counts[node.kind]:
            raise ValueError("stage artifacts are incomplete")
        if node.kind == "ISO02":
            source = self._model(node.artifacts[0], ReviewedSourceIdentity, "source_input_identity")
            runtime = self._model(
                node.artifacts[1], RuntimeBundleInputContract, "runtime_bundle_contract_identity"
            )
            if (
                source.commit_id != claim.candidate.commit
                or source.tree_hash != claim.candidate.tree
                or runtime.state != "sealed"
            ):
                raise ValueError("mandatory ISO02 source/runtime predicate failed")
            self._qualified_inputs(source, runtime, claim)
        elif node.kind in {"ISO03", "ISO04_TICKET06"}:
            iso03 = node.kind == "ISO03"
            model = ISO03HostQualificationReport if iso03 else ISO04Ticket06AcceptanceReport
            report = self._model(node.artifacts[0], model, "report_identity")
            before = self._model(node.artifacts[1], ProtectedHostInventory, "inventory_identity")
            after = self._model(node.artifacts[2], ProtectedHostInventory, "inventory_identity")
            delta = compare_protected_host_inventories(before, after)
            if not delta.equal:
                raise ValueError("mandatory stage host zero delta failed")
            if iso03:
                if (
                    report.status.value != "pass"
                    or report.host_delta != delta
                    or report.host_before_inventory_identity != before.inventory_identity
                    or report.host_after_inventory_identity != after.inventory_identity
                    or report.run_id != claim.stages[1].run_id
                ):
                    raise ValueError("mandatory ISO03 predicate failed")
                self._iso03_bindings(report, before, after, policy, claim)
                bridge = self._model(node.artifacts[3], RawComparison)
                self._comparison(bridge, policy=policy, claim=claim, stage="ISO02", bridge=True)
            else:
                guest = self._model(node.artifacts[3], Ticket06AcceptanceResult, "result_identity")
                completion = self._model(
                    node.artifacts[4], Ticket06CompletionMarker, "marker_identity"
                )
                op = report.operational_evidence
                process = op.sandbox_process
                export = validate_ticket06_acceptance_export(
                    self._guest_export_root(claim),
                    expected=guest.static,
                )
                source, runtime = self._source_runtime(claim)
                self._static_source(guest.static, source, runtime)
                prior03 = self._owned_node("ISO03", claim)
                iso03_report = self._model(
                    prior03.artifacts[0], ISO03HostQualificationReport, "report_identity"
                )
                iso03_after = self._model(
                    prior03.artifacts[2], ProtectedHostInventory, "inventory_identity"
                )
                baseline = self._model(predecessors[0].artifacts[0], QualifiedBaseline)
                if (
                    guest.static.frozen_baseline_identity != baseline.identity
                    or guest.static.boundary_qualification_identity != iso03_report.report_identity
                    or guest.static.command_ledger_identity != policy.lineage_plan.ledger_identity
                    or guest.result_schema_identity != policy.lineage_plan.result_schema_identity
                    or guest.static.runner_input_identity
                    != iso03_report.static_bindings.acceptance_runner_structure.runner_input_identity
                    or guest.static.environment_policy_identity
                    != iso03_report.static_bindings.acceptance_runner_structure.environment_policy_identity
                    or before.protected_root_manifest_identity
                    != guest.static.protected_root_manifest_identity
                    or not compare_protected_host_inventories(iso03_after, before).equal
                ):
                    raise ValueError(
                        "ISO04 qualified source/runner/baseline/predecessor binding mismatch"
                    )
                expected_export = content_identity(
                    "windows-sandbox-ticket06-result-export:v1",
                    {
                        "result_identity": guest.result_identity,
                        "marker_identity": completion.marker_identity,
                        "result_sha256": node.artifacts[3].pin.sha256,
                        "marker_sha256": node.artifacts[4].pin.sha256,
                    },
                )
                if (
                    guest.guest_claim != "pass"
                    or process.outer_sandbox_process_returncode != 0
                    or process.timed_out
                    or not process.termination_confirmed
                    or process.before_processes
                    or process.after_processes
                    or len(process.launched_processes) < 2
                    or not any(
                        x.image_name == "WindowsSandbox.exe" for x in process.launched_processes
                    )
                    or process.run_id != claim.stages[3].run_id
                    or process.acceptance_control_identity != guest.static.control_identity
                    or op.command_failure_identity is not None
                    or op.host_before_inventory_identity != before.inventory_identity
                    or op.host_after_inventory_identity != after.inventory_identity
                    or op.guest_result_identity != guest.result_identity
                    or op.completion_marker_identity != completion.marker_identity
                    or export.result != guest
                    or export.completion != completion
                    or completion.result_sha256 != node.artifacts[3].pin.sha256
                    or export.export_identity != expected_export
                    or op.guest_export_identity != export.export_identity
                ):
                    raise ValueError("mandatory ISO04 guest/process/export predicate failed")
            comparison = self._model(node.artifacts[-1], RawComparison)
            self._comparison(comparison, policy=policy, claim=claim, stage=node.kind)
            from tradingagents.policy_coverage_models import CoveragePolicy

            if isinstance(policy, CoveragePolicy):
                from tradingagents.policy_coverage_verification import require_current_baseline

                require_current_baseline(self, policy, claim, comparison.before)
            else:
                original = self._saved(
                    policy.scope.after.artifact, "crash-protection-historical-inventory:v1"
                )
                current = self._saved(comparison.before, "crash-protection-historical-inventory:v1")
                if self._roots(original) != self._roots(current):
                    raise ValueError("new stage baseline differs from retained history")
        elif node.kind == "QCF17":
            baseline = self._model(node.artifacts[0], QualifiedBaseline)
            require_record_coverage(policy, claim, baseline)
            if (
                baseline.binding != node.binding
                or baseline.candidate != claim.candidate
                or baseline.additive_baseline_version != claim.additive_baseline_version
                or baseline.ledger_identity != policy.lineage_plan.ledger_identity
                or baseline.result_schema_identity != policy.lineage_plan.result_schema_identity
                or baseline.iso03_report != predecessors[0].artifacts[0]
                or baseline.host_inventory != predecessors[0].artifacts[2]
            ):
                raise ValueError("QCF17 baseline binding mismatch")
            iso03 = self._model(
                baseline.iso03_report, ISO03HostQualificationReport, "report_identity"
            )
            if (
                iso03.status.value != "pass"
                or baseline.host_inventory.identity != iso03.host_after_inventory_identity
            ):
                raise ValueError("QCF17 host clause B failed")
        elif node.kind == "PRE_RESULT_CLOSURE":
            self._closure(node, policy=policy, claim=claim)
        else:
            for ref in node.artifacts:
                review = self._model(ref, IndependentReviewRecord)
                require_record_coverage(policy, claim, review)
                if (
                    review.binding != node.binding
                    or review.candidate != claim.candidate
                    or review.axis != node.kind.removeprefix("REVIEW_")
                    or self.owner.review_principal(ref) != review.reviewer_principal
                ):
                    raise ValueError("mandatory independent review failed")
                if review.input_evidence != node.predecessors:
                    raise ValueError("qualification review does not bind all four executed stages")
            if node.kind == "REVIEW_SPEC":
                standards = read_model(
                    BoundEvidence,
                    self.owner.executed_stage(claim.lineage_identity, "REVIEW_STANDARDS"),
                )
                self._stage_payload(standards, policy=policy, claim=claim, _validated=validated)
                if self.owner.review_principal(
                    standards.artifacts[0]
                ) == self.owner.review_principal(node.artifacts[0]) or utc(
                    standards.recorded_at_utc
                ) > utc(node.recorded_at_utc):
                    raise ValueError(
                        "qualification Standards/Spec reviews are not independent and sequential"
                    )
        validated.add(node.identity)

    def _qualified_inputs(self, source: BaseModel, runtime: BaseModel, claim: LineageClaim) -> None:
        from tradingagents.windows_sandbox_qualification import (
            _validate_runtime_copy,
            _validate_staged_source,
        )
        from tradingagents.windows_sandbox_runner import ReviewedSourceManifest, StagedSourceInput

        root = self._allocated_root(self.owner.source_input_root(claim.lineage_identity), claim, 0)
        manifest_path = root / "source-manifest.json"
        manifest = ReviewedSourceManifest.model_validate_json(
            canonical_bytes(strict_json(manifest_path.read_bytes()))
        )
        if (
            manifest.commit_id != source.commit_id
            or manifest.tree_hash != source.tree_hash
            or manifest.source_manifest_identity != source.source_manifest_identity
            or manifest.run_live_environment != source.run_live_environment
            or len(manifest.entries) != source.tracked_file_count
        ):
            raise ValueError("retained source manifest differs from qualified source")
        _validate_staged_source(
            StagedSourceInput(
                identity=source,
                manifest=manifest,
                archive_path=root / "reviewed-source.tar",
                manifest_path=manifest_path,
                commit_object_path=root / "reviewed-commit.object",
            ),
            root,
        )
        _validate_runtime_copy(
            self._allocated_root(self.owner.runtime_bundle_root(claim.lineage_identity), claim, 0),
            source=source,
            expected_contract=runtime,
            expected_bundle_identity=runtime.bundle_manifest_identity,
        )

    def _closure(
        self, node: BoundEvidence, *, policy: AcceptancePolicy, claim: LineageClaim
    ) -> None:
        from tradingagents.windows_sandbox_qualification import (
            ProtectedHostInventory,
            compare_protected_host_inventories,
        )

        closure = self._model(node.artifacts[0], PreResultClosureEvidence)
        from tradingagents.policy_coverage_verification import require_record_coverage

        require_record_coverage(policy, claim, closure)
        kinds = ("ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEW_STANDARDS", "REVIEW_SPEC")
        earlier = tuple(
            read_model(BoundEvidence, self.owner.executed_stage(claim.lineage_identity, kind))
            for kind in kinds
        )
        review_nodes = earlier[-2:]
        review_refs = tuple(reference(x) for x in review_nodes)
        if (
            closure.binding != node.binding
            or closure.candidate != claim.candidate
            or node.predecessors != review_refs
            or closure.reviews != review_refs
            or not max(utc(x.recorded_at_utc) for x in review_nodes)
            <= utc(closure.recorded_at_utc)
            <= utc(node.recorded_at_utc)
        ):
            raise ValueError("post-review closure has wrong review/binding/order")
        if closure.post_review_host_before != earlier[3].artifacts[2]:
            raise ValueError("post-review comparison does not bridge from ISO04 after")
        before = self._model(
            closure.post_review_host_before, ProtectedHostInventory, "inventory_identity"
        )
        after = self._model(
            closure.post_review_host_after, ProtectedHostInventory, "inventory_identity"
        )
        if not compare_protected_host_inventories(before, after).equal:
            raise ValueError("post-review protected discrepancy remains fatal")
        observation = self.owner.observation(closure.post_review_host_observation)
        if not isinstance(observation, ObservationResolution) or (
            observation.observation_id != closure.post_review_host_observation
            or observation.artifact != closure.post_review_host_after
            or observation.lineage_identity != claim.lineage_identity
            or observation.lineage_plan_identity != claim.lineage_plan.lineage_plan_identity
            or observation.allocation_identity != claim.allocation_identity
            or observation.stage != "CLOSURE_HANDOFF"
            or observation.phase != "after"
            or observation.origin != "NEW_QUALIFICATION"
            or observation.measurement_contract_pin != policy.lineage_plan.measurement_contract_pin
            or not max(utc(x.recorded_at_utc) for x in review_nodes)
            <= utc(observation.observed_at_utc)
            <= utc(closure.recorded_at_utc)
        ):
            raise ValueError("post-review canonical inventory is not a new bound acquisition")
        self._comparison(
            closure.post_review_raw, policy=policy, claim=claim, stage="CLOSURE_HANDOFF"
        )
        for observation_id in (
            closure.post_review_raw.before_observation,
            closure.post_review_raw.after_observation,
        ):
            acquired = self.owner.observation(observation_id)
            if (
                not max(utc(x.recorded_at_utc) for x in review_nodes)
                <= utc(acquired.observed_at_utc)
                <= utc(closure.recorded_at_utc)
            ):
                raise ValueError("post-review raw acquisition is outside the closure window")
        from tradingagents.policy_coverage_models import CoveragePolicy

        if isinstance(policy, CoveragePolicy):
            from tradingagents.policy_coverage_verification import require_current_baseline

            require_current_baseline(self, policy, claim, closure.post_review_raw.before)
        else:
            original = self._saved(
                policy.scope.after.artifact, "crash-protection-historical-inventory:v1"
            )
            current = self._saved(
                closure.post_review_raw.before, "crash-protection-historical-inventory:v1"
            )
            if self._roots(original) != self._roots(current):
                raise ValueError("post-review raw bridge has an additional discrepancy")
        inventory = self._model(closure.evidence_inventory, PreResultEvidenceInventory)
        roots = (
            EvidenceInventoryRoot(role="EVIDENCE", path=claim.pre_result_evidence_root),
            *(
                EvidenceInventoryRoot(role=stage.stage, path=stage.root)
                for stage in claim.stages[:5]
            ),
        )
        if (
            inventory.binding != node.binding
            or inventory.candidate != claim.candidate
            or inventory.roots != roots
            or not max(utc(x.recorded_at_utc) for x in review_nodes)
            <= utc(inventory.captured_at_utc)
            <= utc(closure.recorded_at_utc)
            or inventory.entries != _inventory_entries(roots)
        ):
            raise ValueError("post-review complete evidence inventory mismatch")
        required = [reference(x) for x in earlier]
        required.extend(ref for x in earlier for ref in x.artifacts)
        required.extend(
            (
                reference(policy, "acceptance_policy_identity"),
                reference(claim, "lineage_identity"),
                policy.scope.original_blocker,
                policy.scope.before.artifact,
                policy.scope.after.artifact,
                policy.scope.complete_delta_ledger,
                closure.post_review_host_before,
                closure.post_review_host_after,
                reference(closure.post_review_raw),
                closure.post_review_raw.before,
                closure.post_review_raw.after,
            )
        )
        pins = {entry.pin.sha256: entry.pin for entry in inventory.entries if entry.pin is not None}
        from tradingagents.policy_coverage_models import CoverageClaim

        if isinstance(claim, CoverageClaim):
            required.extend(
                (
                    claim.coverage.scope,
                    claim.coverage.measurement,
                    claim.coverage.baseline,
                    claim.coverage.assessment,
                )
            )
        if any(pins.get(ref.pin.sha256) != ref.pin for ref in required):
            raise ValueError("pre-result evidence inventory omits required predecessor evidence")
        if any(
            ref.identity in {inventory.identity, closure.identity, node.identity}
            for ref in required
        ):
            raise ValueError("pre-result evidence inventory cannot include itself or successors")

    def _execution(
        self,
        execution: ExecutionEvidence,
        *,
        policy: AcceptancePolicy,
        approval: OperatorApproval,
        claim: LineageClaim,
        terminal_at: str,
    ) -> None:
        from tradingagents.windows_sandbox_qualification import ISO04Ticket06AcceptanceReport

        execution = read_model(ExecutionEvidence, execution)
        from tradingagents.policy_coverage_verification import require_record_coverage

        require_record_coverage(policy, claim, execution)
        self._graph(
            execution, policy=policy, approval=approval, claim=claim, terminal_at=terminal_at
        )
        validated: set[str] = set()
        for node in execution.graph:
            self._stage_payload(node, policy=policy, claim=claim, _validated=validated)
        report = self._model(
            execution.iso04_report, ISO04Ticket06AcceptanceReport, "report_identity"
        )
        if execution.guest_export_identity != report.operational_evidence.guest_export_identity:
            raise ValueError("terminal execution lost its canonical guest export binding")

    def _graph(
        self,
        execution: ExecutionEvidence,
        *,
        policy: AcceptancePolicy,
        approval: OperatorApproval,
        claim: LineageClaim,
        terminal_at: str,
    ) -> None:
        nodes = execution.graph
        refs = tuple(reference(n) for n in nodes)
        kinds = (
            "ISO02",
            "ISO03",
            "QCF17",
            "ISO04_TICKET06",
            "REVIEW_STANDARDS",
            "REVIEW_SPEC",
            "PRE_RESULT_CLOSURE",
        )
        predecessors = ((), (refs[0],), (refs[1],), (refs[2],), refs[:4], refs[:4], refs[4:6])
        artifacts = (
            (execution.source, execution.runtime),
            (
                execution.iso03_report,
                execution.iso03_before,
                execution.iso03_after,
                reference(execution.new_start_bridge),
                reference(execution.current_raw_comparisons[0]),
            ),
            (execution.baseline,),
            (
                execution.iso04_report,
                execution.iso04_before,
                execution.iso04_after,
                execution.guest_result,
                execution.guest_completion,
                reference(execution.current_raw_comparisons[1]),
            ),
        )
        binding = acceptance_binding(policy, approval, claim)
        for i, node in enumerate(nodes):
            if (
                node.kind != kinds[i]
                or node.predecessors != predecessors[i]
                or node.binding != binding
                or node.candidate != claim.candidate
                or not utc(claim.claimed_at_utc) <= utc(node.recorded_at_utc) <= utc(terminal_at)
                or (i < 4 and node.artifacts != artifacts[i])
            ):
                raise ValueError(
                    "incomplete, cyclic, reordered or misbound execution/review/closure graph"
                )
            self._registered_stage(node, claim)
            for predecessor in node.predecessors:
                previous = nodes[refs.index(predecessor)]
                if utc(previous.recorded_at_utc) > utc(node.recorded_at_utc):
                    raise ValueError("pre-result evidence predates a predecessor")
        principals = []
        for node, axis in zip(nodes[4:6], ("STANDARDS", "SPEC"), strict=True):
            if len(node.artifacts) != 1:
                raise ValueError("review record missing")
            review_ref = node.artifacts[0]
            review = self._model(review_ref, IndependentReviewRecord)
            from tradingagents.policy_coverage_verification import require_record_coverage

            require_record_coverage(policy, claim, review)
            principal = self.owner.review_principal(review_ref)
            if (
                review.axis != axis
                or review.binding != binding
                or review.candidate != claim.candidate
                or review.input_evidence != refs[:4]
                or not principal
                or review.reviewer_principal != principal
            ):
                raise ValueError("independent review authentication/input binding mismatch")
            principals.append(principal)
        if principals[0] == principals[1] or nodes[6].artifacts != (execution.pre_result_closure,):
            raise ValueError("two independent reviews and pre-result closure are required")
        self._closure(nodes[6], policy=policy, claim=claim)
        completed = [
            event for event in self.ledger.events(claim) if event["kind"].startswith("COMPLETE:")
        ]
        expected_refs = (refs[0], refs[1], refs[2], refs[3], refs[5], refs[6])
        if tuple(EvidenceRef.read(e["payload"]) for e in completed) != expected_refs:
            raise ValueError("execution evidence is not the consumed stage ledger")

    @coverage_operation
    def finalize(
        self, *, policy: object, approval: object, claim: object, execution: object
    ) -> PolicyQualifiedExecutionResult:
        policy, approval, claim = (
            read_model(AcceptancePolicy, policy),
            read_model(OperatorApproval, approval),
            read_model(LineageClaim, claim),
        )
        try:
            resolved = self.eligibility(policy, approval)
            self._claim_binding(policy, approval, claim)
            self.ledger.require_active(claim)
            self._budget_fit(policy, approval, resolved, len(policy.lineage_plan.stage_slots))
            validation_started = resolved.now_utc
            execution = read_model(ExecutionEvidence, execution)
            self._execution(
                execution,
                policy=policy,
                approval=approval,
                claim=claim,
                terminal_at=resolved.now_utc,
            )
            resolved = self._authority(policy, approval)
            self._claim_binding(policy, approval, claim)
            self._budget_fit(policy, approval, resolved, len(policy.lineage_plan.stage_slots))
            if (
                not timedelta(0)
                <= utc(resolved.now_utc) - utc(validation_started)
                <= timedelta(seconds=policy.lineage_plan.finalization_maximum_seconds)
            ):
                raise ValueError(
                    "terminal verification exceeded its canonical budget or clock regressed"
                )
            from tradingagents.policy_coverage_models import (
                CoverageHistory,
                CoveragePolicy,
                CoverageResult,
            )

            coverage_route = isinstance(policy, CoveragePolicy)
            result_model = CoverageResult if coverage_route else PolicyQualifiedExecutionResult
            history_model = CoverageHistory if coverage_route else HistoricalClaim
            extra_history = (
                {
                    "contract_version": "policy-coverage-historical-claim-v2",
                    "coverage": claim.coverage,
                    "known_recorded_fields": "MATCH",
                    "current_window": "STRICT_COMPLETE_PRESERVATION_WITHIN_DECLARED_SCOPE",
                }
                if coverage_route
                else {}
            )
            result = result_model.seal(
                contract_version=(
                    "policy-qualified-execution-result-v2"
                    if coverage_route
                    else "policy-qualified-execution-result-v1"
                ),
                status="CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY",
                candidate=claim.candidate,
                binding=acceptance_binding(policy, approval, claim),
                policy=policy,
                approval=approval,
                lineage=claim,
                history=history_model(
                    **extra_history,
                    historical_claim="UNRESOLVED_RAW_DIFFERENCE",
                    historical_equal=False,
                    historical_cause="UNRESOLVED",
                    prior_outcome="BLOCKED",
                    scope=policy.scope,
                ),
                execution=execution,
                terminal_at_utc=resolved.now_utc,
                execution_authorized=False,
            )
            self.ledger.transition(
                claim,
                kind="TERMINAL",
                at=resolved.now_utc,
                evidence=reference(result, "result_identity"),
            )
            return result
        except (ValueError, OSError):
            self._fail_existing(claim)
            raise

    @coverage_operation
    def verify_result(
        self, raw: bytes
    ) -> tuple[PolicyQualifiedExecutionResult, ResultBindingCore, AuthorityResolution]:
        result = read_model(PolicyQualifiedExecutionResult, raw)
        policy, approval, claim = result.policy, result.approval, result.lineage
        resolved = self.eligibility(policy, approval)
        self._claim_binding(policy, approval, claim)
        self._budget_fit(policy, approval, resolved, len(policy.lineage_plan.stage_slots))
        validation_started = resolved.now_utc
        binding = acceptance_binding(policy, approval, claim)
        if (
            result.binding != binding
            or result.candidate != claim.candidate
            or result.history.scope != policy.scope
            or not utc(claim.claimed_at_utc) <= utc(result.terminal_at_utc) <= utc(resolved.now_utc)
            or utc(result.terminal_at_utc) >= utc(approval.expires_utc)
        ):
            raise ValueError("terminal result lost its exact acceptance/history/clock binding")
        ref = EvidenceRef(identity=result.result_identity, pin=BytePin.of(raw))
        self.ledger.require_terminal(claim, ref)
        self._execution(
            result.execution,
            policy=policy,
            approval=approval,
            claim=claim,
            terminal_at=result.terminal_at_utc,
        )
        from tradingagents.policy_coverage_models import CoverageCore, CoverageResult

        coverage_route = isinstance(result, CoverageResult)
        core_model = CoverageCore if coverage_route else ResultBindingCore
        core = core_model(
            contract_version=(
                "policy-qualified-result-binding-core-v2"
                if coverage_route
                else "policy-qualified-result-binding-core-v1"
            ),
            result=ref,
            binding=binding,
            candidate=claim.candidate,
            history=result.history,
            lineage=claim,
            expires_utc=approval.expires_utc,
            execution_authorized=False,
        )
        resolved = self._authority(policy, approval)
        self._claim_binding(policy, approval, claim)
        if (
            not timedelta(0)
            <= utc(resolved.now_utc) - utc(validation_started)
            <= timedelta(seconds=policy.lineage_plan.finalization_maximum_seconds)
        ):
            raise ValueError("result verification exceeded its canonical budget or clock regressed")
        return result, core, resolved

    def issue_receipt(self, result_bytes: bytes) -> PostResultAcceptanceReceipt:
        result, core, resolved = self.verify_result(result_bytes)
        resolved = self._authority(result.policy, result.approval)
        self._budget_fit(
            result.policy, result.approval, resolved, len(result.policy.lineage_plan.stage_slots)
        )
        from tradingagents.policy_coverage_models import CoverageReceipt, CoverageResult

        coverage_route = isinstance(result, CoverageResult)
        receipt_model = CoverageReceipt if coverage_route else PostResultAcceptanceReceipt
        receipt = receipt_model.seal(
            contract_version=(
                "policy-qualified-acceptance-receipt-v2"
                if coverage_route
                else "policy-qualified-acceptance-receipt-v1"
            ),
            core=core,
            pre_result_closure_identity=result.execution.graph[-1].identity,
            authority_state_reference=resolved.state_reference,
            recorded_at_utc=resolved.now_utc,
            execution_authorized=False,
        )
        self.ledger.record_receipt(
            result.lineage, core.result, reference(receipt, "receipt_identity"), at=resolved.now_utc
        )
        return receipt

    def verify_downstream(self, value: object) -> DownstreamEnvelope:
        envelope = read_model(DownstreamEnvelope, value)
        result, core, resolved = self.verify_result(self._bytes(envelope.core.result))
        receipt = envelope.receipt
        self.ledger.require_receipt(result.lineage, reference(receipt, "receipt_identity"))
        if (
            envelope.core != core
            or receipt.core != core
            or receipt.pre_result_closure_identity != result.execution.graph[-1].identity
            or not utc(result.terminal_at_utc)
            <= utc(receipt.recorded_at_utc)
            <= utc(resolved.now_utc)
            or utc(receipt.recorded_at_utc) >= utc(result.approval.expires_utc)
        ):
            raise ValueError("post-result receipt/core/closure is missing or misbound")
        # The recorded state reference is evidence of that receipt's check. Fresh
        # resolve above governs use; a later revocation never rewrites the receipt.
        self._authority(result.policy, result.approval)
        self.ledger.require_receipt(result.lineage, reference(receipt, "receipt_identity"))
        return envelope

    def current_source(self, envelope: object) -> tuple[Candidate, str]:
        verified = self.verify_downstream(envelope)
        result = read_model(PolicyQualifiedExecutionResult, self._bytes(verified.core.result))
        resolved = self._authority(result.policy, result.approval)
        return Candidate.read(
            resolved.current_candidate
        ), resolved.current_production_inventory_identity

    def report(self, envelope: object) -> dict:
        """Report consumer: retain the complete caveat and never announce READY."""
        verified = self.verify_downstream(envelope)
        return {
            "status": "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY",
            "acceptance": verified.model_dump(mode="json"),
            "execution_authorized": False,
        }
