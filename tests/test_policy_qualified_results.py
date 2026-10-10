"""SYNTHETIC ONLY: fixture people, clocks, evidence and disposable claim ledgers.

No historical path, original acquisition, real approval, native launch, model or
qualification is used. Positive results exist only inside synthetic test storage.
"""

from __future__ import annotations

import copy
import io
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest

from tradingagents.policy_qualification_lifecycle import LineageLedger
from tradingagents.policy_qualified_contract import (
    STAGES,
    AcceptancePolicy,
    BoundEvidence,
    BytePin,
    Candidate,
    DownstreamEnvelope,
    EvidenceInventoryRoot,
    EvidenceRef,
    ExecutionEvidence,
    HistoricalExceptionScope,
    IndependentReviewRecord,
    LineageAllocation,
    LineageClaim,
    LineagePlan,
    OperatorApproval,
    OriginalAcquisition,
    PolicyQualifiedExecutionResult,
    PreResultClosureEvidence,
    QualifiedBaseline,
    RawComparison,
    ReviewedPolicyPackage,
    StageAllocation,
    StageBudget,
    canonical_bytes,
    content_identity,
    strict_json,
    utc,
)
from tradingagents.policy_qualified_results import (
    AuthorityResolution,
    ObservationResolution,
    PolicyQualifiedVerifier,
    acceptance_binding,
    capture_pre_result_evidence_inventory,
    confirm_policy_decision_challenge,
    reference,
)


class SyntheticOwner:
    """Deliberately fake authenticated owner; never imported by production code."""

    def __init__(self):
        self.artifacts = {}
        self.pinned = {}
        self.originals = {}
        self.observations = {}
        self.stages = {}
        self.principals = {}
        self.resolution = None
        self.export_root = None
        self.source_root = None
        self.bundle_root = None
        self.evidence_root = None
        self.publication_root = None
        self.allocation = None
        self.resolutions = 0

    def store_raw(self, raw, identity):
        ref = EvidenceRef(identity=identity, pin=BytePin.of(raw))
        self.artifacts[(ref.identity, ref.pin.sha256)] = raw
        root = self.evidence_root or self.publication_root
        if root is not None:
            (root / (ref.pin.sha256 + ".json")).write_bytes(raw)
        return ref

    def store(self, obj, field="identity"):
        return self.store_raw(
            canonical_bytes(obj.model_dump(mode="json")) + b"\n", getattr(obj, field)
        )

    def save(self, namespace, payload):
        obj = {**payload, "identity": content_identity(namespace, payload)}
        return self.store_raw(canonical_bytes(obj) + b"\n", obj["identity"])

    def read_artifact(self, ref):
        try:
            return self.artifacts[(ref.identity, ref.pin.sha256)]
        except KeyError as exc:
            raise ValueError("SYNTHETIC unknown artifact") from exc

    def read_pinned_file(self, pin):
        try:
            return self.pinned[pin.sha256]
        except KeyError as exc:
            raise ValueError("SYNTHETIC unresolved pinned predecessor") from exc

    def resolve(self, approval_identity):
        self.resolutions += 1
        if (
            self.resolution is None
            or OperatorApproval.read(self.resolution.approval_bytes).approval_identity
            != approval_identity
        ):
            raise ValueError("SYNTHETIC unauthenticated approval")
        return self.resolution

    def original_acquisition(self, observation_id):
        try:
            return self.originals[observation_id]
        except KeyError as exc:
            raise ValueError("SYNTHETIC not the retained original observation") from exc

    def canonical_allocation(self, lineage_plan_identity):
        if (
            self.allocation is None
            or self.allocation.lineage_plan.lineage_plan_identity != lineage_plan_identity
        ):
            raise ValueError("SYNTHETIC unknown canonical allocation")
        return self.allocation

    def observation(self, observation_id):
        try:
            return self.observations[observation_id]
        except KeyError as exc:
            raise ValueError("SYNTHETIC unresolved new observation") from exc

    def executed_stage(self, lineage_identity, kind):
        try:
            return self.stages[(lineage_identity, kind)]
        except KeyError as exc:
            raise ValueError("SYNTHETIC unregistered execution") from exc

    def review_principal(self, ref):
        return self.principals.get(ref.identity, "")

    def guest_export_root(self, lineage_identity):
        assert lineage_identity.startswith("historical-state-qualification-lineage:v1:")
        return self.export_root

    def source_input_root(self, lineage_identity):
        assert lineage_identity.startswith("historical-state-qualification-lineage:v1:")
        return self.source_root

    def runtime_bundle_root(self, lineage_identity):
        assert lineage_identity.startswith("historical-state-qualification-lineage:v1:")
        return self.bundle_root


def reseal(model, value, **changes):
    payload = (
        value.model_dump(mode="json") if hasattr(value, "model_dump") else copy.deepcopy(value)
    )
    payload.update(changes)
    if model is LineageClaim and "allocation_identity" not in changes:
        allocation = {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "claimed_at_utc",
                "state",
                "attempt_ordinal",
                "lineage_identity",
                "allocation_identity",
            }
        }
        allocation["contract_version"] = "historical-state-lineage-allocation-v1"
        payload["allocation_identity"] = LineageAllocation.seal(**allocation).allocation_identity
    payload.pop(model.identity_field)
    return model.seal(**payload)


def raw_inventory(
    owner,
    *,
    phase,
    attributes,
    root=r"D:\SYNTHETIC-HISTORY\build",
    elapsed=1.0,
    equal=False,
    extra_null_field=False,
):
    entries = [
        {
            "path": ".",
            "kind": "directory",
            "attributes": attributes,
            "mtime_ns": 101,
            "ctime_ns": 100,
            "descriptor_sha256": "f" * 64,
        }
    ]
    if extra_null_field:
        entries[0]["additional_unapproved_field"] = None
    roots = [
        {
            "path": root,
            "entries": entries,
            "file_count": 0,
            "unreadable_count": 0,
            "security_descriptors_requested": True,
            "inventory_identity": content_identity(
                "crash-protection-historical-root-inventory:v1", entries
            ),
        }
    ]
    payload = {
        "phase": phase,
        "roots": roots,
        "elapsed_seconds": elapsed,
        "file_count": 0,
        "unreadable_count": 0,
        "no_acl_write_or_recovery": True,
    }
    if phase == "after":
        payload.update(equal=equal, changed_roots=[] if equal else [root])
    return owner.save("crash-protection-historical-inventory:v1", payload)


