"""Offline guard, replay, and human-gate bindings; no analysis is launched."""

from __future__ import annotations

import base64
import builtins
import copy
import socket
from pathlib import Path

import pytest

from tests.test_runtime_protection_contract import attempt_inputs
from tradingagents.runtime_protection import (
    AttemptInputs,
    canonical_bytes,
    derive_attempt,
    evidence_identity,
    replay_attempt,
    seal_child_environment,
    validate_attempt_phase,
)
from tradingagents.runtime_protection_handoff import (
    bind_guard_receipt,
    build_guarded_launch_packet,
    confirm_protected_step4,
)


def prepared_evidence():
    environment = {
        "SystemRoot": r"C:\Windows",
        "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
        "PYTHON_DOTENV_DISABLED": "1",
        "PATH": r"{private_runtime_root}\base;{private_runtime_root}\venv\Scripts;C:\Windows\System32",
    }
    cwd = r"D:\qualified\cwd"
    attempt = derive_attempt(
        AttemptInputs(
            **attempt_inputs(
                environment_identity=evidence_identity(
                    "runtime-protection-environment:v1",
                    {"variables": environment, "working_directory": cwd, "credential_bindings": []},
                )
            )
        )
    )
    baseline = [
        {
            "relative_path": "",
            "directory": True,
            "object_identity": "12345678:0123456789abcdef",
            "descriptor_identity": "a" * 64,
            "control_flags": 0x9004,
            "content_sha256": None,
        }
    ]
    body = {
        "contract_version": "runtime-protection-prepared-v1",
        "attempt": attempt.model_dump(mode="json"),
        "materialization_identity": "fixture-materialization:v1:sha256:" + "1" * 64,
        "runtime_manifest_identity": attempt.inputs.runtime_content_identity,
        "app_manifest_identity": attempt.inputs.app_content_identity,
        "baseline": baseline,
        "baseline_identity": evidence_identity("runtime-protection-baseline:v1", baseline),
        "environment": environment,
        "working_directory": cwd,
        "credential_bindings": [],
        "mapped_configuration": {
            "cninfo_operator_minimum_interval_seconds": 7,
            "output_language": "English",
            "mainland_capability_routing_mode": "qualified_v1_shadow",
        },
        "protection_contract_version": "protected-runtime-tree-v3",
        "state": "PREPARED",
        "execution_authorized": False,
    }
    return {**body, "identity": evidence_identity("runtime-protection-prepared:v1", body)}


def handoff_fixture(prepared):
    inputs = prepared["attempt"]["inputs"]
    return {
        "contract_version": "runtime-protection-handoff-v1",
        "preparation_lineage_identity": inputs["preparation_lineage_identity"],
        "plan_version": "1.5",
        "plan_signature": "fixture-plan",
        "cninfo_profile_identity": "fixture-profile",
        "cninfo_source_policy_identity": "fixture-policy",
        "cninfo_operator_minimum_interval_seconds": 7,
        "checkpoint_identity": "fixture-checkpoint:v1:sha256:" + "a" * 64,
        "history_identity": "fixture-history:v1:sha256:" + "b" * 64,
        "calendar_revision_id": "fixture-calendar",
        "valid_before_exclusive": "2099-01-01T00:00:00Z",
        "symbol": "DISPOSABLE_FIXTURE",
        "analysis_session": inputs["completed_session"],
        "selected_analysts": ["fundamentals"],
        "output_language": "English",
        "legacy_authoritative": True,
        "qualified_shadow_authoritative": False,
        "execution_authorized": False,
    }


def observation(prepared, *, consumed=True):
    body = {
        "contract_version": "runtime-protection-observation-v1",
        "attempt_identity": prepared["attempt"]["attempt_identity"],
        "prepared_identity": prepared["identity"],
        "consumed": consumed,
        "baseline_matches": not consumed,
        "state": "INCOMPLETE_RETAINED" if consumed else "PREPARED",
        "completion": None,
    }
    return {**body, "identity": evidence_identity("runtime-protection-observation:v1", body)}


def packet_fixture(prepared):
    return build_guarded_launch_packet(
        prepared,
        guard_arguments=[
            "-I",
            "-B",
            prepared["attempt"]["paths"]["private_app_root"] + r"\guard.py",
        ],
        handoff=handoff_fixture(prepared),
        operation="guard_only",
    )


