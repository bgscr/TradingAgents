"""Pre-authentication fixture checks; never contact an enrolled owner or Hello."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.policy_owner import run_registered_test as driver
from scripts.policy_owner.prepare_test_owner_inputs import prepare
from scripts.policy_owner.registered_test_flow import RegisteredTestCase
from tradingagents.policy_owner_contract import (
    PolicyDecisionProposal,
    QualificationExecutionProposal,
)
from tradingagents.policy_qualified_contract import BytePin, EvidenceRef, content_identity
from tradingagents.windows_policy_owner import RegisteredWindowsPolicyVerifier


@pytest.fixture
def unsigned_case(tmp_path, request):
    instance = getattr(request, "param", driver.INSTANCE)
    inputs = tmp_path / "SYNTHETIC-inputs"
    prepare(
        argparse.Namespace(
            output=inputs,
            instance=instance,
            operator_sid="S-1-5-21-1-2-3-1001",
            account_name="SYNTHETIC\\fixture",
        )
    )
    catalog = json.loads((inputs / "artifact-catalog.json").read_bytes())
    artifacts = {
        entry["reference"]["identity"]: Path(entry["path"]).read_bytes()
        for entry in catalog["artifacts"]
    }

    class NonExecutingAdapter(RegisteredWindowsPolicyVerifier):
        # Explicit local test seam. No registry, native endpoint or credential
        # is queried; a preparation-only read must never enter the worker.
        def __init__(self):
            self.instance = instance

        def call(self, *_args, **_kwargs):
            raise AssertionError("unsigned preparation entered a worker RPC")

    def no_authentication(*_args, **_kwargs):
        raise AssertionError("pre-authentication fixture attempted human authentication")

    return RegisteredTestCase(
        root=tmp_path / "SYNTHETIC-action",
        instance=instance,
        role=(inputs / "role-proposal.json").read_bytes(),
        storage_identity="synthetic-storage:v1:sha256:" + "0" * 64,
        registry=(inputs / "original-acquisitions.json").read_bytes(),
        authenticate=no_authentication,
        verifier=NonExecutingAdapter(),
        artifact_reader=lambda ref: artifacts[ref.identity],
    )


def proposal_period(case, start, end, *, instance=None):
    proposal = case.proposal()
    payload = proposal.model_dump(mode="json", exclude={"identity", "execution"})
    if instance is not None:
        payload["instance"] = instance
    execution = QualificationExecutionProposal.read(
        {
            **proposal.execution.model_dump(mode="json"),
            "effective_utc": start.isoformat(),
            "expires_utc": end.isoformat(),
        }
    )
    return PolicyDecisionProposal.seal(**payload, execution=execution)


def test_unsigned_inputs_need_neither_worker_rpc_nor_authentication(unsigned_case):
    case = unsigned_case
    value = case.proposal()
    assert case.claim is None
    assert value.policy.scope.historical_equal is False
    assert value.policy.scope.historical_cause == "UNRESOLVED"
    assert value.result_execution_authorized is False
    assert value.ticket07_or_live_authorized is False
    assert len(case.owner.artifacts) == 6


@pytest.mark.parametrize("unsigned_case", ("test-20300101-001",), indirect=True)
def test_unsigned_successor_uses_its_own_role_and_proposal(unsigned_case):
    assert unsigned_case.instance == "test-20300101-001"
    assert unsigned_case.role.instance == unsigned_case.instance
    assert unsigned_case.proposal().instance == unsigned_case.instance
    assert unsigned_case.claim is None
    assert not unsigned_case.root.exists()


@pytest.mark.parametrize("position", ("expired", "at-expiry", "not-yet-effective"))
def test_stale_or_future_unsigned_window_rejected_without_consumption(unsigned_case, position):
    instant = datetime(2030, 1, 2, tzinfo=timezone.utc)  # Isolated fixture clock only.
    start, end = instant - timedelta(days=7), instant
    if position == "expired":
        end -= timedelta(seconds=1)
    elif position == "not-yet-effective":
        start, end = instant + timedelta(seconds=1), instant + timedelta(days=7)
    proposal = proposal_period(unsigned_case, start, end)
    with pytest.raises(ValueError, match="expired or not yet effective"):
        driver.require_current_test_proposal(proposal, at=instant)
    assert unsigned_case.claim is None
    assert not unsigned_case.root.exists()


def test_current_window_preserves_exact_document_and_rejects_other_namespace(unsigned_case):
    instant = datetime(2030, 1, 2, tzinfo=timezone.utc)
    proposal = proposal_period(unsigned_case, instant, instant + timedelta(days=7))
    assert driver.require_current_test_proposal(proposal.to_bytes(), at=instant) == proposal
    wrong = proposal_period(
        unsigned_case, instant, instant + timedelta(days=7), instance="operational-20300102-001"
    )
    with pytest.raises(ValueError, match="another instance"):
        driver.require_current_test_proposal(wrong, at=instant)
    with pytest.raises(ValueError, match="explicit UTC"):
        driver.require_current_test_proposal(proposal, at=instant.replace(tzinfo=None))


def test_retained_artifact_reader_checks_exact_bytes_without_owner_state(tmp_path, monkeypatch):
    root = tmp_path / "SYNTHETIC-owner"
    (root / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(driver, "OWNER", root)
    raw = "SYNTHETIC artifact — 中文\n".encode()
    ref = EvidenceRef(
        identity=content_identity("synthetic-artifact:v1", {"label": "TEST"}), pin=BytePin.of(raw)
    )
    path = root / "artifacts" / (ref.pin.sha256 + ".blob")
    path.write_bytes(raw)
    assert driver.read_retained_artifact(ref) == raw
    path.write_bytes(raw + b"SYNTHETIC TAMPER")
    with pytest.raises(ValueError, match="whole-file byte pin mismatch"):
        driver.read_retained_artifact(ref)
    assert not (root / "state").exists()


def test_proposed_source_cannot_silently_reuse_old_deployment(tmp_path, monkeypatch):
    owner, source = tmp_path / "SYNTHETIC-owner", tmp_path / "SYNTHETIC-source"
    owner.mkdir()
    source.mkdir()
    original = b"SYNTHETIC reviewed source\n"
    (source / "component.py").write_bytes(original)
    manifest = {
        "files": [
            {
                "relative_path": "source/component.py",
                "byte_length": len(original),
                "sha256": sha256(original).hexdigest(),
            }
        ]
    }
    raw = json.dumps(manifest).encode()
    (owner / "worker-manifest.json").write_bytes(raw)
    monkeypatch.setattr(driver, "OWNER", owner)
    monkeypatch.setattr(driver, "SOURCE", source)
    monkeypatch.setattr(driver, "EXPECTED", {"worker_manifest_sha256": sha256(raw).hexdigest()})
    assert driver.require_deployed_source_consistency() == manifest
    (source / "component.py").write_bytes(b"SYNTHETIC proposed correction\n")
    with pytest.raises(ValueError, match="deployed immutable binding"):
        driver.require_deployed_source_consistency()
    assert (owner / "worker-manifest.json").read_bytes() == raw
    assert not (owner / "state").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parser")
def test_preparation_audit_allows_only_exact_read_only_status(tmp_path, monkeypatch):
    owner = tmp_path / "SYNTHETIC owner"
    (owner / "bin").mkdir(parents=True)
    executable = owner / "bin/PolicyOwner.exe"
    executable.write_bytes(b"NONEXECUTABLE TEST PATH ONLY")
    monkeypatch.setattr(driver, "OWNER", owner)
    client = driver.BoundedClient(tmp_path, False)

    def event(*arguments):
        return (None, subprocess.list2cmdline([str(executable), *arguments]), None, None)

    client.audit("subprocess.Popen", event("status", driver.INSTANCE))
    for args in (
        ("request", driver.INSTANCE),
        ("status", "test-20260914-001"),
        ("decide", driver.INSTANCE),
        ("status", driver.INSTANCE, "extra"),
    ):
        with pytest.raises(ValueError, match="exact read-only native status"):
            client.audit("subprocess.Popen", event(*args))


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parser")
def test_preparation_permits_validation_but_never_namespace_creation(tmp_path, namespace_binding):
    client = driver.BoundedClient(tmp_path, False)
    arguments = [str(value) for value in driver.namespace_initializer_arguments()]
    client.audit("subprocess.Popen", (None, subprocess.list2cmdline(arguments), None, None))
    request = tmp_path / "SYNTHETIC-request.json"
    request.write_bytes(b"{}")
    effectful = [str(value) for value in driver.namespace_initializer_arguments(request)]
    with pytest.raises(ValueError, match="namespace ValidateOnly"):
        client.audit("subprocess.Popen", (None, subprocess.list2cmdline(effectful), None, None))
    changed = arguments.copy()
    changed[changed.index("-Instance") + 1] = "test-20300101-001"
    with pytest.raises(ValueError, match="namespace ValidateOnly"):
        client.audit("subprocess.Popen", (None, subprocess.list2cmdline(changed), None, None))


def test_namespace_preflight_rejects_unbound_success_response(namespace_binding):
    class WrongBinding:
        def execute(self, *_args, **_kwargs):
            return {"namespace_initialized": False, "execution_authorized": False}

    with pytest.raises(ValueError, match="lost the exact TEST binding"):
        driver.validate_namespace_initializer(WrongBinding())


@pytest.fixture
def namespace_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "DEPLOYMENT_PATH", tmp_path / "SYNTHETIC-deployment.json")
    monkeypatch.setattr(driver, "DEPLOYMENT_SHA256", "0" * 64)
    monkeypatch.setattr(driver, "DEPLOYMENT_STAGING_ONLY", False)
    monkeypatch.setattr(
        driver,
        "NAMESPACE_BINDING",
        {
            "consumer": {
                "path": str(driver.OWNER / "bin/ConditionalRuntimeProtectionHost.exe"),
                "sha256": "1" * 64,
            },
            "library": {
                "path": str(driver.SOURCE / "scripts/policy_owner/pinned_setup_native.ps1"),
                "sha256": "2" * 64,
            },
            "helper": {
                "path": str(driver.SOURCE / "scripts/policy_owner/initialize_test_namespace.ps1"),
                "sha256": "3" * 64,
            },
            "protection_source_identity": "SYNTHETIC-only",
        },
    )


@pytest.mark.parametrize("arguments,code", ((["--help"], 0), (["--run"], 2), (["--enroll"], 2)))
def test_standalone_argument_handling_never_reaches_owner(tmp_path, arguments, code):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE"}
    }
    environment.update(PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(Path(driver.__file__)), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == code, completed.stderr.decode("utf-8", "replace")
    output = (completed.stdout + completed.stderr).decode("utf-8", "strict")
    assert "--prepare" in output and "--run" in output
    assert not list(tmp_path.iterdir())


def test_exact_deployment_json_rejects_pin_and_duplicate_fields(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_bytes(b'{"instance":"test-20300101-001"}')
    expected = driver.pin(path)
    assert driver.read_exact_json(path, expected)["instance"] == "test-20300101-001"
    path.write_bytes(b'{"instance":"operational-20300101-001"}')
    with pytest.raises(ValueError, match="input changed"):
        driver.read_exact_json(path, expected)
    path.write_bytes(b'{"instance":"test-20300101-001","instance":"test-20300101-002"}')
    with pytest.raises(ValueError, match="duplicate"):
        driver.read_exact_json(path, driver.pin(path))


@pytest.fixture
def synthetic_deployment(tmp_path, monkeypatch):
    # Nonexecutable bytes and no installed namespace; test only exact setup linkage.
    if os.name != "nt":
        pytest.skip("Windows deployment path contract")
    staged = tmp_path / "depot"
    staged_source = staged / "source"
    staged_source.mkdir(parents=True)
    members = []
    for name in (
        "bin/PolicyOwner.exe",
        "bin/ConditionalRuntimeProtectionHost.exe",
        "worker-manifest.json",
        "role-proposal.json",
        "original-acquisitions.json",
        "source/scripts/policy_owner/pinned_setup_native.ps1",
        "source/scripts/policy_owner/initialize_test_namespace.ps1",
        "source/scripts/runtime_protection/RuntimeProtectionHost.cs",
    ):
        path = staged / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("SYNTHETIC NONEXECUTABLE " + name).encode())
        members.append(
            {"relative_path": name, **BytePin.of(path.read_bytes()).model_dump(mode="json")}
        )
    owner = r"C:\ProgramData\TA-PolicyOwner-test-20300101-001"
    proposal = {
        "contract_version": "policy-owner-setup-proposal-v1",
        "instance": "test-20300101-001",
        "root": owner,
        "operator_sid": "S-1-5-21-1-2-3-1001",
        "ledger_identity": "SYNTHETIC-NEW-LEDGER",
        "files": members,
        "registration_is_approval": False,
        "execution_authorized": False,
    }
    proposal["identity"] = content_identity("policy-owner-setup-proposal:v1", proposal)
    proposal_path = tmp_path / "SETUP-PROPOSAL.json"
    proposal_path.write_bytes(driver.canonical(proposal))
    dependencies = staged / "test-client-dependencies"
    dependencies.mkdir()
    dependency = dependencies / "SYNTHETIC.py"
    dependency.write_bytes(b"# NONEXECUTING FIXTURE\n")
    manifest = tmp_path / "client-manifest.json"
    dependency_pin = driver.pin(dependency)
    dependency_pin["path"] = str(Path(owner) / "test-client-dependencies/SYNTHETIC.py")
    manifest.write_bytes(
        driver.canonical(
            {
                "contract_version": "registered-test-client-files-v1",
                "files": [dependency_pin],
                "execution_authorized": False,
            }
        )
    )
    executable = staged / "python/python.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"NONEXECUTABLE")
    members.extend(
        [
            {
                "relative_path": "test-client-dependencies/SYNTHETIC.py",
                **BytePin.of(dependency.read_bytes()).model_dump(mode="json"),
            },
            {
                "relative_path": "python/python.exe",
                **BytePin.of(executable.read_bytes()).model_dump(mode="json"),
            },
        ]
    )
    proposal.pop("identity")
    proposal["identity"] = content_identity("policy-owner-setup-proposal:v1", proposal)
    proposal_path.write_bytes(driver.canonical(proposal))
    executable_pin = driver.pin(executable)
    executable_pin["path"] = str(Path(owner) / "python/python.exe")
    value = {
        "contract_version": "registered-test-deployment-v1",
        "instance": proposal["instance"],
        "owner_root": owner,
        "setup_proposal": driver.pin(proposal_path),
        "client_source": str(Path(owner) / "source"),
        "client_python": executable_pin,
        "client_dependencies": str(Path(owner) / "test-client-dependencies"),
        "client_manifest": driver.pin(manifest),
        "reviewed_client_images": [],
        "operational": False,
        "execution_authorized": False,
    }
    path = tmp_path / "deployment.json"
    path.write_bytes(driver.canonical(value))
    for name in (
        "OWNER",
        "DEPENDENCIES",
        "INSTANCE",
        "EXPECTED",
        "AUTHENTICATION_ID",
        "CONSUMER_SHA256",
        "CLIENT_PYTHON",
        "DEPLOYMENT_PATH",
        "DEPLOYMENT_SHA256",
        "DEPLOYMENT_INPUTS",
        "REVIEWED_CLIENT_IMAGES",
        "NAMESPACE_BINDING",
        "DEPLOYMENT_STAGING_ONLY",
    ):
        monkeypatch.setattr(driver, name, getattr(driver, name))
    monkeypatch.setattr(driver, "SOURCE", staged_source)
    return path, value


def test_successor_deployment_binds_setup_without_transferring_authentication(synthetic_deployment):
    path, value = synthetic_deployment
    old_authentication = driver.AUTHENTICATION_ID
    driver.configure_deployment(path, driver.pin(path)["sha256"], staging_only=True)
    assert value["instance"] == driver.INSTANCE
    assert driver.EXPECTED["ledger_identity"] == "SYNTHETIC-NEW-LEDGER"
    assert old_authentication is not None and driver.AUTHENTICATION_ID is None
    assert "storage_binding_identity" not in driver.EXPECTED
    assert not driver.OWNER.exists()


def test_mutable_staging_cannot_be_used_for_human_execution(synthetic_deployment):
    path, _ = synthetic_deployment
    with pytest.raises(ValueError, match="protected installed source"):
        driver.configure_deployment(path, driver.pin(path)["sha256"])


@pytest.mark.parametrize("subtree", ("source", "python", "dependencies"))
def test_deployment_check_rejects_added_cached_import(synthetic_deployment, subtree):
    path, _ = synthetic_deployment
    cached = path.parent / "depot" / subtree / "__pycache__" / "SYNTHETIC.cpython-313.pyc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"NONEXECUTABLE UNREVIEWED CACHE FIXTURE")
    with pytest.raises(ValueError, match="import tree membership changed"):
        driver.configure_deployment(path, driver.pin(path)["sha256"], staging_only=True)


@pytest.mark.parametrize(
    "change", ("operational", "execution", "instance", "dependency", "unexpected")
)
def test_successor_deployment_rejects_scope_and_payload_drift(synthetic_deployment, change):
    path, value = synthetic_deployment
    if change == "operational":
        value["operational"] = True
    elif change == "execution":
        value["execution_authorized"] = True
    elif change == "instance":
        value["instance"] = "operational-20300101-001"
    elif change == "dependency":
        (path.parent / "depot/test-client-dependencies/SYNTHETIC.py").write_bytes(b"CHANGED")
    else:
        (path.parent / "depot/test-client-dependencies/extra.py").write_bytes(b"UNREVIEWED")
    path.write_bytes(driver.canonical(value))
    with pytest.raises(ValueError):
        driver.configure_deployment(path, driver.pin(path)["sha256"], staging_only=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows native module inventory")
def test_unreviewed_client_native_image_pin_is_rejected(tmp_path):
    path = tmp_path / "NONEXECUTABLE.dll"
    path.write_bytes(b"SYNTHETIC")
    expected = driver.pin(path)
    path.write_bytes(b"CHANGED")
    with pytest.raises(ValueError, match="client native image changed"):
        driver.client_native_images(reviewed_client_images=[expected])


@pytest.mark.parametrize(
    "instance,code", (("test-20300101-001", 0), ("operational-20300101-001", 1))
)
def test_actual_inert_child_entrypoint_preserves_test_namespace(tmp_path, instance, code):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    environment.update(PYTHON_DOTENV_DISABLED="1", TRADINGAGENTS_OUTPUT_LANGUAGE="English")
    child = Path(driver.__file__).with_name("registered_test_child.py")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(child), "inert", instance],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == code, result.stderr.decode("utf-8", "replace")
    assert not list(tmp_path.iterdir())
