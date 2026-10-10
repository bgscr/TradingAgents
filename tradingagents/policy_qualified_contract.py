"""Closed prospective policy-qualified evidence, with no operational authority.

Parsing is not approval. The owner-backed verifier in ``policy_qualified_results``
is required at every use. Legacy qualification identities are deliberately absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

CONTRACT_VERSION = "policy-qualified-execution-result-v1"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitID = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Identity = Annotated[
    str, Field(pattern=r"^[a-z0-9][a-z0-9._:-]*:v[1-9][0-9]*:sha256:[0-9a-f]{64}$")
]
Text = Annotated[str, Field(min_length=1)]
STAGES = ("ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEWS", "CLOSURE_HANDOFF")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_identity(namespace: str, value: object) -> str:
    return namespace + ":sha256:" + sha256(canonical_bytes(value)).hexdigest()


def strict_json(raw: bytes | str) -> Any:
    """Reject duplicate keys and nonfinite numbers before any model sees input."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value: str) -> None:
        raise ValueError("nonfinite JSON number: " + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("an explicit UTC time is required")
    return parsed.astimezone(timezone.utc)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @classmethod
    def read(cls, value: object) -> Self:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, (bytes, str)):
            value = strict_json(value)
        # JSON-mode allows the JSON representation of tuples, but not coercion of
        # strings into integers/booleans. Revalidate even model_copy/construct values.
        return cls.model_validate_json(canonical_bytes(value))

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json")) + b"\n"


