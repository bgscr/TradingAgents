"""Standalone staged interpreter/WinPS/preflight/source-intake rehearsal, no setup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "native-build", "python-root", "dependencies"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external fixture preparation output required")
    sys.path[:0] = [str(source), str(args.dependencies)]
    from tradingagents.policy_evidence_client import stage_artifact
    from tradingagents.policy_evidence_contract import ArtifactDescriptor
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, EvidenceRef, canonical_bytes

    bound_evidence_process(seconds=1200)
    output.mkdir()
    unrelated = output / "unrelated-工作目录"
    unrelated.mkdir()
    commands = []

    def run(command, expected=0, cwd=unrelated, input=None, timeout=180):
        result = subprocess.run(
            list(map(str, command)),
            cwd=cwd,
            input=input,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        commands.append(
            {
                "argv": list(map(str, command)),
                "cwd": str(cwd),
                "exit": result.returncode,
                "stdout": result.stdout.decode("utf-8", "replace"),
                "stderr": result.stderr.decode("utf-8", "replace"),
            }
        )
        (output / "commands.json").write_text(
            json.dumps(commands, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if expected is None:
            assert result.returncode != 0, commands[-1]
        else:
            assert result.returncode == expected, commands[-1]
        return result.stdout

    def pin(path):
        return BytePin.of(path.read_bytes()).model_dump(mode="json")

    factory = runpy.run_path(str(source / "tests/fixtures/policy_owner/local_owner_fixture.py"))[
        "SyntheticNativeBoundary"
    ]
    fixture = factory(output / "SETUP-INPUT-FIXTURE", "test-20300101-901")
    registry = output / "registry.json"
    registry.write_bytes(fixture.owner.registry.value.to_bytes())
    role = output / "role.json"
    role.write_bytes(fixture.role.to_bytes())
    references = [
        fixture.policy.scope.before.artifact,
        fixture.policy.scope.after.artifact,
        fixture.policy.scope.complete_delta_ledger,
        fixture.policy.scope.original_blocker,
    ]
    catalog = output / "catalog.json"
    catalog.write_bytes(
        canonical_bytes(
            {
                "contract_version": "policy-owner-setup-artifact-catalog-v1",
                "artifacts": [
                    {
                        "reference": ref.model_dump(mode="json"),
                        "path": str(fixture.owner.root / "artifacts" / (ref.pin.sha256 + ".blob")),
                    }
                    for ref in references
                ],
                "execution_authorized": False,
            }
        )
    )
    bundle = output / "FIXTURE-SETUP-NOT-FOR-HUMAN"
    run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            source / "scripts/policy_owner/prepare_policy_owner_setup.py",
            "--output",
            bundle,
            "--native-build",
            args.native_build,
            "--python-root",
            args.python_root,
            "--dependencies",
            args.dependencies,
            "--registry",
            registry,
            "--role-proposal",
            role,
            "--artifact-catalog",
            catalog,
            "--instance",
            fixture.owner.anchor["instance"],
            "--operator-sid",
            fixture.role.operator_sid,
            "--account-name",
            fixture.role.observed_account_name,
            "--include-inert-fixture",
        ]
    )
    depot = bundle / "depot"
    python = depot / "python/python.exe"
    proposal = bundle / "SETUP-PROPOSAL.json"
    preflight = [
        python,
        "-I",
        "-B",
        "-S",
        depot / "source/scripts/policy_owner/preflight_evidence.py",
        "--depot",
        depot,
        "--worker-manifest",
        depot / "worker-manifest.json",
        "--worker-manifest-sha256",
        pin(depot / "worker-manifest.json")["sha256"],
        "--native-owner",
        depot / "bin/PolicyOwner.exe",
        "--native-owner-sha256",
        pin(depot / "bin/PolicyOwner.exe")["sha256"],
        "--instance",
        fixture.owner.anchor["instance"],
        "--setup-proposal",
        proposal,
        "--setup-proposal-sha256",
        pin(proposal)["sha256"],
    ]
    run([*preflight, "--output", output / "preflight.json"])
    run(
        [*preflight, "--check-registered-capacity", "--output", output / "must-not-exist.json"],
        expected=None,
    )
    assert not (output / "must-not-exist.json").exists()
    wrong = preflight.copy()
    wrong[wrong.index("--instance") + 1] = "test-20300101-902"
    run([*wrong, "--output", output / "must-not-exist.json"], expected=None)
    assert not (output / "must-not-exist.json").exists()
    wrong = preflight.copy()
    wrong[wrong.index("--native-owner-sha256") + 1] = "0" * 64
    run([*wrong, "--output", output / "must-not-exist.json"], expected=None)
    # Staged helper's real WinPS/.NET loading and exact consumer/library bindings.
    deployment = output / "FIXTURE-DEPLOYMENT.json"
    deployment.write_bytes(
        canonical_bytes(
            {
                "contract_version": "registered-test-deployment-v1",
                "instance": fixture.owner.anchor["instance"],
                "owner_root": "C:\\ProgramData\\TA-PolicyOwner-" + fixture.owner.anchor["instance"],
                "setup_proposal": {"path": str(proposal), **pin(proposal)},
                "operational": False,
                "execution_authorized": False,
            }
        )
    )
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    native_args = [
        powershell,
        "-NoProfile",
        "-STA",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        depot / "source/scripts/policy_owner/initialize_test_namespace.ps1",
        "-Instance",
        fixture.owner.anchor["instance"],
        "-DeploymentPath",
        deployment,
        "-DeploymentSha256",
        pin(deployment)["sha256"],
        "-ConsumerPath",
        depot / "bin/ConditionalRuntimeProtectionHost.exe",
        "-ConsumerSha256",
        pin(depot / "bin/ConditionalRuntimeProtectionHost.exe")["sha256"],
        "-PinLibraryPath",
        depot / "source/scripts/policy_owner/pinned_setup_native.ps1",
        "-PinLibrarySha256",
        pin(depot / "source/scripts/policy_owner/pinned_setup_native.ps1")["sha256"],
        "-ValidateOnly",
        "-StagedFixture",
    ]
    run(native_args)
    bad = native_args.copy()
    bad[bad.index("-Instance") + 1] = "test-20300101-902"
    run(bad, expected=None)
    run(
        [depot / "bin/PolicyOwner.exe", "transfer-worker-v2", fixture.owner.anchor["instance"]],
        expected=None,
    )
    # Real producer and verifier with this exact delivered interpreter/dependencies.
    repository = output / "DISPOSABLE-GIT"
    repository.mkdir()

    def git(*values):
        return run(
            [
                "git",
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Isolated Source Fixture",
                "-c",
                "user.email=fixture.invalid@example.invalid",
                *values,
            ],
            cwd=repository,
        )

    git("init", "--initial-branch=fixture")
    (repository / "README").write_bytes(b"SYNTHETIC SOURCE ONLY\n")
    git("add", "--", "README")
    git("commit", "-m", "Synthetic parent")
    (repository / "tradingagents").mkdir()
    (repository / "tradingagents/__init__.py").write_bytes(b'"""Not production code."""\n')
    git("add", "--", "tradingagents")
    git("commit", "-m", "Synthetic current source")
    commit, tree, parent = (
        git("show", "-s", "--format=%H%n%T%n%P", "HEAD").decode().strip().splitlines()
    )
    candidate = {"commit": commit, "tree": tree, "parent": parent}
    candidate_path = output / "candidate.json"
    candidate_path.write_bytes(canonical_bytes(candidate))
    inventory_path = output / "source-inventory.json"
    acquire = [
        python,
        "-I",
        "-B",
        "-S",
        depot / "source/scripts/policy_owner/acquire_evidence.py",
        "--fixture-only",
    ]
    selected_git = Path(shutil.which("git"))
    run(
        [
            *acquire,
            "produce-source",
            "--repository-root",
            repository,
            "--candidate",
            candidate_path,
            "--output",
            inventory_path,
            "--git-executable",
            selected_git,
            "--git-sha256",
            hashlib.sha256(selected_git.read_bytes()).hexdigest(),
        ]
    )
    run(
        [
            *acquire,
            "verify-source",
            "--repository-root",
            repository,
            "--candidate",
            candidate_path,
            "--inventory",
            inventory_path,
        ]
    )
    inventory = json.loads(inventory_path.read_bytes())
    authority = factory(output / "SOURCE-INTAKE-BOUNDARY", "test-20300101-902", candidate=candidate)
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=authority.owner.anchor["instance"],
        role_identity=authority.role.identity,
        approval_identity=authority.approval.approval_identity,
        candidate=candidate,
        lineage_plan_identity=authority.approval.lineage_plan_identity,
        acquisition_identity="SYNTHETIC-SOURCE-CAPTURE",
        stage="CURRENT_SOURCE",
        phase="evidence",
        artifact_schema="production-python-modules:v1",
        artifact_identity_field="identity",
        artifact=EvidenceRef(
            identity=inventory["identity"], pin=BytePin.of(inventory_path.read_bytes())
        ),
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )

    def native(operation, payload):
        return json.loads(
            run(
                [
                    args.native_build / "EvidenceTransferContracts.exe",
                    authority.owner.root,
                    authority.owner.anchor["instance"],
                ],
                input=canonical_bytes({"operation": operation, "payload": payload}),
            )
        )

    intake = stage_artifact(native, inventory_path, descriptor)
    authority.owner.now_utc = datetime.now(timezone.utc).isoformat()
    authority.owner.anchor["operational"] = (
        True  # INVALID synthetic object only; no registered instance
    )
    try:
        authority.owner.validate_authentication_document("REGISTER_EVIDENCE", intake)
    except ValueError as error:
        assert "fixture" in str(error)
    else:
        raise AssertionError("TEST production inventory could authenticate operationally")
    authority.owner.anchor["operational"] = False
    authority.apply("REGISTER_EVIDENCE", intake)
    current = {
        "contract_version": "policy-owner-evidence-intake-v2",
        "kind": "current-source",
        "key": "current",
        "value": {
            "candidate": candidate,
            "lineage_plan_identity": authority.approval.lineage_plan_identity,
            "production_inventory_identity": inventory["identity"],
            "inventory_reference": descriptor.artifact.model_dump(mode="json"),
        },
    }
    authority.apply("REGISTER_EVIDENCE", current)
    assert authority.owner._verified_current_inventory(current["value"]).candidate.commit == commit
    dependency = depot / "dependencies/pydantic/version.py"
    with dependency.open("ab") as stream:
        stream.write(b"\n# DISPOSABLE STAGED DEPENDENCY TAMPER VECTOR\n")
    run([*preflight, "--output", output / "must-not-exist.json"], expected=None)
    assert not (output / "must-not-exist.json").exists()
    report = {
        "status": "PASS_DELIVERED_PREFLIGHT_WINPS_SOURCE_INTAKE_FIXTURES",
        "source_candidate": candidate,
        "source_inventory_identity": inventory["identity"],
        "actual_delivered_interpreter": str(python),
        "preflight_owner_rpc": False,
        "fixture_inventory_rejected_in_operational_mode": True,
        "winps_native_assembly_preflight": True,
        "private_service_worker_rejected_ordinary_token": True,
        "changed_delivered_dependency_rejected_before_import": True,
        "test_instances": [fixture.owner.anchor["instance"], authority.owner.anchor["instance"]],
        "setup_called": False,
        "enrollment_called": False,
        "native_authentication": False,
        "qualification": False,
        "execution_authorized": False,
    }
    (output / "result.json").write_bytes(canonical_bytes(report))
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
