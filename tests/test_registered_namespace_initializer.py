"""Disposable exact-binding matrix through the real coordinator, WinPS and native code.

No enrolled root, real credential, registered claim or service is used.
Legacy NativeContracts is a labelled fixture authority, not a registered owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native namespace contract")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def inputs():
    configured = os.environ.get("TRADINGAGENTS_NAMESPACE_TEST_INPUTS")
    if configured is None:
        pytest.skip("explicit disposable staged bindings required")
    value = json.loads(Path(configured).read_bytes())
    assert value["fixture_only"] is True and value["existing_enrolled_roots_used"] is False
    assert len({row["instance"] for row in value["bindings"]}) >= 2
    for row in value["bindings"]:
        assert not Path(row["root"]).is_relative_to(Path(r"C:\ProgramData"))
        assert digest(row["deployment"]["path"]) == row["deployment"]["sha256"]
    return value


def case_root(binding, name):
    root = Path(binding["root"]) / "SYNTHETIC-namespace-fixtures" / name
    root.mkdir(parents=True, exist_ok=False)
    return root


def write_request(binding, case, namespace=None, source_identity=None):
    root = Path(binding["root"])
    source = root / "depot/source/scripts/runtime_protection/RuntimeProtectionHost.cs"
    value = {
        "contract_version": "runtime-protection-namespace-request-v1",
        "namespace_root": str(namespace or case / "namespace"),
        "protection_source_identity": source_identity
        or "runtime-protection-source:v1:sha256:" + digest(source),
    }
    path = case / "request.json"
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    return path


def coordinator(binding, case, request=None, suffix="coordinator"):
    root = Path(binding["root"])
    arguments = [
        binding["python"]["path"],
        "-I",
        "-B",
        "-S",
        str(root / "depot/source/scripts/policy_owner/run_registered_test.py"),
        "--rehearse-namespace",
        "--output",
        str(case / suffix),
        "--deployment",
        binding["deployment"]["path"],
        "--deployment-sha256",
        binding["deployment"]["sha256"],
    ]
    if request is not None:
        arguments += [
            "--namespace-request",
            str(request),
            "--namespace-request-sha256",
            digest(request),
        ]
    unrelated = case / (suffix + "-unrelated-cwd")
    unrelated.mkdir()
    result = subprocess.run(arguments, capture_output=True, check=False, timeout=120, cwd=unrelated)
    (case / (suffix + ".stdout")).write_bytes(result.stdout)
    (case / (suffix + ".stderr")).write_bytes(result.stderr)
    return result


def helper_arguments(binding, case):
    # Actual coordinator produces this argv; tests do not duplicate the path check.
    result = coordinator(binding, case)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    request = json.loads(
        (case / "coordinator/namespace-initializer-preflight.request.json").read_bytes()
    )
    return request["arguments"]


def invoke_helper(arguments, case):
    result = subprocess.run(arguments, capture_output=True, check=False, timeout=60, cwd=case)
    (case / "helper.stdout").write_bytes(result.stdout)
    (case / "helper.stderr").write_bytes(result.stderr)
    return result


def change(arguments, flag, value):
    modified = list(arguments)
    modified[modified.index(flag) + 1] = str(value)
    return modified


@pytest.mark.parametrize("index", (0, 1))
def test_coordinator_namespace_then_real_fixture_prepare_run_restore(inputs, index):
    from tests.test_runtime_protection_windows import (
        _native_host,
        _write_json,
        materialization_fixture,
    )
    from tradingagents.runtime_protection import build_launch_packet

    binding = inputs["bindings"][index]
    case = case_root(binding, "positive")
    request = write_request(binding, case)
    result = coordinator(binding, case, request)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    observed = json.loads(result.stdout)
    assert observed["instance"] == binding["instance"]
    assert observed["status"] == "STAGED_NAMESPACE_INITIALIZED"
    assert observed["owner_contacted"] is False and observed["authentication_executed"] is False
    namespace = json.loads((case / "namespace/namespace.json").read_bytes())
    assert observed["namespace"] == namespace
    host = Path(inputs["native_fixture"]["path"])
    assert digest(host) == inputs["native_fixture"]["sha256"]
    plan = materialization_fixture(
        case, host, source_root=Path(inputs["native_fixture"]["input_sources"])
    )
    # Existing fixture sees the coordinator-created namespace; never reinitializes it.
    assert plan["attempt"]["inputs"]["namespace_identity"] == namespace["identity"]
    plan_path = case / "SYNTHETIC-plan.json"
    prepared_response = _native_host(host, "prepare", plan_path, _write_json(plan_path, plan))
    assert prepared_response.returncode == 0, prepared_response.stderr
    prepared = json.loads(prepared_response.stdout)
    packet = build_launch_packet(
        prepared,
        child_arguments=[
            "-I",
            "-B",
            prepared["attempt"]["paths"]["private_app_root"] + r"\probe.py",
        ],
    )
    packet_path = case / "SYNTHETIC-packet.json"
    prepared_path = Path(prepared["attempt"]["paths"]["evidence_root"]) / "prepared.json"
    completion = _native_host(
        host,
        "run",
        prepared_path,
        digest(prepared_path),
        packet_path,
        _write_json(packet_path, packet),
    )
    (case / "native-completion.stdout").write_text(completion.stdout)
    (case / "native-completion.stderr").write_text(completion.stderr)
    assert completion.returncode == 0, completion.stderr
    receipt = json.loads(completion.stdout)
    assert receipt["state"] == "COMPLETED_RESTORED" and receipt["child_launches"] == 1
    assert receipt["exact_baseline_restored"] is True
    before = (case / "namespace/namespace.json").read_bytes()
    rejected = coordinator(binding, case, request, suffix="existing-namespace")
    assert rejected.returncode != 0
    assert (case / "namespace/namespace.json").read_bytes() == before


@pytest.mark.parametrize("index", (0, 1))
def test_nonconsuming_preflight_loads_pinned_native_entrypoint(inputs, index):
    binding = inputs["bindings"][index]
    case = case_root(binding, "preflight")
    result = coordinator(binding, case)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    value = json.loads((case / "coordinator/namespace-initializer-preflight.stdout").read_bytes())
    assert value["instance"] == binding["instance"]
    assert value["native_entrypoint_loaded"] is True
    assert value["staged_fixture"] is True and value["namespace_initialized"] is False
    assert value["execution_authorized"] is False
    assert not (case / "namespace").exists()


@pytest.mark.parametrize(
    "kind",
    (
        "other-consumer",
        "historical-instance",
        "similar-instance",
        "similar-prefix-path",
        "relocated-consumer",
        "relocated-helper",
        "wrong-consumer-pin",
        "wrong-library-pin",
        "missing-consumer",
        "changed-consumer",
        "missing-library",
        "changed-library",
    ),
)
def test_real_wrapper_rejects_binding_drift_before_creation(inputs, kind):
    binding, other = inputs["bindings"][:2]
    case = case_root(binding, "negative-" + kind)
    arguments = helper_arguments(binding, case)
    request = write_request(binding, case)
    arguments.remove("-ValidateOnly")
    arguments += ["-RequestPath", str(request), "-RequestSha256", digest(request)]
    backup = None
    if kind == "other-consumer":
        arguments = change(arguments, "-ConsumerPath", other["consumer"]["path"])
    elif kind == "historical-instance":
        arguments = change(arguments, "-Instance", "test-20260915-002")
    elif kind == "similar-instance":
        arguments = change(arguments, "-Instance", binding["instance"] + "0")
    elif kind in {"similar-prefix-path", "relocated-consumer"}:
        path = case / "relocated/ConditionalRuntimeProtectionHost.exe"
        path.parent.mkdir()
        shutil.copyfile(binding["consumer"]["path"], path)
        if kind == "similar-prefix-path":
            path = Path(binding["consumer"]["path"]).with_name(
                "ConditionalRuntimeProtectionHost.exe-extra"
            )
        arguments = change(arguments, "-ConsumerPath", path)
    elif kind == "relocated-helper":
        path = case / "relocated-helper.ps1"
        shutil.copyfile(arguments[arguments.index("-File") + 1], path)
        arguments = change(arguments, "-File", path)
    elif kind in {"wrong-consumer-pin", "wrong-library-pin"}:
        arguments = change(
            arguments,
            "-ConsumerSha256" if kind == "wrong-consumer-pin" else "-PinLibrarySha256",
            "0" * 64,
        )
    else:
        path = Path(
            arguments[
                arguments.index("-ConsumerPath" if "consumer" in kind else "-PinLibraryPath") + 1
            ]
        )
        raw = path.read_bytes()
        backup = (path, raw)
        (case / "retained-original.bin").write_bytes(raw)
        if kind.startswith("missing"):
            path.unlink()
        else:
            path.write_bytes(raw + b"SYNTHETIC TAMPER")
    try:
        result = invoke_helper(arguments, case)
        assert result.returncode != 0
        assert not (case / "namespace").exists()
    finally:
        if backup is not None:
            backup[0].write_bytes(backup[1])


@pytest.mark.parametrize("kind", ("other-binding-root", "wrong-source", "wrong-request-pin"))
def test_namespace_request_binding_rejection(inputs, kind):
    binding, other = inputs["bindings"][:2]
    case = case_root(binding, "request-" + kind)
    arguments = helper_arguments(binding, case)
    namespace = (
        Path(other["root"]) / "SYNTHETIC-namespace-fixtures/UNAUTHORIZED-namespace"
        if kind == "other-binding-root"
        else None
    )
    request = write_request(
        binding,
        case,
        namespace=namespace,
        source_identity="runtime-protection-source:v1:sha256:" + "0" * 64
        if kind == "wrong-source"
        else None,
    )
    arguments.remove("-ValidateOnly")
    arguments += [
        "-RequestPath",
        str(request),
        "-RequestSha256",
        "0" * 64 if kind == "wrong-request-pin" else digest(request),
    ]
    result = invoke_helper(arguments, case)
    assert result.returncode != 0
    assert not (case / "namespace").exists()
    if namespace is not None:
        assert not namespace.exists()


def test_redirected_rehearsal_root_rejects_before_any_output_write(inputs):
    binding = inputs["bindings"][2]
    root = Path(binding["root"])
    outside = root / "SYNTHETIC-outside-sentinel"
    outside.mkdir()
    redirected = root / "SYNTHETIC-namespace-fixtures"
    created = subprocess.run(
        [r"C:\Windows\System32\cmd.exe", "/d", "/c", "mklink", "/J", str(redirected), str(outside)],
        capture_output=True,
        check=False,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert created.returncode == 0, created.stderr
    arguments = [
        binding["python"]["path"],
        "-I",
        "-B",
        "-S",
        str(root / "depot/source/scripts/policy_owner/run_registered_test.py"),
        "--rehearse-namespace",
        "--output",
        str(redirected / "must-not-exist"),
        "--deployment",
        binding["deployment"]["path"],
        "--deployment-sha256",
        binding["deployment"]["sha256"],
    ]
    result = subprocess.run(arguments, capture_output=True, check=False, timeout=120, cwd=root)
    (root / "redirection.stdout").write_bytes(result.stdout)
    (root / "redirection.stderr").write_bytes(result.stderr)
    assert result.returncode != 0
    assert b"redirected" in result.stderr
    assert not list(outside.iterdir())