class Sealed(Closed):
    namespace: ClassVar[str]
    identity_field: ClassVar[str] = "identity"

    @model_validator(mode="after")
    def _seal(self) -> Self:
        payload = self.model_dump(mode="json", exclude={self.identity_field})
        if getattr(self, self.identity_field) != content_identity(self.namespace, payload):
            raise ValueError(self.namespace + " content identity mismatch")
        return self

    @classmethod
    def seal(cls, **payload: Any) -> Self:
        if cls.identity_field in payload:
            raise ValueError("sealing cannot overwrite an existing identity")

        # Normalize nested models without constructing an unvalidated model.
        def plain(item: Any) -> Any:
            if isinstance(item, BaseModel):
                return item.model_dump(mode="json")
            if isinstance(item, dict):
                return {k: plain(v) for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [plain(v) for v in item]
            return item

        payload = plain(payload)
        return cls.read({**payload, cls.identity_field: content_identity(cls.namespace, payload)})


class BytePin(Closed):
    byte_length: int = Field(ge=0)
    sha256: Digest

    @classmethod
    def of(cls, raw: bytes) -> Self:
        return cls(byte_length=len(raw), sha256=sha256(raw).hexdigest())

    def check(self, raw: bytes) -> None:
        if self != BytePin.of(raw):
            raise ValueError("whole-file byte pin mismatch")


class EvidenceRef(Closed):
    identity: Identity
    pin: BytePin


class Candidate(Closed):
    commit: GitID
    tree: GitID
    parent: GitID


class OriginalAcquisition(Closed):
    """Owner registry key plus original path/phase; bytes cannot prove acquisition."""

    observation_id: Text
    original_path: Text
    phase: Literal["before", "after"]
    origin: Literal["RETAINED_ORIGINAL_FILES"]
    artifact: EvidenceRef
    collector_pin: BytePin
    procedure_pin: BytePin
    collection_elapsed_seconds: float = Field(gt=0, allow_inf_nan=False)
    collection_utc: None
    sample_utc: None
    transition_utc: None


class HistoricalExceptionScope(Sealed):
    namespace = "historical-state-exception:v1"
    identity_field = "exception_identity"
    contract_version: Literal["historical-state-exception-v1"]
    candidate: Candidate
    original_candidate: Candidate
    original_blocker: EvidenceRef
    before: OriginalAcquisition
    after: OriginalAcquisition
    root: Text
    before_root_identity: Identity
    after_root_identity: Identity
    complete_delta_ledger: EvidenceRef
    changed_rows: int = Field(gt=0)
    changed_field: Literal["attributes"]
    raw_before: int = Field(ge=0, le=0xFFFFFFFF)
    raw_after: int = Field(ge=0, le=0xFFFFFFFF)
    historical_equal: Literal[False]
    historical_cause: Literal["UNRESOLVED"]
    prior_outcome: Literal["BLOCKED"]
    exception_identity: Identity

    @model_validator(mode="after")
    def _pair(self) -> Self:
        if (self.before.phase, self.after.phase) != ("before", "after") or (
            self.before.observation_id == self.after.observation_id
            or self.raw_before == self.raw_after
        ):
            raise ValueError("historical pair is incomplete or has no raw difference")
        return self


class StageBudget(Closed):
    stage: Text
    maximum_execution_seconds: int = Field(gt=0, le=30 * 86400)
    maximum_cleanup_seconds: int = Field(ge=0, le=30 * 86400)


class LineagePlan(Sealed):
    namespace = "historical-state-lineage-plan:v1"
    identity_field = "lineage_plan_identity"
    contract_version: Literal["historical-state-lineage-plan-v1"]
    candidate: Candidate
    stage_slots: tuple[str, ...]
    stage_budgets: tuple[StageBudget, ...]
    finalization_maximum_seconds: int = Field(gt=0, le=30 * 86400)
    maximum_lineages: Literal[1]
    maximum_claims: Literal[1]
    claim_window_days: Literal[7]
    terminal_window_days: Literal[30]
    source_change_transfer_permitted: Literal[False]
    ledger_identity: Identity
    result_schema_identity: Identity
    procedure_pin: BytePin
    coordinator_pin: BytePin
    consumer_contract_pin: BytePin
    measurement_contract_pin: BytePin
    pre_result_inventory_scope: Literal["complete-evidence-and-pre-result-stage-roots-v1"]
    lineage_plan_identity: Identity

    @model_validator(mode="after")
    def _stages(self) -> Self:
        if self.stage_slots != STAGES or tuple(x.stage for x in self.stage_budgets) != STAGES:
            raise ValueError("the complete ordered one-lineage stage plan is required")
        return self


class AcceptancePolicy(Sealed):
    namespace = "historical-state-acceptance-policy:v1"
    identity_field = "acceptance_policy_identity"
    contract_version: Literal["historical-state-acceptance-policy-v1"]
    scope: HistoricalExceptionScope
    exception_identity: Identity
    lineage_plan: LineagePlan
    predecessor_proposal_pin: BytePin
    scope_evidence_manifest_pin: BytePin
    authority_source_contract_pin: BytePin
    claim_window_days: Literal[7]
    terminal_window_days: Literal[30]
    maximum_claims: Literal[1]
    revocation_is_terminal: Literal[True]
    failure_consumes_attempt: Literal[True]
    new_discrepancy_is_terminal: Literal[True]
    mandatory_predicate_contract: Literal[
        "canonical-ISO02-ISO03-QCF17-A-and-B-before-QCF18-all-27-commands-zero-current-delta-v1"
    ]
    acceptance_policy_identity: Identity

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if self.exception_identity != self.scope.exception_identity or (
            self.scope.candidate != self.lineage_plan.candidate
        ):
            raise ValueError("policy scope/plan binding mismatch")
        return self


class OperatorApproval(Sealed):
    namespace = "historical-state-operator-approval:v1"
    identity_field = "approval_identity"
    contract_version: Literal["historical-state-operator-approval-v1"]
    acceptance_policy_identity: Identity
    exception_identity: Identity
    lineage_plan_identity: Identity
    candidate: Candidate
    scope_evidence_manifest_pin: BytePin
    reviewed_package_manifest_pin: BytePin
    issuer_principal: Text
    issuer_authority_pin: BytePin
    decision_artifact_pin: BytePin
    channel_session_reference: Text
    selection: Literal["APPROVE"]
    effective_utc: Text
    claim_by_utc: Text
    expires_utc: Text
    approval_identity: Identity

    @model_validator(mode="after")
    def _periods(self) -> Self:
        effective, claim, expires = map(
            utc, (self.effective_utc, self.claim_by_utc, self.expires_utc)
        )
        if claim != effective + timedelta(days=7) or expires != effective + timedelta(days=30):
            raise ValueError("approval requires exact seven-day claim/thirty-day terminal periods")
        return self


class ReviewedPolicyPackage(Sealed):
    namespace = "reviewed-policy-contract-package:v1"
    contract_version: Literal["reviewed-policy-contract-package-v1"]
    policy: EvidenceRef
    scope_evidence_manifest_pin: BytePin
    contract_source_pin: BytePin
    identity: Identity


class StageAllocation(Closed):
    stage: Text
    run_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")]
    root: Text


class LineageAllocation(Sealed):
    """Canonical names/roots selected before the exclusive, freshly dated claim."""

    namespace = "historical-state-lineage-allocation:v1"
    identity_field = "allocation_identity"
    contract_version: Literal["historical-state-lineage-allocation-v1"]
    approval_identity: Identity
    acceptance_policy_identity: Identity
    exception_identity: Identity
    lineage_plan: LineagePlan
    candidate: Candidate
    stages: tuple[StageAllocation, ...]
    additive_baseline_version: Text
    pre_result_evidence_root: Text
    allocation_identity: Identity

    @model_validator(mode="after")
    def _allocation(self) -> Self:
        from tradingagents.runtime_protection import _windows_path

        if (
            self.candidate != self.lineage_plan.candidate
            or tuple(x.stage for x in self.stages) != STAGES
        ):
            raise ValueError("lineage plan/stage allocation mismatch")
        if len({x.run_id for x in self.stages}) != len(STAGES):
            raise ValueError("stage IDs must be distinct")
        roots = [_windows_path(x.root).casefold() for x in self.stages]
        roots.append(_windows_path(self.pre_result_evidence_root).casefold())
        if any(
            a == b or a.startswith(b + "\\") or b.startswith(a + "\\")
            for i, a in enumerate(roots)
            for b in roots[i + 1 :]
        ):
            raise ValueError("stage roots overlap")
        return self


class LineageClaim(LineageAllocation):
    namespace = "historical-state-qualification-lineage:v1"
    identity_field = "lineage_identity"
    contract_version: Literal["historical-state-qualification-lineage-v1"]
    claimed_at_utc: Text
    state: Literal["CLAIMED_ATTEMPT_CONSUMED"]
    attempt_ordinal: Literal[1]
    lineage_identity: Identity

    def to_allocation(self) -> LineageAllocation:
        payload = self.model_dump(
            mode="json", exclude={"claimed_at_utc", "state", "attempt_ordinal", "lineage_identity"}
        )
        payload["contract_version"] = "historical-state-lineage-allocation-v1"
        return LineageAllocation.read(payload)

    @model_validator(mode="after")
    def _claimed(self) -> Self:
        utc(self.claimed_at_utc)
        self.to_allocation()
        return self


class AcceptanceBinding(Closed):
    acceptance_policy_identity: Identity
    exception_identity: Identity
    approval_identity: Identity
    lineage_plan_identity: Identity
    lineage_identity: Identity
    original_blocker_identity: Identity
    scope_evidence_manifest_pin: BytePin


class HistoricalClaim(Closed):
    historical_claim: Literal["UNRESOLVED_RAW_DIFFERENCE"]
    historical_equal: Literal[False]
    historical_cause: Literal["UNRESOLVED"]
    prior_outcome: Literal["BLOCKED"]
    scope: HistoricalExceptionScope


class BoundEvidence(Sealed):
    """Predecessor-only execution/review/closure record, never a terminal result."""

    namespace = "policy-qualified-pre-result-evidence:v1"
    contract_version: Literal["policy-qualified-pre-result-evidence-v1"]
    kind: Literal[
        "ISO02",
        "ISO03",
        "QCF17",
        "ISO04_TICKET06",
        "REVIEW_STANDARDS",
        "REVIEW_SPEC",
        "PRE_RESULT_CLOSURE",
    ]
    binding: AcceptanceBinding
    candidate: Candidate
    predecessors: tuple[EvidenceRef, ...]
    artifacts: tuple[EvidenceRef, ...] = Field(min_length=1)
    recorded_at_utc: Text
    identity: Identity

    @field_validator("recorded_at_utc")
    @classmethod
    def _time(cls, value: str) -> str:
        utc(value)
        return value


class RawComparison(Sealed):
    namespace = "policy-qualified-raw-comparison:v1"
    contract_version: Literal["policy-qualified-raw-comparison-v1"]
    measurement_contract_pin: BytePin
    before_observation: Text
    after_observation: Text
    before: EvidenceRef
    after: EvidenceRef
    comparable: Literal[True]
    equal: Literal[True]
    identity: Identity


class ExecutionEvidence(Closed):
    """References resolve to original canonical models/bytes, not PASS summaries."""

    source: EvidenceRef
    runtime: EvidenceRef
    iso03_report: EvidenceRef
    iso03_before: EvidenceRef
    iso03_after: EvidenceRef
    baseline: EvidenceRef
    iso04_report: EvidenceRef
    iso04_before: EvidenceRef
    iso04_after: EvidenceRef
    guest_result: EvidenceRef
    guest_completion: EvidenceRef
    guest_export_identity: Identity
    new_start_bridge: RawComparison
    current_raw_comparisons: tuple[RawComparison, ...] = Field(min_length=2, max_length=2)
    pre_result_closure: EvidenceRef
    graph: tuple[BoundEvidence, ...] = Field(min_length=7, max_length=7)


class QualifiedBaseline(Sealed):
    namespace = "policy-qualified-qcf17-baseline:v1"
    contract_version: Literal["policy-qualified-qcf17-baseline-v1"]
    binding: AcceptanceBinding
    candidate: Candidate
    additive_baseline_version: Text
    iso03_report: EvidenceRef
    host_inventory: EvidenceRef
    ledger_identity: Identity
    result_schema_identity: Identity
    identity: Identity


class IndependentReviewRecord(Sealed):
    namespace = "policy-qualified-independent-review:v1"
    contract_version: Literal["policy-qualified-independent-review-v1"]
    binding: AcceptanceBinding
    candidate: Candidate
    axis: Literal["STANDARDS", "SPEC"]
    input_evidence: tuple[EvidenceRef, ...] = Field(min_length=4, max_length=4)
    reviewer_principal: Text
    qualification_critical: Literal[0]
    qualification_important: Literal[0]
    historical_important_unresolved: int = Field(ge=1)
    identity: Identity


class EvidenceInventoryRoot(Closed):
    role: Literal["EVIDENCE", "ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEWS"]
    path: Text


class EvidenceInventoryEntry(Closed):
    root_role: Literal["EVIDENCE", "ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEWS"]
    relative_path: Text
    kind: Literal["file", "directory"]
    pin: BytePin | None

    @model_validator(mode="after")
    def _entry(self) -> Self:
        from tradingagents.runtime_protection import _relative

        _relative(self.relative_path)
        if (self.kind == "file") != (self.pin is not None):
            raise ValueError("evidence inventory entry pin/kind mismatch")
        return self


class PreResultEvidenceInventory(Sealed):
    namespace = "policy-qualified-pre-result-inventory:v1"
    contract_version: Literal["policy-qualified-pre-result-inventory-v1"]
    binding: AcceptanceBinding
    candidate: Candidate
    roots: tuple[EvidenceInventoryRoot, ...] = Field(min_length=6, max_length=6)
    entries: tuple[EvidenceInventoryEntry, ...] = Field(min_length=1)
    captured_at_utc: Text
    identity: Identity

    @model_validator(mode="after")
    def _inventory(self) -> Self:
        from tradingagents.runtime_protection import _windows_path

        utc(self.captured_at_utc)
        roles = ("EVIDENCE", "ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEWS")
        if tuple(x.role for x in self.roots) != roles:
            raise ValueError("complete ordered pre-result inventory scope is required")
        paths = [_windows_path(x.path).casefold() for x in self.roots]
        if any(
            a == b or a.startswith(b + "\\") or b.startswith(a + "\\")
            for i, a in enumerate(paths)
            for b in paths[i + 1 :]
        ):
            raise ValueError("pre-result evidence inventory roots overlap")
        keys = [(roles.index(x.root_role), x.relative_path.casefold()) for x in self.entries]
        if keys != sorted(set(keys)):
            raise ValueError("pre-result evidence inventory entries are duplicated or unordered")
        return self


class PreResultClosureEvidence(Sealed):
    namespace = "policy-qualified-pre-result-closure:v1"
    contract_version: Literal["policy-qualified-pre-result-closure-v1"]
    binding: AcceptanceBinding
    candidate: Candidate
    reviews: tuple[EvidenceRef, ...] = Field(min_length=2, max_length=2)
    post_review_host_before: EvidenceRef
    post_review_host_after: EvidenceRef
    post_review_host_observation: Text
    post_review_raw: RawComparison
    evidence_inventory: EvidenceRef
    recorded_at_utc: Text
    identity: Identity


class PolicyQualifiedExecutionResult(Sealed):
    namespace = "policy-qualified-execution-result:v1"
    identity_field = "result_identity"
    contract_version: Literal["policy-qualified-execution-result-v1"]
    status: Literal["CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY"]
    candidate: Candidate
    binding: AcceptanceBinding
    policy: AcceptancePolicy
    approval: OperatorApproval
    lineage: LineageClaim
    history: HistoricalClaim
    execution: ExecutionEvidence
    terminal_at_utc: Text
    execution_authorized: Literal[False]
    result_identity: Identity


class ResultBindingCore(Closed):
    contract_version: Literal["policy-qualified-result-binding-core-v1"]
    result: EvidenceRef
    binding: AcceptanceBinding
    candidate: Candidate
    history: HistoricalClaim
    lineage: LineageClaim
    expires_utc: Text
    execution_authorized: Literal[False]


class PostResultAcceptanceReceipt(Sealed):
    namespace = "policy-qualified-acceptance-receipt:v1"
    identity_field = "receipt_identity"
    contract_version: Literal["policy-qualified-acceptance-receipt-v1"]
    core: ResultBindingCore
    pre_result_closure_identity: Identity
    authority_state_reference: Text
    recorded_at_utc: Text
    execution_authorized: Literal[False]
    receipt_identity: Identity


class DownstreamEnvelope(Closed):
    contract_version: Literal["policy-qualified-downstream-envelope-v1"]
    core: ResultBindingCore
    receipt: PostResultAcceptanceReceipt
    execution_authorized: Literal[False]

    @model_validator(mode="after")
    def _core(self) -> Self:
        if self.core != self.receipt.core:
            raise ValueError("receipt lost or changed the result binding core")
        return self


def contract_descriptor() -> dict[str, Any]:
    """Deterministic schema identity; no candidate/approval/clock or self-hash."""
    from tradingagents.qualified_routing_composition import (
        ConditionalProductionQualifiedRoutingPreparation,
        ConditionalProductionQualifiedRoutingSourceBinding,
    )
    from tradingagents.runtime_protection import (
        ConditionalAttemptInputs,
        ConditionalProtectionAttempt,
    )

    models = (
        AcceptancePolicy,
        ReviewedPolicyPackage,
        OperatorApproval,
        LineageAllocation,
        LineageClaim,
        PolicyQualifiedExecutionResult,
        PostResultAcceptanceReceipt,
        DownstreamEnvelope,
        QualifiedBaseline,
        IndependentReviewRecord,
        PreResultClosureEvidence,
        PreResultEvidenceInventory,
        ConditionalProductionQualifiedRoutingSourceBinding,
        ConditionalProductionQualifiedRoutingPreparation,
        ConditionalAttemptInputs,
        ConditionalProtectionAttempt,
    )
    definitions: dict[str, Any] = {}
    entries = {}
    for model in models:
        schema = model.model_json_schema()
        nested = schema.pop("$defs", {})
        for name, definition in {**nested, model.__name__: schema}.items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("ambiguous contract schema definition")
            definitions[name] = definition
        entries[model.__name__] = {"$ref": "#/$defs/" + model.__name__}
    payload = {
        "contract_version": CONTRACT_VERSION,
        "entry_points": entries,
        "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": definitions},
        "validation_required": "authenticated-owner-and-durable-lineage-plus-canonical-evidence-v1",
        "execution_authorized": False,
    }
    return {
        **payload,
        "contract_identity": content_identity("policy-qualified-result-contract:v1", payload),
    }
