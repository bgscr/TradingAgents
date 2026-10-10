"""Native Windows lifecycle checks; stranded fixtures are deliberately retained.

No cleanup or ACL recovery is performed on objects stranded by a child process.
Native objects live in new worktree-local roots outside pytest's pruning area.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from hashlib import sha256
from pathlib import Path

import pytest

from tests.native_policy_gate import native_policy_test

pytestmark = native_policy_test
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def retained_root():
    # Outside pytest's retention/pruning tree. Even a later pytest invocation
    # must never attempt to reset or delete a deliberately stranded fixture.
    external = os.environ.get("TRADINGAGENTS_NATIVE_TEST_ROOT")
    parent = Path(external).resolve() if external else ROOT / "build/rp-retained"
    if external and (not Path(external).is_absolute() or parent.is_relative_to(ROOT)):
        raise ValueError("explicit native test retention must be an absolute external directory")
    parent.mkdir(parents=True, exist_ok=True)
    # Keep the separate CLR fixture executable below its own startup path
    # limit; the payload tests still explicitly cover paths over MAX_PATH.
    root = parent / ("r" + uuid.uuid4().hex[:8])
    root.mkdir()
    return root


@pytest.fixture
def tmp_path(retained_root, request):
    path = retained_root / sha256(request.node.nodeid.encode()).hexdigest()[:8]
    path.mkdir()
    return path


@pytest.fixture(scope="module")
def native_contract_host(retained_root):
    build = retained_root / "native-contract-host"
    return _compile_native_host(build)


def _compile_native_host(build, *, synchronize_admission=False):
    build.mkdir()
    compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    output = build / "NativeContracts.exe"
    stamp = build / "BuildIdentity.cs"
    digest = sha256(
        (ROOT / "scripts/runtime_protection/RuntimeProtectionHost.cs").read_bytes()
    ).hexdigest()
    stamp_text = (
        '[assembly: System.Reflection.AssemblyMetadataAttribute("RuntimeProtectionSourceSha256", "'
        + digest
        + '")]'
    )
    fixture_hash = sha256(
        (ROOT / "tests/fixtures/runtime_protection/HandoffChild.py").read_bytes()
    ).hexdigest()
    for name in (
        "RuntimeProtectionCliSha256",
        "RuntimeProtectionContractSha256",
        "RuntimeProtectionHandoffSha256",
    ):
        stamp_text += (
            f'[assembly: System.Reflection.AssemblyMetadataAttribute("{name}", "{fixture_hash}")]'
        )
    stamp.write_text(stamp_text)
    source = ROOT / "scripts/runtime_protection/RuntimeProtectionHost.cs"
    if synchronize_admission:
        # Instrument a disposable copy solely to select an interleaving. No
        # production hooks or changed lifecycle logic are compiled into releases.
        code = source.read_text()
        marker = "ProtectedRuntimeTreeV3 protection=null;"
        assert code.count(marker) == 1
        source = build / "SynchronizedHost.cs"
        source.write_text(code.replace(marker, "NativeContracts.WaitForAdmission();" + marker))
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/platform:x64",
            "/main:NativeContracts",
            "/r:System.Web.Extensions.dll",
            f"/out:{output}",
            str(source),
            str(ROOT / "tests/fixtures/runtime_protection/NativeContracts.cs"),
            str(stamp),
        ],
        cwd=build,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def test_fresh_baseline_exact_restore_and_consumer_rejection(native_contract_host, tmp_path):
    result = subprocess.run(
        [str(native_contract_host), "normal", str(tmp_path / "attempt")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXACT_BASELINE_RESTORED_AND_CONSUMED" in result.stdout


def test_long_path_payload_preserves_complete_baseline(native_contract_host, tmp_path):
    result = subprocess.run(
        [str(native_contract_host), "long-path", str(tmp_path / "attempt")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "long-path creation, complete inventory and exact restoration" in result.stdout


@pytest.mark.parametrize(
    "case",
    (
        "foreign-addition",
        "prebaseline-drift",
        "baseline-drift",
        "stranded-successor",
        "cleanup-failure",
        "preopened-write-dac",
    ),
)
def test_native_failure_boundaries(native_contract_host, tmp_path, case):
    result = subprocess.run(
        [str(native_contract_host), case, str(tmp_path / "attempt")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONTRACT_PASS" in result.stdout


def _native_host(executable, operation, *arguments, input=None, wrapper="host"):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [str(executable), wrapper, operation, *map(str, arguments)],
        cwd=executable.parent,
        capture_output=True,
        text=True,
        check=False,
        input=input,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return result


def _write_json(path, value):
    from tradingagents.runtime_protection import canonical_bytes

    data = canonical_bytes(value)
    with path.open("xb") as stream:
        stream.write(data)
    return sha256(data).hexdigest()


def materialization_fixture(
    tmp_path,
    native_contract_host,
    *,
    ordinal=1,
    source_root=None,
    real_python=False,
    credential_bindings=(),
):
    from tests.test_runtime_protection_contract import attempt_inputs
    from tradingagents.runtime_protection import (
        AttemptInputs,
        derive_attempt,
        evidence_identity,
        make_content_manifest,
        seal_materialization_plan,
    )

    sources = source_root or tmp_path / "sources"
    if source_root is None:
        sources.mkdir()
        compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/target:exe",
                "/platform:x64",
                f"/out:{sources / 'inert.exe'}",
                str(ROOT / "tests/fixtures/runtime_protection/InertChild.cs"),
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        (sources / "ssl.py").write_bytes(b"# inert stdlib fixture\n")
        (sources / "probe.py").write_bytes(b"# inert application fixture\n")
    rows = {}
    locations = {
        "runtime/base/python.exe": sources / "inert.exe",
        "runtime/base/Lib/ssl.py": sources / "ssl.py",
        "runtime/base/fixture.pyd": sources / "inert.exe",
        "runtime/base/fixture.dll": sources / "inert.exe",
        "runtime/venv/Scripts/python.exe": sources / "inert.exe",
        "app/probe.py": sources / "probe.py",
    }
    if real_python:
        base = Path(sys.base_prefix)
        locations = {
            "runtime/base/python.exe": base / "python.exe",
            "runtime/venv/Scripts/python.exe": Path(sys.executable),
            "app/probe.py": ROOT / "tests/fixtures/runtime_protection/HandoffChild.py",
            "app/cli/main.py": ROOT / "tests/fixtures/runtime_protection/HandoffChild.py",
            "app/tradingagents/runtime_protection.py": ROOT
            / "tests/fixtures/runtime_protection/HandoffChild.py",
            "app/tradingagents/runtime_protection_handoff.py": ROOT
            / "tests/fixtures/runtime_protection/HandoffChild.py",
        }
        (sources / "empty.py").write_bytes(b"")
        locations["app/cli/__init__.py"] = sources / "empty.py"
        for path in base.glob("*.dll"):
            locations["runtime/base/" + path.name] = path
        for path in (base / "DLLs").glob("*"):
            if path.suffix.lower() in (".dll", ".pyd"):
                locations["runtime/base/DLLs/" + path.name] = path
        for path in (base / "Lib").glob("*.py"):
            locations["runtime/base/Lib/" + path.name] = path
        for directory in ("encodings", "json", "re", "collections", "importlib"):
            for path in (base / "Lib" / directory).rglob("*.py"):
                locations["runtime/base/Lib/" + path.relative_to(base / "Lib").as_posix()] = path
    for key, path in locations.items():
        data = path.read_bytes()
        rows[key] = {
            "relative_path": key.split("/", 1)[1],
            "byte_length": len(data),
            "sha256": sha256(data).hexdigest(),
        }
    values = attempt_inputs(namespace_root=str(tmp_path / "namespace"), attempt_ordinal=ordinal)
    registration = tmp_path / "namespace" / "namespace.json"
    if not registration.exists():
        request = {
            "contract_version": "runtime-protection-namespace-request-v1",
            "namespace_root": values["namespace_root"],
            "protection_source_identity": "runtime-protection-source:v1:sha256:"
            + sha256(
                (ROOT / "scripts/runtime_protection/RuntimeProtectionHost.cs").read_bytes()
            ).hexdigest(),
        }
        request_path = tmp_path / "namespace-request.json"
        result = _native_host(
            native_contract_host, "initialize", request_path, _write_json(request_path, request)
        )
        assert result.returncode == 0, result.stdout + result.stderr
    values["namespace_identity"] = json.loads(registration.read_bytes())["identity"]
    runtime = make_content_manifest(
        role="runtime",
        source_commit=values["source_commit"],
        source_tree=values["source_tree"],
        ancestry_identity=values["runtime_ancestry_identity"],
        files=[row for key, row in rows.items() if key.startswith("runtime/")],
        native_images=[
            key.split("/", 1)[1]
            for key, path in locations.items()
            if key.startswith("runtime/") and path.suffix.lower() in (".exe", ".dll", ".pyd")
        ],
        python_version="3.13.7",
    )
    app = make_content_manifest(
        role="app",
        source_commit=values["source_commit"],
        source_tree=values["source_tree"],
        ancestry_identity=values["app_ancestry_identity"],
        files=[row for key, row in rows.items() if key.startswith("app/")],
        native_images=[],
    )
    config = {
        "project_dir": r"D:\qualified\tradingagents",
        "crypto_identity_registry_path": r"D:\qualified\registry.json",
        "crypto_identity_registry_checksum_path": r"D:\qualified\registry.sha256",
        "output_language": "English",
        "mainland_capability_routing_mode": "qualified_v1_shadow",
        "cninfo_operator_minimum_interval_seconds": 7,
    }
    environment = {
        "SystemRoot": os.environ["SYSTEMROOT"],
        "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
        "PYTHON_DOTENV_DISABLED": "1",
        "RUN_LIVE_PROVIDER_TESTS": "0",
        "RUN_LIVE_TUSHARE_TESTS": "0",
        "RUN_LIVE_DEEPSEEK_TESTS": "0",
        "PATH": "{private_runtime_root}\\base;{private_runtime_root}\\venv\\Scripts;"
        + os.environ["SYSTEMROOT"]
        + r"\System32",
    }
    values.update(
        runtime_content_identity=runtime["identity"],
        app_content_identity=app["identity"],
        deployment_configuration_identity=evidence_identity(
            "runtime-protection-configuration:v1", config
        ),
        environment_identity=evidence_identity(
            "runtime-protection-environment:v1",
            {
                "variables": environment,
                "working_directory": str(tmp_path),
                "credential_bindings": list(credential_bindings),
            },
        ),
        protection_source_identity="runtime-protection-source:v1:sha256:"
        + sha256(
            (ROOT / "scripts/runtime_protection/RuntimeProtectionHost.cs").read_bytes()
        ).hexdigest(),
    )
    attempt = derive_attempt(AttemptInputs(**values))
    plan = seal_materialization_plan(
        attempt=attempt,
        runtime_content=runtime,
        app_content=app,
        source_locations={key: str(path) for key, path in locations.items()},
        deployment_configuration=config,
        environment=environment,
        working_directory=str(tmp_path),
        credential_bindings=credential_bindings,
        python_version="3.13.7",
    )
    return plan


def test_preparation_packet_direct_handoff_and_reuse_rejection(native_contract_host, tmp_path):
    from tradingagents.runtime_protection import (
        build_launch_packet,
        replay_attempt,
        validate_attempt_phase,
    )

    plan = materialization_fixture(tmp_path, native_contract_host)
    plan_path = tmp_path / "plan.json"
    result = _native_host(native_contract_host, "prepare", plan_path, _write_json(plan_path, plan))
    assert result.returncode == 0, result.stdout + result.stderr
    prepared = json.loads(result.stdout)
    paths = prepared["attempt"]["paths"]
    assert (
        Path(paths["private_runtime_root"], "venv/pyvenv.cfg")
        .read_text()
        .startswith("home = " + paths["private_runtime_root"])
    )
    packet = build_launch_packet(
        prepared, child_arguments=["-I", "-B", paths["private_app_root"] + r"\probe.py"]
    )
    packet_path = tmp_path / "inert-packet.json"
    packet_hash = _write_json(packet_path, packet)
    prepared_path = Path(paths["evidence_root"], "prepared.json")
    prepared_hash = sha256(prepared_path.read_bytes()).hexdigest()
    observed = _native_host(native_contract_host, "inspect", prepared_path, prepared_hash)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    validate_attempt_phase(prepared, packet, json.loads(observed.stdout), phase="readiness")
    result = _native_host(
        native_contract_host, "run", prepared_path, prepared_hash, packet_path, packet_hash
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["state"] == "COMPLETED_RESTORED"
    assert receipt["child_launches"] == 1
    assert receipt["exact_baseline_restored"] is True
    observed = _native_host(native_contract_host, "inspect", prepared_path, prepared_hash)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    replay = replay_attempt(prepared, json.loads(observed.stdout))
    assert replay["state"] == "COMPLETED_RESTORED"
    assert replay["reusable"] is False
    with pytest.raises(ValueError, match="consumed"):
        validate_attempt_phase(prepared, packet, json.loads(observed.stdout), phase="packet")
    result = _native_host(
        native_contract_host, "run", prepared_path, prepared_hash, packet_path, packet_hash
    )
    assert result.returncode != 0
    assert "consumed" in result.stderr.lower()
    with pytest.raises(PermissionError):
        Path(plan["attempt"]["inputs"]["namespace_root"], "claims").rename(
            tmp_path / "removed-claims"
        )


@pytest.mark.parametrize(
    "bad_path",
    (
        "D:relative-fixture.py",
        r"\\server\share\fixture.py",
        r"D:\fixture.py:stream",
        r"\\?\D:\fixture.py",
        r"D:\NUL",
        r"D:\a\..\b.py",
    ),
)
def test_native_rejects_nonlocal_sources_before_allocation(
    native_contract_host, tmp_path, bad_path
):
    from tradingagents.runtime_protection import evidence_identity

    plan = materialization_fixture(tmp_path, native_contract_host)
    plan["source_locations"]["runtime/base/Lib/ssl.py"] = bad_path
    plan.pop("identity")
    plan["identity"] = evidence_identity("runtime-protection-materialization:v1", plan)
    path = tmp_path / "bad-source-plan.json"
    result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    assert result.returncode != 0
    assert not Path(plan["attempt"]["paths"]["attempt_root"]).exists()


@pytest.mark.parametrize(
    "change",
    (
        "source_bytes",
        "runtime_manifest",
        "app_manifest",
        "source_mirror",
        "native_inventory",
        "environment",
        "search_path",
        "configuration",
    ),
)
def test_materialization_integrity_matrix(native_contract_host, tmp_path, change):
    from tradingagents.runtime_protection import evidence_identity

    plan = materialization_fixture(tmp_path, native_contract_host)
    if change == "source_bytes":
        (tmp_path / "sources/ssl.py").write_bytes(b"source drift")
    elif change == "source_mirror":
        plan["app_content"]["source_tree"] = "0" * 40
    elif change == "native_inventory":
        plan["runtime_content"]["native_images"].remove("base/python.exe")
    elif change in ("runtime_manifest", "app_manifest"):
        plan[change.replace("manifest", "content")]["identity"] = "wrong:v1:sha256:" + "f" * 64
    elif change == "environment":
        plan["environment"]["PYTHONPATH"] = r"D:\unqualified"
    elif change == "search_path":
        plan["environment"]["PATH"] = r"D:\unqualified;C:\Windows\System32"
    else:
        plan["mapped_configuration"]["cninfo_operator_minimum_interval_seconds"] = 99
    # Re-sealing outer transport must not conceal changes to inner authorities.
    plan.pop("identity")
    plan["identity"] = evidence_identity("runtime-protection-materialization:v1", plan)
    path = tmp_path / "bad-materialization.json"
    result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    assert result.returncode != 0
    assert not Path(plan["attempt"]["paths"]["evidence_root"], "prepared.json").exists()


def test_unknown_native_import_is_rejected_before_prepared_evidence(native_contract_host, tmp_path):
    materialization_fixture(tmp_path, native_contract_host)
    image = tmp_path / "sources/inert.exe"
    original = (Path(sys.base_prefix) / "python.exe").read_bytes()
    offset = original.lower().index(b"kernel32.dll")
    image.write_bytes(original[:offset] + b"unqlfxxx.dll" + original[offset + 12 :])
    # The altered native image is explicitly in the content manifest; its
    # unqualified import must still fail the native dependency closure.
    plan = materialization_fixture(tmp_path, native_contract_host, source_root=tmp_path / "sources")
    path = tmp_path / "unqualified-native-dependency.json"
    result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    assert result.returncode != 0, "Unknown native dependency was accepted"
    assert "dependency" in result.stderr.lower()


@pytest.mark.parametrize("kind", ("source", "evidence"))
def test_local_leaf_symlinks_are_rejected_without_following_them(
    native_contract_host, tmp_path, kind
):
    from tradingagents.runtime_protection import evidence_identity

    plan = materialization_fixture(tmp_path, native_contract_host)
    original = tmp_path / "sources/ssl.py" if kind == "source" else tmp_path / "input.json"
    if kind == "evidence":
        original_hash = _write_json(original, plan)
    link = tmp_path / "leaf-link"
    try:
        link.symlink_to(original)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Creating a file symlink is unavailable to this unprivileged test process")
        raise
    if kind == "source":
        plan["source_locations"]["runtime/base/Lib/ssl.py"] = str(link)
        plan.pop("identity")
        plan["identity"] = evidence_identity("runtime-protection-materialization:v1", plan)
        path = tmp_path / "linked-source.json"
        result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    else:
        result = _native_host(native_contract_host, "prepare", link, original_hash)
    assert result.returncode != 0
    assert "reparse" in result.stderr.lower()
    assert not Path(plan["attempt"]["paths"]["attempt_root"]).exists()


def test_preconsumption_rejection_leaves_coherent_prepared_evidence(native_contract_host, tmp_path):
    _, prepared, packet = prepared_fixture(tmp_path, native_contract_host)
    paths = prepared["attempt"]["paths"]
    capsule = Path(paths["evidence_root"], "prepared.json")
    packet_path = tmp_path / "dll-preflight.json"
    result = _native_host(
        native_contract_host,
        "run",
        capsule,
        sha256(capsule.read_bytes()).hexdigest(),
        packet_path,
        _write_json(packet_path, packet),
        wrapper="host-dll",
    )
    assert result.returncode != 0
    assert not Path(paths["claim_root"], "consumed").exists()
    assert not Path(paths["evidence_root"], "completion.json").exists()
    result = _native_host(
        native_contract_host, "inspect", capsule, sha256(capsule.read_bytes()).hexdigest()
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "PREPARED"


def test_interactive_handoff_preserves_an_inherited_hidden_console(native_contract_host, tmp_path):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [str(native_contract_host), "console-parent", str(tmp_path)],
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inherited interactive console preserved" in result.stdout


def test_losing_admission_owner_cannot_publish_completion(retained_root, tmp_path, monkeypatch):
    host = _compile_native_host(retained_root / "race-host", synchronize_admission=True)
    child_prefix = tmp_path / "child"
    _, prepared, packet = prepared_fixture(tmp_path, host, extra_arguments=["--fixture-hold", str(child_prefix)])
    paths = prepared["attempt"]["paths"]
    capsule = Path(paths["evidence_root"], "prepared.json")
    packet_path = tmp_path / "race-packet.json"
    packet_hash = _write_json(packet_path, packet)
    prefix = tmp_path / "admission"
    monkeypatch.setenv("TA_TEST_ADMISSION", str(prefix))
    arguments = [str(host), "host", "run", str(capsule), sha256(capsule.read_bytes()).hexdigest(), str(packet_path), packet_hash]
    processes = [subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(2)]

    def until(predicate):
        deadline = time.monotonic() + 25
        while not predicate():
            assert time.monotonic() < deadline, "Controlled admission interleaving timed out"
            time.sleep(0.01)

    outputs = []
    try:
        until(lambda: len(list(tmp_path.glob("admission.*.ready"))) == 2)
        Path(str(prefix) + ".release").write_bytes(b"release")
        until(lambda: Path(str(child_prefix) + ".started").exists())
        until(lambda: any(process.poll() is not None for process in processes))
        premature_completion = Path(paths["evidence_root"], "completion.json").exists()
    finally:
        Path(str(prefix) + ".release").touch(exist_ok=True)
        Path(str(child_prefix) + ".release").write_bytes(b"release")
        for process in processes:
            outputs.append(process.communicate(timeout=30))
    assert not premature_completion, "A losing owner published terminal evidence while the winner was active"
    assert sorted(process.returncode for process in processes) == [0, 1], outputs
    receipt = json.loads(Path(paths["evidence_root"], "completion.json").read_bytes())
    assert receipt["state"] == "COMPLETED_RESTORED" and receipt["failure_count"] == 0


def prepared_fixture(tmp_path, host, *, ordinal=1, source_root=None, extra_arguments=()):
    from tradingagents.runtime_protection import build_launch_packet

    plan = materialization_fixture(tmp_path, host, ordinal=ordinal, source_root=source_root)
    path = tmp_path / f"plan-{ordinal}.json"
    plan_hash = _write_json(path, plan)
    result = _native_host(host, "prepare", path, plan_hash)
    assert result.returncode == 0, result.stdout + result.stderr
    prepared = json.loads(result.stdout)
    paths = prepared["attempt"]["paths"]
    arguments = ["-I", "-B", paths["private_app_root"] + r"\probe.py", *extra_arguments]
    packet = build_launch_packet(prepared, child_arguments=arguments)
    return plan, prepared, packet


def run_fixture(tmp_path, host, prepared, packet, *, name="packet"):
    path = tmp_path / f"{name}.json"
    packet_hash = _write_json(path, packet)
    prepared_path = Path(prepared["attempt"]["paths"]["evidence_root"], "prepared.json")
    return _native_host(
        host,
        "run",
        prepared_path,
        sha256(prepared_path.read_bytes()).hexdigest(),
        path,
        packet_hash,
    )


@pytest.mark.parametrize(
    "relative",
    (
        "runtime/base/Lib/ssl.py",
        "runtime/base/fixture.pyd",
        "runtime/base/fixture.dll",
        "runtime/venv/pyvenv.cfg",
        "runtime/venv/Lib/site-packages/zz_ticket07_private_app.pth",
        "runtime/app/probe.py",
        "anchors/history/provenance.json",
    ),
)
def test_prepared_byte_drift_never_launches(native_contract_host, tmp_path, relative):
    _, prepared, packet = prepared_fixture(tmp_path, native_contract_host)
    paths = prepared["attempt"]["paths"]
    file = Path(paths["payload_root"], relative)
    with file.open("ab") as stream:
        stream.write(b"fixture-drift")
    result = run_fixture(tmp_path, native_contract_host, prepared, packet)
    assert result.returncode != 0
    assert "drift" in result.stderr.lower()
    assert not Path(paths["claim_root"], "consumed").exists()
    assert not Path(paths["evidence_root"], "launching.json").exists()


@pytest.mark.parametrize(
    "relative",
    (
        "runtime/base/Lib/ssl.pyc",
        "runtime/app/foreign.py",
        "runtime/base/unknown.dll",
        "anchors/current/foreign",
    ),
)
def test_unknown_members_and_bytecode_are_rejected(native_contract_host, tmp_path, relative):
    _, prepared, packet = prepared_fixture(tmp_path, native_contract_host)
    paths = prepared["attempt"]["paths"]
    Path(paths["payload_root"], relative).write_bytes(b"MZunqualified-fixture")
    result = run_fixture(tmp_path, native_contract_host, prepared, packet)
    assert result.returncode != 0
    assert not Path(paths["evidence_root"], "launching.json").exists()


@pytest.mark.parametrize(
    "field",
    (
        "attempt_identity",
        "prepared_identity",
        "runtime_manifest_identity",
        "app_manifest_identity",
        "baseline_identity",
        "protection_contract_version",
        "attempt_root",
        "current_anchor",
        "history_anchor",
    ),
)
def test_packet_cannot_substitute_attempt_or_evidence(native_contract_host, tmp_path, field):
    from tradingagents.runtime_protection import evidence_identity

    _, prepared, packet = prepared_fixture(tmp_path, native_contract_host)
    target = packet["paths"] if field in packet["paths"] else packet
    target[field] += "-different-prior-attempt"
    packet.pop("identity")
    packet["identity"] = evidence_identity("runtime-protection-packet:v1", packet)
    result = run_fixture(tmp_path, native_contract_host, prepared, packet)
    assert result.returncode != 0
    assert not Path(prepared["attempt"]["paths"]["claim_root"], "consumed").exists()


@pytest.mark.parametrize(
    "state", ("root_without_provenance", "provenance_without_root", "prepared_without_completion")
)
def test_all_allocated_or_ambiguous_attempt_states_reject_materialization(
    native_contract_host, tmp_path, state
):
    plan = materialization_fixture(tmp_path, native_contract_host)
    paths = plan["attempt"]["paths"]
    if state == "root_without_provenance":
        Path(paths["attempt_root"]).mkdir()
    elif state == "provenance_without_root":
        Path(paths["claim_root"]).mkdir()
    path = tmp_path / "plan.json"
    digest = _write_json(path, plan)
    if state == "prepared_without_completion":
        result = _native_host(native_contract_host, "prepare", path, digest)
        assert result.returncode == 0, result.stderr
    result = _native_host(native_contract_host, "prepare", path, digest)
    assert result.returncode != 0
    assert "exists" in result.stderr.lower() or "provenance" in result.stderr.lower()


def test_v22_full_pipeline_strands_a_and_runs_only_fresh_b(native_contract_host, tmp_path):
    from tradingagents.runtime_protection import evidence_identity, replay_attempt

    _, first, packet_a = prepared_fixture(tmp_path, native_contract_host)
    paths_a = first["attempt"]["paths"]
    packet_a["child_arguments"] += ["--fixture-strand-owner", paths_a["current_anchor"]]
    packet_a.pop("identity")
    packet_a["identity"] = evidence_identity("runtime-protection-packet:v1", packet_a)
    stranded = run_fixture(tmp_path, native_contract_host, first, packet_a, name="strand-a")
    assert stranded.returncode != 0
    assert Path(paths_a["claim_root"], "consumed").is_dir()
    assert not Path(paths_a["evidence_root"], "completion.json").exists()

    def observe(root):
        result = subprocess.run(
            [str(native_contract_host), "observe", root],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    before = observe(paths_a["payload_root"])
    assert "WRITE_DAC_DENIED" in before
    path = Path(paths_a["evidence_root"], "prepared.json")
    observation = _native_host(
        native_contract_host, "inspect", path, sha256(path.read_bytes()).hexdigest()
    )
    assert observation.returncode == 0, observation.stderr
    replay = replay_attempt(first, json.loads(observation.stdout))
    assert replay["state"] == "INCOMPLETE_RETAINED" and replay["reusable"] is False
    _, second, packet_b = prepared_fixture(
        tmp_path, native_contract_host, ordinal=2, source_root=tmp_path / "sources"
    )
    assert not set(paths_a.values()) & set(second["attempt"]["paths"].values())
    result = run_fixture(tmp_path, native_contract_host, second, packet_b, name="successor-b")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["exact_baseline_restored"] is True
    assert observe(paths_a["payload_root"]) == before
    # No GuardOnly, recovery, cleanup, or WRITE_DAC call runs against A.
    retry = run_fixture(tmp_path, native_contract_host, first, packet_a, name="retry-a")
    assert retry.returncode != 0 and "consumed" in retry.stderr.lower()
    assert observe(paths_a["payload_root"]) == before


def test_live_handoff_rejects_missing_gate_before_any_child(native_contract_host, tmp_path):
    from tests.test_runtime_protection_handoff import handoff_fixture
    from tradingagents.runtime_protection_handoff import build_guarded_launch_packet

    _, prepared, inert = prepared_fixture(tmp_path, native_contract_host)
    packet = build_guarded_launch_packet(
        prepared,
        guard_arguments=inert["child_arguments"],
        handoff=handoff_fixture(prepared),
        operation="guard_then_live",
    )
    result = run_fixture(tmp_path, native_contract_host, prepared, packet)
    assert result.returncode != 0
    assert "build-bound Step-4 gate" in result.stderr
    assert not Path(prepared["attempt"]["paths"]["claim_root"], "consumed").exists()


def test_credential_evidence_contains_only_name_and_digest(native_contract_host, tmp_path):
    from tradingagents.runtime_protection import build_launch_packet

    secret = "fixture-only-credential"
    credentials = [{"name": "FIXTURE_API_KEY", "sha256": sha256(secret.encode()).hexdigest()}]
    plan = materialization_fixture(tmp_path, native_contract_host, credential_bindings=credentials)
    assert secret not in json.dumps(plan)
    path = tmp_path / "credential-plan.json"
    result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    assert result.returncode == 0, result.stderr
    prepared = json.loads(result.stdout)
    assert prepared["credential_bindings"] == credentials
    assert secret not in result.stdout
    packet = build_launch_packet(
        prepared,
        child_arguments=[
            "-I",
            "-B",
            prepared["attempt"]["paths"]["private_app_root"] + r"\probe.py",
        ],
    )
    result = run_fixture(tmp_path, native_contract_host, prepared, packet)
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("operation", ("guard_only", "guard_then_live"))
def test_guard_and_step4_share_one_current_protection_owner(
    native_contract_host, tmp_path, operation
):
    from tradingagents.runtime_protection_handoff import build_guarded_launch_packet

    plan = materialization_fixture(tmp_path, native_contract_host, real_python=True)
    path = tmp_path / "plan.json"
    result = _native_host(native_contract_host, "prepare", path, _write_json(path, plan))
    assert result.returncode == 0, result.stderr
    prepared = json.loads(result.stdout)
    inputs = prepared["attempt"]["inputs"]
    gate = {
        "contract_version": "runtime-protection-handoff-v1",
        "preparation_lineage_identity": inputs["preparation_lineage_identity"],
        "plan_version": "1.5",
        "plan_signature": "fixture-plan",
        "cninfo_profile_identity": "fixture-profile",
        "cninfo_source_policy_identity": "fixture-policy",
        "cninfo_operator_minimum_interval_seconds": 7,
        "checkpoint_identity": "fixture-checkpoint:v1:sha256:" + "a" * 64,
        "history_identity": "fixture-history:v1:sha256:" + "b" * 64,
        "calendar_revision_id": "fixture-calendar",
        "valid_before_exclusive": "2099-01-01T00:00:00Z",
        "symbol": "DISPOSABLE_FIXTURE",
        "analysis_session": inputs["completed_session"],
        "selected_analysts": ["fundamentals"],
        "output_language": "English",
        "legacy_authoritative": True,
        "qualified_shadow_authoritative": False,
        "execution_authorized": False,
    }
    packet = build_guarded_launch_packet(
        prepared,
        guard_arguments=[
            "-I",
            "-B",
            prepared["attempt"]["paths"]["private_app_root"] + r"\probe.py",
        ],
        handoff=gate,
        operation=operation,
    )
    packet_path = tmp_path / "disposable-handoff-fixture.json"
    prepared_path = Path(prepared["attempt"]["paths"]["evidence_root"], "prepared.json")
    result = _native_host(
        native_contract_host,
        "run",
        prepared_path,
        sha256(prepared_path.read_bytes()).hexdigest(),
        packet_path,
        _write_json(packet_path, packet),
        input=packet["identity"] + "\nYES\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["exact_baseline_restored"] is True
    assert receipt["child_launches"] == (2 if operation == "guard_then_live" else 1)
    assert Path(prepared["attempt"]["paths"]["claim_root"], "consumed").is_dir()


def test_closed_builder_and_verified_bootstrap(tmp_path):
    powershell = Path(os.environ["SYSTEMROOT"], "System32/WindowsPowerShell/v1.0/powershell.exe")
    scripts = ROOT / "scripts/runtime_protection"
    output = tmp_path / "compiled"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(scripts / "Build-RuntimeProtectionHost.ps1"),
            "-OutputDirectory",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    build_reference = json.loads(result.stdout.splitlines()[-1])
    manifest = json.loads(Path(build_reference["build_manifest"]).read_bytes())
    namespace = tmp_path / "bootstrap-namespace"
    request = {
        "contract_version": "runtime-protection-namespace-request-v1",
        "namespace_root": str(namespace),
        "protection_source_identity": manifest["protection_source_identity"],
    }
    request_path = tmp_path / "initialize.json"
    arguments = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(scripts / "Invoke-RuntimeProtectionHost.ps1"),
        "-BuildManifestPath",
        build_reference["build_manifest"],
        "-BuildManifestSha256",
        build_reference["sha256"],
        "-Operation",
        "initialize",
        "-InputPath",
        str(request_path),
        "-InputSha256",
        _write_json(request_path, request),
    ]
    result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["contract_version"] == "runtime-protection-namespace-v1"
    assert (
        manifest["qualification_performed"] is False and manifest["execution_authorized"] is False
    )
    with (output / "RuntimeProtectionHost.exe").open("ab") as stream:
        stream.write(b"fixture-build-drift")
    result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Pinned build input bytes differ" in result.stderr


def test_tampered_bootstrap_helper_is_never_executed(tmp_path):
    copied = tmp_path / "source"
    files = [
        "cli/main.py",
        "tradingagents/runtime_protection.py",
        "tradingagents/runtime_protection_handoff.py",
        "scripts/runtime_protection/RuntimeProtectionHost.cs",
        "scripts/runtime_protection/Build-RuntimeProtectionHost.ps1",
        "scripts/runtime_protection/Invoke-RuntimeProtectionHost.ps1",
        "scripts/runtime_protection/ProtectionBootstrap.ps1",
    ]
    for relative in files:
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    powershell = Path(os.environ["SYSTEMROOT"], "System32/WindowsPowerShell/v1.0/powershell.exe")
    scripts = copied / "scripts/runtime_protection"
    output = tmp_path / "compiled"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(scripts / "Build-RuntimeProtectionHost.ps1"),
            "-OutputDirectory",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reference = json.loads(result.stdout.splitlines()[-1])
    manifest = json.loads(Path(reference["build_manifest"]).read_bytes())
    marker = tmp_path / "unexpected-helper-execution"
    helper = scripts / "ProtectionBootstrap.ps1"
    helper.write_text(
        "[IO.File]::WriteAllText('"
        + str(marker).replace("'", "''")
        + "','unexpected')\n"
        + helper.read_text()
    )
    request = {
        "contract_version": "runtime-protection-namespace-request-v1",
        "namespace_root": str(tmp_path / "namespace"),
        "protection_source_identity": manifest["protection_source_identity"],
    }
    path = tmp_path / "request.json"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(scripts / "Invoke-RuntimeProtectionHost.ps1"),
            "-BuildManifestPath",
            reference["build_manifest"],
            "-BuildManifestSha256",
            reference["sha256"],
            "-Operation",
            "initialize",
            "-InputPath",
            str(path),
            "-InputSha256",
            _write_json(path, request),
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert not marker.exists(), "The helper executed before its expected digest was checked"
    assert not Path(request["namespace_root"]).exists()