def test_retained_replay_never_opens_files_or_calls_live_io(monkeypatch):
    prepared = prepared_evidence()
    observed = observation(prepared)

    def forbidden(*args, **kwargs):
        raise AssertionError("historical replay attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    result = replay_attempt(prepared, observed)
    assert result["state"] == "INCOMPLETE_RETAINED"
    assert result["consumed"] and not result["reusable"] and not result["execution_authorized"]


def test_ready_and_packet_use_the_same_fresh_attempt_and_reject_history():
    prepared = prepared_evidence()
    packet = packet_fixture(prepared)
    for phase in ("readiness", "packet"):
        assert (
            validate_attempt_phase(
                prepared, packet, observation(prepared, consumed=False), phase=phase
            )["execution_authorized"]
            is False
        )
        with pytest.raises(ValueError, match="consumed"):
            validate_attempt_phase(prepared, packet, observation(prepared), phase=phase)
    with pytest.raises(ValueError, match="current native protection owner"):
        validate_attempt_phase(prepared, packet, observation(prepared), phase="step4")


@pytest.mark.parametrize(
    "field,value",
    (
        ("plan_version", "1.4"),
        ("cninfo_operator_minimum_interval_seconds", 0),
        ("cninfo_operator_minimum_interval_seconds", 8),
        ("output_language", "Chinese"),
        ("qualified_shadow_authoritative", True),
        ("legacy_authoritative", False),
        ("execution_authorized", True),
        ("selected_analysts", ["fundamentals", "market"]),
        ("analysis_session", "2026-01-05"),
    ),
)
def test_handoff_preserves_explicit_policy_and_authority(field, value):
    prepared = prepared_evidence()
    gate = handoff_fixture(prepared)
    gate[field] = value
    with pytest.raises(ValueError):
        build_guarded_launch_packet(
            prepared,
            guard_arguments=[
                "-I",
                "-B",
                prepared["attempt"]["paths"]["private_app_root"] + r"\guard.py",
            ],
            handoff=gate,
            operation="guard_then_live",
        )


def context_fixture():
    prepared = prepared_evidence()
    packet = packet_fixture(prepared)
    return {
        "phase": "guard",
        "attempt_identity": packet["attempt_identity"],
        "packet_identity": packet["identity"],
        "prepared_identity": prepared["identity"],
        "handoff": packet["handoff"],
        "runtime_manifest_identity": prepared["runtime_manifest_identity"],
        "app_manifest_identity": prepared["app_manifest_identity"],
        "environment_identity": prepared["attempt"]["inputs"]["environment_identity"],
        "runtime_config_identity": evidence_identity(
            "runtime-protection-deployed-configuration:v1", prepared["mapped_configuration"]
        ),
        "prospective_thread": prepared["attempt"]["inputs"]["prospective_thread"],
        "prospective_run": prepared["attempt"]["inputs"]["prospective_run"],
    }


def source_guard(context):
    gate = context["handoff"]
    body = {
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
    return {**body, "identity": evidence_identity("fixture-guard:v1", body)}


@pytest.mark.parametrize(
    "field,value",
    (
        ("retained_provenance_changed", True),
        ("checkpoint_original_exact_equal", False),
        ("run_or_thread_allocated", True),
        ("original_sqlite_opened", True),
        ("live_io", {"provider": 0, "model": 1, "network": 0, "CNINFO": 0}),
        ("prospective_thread", "f" * 16),
        ("ticket07_live_authorized", True),
    ),
)
def test_guard_history_and_live_io_boundaries_fail_closed(field, value):
    context = context_fixture()
    source = source_guard(context)
    assert bind_guard_receipt(source, context)["execution_authorized"] is False
    source.pop("identity")
    source[field] = value
    source["identity"] = evidence_identity("fixture-guard:v1", source)
    with pytest.raises(ValueError, match="scope failed"):
        bind_guard_receipt(source, context)


def test_step4_binds_actual_questionnaire_and_fresh_human_confirmation(monkeypatch):
    import tradingagents.runtime_protection_handoff as handoff

    context = context_fixture()
    context["phase"] = "step4"
    monkeypatch.setenv(
        "TRADINGAGENTS_PROTECTION_CONTEXT", base64.b64encode(canonical_bytes(context)).decode()
    )
    replies = iter([context["packet_identity"], "YES"])
    monkeypatch.setattr(builtins, "input", lambda _: next(replies))
    requests = []
    monkeypatch.setattr(
        handoff,
        "_exchange_with_owner",
        lambda owner, message: requests.append((copy.deepcopy(owner), copy.deepcopy(message))),
    )
    confirm_protected_step4(
        symbol="DISPOSABLE_FIXTURE",
        analysis_session=context["handoff"]["analysis_session"],
        selected_analysts=["fundamentals"],
        output_language="English",
        configuration=prepared_evidence()["mapped_configuration"],
    )
    assert len(requests) == 1 and requests[0][1]["literal_confirmation"] == "YES"
    with pytest.raises(ValueError, match="questionnaire"):
        confirm_protected_step4(
            symbol="DISPOSABLE_FIXTURE",
            analysis_session=context["handoff"]["analysis_session"],
            selected_analysts=["market"],
            output_language="English",
            configuration=prepared_evidence()["mapped_configuration"],
        )
    assert len(requests) == 1


def test_public_environment_refuses_plaintext_credentials():
    prepared = prepared_evidence()
    environment = dict(prepared["environment"], TUSHARE_TOKEN="fixture-only-plaintext")
    with pytest.raises(ValueError, match="injection"):
        seal_child_environment(environment, working_directory=prepared["working_directory"])


def test_cli_rejects_configuration_drift_before_questionnaire_io(monkeypatch):
    import cli.main as cli

    context = context_fixture()
    context["phase"] = "step4"
    monkeypatch.setenv(
        "TRADINGAGENTS_PROTECTION_CONTEXT", base64.b64encode(canonical_bytes(context)).decode()
    )
    monkeypatch.setattr(
        cli,
        "DEFAULT_CONFIG",
        {
            **prepared_evidence()["mapped_configuration"],
            "cninfo_operator_minimum_interval_seconds": 99,
        },
    )

    def forbidden():
        raise AssertionError("Questionnaire I/O occurred before configuration validation")

    monkeypatch.setattr(cli, "fetch_announcements", forbidden)
    with pytest.raises(ValueError, match="effective runtime configuration"):
        cli.get_user_selections()


@pytest.mark.parametrize("change", (None, "rounds", "model", "enhancement", "checkpoint"))
def test_assembled_cli_config_is_checked_before_asset_or_execution_work(monkeypatch, change):
    import cli.main as cli

    config = {**cli.DEFAULT_CONFIG, "max_debate_rounds": 2, "max_risk_discuss_rounds": 2,
              "checkpoint_enabled": True, "output_language": "English", "china_a_enhancement_preset": "basic"}
    selections = {
        "ticker": "DISPOSABLE_FIXTURE", "research_depth": 2, "shallow_thinker": config["quick_think_llm"],
        "deep_thinker": config["deep_think_llm"], "backend_url": config["backend_url"], "llm_provider": config["llm_provider"],
        "google_thinking_level": config.get("google_thinking_level"), "openai_reasoning_effort": config.get("openai_reasoning_effort"),
        "anthropic_effort": config.get("anthropic_effort"), "output_language": "English", "china_a_enhancement_preset": "basic",
    }
    context = context_fixture()
    context.update(phase="step4", runtime_config_identity=evidence_identity("runtime-protection-deployed-configuration:v1", config))
    monkeypatch.setenv("TRADINGAGENTS_PROTECTION_CONTEXT", base64.b64encode(canonical_bytes(context)).decode())
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli, "get_user_selections", lambda: selections)

    class ReachedNextBoundary(Exception):
        pass

    def next_boundary(_symbol):
        raise ReachedNextBoundary

    monkeypatch.setattr(cli, "resolve_mainland_instrument", next_boundary)
    checkpoint = True
    if change == "rounds":
        selections["research_depth"] = 3
    elif change == "model":
        selections["shallow_thinker"] = "changed-fixture-model"
    elif change == "enhancement":
        selections["china_a_enhancement_preset"] = "changed-fixture-preset"
    elif change == "checkpoint":
        checkpoint = False
    with pytest.raises(ValueError if change else ReachedNextBoundary):
        cli.run_analysis(checkpoint=checkpoint)
