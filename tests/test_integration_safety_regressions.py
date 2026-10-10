"""Pure checks: no Windows objects, subprocesses, enrollment or live I/O."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.native_policy_gate import native_policy_tests_enabled
from tests.test_runtime_protection_handoff import handoff_fixture, prepared_evidence
from tradingagents.runtime_protection import canonical_bytes
from tradingagents.runtime_protection_handoff import _validate_owner_response


def response_fixture():
    context = {
        "phase": "step4",
        "attempt_identity": "attempt",
        "packet_identity": "packet",
        "handoff": handoff_fixture(prepared_evidence()),
    }
    response = (
        canonical_bytes(
            {
                "phase": "step4",
                "attempt_identity": "attempt",
                "packet_identity": "packet",
                "accepted": True,
            }
        )
        + b"\n"
    )
    return context, response


@pytest.mark.parametrize("offset", (0, 1, 60))
def test_complete_response_received_at_or_after_expiry_is_rejected(offset):
    context, response = response_fixture()
    received = datetime(2099, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
    with pytest.raises(ValueError, match="expired"):
        _validate_owner_response(context, response, now=lambda: received)


def test_fresh_complete_response_retains_only_existing_authority():
    context, response = response_fixture()
    result = _validate_owner_response(
        context, response, now=lambda: datetime(2098, 12, 31, tzinfo=timezone.utc)
    )
    assert result["accepted"] is True
    assert "execution_authorized" not in result


@pytest.mark.parametrize("mutation", (lambda data: data[:-1], lambda data: b"x" * 65536 + b"\n"))
def test_incomplete_or_oversized_response_cannot_release_child(mutation):
    context, response = response_fixture()
    with pytest.raises(ValueError, match="incomplete or exceeds"):
        _validate_owner_response(context, mutation(response))


@pytest.mark.parametrize(
    "platform,opt_in,enabled",
    [
        ("nt", "1", True),
        ("nt", "0", False),
        ("nt", None, False),
        ("posix", "1", False),
        ("posix", None, False),
    ],
)
def test_native_policy_requires_platform_and_explicit_opt_in(platform, opt_in, enabled):
    environment = {} if opt_in is None else {"TRADINGAGENTS_RUN_NATIVE_POLICY_TESTS": opt_in}
    assert native_policy_tests_enabled(environment, platform=platform) is enabled


def test_admission_retains_all_21_cases_outside_default_pytest_collection():
    from tests import test_policy_owner_admission_v2 as batch

    assert batch.__test__ is False
    assert len(unittest.defaultTestLoader.getTestCaseNames(batch.AdmissionTests)) == 21
    assert batch.CORE is None and batch.CRYPTO is None and batch.ARTIFACT_ROOT is None


def test_admission_entry_rejects_nonisolated_launch_before_allocating_artifacts(monkeypatch):
    from tests import test_policy_owner_admission_v2 as batch

    monkeypatch.setattr(
        batch,
        "sys",
        SimpleNamespace(
            flags=SimpleNamespace(isolated=0, no_site=0),
            dont_write_bytecode=False,
        ),
    )
    monkeypatch.setattr(batch, "_load", lambda: pytest.fail("must not import project"))
    with pytest.raises(RuntimeError, match="requires -I -B -S"):
        batch.main()
