"""Synthetic data for pre-enrollment verifier execution; never real authority.

Fixture bodies adapted from the existing policy and offline-bundle regression
fixtures. No pytest, mocked imports, human authentication or native execution.
All effects are confined by the invoking validation process to fresh scratch.
"""

from __future__ import annotations

import copy
import io
import json
import tarfile
import zipfile
from collections.abc import MutableMapping
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from packaging.utils import parse_wheel_filename

from tradingagents.offline_runtime_bundle import (
    OfflineBundleSourceBinding,
    OfflineRuntimeBundleBuildRequest,
    build_offline_runtime_bundle,
    verify_offline_runtime_bundle,
)
from tradingagents.policy_coverage_models import read_model
from tradingagents.policy_qualification_lifecycle import LineageLedger
from tradingagents.policy_qualified_contract import (
    STAGES,
    AcceptancePolicy,
    BoundEvidence,
    BytePin,
    Candidate,
    EvidenceRef,
    HistoricalExceptionScope,
    LineageAllocation,
    LineageClaim,
    LineagePlan,
    OperatorApproval,
    OriginalAcquisition,
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
)
from tradingagents.windows_sandbox_runner import (
    ReviewedSourceIdentity,
    ReviewedSourceManifest,
    build_sealed_runtime_bundle_input,
)