class SyntheticCase:
    def __init__(self, tmp_path, *, additional_historical_field=False):
        from tradingagents.windows_sandbox_acceptance import (
            TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
            TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        )

        self.root = tmp_path
        self.owner = owner = SyntheticOwner()
        self.candidate = Candidate(commit="a" * 40, tree="b" * 40, parent="c" * 40)
        pin = BytePin.of(b"SYNTHETIC reviewed source/procedure contract")
        owner.pinned[pin.sha256] = b"SYNTHETIC reviewed source/procedure contract"
        before = raw_inventory(owner, phase="before", attributes=0x10000010)
        after = raw_inventory(
            owner,
            phase="after",
            attributes=0x10,
            elapsed=2.0,
            extra_null_field=additional_historical_field,
        )
        acquisitions = []
        for phase, artifact, duration in (("before", before, 1.0), ("after", after, 2.0)):
            acquisition = OriginalAcquisition(
                observation_id="SYNTHETIC-original-" + phase,
                original_path="D:\\SYNTHETIC-SAVED\\" + phase + ".json",
                phase=phase,
                origin="RETAINED_ORIGINAL_FILES",
                artifact=artifact,
                collector_pin=pin,
                procedure_pin=pin,
                collection_elapsed_seconds=duration,
                collection_utc=None,
                sample_utc=None,
                transition_utc=None,
            )
            owner.originals[acquisition.observation_id] = acquisition
            acquisitions.append(acquisition)
        a = strict_json(owner.read_artifact(before))["roots"][0]
        b = strict_json(owner.read_artifact(after))["roots"][0]
        delta = owner.save(
            "crash-protection-historical-delta-detail:v1",
            {
                "status": "DIFFERENCE_REQUIRES_CLASSIFICATION",
                "source_modified": False,
                "iso02_started": False,
                "no_repair_performed": True,
                "roots": [
                    {
                        "root": a["path"],
                        "before_identity": a["inventory_identity"],
                        "after_identity": b["inventory_identity"],
                        "changes": [
                            {
                                "relative_path": ".",
                                "kind": "directory",
                                "before": a["entries"][0],
                                "after": b["entries"][0],
                                "changed_fields": ["attributes"],
                            }
                        ],
                    }
                ],
            },
        )
        blocker = owner.save(
            "crash-protection-source-requalification-blocked:v1",
            {
                "label": "SYNTHETIC",
                "status": "BLOCKED",
                "candidate": {"commit": "1" * 40, "tree": "2" * 40, "parent": "3" * 40},
                "historical_inventory": {"equal": False, "changed_roots": [a["path"]]},
                "difference": {
                    "root": a["path"],
                    "changed_directory_count": 1,
                    "changed_field": "attributes",
                    "before_hex": "0x10000010",
                    "after_hex": "0x00000010",
                    "changed_bit": "0x10000000",
                },
                "artifacts": [
                    {"path": acq.original_path, **acq.artifact.pin.model_dump(mode="json")}
                    for acq in acquisitions
                ]
                + [
                    {
                        "path": r"D:\SYNTHETIC-SAVED\complete-delta.json",
                        **delta.pin.model_dump(mode="json"),
                    }
                ],
            },
        )
        scope = HistoricalExceptionScope.seal(
            contract_version="historical-state-exception-v1",
            candidate=self.candidate,
            original_candidate=Candidate(commit="1" * 40, tree="2" * 40, parent="3" * 40),
            original_blocker=blocker,
            before=acquisitions[0],
            after=acquisitions[1],
            root=a["path"],
            before_root_identity=a["inventory_identity"],
            after_root_identity=b["inventory_identity"],
            complete_delta_ledger=delta,
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
            candidate=self.candidate,
            stage_slots=STAGES,
            stage_budgets=tuple(
                StageBudget(
                    stage=stage, maximum_execution_seconds=seconds, maximum_cleanup_seconds=30
                )
                for stage, seconds in zip(STAGES, (60, 600, 60, 14400, 3600, 600), strict=True)
            ),
            finalization_maximum_seconds=60,
            maximum_lineages=1,
            maximum_claims=1,
            claim_window_days=7,
            terminal_window_days=30,
            source_change_transfer_permitted=False,
            ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
            procedure_pin=pin,
            coordinator_pin=pin,
            consumer_contract_pin=pin,
            measurement_contract_pin=pin,
            pre_result_inventory_scope="complete-evidence-and-pre-result-stage-roots-v1",
        )
        self.policy = AcceptancePolicy.seal(
            contract_version="historical-state-acceptance-policy-v1",
            scope=scope,
            exception_identity=scope.exception_identity,
            lineage_plan=plan,
            predecessor_proposal_pin=pin,
            scope_evidence_manifest_pin=pin,
            authority_source_contract_pin=pin,
            claim_window_days=7,
            terminal_window_days=30,
            maximum_claims=1,
            revocation_is_terminal=True,
            failure_consumes_attempt=True,
            new_discrepancy_is_terminal=True,
            mandatory_predicate_contract="canonical-ISO02-ISO03-QCF17-A-and-B-before-QCF18-all-27-commands-zero-current-delta-v1",
        )
        package = ReviewedPolicyPackage.seal(
            contract_version="reviewed-policy-contract-package-v1",
            policy=owner.store(self.policy, "acceptance_policy_identity"),
            scope_evidence_manifest_pin=pin,
            contract_source_pin=pin,
        )
        decision = b"SYNTHETIC person selected APPROVE for this fixture only"
        authority = b"SYNTHETIC issuer authority fixture only"
        self.approval = OperatorApproval.seal(
            contract_version="historical-state-operator-approval-v1",
            acceptance_policy_identity=self.policy.acceptance_policy_identity,
            exception_identity=scope.exception_identity,
            lineage_plan_identity=plan.lineage_plan_identity,
            candidate=self.candidate,
            scope_evidence_manifest_pin=pin,
            reviewed_package_manifest_pin=BytePin.of(package.to_bytes()),
            issuer_principal="SYNTHETIC-human",
            issuer_authority_pin=BytePin.of(authority),
            decision_artifact_pin=BytePin.of(decision),
            channel_session_reference="SYNTHETIC-authenticated-channel",
            selection="APPROVE",
            effective_utc="2001-01-01T00:00:00Z",
            claim_by_utc="2001-01-08T00:00:00Z",
            expires_utc="2001-01-31T00:00:00Z",
        )
        self.ledger = LineageLedger.provision(
            tmp_path / "SYNTHETIC-claims.sqlite", "SYNTHETIC-ledger"
        )
        authority_ref = owner.save("synthetic-authority:v1", {"label": "SYNTHETIC ONLY"})
        owner.resolution = AuthorityResolution(
            approval_bytes=self.approval.to_bytes(),
            authenticated_principal="SYNTHETIC-human",
            authenticated_channel_session="SYNTHETIC-authenticated-channel",
            decision_artifact_bytes=decision,
            issuer_authority_bytes=authority,
            reviewed_package_manifest_bytes=package.to_bytes(),
            originating_policy_disposition="PERMITTED_BY_PROSPECTIVE_REPLACEMENT",
            originating_or_replacement_authority=authority_ref,
            campaign_replacement_authority=authority_ref,
            project_contract_authority=authority_ref,
            qualification_execution_authority=authority_ref,
            execution_candidate=self.candidate,
            execution_lineage_plan_identity=plan.lineage_plan_identity,
            execution_effective_utc="2001-01-01T00:00:00Z",
            execution_expires_utc="2001-01-31T00:00:00Z",
            execution_stage_budgets=plan.stage_budgets,
            execution_finalization_maximum_seconds=plan.finalization_maximum_seconds,
            approved_policy_identity=self.policy.acceptance_policy_identity,
            current_candidate=self.candidate,
            current_lineage_plan_identity=plan.lineage_plan_identity,
            current_production_inventory_identity="production-python-modules:v1:sha256:" + "d" * 64,
            ledger_id="SYNTHETIC-ledger",
            ledger_path=self.ledger.path,
            state="ACTIVE",
            state_reference="SYNTHETIC-fresh-authority-state",
            now_utc="2001-01-02T00:00:00Z",
        )
        self.allocation = LineageAllocation.seal(
            contract_version="historical-state-lineage-allocation-v1",
            approval_identity=self.approval.approval_identity,
            acceptance_policy_identity=self.policy.acceptance_policy_identity,
            exception_identity=scope.exception_identity,
            lineage_plan=plan,
            candidate=self.candidate,
            stages=tuple(
                StageAllocation(
                    stage=stage,
                    run_id="synthetic-" + str(i),
                    root=str(tmp_path / ("SYNTHETIC-stage-" + str(i))),
                )
                for i, stage in enumerate(STAGES)
            ),
            additive_baseline_version="SYNTHETIC-additive-baseline-v1",
            pre_result_evidence_root=str(tmp_path / "SYNTHETIC-pre-result-evidence"),
        )
        payload = self.allocation.model_dump(mode="json")
        payload["contract_version"] = "historical-state-qualification-lineage-v1"
        self.claim = LineageClaim.seal(
            **payload,
            claimed_at_utc=owner.resolution.now_utc,
            state="CLAIMED_ATTEMPT_CONSUMED",
            attempt_ordinal=1,
        )
        owner.allocation = self.allocation
        self.verifier = PolicyQualifiedVerifier(owner=owner, ledger=self.ledger)
        self.bridge = self.raw_comparison("ISO02", bridge=True)

    def raw_comparison(self, stage, *, bridge=False):
        refs = []
        names = []
        for phase in ("before", "after"):
            if bridge and phase == "before":
                refs.append(self.policy.scope.after.artifact)
                names.append(self.policy.scope.after.observation_id)
                continue
            observation_phase = "before" if bridge else phase
            name = "SYNTHETIC-new-" + stage + "-" + observation_phase
            ref = raw_inventory(
                self.owner, phase=observation_phase, attributes=0x10, elapsed=3.0, equal=True
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
            names.append(name)
            refs.append(ref)
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

    def reserve(self):
        self.claim = self.verifier.claim(
            self.policy.to_bytes(),
            self.approval.to_bytes(),
            self.allocation.to_bytes(),
            new_start_bridge=self.bridge.to_bytes(),
        )
        return self.claim


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "SYNTHETIC-only-unused-token")
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "0")
    return SyntheticCase(tmp_path)


def test_synthetic_eligibility_requires_a_trusted_owner(synthetic):
    with pytest.raises(ValueError, match="authenticated"):
        PolicyQualifiedVerifier().eligibility(synthetic.policy, synthetic.approval)
    synthetic.verifier.eligibility(synthetic.policy.to_bytes(), synthetic.approval.to_bytes())
    with pytest.raises(ValueError, match="missing"):
        synthetic.ledger.events(synthetic.claim)


def test_exact_human_challenge_creates_no_approval(synthetic):
    answers = iter([synthetic.policy.acceptance_policy_identity, "YES"])
    assert (
        confirm_policy_decision_challenge(
            synthetic.policy.acceptance_policy_identity, input_fn=lambda _: next(answers)
        )
        is None
    )
    with pytest.raises(ValueError):
        confirm_policy_decision_challenge(
            synthetic.policy.acceptance_policy_identity, input_fn=lambda _: "yes"
        )


def canonical_model(model, payload, namespace, identity_field):
    """Build synthetic canonical evidence, including declared defaults, then validate."""
    payload = dict(payload)
    for name, field in model.model_fields.items():
        if name not in payload and not field.is_required():
            payload[name] = copy.deepcopy(field.default)
    payload.pop(identity_field, None)
    payload[identity_field] = content_identity(namespace, payload)
    return model.model_validate_json(canonical_bytes(payload))


