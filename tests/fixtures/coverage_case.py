"""One explicitly synthetic prospective authority; cannot satisfy native authentication."""

from copy import deepcopy
from dataclasses import replace

from scripts.policy_owner.predeployment_fixtures import SyntheticCase
from tests.fixtures.policy_coverage_fixture import make_coverage_fixture, seal_current
from tradingagents.policy_coverage_contract import (
    CoverageMeasurement,
    artifact_ref,
    build_scope,
    compare_known_fields,
)
from tradingagents.policy_coverage_models import (
    CoverageApproval,
    CoverageBinding,
    CoveragePolicy,
    CoverageRawComparison,
)
from tradingagents.policy_qualified_contract import (
    BytePin,
    LineageAllocation,
    LineagePlan,
    ReviewedPolicyPackage,
    strict_json,
)
from tradingagents.policy_qualified_results import ObservationResolution


def coverage_case(root, *, fixture=None, artifact_directory=None):
    f = make_coverage_fixture() if fixture is None else fixture
    case = SyntheticCase(
        root,
        root_template=strict_json(f["after"])["roots"][0],
        clock_utc="2099-01-01T00:00:04+00:00",
        artifact_directory=artifact_directory,
    )
    owner = case.owner
    before = owner.read_artifact(case.policy.scope.before.artifact)
    after = owner.read_artifact(case.policy.scope.after.artifact)
    scope = build_scope(before, after, fixture_only=True)
    m = f["measurement"].model_dump(mode="json")
    m.pop("identity")
    m["scope"] = artifact_ref(scope)
    m["candidate"] = case.candidate
    measurement = CoverageMeasurement.seal(**m)
    p = case.policy.lineage_plan.model_dump(mode="json")
    p.pop("lineage_plan_identity")
    p["measurement_contract_pin"] = BytePin.of(measurement.to_bytes())
    plan = LineagePlan.seal(**p)
    p = case.policy.model_dump(mode="json")
    p.pop("acceptance_policy_identity")
    p.update(
        contract_version="historical-state-acceptance-policy-v2",
        lineage_plan=plan,
        coverage_scope=artifact_ref(scope),
        coverage_measurement=measurement,
        coverage_rule="KNOWN_RECORDED_FIELDS_UNKNOWN_HISTORY_STRICT_CURRENT_WINDOW",
    )
    policy = CoveragePolicy.seal(**p)
    owner.store(scope)
    owner.store(measurement)
    owner.pinned[artifact_ref(measurement).pin.sha256] = measurement.to_bytes()
    package = ReviewedPolicyPackage.seal(
        contract_version="reviewed-policy-contract-package-v1",
        policy=owner.store(policy, "acceptance_policy_identity"),
        scope_evidence_manifest_pin=policy.scope_evidence_manifest_pin,
        contract_source_pin=plan.consumer_contract_pin,
    )
    a = case.approval.model_dump(mode="json")
    a.pop("approval_identity")
    a.update(
        contract_version="historical-state-operator-approval-v2",
        acceptance_policy_identity=policy.acceptance_policy_identity,
        lineage_plan_identity=plan.lineage_plan_identity,
        reviewed_package_manifest_pin=BytePin.of(package.to_bytes()),
        coverage_scope=artifact_ref(scope),
        coverage_measurement=artifact_ref(measurement),
        historical_coverage="UNKNOWN",
    )
    approval = CoverageApproval.seal(**a)
    case.old_approval = case.approval
    case.policy = policy
    case.approval = approval
    owner.resolution = replace(
        owner.resolution,
        approval_bytes=approval.to_bytes(),
        reviewed_package_manifest_bytes=package.to_bytes(),
        approved_policy_identity=policy.acceptance_policy_identity,
        execution_lineage_plan_identity=plan.lineage_plan_identity,
        current_lineage_plan_identity=plan.lineage_plan_identity,
    )
    a = case.allocation.model_dump(mode="json")
    a.pop("allocation_identity")
    a.update(
        approval_identity=approval.approval_identity,
        acceptance_policy_identity=policy.acceptance_policy_identity,
        lineage_plan=plan,
    )
    case.allocation = LineageAllocation.seal(**a)
    owner.allocation = case.allocation
    baseline = deepcopy(f["baseline"])
    baseline.update(
        scope=artifact_ref(scope).model_dump(mode="json"),
        measurement=artifact_ref(measurement).model_dump(mode="json"),
        candidate=case.candidate.model_dump(mode="json"),
    )
    baseline = seal_current(baseline)
    assessment = compare_known_fields(scope, before, after, baseline, measurement)
    baseline_ref = owner.store_raw(
        __import__(
            "tradingagents.policy_qualified_contract", fromlist=["canonical_bytes"]
        ).canonical_bytes(baseline)
        + b"\n",
        baseline["identity"],
    )
    assessment_ref = owner.store(assessment)
    binding = CoverageBinding(
        scope=artifact_ref(scope),
        measurement=artifact_ref(measurement),
        baseline=baseline_ref,
        assessment=assessment_ref,
        historical_coverage="UNKNOWN",
        historical_object_continuity="NOT_ESTABLISHED",
        hidden_contents="UNKNOWN_NOT_RETROACTIVELY_COMPLETED",
    )
    name = "SYNTHETIC-coverage-B0"
    owner.observations[name] = ObservationResolution(
        name,
        baseline_ref,
        plan.measurement_contract_pin,
        None,
        plan.lineage_plan_identity,
        case.allocation.allocation_identity,
        "ISO02",
        "before",
        "NEW_QUALIFICATION",
        baseline["capture"]["finished_utc"],
    )
    case.bridge = CoverageRawComparison.seal(
        contract_version="policy-qualified-raw-comparison-v2",
        measurement_contract_pin=plan.measurement_contract_pin,
        before_observation=policy.scope.after.observation_id,
        after_observation=name,
        before=policy.scope.after.artifact,
        after=baseline_ref,
        comparable=True,
        equal=False,
        historical_equal=False,
        relation="KNOWN_RECORDED_FIELDS",
        coverage=binding,
    )
    case.coverage = binding
    case.coverage_scope = scope
    case.coverage_measurement = measurement
    case.coverage_baseline = baseline
    owner.coverage_instance = lambda: "test-20990101-999"

    def comparison(stage, *, bridge=False):
        if bridge:
            return case.bridge
        from tradingagents.policy_qualified_contract import canonical_bytes

        refs = []
        names = []
        for phase in ("before", "after"):
            value = deepcopy(case.coverage_baseline)
            value["phase"] = phase
            name = "SYNTHETIC-coverage-" + stage + "-" + phase
            value["capture"].update(
                nonce=__import__("hashlib").sha256(name.encode()).hexdigest()[:32],
                started_utc=owner.resolution.now_utc,
                finished_utc=owner.resolution.now_utc,
            )
            value = seal_current(value)
            ref = owner.store_raw(canonical_bytes(value) + b"\n", value["identity"])
            owner.observations[name] = ObservationResolution(
                name,
                ref,
                plan.measurement_contract_pin,
                case.claim.lineage_identity,
                plan.lineage_plan_identity,
                case.allocation.allocation_identity,
                stage,
                phase,
                "NEW_QUALIFICATION",
                owner.resolution.now_utc,
            )
            names.append(name)
            refs.append(ref)
        return CoverageRawComparison.seal(
            contract_version="policy-qualified-raw-comparison-v2",
            measurement_contract_pin=plan.measurement_contract_pin,
            before_observation=names[0],
            after_observation=names[1],
            before=refs[0],
            after=refs[1],
            comparable=True,
            equal=True,
            historical_equal=False,
            relation="CURRENT_WINDOW",
            coverage=binding,
        )

    case.raw_comparison = comparison

    def stage_for(kind):
        return {
            "REVIEW_STANDARDS": "REVIEWS",
            "REVIEW_SPEC": "REVIEWS",
            "PRE_RESULT_CLOSURE": "CLOSURE_HANDOFF",
        }.get(kind, kind)

    def before_node(kind):
        if kind != "REVIEW_SPEC":
            case.verifier.begin_stage(
                case.policy,
                case.approval,
                case.claim,
                stage=stage_for(kind),
                new_start_bridge=case.bridge,
            )

    def after_node(node):
        if node.kind != "REVIEW_STANDARDS":
            case.verifier.complete_stage(
                case.policy, case.approval, case.claim, stage=stage_for(node.kind), evidence=node
            )

    case.before_node = before_node
    case.after_node = after_node
    # New v2 comparisons use the explicit immutable B0, not the old template.
    owner.fixture_root_template = None
    original_reserve = case.reserve

    def reserve():
        claim = original_reserve()
        owner.coverage_lineage = claim.lineage_identity
        return claim

    case.reserve = reserve
    return case
