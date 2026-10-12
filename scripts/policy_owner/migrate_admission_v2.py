"""Future explicit maintenance entry. Default is plan-only and performs no I/O."""

from __future__ import annotations

import argparse
import importlib.machinery
import json
import sys
import types
from pathlib import Path

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


def _error_details(error: BaseException) -> dict:
    result = {"type": type(error).__name__, "message": str(error)}
    if isinstance(error, BaseExceptionGroup):
        result["exceptions"] = [_error_details(item) for item in error.exceptions]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", choices=("prepare_guard", "post_boot"))
    parser.add_argument("--instance", default="operational-20260930-001")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-authorization")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "PLAN_ONLY_NO_IO", "steps": ["prepare_guard", "post_boot"],
            "new_personal_authorization_required": True, "service_stop_or_start": False,
            "automatic_reboot": False, "authentication": False, "field_path_tested": False}, sort_keys=True))
        return 0
    try:
        if not args.step or not args.confirm_authorization:
            raise ValueError("exact protected step authorization identity and explicit confirmation required")
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise ValueError("isolated pinned maintenance interpreter requires -I -B -S")
        source = Path(__file__).resolve(strict=True).parents[2]
        if source.name != "source" or "tradingagents" in sys.modules:
            raise ValueError("new registered depot and uninitialized namespace required")
        package = types.ModuleType("tradingagents")
        package.__path__ = [str(source / "tradingagents")]
        package.__package__ = "tradingagents"
        package.__spec__ = importlib.machinery.ModuleSpec("tradingagents", None, is_package=True)
        sys.modules["tradingagents"] = package
        from tradingagents.policy_owner_admission_migration import run_authorized_maintenance

        result = run_authorized_maintenance(args.instance, args.step, args.confirm_authorization, source.parent)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True), flush=True)
        return 0 if result["status"].startswith(("GUARD_ESTABLISHED", "V2_ACTIVATED_CLOSED")) else 2
    except BaseException as error:
        import traceback

        complete_trace = "".join(traceback.format_exception(error))
        print(json.dumps({"status": "STOP_PARTIAL_STATE_PRESERVED", "type": type(error).__name__,
            "message": str(error), "exceptions": _error_details(error), "traceback": complete_trace,
            "retry": False, "rollback_to_v1": False, "cleanup": False}, sort_keys=True), flush=True)
        sys.stderr.write(complete_trace)
        sys.stderr.flush()
        if not isinstance(error, Exception):
            raise
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