def synthetic_execution(case, *, guest_pass=True):
    from tests.test_iso02_offline_runtime_bundle import _build_request
    from tests.test_iso03_windows_sandbox_qualification import (
        _safe_historical_report_bytes,
        _successful_process_topology,
    )
    from tests.test_iso04_reporting_durability import _process_result, _validated_export
    from tradingagents.offline_runtime_bundle import (
        BUNDLE_MANIFEST_FILENAME,
        OfflineBundleSourceBinding,
        build_offline_runtime_bundle,
    )
    from tradingagents.windows_sandbox_acceptance import (
        Ticket06AcceptanceStaticBindings,
        build_ticket06_acceptance_result,
        build_ticket06_command_ledger,
        build_ticket06_completion_marker,
        build_ticket06_control_environment,
        validate_ticket06_acceptance_export,
    )
    from tradingagents.windows_sandbox_qualification import (
        ISO03HostQualificationReport,
        ISO04SandboxProcessEvidence,
        ISO04Ticket06AcceptanceReport,
        ISO04Ticket06OperationalEvidence,
        ProtectedHostInventory,
        ProtectedRootInventory,
        ProtectedWriterQuiescenceContract,
        ProtectedWriterQuiescenceEvidence,
        compare_protected_host_inventories,
    )
    from tradingagents.windows_sandbox_runner import (
        ProtectedObjectKind,
        ProtectedRootRole,
        ReviewedSourceIdentity,
        ReviewedSourceManifest,
        build_environment_policy,
        build_guest_layout,
        build_sealed_runtime_bundle_input,
    )

    owner, plan = case.owner, case.policy.lineage_plan
    owner.evidence_root = Path(case.claim.pre_result_evidence_root)
    owner.evidence_root.mkdir()
    for stage in case.claim.stages:
        Path(stage.root).mkdir()
    for raw in tuple(owner.artifacts.values()):
        (owner.evidence_root / (sha256(raw).hexdigest() + ".json")).write_bytes(raw)
    owner.store(case.claim, "lineage_identity")
    owner.store(case.approval, "approval_identity")
    source = canonical_model(
        ReviewedSourceIdentity,
        {
            "commit_id": case.candidate.commit,
            "tree_hash": case.candidate.tree,
            "git_object_format": "sha1",
            "commit_object_sha256": "1" * 64,
            "archive_sha256": "2" * 64,
            "source_manifest_identity": "windows-sandbox-source-manifest:v1:sha256:" + "3" * 64,
            "tracked_file_count": 1,
            "run_live_environment": [
                "RUN_LIVE_CUSTOM_TESTS",
                "RUN_LIVE_DEEPSEEK_TESTS",
                "RUN_LIVE_ENVIRONMENT",
                "RUN_LIVE_PROVIDER_TESTS",
                "RUN_LIVE_TUSHARE_TESTS",
            ],
        },
        "windows-sandbox-reviewed-source:v1",
        "source_input_identity",
    )
    request = _build_request(Path(case.claim.stages[0].root) / "SYNTHETIC-ISO02")
    manifest_payload = strict_json(request.reviewed_source_manifest.read_bytes())
    manifest_payload["run_live_environment"] = list(source.run_live_environment)
    manifest = canonical_model(
        ReviewedSourceManifest,
        manifest_payload,
        "windows-sandbox-source-manifest:v1",
        "source_manifest_identity",
    )
    manifest_raw = canonical_bytes(manifest.model_dump(mode="json")) + b"\n"
    request.reviewed_source_manifest.write_bytes(manifest_raw)
    archive = io.BytesIO()
    tracked_paths = {
        "pyproject.toml": request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": request.runtime_artifact_lock,
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": request.expected_dependency_lock,
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": request.bootstrap_script,
        "scripts/windows_sandbox/verify_offline_bundle.py": request.verifier_script,
    }
    with tarfile.open(fileobj=archive, mode="w") as stream:
        for entry in manifest.entries:
            data = tracked_paths[entry.relative_path].read_bytes()
            info = tarfile.TarInfo(entry.relative_path)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    archive_raw = archive.getvalue()
    commit_raw = ("tree " + case.candidate.tree + "\n\nSYNTHETIC commit fixture\n").encode()
    source_payload = source.model_dump(mode="json")
    source_payload.update(
        source_manifest_identity=manifest.source_manifest_identity,
        tracked_file_count=len(manifest.entries),
        archive_sha256=sha256(archive_raw).hexdigest(),
        commit_object_sha256=sha256(commit_raw).hexdigest(),
    )
    source = canonical_model(
        ReviewedSourceIdentity,
        source_payload,
        "windows-sandbox-reviewed-source:v1",
        "source_input_identity",
    )
    owner.source_root = Path(case.claim.stages[0].root) / "SYNTHETIC-source-input"
    owner.source_root.mkdir()
    (owner.source_root / "source-manifest.json").write_bytes(manifest_raw)
    (owner.source_root / "reviewed-source.tar").write_bytes(archive_raw)
    (owner.source_root / "reviewed-commit.object").write_bytes(commit_raw)
    bundle_source = OfflineBundleSourceBinding(
        contract_version=source.contract_version,
        commit_id=source.commit_id,
        tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
    )
    request = replace(request, source=bundle_source)
    built = build_offline_runtime_bundle(request)
    owner.bundle_root = request.destination
    runtime = build_sealed_runtime_bundle_input(
        built.manifest,
        manifest_sha256=sha256(
            (request.destination / BUNDLE_MANIFEST_FILENAME).read_bytes()
        ).hexdigest(),
    )
    layout = build_guest_layout()
    environment = build_ticket06_control_environment(
        build_environment_policy(
            run_live_environment=source.run_live_environment,
            layout=layout,
        ).variables
    )
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    assert ledger.ledger_identity == plan.ledger_identity
    source_ref = owner.store(source, "source_input_identity")
    runtime_ref = owner.store(runtime, "runtime_bundle_contract_identity")
    raw03 = strict_json(
        _safe_historical_report_bytes(
            run_id=case.claim.stages[1].run_id,
            command_ledger_identity=plan.ledger_identity,
        )
    )
    roots = []
    for role in ProtectedRootRole:
        roots.append(
            canonical_model(
                ProtectedRootInventory,
                {
                    "role": role.value,
                    "object_kind": ProtectedObjectKind.DIRECTORY.value,
                    "root_path_identity": "host-path:sha256:" + "6" * 64,
                    "file_count": 0,
                    "byte_length": 0,
                    "files": [],
                },
                "windows-sandbox-protected-root-inventory:v1",
                "inventory_identity",
            ).model_dump(mode="json")
        )
    host = canonical_model(
        ProtectedHostInventory,
        {
            "protected_root_manifest_identity": raw03["static_bindings"][
                "protected_root_manifest_identity"
            ],
            "immutable_verifier_identity": raw03["static_bindings"]["immutable_verifier_identity"],
            "roots": roots,
            "files": [],
            "file_count": 0,
            "byte_length": 0,
            "semantic_inventory": {"SYNTHETIC": "unchanged"},
            "semantic_inventory_identity": content_identity(
                "ticket06-immutable-semantic-inventory:v1", {"SYNTHETIC": "unchanged"}
            ),
        },
        "windows-sandbox-protected-host-inventory:v1",
        "inventory_identity",
    )
    host_ref = owner.store(host, "inventory_identity")
    delta = compare_protected_host_inventories(host, host)
    quiescence_contract = canonical_model(
        ProtectedWriterQuiescenceContract,
        {},
        "windows-sandbox-protected-writer-quiescence:v1",
        "contract_identity",
    )
    quiescence = canonical_model(
        ProtectedWriterQuiescenceEvidence,
        {
            "contract": quiescence_contract.model_dump(mode="json"),
            "sentinel_inventory_identity": host.inventory_identity,
            "immediate_before_inventory_identity": host.inventory_identity,
            "prelaunch_delta": delta.model_dump(mode="json"),
        },
        "windows-sandbox-protected-writer-quiescence-evidence:v1",
        "evidence_identity",
    )
    common = {
        "source_commit_id": source.commit_id,
        "source_tree_hash": source.tree_hash,
        "source_input_identity": source.source_input_identity,
        "source_manifest_identity": source.source_manifest_identity,
        "source_archive_sha256": source.archive_sha256,
        "runtime_bundle_manifest_sha256": runtime.bundle_manifest_sha256,
        "runtime_bundle_contract_identity": runtime.runtime_bundle_contract_identity,
        "runtime_bundle_identity": runtime.bundle_manifest_identity,
    }
    raw03["static_bindings"].update(common, host_before_inventory_identity=host.inventory_identity)
    from tradingagents.windows_sandbox_acceptance import Ticket06AcceptanceStructuralQualification

    structure = canonical_model(
        Ticket06AcceptanceStructuralQualification,
        {
            **raw03["static_bindings"]["acceptance_runner_structure"],
            "environment_policy_identity": environment.policy_identity,
        },
        "ticket06-acceptance-runner-structure:v2",
        "qualification_identity",
    )
    raw03["static_bindings"]["acceptance_runner_structure"] = structure.model_dump(mode="json")
    raw03["guest_result"].update(raw03["static_bindings"])
    raw03["guest_result"]["result_identity"] = content_identity(
        "windows-sandbox-boundary-qualification-result:v1",
        {k: v for k, v in raw03["guest_result"].items() if k != "result_identity"},
    )
    topology = [p.model_dump(mode="json") for p in _successful_process_topology()]
    raw03.update(
        status="pass",
        actual_sandbox_launch=True,
        sandbox_returncode=0,
        sandbox_termination_confirmed=True,
        sandbox_process_observed=topology,
        sandbox_process_ids=[p["process_id"] for p in topology],
        host_before_inventory_identity=host.inventory_identity,
        host_after_inventory_identity=host.inventory_identity,
        host_delta=delta.model_dump(mode="json"),
        protected_writer_quiescence=quiescence.model_dump(mode="json"),
        semantic_before=host.semantic_inventory,
        semantic_after=host.semantic_inventory,
        guest_export_identity="synthetic-iso03-export:v1:sha256:" + "7" * 64,
        failure_reason=None,
    )
    iso03 = canonical_model(
        ISO03HostQualificationReport, raw03, "iso03-host-qualification-report:v1", "report_identity"
    )
    iso03_ref = owner.store(iso03, "report_identity")
    binding = acceptance_binding(case.policy, case.approval, case.claim)
    baseline = QualifiedBaseline.seal(
        contract_version="policy-qualified-qcf17-baseline-v1",
        binding=binding,
        candidate=case.candidate,
        additive_baseline_version=case.claim.additive_baseline_version,
        iso03_report=iso03_ref,
        host_inventory=host_ref,
        ledger_identity=plan.ledger_identity,
        result_schema_identity=plan.result_schema_identity,
    )
    baseline_ref = owner.store(baseline)
    base_export = _validated_export(Path(case.claim.stages[3].root), guest_pass=guest_pass)
    guest_payload = base_export.result.model_dump(mode="json")
    guest_payload["static"].update(
        common,
        frozen_baseline_identity=baseline.identity,
        boundary_qualification_identity=iso03.report_identity,
        command_ledger_identity=ledger.ledger_identity,
        environment_policy_identity=environment.policy_identity,
        runner_input_identity=structure.runner_input_identity,
        protected_root_manifest_identity=host.protected_root_manifest_identity,
    )
    guest = build_ticket06_acceptance_result(
        static=Ticket06AcceptanceStaticBindings.model_validate(guest_payload["static"]),
        ledger=ledger,
        command_results=base_export.result.command_results,
        live_environment=environment.variables,
    )
    assert guest.static.command_ledger_identity == plan.ledger_identity
    guest_ref = owner.store(guest, "result_identity")
    completion = build_ticket06_completion_marker(
        result=guest, result_bytes=guest.canonical_json_bytes() + b"\n"
    )
    completion_ref = owner.store(completion, "marker_identity")
    owner.export_root = Path(case.claim.stages[3].root) / "results-export"
    (owner.export_root / "result.json").write_bytes(owner.read_artifact(guest_ref))
    (owner.export_root / "completion.json").write_bytes(owner.read_artifact(completion_ref))
    export = validate_ticket06_acceptance_export(owner.export_root, expected=guest.static)
    process = ISO04SandboxProcessEvidence.create(
        run_id=case.claim.stages[3].run_id,
        acceptance_control_identity=guest.static.control_identity,
        process=_process_result(0),
    )
    op = ISO04Ticket06OperationalEvidence.create(
        sandbox_process=process,
        export=export,
        host_before_inventory_identity=host.inventory_identity,
        host_after_inventory_identity=host.inventory_identity,
    )
    iso04_ref = owner.store(ISO04Ticket06AcceptanceReport.create(op), "report_identity")
    raw_comparisons = (case.raw_comparison("ISO03"), case.raw_comparison("ISO04_TICKET06"))
    for obj in (case.bridge, *raw_comparisons):
        owner.store(obj)
    nodes = []

    def node(kind, predecessors, artifacts):
        result = BoundEvidence.seal(
            contract_version="policy-qualified-pre-result-evidence-v1",
            kind=kind,
            binding=binding,
            candidate=case.candidate,
            predecessors=predecessors,
            artifacts=artifacts,
            recorded_at_utc=owner.resolution.now_utc,
        )
        nodes.append(result)
        owner.stages[(case.claim.lineage_identity, kind)] = result.to_bytes()
        return owner.store(result)

    first = node("ISO02", (), (source_ref, runtime_ref))
    second = node(
        "ISO03",
        (first,),
        (iso03_ref, host_ref, host_ref, reference(case.bridge), reference(raw_comparisons[0])),
    )
    third = node("QCF17", (second,), (baseline_ref,))
    fourth = node(
        "ISO04_TICKET06",
        (third,),
        (iso04_ref, host_ref, host_ref, guest_ref, completion_ref, reference(raw_comparisons[1])),
    )
    stage_refs = (first, second, third, fourth)
    review_refs = []
    review_nodes = []
    for axis, kind in (("STANDARDS", "REVIEW_STANDARDS"), ("SPEC", "REVIEW_SPEC")):
        review = IndependentReviewRecord.seal(
            contract_version="policy-qualified-independent-review-v1",
            binding=binding,
            candidate=case.candidate,
            axis=axis,
            input_evidence=stage_refs,
            reviewer_principal="SYNTHETIC-independent-" + axis,
            qualification_critical=0,
            qualification_important=0,
            historical_important_unresolved=1,
        )
        review_ref = owner.store(review)
        owner.principals[review_ref.identity] = review.reviewer_principal
        review_refs.append(review_ref)
        review_nodes.append(node(kind, stage_refs, (review_ref,)))
    closing_raw = case.raw_comparison("CLOSURE_HANDOFF")
    owner.store(closing_raw)
    host_observation = "SYNTHETIC-new-post-review-host-after"
    owner.observations[host_observation] = ObservationResolution(
        host_observation,
        host_ref,
        plan.measurement_contract_pin,
        case.claim.lineage_identity,
        case.policy.lineage_plan.lineage_plan_identity,
        case.allocation.allocation_identity,
        "CLOSURE_HANDOFF",
        "after",
        "NEW_QUALIFICATION",
        owner.resolution.now_utc,
    )
    roots = (
        EvidenceInventoryRoot(role="EVIDENCE", path=case.claim.pre_result_evidence_root),
        *(
            EvidenceInventoryRoot(role=stage.stage, path=stage.root)
            for stage in case.claim.stages[:5]
        ),
    )
    inventory = capture_pre_result_evidence_inventory(
        binding=binding,
        candidate=case.candidate,
        roots=roots,
        captured_at_utc=owner.resolution.now_utc,
    )
    # Immutable inventory roots are now closed. Successor artifacts are written
    # only to the separately allocated closure/publication root, outside them.
    owner.evidence_root = None
    owner.publication_root = Path(case.claim.stages[5].root)
    inventory_ref = owner.store(inventory)
    closure = PreResultClosureEvidence.seal(
        contract_version="policy-qualified-pre-result-closure-v1",
        binding=binding,
        candidate=case.candidate,
        reviews=tuple(review_nodes),
        post_review_host_before=host_ref,
        post_review_host_after=host_ref,
        post_review_host_observation=host_observation,
        post_review_raw=closing_raw,
        evidence_inventory=inventory_ref,
        recorded_at_utc=owner.resolution.now_utc,
    )
    closure_ref = owner.store(closure)
    node("PRE_RESULT_CLOSURE", tuple(review_nodes), (closure_ref,))
    return ExecutionEvidence(
        source=source_ref,
        runtime=runtime_ref,
        iso03_report=iso03_ref,
        iso03_before=host_ref,
        iso03_after=host_ref,
        baseline=baseline_ref,
        iso04_report=iso04_ref,
        iso04_before=host_ref,
        iso04_after=host_ref,
        guest_result=guest_ref,
        guest_completion=completion_ref,
        guest_export_identity=export.export_identity,
        new_start_bridge=case.bridge,
        current_raw_comparisons=raw_comparisons,
        pre_result_closure=closure_ref,
        graph=tuple(nodes),
    )


