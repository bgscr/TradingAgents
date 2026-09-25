"""Inert child for the actual conditional native boundary; never an analyzer."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys


def check_environment():
    if not (sys.flags.isolated and sys.dont_write_bytecode):
        raise ValueError("native fixture requires -I -B")
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE") != "English":
        raise ValueError("native fixture lost the sealed English environment")
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        raise ValueError("native fixture requires disabled dotenv")
    if any(value != "0" for key, value in os.environ.items() if key.startswith("RUN_LIVE_")):
        raise ValueError("native fixture cannot have a live-test flag")


def enforce_no_workload(instance):
    """Enforce actual zero-workload behavior in this disposable child."""
    if not re.fullmatch(r"test-[0-9]{8}-[0-9]{3}", instance):
        raise ValueError("native fixture requires an exact TEST instance")
    owner = "C:\\ProgramData\\TA-PolicyOwner-" + instance + "\\bin\\PolicyOwner.exe"
    allowed_command = subprocess.list2cmdline([owner, "request", instance])
    encoded = os.environ.get("TRADINGAGENTS_PROTECTION_CONTEXT")
    pipe = None
    if encoded is not None:
        context = json.loads(base64.b64decode(encoded, validate=True))
        match = re.fullmatch(
            r"policy-qualified-runtime-protection-attempt:v1:sha256:([0-9a-f]{64})",
            context["attempt_identity"],
        )
        if match is None:
            raise ValueError("TEST child has an invalid conditional attempt context")
        pipe = r"\\.\pipe\TradingAgentsProtection-" + match.group(1)

    def audit(event, args):
        if event.startswith("socket.") and event not in {"socket.__new__", "socket.gethostname"}:
            raise RuntimeError("TEST child blocks network I/O")
        if event in {
            "sqlite3.connect",
            "os.system",
            "os.spawn",
            "os.exec",
            "os.mkdir",
            "os.remove",
            "os.rmdir",
            "os.rename",
            "os.link",
            "os.symlink",
            "os.chmod",
            "os.utime",
            "os.truncate",
        }:
            raise RuntimeError("TEST child blocks database, shell and physical mutation")
        if event == "subprocess.Popen" and (
            args[1] != allowed_command or args[0] not in (None, owner)
        ):
            raise RuntimeError("TEST child may invoke only its exact registered verifier client")
        if event == "open":
            writing = (isinstance(args[1], str) and any(c in args[1] for c in "wax+")) or (
                isinstance(args[2], int)
                and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            )
            # Match the exact native named pipe before any path normalization.
            # Integer descriptors are the subprocess's already-created stdio.
            if writing and not isinstance(args[0], int) and (pipe is None or args[0] != pipe):
                raise RuntimeError("TEST child blocks physical-file writes")

    sys.addaudithook(audit)
    return audit


def synthetic_guard_receipt(instance):
    """Manufacture guard input data; the real adapter and native pipe verify it.

    The checkpoint/history labels name nonexistent TEST objects. This fixture
    never opens a checkpoint and cannot establish a real source-guard result.
    """
    from tradingagents.runtime_protection import evidence_identity

    context = json.loads(base64.b64decode(os.environ["TRADINGAGENTS_PROTECTION_CONTEXT"]))
    gate = context["handoff"]
    if (
        gate["symbol"] != "DISPOSABLE_FIXTURE"
        or gate["owner_binding"]["instance"] != instance
    ):
        raise ValueError("native child refuses a non-TEST handoff")
    body = {
        "label": "SYNTHETIC GUARD INPUT ONLY; NOT A REAL CHECKPOINT OR QUALIFICATION GUARD",
        "status": "PASS",
        "packet_identity": context["packet_identity"],
        "checkpoint_identity": gate["checkpoint_identity"],
        "history_identity": gate["history_identity"],
        "checkpoint_original_exact_equal": True,
        "retained_provenance_changed": False,
        "run_or_thread_allocated": False,
        "prelaunch_authorization_granted": False,
        "ticket07_live_authorized": False,
        "original_sqlite_opened": False,
        "forbidden_events": [],
        "prospective_thread": context["prospective_thread"],
        "prospective_run": context["prospective_run"],
        "live_io": {"provider": 0, "model": 0, "network": 0, "CNINFO": 0},
        **{
            key: context[key]
            for key in (
                "runtime_manifest_identity",
                "app_manifest_identity",
                "environment_identity",
                "runtime_config_identity",
            )
        },
        **{
            key: gate[key]
            for key in (
                "plan_signature",
                "cninfo_profile_identity",
                "cninfo_source_policy_identity",
                "cninfo_operator_minimum_interval_seconds",
                "legacy_authoritative",
                "qualified_shadow_authoritative",
            )
        },
    }
    return {**body, "identity": evidence_identity("synthetic-native-guard-input:v1", body)}


def main():
    check_environment()
    if len(sys.argv) != 3:
        raise ValueError("exact TEST operation and instance arguments required")
    operation, instance = sys.argv[1:]
    enforce_no_workload(instance)
    if operation == "inert":
        if "TRADINGAGENTS_PROTECTION_CONTEXT" in os.environ:
            raise ValueError("plain inert mode cannot process a handoff")
    elif operation == "guard_only":
        from tradingagents.runtime_protection_handoff import run_bound_guard

        run_bound_guard(lambda: synthetic_guard_receipt(instance))
    else:
        raise ValueError("only the exact inert/guard_only TEST operations exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
