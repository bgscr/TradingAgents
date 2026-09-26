"""Human-operated synthetic TEST exercise of the actual registered owner.

The catalog below contains untrusted synthetic data. It is not an owner adapter.
Every authentication is performed by the unchanged registered Windows frontend;
all approval, claim, stage and result operations use its real verifier service.
No sandbox, analysis, provider, model or Ticket-07 workload is executed.
"""

from __future__ import annotations

import base64
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.policy_owner.predeployment_fixtures import raw_inventory
from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
from tradingagents.policy_owner_contract import (
    OriginalAcquisitionRegistry,
    PolicyDecisionProposal,
    QualificationExecutionProposal,
    RoleProposal,
)
from tradingagents.policy_qualified_contract import (
    STAGES,
    AcceptancePolicy,
    BytePin,
    Candidate,
    DownstreamEnvelope,
    EvidenceRef,
    HistoricalExceptionScope,
    LineageAllocation,
    LineagePlan,
    OperatorApproval,
    RawComparison,
    ReviewedPolicyPackage,
    StageAllocation,
    StageBudget,
    canonical_bytes,
    content_identity,
    strict_json,
)
from tradingagents.policy_qualified_results import ObservationResolution, reference
from tradingagents.windows_policy_owner import RegisteredWindowsPolicyVerifier
from tradingagents.windows_sandbox_acceptance import (
    TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_new(path: Path, raw: bytes):
    with path.open("xb") as stream:
        stream.write(raw)


class SyntheticCatalog:
    """Untrusted vector storage; deliberately has no authority/ledger methods."""

    def __init__(self):
        self.artifacts = {}
        self.registered = set()
        self.observations = {}
        self.stages = {}
        self.principals = {}
        self.export_root = self.source_root = self.bundle_root = None
        self.evidence_root = self.publication_root = None
        self.resolution = SimpleNamespace(now_utc=now())

    def refresh_time(self):
        # Synthetic observation time only. The owner independently timestamps
        # and orders each durable event with its own native UTC clock.
        self.resolution.now_utc = now()

    def store_raw(self, raw, identity, *, already_registered=False):
        ref = EvidenceRef(identity=identity, pin=BytePin.of(raw))
        for (old_id, old_pin), old_raw in self.artifacts.items():
            if old_id == identity and (old_pin != ref.pin.sha256 or old_raw != raw):
                raise ValueError("synthetic catalog cannot rebind an existing identity")
        self.artifacts[(identity, ref.pin.sha256)] = raw
        if already_registered:
            self.registered.add(("artifact", identity))
        root = self.evidence_root or self.publication_root
        if root is not None:
            path = root / (ref.pin.sha256 + ".json")
            if path.exists():
                if path.read_bytes() != raw:
                    raise ValueError("synthetic evidence content changed")
            else:
                write_new(path, raw)
        return ref

    def store(self, obj, field="identity", *, already_registered=False):
        return self.store_raw(
            canonical_bytes(obj.model_dump(mode="json")) + b"\n",
            getattr(obj, field),
            already_registered=already_registered,
        )

    def save(self, namespace, payload):
        value = {**payload, "identity": content_identity(namespace, payload)}
        return self.store_raw(canonical_bytes(value) + b"\n", value["identity"])

    def read_artifact(self, ref):
        raw = self.artifacts[(ref.identity, ref.pin.sha256)]
        ref.pin.check(raw)
        return raw

    def entry(self, kind, key, value):
        return {
            "contract_version": "policy-owner-evidence-intake-v1",
            "kind": kind,
            "key": key,
            "value": value,
        }

    def pending(self, claim=None):
        entries = []
        for (identity, _digest), raw in self.artifacts.items():
            entries.append(
                self.entry(
                    "artifact",
                    identity,
                    {
                        "reference": {
                            "identity": identity,
                            "pin": BytePin.of(raw).model_dump(mode="json"),
                        },
                        "bytes_base64": base64.b64encode(raw).decode("ascii"),
                    },
                )
            )
        for key, observation in self.observations.items():
            value = asdict(observation)
            value["artifact"] = observation.artifact.model_dump(mode="json")
            value["measurement_contract_pin"] = observation.measurement_contract_pin.model_dump(
                mode="json"
            )
            entries.append(self.entry("observation", key, value))
        for (lineage, kind), raw in self.stages.items():
            value = strict_json(raw)
            ref = EvidenceRef(identity=value["identity"], pin=BytePin.of(raw))
            entries.append(
                self.entry("executed-stage", lineage + "|" + kind, ref.model_dump(mode="json"))
            )
        if claim is not None and all((self.export_root, self.source_root, self.bundle_root)):
            entries.append(
                self.entry(
                    "roots",
                    claim.lineage_identity,
                    {
                        "guest_export": str(self.export_root),
                        "source_input": str(self.source_root),
                        "runtime_bundle": str(self.bundle_root),
                    },
                )
            )
        return [e for e in entries if (e["kind"], e["key"]) not in self.registered]

    def mark_registered(self, entries):
        self.registered.update((e["kind"], e["key"]) for e in entries)


def build_test_policy(registry, read_artifact, contract_pin):
    """Build a new TEST proposal against the actual immutable TEST registry."""
    registry = OriginalAcquisitionRegistry.read(registry)
    before = strict_json(read_artifact(registry.before.artifact))["roots"][0]
    after = strict_json(read_artifact(registry.after.artifact))["roots"][0]
    blocker = strict_json(read_artifact(registry.original_blocker))
    if not str(before["path"]).endswith("TEST-data-only-root-never-created"):
        raise ValueError("registered exercise refuses non-synthetic acquisitions")
    candidate = Candidate(commit="a" * 40, tree="b" * 40, parent="c" * 40)
    scope = HistoricalExceptionScope.seal(
        contract_version="historical-state-exception-v1",
        candidate=candidate,
        original_candidate=Candidate.read(blocker["candidate"]),
        original_blocker=registry.original_blocker,
        before=registry.before,
        after=registry.after,
        root=before["path"],
        before_root_identity=before["inventory_identity"],
        after_root_identity=after["inventory_identity"],
        complete_delta_ledger=registry.complete_delta_ledger,
        changed_rows=1,
        changed_field="attributes",
        raw_before=0x10000010,
        raw_after=0x10,
        historical_equal=False,
        historical_cause="UNRESOLVED",
        prior_outcome="BLOCKED",
    )
    plan = LineagePlan.seal(
        contract_version="historical-state-lineage-plan-v1",
        candidate=candidate,
        stage_slots=STAGES,
        stage_budgets=tuple(
            StageBudget(stage=stage, maximum_execution_seconds=seconds, maximum_cleanup_seconds=120)
            for stage, seconds in zip(STAGES, (1800, 1800, 1800, 14400, 3600, 1800), strict=True)
        ),
        finalization_maximum_seconds=600,
        maximum_lineages=1,
        maximum_claims=1,
        claim_window_days=7,
        terminal_window_days=30,
        source_change_transfer_permitted=False,
        ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        procedure_pin=contract_pin,
        coordinator_pin=contract_pin,
        consumer_contract_pin=contract_pin,
        measurement_contract_pin=contract_pin,
        pre_result_inventory_scope="complete-evidence-and-pre-result-stage-roots-v1",
    )
    return AcceptancePolicy.seal(
        contract_version="historical-state-acceptance-policy-v1",
        scope=scope,
        exception_identity=scope.exception_identity,
        lineage_plan=plan,
        predecessor_proposal_pin=contract_pin,
        scope_evidence_manifest_pin=contract_pin,
        authority_source_contract_pin=contract_pin,
        claim_window_days=7,
        terminal_window_days=30,
        maximum_claims=1,
        revocation_is_terminal=True,
        failure_consumes_attempt=True,
        new_discrepancy_is_terminal=True,
        mandatory_predicate_contract="canonical-ISO02-ISO03-QCF17-A-and-B-before-QCF18-all-27-commands-zero-current-delta-v1",
    )


class RegisteredTestCase:
    """One permanently consumed synthetic lineage, driven through real IPC."""

    def __init__(
        self,
        *,
        root,
        instance,
        role,
        storage_identity,
        registry,
        authenticate,
        verifier=None,
        artifact_reader=None,
        coverage_v2=False,
    ):
        role = RoleProposal.read(role)
        if not re.fullmatch(r"test-[0-9]{8}-[0-9]{3}", instance) or role.instance != instance:
            raise ValueError("this exercise requires the exact enrolled TEST role/instance")
        self.root = Path(root)
        self.instance = instance
        self.role = role
        self.storage_identity = storage_identity
        self.verifier = (
            verifier if verifier is not None else RegisteredWindowsPolicyVerifier(instance)
        )
        if (
            not isinstance(self.verifier, RegisteredWindowsPolicyVerifier)
            or self.verifier.instance != instance
        ):
            raise ValueError("TEST flow requires the actual registered successor adapter")
        self.authenticate = authenticate
        self.owner = SyntheticCatalog()
        self.coverage_enabled = coverage_v2
        self.claim = None
        self.registry = OriginalAcquisitionRegistry.read(registry)
        for ref in (
            self.registry.before.artifact,
            self.registry.after.artifact,
            self.registry.original_blocker,
            self.registry.complete_delta_ledger,
        ):
            if artifact_reader is None:
                result = self.verifier.call(
                    "read-artifact", {"reference": ref.model_dump(mode="json")}
                )
                raw = base64.b64decode(result["bytes_base64"], validate=True)
            else:
                # Unsigned preparation reads immutable data only. All actual
                # authority, claim and stage methods still use the real adapter.
                raw = artifact_reader(ref)
            ref.pin.check(raw)
            self.owner.store_raw(raw, ref.identity, already_registered=True)
        self.contract = self.owner.save(
            "registered-owner-synthetic-exercise:v1",
            {
                "label": "SYNTHETIC TEST DATA ONLY; NO ISO EXECUTION OR OPERATIONAL AUTHORITY",
                "instance": instance,
                "root": str(self.root),
                "historical_cause": "UNRESOLVED",
                "operational": False,
                "execution_authorized": False,
                "stage_vectors": "manufactured offline data; not actual sandbox launches or final reviewer records",
                "stage_budget_rationale": "TEST-only time for human evidence intake; no real budget grant",
            },
        )
        self.policy = build_test_policy(self.registry, self.owner.read_artifact, self.contract.pin)
        if coverage_v2:
            from scripts.policy_owner.registered_coverage_test import configure

            configure(self)
        self.candidate = self.policy.scope.candidate
        self.owner.store(self.policy, "acceptance_policy_identity")
        self.package = ReviewedPolicyPackage.seal(
            contract_version="reviewed-policy-contract-package-v1",
            policy=reference(self.policy, "acceptance_policy_identity"),
            scope_evidence_manifest_pin=self.contract.pin,
            contract_source_pin=self.contract.pin,
        )

    def flush(self, additional=()):
        if self.coverage_enabled and hasattr(self, "approval"):
            from scripts.policy_owner.registered_coverage_test import flush

            return flush(self, additional)
        entries = self.owner.pending(self.claim) + list(additional)
        if not entries:
            return
        if len(entries) > 512:
            raise ValueError("one reviewed intake exceeds the native batch limit")
        result = self.authenticate(
            "register-evidence",
            {
                "contract_version": "policy-owner-evidence-intake-batch-v1",
                "entries": entries,
                "execution_authorized": False,
            },
        )
        if result.get("status") != "AUTHENTICATED_OWNER_RECORD_RETAINED":
            raise ValueError("evidence intake was not authenticated")
        self.owner.mark_registered(entries)

    def proposal(self):
        effective = datetime.now(timezone.utc)
        from tradingagents.policy_coverage_models import CoverageDecisionProposal

        model = CoverageDecisionProposal if self.coverage_enabled else PolicyDecisionProposal
        extra = (
            {
                "coverage_adoption": "PROSPECTIVE_ONLY_UNKNOWN_HISTORY_RETAINED",
                "coverage_risk_selection": "ACCEPT_PERSISTENT_HISTORICAL_COVERAGE_UNKNOWN",
            }
            if self.coverage_enabled
            else {}
        )
        return model.seal(
            contract_version="policy-owner-exact-decision-proposal-v2"
            if self.coverage_enabled
            else "policy-owner-exact-decision-proposal-v1",
            **extra,
            instance=self.instance,
            registered_role_identity=self.role.identity,
            storage_binding_identity=self.storage_identity,
            reviewed_package=self.package,
            policy=self.policy,
            prospective_replacement_scope=self.role.prospective_project_policy_scope,
            campaign_specification_adoption=self.role.campaign_specification_scope,
            project_contract_adoption=self.role.project_contract_scope,
            execution=QualificationExecutionProposal(
                selection="AUTHORIZE_BOUNDED_QUALIFICATION",
                candidate=self.candidate,
                lineage_plan_identity=self.policy.lineage_plan.lineage_plan_identity,
                stage_budgets=self.policy.lineage_plan.stage_budgets,
                finalization_maximum_seconds=self.policy.lineage_plan.finalization_maximum_seconds,
                maximum_lineages=1,
                maximum_claims=1,
                effective_utc=effective.isoformat(),
                expires_utc=(effective + timedelta(days=7)).isoformat(),
                ticket07_or_live_authorized=False,
            ),
            selection="APPROVE",
            historical_cause="UNRESOLVED",
            historical_equal=False,
            historical_outcome="BLOCKED",
            result_execution_authorized=False,
            ticket07_or_live_authorized=False,
        )

    def start(self, proposal):
        self.flush(
            (
                self.owner.entry(
                    "current-source",
                    self.contract.identity,
                    {
                        "candidate": self.candidate.model_dump(mode="json"),
                        "lineage_plan_identity": self.policy.lineage_plan.lineage_plan_identity,
                        "production_inventory_identity": "production-python-modules:v1:sha256:"
                        + "d" * 64,
                    },
                ),
            )
        )
        result = self.authenticate("decide", proposal.model_dump(mode="json"))
        from tradingagents.policy_coverage_models import read_model

        self.approval = read_model(OperatorApproval, result["approval"])
        self.owner.store(self.approval, "approval_identity", already_registered=True)
        if self.coverage_enabled:
            from scripts.policy_owner.registered_coverage_test import verify_bootstrap_artifacts

            verify_bootstrap_artifacts(self)
        self.verifier.eligibility(self.policy, self.approval)
        self.allocation = LineageAllocation.seal(
            contract_version="historical-state-lineage-allocation-v1",
            approval_identity=self.approval.approval_identity,
            acceptance_policy_identity=self.policy.acceptance_policy_identity,
            exception_identity=self.policy.exception_identity,
            lineage_plan=self.policy.lineage_plan,
            candidate=self.candidate,
            stages=tuple(
                StageAllocation(
                    stage=stage,
                    run_id="synthetic-registered-" + str(i),
                    root=str(self.root / ("SYNTHETIC-stage-" + str(i))),
                )
                for i, stage in enumerate(STAGES)
            ),
            additive_baseline_version="SYNTHETIC-registered-additive-baseline-v1",
            pre_result_evidence_root=str(self.root / "SYNTHETIC-pre-result-evidence"),
        )
        self.owner.refresh_time()
        self.bridge = self.raw_comparison("ISO02", bridge=True)
        self.owner.store(self.bridge)
        self.flush(
            (
                self.owner.entry(
                    "allocation",
                    self.policy.lineage_plan.lineage_plan_identity,
                    self.allocation.model_dump(mode="json"),
                ),
            )
        )
        self.claim = self.verifier.claim(
            self.policy, self.approval, self.allocation, new_start_bridge=self.bridge
        )
        self.owner.store(self.claim, "lineage_identity", already_registered=True)
        self.owner.refresh_time()

    def raw_comparison(self, stage, *, bridge=False):
        if self.coverage_enabled:
            from scripts.policy_owner.registered_coverage_test import raw_comparison

            return raw_comparison(self, stage, bridge=bridge)
        self.owner.refresh_time()
        refs, names = [], []
        for phase in ("before", "after"):
            if bridge and phase == "before":
                refs.append(self.policy.scope.after.artifact)
                names.append(self.policy.scope.after.observation_id)
                continue
            observation_phase = "before" if bridge else phase
            name = "SYNTHETIC-registered-" + stage + "-" + observation_phase
            ref = raw_inventory(
                self.owner,
                phase=observation_phase,
                attributes=0x10,
                root=self.policy.scope.root,
                elapsed=3.0,
                equal=True,
            )
            self.owner.observations[name] = ObservationResolution(
                name,
                ref,
                self.policy.lineage_plan.measurement_contract_pin,
                None if bridge else self.claim.lineage_identity,
                self.policy.lineage_plan.lineage_plan_identity,
                self.allocation.allocation_identity,
                stage,
                observation_phase,
                "NEW_QUALIFICATION",
                self.owner.resolution.now_utc,
            )
            refs.append(ref)
            names.append(name)
        return RawComparison.seal(
            contract_version="policy-qualified-raw-comparison-v1",
            measurement_contract_pin=self.policy.lineage_plan.measurement_contract_pin,
            before_observation=names[0],
            after_observation=names[1],
            before=refs[0],
            after=refs[1],
            comparable=True,
            equal=True,
        )

    @staticmethod
    def stage(kind):
        return {
            "REVIEW_STANDARDS": "REVIEWS",
            "REVIEW_SPEC": "REVIEWS",
            "PRE_RESULT_CLOSURE": "CLOSURE_HANDOFF",
        }.get(kind, kind)

    def before_node(self, kind):
        if self.coverage_enabled:
            self.coverage_stage = self.stage(kind)
        if kind != "REVIEW_SPEC":
            self.verifier.begin_stage(
                self.policy,
                self.approval,
                self.claim,
                stage=self.stage(kind),
                new_start_bridge=self.bridge,
            )
        self.owner.refresh_time()

    def after_node(self, node):
        additional = []
        for ref in node.artifacts:
            principal = self.owner.principals.get(ref.identity)
            if principal:
                session = self.owner.save(
                    "synthetic-review-session:v1",
                    {
                        "label": "MANUFACTURED TEST VECTOR ONLY; NOT AN ACTUAL INDEPENDENT REVIEW",
                        "reviewer_principal": principal,
                        "review": ref.model_dump(mode="json"),
                        "execution_authorized": False,
                    },
                )
                additional.append(
                    self.owner.entry(
                        "review-provenance",
                        ref.identity,
                        {
                            "reference": ref.model_dump(mode="json"),
                            "reviewer_principal": principal,
                            "source_session_evidence": session.model_dump(mode="json"),
                        },
                    )
                )
        self.flush(additional)
        if node.kind != "REVIEW_STANDARDS":
            self.verifier.complete_stage(
                self.policy, self.approval, self.claim, stage=self.stage(node.kind), evidence=node
            )
        self.owner.refresh_time()

    def finish(self):
        execution = synthetic_stage_vectors(self)
        result = self.verifier.finalize(
            policy=self.policy, approval=self.approval, claim=self.claim, execution=execution
        )
        receipt = self.verifier.issue_receipt(result.to_bytes())
        from tradingagents.policy_coverage_models import CoverageEnvelope

        model = CoverageEnvelope if self.coverage_enabled else DownstreamEnvelope
        envelope = model(
            contract_version="policy-qualified-downstream-envelope-v2"
            if self.coverage_enabled
            else "policy-qualified-downstream-envelope-v1",
            core=receipt.core,
            receipt=receipt,
            execution_authorized=False,
        )
        verified = self.verifier.verify_downstream(envelope)
        if verified != envelope:
            raise ValueError("registered verifier changed the complete conditional envelope")
        for name, model in (
            ("TEST-result.json", result),
            ("TEST-receipt.json", receipt),
            ("TEST-envelope.json", envelope),
        ):
            write_new(self.root / name, model.to_bytes())
        return result, envelope