def complete_synthetic_stages(case, execution):
    case.reserve()
    for stage, index in zip(STAGES, (0, 1, 2, 3, 5, 6), strict=True):
        case.verifier.begin_stage(
            case.policy, case.approval, case.claim, stage=stage, new_start_bridge=case.bridge
        )
        case.verifier.complete_stage(
            case.policy, case.approval, case.claim, stage=stage, evidence=execution.graph[index]
        )


def synthetic_result(case):
    execution = synthetic_execution(case)
    complete_synthetic_stages(case, execution)
    result = case.verifier.finalize(
        policy=case.policy.to_bytes(),
        approval=case.approval.to_bytes(),
        claim=case.claim.to_bytes(),
        execution=execution.to_bytes(),
    )
    case.owner.store(result, "result_identity")
    receipt = case.verifier.issue_receipt(result.to_bytes())
    envelope = DownstreamEnvelope(
        contract_version="policy-qualified-downstream-envelope-v1",
        core=receipt.core,
        receipt=receipt,
        execution_authorized=False,
    )
    return result, envelope


def test_real_serialization_finalizer_receipt_and_report_preserve_unresolved_history(synthetic):
    result, envelope = synthetic_result(synthetic)
    assert result.status == "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY"
    assert result.history.historical_equal is False
    assert result.history.scope.raw_before == 0x10000010
    assert result.history.scope.raw_after == 0x10
    assert (
        len(
            strict_json(synthetic.owner.read_artifact(result.execution.guest_result))[
                "command_results"
            ]
        )
        == 27
    )
    assert synthetic.verifier.verify_downstream(envelope.to_bytes()) == envelope
    assert synthetic.verifier.report(envelope.to_bytes())["acceptance"] == envelope.model_dump(
        mode="json"
    )
    assert synthetic.ledger.require_terminal(synthetic.claim, envelope.core.result)
    assert envelope.execution_authorized is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "UNKNOWN"),
        ("state", "REVOKED"),
        ("state", "unrecognized"),
        ("authenticated_principal", "SYNTHETIC-impostor"),
        ("authenticated_channel_session", "SYNTHETIC-other-session"),
        ("originating_policy_disposition", "UNKNOWN"),
        ("decision_artifact_bytes", b"SYNTHETIC different decision"),
        ("issuer_authority_bytes", b"SYNTHETIC different issuer"),
        ("reviewed_package_manifest_bytes", b"{}"),
        ("approved_policy_identity", "wrong-policy"),
        ("current_lineage_plan_identity", "wrong-lineage"),
        ("execution_lineage_plan_identity", "wrong-execution-lineage"),
        ("ledger_id", "SYNTHETIC-other-ledger"),
        ("execution_expires_utc", "2001-01-01T12:00:00Z"),
    ],
)
def test_unapproved_unknown_unauthenticated_or_misbound_authority_rejected(synthetic, field, value):
    synthetic.owner.resolution = replace(synthetic.owner.resolution, **{field: value})
    with pytest.raises(ValueError):
        synthetic.reserve()
    with pytest.raises(ValueError, match="missing"):
        synthetic.ledger.events(synthetic.claim)


@pytest.mark.parametrize("field", ["current_candidate", "execution_candidate"])
def test_wrong_candidate_from_trusted_resolution_is_rejected(synthetic, field):
    other = Candidate(commit="f" * 40, tree="e" * 40, parent="d" * 40)
    synthetic.owner.resolution = replace(synthetic.owner.resolution, **{field: other})
    with pytest.raises(ValueError, match="binding"):
        synthetic.reserve()


@pytest.mark.parametrize("selection", ["DRAFT", "NOT_APPROVED", "REJECT", None, True])
def test_serialized_approval_has_no_default_approve(synthetic, selection):
    approval = synthetic.approval.model_dump(mode="json")
    approval["selection"] = selection
    with pytest.raises(ValueError):
        synthetic.verifier.eligibility(synthetic.policy, canonical_bytes(approval))


@pytest.mark.parametrize(
    "field", ["acceptance_policy_identity", "exception_identity", "approval_identity"]
)
def test_wrong_policy_exception_or_approval_in_claim_rejected(synthetic, field):
    claim = reseal(
        LineageAllocation,
        synthetic.allocation,
        **{field: "synthetic-wrong-binding:v1:sha256:" + "0" * 64},
    )
    with pytest.raises(ValueError, match="claim"):
        synthetic.verifier.claim(
            synthetic.policy, synthetic.approval, claim, new_start_bridge=synthetic.bridge
        )


def test_original_pair_cannot_be_replaced_by_a_new_identical_acquisition(synthetic):
    original = synthetic.policy.scope.before
    # Same bytes and numbers, but the trusted acquisition registry says this is
    # a later copy. The saved original observation must remain distinguishable.
    synthetic.owner.originals[original.observation_id] = original.model_copy(
        update={
            "observation_id": "SYNTHETIC-new-identical-copy",
            "original_path": r"D:\SYNTHETIC-NEW\before.json",
        }
    )
    with pytest.raises(ValueError, match="exact retained original"):
        synthetic.reserve()


@pytest.mark.parametrize(
    "field,value", [("root", r"D:\SYNTHETIC-OTHER\build"), ("raw_after", 0x30), ("changed_rows", 2)]
)
def test_additional_historical_path_bit_or_ledger_scope_cannot_use_old_approval(
    synthetic, field, value
):
    scope = reseal(HistoricalExceptionScope, synthetic.policy.scope, **{field: value})
    policy = reseal(
        AcceptancePolicy, synthetic.policy, scope=scope, exception_identity=scope.exception_identity
    )
    with pytest.raises(ValueError):
        synthetic.verifier.eligibility(policy, synthetic.approval)


def test_truncated_or_changed_complete_delta_ledger_is_rejected(synthetic):
    ref = synthetic.policy.scope.complete_delta_ledger
    raw = strict_json(synthetic.owner.read_artifact(ref))
    raw["roots"][0]["changes"] = []
    synthetic.owner.artifacts[(ref.identity, ref.pin.sha256)] = canonical_bytes(raw) + b"\n"
    with pytest.raises(ValueError, match="whole-file"):
        synthetic.reserve()


@pytest.mark.parametrize("attributes", [0x30, 0x10000010, 0x20000010])
def test_new_raw_discrepancy_is_not_covered_by_the_historical_exception(synthetic, attributes):
    old = synthetic.owner.observations[synthetic.bridge.after_observation]
    artifact = raw_inventory(synthetic.owner, phase="before", attributes=attributes, elapsed=3.0)
    synthetic.owner.observations[old.observation_id] = replace(old, artifact=artifact)
    bridge = reseal(RawComparison, synthetic.bridge, after=artifact)
    with pytest.raises(ValueError, match="new raw discrepancy"):
        synthetic.verifier.claim(
            synthetic.policy, synthetic.approval, synthetic.allocation, new_start_bridge=bridge
        )


