"""Execute the sealed Ticket 06 ledger inside one Windows Sandbox guest.

The public :func:`execute_ticket06_acceptance` seam accepts an injected process
executor for deterministic ISO-01A regressions.  The CLI path uses only fixed
argv vectors with ``shell=False`` and never exports captured command output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    from ticket06_acceptance_contract import (
        AcceptanceCommandStatus,
        AcceptanceParserKind,
        Ticket06AcceptanceControlEnvelope,
        Ticket06AcceptanceResult,
        Ticket06AcceptanceStaticBindings,
        Ticket06CommandEntry,
        Ticket06CommandLedger,
        Ticket06ControlEnvironment,
        build_ticket06_acceptance_result,
        build_ticket06_command_result,
        build_ticket06_completion_marker,
        content_identity,
        validate_qualified_runner_input,
    )
except ModuleNotFoundError:  # Host-side deterministic unit tests.
    from tradingagents.windows_sandbox_acceptance import (
        AcceptanceCommandStatus,
        AcceptanceParserKind,
        Ticket06AcceptanceControlEnvelope,
        Ticket06AcceptanceResult,
        Ticket06AcceptanceStaticBindings,
        Ticket06CommandEntry,
        Ticket06CommandLedger,
        Ticket06ControlEnvironment,
        build_ticket06_acceptance_result,
        build_ticket06_command_result,
        build_ticket06_completion_marker,
        content_identity,
        validate_qualified_runner_input,
    )

_PYTHON_ROLE = "$PYTHON"
_GIT_ROLE = "$GIT"
_CREDENTIAL_NAME = re.compile(
    r"(?i)(?:API_?KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|COOKIE)"
)
_PYTEST_COUNT = re.compile(
    rb"(?P<count>[0-9]{1,6})\s+"
    rb"(?P<kind>passed|failed|skipped|xfailed|xpassed|deselected)\b"
)


@dataclass(frozen=True, slots=True)
class BoundedProcessObservation:
    returncode: int
    output: bytes = field(default=b"", repr=False)
    timed_out: bool = False
    output_limit_exceeded: bool = False


ProcessExecutor = Callable[
    [tuple[str, ...], Path, dict[str, str], int, int],
    BoundedProcessObservation,
]


def _is_safe_directory(path: Path, *, empty: bool = False) -> bool:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return False
    return not empty or not any(resolved.iterdir())


def _resolve_argv(
    entry: Ticket06CommandEntry,
    *,
    python_executable: Path,
    git_executable: Path,
) -> tuple[str, ...]:
    roles = {
        _PYTHON_ROLE: str(python_executable),
        _GIT_ROLE: str(git_executable),
    }
    return tuple(roles.get(argument, argument) for argument in entry.argv)


def _pytest_counts(output: bytes) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    for match in _PYTEST_COUNT.finditer(output[-65_536:]):
        counts[match.group("kind").decode("ascii")] = int(match.group("count"))
    return counts


def _offline_review_output_matches(
    entry: Ticket06CommandEntry,
    output: bytes,
) -> bool:
    if len(entry.argv) < 2 or entry.argv[-2] != "--stage":
        return False
    expected = re.compile(
        (
            rf"ticket06-offline-{re.escape(entry.argv[-1])}-review:"
            r"v1:sha256:[0-9a-f]{64}"
        ).encode("ascii")
    )
    return expected.fullmatch(output.strip()) is not None


def _bounded_command_result(
    entry: Ticket06CommandEntry,
    observation: BoundedProcessObservation,
):
    counts = _pytest_counts(observation.output)
    if observation.timed_out:
        status = AcceptanceCommandStatus.TIMEOUT
    elif observation.returncode != 0 or observation.output_limit_exceeded:
        status = AcceptanceCommandStatus.FAIL
    elif entry.expected_result_parser.kind is AcceptanceParserKind.PYTEST_SUMMARY:
        if (
            counts["passed"] == 0
            or counts["failed"]
            or counts["xfailed"]
            or counts["xpassed"]
        ):
            status = AcceptanceCommandStatus.FAIL
            if counts["failed"] == 0:
                counts["failed"] = 1
        else:
            status = AcceptanceCommandStatus.PASS
    elif entry.expected_result_parser.kind is AcceptanceParserKind.CLEAN_WORKSPACE:
        status = (
            AcceptanceCommandStatus.PASS
            if not observation.output.strip()
            else AcceptanceCommandStatus.FAIL
        )
    elif entry.expected_result_parser.kind is AcceptanceParserKind.OFFLINE_REVIEW:
        status = (
            AcceptanceCommandStatus.PASS
            if _offline_review_output_matches(entry, observation.output)
            else AcceptanceCommandStatus.FAIL
        )
    else:
        status = AcceptanceCommandStatus.PASS
    if status is AcceptanceCommandStatus.FAIL and counts["failed"] == 0:
        counts["failed"] = 1
    return build_ticket06_command_result(
        entry,
        status=status,
        returncode=observation.returncode,
        output_limit_exceeded=observation.output_limit_exceeded,
        **counts,
    )


def _not_run_result(entry: Ticket06CommandEntry):
    return build_ticket06_command_result(
        entry,
        status=AcceptanceCommandStatus.NOT_RUN,
    )


def execute_ticket06_acceptance(
    *,
    static: Ticket06AcceptanceStaticBindings,
    ledger: Ticket06CommandLedger,
    live_environment: tuple[tuple[str, str], ...],
    workspace: Path,
    results_root: Path,
    python_executable: Path,
    git_executable: Path,
    executor: ProcessExecutor,
) -> Ticket06AcceptanceResult:
    """Execute the complete ledger sequentially and write a bounded export."""

    workspace = workspace.resolve(strict=True)
    results_root = results_root.resolve(strict=True)
    if not _is_safe_directory(workspace) or not _is_safe_directory(
        results_root,
        empty=True,
    ):
        raise ValueError("ticket06_acceptance_directory_contract_invalid")
    if ledger.ledger_identity != static.command_ledger_identity:
        raise ValueError("ticket06_acceptance_ledger_binding_invalid")
    try:
        process_environment = Ticket06ControlEnvironment(
            variables=live_environment,
            policy_identity=static.environment_policy_identity,
        )
    except ValueError as exc:
        raise ValueError("ticket06_acceptance_environment_policy_invalid") from exc
    environment = dict(process_environment.variables)
    if any(_CREDENTIAL_NAME.search(name) for name in environment):
        raise ValueError("ticket06_acceptance_credential_environment_forbidden")

    command_results = []
    stopped = False
    for entry in ledger.entries:
        if stopped:
            command_results.append(_not_run_result(entry))
            continue
        argv = _resolve_argv(
            entry,
            python_executable=python_executable,
            git_executable=git_executable,
        )
        observation = executor(
            argv,
            workspace,
            environment.copy(),
            entry.timeout_policy.timeout_seconds,
            entry.timeout_policy.maximum_captured_output_bytes,
        )
        result = _bounded_command_result(entry, observation)
        command_results.append(result)
        if entry.mandatory and result.status is not AcceptanceCommandStatus.PASS:
            stopped = True

    acceptance_result = build_ticket06_acceptance_result(
        static=static,
        ledger=ledger,
        command_results=tuple(command_results),
        live_environment=tuple(
            item
            for item in live_environment
            if item[0] == "PYTHON_DOTENV_DISABLED"
            or item[0].startswith("RUN_LIVE_")
        ),
    )
    result_bytes = acceptance_result.canonical_json_bytes()
    marker = build_ticket06_completion_marker(
        result=acceptance_result,
        result_bytes=result_bytes,
    )
    result_path = results_root / "result.json"
    completion_path = results_root / "completion.json"
    with result_path.open("xb") as handle:
        handle.write(result_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    with completion_path.open("xb") as handle:
        handle.write(marker.canonical_json_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    return acceptance_result


def run_bounded_process(
    argv: tuple[str, ...],
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> BoundedProcessObservation:
    """Run one fixed argv while draining and bounding non-exported output."""

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
        )
    except OSError:
        return BoundedProcessObservation(returncode=-1)
    assert process.stdout is not None
    output = bytearray()
    output_limit_exceeded = threading.Event()

    def drain() -> None:
        while True:
            chunk = process.stdout.read(65_536)
            if not chunk:
                break
            remaining = maximum_output_bytes - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                output_limit_exceeded.set()

    reader = threading.Thread(target=drain, name="ticket06-output-drain", daemon=True)
    reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            returncode = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=15)
    reader.join(timeout=15)
    if reader.is_alive():
        process.kill()
        returncode = process.wait(timeout=15)
        timed_out = True
    return BoundedProcessObservation(
        returncode=returncode,
        output=bytes(output),
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded.is_set(),
    )


def _load_control(path: Path) -> Ticket06AcceptanceControlEnvelope:
    if not path.is_file() or path.stat().st_size > 262_144:
        raise ValueError("ticket06_acceptance_control_invalid")
    return Ticket06AcceptanceControlEnvelope.model_validate_json(path.read_bytes())


def _verify_reconstructed_source(
    path: Path,
    *,
    expected: Ticket06AcceptanceStaticBindings,
) -> None:
    if not path.is_file() or path.stat().st_size > 65_536:
        raise ValueError("ticket06_reconstructed_source_evidence_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or content_identity(
        "windows-sandbox-reconstructed-source:v1",
        value,
    ) != expected.reconstructed_source_tree_identity:
        raise ValueError("ticket06_reconstructed_source_identity_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--runner-input", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--git-executable", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        control = _load_control(arguments.control)
        validate_qualified_runner_input(
            arguments.runner_input,
            expected_identity=control.base.runner_input_identity,
        )
        expected_git = Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe")
        expected_python = Path(r"C:\QCF17\Venv\Scripts\python.exe")
        if (
            arguments.control != Path(r"C:\QCF17\Input\Control\run-control.json")
            or arguments.runner_input != Path(r"C:\QCF17\Input\Runner")
            or arguments.results != Path(r"C:\QCF17\Export")
            or arguments.workspace != Path(control.base.guest_layout.workspace_repository)
            or arguments.git_executable != expected_git
            or Path(sys.executable) != expected_python
        ):
            raise ValueError("ticket06_acceptance_pinned_path_mismatch")
        _verify_reconstructed_source(
            Path(control.base.guest_layout.guest_output)
            / "workspace-verification.json",
            expected=control.derived,
        )
        result = execute_ticket06_acceptance(
            static=control.derived,
            ledger=control.base.command_ledger,
            live_environment=control.base.environment.variables,
            workspace=arguments.workspace,
            results_root=arguments.results,
            python_executable=expected_python,
            git_executable=expected_git,
            executor=run_bounded_process,
        )
    except (OSError, TypeError, ValueError):
        return 1
    return 0 if result.guest_claim == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