def first_stage(root: Path) -> dict:
    """Exercise the real producer and source/runtime verifier on synthetic data."""
    root.mkdir()
    case = SyntheticCase(root)
    case.reserve()
    stage_root = Path(case.claim.stages[0].root)
    stage_root.mkdir()
    request = _build_request(stage_root / "SYNTHETIC-runtime")
    manifest_raw = request.reviewed_source_manifest.read_bytes()
    manifest = ReviewedSourceManifest.model_validate_json(manifest_raw)
    # Canonical evidence is UTF-8 plus LF even on Windows; do not allow the
    # fixture writer's text newline conversion to alter the retained bytes.
    manifest_raw = canonical_bytes(manifest.model_dump(mode="json")) + b"\n"
    request.reviewed_source_manifest.write_bytes(manifest_raw)
    tracked = {
        "pyproject.toml": request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": request.runtime_artifact_lock,
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": request.expected_dependency_lock,
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": request.bootstrap_script,
        "scripts/windows_sandbox/verify_offline_bundle.py": request.verifier_script,
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for entry in manifest.entries:
            raw = tracked[entry.relative_path].read_bytes()
            info = tarfile.TarInfo(entry.relative_path)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    archive_raw = buffer.getvalue()
    commit_raw = ("tree " + case.candidate.tree + "\n\nSYNTHETIC verifier fixture\n").encode()
    source = canonical_model(
        ReviewedSourceIdentity,
        {
            "commit_id": case.candidate.commit,
            "tree_hash": case.candidate.tree,
            "git_object_format": "sha1",
            "commit_object_sha256": sha256(commit_raw).hexdigest(),
            "archive_sha256": sha256(archive_raw).hexdigest(),
            "source_manifest_identity": manifest.source_manifest_identity,
            "tracked_file_count": len(manifest.entries),
            "run_live_environment": [],
        },
        "windows-sandbox-reviewed-source:v1",
        "source_input_identity",
    )
    case.owner.source_root = stage_root / "SYNTHETIC-source-input"
    case.owner.source_root.mkdir()
    (case.owner.source_root / "source-manifest.json").write_bytes(manifest_raw)
    (case.owner.source_root / "reviewed-source.tar").write_bytes(archive_raw)
    (case.owner.source_root / "reviewed-commit.object").write_bytes(commit_raw)
    binding = OfflineBundleSourceBinding(
        contract_version=source.contract_version,
        commit_id=source.commit_id,
        tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
    )
    request = replace(request, source=binding)
    built = build_offline_runtime_bundle(request)
    case.owner.bundle_root = request.destination
    runtime = build_sealed_runtime_bundle_input(
        built.manifest,
        manifest_sha256=sha256(
            (request.destination / "bundle-manifest.json").read_bytes()
        ).hexdigest(),
    )
    node = BoundEvidence.seal(
        contract_version="policy-qualified-pre-result-evidence-v1",
        kind="ISO02",
        binding=acceptance_binding(case.policy, case.approval, case.claim),
        candidate=case.candidate,
        predecessors=(),
        artifacts=(
            case.owner.store(source, "source_input_identity"),
            case.owner.store(runtime, "runtime_bundle_contract_identity"),
        ),
        recorded_at_utc=case.owner.resolution.now_utc,
    )
    case.owner.stages[(case.claim.lineage_identity, "ISO02")] = node.to_bytes()
    case.verifier.begin_stage(
        case.policy, case.approval, case.claim, stage="ISO02", new_start_bridge=case.bridge
    )
    event = case.verifier.complete_stage(
        case.policy, case.approval, case.claim, stage="ISO02", evidence=node
    )
    verified = verify_offline_runtime_bundle(request.destination, expected_source=binding)
    assert verified == built.manifest
    assert case.ledger.require_active(case.claim)[-1]["kind"] == "COMPLETE:ISO02"
    return {"case": case, "source": binding, "manifest": verified, "completion_event": event}


class DiskFixtureArtifacts(MutableMapping):
    """Complete immutable fixture bytes on disk, not a mocked evidence reader."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=False)
        self.index = {}

    def __getitem__(self, key):
        return self.index[key].read_bytes()

    def __setitem__(self, key, value):
        if sha256(value).hexdigest() != key[1]:
            raise ValueError("fixture byte pin mismatch")
        path = self.root / (key[1] + ".blob")
        if path.exists():
            if path.read_bytes() != value:
                raise ValueError("immutable fixture conflict")
        else:
            with path.open("xb") as stream:
                stream.write(value)
        self.index[key] = path

    def __delitem__(self, key):
        raise ValueError("fixture artifacts are preserved")

    def __iter__(self):
        return iter(self.index)

    def __len__(self):
        return len(self.index)


class SyntheticOwner:
    """Deliberately fake authenticated owner; never imported by production code."""

    def __init__(self, artifact_directory=None):
        self.artifacts = (
            {} if artifact_directory is None else DiskFixtureArtifacts(artifact_directory)
        )
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
            or read_model(OperatorApproval, self.resolution.approval_bytes).approval_identity
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
        self._fixture_lineage(lineage_identity)
        return self.export_root

    def source_input_root(self, lineage_identity):
        self._fixture_lineage(lineage_identity)
        return self.source_root

    def runtime_bundle_root(self, lineage_identity):
        self._fixture_lineage(lineage_identity)
        return self.bundle_root

    def _fixture_lineage(self, lineage_identity):
        expected = getattr(self, "coverage_lineage", None)
        if expected is not None:
            assert lineage_identity == expected
        else:
            assert lineage_identity.startswith("historical-state-qualification-lineage:v1:")


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
    template = getattr(owner, "fixture_root_template", None)
    if template is not None:
        # Explicit TEST input extension for production-size/full-capture vectors.
        # No comparator or authority method is substituted.
        roots = [copy.deepcopy(template)]
        roots[0]["entries"][0]["attributes"] = attributes
        if root != r"D:\SYNTHETIC-HISTORY\build":
            roots[0]["path"] = root
        roots[0]["inventory_identity"] = content_identity(
            "crash-protection-historical-root-inventory:v1", roots[0]["entries"]
        )
        root = roots[0]["path"]
    payload = {
        "phase": phase,
        "roots": roots,
        "elapsed_seconds": elapsed,
        "file_count": sum(item["file_count"] for item in roots),
        "unreadable_count": sum(item["unreadable_count"] for item in roots),
        "no_acl_write_or_recovery": True,
    }
    if phase == "after":
        payload.update(equal=equal, changed_roots=[] if equal else [root])
    return owner.save("crash-protection-historical-inventory:v1", payload)


class SyntheticCase:
    def __init__(
        self,
        tmp_path,
        *,
        additional_historical_field=False,
        root_template=None,
        clock_utc=None,
        candidate=None,
        artifact_directory=None,
    ):
        from tradingagents.windows_sandbox_acceptance import (
            TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
            TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        )

        self.root = tmp_path
        self.owner = owner = SyntheticOwner(artifact_directory)
        owner.fixture_root_template = root_template
        if clock_utc is not None:
            fixture_now = utc(clock_utc)
            effective_utc = (fixture_now - timedelta(hours=1)).isoformat()
            claim_by_utc = (fixture_now - timedelta(hours=1) + timedelta(days=7)).isoformat()
            expires_utc = (fixture_now - timedelta(hours=1) + timedelta(days=30)).isoformat()
        else:
            effective_utc, claim_by_utc, expires_utc = (
                "2001-01-01T00:00:00Z",
                "2001-01-08T00:00:00Z",
                "2001-01-31T00:00:00Z",
            )
        self.candidate = (
            Candidate.read(candidate)
            if candidate is not None
            else Candidate(commit="a" * 40, tree="b" * 40, parent="c" * 40)
        )
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
            effective_utc=effective_utc,
            claim_by_utc=claim_by_utc,
            expires_utc=expires_utc,
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
            execution_effective_utc=effective_utc,
            execution_expires_utc=expires_utc,
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
            now_utc=clock_utc or "2001-01-02T00:00:00Z",
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


def canonical_model(model, payload, namespace, identity_field):
    """Build synthetic canonical evidence, including declared defaults, then validate."""
    payload = dict(payload)
    for name, field in model.model_fields.items():
        if name not in payload and not field.is_required():
            payload[name] = copy.deepcopy(field.default)
    payload.pop(identity_field, None)
    payload[identity_field] = content_identity(namespace, payload)
    return model.model_validate_json(canonical_bytes(payload))


def _write_wheel(
    wheelhouse: Path,
    *,
    distribution: str,
    version: str,
    requires: tuple[str, ...] = (),
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
    module_content: str = "",
    metadata_version: str | None = None,
) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    filename = f"{distribution}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    path = wheelhouse / filename
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {metadata_version or version}",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
        "",
    ]
    dist_info = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        members = {
            f"{distribution}/__init__.py": module_content,
            f"{dist_info}/METADATA": "\n".join(metadata),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: iso-02-test\n"
                f"Root-Is-Purelib: true\nTag: {python_tag}-{abi_tag}-{platform_tag}\n"
            ),
        }
        for member, content in members.items():
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, content)
    return path


def _source_binding(*, commit: str = "a" * 40) -> OfflineBundleSourceBinding:
    return OfflineBundleSourceBinding(
        contract_version="windows-sandbox-reviewed-source-v1",
        commit_id=commit,
        tree_hash="b" * 40,
        source_input_identity=(f"windows-sandbox-reviewed-source:v1:sha256:{'c' * 64}"),
        source_manifest_identity=(f"windows-sandbox-source-manifest:v1:sha256:{'d' * 64}"),
        source_archive_sha256="e" * 64,
    )


def _bundle_inputs(
    root: Path,
    *,
    project_dependency: str = "demo==1.0",
    reverse_wheels: bool = False,
) -> dict[str, object]:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    python_installer = inputs / "python-3.13.7-amd64.exe"
    python_installer.write_bytes(b"reviewed-python-installer")
    bootstrap = inputs / "install-runtime.ps1"
    bootstrap.write_text("param()\n", encoding="utf-8")
    verifier = inputs / "verify_bundle.py"
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    portable_git = inputs / "MinGit"
    (portable_git / "cmd").mkdir(parents=True)
    (portable_git / "cmd" / "git.exe").write_bytes(b"reviewed-portable-git")
    (portable_git / "LICENSE.txt").write_text("Git license\n", encoding="utf-8")
    portable_git_archive = inputs / "MinGit-2.45.1-64-bit.zip"
    with zipfile.ZipFile(portable_git_archive, "w") as archive:
        for relative_path in ("LICENSE.txt", "cmd/git.exe"):
            info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, (portable_git / relative_path).read_bytes())
    wheelhouse = inputs / "wheelhouse"
    wheel_specs = (
        ("demo", "1.0", ("dependency==2.0",)),
        ("dependency", "2.0", ()),
        ("pip", "25.2", ()),
        ("wheel", "0.46.3", ()),
    )
    for distribution, version, requires in reversed(wheel_specs) if reverse_wheels else wheel_specs:
        _write_wheel(
            wheelhouse,
            distribution=distribution,
            version=version,
            requires=requires,
        )
    project_metadata = inputs / "pyproject.toml"
    project_metadata.write_text(
        "[project]\n"
        "name='fixture'\n"
        f"dependencies=['{project_dependency}']\n"
        "[project.optional-dependencies]\n"
        "dev=[]\n"
        "pit=[]\n",
        encoding="utf-8",
    )
    artifact_lock = inputs / "iso02-artifact-lock.json"
    python_content = python_installer.read_bytes()
    archive_content = portable_git_archive.read_bytes()
    artifact_lock.write_text(
        json.dumps(
            {
                "contract_version": "windows-offline-runtime-artifact-lock-v1",
                "portable_git": {
                    "archive_byte_length": len(archive_content),
                    "archive_filename": portable_git_archive.name,
                    "archive_sha256": sha256(archive_content).hexdigest(),
                    "required": True,
                    "source_url": "https://example.invalid/MinGit.zip",
                    "version": "2.45.1.windows.1",
                },
                "python": {
                    "architecture": "AMD64",
                    "artifact_byte_length": len(python_content),
                    "artifact_filename": python_installer.name,
                    "artifact_sha256": sha256(python_content).hexdigest(),
                    "implementation": "CPython",
                    "source_url": "https://example.invalid/python.exe",
                    "version": "3.13.7",
                },
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    expected_dependency_lock = inputs / "requirements-win-cp313.txt"
    reviewed_source_manifest = inputs / "source-manifest.json"
    return {
        "python_installer": python_installer,
        "bootstrap_script": bootstrap,
        "verifier_script": verifier,
        "portable_git_root": portable_git,
        "portable_git_archive": portable_git_archive,
        "wheelhouse": wheelhouse,
        "project_metadata": project_metadata,
        "runtime_artifact_lock": artifact_lock,
        "expected_dependency_lock": expected_dependency_lock,
        "reviewed_source_manifest": reviewed_source_manifest,
    }


def _refresh_reviewed_request(
    request: OfflineRuntimeBundleBuildRequest,
    *,
    refresh_dependency_lock: bool = True,
) -> OfflineRuntimeBundleBuildRequest:
    if refresh_dependency_lock:
        wheel_rows = []
        for wheel in request.wheelhouse.glob("*.whl"):
            distribution, version, _build, _tags = parse_wheel_filename(wheel.name)
            wheel_rows.append(
                (
                    str(distribution),
                    f"{distribution}=={version} --hash=sha256:"
                    f"{sha256(wheel.read_bytes()).hexdigest()}",
                )
            )
        request.expected_dependency_lock.write_bytes(
            ("\n".join(line for _distribution, line in sorted(wheel_rows)) + "\n").encode("utf-8")
        )
    tracked_inputs = {
        "pyproject.toml": request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": (request.runtime_artifact_lock),
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": (
            request.expected_dependency_lock
        ),
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": (request.bootstrap_script),
        "scripts/windows_sandbox/verify_offline_bundle.py": request.verifier_script,
    }
    entries = []
    for relative_path, path in sorted(tracked_inputs.items()):
        content = path.read_bytes()
        entries.append(
            {
                "byte_length": len(content),
                "git_mode": "100644",
                "git_object_id": sha256(content).hexdigest()[:40],
                "relative_path": relative_path,
                "sha256": sha256(content).hexdigest(),
            }
        )
    manifest_payload = {
        "commit_id": request.source.commit_id,
        "contract_version": request.source.contract_version,
        "entries": entries,
        "git_object_format": "sha1",
        "run_live_environment": [],
        "tree_hash": request.source.tree_hash,
    }
    manifest_identity = (
        "windows-sandbox-source-manifest:v1:sha256:"
        + sha256(
            json.dumps(
                manifest_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    source = request.source.model_copy(update={"source_manifest_identity": manifest_identity})
    request.reviewed_source_manifest.write_text(
        json.dumps(
            {**manifest_payload, "source_manifest_identity": manifest_identity},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return replace(request, source=source)


def _build_request(
    root: Path,
    *,
    source: OfflineBundleSourceBinding | None = None,
    project_dependency: str = "demo==1.0",
    reverse_wheels: bool = False,
) -> OfflineRuntimeBundleBuildRequest:
    inputs = _bundle_inputs(
        root,
        project_dependency=project_dependency,
        reverse_wheels=reverse_wheels,
    )
    project_metadata = inputs["project_metadata"].read_bytes()
    request = OfflineRuntimeBundleBuildRequest(
        destination=root / "bundle",
        source=source or _source_binding(),
        python_installer=inputs["python_installer"],
        python_version="3.13.7",
        windows_architecture="AMD64",
        wheelhouse=inputs["wheelhouse"],
        dependency_roots=("demo==1.0", "pip==25.2", "wheel==0.46.3")
        if project_dependency == "demo==1.0"
        else tuple(sorted((project_dependency, "pip==25.2", "wheel==0.46.3"))),
        project_metadata_sha256=sha256(project_metadata).hexdigest(),
        bootstrap_script=inputs["bootstrap_script"],
        verifier_script=inputs["verifier_script"],
        portable_git_root=inputs["portable_git_root"],
        reviewed_source_manifest=inputs["reviewed_source_manifest"],
        runtime_artifact_lock=inputs["runtime_artifact_lock"],
        project_metadata=inputs["project_metadata"],
        portable_git_archive=inputs["portable_git_archive"],
        expected_dependency_lock=inputs["expected_dependency_lock"],
    )
    return _refresh_reviewed_request(request)