@pytest.mark.parametrize(
    "when", ["2000-12-31T23:59:59Z", "2001-01-08T00:00:00Z", "2001-01-31T00:00:00Z"]
)
def test_claim_and_terminal_boundaries_use_fixture_clocks(synthetic, when):
    synthetic.owner.resolution = replace(synthetic.owner.resolution, now_utc=when)
    with pytest.raises(ValueError):
        synthetic.reserve()


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_by_utc", "2001-01-09T00:00:00Z"),
        ("expires_utc", "2001-02-01T00:00:00Z"),
        ("effective_utc", "2001-01-01T00:00:00"),
    ],
)
def test_seven_and_thirty_day_policy_cannot_be_extended_or_made_ambiguous(synthetic, field, value):
    with pytest.raises(ValueError):
        reseal(OperatorApproval, synthetic.approval, **{field: value})


def test_consumed_claim_survives_reopen_and_concurrent_claims_have_one_winner(synthetic):
    def attempt(_):
        try:
            synthetic.reserve()
            return "claimed"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count("claimed") == 1
    assert results.count("rejected") == 3
    reopened = LineageLedger(synthetic.ledger.path, "SYNTHETIC-ledger")
    assert reopened.events(synthetic.claim)[0]["kind"] == "CLAIMED"
    with pytest.raises(ValueError, match="consumed"):
        reopened.claim(synthetic.claim)


def test_missing_or_wrong_owner_ledger_is_not_initialized(synthetic):
    path = synthetic.root / "SYNTHETIC-missing.sqlite"
    with pytest.raises(FileNotFoundError):
        LineageLedger(path, "SYNTHETIC-ledger")
    assert not path.exists()
    with pytest.raises(ValueError, match="registration"):
        LineageLedger(synthetic.ledger.path, "wrong-ledger")


def test_failed_mandatory_execution_stops_before_later_stages_and_cannot_finalize(synthetic):
    execution = synthetic_execution(synthetic, guest_pass=False)
    with pytest.raises(ValueError, match="mandatory ISO04"):
        complete_synthetic_stages(synthetic, execution)
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"
    with pytest.raises(ValueError):
        synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=execution,
        )
    with pytest.raises(ValueError):
        synthetic.verifier.begin_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="REVIEWS",
            new_start_bridge=synthetic.bridge,
        )


def test_started_stage_is_consumed_even_if_no_completion_was_saved(synthetic):
    synthetic.reserve()
    synthetic.verifier.begin_stage(
        synthetic.policy,
        synthetic.approval,
        synthetic.claim,
        stage="ISO02",
        new_start_bridge=synthetic.bridge,
    )
    with pytest.raises(ValueError):
        synthetic.verifier.begin_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO02",
            new_start_bridge=synthetic.bridge,
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


@pytest.mark.parametrize("state", ["REVOKED", "EXPIRED"])
def test_terminal_revocation_and_expiry_cannot_be_undone_by_a_later_active_snapshot(
    synthetic, state
):
    _result, envelope = synthetic_result(synthetic)
    old = synthetic.owner.resolution
    changes = {"state": "REVOKED"} if state == "REVOKED" else {"now_utc": "2001-01-31T00:00:00Z"}
    synthetic.owner.resolution = replace(old, **changes)
    with pytest.raises(ValueError):
        synthetic.verifier.verify_downstream(envelope)
    synthetic.owner.resolution = old
    with pytest.raises(ValueError, match="permanently invalidated"):
        synthetic.verifier.verify_downstream(envelope)


def test_no_terminal_result_without_complete_pre_result_closure(synthetic):
    execution = synthetic_execution(synthetic)
    complete_synthetic_stages(synthetic, execution)
    bad = execution.model_dump(mode="json")
    bad["graph"][-1]["predecessors"] = [reference(execution.graph[-1]).model_dump(mode="json")]
    # Even a recomputed graph hash cannot introduce a cycle or a future-result dependency.
    bad["graph"][-1] = reseal(BoundEvidence, bad["graph"][-1]).model_dump(mode="json")
    with pytest.raises(ValueError):
        synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=canonical_bytes(bad),
        )


@pytest.mark.parametrize("change", ["bridge", "comparable", "current_comparison", "wrong_ledger"])
def test_missing_bridge_comparability_or_wrong_ledger_cannot_finalize(synthetic, change):
    execution = synthetic_execution(synthetic)
    complete_synthetic_stages(synthetic, execution)
    bad = execution.model_dump(mode="json")
    if change == "bridge":
        bad.pop("new_start_bridge")
    elif change == "comparable":
        bad["new_start_bridge"]["comparable"] = False
    elif change == "current_comparison":
        bad["current_raw_comparisons"].pop()
    else:
        bad["graph"][0]["binding"]["lineage_plan_identity"] = (
            "synthetic-other-ledger:v1:sha256:" + "0" * 64
        )
        bad["graph"][0] = reseal(BoundEvidence, bad["graph"][0]).model_dump(mode="json")
    with pytest.raises(ValueError):
        synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=canonical_bytes(bad),
        )


def conditional_consumers(case, envelope):
    from tests.test_plan15_production_binding import _blueprint, _config
    from tests.test_runtime_protection_contract import attempt_inputs
    from tradingagents.qualified_routing_composition import (
        ConditionalProductionQualifiedRoutingSourceBinding,
        seal_production_qualified_routing_preparation,
        validate_production_qualified_routing_preparation,
    )
    from tradingagents.runtime_protection import (
        bind_production_preparation,
        validate_conditional_attempt,
    )

    blueprint = _blueprint(_config(case.root / "SYNTHETIC-runtime"))
    source = ConditionalProductionQualifiedRoutingSourceBinding(
        contract_version="production-policy-qualified-routing-source-binding-v1",
        commit_id=case.candidate.commit,
        tree_hash=case.candidate.tree,
        production_inventory_identity=case.owner.resolution.current_production_inventory_identity,
        qualified_source_identity=envelope.core.result.identity,
        acceptance=envelope,
    )
    prepare = seal_production_qualified_routing_preparation
    prep = prepare(
        blueprint=blueprint,
        qualified_source=source.model_dump(mode="json"),
        current_source=source.model_dump(mode="json"),
        policy_verifier=case.verifier,
    )
    validated = validate_production_qualified_routing_preparation(
        canonical_bytes(prep.model_dump(mode="json")),
        blueprint=blueprint,
        qualified_source=source,
        current_source=source,
        policy_verifier=case.verifier,
    )
    assert validated == prep
    inputs = attempt_inputs()
    for name in (
        "source_commit",
        "source_tree",
        "qualified_source_identity",
        "production_preparation_identity",
    ):
        inputs.pop(name)
    attempt = bind_production_preparation(
        canonical_bytes(prep.model_dump(mode="json")), policy_verifier=case.verifier, **inputs
    )
    assert (
        validate_conditional_attempt(
            canonical_bytes(attempt.model_dump(mode="json")), policy_verifier=case.verifier
        )
        == attempt
    )
    return blueprint, source, prep, attempt, inputs


def test_actual_conditional_preparation_and_attempt_propagate_the_whole_binding(synthetic):
    _result, envelope = synthetic_result(synthetic)
    before = synthetic.owner.resolutions
    _blueprint, source, prep, attempt, _inputs = conditional_consumers(synthetic, envelope)
    assert (
        source.acceptance
        == prep.qualified_source.acceptance
        == attempt.inputs.acceptance
        == envelope
    )
    assert synthetic.owner.resolutions > before + 3
    assert prep.execution_authorized is False and attempt.execution_authorized is False
    assert attempt.contract_version == "policy-qualified-runtime-protection-attempt-v1"


@pytest.mark.parametrize(
    "consumer",
    [
        "result",
        "receipt",
        "report",
        "preparation",
        "preparation_verifier",
        "attempt_adapter",
        "attempt_verifier",
    ],
)
def test_missing_caveat_rejected_at_every_intended_consumer(synthetic, consumer):
    from tradingagents.qualified_routing_composition import (
        _canonical_digest,
        seal_production_qualified_routing_preparation,
        validate_production_qualified_routing_preparation,
    )
    from tradingagents.runtime_protection import (
        bind_production_preparation,
        validate_conditional_attempt,
    )

    result, envelope = synthetic_result(synthetic)
    blueprint, source, prep, attempt, inputs = conditional_consumers(synthetic, envelope)
    bad = envelope.model_dump(mode="json")
    bad["core"].pop("history")
    with pytest.raises(ValueError):
        if consumer == "result":
            value = result.model_dump(mode="json")
            value.pop("history")
            value["result_identity"] = content_identity(
                "policy-qualified-execution-result:v1",
                {k: v for k, v in value.items() if k != "result_identity"},
            )
            synthetic.verifier.verify_result(canonical_bytes(value))
        elif consumer == "receipt":
            synthetic.verifier.verify_downstream(canonical_bytes(bad))
        elif consumer == "report":
            synthetic.verifier.report(canonical_bytes(bad))
        elif consumer == "preparation":
            source_payload = source.model_dump(mode="json")
            source_payload["acceptance"] = bad
            seal_production_qualified_routing_preparation(
                blueprint=blueprint,
                qualified_source=source_payload,
                current_source=source_payload,
                policy_verifier=synthetic.verifier,
            )
        elif consumer in {"preparation_verifier", "attempt_adapter"}:
            value = prep.model_dump(mode="json")
            value["qualified_source"]["acceptance"] = bad
            value["preparation_identity"] = (
                "production-policy-qualified-routing-preparation:v1:"
                + _canonical_digest({k: v for k, v in value.items() if k != "preparation_identity"})
            )
            if consumer == "preparation_verifier":
                validate_production_qualified_routing_preparation(
                    canonical_bytes(value),
                    blueprint=blueprint,
                    qualified_source=source,
                    current_source=source,
                    policy_verifier=synthetic.verifier,
                )
            else:
                bind_production_preparation(
                    canonical_bytes(value), policy_verifier=synthetic.verifier, **inputs
                )
        else:
            value = attempt.model_dump(mode="json")
            value["inputs"]["acceptance"] = bad
            validate_conditional_attempt(canonical_bytes(value), policy_verifier=synthetic.verifier)


