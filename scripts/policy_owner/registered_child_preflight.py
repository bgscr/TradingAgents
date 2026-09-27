"""Non-consuming behavioral check of the delivered TEST child and schema families.

The child runs only its existing inert rejection path. No validation bypass,
guard generation, pipe, owner call, namespace, claim or authentication is used.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import get_args
from uuid import uuid4


def child_probe_arguments(python: Path, child: Path, instance: str) -> list[str]:
    return [str(python), "-I", "-B", "-S", str(child), "inert", instance]


def _schema(model) -> str:
    values = get_args(model.model_fields["contract_version"].annotation)
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("TEST child preflight needs an exact committed schema")
    return values[0]


def _contexts(instance):
    from tradingagents.policy_coverage_models import CoverageEnvelope
    from tradingagents.policy_qualified_contract import DownstreamEnvelope
    from tradingagents.policy_qualified_native import (
        NativeOwnerBinding,
        PreparedConditionalProtectionAttempt,
        PreparedCoverageProtectionAttempt,
    )
    from tradingagents.runtime_protection import (
        ConditionalProtectionAttempt,
        CoverageProtectionAttempt,
    )
    from tradingagents.runtime_protection_handoff import (
        ConditionalHandoffContract,
        CoverageHandoffContract,
    )

    for attempt, prepared, handoff, envelope in (
        (
            ConditionalProtectionAttempt,
            PreparedConditionalProtectionAttempt,
            ConditionalHandoffContract,
            DownstreamEnvelope,
        ),
        (
            CoverageProtectionAttempt,
            PreparedCoverageProtectionAttempt,
            CoverageHandoffContract,
            CoverageEnvelope,
        ),
    ):
        # Metadata only, never materialized as an attempt/model/authority. Read
        # expected families from committed models, not the child's own table.
        prefix, separator, version = _schema(attempt).rpartition("-v")
        if not separator:
            raise ValueError("Unexpected committed conditional attempt schema")
        digest = sha256(uuid4().bytes).hexdigest()
        yield {
            "phase": "guard",
            "attempt_identity": f"{prefix}:v{version}:sha256:{digest}",
            "prepared_identity": prepared.identity_namespace + ":sha256:" + digest,
            "packet_identity": prepared.identity_namespace.replace("-prepared:", "-control:")
            + ":sha256:"
            + digest,
            "handoff": {
                "contract_version": _schema(handoff),
                "execution_authorized": False,
                "owner_binding": {
                    "contract_version": _schema(NativeOwnerBinding),
                    "instance": instance,
                    "execution_authorized": False,
                },
                "acceptance": {
                    "contract_version": _schema(envelope),
                    "execution_authorized": False,
                },
            },
        }


def verify_child_compatibility(python, child, instance, *, cwd, expected_child_pin):
    python, child = Path(python), Path(child)
    raw = child.read_bytes()
    actual = {"byte_length": len(raw), "sha256": sha256(raw).hexdigest()}
    if actual != expected_child_pin:
        raise ValueError("Delivered TEST child differs from its verified source pin")
    arguments = child_probe_arguments(python, child, instance)
    contexts = list(_contexts(instance))
    mixed = deepcopy(contexts[1])
    mixed["packet_identity"] = contexts[0]["packet_identity"]
    cases = [
        ("legacy", contexts[0], "ValueError: plain inert mode cannot process a handoff"),
        ("coverage", contexts[1], "ValueError: plain inert mode cannot process a handoff"),
        (
            "mixed-rejection",
            mixed,
            "ValueError: TEST child has incompatible conditional context bindings",
        ),
    ]
    retained = []
    for label, context, expected in cases:
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP"}
        }
        environment.update(
            PYTHON_DOTENV_DISABLED="1",
            TRADINGAGENTS_OUTPUT_LANGUAGE="English",
            TRADINGAGENTS_PROTECTION_CONTEXT=base64.b64encode(
                json.dumps(context).encode()
            ).decode(),
        )
        result = subprocess.run(
            arguments, cwd=cwd, env=environment, capture_output=True, timeout=15, check=False
        )
        stderr = result.stderr.decode("utf-8", "strict")
        if (
            result.returncode != 1
            or result.stdout
            or not stderr.splitlines()
            or stderr.splitlines()[-1] != expected
        ):
            raise ValueError(
                f"Delivered TEST child compatibility probe failed ({label}): {stderr[-2048:]}"
            )
        retained.append(
            {
                "case": label,
                "context": context,
                "returncode": result.returncode,
                "stdout": "",
                "stderr": stderr,
            }
        )
    if child.read_bytes() != raw:
        raise ValueError("Delivered TEST child changed during compatibility preflight")
    return {
        "status": "TEST_CHILD_COMPATIBILITY_PREFLIGHT_PASS",
        "arguments": arguments,
        "child_pin": actual,
        "interpreter_sha256": sha256(python.read_bytes()).hexdigest(),
        "probes": retained,
        "scope": "VERSION_ONLY_NEGATIVE_PROBES_NOT_AUTHORITY",
        "guard_launched": False,
        "owner_called": False,
        "execution_authorized": False,
    }
