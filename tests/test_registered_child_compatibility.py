"""Actual TEST-child CLI boundaries; no owner, native launch, or authentication."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

CHILD = Path(__file__).resolve().parents[1] / "scripts/policy_owner/registered_test_child.py"


def context_fixture(instance: str, *, coverage: bool = True) -> dict:
    """Fresh unsigned context metadata matching Native ChildContext's families.

    This is only a compatibility probe. It cannot be used as a signed control,
    owner receipt or authority, and the CLI's inert rejection never uses it.
    """
    suffix = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    version, container = (2, 3) if coverage else (1, 2)
    return {
        "phase": "guard",
        "attempt_identity": f"policy-qualified-runtime-protection-attempt:v{version}:sha256:{suffix}",
        "packet_identity": f"policy-qualified-runtime-protection-control:v{container}:sha256:{suffix}",
        "prepared_identity": f"policy-qualified-runtime-protection-prepared:v{container}:sha256:{suffix}",
        "handoff": {
            "contract_version": f"policy-qualified-runtime-protection-handoff-v{container}",
            "symbol": "DISPOSABLE_FIXTURE",
            "execution_authorized": False,
            "owner_binding": {
                "contract_version": "policy-qualified-native-owner-binding-v1",
                "instance": instance,
                "execution_authorized": False,
            },
            "acceptance": {
                "contract_version": f"policy-qualified-downstream-envelope-v{version}",
                "execution_authorized": False,
            },
        },
    }


def invoke_child(tmp_path, context, *, operation="inert", instance="test-20991231-571"):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    environment.update(PYTHON_DOTENV_DISABLED="1", TRADINGAGENTS_OUTPUT_LANGUAGE="English")
    if context is not None:
        environment["TRADINGAGENTS_PROTECTION_CONTEXT"] = base64.b64encode(
            json.dumps(context).encode("utf-8")
        ).decode("ascii")
    return subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(CHILD), operation, instance],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_supported_coverage_context_reaches_the_plain_inert_prohibition(tmp_path):
    result = invoke_child(tmp_path, context_fixture("test-20991231-571"))
    assert result.returncode == 1
    assert result.stderr.decode("utf-8", "replace").splitlines()[-1] == (
        "ValueError: plain inert mode cannot process a handoff"
    )
    assert not list(tmp_path.iterdir())


def test_coverage_attempt_cannot_use_legacy_control(tmp_path):
    context = context_fixture("test-20991231-572")
    context["packet_identity"] = context["packet_identity"].replace("control:v3:", "control:v2:")
    result = invoke_child(tmp_path, context, instance="test-20991231-572")
    assert result.returncode == 1
    assert result.stderr.decode("utf-8", "replace").splitlines()[-1] == (
        "ValueError: TEST child has incompatible conditional context bindings"
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("coverage", (False, True))
def test_supported_families_keep_the_inert_handoff_prohibition(tmp_path, coverage):
    result = invoke_child(tmp_path, context_fixture("test-20991231-571", coverage=coverage))
    assert result.returncode == 1
    assert result.stderr.decode().splitlines()[-1] == (
        "ValueError: plain inert mode cannot process a handoff"
    )


@pytest.mark.parametrize("change", ("future", "namespace", "truncated", "uppercase", "missing"))
def test_bad_attempt_identity_is_rejected_before_imports_or_io(tmp_path, change):
    context = context_fixture("test-20991231-571")
    if change == "future":
        context["attempt_identity"] = context["attempt_identity"].replace(":v2:", ":v3:")
    elif change == "namespace":
        context["attempt_identity"] = context["attempt_identity"].replace("policy-qualified-", "")
    elif change == "truncated":
        context["attempt_identity"] = context["attempt_identity"][:-1]
    elif change == "uppercase":
        context["attempt_identity"] = context["attempt_identity"].rsplit(":", 1)[0] + ":" + "A" * 64
    else:
        context.pop("attempt_identity")
    result = invoke_child(tmp_path, context, operation="guard_only")
    assert result.returncode == 1
    assert result.stderr.decode().splitlines()[-1] == (
        "ValueError: TEST child has an invalid conditional attempt context"
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "change",
    (
        "control",
        "prepared",
        "handoff",
        "envelope",
        "phase",
        "owner",
        "authority",
        "missing_control",
        "missing_handoff",
    ),
)
def test_mixed_or_missing_context_bindings_fail_before_guard_work(tmp_path, change):
    context = context_fixture("test-20991231-571")
    if change in ("control", "prepared"):
        field = "packet_identity" if change == "control" else "prepared_identity"
        context[field] = context[field].replace(":v3:", ":v2:")
    elif change == "handoff":
        context["handoff"]["contract_version"] = "policy-qualified-runtime-protection-handoff-v2"
    elif change == "envelope":
        context["handoff"]["acceptance"]["contract_version"] = (
            "policy-qualified-downstream-envelope-v1"
        )
    elif change == "phase":
        context["phase"] = "step4"
    elif change == "owner":
        context["handoff"]["owner_binding"]["instance"] = "test-20991231-572"
    elif change == "authority":
        context["handoff"]["execution_authorized"] = True
    elif change == "missing_control":
        context.pop("packet_identity")
    else:
        context.pop("handoff")
    result = invoke_child(tmp_path, context, operation="guard_only")
    assert result.returncode == 1
    assert result.stderr.decode().splitlines()[-1] == (
        "ValueError: TEST child has incompatible conditional context bindings"
    )
    assert not list(tmp_path.iterdir())


def test_guard_requires_context_before_importing_product_modules(tmp_path):
    result = invoke_child(tmp_path, None, operation="guard_only")
    assert result.returncode == 1
    assert result.stderr.decode().splitlines()[-1] == (
        "ValueError: TEST guard requires an exact conditional context"
    )


def test_plain_inert_is_still_zero_workload(tmp_path):
    result = invoke_child(tmp_path, None)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert not list(tmp_path.iterdir())


def test_operational_namespace_never_reaches_guard_work(tmp_path):
    result = invoke_child(
        tmp_path, None, operation="guard_only", instance="operational-20991231-571"
    )
    assert result.returncode == 1
    assert result.stderr.decode().splitlines()[-1] == (
        "ValueError: native fixture requires an exact TEST instance"
    )


def test_driver_preflight_exercises_delivered_child_without_owner(tmp_path, monkeypatch):
    from scripts.policy_owner import run_registered_test as driver

    monkeypatch.setattr(driver, "SOURCE", CHILD.parents[2])
    monkeypatch.setattr(driver, "CLIENT_PYTHON", Path(sys.executable))
    monkeypatch.setattr(driver, "INSTANCE", "test-20991231-573")
    result = driver.child_compatibility_preflight(cwd=tmp_path)
    assert result["status"] == "TEST_CHILD_COMPATIBILITY_PREFLIGHT_PASS"
    assert [row["case"] for row in result["probes"]] == ["legacy", "coverage", "mixed-rejection"]
    assert result["owner_called"] is False
    assert result["guard_launched"] is False
    assert not list(tmp_path.iterdir())