def test_conditional_to_legacy_downgrade_and_unaware_models_reject(synthetic):
    from tradingagents.qualified_routing_composition import (
        ProductionQualifiedRoutingPreparation,
        ProductionQualifiedRoutingSourceBinding,
        seal_production_qualified_routing_preparation,
    )
    from tradingagents.runtime_protection import AttemptInputs, ProtectionAttempt

    _result, envelope = synthetic_result(synthetic)
    blueprint, source, prep, attempt, _inputs = conditional_consumers(synthetic, envelope)
    for model, value in (
        (ProductionQualifiedRoutingSourceBinding, source),
        (ProductionQualifiedRoutingPreparation, prep),
        (AttemptInputs, attempt.inputs),
        (ProtectionAttempt, attempt),
    ):
        with pytest.raises(ValueError):
            model.model_validate_json(canonical_bytes(value.model_dump(mode="json")))
        with pytest.raises(ValueError):
            model.model_validate(value)
    # Rewrapping an opaque digest cannot match the readiness owner's complete
    # current conditional source, even if it is syntactically a legacy v1 string.
    forged = {
        "commit_id": source.commit_id,
        "tree_hash": source.tree_hash,
        "production_inventory_identity": source.production_inventory_identity,
        "qualified_source_identity": source.qualified_source_identity.replace(
            "policy-qualified-execution-result:v1",
            "qualified-source-ready-for-ticket07-preparation:v1",
        ),
    }
    with pytest.raises(ValueError, match="current source"):
        seal_production_qualified_routing_preparation(
            blueprint=blueprint,
            qualified_source=forged,
            current_source=source,
            policy_verifier=synthetic.verifier,
        )


def test_authentication_is_required_again_at_each_consumer(synthetic):
    from tradingagents.qualified_routing_composition import (
        seal_production_qualified_routing_preparation,
    )
    from tradingagents.runtime_protection import bind_production_preparation, derive_attempt

    result, envelope = synthetic_result(synthetic)
    blueprint, source, prep, attempt, inputs = conditional_consumers(synthetic, envelope)
    with pytest.raises(ValueError):
        seal_production_qualified_routing_preparation(
            blueprint=blueprint, qualified_source=source, current_source=source
        )
    with pytest.raises(ValueError):
        bind_production_preparation(prep, **inputs)
    with pytest.raises(ValueError):
        derive_attempt(attempt.inputs)
    synthetic.owner.resolution = replace(synthetic.owner.resolution, state="UNKNOWN")
    for use in (
        lambda: synthetic.verifier.verify_result(result.to_bytes()),
        lambda: synthetic.verifier.issue_receipt(result.to_bytes()),
        lambda: synthetic.verifier.report(envelope),
        lambda: bind_production_preparation(prep, policy_verifier=synthetic.verifier, **inputs),
        lambda: derive_attempt(attempt.inputs, policy_verifier=synthetic.verifier),
    ):
        with pytest.raises(ValueError):
            use()


def test_missing_or_forged_separate_receipt_is_rejected(synthetic):
    from tradingagents.policy_qualified_contract import PostResultAcceptanceReceipt

    _result, envelope = synthetic_result(synthetic)
    missing = envelope.model_dump(mode="json")
    missing.pop("receipt")
    with pytest.raises(ValueError):
        synthetic.verifier.verify_downstream(canonical_bytes(missing))
    receipt = reseal(
        PostResultAcceptanceReceipt,
        envelope.receipt,
        authority_state_reference="SYNTHETIC-fabricated-check",
    )
    with pytest.raises(ValueError, match="fabricated"):
        synthetic.verifier.verify_downstream(envelope.model_copy(update={"receipt": receipt}))


def test_file_hashes_are_distinct_from_content_identities_and_framing_is_bound(synthetic):
    result, envelope = synthetic_result(synthetic)
    assert envelope.core.result.pin.sha256 != result.result_identity.rsplit(":", 1)[1]
    other_framing = result.to_bytes() + b"\n"
    assert PolicyQualifiedExecutionResult.read(other_framing) == result
    with pytest.raises(ValueError, match="durable terminal"):
        synthetic.verifier.verify_result(other_framing)
    wrong = envelope.model_dump(mode="json")
    wrong["core"]["result"]["pin"]["sha256"] = result.result_identity.rsplit(":", 1)[1]
    with pytest.raises(ValueError):
        synthetic.verifier.verify_downstream(canonical_bytes(wrong))


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_json_trust_boundary_rejects_duplicate_keys_and_nonfinite_values(raw):
    with pytest.raises(ValueError):
        PolicyQualifiedExecutionResult.read(raw)


def test_closed_schema_descriptor_has_acyclic_identity_and_complete_consumer_entries(tmp_path):
    from tradingagents.policy_qualified_contract import contract_descriptor

    descriptor = contract_descriptor()
    (tmp_path / "policy-contract.json").write_bytes(canonical_bytes(descriptor) + b"\n")
    identity = descriptor.pop("contract_identity")
    assert identity == content_identity("policy-qualified-result-contract:v1", descriptor)
    assert len(descriptor["entry_points"]) == 16
    assert "history" in descriptor["schema"]["$defs"]["PolicyQualifiedExecutionResult"]["required"]
    assert "receipt" not in descriptor["schema"]["$defs"]["ResultBindingCore"]["properties"]
    assert "receipt" in descriptor["schema"]["$defs"]["DownstreamEnvelope"]["required"]


@pytest.mark.parametrize("artifact", ["source", "runtime"])
def test_canonical_source_and_full_bundle_verifiers_reject_changed_fixture_bytes(
    synthetic, artifact
):
    execution = synthetic_execution(synthetic)
    synthetic.reserve()
    synthetic.verifier.begin_stage(
        synthetic.policy,
        synthetic.approval,
        synthetic.claim,
        stage="ISO02",
        new_start_bridge=synthetic.bridge,
    )
    target = (
        synthetic.owner.source_root / "reviewed-source.tar"
        if artifact == "source"
        else next((synthetic.owner.bundle_root / "wheelhouse").glob("*.whl"))
    )
    target.write_bytes(target.read_bytes() + b"SYNTHETIC tamper")
    with pytest.raises(ValueError):
        synthetic.verifier.complete_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO02",
            evidence=execution.graph[0],
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


def test_missing_reviewed_predecessor_file_fails_activation(synthetic):
    synthetic.owner.pinned.clear()
    with pytest.raises(ValueError, match="predecessor"):
        synthetic.reserve()


def test_repeated_finalization_or_receipt_does_not_rewrite_terminal_outcome(synthetic):
    result, envelope = synthetic_result(synthetic)
    before = synthetic.ledger.events(synthetic.claim)
    with pytest.raises(ValueError):
        synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=result.execution,
        )
    with pytest.raises(ValueError):
        synthetic.verifier.issue_receipt(result.to_bytes())
    assert synthetic.ledger.events(synthetic.claim) == before
    assert synthetic.verifier.verify_downstream(envelope) == envelope


def test_wrong_ledger_in_claim_is_not_the_approved_lineage_plan(synthetic):
    plan = reseal(
        LineagePlan,
        synthetic.policy.lineage_plan,
        ledger_identity="ticket06-sandbox-command-ledger:v1:sha256:" + "0" * 64,
    )
    claim = reseal(LineageAllocation, synthetic.allocation, lineage_plan=plan)
    with pytest.raises(ValueError, match="claim"):
        synthetic.verifier.claim(
            synthetic.policy, synthetic.approval, claim, new_start_bridge=synthetic.bridge
        )


def replace_synthetic_closure(case, execution, closure_ref):
    last = reseal(BoundEvidence, execution.graph[-1], artifacts=(closure_ref,))
    case.owner.stages[(case.claim.lineage_identity, "PRE_RESULT_CLOSURE")] = last.to_bytes()
    case.owner.store(last)
    return execution.model_copy(
        update={"pre_result_closure": closure_ref, "graph": (*execution.graph[:-1], last)}
    )


@pytest.mark.parametrize(
    "missing",
    [
        "evidence_inventory",
        "post_review_host_after",
        "post_review_raw",
        "post_review_host_observation",
    ],
)
def test_required_post_review_closure_evidence_cannot_be_omitted(synthetic, missing):
    execution = synthetic_execution(synthetic)
    raw = strict_json(synthetic.owner.read_artifact(execution.pre_result_closure))
    raw.pop(missing)
    raw["identity"] = content_identity(
        "policy-qualified-pre-result-closure:v1", {k: v for k, v in raw.items() if k != "identity"}
    )
    ref = synthetic.owner.store_raw(canonical_bytes(raw) + b"\n", raw["identity"])
    execution = replace_synthetic_closure(synthetic, execution, ref)
    with pytest.raises(ValueError):
        complete_synthetic_stages(synthetic, execution)
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


@pytest.mark.parametrize("kind", ["canonical", "raw"])
def test_post_review_discrepancy_is_fatal_even_after_successful_iso04(synthetic, kind):
    from tradingagents.windows_sandbox_qualification import ProtectedHostInventory

    execution = synthetic_execution(synthetic)
    closure = PreResultClosureEvidence.read(
        synthetic.owner.read_artifact(execution.pre_result_closure)
    )
    if kind == "canonical":
        host = strict_json(synthetic.owner.read_artifact(closure.post_review_host_after))
        host["semantic_inventory"] = {"SYNTHETIC": "changed-after-reviews"}
        host["semantic_inventory_identity"] = content_identity(
            "ticket06-immutable-semantic-inventory:v1", host["semantic_inventory"]
        )
        changed = canonical_model(
            ProtectedHostInventory,
            host,
            "windows-sandbox-protected-host-inventory:v1",
            "inventory_identity",
        )
        ref = synthetic.owner.store(changed, "inventory_identity")
        observation = synthetic.owner.observations[closure.post_review_host_observation]
        synthetic.owner.observations[observation.observation_id] = replace(
            observation, artifact=ref
        )
        closure = reseal(PreResultClosureEvidence, closure, post_review_host_after=ref)
    else:
        raw = raw_inventory(
            synthetic.owner, phase="after", attributes=0x30, elapsed=3.0, equal=True
        )
        observed = synthetic.owner.observations[closure.post_review_raw.after_observation]
        synthetic.owner.observations[observed.observation_id] = replace(observed, artifact=raw)
        comparison = reseal(RawComparison, closure.post_review_raw, after=raw)
        closure = reseal(PreResultClosureEvidence, closure, post_review_raw=comparison)
    execution = replace_synthetic_closure(synthetic, execution, synthetic.owner.store(closure))
    with pytest.raises(ValueError, match="discrepancy"):
        complete_synthetic_stages(synthetic, execution)
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


def test_post_review_inventory_detects_new_unlisted_evidence(synthetic):
    execution = synthetic_execution(synthetic)
    (Path(synthetic.claim.stages[4].root) / "SYNTHETIC-extra-after-review.txt").write_text(
        "new evidence"
    )
    with pytest.raises(ValueError, match="complete evidence inventory"):
        complete_synthetic_stages(synthetic, execution)


