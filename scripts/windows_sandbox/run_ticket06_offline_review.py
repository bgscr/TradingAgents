"""Deterministic offline Ticket 06 review checks for the Sandbox guest.

The ticket's judgment review remains a human/agent responsibility.  This
executable substitute records only the objective offline checks permitted in a
network-disabled guest; it never invokes an external reviewer or model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from ticket06_acceptance_contract import (
        TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        QualifiedRunnerManifest,
        Ticket06CommandLedger,
        build_ticket06_command_ledger,
        content_identity,
    )
except ModuleNotFoundError:  # Host-side parser/static tests.
    from tradingagents.windows_sandbox_acceptance import (
        TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        QualifiedRunnerManifest,
        Ticket06CommandLedger,
        build_ticket06_command_ledger,
        content_identity,
    )

_CONTROL = Path(r"C:\QCF17\Input\Control\run-control.json")
_RUNNER_MANIFEST = Path(r"C:\QCF17\Input\Runner\runner-manifest.json")


def _load_json(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > 262_144:
        raise RuntimeError("offline_review_input_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("offline_review_input_invalid")
    return value


def run_offline_review(stage: str) -> str:
    control = _load_json(_CONTROL)
    base = control.get("base")
    if not isinstance(base, dict) or base.get("execution_mode") != "ticket06_acceptance":
        raise RuntimeError("offline_review_control_invalid")
    environment = base.get("environment")
    if not isinstance(environment, dict):
        raise RuntimeError("offline_review_environment_invalid")
    policy_identity = environment.get("policy_identity")
    if not isinstance(policy_identity, str):
        raise RuntimeError("offline_review_environment_invalid")
    ledger = Ticket06CommandLedger.model_validate(base.get("command_ledger"))
    if ledger != build_ticket06_command_ledger(policy_identity):
        raise RuntimeError("offline_review_ledger_invalid")
    runner = QualifiedRunnerManifest.model_validate(_load_json(_RUNNER_MANIFEST))
    if runner.runner_input_identity != base.get("runner_input_identity"):
        raise RuntimeError("offline_review_runner_binding_invalid")

    if stage == "spec_compliance":
        material = {
            "execution_mode": base["execution_mode"],
            "ledger_identity": ledger.ledger_identity,
            "result_schema_identity": TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
            "runner_input_identity": runner.runner_input_identity,
            "qcf17_authority": "guest_clause_a_only",
            "qcf18_authority": "conditional_host_owned",
        }
    elif stage == "code_quality":
        if not all(
            entry.mandatory
            and entry.argv[0] in {"$PYTHON", "$GIT"}
            and not entry.expected_result_parser.raw_output_exported
            and not entry.expected_result_parser.failure_text_exported
            and entry.timeout_policy.timeout_seconds > 0
            for entry in ledger.entries
        ):
            raise RuntimeError("offline_review_command_safety_invalid")
        material = {
            "command_count": len(ledger.entries),
            "ledger_identity": ledger.ledger_identity,
            "shell_command_strings_permitted": (
                ledger.shell_command_strings_permitted
            ),
            "dynamic_command_interpolation_permitted": (
                ledger.dynamic_command_interpolation_permitted
            ),
            "raw_output_exported": False,
        }
    else:
        raise RuntimeError("offline_review_stage_invalid")
    return content_identity(f"ticket06-offline-{stage}-review:v1", material)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        required=True,
        choices=("spec_compliance", "code_quality"),
    )
    arguments = parser.parse_args()
    try:
        identity = run_offline_review(arguments.stage)
    except (OSError, TypeError, ValueError, RuntimeError):
        return 1
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
