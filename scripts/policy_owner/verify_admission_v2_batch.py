"""One declared TEST batch. No retry and no installed owner access.

The immutable E6 Job helper is loaded by exact pin for owned-handle operations;
its old eight-case runner is never invoked. Each new controller waits for GO
until it belongs to the new outer kill-on-close Job. All products use TEST-only
paths. This script cannot invoke maintenance, a service, or a real authenticator.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes as c
import hashlib
import importlib.machinery
import io
import json
import os
import runpy
import subprocess
import sys
import time
import types
from pathlib import Path

CASES = ("syntax-imports", "legacy-worker-rejection", "synthetic-core", "native-contracts")
GO = b"GO_ADMISSION_V2_SYNTHETIC_ONLY\n"
HELPER_PIN = {
    "byte_length": 38613,
    "sha256": "0de2a712e4ab69b19a9f31b5b38924ba688a508251a341602ae6662e7a6b3365",
}


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def pin(path: Path) -> dict:
    raw = path.read_bytes()
    return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def write_new(path: Path, raw: bytes) -> dict:
    with path.open("xb", buffering=0) as stream:
        if stream.write(raw) != len(raw):
            raise OSError("short TEST evidence write")
        stream.flush()
        os.fsync(stream.fileno())
    result = pin(path)
    if result != {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
        raise OSError("TEST evidence readback mismatch")
    return {"path": str(path), **result}


def require(ok: object, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def load(path: Path, name: str, expected: dict | None = None):
    raw = path.read_bytes()
    if expected is not None:
        require(
            {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} == expected,
            "exact raw loader pin mismatch",
        )
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = name.rpartition(".")[0]
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, origin=str(path))
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    return module


def audit_controller(reservation: dict) -> None:
    root = Path(reservation["case_root"])
    reads = [Path(value).absolute() for value in reservation["allowed_read_roots"]]
    native_argv = reservation.get("native_argv")

    def within(path: Path, targets: list[Path]) -> bool:
        return any(path == target or path.is_relative_to(target) for target in targets)

    def audit(event: str, values: tuple) -> None:
        if event.startswith("winreg.") or event in {
            "os.system",
            "os.startfile",
            "os.startfile/2",
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.gethostbyname",
        }:
            raise RuntimeError("TEST controller forbids field/network effects: " + event)
        if event == "subprocess.Popen":
            actual_args = values[1]
            argv_matches = (
                (
                    actual_args == subprocess.list2cmdline(native_argv)
                    if isinstance(actual_args, str)
                    else list(actual_args) == native_argv
                )
                if native_argv is not None
                else False
            )
            require(
                reservation["case"] == "native-contracts"
                and argv_matches
                and Path(values[2]) == root,
                "only the exact pinned TEST native child is allowed",
            )
        if event == "ctypes.dlopen" and values and values[0] is not None:
            # The synthetic controller never constructs a Windows authority.
            raise RuntimeError("TEST Python cannot load a Windows platform authority")
        if event == "open" and not isinstance(values[0], int):
            path = Path(os.fsdecode(values[0])).absolute()
            write = bool(
                values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            )
            require(
                within(path, [root] if write else reads),
                "TEST file access escaped declared synthetic inputs/output",
            )
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            require(
                within(Path(os.fsdecode(values[0])).absolute(), [root]),
                "TEST mutation escaped new case root",
            )
        if event in {"os.rename", "os.link", "os.symlink"}:
            for value in values[:2]:
                require(
                    within(Path(os.fsdecode(value)).absolute(), [root]),
                    "TEST move/link escaped new case root",
                )
        if event == "sqlite3.connect":
            from urllib.parse import unquote, urlparse

            value = str(values[0])
            if value.startswith("file:"):
                value = unquote(urlparse(value).path).lstrip("/")
            require(
                within(Path(value).absolute(), [root]),
                "TEST SQLite must be a new synthetic database",
            )

    sys.addaudithook(audit)


def controller(path: Path) -> int:
    # No project import, test, native child or state access before owned Job GO.
    require(sys.stdin.buffer.readline(128) == GO, "owned Job GO missing")
    reservation = json.loads(path.read_bytes())
    require(
        reservation["contract_version"] == "policy-owner-admission-test-chain-v2"
        and reservation["case"] in CASES,
        "closed TEST chain context",
    )
    for item in reservation["input_pins"]:
        require(
            pin(Path(item["path"]))
            == {"byte_length": item["byte_length"], "sha256": item["sha256"]},
            "frozen test input changed",
        )
    source = Path(reservation["source_root"])
    case = reservation["case"]
    case_root = Path(reservation["case_root"])
    audit_controller(reservation)
    if case == "syntax-imports":
        for name in reservation["python_sources"]:
            compile((source / name).read_bytes(), str(source / name), "exec", dont_inherit=True)
        checker = load(
            source / "scripts/policy_owner/check_worker_imports.py", "admission_import_check"
        )
        report = checker.check(Path(reservation["depot"]))
        require(report["passed"], "v2 isolated import closure failed")
        # The import check deliberately installs a permanent no-write audit
        # hook. Its complete report goes through already-open stdout only.
        print(
            json.dumps(
                {
                    "case": case,
                    "compiled_sources": len(reservation["python_sources"]),
                    "import_report": report,
                    "passed": True,
                },
                sort_keys=True,
            )
        )
        return 0
    if case == "legacy-worker-rejection":
        worker = source / "scripts/policy_owner/owner_worker.py"
        frames = [{"contract_version": "policy-owner-trusted-worker-request-v1"}]
        for operation in (
            "status",
            "worker",
            "challenge",
            "complete-authentication",
            "apply-authenticated-record",
            "reconcile",
            "report",
            "issue-receipt",
            "migrate-closed-v2",
        ):
            frames.append(
                {
                    "contract_version": "policy-owner-trusted-worker-request-v2",
                    "mode": "ENROLLMENT_ONLY",
                    "anchor_v2": {},
                    "lease_id": "0" * 64,
                    "operation": operation,
                    "payload": {},
                }
            )
        observations = []
        for frame in frames:
            previous_stdin, previous_argv = sys.stdin, sys.argv
            output, errors = io.StringIO(), io.StringIO()
            try:
                sys.stdin = io.TextIOWrapper(io.BytesIO(canonical(frame)), encoding="utf-8")
                sys.argv = [str(worker)]
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    try:
                        runpy.run_path(str(worker), run_name="__main__")
                        result = 0
                    except SystemExit as error:
                        result = error.code
            finally:
                sys.stdin, sys.argv = previous_stdin, previous_argv
            require(
                result == 2
                and "v1/generic worker route permanently disabled" in errors.getvalue()
                and output.getvalue() == "",
                "legacy worker did not reject at the pre-import gate",
            )
            require(
                "tradingagents" not in sys.modules,
                "legacy rejection imported the product namespace",
            )
            observations.append(
                {
                    "frame": frame,
                    "exit": result,
                    "stdout": output.getvalue(),
                    "stderr": errors.getvalue(),
                }
            )
        write_new(case_root / "rejections.json", canonical(observations))
        print(
            json.dumps(
                {
                    "case": case,
                    "rejections": len(observations),
                    "project_namespace_imported": False,
                    "passed": True,
                },
                sort_keys=True,
            )
        )
        return 0
    if case == "synthetic-core":
        sys.argv = [
            str(source / "tests/test_policy_owner_admission_v2.py"),
            "--artifact-root",
            str(case_root / "artifacts"),
        ]
        try:
            runpy.run_path(sys.argv[0], run_name="__main__")
        except SystemExit as error:
            return int(error.code or 0)
        return 0
    require(case == "native-contracts", "unknown TEST case")
    completed = subprocess.run(
        reservation["native_argv"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=case_root,
        env=reservation["environment"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=40,
        check=False,
    )
    write_new(case_root / "native.stdout", completed.stdout)
    write_new(case_root / "native.stderr", completed.stderr)
    write_new(
        case_root / "native-exit.json",
        canonical(
            {
                "actual_exit": completed.returncode,
                "source": "NEW_OWNED_POPEN_HANDLE_WAIT",
                "passed": completed.returncode == 0,
            }
        ),
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


def batch(plan_path: Path) -> int:
    started = time.monotonic()
    deadline = started + 900
    plan = json.loads(plan_path.read_bytes())
    require(
        plan["contract_version"] == "policy-owner-admission-test-plan-v2"
        and plan["cases"] == list(CASES),
        "one fixed TEST batch required",
    )
    require(
        plan["total_limit_seconds"] == 900
        and plan["chain_limit_seconds"] == 60
        and plan["retry"] is False,
        "test budget changed",
    )
    source, output = Path(plan["source_root"]), Path(plan["output_root"])
    require(
        output.is_absolute() and not output.exists(),
        "a fresh absolute test result root is required",
    )
    helper_path = Path(plan["immutable_e6_helper"])
    require(pin(helper_path) == HELPER_PIN, "E6 Job component pin changed")
    helper = load(helper_path, "immutable_e6_job_helpers", HELPER_PIN)
    api = helper._Api()
    own_token = helper.non_admin_evidence(api)
    output.mkdir()
    held = []
    directories = {}
    records = []
    summary = {
        "contract_version": "policy-owner-admission-test-batch-v2",
        "status": "STOP_PARTIAL_TEST_EVIDENCE_PRESERVED",
        "context": "SYNTHETIC_TEST_ONLY",
        "budget_seconds": 900,
        "per_chain_seconds": 60,
        "cases_declared": list(CASES),
        "old_eight_case_runner_invoked": False,
        "reused_helper_pin": HELPER_PIN,
        "own_test_host_token": own_token,
        "records": records,
        "release_errors": [],
        "no_retry": True,
    }
    try:
        input_pins = plan["input_pins"] + [{"path": str(plan_path), **pin(plan_path)}]
        helper._hold_directories(
            api,
            [Path(item["path"]).parent for item in input_pins] + [output],
            held,
            directories,
            deadline,
        )
        for item in input_pins:
            helper._locked_pin(
                api,
                Path(item["path"]),
                {"byte_length": item["byte_length"], "sha256": item["sha256"]},
                held,
            )
        for index, case in enumerate(CASES, 1):
            case_started = time.monotonic()
            case_deadline = min(deadline, case_started + 60)
            active_deadline = min(deadline - 5, case_started + 50)
            require(active_deadline > case_started, "batch budget exhausted")
            case_root = output / case
            case_root.mkdir()
            (case_root / "temp").mkdir()
            helper._hold_directories(
                api, [case_root, case_root / "temp"], held, directories, case_deadline
            )
            environment = {
                "SystemRoot": os.environ["SYSTEMROOT"],
                "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", "C:"),
                "TEMP": str(case_root / "temp"),
                "TMP": str(case_root / "temp"),
            }
            reservation = {
                "contract_version": "policy-owner-admission-test-chain-v2",
                "case": case,
                "index": index,
                "source_root": str(source),
                "case_root": str(case_root),
                "input_pins": plan["input_pins"],
                "python_sources": plan["python_sources"],
                "depot": plan["depot"],
                "allowed_read_roots": plan["allowed_read_roots"] + [str(output)],
                "environment": environment,
                "chain_limit_seconds": 60,
                "active_limit_seconds": 50,
                "cleanup_limit_seconds": 5,
                "retry": False,
            }
            if case == "native-contracts":
                vector = output / "synthetic-core/artifacts/interop-vector.json"
                vector_pin = pin(vector)
                helper._locked_pin(api, vector, vector_pin, held)
                reservation["input_pins"] = [
                    *reservation["input_pins"],
                    {"path": str(vector), **vector_pin},
                ]
                reservation["native_argv"] = [
                    plan["native_test_path"],
                    str(vector),
                    vector_pin["sha256"],
                    plan["formal_native_path"],
                    plan["formal_native_pin"]["sha256"],
                ]
            reservation_path = case_root / "reservation.json"
            write_new(reservation_path, canonical(reservation))
            helper._locked_pin(api, reservation_path, pin(reservation_path), held)
            record = {
                "case": case,
                "created": False,
                "go_sent": False,
                "job_assigned": False,
                "job_empty_confirmed": False,
                "controller_exit": None,
                "controller_exit_confirmed": False,
                "controller_handle_closed": False,
                "controller_handle_close_attempted": False,
                "job_observations": [],
                "job_query_failed": False,
                "errors": [],
                "status": "STOP_UNEXPECTED",
            }
            records.append(record)
            process = None
            job = None
            stdout = stderr = b""
            cleanup_started = None

            def close_controller(process, record) -> None:
                if process is None or record["controller_handle_close_attempted"]:
                    return
                record["controller_handle_close_attempted"] = True
                record["controller_handle_close_monotonic_before"] = time.monotonic()
                try:
                    process._handle.Close()
                    record["controller_handle_closed"] = True
                finally:
                    record["controller_handle_close_monotonic_after"] = time.monotonic()

            try:
                require(
                    time.monotonic() < active_deadline,
                    "chain active budget exhausted before Job creation",
                )
                job = api.CreateJobObjectW(None, None)
                api.check(job, "create new TEST outer Job")
                limits = helper._Extended()
                limits.limits.flags = 0x2000
                api.check(
                    api.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)),
                    "TEST Job kill-on-close",
                )
                require(
                    time.monotonic() < active_deadline,
                    "chain active budget exhausted before controller launch",
                )
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-S",
                        str(Path(__file__).absolute()),
                        "--controller",
                        str(reservation_path),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    cwd=case_root,
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                record["created"] = True
                api.check(
                    api.AssignProcessToJobObject(job, int(process._handle)),
                    "assign GO-blocked TEST controller",
                )
                record["job_assigned"] = True
                record["identity"] = helper._controller_identity(api, process, sys.executable)
                remaining = active_deadline - time.monotonic()
                require(remaining > 0, "chain active budget exhausted before GO")
                record["go_sent"] = True
                stdout, stderr = process.communicate(input=GO, timeout=remaining)
                cleanup_started = time.monotonic()
                record["controller_exit"] = process.returncode
                record["controller_exit_confirmed"] = True
                record["output_pins"] = {
                    "stdout": write_new(case_root / "controller.stdout", stdout),
                    "stderr": write_new(case_root / "controller.stderr", stderr),
                }
                require(
                    len(stdout) <= 2 * 1024 * 1024 and len(stderr) <= 2 * 1024 * 1024,
                    "TEST diagnostic output bound",
                )
                write_new(case_root / "before-controller-close.json", canonical(record))
                helper._job_observation(
                    api, job, record, "BEFORE_CONFIRMED_CONTROLLER_HANDLE_CLOSE"
                )
                close_controller(process, record)
                cleanup_deadline = min(case_deadline, cleanup_started + 5)
                while time.monotonic() < cleanup_deadline:
                    if helper._job_observation(
                        api, job, record, "AFTER_CONFIRMED_CONTROLLER_HANDLE_CLOSE"
                    ):
                        record["job_empty_confirmed"] = True
                        break
                    time.sleep(min(0.005, max(0, cleanup_deadline - time.monotonic())))
                require(
                    record["job_empty_confirmed"] and time.monotonic() < cleanup_deadline,
                    "owned TEST Job did not become empty within cleanup budget",
                )
                require(
                    process.returncode == 0, "unexpected TEST chain failure; stop all later cases"
                )
                record["status"] = "EXPECTED_TEST_OUTCOME_VERIFIED"
            except BaseException as error:
                record["errors"].append({"type": type(error).__name__, "message": str(error)})
                if isinstance(error, subprocess.TimeoutExpired):
                    stdout, stderr = error.output or b"", error.stderr or b""
                if not isinstance(error, Exception):
                    raise
            finally:
                if cleanup_started is None:
                    cleanup_started = time.monotonic()
                cleanup_deadline = min(case_deadline, cleanup_started + 5, deadline)
                if process is not None and not record["controller_exit_confirmed"]:
                    try:
                        if record["job_assigned"]:
                            api.check(
                                api.TerminateJobObject(job, 0xC000013A),
                                "terminate only new TEST Job",
                            )
                        else:
                            process.kill()  # controller was never given GO
                        if time.monotonic() < cleanup_deadline:
                            stdout, stderr = process.communicate(
                                timeout=cleanup_deadline - time.monotonic()
                            )
                            record["controller_exit"] = process.returncode
                            record["controller_exit_confirmed"] = True
                    except Exception as error:
                        record["errors"].append(
                            {
                                "stage": "OWNED_CLEANUP",
                                "type": type(error).__name__,
                                "message": str(error),
                            }
                        )
                # Persistence failure must never skip owned handle cleanup or
                # release the input fences while children remain unaccounted.
                try:
                    for name, raw in (("stdout", stdout), ("stderr", stderr)):
                        path = case_root / ("controller." + name)
                        try:
                            if not path.exists():
                                write_new(path, raw)
                            require(path.read_bytes() == raw, "retained raw output differs/partial")
                        except Exception as error:
                            record["errors"].append(
                                {
                                    "stage": "SAVE_" + name.upper(),
                                    "type": type(error).__name__,
                                    "message": str(error),
                                }
                            )
                            record["unsaved_" + name + "_base64"] = base64.b64encode(raw).decode(
                                "ascii"
                            )
                    if process is not None and not record["controller_handle_close_attempted"]:
                        try:
                            write_new(
                                case_root / "failure-before-controller-close.json",
                                canonical(record),
                            )
                        except Exception as error:
                            record["errors"].append(
                                {
                                    "stage": "SAVE_PRE_CLOSE",
                                    "type": type(error).__name__,
                                    "message": str(error),
                                }
                            )
                finally:
                    try:
                        try:
                            close_controller(process, record)
                        except Exception as error:
                            record["errors"].append(
                                {
                                    "stage": "CONTROLLER_HANDLE_CLOSE",
                                    "type": type(error).__name__,
                                    "message": str(error),
                                }
                            )
                    finally:
                        if job is not None:
                            try:
                                record["job_handle_closed"] = bool(api.CloseHandle(job))
                                if not record["job_handle_closed"]:
                                    record["errors"].append(
                                        {"stage": "JOB_CLOSE", "errno": c.get_last_error()}
                                    )
                            except Exception as error:
                                record["errors"].append(
                                    {
                                        "stage": "JOB_CLOSE",
                                        "type": type(error).__name__,
                                        "message": str(error),
                                    }
                                )
                record["cleanup_seconds"] = time.monotonic() - cleanup_started
                record["elapsed_seconds"] = time.monotonic() - case_started
                if (
                    record["errors"]
                    or record["cleanup_seconds"] > 5
                    or record["elapsed_seconds"] > 60
                ):
                    record["status"] = "STOP_UNEXPECTED"
            write_new(
                case_root / "chain-result.json",
                canonical(
                    {
                        **record,
                        "phase": "PRE_CHAIN_RESULT_SAVE_NOT_FINAL",
                        "final_budget": "REQUIRES_BATCH_POST_SAVE_RECORD_AND_HOST_EXIT",
                    }
                ),
            )
            record["elapsed_including_result_save_seconds"] = time.monotonic() - case_started
            require(
                record["status"] == "EXPECTED_TEST_OUTCOME_VERIFIED"
                and record["elapsed_including_result_save_seconds"] <= 60,
                "unexpected chain result; no later test may start",
            )
        summary["status"] = "DECLARED_ISOLATED_BATCH_PASSED_PLATFORM_UNVERIFIED"
    except BaseException as error:
        summary["error"] = {"type": type(error).__name__, "message": str(error)}
        if not isinstance(error, Exception):
            raise
    finally:
        for handle in reversed(held):
            if not api.CloseHandle(handle):
                summary["release_errors"].append(c.get_last_error())
        summary["elapsed_seconds"] = time.monotonic() - started
        summary["input_directory_protections"] = list(directories.values())
        summary["cumulative_chain_seconds"] = sum(
            record.get("elapsed_including_result_save_seconds", record.get("elapsed_seconds", 0))
            for record in records
        )
        if summary["release_errors"] or summary["elapsed_seconds"] > 900:
            summary["status"] = "STOP_PARTIAL_TEST_EVIDENCE_PRESERVED"
    write_new(
        output / "batch-result.json",
        canonical(
            {
                **summary,
                "phase": "PRE_BATCH_RESULT_SAVE_NOT_FINAL",
                "final_status": "REQUIRES_RETURN_CONSOLE_AND_ACTUAL_HOST_EXIT_BUDGET",
            }
        ),
    )
    summary["elapsed_after_final_save_seconds"] = time.monotonic() - started
    if summary["elapsed_after_final_save_seconds"] > 900:
        summary["status"] = "STOP_PARTIAL_TEST_EVIDENCE_PRESERVED"
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "DECLARED_ISOLATED_BATCH_PASSED_PLATFORM_UNVERIFIED" else 1


def main() -> int:
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "-I -B -S required"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", type=Path)
    group.add_argument("--controller", type=Path)
    args = parser.parse_args()
    return controller(args.controller) if args.controller else batch(args.plan)


if __name__ == "__main__":
    raise SystemExit(main())