def test_post_review_inventory_cannot_omit_a_required_file_even_after_removal(synthetic):
    from tradingagents.policy_qualified_contract import PreResultEvidenceInventory

    execution = synthetic_execution(synthetic)
    closure = PreResultClosureEvidence.read(
        synthetic.owner.read_artifact(execution.pre_result_closure)
    )
    inventory = PreResultEvidenceInventory.read(
        synthetic.owner.read_artifact(closure.evidence_inventory)
    )
    removed_pin = reference(synthetic.policy, "acceptance_policy_identity").pin
    target = Path(synthetic.claim.pre_result_evidence_root) / (removed_pin.sha256 + ".json")
    target.unlink()  # Adversarial mutation of this test's synthetic copy only.
    inventory = reseal(
        PreResultEvidenceInventory,
        inventory,
        entries=tuple(x for x in inventory.entries if x.pin != removed_pin),
    )
    closure = reseal(
        PreResultClosureEvidence, closure, evidence_inventory=synthetic.owner.store(inventory)
    )
    execution = replace_synthetic_closure(synthetic, execution, synthetic.owner.store(closure))
    with pytest.raises(ValueError, match="omits required"):
        complete_synthetic_stages(synthetic, execution)


def test_post_review_inventory_cannot_suppress_inaccessible_directories(synthetic, monkeypatch):
    import tradingagents.policy_qualified_results as results

    execution = synthetic_execution(synthetic)
    original = results.os.scandir
    denied = Path(synthetic.claim.stages[4].root)

    def guarded_scan(path):
        if Path(path) == denied:
            raise PermissionError("SYNTHETIC unreadable evidence directory")
        return original(path)

    monkeypatch.setattr(results.os, "scandir", guarded_scan)
    with pytest.raises(PermissionError, match="SYNTHETIC"):
        complete_synthetic_stages(synthetic, execution)
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


@pytest.mark.parametrize("change", ["earlier", "wrong_stage", "missing"])
def test_post_review_host_inventory_requires_a_fresh_registered_acquisition(synthetic, change):
    execution = synthetic_execution(synthetic)
    closure = PreResultClosureEvidence.read(
        synthetic.owner.read_artifact(execution.pre_result_closure)
    )
    key = closure.post_review_host_observation
    if change == "missing":
        synthetic.owner.observations.pop(key)
    elif change == "earlier":
        synthetic.owner.observations[key] = replace(
            synthetic.owner.observations[key], observed_at_utc="2001-01-01T00:00:00Z"
        )
    else:
        synthetic.owner.observations[key] = replace(
            synthetic.owner.observations[key], stage="ISO04_TICKET06"
        )
    with pytest.raises(ValueError):
        complete_synthetic_stages(synthetic, execution)


@pytest.mark.parametrize("axis", ["EVIDENCE", "CONTRACT"])
def test_implementation_review_axes_cannot_substitute_for_qualification_reviews(synthetic, axis):
    execution = synthetic_execution(synthetic)
    record = IndependentReviewRecord.read(
        synthetic.owner.read_artifact(execution.graph[4].artifacts[0])
    )
    assert record.axis == "STANDARDS" and execution.graph[5].kind == "REVIEW_SPEC"
    with pytest.raises(ValueError):
        reseal(IndependentReviewRecord, record, axis=axis)


def test_additional_null_historical_field_is_not_misclassified_as_attributes_only(tmp_path):
    case = SyntheticCase(tmp_path, additional_historical_field=True)
    with pytest.raises(ValueError, match="additional historical field"):
        case.reserve()


def test_missing_source_bytes_terminate_the_claim_without_retry(synthetic):
    execution = synthetic_execution(synthetic)
    (synthetic.owner.source_root / "source-manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        complete_synthetic_stages(synthetic, execution)
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


@pytest.mark.parametrize("change", ["stage_id", "stage_root", "baseline", "inventory_root"])
def test_claim_must_match_canonical_allocation_authority(synthetic, change):
    payload = synthetic.allocation.model_dump(mode="json")
    if change == "stage_id":
        payload["stages"][0]["run_id"] = "synthetic-other-stage"
    elif change == "stage_root":
        payload["stages"][0]["root"] += "-other"
    elif change == "baseline":
        payload["additive_baseline_version"] = "SYNTHETIC-unallocated-baseline"
    else:
        payload["pre_result_evidence_root"] += "-other"
    claim = reseal(LineageAllocation, payload)
    with pytest.raises(ValueError, match="canonical one-lineage allocation"):
        synthetic.verifier.claim(
            synthetic.policy, synthetic.approval, claim, new_start_bridge=synthetic.bridge
        )


@pytest.mark.parametrize("reused", ["root", "run_id", "baseline"])
def test_new_policy_cannot_reuse_consumed_stage_or_baseline_allocations(synthetic, reused):
    synthetic.reserve()
    plan = reseal(
        LineagePlan,
        synthetic.policy.lineage_plan,
        procedure_pin=BytePin.of(b"SYNTHETIC separately proposed future procedure"),
    )
    payload = synthetic.claim.model_dump(mode="json")
    payload.update(
        approval_identity="synthetic-future-approval:v1:sha256:" + "4" * 64,
        lineage_plan=plan.model_dump(mode="json"),
    )
    for stage in payload["stages"]:
        if reused != "root":
            stage["root"] += "-future"
        if reused != "run_id":
            stage["run_id"] += "-future"
    if reused != "root":
        payload["pre_result_evidence_root"] += "-future"
    if reused != "baseline":
        payload["additive_baseline_version"] += "-future"
    future = reseal(LineageClaim, payload)
    with pytest.raises(ValueError, match="already"):
        synthetic.ledger.claim(future)
    assert len(synthetic.ledger.events(synthetic.claim)) == 1
    with pytest.raises(ValueError, match="missing"):
        synthetic.ledger.events(future)


def begin_synthetic_stage(case, execution, index):
    case.reserve()
    for stage, node_index in zip(STAGES[:index], (0, 1, 2, 3, 5, 6)[:index], strict=True):
        case.verifier.begin_stage(
            case.policy, case.approval, case.claim, stage=stage, new_start_bridge=case.bridge
        )
        case.verifier.complete_stage(
            case.policy,
            case.approval,
            case.claim,
            stage=stage,
            evidence=execution.graph[node_index],
        )
    case.verifier.begin_stage(
        case.policy, case.approval, case.claim, stage=STAGES[index], new_start_bridge=case.bridge
    )


def advance_authority_on_read(case, reference_to_read, **changes):
    original = case.owner.read_artifact
    fired = []

    def read(ref):
        raw = original(ref)
        if ref == reference_to_read and not fired:
            case.owner.resolution = replace(case.owner.resolution, **changes)
            fired.append(True)
        return raw

    case.owner.read_artifact = read
    return fired


def test_iso03_wrong_source_fails_completion_before_any_later_stage(synthetic):
    from tradingagents.windows_sandbox_qualification import ISO03HostQualificationReport

    execution = synthetic_execution(synthetic)
    begin_synthetic_stage(synthetic, execution, 1)
    raw = strict_json(synthetic.owner.read_artifact(execution.iso03_report))
    wrong = "windows-sandbox-reviewed-source:v1:sha256:" + "f" * 64
    raw["static_bindings"]["source_input_identity"] = wrong
    raw["guest_result"]["source_input_identity"] = wrong
    raw["guest_result"]["result_identity"] = content_identity(
        "windows-sandbox-boundary-qualification-result:v1",
        {k: v for k, v in raw["guest_result"].items() if k != "result_identity"},
    )
    report = canonical_model(
        ISO03HostQualificationReport, raw, "iso03-host-qualification-report:v1", "report_identity"
    )
    assert report.status.value == "pass"  # Canonical local predicates alone pass.
    report_ref = synthetic.owner.store(report, "report_identity")
    node = reseal(
        BoundEvidence, execution.graph[1], artifacts=(report_ref, *execution.graph[1].artifacts[1:])
    )
    synthetic.owner.stages[(synthetic.claim.lineage_identity, "ISO03")] = node.to_bytes()
    with pytest.raises(ValueError, match="source/runtime binding"):
        synthetic.verifier.complete_stage(
            synthetic.policy, synthetic.approval, synthetic.claim, stage="ISO03", evidence=node
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"
    assert not any(x["kind"] == "COMPLETE:ISO03" for x in synthetic.ledger.events(synthetic.claim))
    with pytest.raises(ValueError):
        synthetic.verifier.begin_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="QCF17",
            new_start_bridge=synthetic.bridge,
        )


def test_wrong_predecessor_fails_at_stage_completion(synthetic):
    execution = synthetic_execution(synthetic)
    begin_synthetic_stage(synthetic, execution, 1)
    node = reseal(BoundEvidence, execution.graph[1], predecessors=())
    synthetic.owner.stages[(synthetic.claim.lineage_identity, "ISO03")] = node.to_bytes()
    with pytest.raises(ValueError, match="predecessor"):
        synthetic.verifier.complete_stage(
            synthetic.policy, synthetic.approval, synthetic.claim, stage="ISO03", evidence=node
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


def test_baseline_must_bind_the_completed_iso03_before_iso04_admission(synthetic):
    execution = synthetic_execution(synthetic)
    begin_synthetic_stage(synthetic, execution, 2)
    baseline = QualifiedBaseline.read(synthetic.owner.read_artifact(execution.baseline))
    wrong = EvidenceRef(
        identity="iso03-host-qualification-report:v1:sha256:" + "e" * 64,
        pin=baseline.iso03_report.pin,
    )
    baseline = reseal(QualifiedBaseline, baseline, iso03_report=wrong)
    node = reseal(BoundEvidence, execution.graph[2], artifacts=(synthetic.owner.store(baseline),))
    synthetic.owner.stages[(synthetic.claim.lineage_identity, "QCF17")] = node.to_bytes()
    with pytest.raises(ValueError, match="baseline binding"):
        synthetic.verifier.complete_stage(
            synthetic.policy, synthetic.approval, synthetic.claim, stage="QCF17", evidence=node
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


def test_source_change_after_completion_blocks_the_next_start(synthetic):
    execution = synthetic_execution(synthetic)
    synthetic.reserve()
    synthetic.verifier.begin_stage(
        synthetic.policy,
        synthetic.approval,
        synthetic.claim,
        stage="ISO02",
        new_start_bridge=synthetic.bridge,
    )
    synthetic.verifier.complete_stage(
        synthetic.policy,
        synthetic.approval,
        synthetic.claim,
        stage="ISO02",
        evidence=execution.graph[0],
    )
    source = synthetic.owner.source_root / "reviewed-source.tar"
    source.write_bytes(source.read_bytes() + b"SYNTHETIC changed between stages")
    with pytest.raises(ValueError):
        synthetic.verifier.begin_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO03",
            new_start_bridge=synthetic.bridge,
        )
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"
    assert not any(x["kind"] == "START:ISO03" for x in synthetic.ledger.events(synthetic.claim))


def test_claim_timestamp_is_minted_after_checks_with_an_advancing_fixture_clock(synthetic):
    original = synthetic.owner.resolve
    initial = utc(synthetic.owner.resolution.now_utc)
    calls = []

    def advancing(identity):
        response = original(identity)
        calls.append(identity)
        return replace(response, now_utc=(initial + timedelta(seconds=len(calls))).isoformat())

    synthetic.owner.resolve = advancing
    claim = synthetic.reserve()
    assert utc(claim.claimed_at_utc) > initial
    assert synthetic.ledger.events(claim)[0]["recorded_at"] == claim.claimed_at_utc
    assert claim.to_allocation() == synthetic.allocation
    bridge_observation = synthetic.owner.observations[synthetic.bridge.after_observation]
    assert bridge_observation.lineage_identity is None
    assert bridge_observation.allocation_identity == claim.allocation_identity


def test_claim_cannot_backdate_across_seven_day_deadline_during_evidence_reads(synthetic):
    old = synthetic.owner.resolution
    fired = advance_authority_on_read(
        synthetic,
        synthetic.policy.scope.complete_delta_ledger,
        now_utc=synthetic.approval.claim_by_utc,
    )
    with pytest.raises(ValueError, match="claim clock/window"):
        synthetic.reserve()
    assert fired
    synthetic.owner.resolution = old
    with pytest.raises(ValueError, match="permanently invalidated"):
        synthetic.reserve()


@pytest.mark.parametrize(
    "operation", ["begin", "complete", "finalize", "receipt", "verify", "report"]
)
def test_later_expiry_snapshot_blocks_each_lifecycle_or_acceptance_operation(synthetic, operation):
    execution = synthetic_execution(synthetic)
    if operation in {"begin", "complete"}:
        begin_synthetic_stage(synthetic, execution, 0)
        synthetic.verifier.complete_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO02",
            evidence=execution.graph[0],
        )
        if operation == "complete":
            synthetic.verifier.begin_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO03",
                new_start_bridge=synthetic.bridge,
            )
    else:
        complete_synthetic_stages(synthetic, execution)
    result = envelope = None
    if operation in {"receipt", "verify", "report"}:
        result = synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=execution,
        )
        synthetic.owner.store(result, "result_identity")
        if operation == "report":
            receipt = synthetic.verifier.issue_receipt(result.to_bytes())
            envelope = DownstreamEnvelope(
                contract_version="policy-qualified-downstream-envelope-v1",
                core=receipt.core,
                receipt=receipt,
                execution_authorized=False,
            )
    fired = advance_authority_on_read(
        synthetic, execution.source, now_utc=synthetic.approval.expires_utc
    )
    with pytest.raises(ValueError, match="expired|invalidated"):
        if operation == "begin":
            synthetic.verifier.begin_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO03",
                new_start_bridge=synthetic.bridge,
            )
        elif operation == "complete":
            synthetic.verifier.complete_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO03",
                evidence=execution.graph[1],
            )
        elif operation == "finalize":
            synthetic.verifier.finalize(
                policy=synthetic.policy,
                approval=synthetic.approval,
                claim=synthetic.claim,
                execution=execution,
            )
        elif operation == "receipt":
            synthetic.verifier.issue_receipt(result.to_bytes())
        elif operation == "verify":
            synthetic.verifier.verify_result(result.to_bytes())
        else:
            synthetic.verifier.report(envelope)
    assert fired
    if operation == "begin":
        assert not any(x["kind"] == "START:ISO03" for x in synthetic.ledger.events(synthetic.claim))
    if operation == "receipt":
        assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "TERMINAL"


