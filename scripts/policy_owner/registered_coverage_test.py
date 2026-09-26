"""Opt-in coverage vectors for the existing genuinely human-operated TEST flow.

No authentication adapter is supplied here. Staging is unauthenticated; both
descriptor and dependent registration batches pass through the native frontend.
Generated inventories are conspicuously TEST-only, never physical observations.
"""

from __future__ import annotations

import base64
import copy
import uuid
from datetime import datetime, timedelta, timezone

from scripts.policy_owner.coverage_test_vectors import generated_baseline, seal_generated
from scripts.policy_owner.registered_test_flow import write_new
from tradingagents.policy_coverage_contract import (
    CoverageMeasurement,
    artifact_ref,
    build_scope,
    compare_known_fields,
)
from tradingagents.policy_coverage_models import (
    CoverageBinding,
    CoveragePolicy,
    CoverageRawComparison,
)
from tradingagents.policy_evidence_client import stage_artifact
from tradingagents.policy_evidence_contract import ArtifactDescriptor
from tradingagents.policy_qualified_contract import (
    BytePin,
    LineagePlan,
    canonical_bytes,
    strict_json,
)
from tradingagents.policy_qualified_results import ObservationResolution


def configure(case):
    before = case.owner.read_artifact(case.policy.scope.before.artifact)
    after = case.owner.read_artifact(case.policy.scope.after.artifact)
    scope = build_scope(before, after, fixture_only=True)
    if len(scope.gaps) != 7 or len(scope.links) != 5322:
        raise ValueError(
            "coverage TEST route requires its exact seven-marker/5322-link setup corpus"
        )
    measurement = CoverageMeasurement.seal(
        contract_version="policy-coverage-measurement-v2",
        scope=artifact_ref(scope),
        candidate=case.policy.scope.candidate,
        supported_reparse_tags=(0xA0000003,),
        security_required=True,
        maximum_entries=100000,
        maximum_read_bytes=1024**3,
        maximum_seconds=120,
        maximum_depth=128,
        maximum_reparse_bytes=16384,
        maximum_indirect_link_depth=0,
        target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
        known_fields_required=True,
    )
    p = case.policy.lineage_plan.model_dump(mode="json", exclude={"lineage_plan_identity"})
    p["measurement_contract_pin"] = BytePin.of(measurement.to_bytes())
    plan = LineagePlan.seal(**p)
    p = case.policy.model_dump(mode="json", exclude={"acceptance_policy_identity"})
    p.update(
        contract_version="historical-state-acceptance-policy-v2",
        lineage_plan=plan,
        coverage_scope=artifact_ref(scope),
        coverage_measurement=measurement,
        coverage_rule="KNOWN_RECORDED_FIELDS_UNKNOWN_HISTORY_STRICT_CURRENT_WINDOW",
    )
    case.policy = CoveragePolicy.seal(**p)
    case.coverage_scope = scope
    case.coverage_measurement = measurement
    case.coverage_stage = "ISO02"
    # The trusted owner independently derives/retains these only after the exact
    # policy decision. They are displayed with the unsigned package beforehand.
    for name, obj in [
        ("TEST-coverage-scope.json", scope),
        ("TEST-coverage-measurement.json", measurement),
    ]:
        path = case.root / name
        if path.exists():
            if path.read_bytes() != obj.to_bytes():
                raise ValueError("reviewed coverage TEST bindings changed")
        else:
            write_new(path, obj.to_bytes())


def verify_bootstrap_artifacts(case):
    for obj in (case.coverage_scope, case.coverage_measurement):
        reference = artifact_ref(obj)
        response = case.verifier.call(
            "read-artifact", {"reference": reference.model_dump(mode="json")}
        )
        raw = base64.b64decode(response["bytes_base64"], validate=True)
        reference.pin.check(raw)
        case.owner.store_raw(raw, reference.identity, already_registered=True)


def raw_comparison(case, stage, *, bridge=False):
    case.owner.refresh_time()
    clock = case.owner.resolution.now_utc
    if bridge:
        baseline = generated_baseline(
            case.coverage_scope,
            case.coverage_measurement,
            case.owner.read_artifact(case.policy.scope.after.artifact),
            instance=case.instance,
            clock=clock,
        )
        assessment = compare_known_fields(
            case.coverage_scope,
            case.owner.read_artifact(case.policy.scope.before.artifact),
            case.owner.read_artifact(case.policy.scope.after.artifact),
            baseline,
            case.coverage_measurement,
        )
        b0 = case.owner.store_raw(canonical_bytes(baseline) + b"\n", baseline["identity"])
        case.coverage_baseline = baseline
        case.coverage = CoverageBinding(
            scope=artifact_ref(case.coverage_scope),
            measurement=artifact_ref(case.coverage_measurement),
            baseline=b0,
            assessment=case.owner.store(assessment),
            historical_coverage="UNKNOWN",
            historical_object_continuity="NOT_ESTABLISHED",
            hidden_contents="UNKNOWN_NOT_RETROACTIVELY_COMPLETED",
        )
    refs = []
    names = []
    for phase in ("before", "after"):
        if bridge and phase == "before":
            refs.append(case.policy.scope.after.artifact)
            names.append(case.policy.scope.after.observation_id)
            continue
        observation_phase = "before" if bridge else phase
        if bridge:
            value = case.coverage_baseline
        else:
            value = copy.deepcopy(case.coverage_baseline)
            value["phase"] = phase
            value["capture"].update(nonce=uuid.uuid4().hex, started_utc=clock, finished_utc=clock)
            value = seal_generated(value)
        ref = case.owner.store_raw(canonical_bytes(value) + b"\n", value["identity"])
        name = value["capture"]["observation_identity"]
        case.owner.observations[name] = ObservationResolution(
            name,
            ref,
            case.policy.lineage_plan.measurement_contract_pin,
            None if bridge else case.claim.lineage_identity,
            case.policy.lineage_plan.lineage_plan_identity,
            case.allocation.allocation_identity,
            stage,
            observation_phase,
            "NEW_QUALIFICATION",
            clock,
        )
        refs.append(ref)
        names.append(name)
    return CoverageRawComparison.seal(
        contract_version="policy-qualified-raw-comparison-v2",
        measurement_contract_pin=case.policy.lineage_plan.measurement_contract_pin,
        before_observation=names[0],
        after_observation=names[1],
        before=refs[0],
        after=refs[1],
        comparable=True,
        equal=not bridge,
        historical_equal=False,
        relation="KNOWN_RECORDED_FIELDS" if bridge else "CURRENT_WINDOW",
        coverage=case.coverage,
    )


