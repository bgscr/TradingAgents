"""Explicit v2 opt-in models. Existing v1 model readers remain closed and unchanged."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self

from tradingagents import policy_qualified_contract as v1
from tradingagents.policy_coverage_contract import CoverageMeasurement, artifact_ref
from tradingagents.policy_owner_contract import PolicyDecisionProposal


class CoverageBinding(v1.Closed):
    scope: v1.EvidenceRef
    measurement: v1.EvidenceRef
    baseline: v1.EvidenceRef
    assessment: v1.EvidenceRef
    historical_coverage: Literal["UNKNOWN"]
    historical_object_continuity: Literal["NOT_ESTABLISHED"]
    hidden_contents: Literal["UNKNOWN_NOT_RETROACTIVELY_COMPLETED"]


class CoveragePolicy(v1.AcceptancePolicy):
    namespace = "historical-state-acceptance-policy:v2"
    contract_version: Literal["historical-state-acceptance-policy-v2"]
    coverage_scope: v1.EvidenceRef
    coverage_measurement: CoverageMeasurement
    coverage_rule: Literal["KNOWN_RECORDED_FIELDS_UNKNOWN_HISTORY_STRICT_CURRENT_WINDOW"]

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        if (
            self.coverage_measurement.scope != self.coverage_scope
            or self.coverage_measurement.candidate != self.scope.candidate
            or self.lineage_plan.measurement_contract_pin
            != v1.BytePin.of(self.coverage_measurement.to_bytes())
        ):
            raise ValueError("versioned coverage policy/measurement/scope/candidate mismatch")
        return self


class CoverageApproval(v1.OperatorApproval):
    namespace = "historical-state-operator-approval:v2"
    contract_version: Literal["historical-state-operator-approval-v2"]
    coverage_scope: v1.EvidenceRef
    coverage_measurement: v1.EvidenceRef
    historical_coverage: Literal["UNKNOWN"]


class CoverageClaim(v1.LineageClaim):
    namespace = "historical-state-qualification-lineage:v2"
    contract_version: Literal["historical-state-qualification-lineage-v2"]
    coverage: CoverageBinding

    def to_allocation(self) -> v1.LineageAllocation:
        payload = self.model_dump(
            mode="json",
            exclude={"claimed_at_utc", "state", "attempt_ordinal", "lineage_identity", "coverage"},
        )
        payload["contract_version"] = "historical-state-lineage-allocation-v1"
        return v1.LineageAllocation.read(payload)


class CoverageRawComparison(v1.RawComparison):
    namespace = "policy-qualified-raw-comparison:v2"
    contract_version: Literal["policy-qualified-raw-comparison-v2"]
    coverage: CoverageBinding
    relation: Literal["KNOWN_RECORDED_FIELDS", "CURRENT_WINDOW"]
    equal: bool
    historical_equal: Literal[False]

    @model_validator(mode="after")
    def _meaning(self) -> Self:
        if self.equal is not (self.relation == "CURRENT_WINDOW"):
            raise ValueError("known-field comparison cannot assert full historical equality")
        if self.measurement_contract_pin != self.coverage.measurement.pin:
            raise ValueError("comparison measurement differs from coverage binding")
        return self


class CoverageBoundEvidence(v1.BoundEvidence):
    namespace = "policy-qualified-pre-result-evidence:v2"
    contract_version: Literal["policy-qualified-pre-result-evidence-v2"]
    coverage: CoverageBinding


class CoverageBaseline(v1.QualifiedBaseline):
    namespace = "policy-qualified-qcf17-baseline:v2"
    contract_version: Literal["policy-qualified-qcf17-baseline-v2"]
    coverage: CoverageBinding


class CoverageReview(v1.IndependentReviewRecord):
    namespace = "policy-qualified-independent-review:v2"
    contract_version: Literal["policy-qualified-independent-review-v2"]
    coverage: CoverageBinding


class CoverageClosure(v1.PreResultClosureEvidence):
    namespace = "policy-qualified-pre-result-closure:v2"
    contract_version: Literal["policy-qualified-pre-result-closure-v2"]
    post_review_raw: CoverageRawComparison
    coverage: CoverageBinding


class CoverageExecution(v1.ExecutionEvidence):
    contract_version: Literal["policy-coverage-execution-evidence-v2"]
    new_start_bridge: CoverageRawComparison
    current_raw_comparisons: tuple[CoverageRawComparison, ...] = Field(min_length=2, max_length=2)
    graph: tuple[CoverageBoundEvidence, ...] = Field(min_length=7, max_length=7)
    coverage: CoverageBinding

    @model_validator(mode="after")
    def _same_coverage(self) -> Self:
        if self.new_start_bridge.coverage != self.coverage or any(
            x.coverage != self.coverage for x in (*self.current_raw_comparisons, *self.graph)
        ):
            raise ValueError("execution stage/review/closure lost its complete coverage binding")
        return self


class CoverageHistory(v1.HistoricalClaim):
    contract_version: Literal["policy-coverage-historical-claim-v2"]
    coverage: CoverageBinding
    known_recorded_fields: Literal["MATCH"]
    current_window: Literal["STRICT_COMPLETE_PRESERVATION_WITHIN_DECLARED_SCOPE"]


class CoverageResult(v1.PolicyQualifiedExecutionResult):
    namespace = "policy-qualified-execution-result:v2"
    contract_version: Literal["policy-qualified-execution-result-v2"]
    policy: CoveragePolicy
    approval: CoverageApproval
    lineage: CoverageClaim
    history: CoverageHistory
    execution: CoverageExecution

    @model_validator(mode="after")
    def _complete_caveat(self) -> Self:
        if (
            self.history.coverage != self.execution.coverage
            or self.lineage.coverage != self.history.coverage
            or self.policy.coverage_scope != self.history.coverage.scope
            or artifact_ref(self.policy.coverage_measurement) != self.history.coverage.measurement
            or self.approval.coverage_scope != self.history.coverage.scope
            or self.approval.coverage_measurement != self.history.coverage.measurement
        ):
            raise ValueError("result lost policy/approval/coverage caveat")
        return self


class CoverageCore(v1.ResultBindingCore):
    contract_version: Literal["policy-qualified-result-binding-core-v2"]
    history: CoverageHistory
    lineage: CoverageClaim

    @model_validator(mode="after")
    def _claim_caveat(self) -> Self:
        if self.history.coverage != self.lineage.coverage:
            raise ValueError("core history differs from irreversibly bound coverage")
        return self


class CoverageReceipt(v1.PostResultAcceptanceReceipt):
    namespace = "policy-qualified-acceptance-receipt:v2"
    contract_version: Literal["policy-qualified-acceptance-receipt-v2"]
    core: CoverageCore


class CoverageEnvelope(v1.DownstreamEnvelope):
    contract_version: Literal["policy-qualified-downstream-envelope-v2"]
    core: CoverageCore
    receipt: CoverageReceipt


_MODELS = {
    v1.AcceptancePolicy: CoveragePolicy,
    v1.OperatorApproval: CoverageApproval,
    v1.LineageClaim: CoverageClaim,
    v1.RawComparison: CoverageRawComparison,
    v1.BoundEvidence: CoverageBoundEvidence,
    v1.QualifiedBaseline: CoverageBaseline,
    v1.IndependentReviewRecord: CoverageReview,
    v1.PreResultClosureEvidence: CoverageClosure,
    v1.ExecutionEvidence: CoverageExecution,
    v1.HistoricalClaim: CoverageHistory,
    v1.PolicyQualifiedExecutionResult: CoverageResult,
    v1.ResultBindingCore: CoverageCore,
    v1.PostResultAcceptanceReceipt: CoverageReceipt,
    v1.DownstreamEnvelope: CoverageEnvelope,
}


def model_for(model, value):
    """Only callers explicitly using this function opt in to supported v2 types."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, (bytes, str)):
        value = v1.strict_json(value)
    alternate = _MODELS.get(model)
    if alternate is not None and isinstance(value, dict):
        versions = alternate.model_fields["contract_version"].annotation.__args__
        if value.get("contract_version") in versions:
            return alternate
    return model


def read_model(model, value):
    return model_for(model, value).read(value)


def require_policy_pair(policy, approval):
    if isinstance(policy, CoveragePolicy) != isinstance(approval, CoverageApproval):
        raise ValueError("new coverage semantics require a distinct versioned approval")
    if isinstance(policy, CoveragePolicy) and (
        approval.coverage_scope != policy.coverage_scope
        or approval.coverage_measurement != artifact_ref(policy.coverage_measurement)
    ):
        raise ValueError("coverage approval does not bind exact scope and measurement")


class CoverageDecisionProposal(PolicyDecisionProposal):
    namespace = "policy-owner-exact-decision-proposal:v2"
    contract_version: Literal["policy-owner-exact-decision-proposal-v2"]
    policy: CoveragePolicy
    coverage_adoption: Literal["PROSPECTIVE_ONLY_UNKNOWN_HISTORY_RETAINED"]
    coverage_risk_selection: Literal["ACCEPT_PERSISTENT_HISTORICAL_COVERAGE_UNKNOWN"]


_MODELS[PolicyDecisionProposal] = CoverageDecisionProposal