@pytest.mark.parametrize("margin", [0, 1])
def test_remaining_stage_cleanup_and_closure_budgets_must_fit_exclusively(synthetic, margin):
    synthetic.reserve()
    remaining = synthetic.policy.lineage_plan.finalization_maximum_seconds + sum(
        x.maximum_execution_seconds + x.maximum_cleanup_seconds
        for x in synthetic.policy.lineage_plan.stage_budgets
    )
    when = utc(synthetic.approval.expires_utc) - timedelta(seconds=remaining + margin)
    synthetic.owner.resolution = replace(synthetic.owner.resolution, now_utc=when.isoformat())
    if margin == 0:
        with pytest.raises(ValueError, match="budgets do not fit"):
            synthetic.verifier.begin_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO02",
                new_start_bridge=synthetic.bridge,
            )
        assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"
    else:
        synthetic.verifier.begin_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO02",
            new_start_bridge=synthetic.bridge,
        )
        assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "START:ISO02"


def test_separate_execution_window_must_fit_canonical_remaining_budgets(synthetic):
    synthetic.owner.resolution = replace(
        synthetic.owner.resolution, execution_expires_utc="2001-01-02T00:01:00Z"
    )
    with pytest.raises(ValueError, match="budgets do not fit"):
        synthetic.reserve()


def test_declared_budgets_must_match_the_authenticated_execution_procedure(synthetic):
    budgets = list(synthetic.owner.resolution.execution_stage_budgets)
    budgets[3] = budgets[3].model_copy(update={"maximum_execution_seconds": 1})
    synthetic.owner.resolution = replace(
        synthetic.owner.resolution, execution_stage_budgets=tuple(budgets)
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        synthetic.reserve()


def test_stage_cannot_complete_after_its_approved_execution_and_cleanup_budget(synthetic):
    execution = synthetic_execution(synthetic)
    begin_synthetic_stage(synthetic, execution, 0)
    budget = synthetic.policy.lineage_plan.stage_budgets[0]
    late = utc(synthetic.owner.resolution.now_utc) + timedelta(
        seconds=budget.maximum_execution_seconds + budget.maximum_cleanup_seconds + 1
    )
    synthetic.owner.resolution = replace(synthetic.owner.resolution, now_utc=late.isoformat())
    with pytest.raises(ValueError, match="canonical execution/cleanup budget"):
        synthetic.verifier.complete_stage(
            synthetic.policy,
            synthetic.approval,
            synthetic.claim,
            stage="ISO02",
            evidence=execution.graph[0],
        )


def test_terminal_verification_cannot_exceed_its_approved_budget(synthetic):
    execution = synthetic_execution(synthetic)
    complete_synthetic_stages(synthetic, execution)
    late = utc(synthetic.owner.resolution.now_utc) + timedelta(
        seconds=synthetic.policy.lineage_plan.finalization_maximum_seconds + 1
    )
    fired = advance_authority_on_read(synthetic, execution.source, now_utc=late.isoformat())
    with pytest.raises(ValueError, match="terminal verification exceeded"):
        synthetic.verifier.finalize(
            policy=synthetic.policy,
            approval=synthetic.approval,
            claim=synthetic.claim,
            execution=execution,
        )
    assert fired
    assert synthetic.ledger.events(synthetic.claim)[-1]["kind"] == "FAILED"


@pytest.mark.parametrize("operation", ["start", "complete", "terminal", "receipt"])
def test_concurrent_durable_revocation_blocks_lifecycle_commit_atomically(synthetic, operation):
    """The authority read succeeds, then a concurrent owner invalidates storage."""
    execution = result = None
    if operation == "start":
        synthetic.reserve()
    else:
        execution = synthetic_execution(synthetic)
        if operation == "complete":
            begin_synthetic_stage(synthetic, execution, 0)
        else:
            complete_synthetic_stages(synthetic, execution)
            if operation == "receipt":
                result = synthetic.verifier.finalize(
                    policy=synthetic.policy,
                    approval=synthetic.approval,
                    claim=synthetic.claim,
                    execution=execution,
                )
                synthetic.owner.store(result, "result_identity")
    paused, resume = Event(), Event()
    target = {"start": "START:ISO02", "complete": "COMPLETE:ISO02", "terminal": "TERMINAL"}.get(
        operation
    )

    class ScheduledLedger(LineageLedger):
        # Control scheduling at the storage seam, without replacing validation
        # or SQL. Both operations use the actual disposable SQLite database.
        def transition(self, claim, *, kind, at, evidence=None):
            if kind == target:
                paused.set()
                assert resume.wait(10), "SYNTHETIC race was not released"
            return super().transition(claim, kind=kind, at=at, evidence=evidence)

        def record_receipt(self, claim, result, receipt, *, at):
            if operation == "receipt":
                paused.set()
                assert resume.wait(10), "SYNTHETIC race was not released"
            return super().record_receipt(claim, result, receipt, at=at)

    ledger = ScheduledLedger(synthetic.ledger.path, synthetic.ledger.ledger_id)
    verifier = PolicyQualifiedVerifier(owner=synthetic.owner, ledger=ledger)

    def pending_operation():
        if operation == "start":
            return verifier.begin_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO02",
                new_start_bridge=synthetic.bridge,
            )
        if operation == "complete":
            return verifier.complete_stage(
                synthetic.policy,
                synthetic.approval,
                synthetic.claim,
                stage="ISO02",
                evidence=execution.graph[0],
            )
        if operation == "terminal":
            return verifier.finalize(
                policy=synthetic.policy,
                approval=synthetic.approval,
                claim=synthetic.claim,
                execution=execution,
            )
        return verifier.issue_receipt(result.to_bytes())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pending_operation)
        try:
            assert paused.wait(10), "SYNTHETIC operation did not reach its commit boundary"
            synthetic.owner.resolution = replace(synthetic.owner.resolution, state="REVOKED")
            with pytest.raises(ValueError, match="revoked"):
                synthetic.verifier.eligibility(synthetic.policy, synthetic.approval)
        finally:
            resume.set()
        with pytest.raises(ValueError, match="permanently invalidated"):
            future.result(timeout=10)
    events = synthetic.ledger.events(synthetic.claim)
    forbidden = "RECEIPT" if operation == "receipt" else target
    assert not any(event["kind"] == forbidden for event in events)
    if operation != "receipt":
        assert events[-1]["kind"] == "FAILED"  # Failure recording is still permitted.
    else:
        assert events[-1]["kind"] == "TERMINAL"  # Earlier immutable result is retained.