def flush(case, additional):
    from tradingagents.policy_qualified_contract import EvidenceRef

    entries = case.owner.pending(case.claim) + list(additional)
    if not entries:
        return
    compact, dependent, marked = [], [], []
    observations = [entry for entry in entries if entry["kind"] == "observation"]
    observed_refs = {entry["value"]["artifact"]["identity"] for entry in observations}

    def stage(reference, observation=None):
        reference = EvidenceRef.read(reference)
        raw = case.owner.read_artifact(reference)
        opaque = reference.identity.startswith("policy-evidence-bytes:v1:sha256:")
        value = {} if opaque else strict_json(raw)
        fields = (
            ["byte_digest"]
            if opaque
            else [key for key, item in value.items() if item == reference.identity]
        )
        if len(fields) != 1:
            raise ValueError("TEST artifact has no unambiguous semantic identity field")
        at = datetime.now(timezone.utc)
        descriptor = ArtifactDescriptor.seal(
            contract_version="policy-owner-artifact-descriptor-v2",
            operation="REGISTER_EVIDENCE",
            instance=case.instance,
            role_identity=case.role.identity,
            approval_identity=case.approval.approval_identity,
            candidate=case.candidate,
            lineage_plan_identity=case.policy.lineage_plan.lineage_plan_identity,
            acquisition_identity=(
                observation["observation_id"]
                if observation
                else "GENERATED-TEST-artifact-" + reference.pin.sha256
            ),
            stage=observation["stage"] if observation else case.coverage_stage,
            phase=observation["phase"] if observation else value.get("phase", "evidence"),
            artifact_schema=reference.identity.split(":sha256:")[0],
            artifact_identity_field=fields[0],
            artifact=reference,
            issued_utc=at.isoformat(),
            expires_utc=(at + timedelta(hours=1)).isoformat(),
        )
        path = case.root / ("coverage-artifact-" + reference.pin.sha256 + ".json")
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("retained TEST artifact changed")
        else:
            write_new(path, raw)
        compact.append(
            stage_artifact(
                case.verifier.call,
                path,
                descriptor,
                coverage_context=(case.coverage_scope, case.coverage_measurement),
                acquisition_registration=observation is not None,
            )
        )
        return descriptor

    # Every new observation gets its own exact descriptor, even when the
    # canonical measured state was previously registered with identical bytes.
    for original in observations:
        value = original["value"]
        descriptor = stage(value["artifact"], value)
        dependent.append(
            {
                **original,
                "contract_version": "policy-owner-evidence-intake-v3",
                "value": {**value, "artifact_descriptor_identity": descriptor.identity},
            }
        )
    for original in entries:
        if original["kind"] == "observation":
            continue
        if original["kind"] != "artifact":
            dependent.append(original)
            continue
        reference = EvidenceRef.read(original["value"]["reference"])
        if reference.identity in observed_refs:
            marked.append(original)
            continue
        raw = case.owner.read_artifact(reference)
        namespace = reference.identity.split(":sha256:")[0]
        if len(raw) <= 16384 and not namespace.startswith(
            ("policy-coverage-", "historical-coverage-", "policy-qualified-")
        ):
            dependent.append(original)
            continue
        stage(reference)
        marked.append(original)

    # Separate genuine descriptor and dependent registration confirmations.
    # The owner never resolves an unauthenticated same-batch descriptor.
    for batch, originals in ((compact, marked), (dependent, dependent)):
        if not batch:
            continue
        if len(batch) > 512:
            raise ValueError("bounded TEST evidence batch exceeded")
        document = {
            "contract_version": "policy-owner-evidence-intake-batch-v3",
            "entries": batch,
            "execution_authorized": False,
        }
        if len(canonical_bytes(document)) > 16 * 1024**2:
            raise ValueError("TEST control document capacity exceeded")
        result = case.authenticate("register-evidence", document)
        if result.get("status") != "AUTHENTICATED_OWNER_RECORD_RETAINED":
            raise ValueError("TEST document was not authenticated")
        case.owner.mark_registered(originals)
