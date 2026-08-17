from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import ntpath
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

PROBE_MECHANISMS = (
    "normal_python",
    "python_i",
    "python_s",
    "python_e",
    "multiprocessing_spawn",
    "powershell",
    "cmd",
    "direct_filesystem",
    "sqlite",
)
LOCAL_WRITE_TARGETS = (
    "temp",
    "tradingagents_runtime",
    "data",
    "cache",
    "market_history",
    "checkpoints",
    "reports",
    "logs",
    "payloads",
    "sqlite",
    "source_tree",
)
FAILURE_DIAGNOSTIC_PATH = Path(r"C:\QCF17\Output\boundary-failure.json")
_FAILURE_REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")
INPUT_ROOTS = {
    "source": Path(r"C:\QCF17\Input\Source"),
    "runner": Path(r"C:\QCF17\Input\Runner"),
    "control": Path(r"C:\QCF17\Input\Control"),
    "runtime": Path(r"C:\QCF17\Input\Runtime"),
}
_CREDENTIAL_NAME = re.compile(
    r"(?i)(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|COOKIE)"
)
_CREDENTIAL_FILENAMES = {
    ".env",
    ".env.enterprise",
    ".netrc",
    ".pypirc",
    "cookies.sqlite",
    "credentials.json",
    "credential-store.json",
    "login data",
    "pip.ini",
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _identity(namespace: str, value: object) -> str:
    return f"{namespace}:sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > maximum_bytes:
        raise RuntimeError("bounded_json_input_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("bounded_json_input_invalid")
    return value


def _path_identity(value: str) -> str:
    normalized = ntpath.normcase(value).replace("/", "\\").casefold()
    return _identity("host-path", normalized)


def _verify_runner(control: dict[str, Any], runner_root: Path) -> None:
    expected_names = {
        "bootstrap.ps1",
        "install_boundary_runtime.ps1",
        "run_boundary_qualification.py",
        "run_ticket06_acceptance.py",
        "run_ticket06_offline_review.py",
        "runner-manifest.json",
        "ticket06_acceptance_contract.py",
        "verify_source.py",
    }
    if {path.name for path in runner_root.iterdir()} != expected_names:
        raise RuntimeError("runner_input_file_set_invalid")
    manifest = _load_json(runner_root / "runner-manifest.json", maximum_bytes=65_536)
    identity = manifest.pop("runner_input_identity", None)
    if identity != _identity("windows-sandbox-qualified-runner-input:v2", manifest):
        raise RuntimeError("runner_input_identity_mismatch")
    manifest["runner_input_identity"] = identity
    if manifest != control["base"]["runner_input"]:
        raise RuntimeError("runner_control_binding_mismatch")
    expected_files = (
        "bootstrap.ps1",
        "install_boundary_runtime.ps1",
        "run_boundary_qualification.py",
        "run_ticket06_acceptance.py",
        "run_ticket06_offline_review.py",
        "ticket06_acceptance_contract.py",
        "verify_source.py",
    )
    rows = manifest.get("files")
    if not isinstance(rows, list) or tuple(row.get("relative_path") for row in rows) != expected_files:
        raise RuntimeError("runner_input_manifest_invalid")
    for row in rows:
        path = runner_root / row["relative_path"]
        if (
            not path.is_file()
            or path.stat().st_size != row["byte_length"]
            or _sha256(path) != row["sha256"]
        ):
            raise RuntimeError("runner_input_file_mismatch")


def _verify_control(control: dict[str, Any]) -> None:
    if set(control) != {
        "base",
        "contract_version",
        "derived",
        "qualification_control_identity",
        "runtime_bundle",
        "source",
    }:
        raise RuntimeError("qualification_control_shape_invalid")
    if control["contract_version"] != "windows-sandbox-boundary-control-envelope-v1":
        raise RuntimeError("qualification_control_version_invalid")
    control_identity = _identity(
        "windows-sandbox-qualification-control:v1", control["base"]
    )
    if control_identity != control["qualification_control_identity"]:
        raise RuntimeError("qualification_control_identity_mismatch")
    base = control["base"]
    derived = control["derived"]
    acceptance_structure = derived.get("acceptance_runner_structure")
    if not isinstance(acceptance_structure, dict):
        raise RuntimeError("acceptance_runner_structure_missing")
    acceptance_identity = acceptance_structure.get("qualification_identity")
    acceptance_payload = {
        key: value
        for key, value in acceptance_structure.items()
        if key != "qualification_identity"
    }
    if (
        acceptance_identity
        != _identity("ticket06-acceptance-runner-structure:v2", acceptance_payload)
        or acceptance_structure.get("command_ledger_identity")
        != (
            "ticket06-sandbox-command-ledger:v1:sha256:"
            "8a2cb9a483e2c68dbef2b154c8c22634878c159a8a78ad0c0c7d8776570e85ea"
        )
        or acceptance_structure.get("mandatory_command_count") != 27
        or acceptance_structure.get("ticket06_executed") is not False
        or acceptance_structure.get("guest_qcf17_host_clause_b_authority") is not False
        or acceptance_structure.get("guest_qcf18_final_authority") is not False
        or acceptance_structure.get("completion_marker_write_order") != "last"
        or acceptance_structure.get("additional_host_mapping_required") is not False
        or acceptance_structure.get("network_permission_required") is not False
        or acceptance_structure.get("runner_input_identity")
        != base.get("runner_input", {}).get("runner_input_identity")
        or acceptance_structure.get("boundary_runner_identity")
        != derived.get("runner_identity")
        or acceptance_structure.get("wsb_config_identity")
        != derived.get("wsb_config_identity")
        or acceptance_structure.get("mapping_manifest_identity")
        != derived.get("mapping_manifest_identity")
        or acceptance_structure.get("host_harness_manifest_identity")
        != base.get("host_harness", {}).get("manifest_identity")
        or acceptance_structure.get("offline_review_executable_closed") is not True
        or acceptance_structure.get(
            "acceptance_orchestrator_write_order_validated"
        )
        is not True
        or acceptance_structure.get("host_acceptance_mapping_reuse_validated")
        is not True
        or acceptance_structure.get(
            "host_acceptance_export_validation_path_validated"
        )
        is not True
    ):
        raise RuntimeError("acceptance_runner_structure_invalid")
    host_harness_files = base.get("host_harness", {}).get("files")
    if not isinstance(host_harness_files, list):
        raise RuntimeError("acceptance_runner_structure_invalid")
    host_acceptance_files = [
        item
        for item in host_harness_files
        if isinstance(item, dict)
        and item.get("relative_path")
        == "tradingagents/windows_sandbox_qualification.py"
    ]
    if (
        len(host_acceptance_files) != 1
        or acceptance_structure.get("host_acceptance_module_sha256")
        != host_acceptance_files[0].get("sha256")
    ):
        raise RuntimeError("acceptance_runner_structure_invalid")
    source_alias = control.get("source", {})
    expected_source_alias = {
        "contract_version": base["source"]["contract_version"],
        "commit_id": base["source"]["commit_id"],
        "tree_hash": base["source"]["tree_hash"],
        "source_input_identity": base["source"]["source_input_identity"],
        "source_manifest_identity": base["source"]["source_manifest_identity"],
        "source_archive_sha256": base["source"]["archive_sha256"],
    }
    if (
        source_alias != expected_source_alias
        or control.get("runtime_bundle") != base.get("runtime_bundle")
        or base.get("execution_mode") != "boundary_qualification"
        or derived.get("execution_mode") != "boundary_qualification"
        or derived.get("qualification_control_identity") != control_identity
        or base.get("networking_disabled") is not True
        or base.get("input_mappings_read_only") is not True
        or base.get("exactly_one_writable_results_mapping") is not True
        or base.get("credential_inputs_present") is not False
        or any(
            base.get(name) is not False
            for name in (
                "ticket06_permitted",
                "ticket07_permitted",
                "provider_model_io_permitted",
                "calendar_refresh_permitted",
                "qualified_v1_activation_permitted",
            )
        )
    ):
        raise RuntimeError("qualification_control_policy_invalid")
    source = base["source"]
    runtime = base["runtime_bundle"]
    bindings = {
        "qualification_run_identity": base["qualification_run_identity"],
        "source_commit_id": source["commit_id"],
        "source_tree_hash": source["tree_hash"],
        "source_input_identity": source["source_input_identity"],
        "source_manifest_identity": source["source_manifest_identity"],
        "source_archive_sha256": source["archive_sha256"],
        "runtime_bundle_identity": base["runtime_bundle_identity"],
        "runtime_bundle_manifest_sha256": runtime["bundle_manifest_sha256"],
        "runtime_bundle_contract_identity": runtime[
            "runtime_bundle_contract_identity"
        ],
        "protected_root_manifest_identity": base["protected_roots"][
            "manifest_identity"
        ],
        "qualification_control_identity": control_identity,
        "platform_qualification_identity": base["platform_qualification"][
            "qualification_identity"
        ],
        "immutable_verifier_identity": base["immutable_verifier_identity"],
        "host_before_inventory_identity": base["host_before_inventory_identity"],
    }
    if any(derived.get(name) != value for name, value in bindings.items()):
        raise RuntimeError("qualification_derived_binding_mismatch")


def _verify_source(
    control: dict[str, Any], source_root: Path, workspace: Path, runtime_root: Path
) -> None:
    if {path.name for path in source_root.iterdir()} != {
        "reviewed-commit.object",
        "reviewed-source.tar",
        "source-manifest.json",
    }:
        raise RuntimeError("source_input_file_set_invalid")
    expected = control["base"]["source"]
    manifest = _load_json(source_root / "source-manifest.json", maximum_bytes=16 * 1024 * 1024)
    manifest_identity = manifest.pop("source_manifest_identity", None)
    if manifest_identity != _identity("windows-sandbox-source-manifest:v1", manifest):
        raise RuntimeError("source_manifest_identity_mismatch")
    if (
        manifest_identity != expected["source_manifest_identity"]
        or manifest.get("commit_id") != expected["commit_id"]
        or manifest.get("tree_hash") != expected["tree_hash"]
        or len(manifest.get("entries", ())) != expected["tracked_file_count"]
        or _sha256(source_root / "reviewed-source.tar") != expected["archive_sha256"]
        or _sha256(source_root / "reviewed-commit.object")
        != expected["commit_object_sha256"]
    ):
        raise RuntimeError("source_input_identity_mismatch")
    runtime_contract = control["base"]["runtime_bundle"]
    git = runtime_root / Path(runtime_contract["git_relative_path"])
    completed = subprocess.run(
        [str(git), "-C", str(workspace), "rev-parse", "HEAD", "HEAD^{tree}"],
        check=False,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    values = completed.stdout.decode("ascii", errors="strict").splitlines()
    if completed.returncode != 0 or values != [expected["commit_id"], expected["tree_hash"]]:
        raise RuntimeError("guest_workspace_source_mismatch")


def _verify_runtime(control: dict[str, Any], runtime_root: Path) -> None:
    runtime = control["base"]["runtime_bundle"]
    manifest_path = runtime_root / "bundle-manifest.json"
    if (
        _sha256(manifest_path) != runtime["bundle_manifest_sha256"]
        or runtime.get("state") != "sealed"
        or runtime.get("mapped_read_only") is not True
        or runtime.get("offline_required") is not True
        or runtime.get("network_install_permitted") is not False
    ):
        raise RuntimeError("runtime_bundle_contract_mismatch")
    verifier = runtime_root / "bootstrap" / "verify_bundle.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(verifier),
            "--bundle-root",
            str(runtime_root),
            "--expected-bundle-identity",
            control["base"]["runtime_bundle_identity"],
            "--expected-source-input-identity",
            control["base"]["source"]["source_input_identity"],
        ],
        check=False,
        capture_output=True,
        shell=False,
        timeout=180,
    )
    if completed.returncode != 0 or len(completed.stdout) > 32_768:
        raise RuntimeError("runtime_bundle_verification_failed")
    evidence = json.loads(completed.stdout.decode("utf-8"))
    if (
        evidence.get("bundle_identity")
        != control["base"]["runtime_bundle_identity"]
        or evidence.get("network_used") is not False
    ):
        raise RuntimeError("runtime_bundle_verification_mismatch")


def _verify_environment_and_credentials(control: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    expected = dict(control["base"]["environment"]["variables"])
    for name, value in expected.items():
        if os.environ.get(name) != value:
            bounded_name = name.casefold()
            if _FAILURE_REASON.fullmatch(bounded_name) is None:
                bounded_name = "invalid_name"
            raise RuntimeError(f"guest_environment_mismatch_{bounded_name}")
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1" or any(
        value != "0" for name, value in expected.items() if name.startswith("RUN_LIVE_")
    ):
        raise RuntimeError("guest_live_environment_unsafe")
    credential_names = tuple(name for name in os.environ if _CREDENTIAL_NAME.search(name))
    if credential_names:
        raise RuntimeError("guest_credential_environment_present")
    for root in INPUT_ROOTS.values():
        for path in root.rglob("*"):
            if path.is_file() and path.name.casefold() in _CREDENTIAL_FILENAMES:
                raise RuntimeError("guest_credential_file_present")
    evidence = tuple(
        sorted(
            (name, value)
            for name, value in expected.items()
            if name.startswith("RUN_LIVE_") or name == "PYTHON_DOTENV_DISABLED"
        )
    )
    if len(evidence) < 4:
        raise RuntimeError("guest_live_environment_incomplete")
    return evidence


def _verify_read_only_mappings(run_id: str) -> None:
    for root in INPUT_ROOTS.values():
        probe = root / f".iso03-readonly-{run_id}"
        try:
            probe.write_bytes(b"must-be-os-denied")
        except OSError:
            continue
        with contextlib.suppress(OSError):
            probe.unlink()
        raise RuntimeError("read_only_mapping_write_succeeded")


def _network_state() -> dict[str, bool]:
    script = (
        "$ErrorActionPreference='Stop';"
        "$v4=@(Get-NetRoute -AddressFamily IPv4 -ErrorAction Stop | "
        "Where-Object DestinationPrefix -eq '0.0.0.0/0').Count;"
        "$v6=@(Get-NetRoute -AddressFamily IPv6 -ErrorAction Stop | "
        "Where-Object DestinationPrefix -eq '::/0').Count;"
        "[pscustomobject]@{ipv4=[bool]($v4 -gt 0);ipv6=[bool]($v6 -gt 0)} | "
        "ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0 or len(completed.stdout) > 8_192:
        raise RuntimeError("guest_network_route_check_failed")
    routes = json.loads(completed.stdout.decode("utf-8-sig"))
    ipv4 = routes.get("ipv4") is True
    ipv6 = routes.get("ipv6") is True
    if ipv4 or ipv6:
        raise RuntimeError("guest_default_route_present")
    return {
        "ipv4_default_route_present": ipv4,
        "ipv6_default_route_present": ipv6,
        "external_route_usable": False,
    }


def _prepare_guest_parent(target: Path) -> None:
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)


def _run_quiet(command: list[str]) -> bool:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _write_spawn_helper(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import multiprocessing
import pathlib
import sys

def child(target: str) -> None:
    pathlib.Path(target).write_bytes(b'iso03-multiprocessing-canary')

if __name__ == '__main__':
    multiprocessing.set_start_method('spawn', force=True)
    process = multiprocessing.Process(target=child, args=(sys.argv[1],))
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
    raise SystemExit(0 if process.exitcode == 0 else 1)
""",
        encoding="utf-8",
        newline="\n",
    )


def _probe(
    mechanism: str,
    target_value: str,
    *,
    spawn_helper: Path,
) -> dict[str, object]:
    target = Path(target_value)
    existed_before = os.path.lexists(target)
    _prepare_guest_parent(target)
    code = (
        "import pathlib,sys;"
        "pathlib.Path(sys.argv[1]).write_bytes(b'iso03-python-canary')"
    )
    succeeded = False
    if mechanism == "normal_python":
        succeeded = _run_quiet([sys.executable, "-c", code, target_value])
    elif mechanism == "python_i":
        succeeded = _run_quiet([sys.executable, "-I", "-c", code, target_value])
    elif mechanism == "python_s":
        succeeded = _run_quiet([sys.executable, "-S", "-c", code, target_value])
    elif mechanism == "python_e":
        succeeded = _run_quiet([sys.executable, "-E", "-c", code, target_value])
    elif mechanism == "multiprocessing_spawn":
        succeeded = _run_quiet([sys.executable, str(spawn_helper), target_value])
    elif mechanism == "powershell":
        succeeded = _run_quiet(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "& { param($p) Set-Content -LiteralPath $p "
                "-Value 'iso03-powershell-canary' -NoNewline }",
                target_value,
            ]
        )
    elif mechanism == "cmd":
        succeeded = _run_quiet(
            [
                "cmd.exe",
                "/d",
                "/s",
                "/c",
                f'(echo|set /p="iso03-cmd-canary")>"{target_value}"',
            ]
        )
    elif mechanism == "direct_filesystem":
        try:
            descriptor = os.open(
                target,
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, b"iso03-direct-filesystem-canary")
            finally:
                os.close(descriptor)
            succeeded = True
        except OSError:
            succeeded = False
    elif mechanism == "sqlite":
        try:
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE iso03_probe(value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO iso03_probe(value) VALUES (?)", ("canary",)
                )
                connection.commit()
            succeeded = True
        except (OSError, sqlite3.Error):
            succeeded = False
    else:
        raise RuntimeError("unknown_probe_mechanism")

    if existed_before and succeeded:
        raise RuntimeError("protected_path_visible_and_writable")
    observation = (
        "guest_local_only"
        if succeeded
        else "os_write_denied"
        if existed_before
        else "host_object_absent"
    )
    return {
        "mechanism": mechanism,
        "target_host_path": target_value,
        "target_path_identity": _path_identity(target_value),
        "observation": observation,
        "contained": True,
    }


def _run_probes(control: dict[str, Any], guest_output: Path) -> list[dict[str, object]]:
    targets = control["base"]["probe_targets"]
    if tuple(item.get("mechanism") for item in targets) != PROBE_MECHANISMS:
        raise RuntimeError("probe_target_plan_invalid")
    if any(
        item.get("target_path_identity") != _path_identity(item.get("target_host_path", ""))
        for item in targets
    ):
        raise RuntimeError("probe_target_path_identity_mismatch")
    helper = guest_output / "multiprocessing_probe.py"
    _write_spawn_helper(helper)
    return [
        _probe(
            item["mechanism"],
            item["target_host_path"],
            spawn_helper=helper,
        )
        for item in targets
    ]


def _local_write(path: Path, marker: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker)
    if path.read_bytes() != marker:
        raise RuntimeError("guest_local_write_readback_failed")


def _run_local_write_controls(
    control: dict[str, Any], workspace: Path
) -> list[dict[str, object]]:
    layout = control["base"]["guest_layout"]
    paths = {
        "temp": Path(layout["temp"]) / "iso03-temp.bin",
        "tradingagents_runtime": Path(layout["tradingagents_data"]).parent
        / "iso03-runtime.bin",
        "data": Path(layout["tradingagents_data"]) / "iso03-data.bin",
        "cache": Path(layout["tradingagents_cache"]) / "iso03-cache.bin",
        "market_history": Path(layout["market_history"]) / "iso03-history.bin",
        "checkpoints": Path(layout["checkpoints"]) / "iso03-checkpoint.bin",
        "reports": Path(layout["reports"]) / "iso03-report.bin",
        "logs": Path(layout["logs"]) / "iso03-log.bin",
        "payloads": Path(layout["payloads"]) / "iso03-payload.bin",
        "source_tree": workspace / "build" / "iso03-source-write.bin",
    }
    results: list[dict[str, object]] = []
    for name in LOCAL_WRITE_TARGETS:
        if name == "sqlite":
            database = Path(layout["market_history"]) / "iso03-local.sqlite3"
            database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE control(value TEXT NOT NULL)")
                connection.execute("INSERT INTO control(value) VALUES ('pass')")
                connection.commit()
                value = connection.execute("SELECT value FROM control").fetchone()
            if value != ("pass",):
                raise RuntimeError("guest_local_sqlite_control_failed")
        else:
            _local_write(paths[name], f"iso03-{name}-control".encode("ascii"))
        results.append({"target": name, "succeeded": True})
    return results


def _write_result(
    result_root: Path,
    control: dict[str, Any],
    *,
    network: dict[str, bool],
    live_environment: tuple[tuple[str, str], ...],
    probes: list[dict[str, object]],
    local_writes: list[dict[str, object]],
) -> None:
    if any(result_root.iterdir()):
        raise RuntimeError("result_export_not_fresh")
    payload = {
        **control["derived"],
        "contract_version": "windows-sandbox-boundary-qualification-result-v1",
        "source_verified": True,
        "runtime_bundle_verified": True,
        "guest_workspace_verified": True,
        "mapping": {
            "networking_disabled": True,
            "read_only_input_mappings": True,
            "exactly_one_writable_results_mapping": True,
            "forbidden_host_mappings_absent": True,
            "guest_read_only_write_checks_passed": True,
        },
        "network": network,
        "live_environment": [list(item) for item in live_environment],
        "credential_inputs_present": False,
        "probes": probes,
        "local_writes": local_writes,
        "guest_completion_status": "pass",
        "ticket06_executed": False,
        "ticket07_executed": False,
        "provider_model_io": False,
        "calendar_refresh_executed": False,
        "qualified_v1_enabled": False,
    }
    payload["result_identity"] = _identity(
        "windows-sandbox-boundary-qualification-result:v1", payload
    )
    result_bytes = _canonical_json(payload) + b"\n"
    if len(result_bytes) > 262_144:
        raise RuntimeError("bounded_result_too_large")
    result_path = result_root / "result.json"
    with result_path.open("xb") as handle:
        handle.write(result_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    marker_payload = {
        "contract_version": "windows-sandbox-boundary-completion-v1",
        "result_identity": payload["result_identity"],
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "write_order": "last",
    }
    marker_payload["marker_identity"] = _identity(
        "windows-sandbox-boundary-completion:v1", marker_payload
    )
    marker_bytes = _canonical_json(marker_payload) + b"\n"
    with (result_root / "completion.json").open("xb") as handle:
        handle.write(marker_bytes)
        handle.flush()
        os.fsync(handle.fileno())


def _write_failure_diagnostic(stage: str, error: Exception) -> None:
    reason = str(error)
    if not isinstance(error, RuntimeError) or _FAILURE_REASON.fullmatch(reason) is None:
        reason = f"{stage}_failed"
    payload = {
        "contract_version": "windows-sandbox-boundary-diagnostic-v1",
        "reason": reason,
        "stage": stage,
        "status": "fail",
    }
    material = _canonical_json(payload) + b"\n"
    if len(material) > 4096:
        return
    with contextlib.suppress(OSError):
        FAILURE_DIAGNOSTIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        FAILURE_DIAGNOSTIC_PATH.write_bytes(material)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--source-input", required=True, type=Path)
    parser.add_argument("--runner-input", required=True, type=Path)
    parser.add_argument("--runtime-input", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    stage = "load_control"
    try:
        control = _load_json(args.control, maximum_bytes=262_144)
        stage = "verify_control"
        _verify_control(control)
        stage = "verify_runner"
        _verify_runner(control, args.runner_input)
        stage = "verify_source"
        _verify_source(control, args.source_input, args.workspace, args.runtime_input)
        stage = "verify_runtime"
        _verify_runtime(control, args.runtime_input)
        stage = "verify_environment"
        live_environment = _verify_environment_and_credentials(control)
        stage = "verify_read_only_mappings"
        _verify_read_only_mappings(control["base"]["run_id"])
        stage = "verify_network"
        network = _network_state()
        guest_output = Path(control["base"]["guest_layout"]["guest_output"])
        stage = "run_adversarial_probes"
        probes = _run_probes(control, guest_output)
        stage = "run_local_write_controls"
        local_writes = _run_local_write_controls(control, args.workspace)
        stage = "write_result"
        _write_result(
            args.results,
            control,
            network=network,
            live_environment=live_environment,
            probes=probes,
            local_writes=local_writes,
        )
    except Exception as error:
        _write_failure_diagnostic(stage, error)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
