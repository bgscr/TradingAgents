"""ENROLLMENT_ONLY v2 worker entry; v1 dispatch/import closure is removed.

No owner constructor, reconciliation, ledger writer, .env loader or ordinary
tradingagents package initializer is reachable through this entrypoint.
"""

from __future__ import annotations

import importlib.machinery
import json
import sys
import traceback
import types
from pathlib import Path

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


OPERATIONS = {"inspect-v2", "adopt-closed-v2", "reserve-v2", "begin-creation-v2",
              "accept-v2", "verify-complete-v2"}


def _frame(raw: bytes) -> dict:
    if not 0 < len(raw) <= 4 * 1024 * 1024:
        raise ValueError("bounded v2 worker frame required")

    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate frame field")
            value[key] = item
        return value

    def invalid(value):
        raise ValueError("nonfinite frame scalar: " + value)

    frame = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=unique, parse_constant=invalid)
    if (type(frame) is not dict or set(frame) != {
            "contract_version", "mode", "anchor_v2", "lease_id", "operation", "payload"} or
            frame["contract_version"] != "policy-owner-trusted-worker-request-v2" or
            frame["mode"] != "ENROLLMENT_ONLY" or frame["operation"] not in OPERATIONS):
        raise ValueError("v1/generic worker route permanently disabled")
    return frame


def _namespace(source: Path) -> None:
    if "tradingagents" in sys.modules:
        raise ValueError("worker must begin with an uninitialized project namespace")
    package = types.ModuleType("tradingagents")
    package.__path__ = [str(source / "tradingagents")]
    package.__package__ = "tradingagents"
    package.__spec__ = importlib.machinery.ModuleSpec("tradingagents", loader=None, is_package=True)
    sys.modules["tradingagents"] = package


def bootstrap() -> tuple[Path, dict]:
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("v2 worker requires -I -B -S")
    # Reject the frame BEFORE loading any project code or opening owner state.
    frame = _frame(sys.stdin.buffer.read(4 * 1024 * 1024 + 1))
    source = Path(__file__).resolve(strict=True).parents[2]
    if source.name != "source":
        raise ValueError("worker must reside in the separately registered v2 source depot")
    _namespace(source)
    return source.parent, frame


def dispatch(request: dict, depot: Path | None = None) -> dict:
    # Public direct calls cannot re-enter the old v1 dispatcher either.
    _frame(json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    if depot is None:
        raise ValueError("actual registered v2 depot context required")
    from tradingagents.policy_owner_admission_v2 import dispatch_v2, validate_binding

    binding = validate_binding(request["anchor_v2"])
    # The root retains the original installation identity. The new implementation
    # depot is distinct and is checked independently by the Windows OS adapter.
    root = Path(binding["legacy_setup_binding"]["owner_root"])
    from tradingagents.policy_owner_admission_windows import authority_from_os

    authority = authority_from_os(request, depot)
    try:
        result = dispatch_v2(request, root, authority)
    except BaseException as primary:
        try:
            authority.close()
        except BaseException as release:
            raise BaseExceptionGroup("admission worker operation and release both failed", [primary, release]) from None
        raise
    else:
        authority.close()
        return result


def main() -> int:
    try:
        depot, request = bootstrap()
        result = dispatch(request, depot)
        from tradingagents.policy_owner_admission_crypto import canonical

        sys.stdout.buffer.write(canonical(result))
        sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        traceback.print_exception(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
