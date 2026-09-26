"""Bindings for a guard and Step-4 under the same native protection owner.

The existing qualified guard owns checkpoint/history and policy validation.
This adapter checks its receipt and binds that result to the new attempt. It
does not turn preparation authority into execution authority. The native host
requires separate fresh human confirmation for a live handoff.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.policy_coverage_models import CoverageEnvelope, read_model
from tradingagents.policy_qualified_contract import DownstreamEnvelope, strict_json, utc
from tradingagents.policy_qualified_native import (
    NativeOwnerBinding,
    build_conditional_inert_control,
    conditional_container,
    parse_prepared_conditional,
)
from tradingagents.policy_qualified_results import PolicyQualifiedVerifier
from tradingagents.runtime_protection import (
    Identity,
    PreparedProtectionAttempt,
    build_launch_packet,
    canonical_bytes,
    evidence_identity,
)


class HandoffContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False, revalidate_instances="always"
    )
    contract_version: Literal["runtime-protection-handoff-v1"]
    preparation_lineage_identity: Identity
    plan_version: Literal["1.5"]
    plan_signature: str = Field(min_length=1)
    cninfo_profile_identity: str = Field(min_length=1)
    cninfo_source_policy_identity: str = Field(min_length=1)
    cninfo_operator_minimum_interval_seconds: int | float = Field(gt=0)
    checkpoint_identity: Identity
    history_identity: Identity
    calendar_revision_id: str = Field(min_length=1)
    valid_before_exclusive: str
    symbol: str = Field(min_length=1)
    analysis_session: str
    selected_analysts: tuple[Literal["fundamentals"], ...]
    output_language: Literal["English"]
    legacy_authoritative: Literal[True]
    qualified_shadow_authoritative: Literal[False]
    execution_authorized: Literal[False]

    @model_validator(mode="after")
    def _bounds(self) -> HandoffContract:
        if self.selected_analysts != ("fundamentals",):
            raise ValueError("Step-4 requires exactly Fundamentals Analyst")
        expiry = datetime.fromisoformat(self.valid_before_exclusive.replace("Z", "+00:00"))
        if expiry.tzinfo != timezone.utc:
            raise ValueError("handoff expiry must be explicit UTC")
        if any("\0" in value for value in self.model_dump().values() if isinstance(value, str)):
            raise ValueError("invalid handoff string")
        return self


class ConditionalHandoffContract(HandoffContract):
    contract_version: Literal["policy-qualified-runtime-protection-handoff-v2"]
    acceptance: DownstreamEnvelope
    owner_binding: NativeOwnerBinding

    @model_validator(mode="after")
    def _conditional_expiry(self) -> ConditionalHandoffContract:
        if utc(self.valid_before_exclusive) > utc(self.acceptance.core.expires_utc):
            raise ValueError("conditional handoff exceeds its acceptance window")
        return self


class CoverageHandoffContract(ConditionalHandoffContract):
    contract_version: Literal["policy-qualified-runtime-protection-handoff-v3"]
    acceptance: CoverageEnvelope


def parse_conditional_handoff(value):
    model = (
        CoverageHandoffContract
        if value.get("contract_version") == "policy-qualified-runtime-protection-handoff-v3"
        else ConditionalHandoffContract
    )
    return model.model_validate_json(canonical_bytes(value))


def build_guarded_launch_packet(
    prepared: Mapping[str, Any],
    *,
    guard_arguments: Sequence[str],
    handoff: Mapping[str, Any],
    operation: Literal["guard_only", "guard_then_live"],
) -> dict[str, Any]:
    """Seal future handoff structures; allocate no run or authorization receipt."""
    value = PreparedProtectionAttempt.model_validate_json(canonical_bytes(prepared))
    gate = HandoffContract.model_validate_json(canonical_bytes(handoff))
    if operation not in ("guard_only", "guard_then_live"):
        raise ValueError("unsupported protected handoff")
    if (
        gate.preparation_lineage_identity != value.attempt.inputs.preparation_lineage_identity
        or gate.analysis_session != value.attempt.inputs.completed_session
    ):
        raise ValueError("handoff preparation/session lineage mismatch")
    config = value.mapped_configuration
    if (
        config.get("cninfo_operator_minimum_interval_seconds")
        != gate.cninfo_operator_minimum_interval_seconds
        or config.get("output_language") != gate.output_language
        or config.get("mainland_capability_routing_mode") != "qualified_v1_shadow"
    ):
        raise ValueError("handoff changes sealed policy, pacing, or language")
    packet = build_launch_packet(prepared, child_arguments=guard_arguments)
    packet.pop("identity")
    packet.update(
        operation=operation,
        handoff=gate.model_dump(mode="json"),
        live_child_arguments=(
            [
                "-I",
                "-B",
                "-m",
                "cli.main",
                "analyze",
                "--qualified-routing-validation",
                "--qualified-routing-calendar-revision-id",
                gate.calendar_revision_id,
                "--checkpoint",
            ]
            if operation == "guard_then_live"
            else None
        ),
    )
    return {**packet, "identity": evidence_identity("runtime-protection-packet:v1", packet)}


def bind_guard_receipt(
    source_receipt: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    """Accept the qualified guard's zero-I/O and fail-closed history contract."""
    gate = HandoffContract.model_validate_json(canonical_bytes(context["handoff"]))
    body = {
        "contract_version": "runtime-protection-guard-v1",
        **_guard_payload(source_receipt, context, gate),
    }
    return {**body, "identity": evidence_identity("runtime-protection-guard:v1", body)}


def _guard_payload(
    source_receipt: Mapping[str, Any], context: Mapping[str, Any], gate: HandoffContract
) -> dict[str, Any]:
    receipt = dict(source_receipt)
    identity = receipt.pop("identity", "")
    namespace, separator, _ = identity.rpartition(":sha256:")
    if not separator or evidence_identity(namespace, receipt) != identity:
        raise ValueError("qualified guard receipt identity mismatch")
    for key, expected in {
        "status": "PASS",
        "packet_identity": context["packet_identity"],
        "checkpoint_identity": gate.checkpoint_identity,
        "history_identity": gate.history_identity,
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
            key: gate.model_dump(mode="json")[key]
            for key in (
                "plan_signature",
                "cninfo_profile_identity",
                "cninfo_source_policy_identity",
                "cninfo_operator_minimum_interval_seconds",
                "legacy_authoritative",
                "qualified_shadow_authoritative",
            )
        },
    }.items():
        if (
            key not in receipt
            or type(receipt[key]) is not type(expected)
            or receipt[key] != expected
        ):
            raise ValueError(f"qualified guard scope failed: {key}")
    return {
        "phase": "guard",
        "attempt_identity": context["attempt_identity"],
        "prepared_identity": context["prepared_identity"],
        "packet_identity": context["packet_identity"],
        "source_guard_identity": identity,
        "checkpoint_identity": gate.checkpoint_identity,
        "history_identity": gate.history_identity,
        "prospective_thread": context["prospective_thread"],
        "prospective_run": context["prospective_run"],
        "status": "PASS",
        "live_io": receipt["live_io"],
        "checkpoint_original_exact_equal": True,
        "retained_provenance_changed": False,
        "run_or_thread_allocated": False,
        "execution_authorized": False,
        **{
            key: receipt[key]
            for key in (
                "runtime_manifest_identity",
                "app_manifest_identity",
                "environment_identity",
                "runtime_config_identity",
                "plan_signature",
                "cninfo_profile_identity",
                "cninfo_source_policy_identity",
                "cninfo_operator_minimum_interval_seconds",
                "legacy_authoritative",
                "qualified_shadow_authoritative",
            )
        },
    }


def build_conditional_guard_control(
    prepared: Mapping[str, Any],
    *,
    guard_arguments: Sequence[str],
    handoff: Mapping[str, Any],
    policy_verifier: PolicyQualifiedVerifier,
) -> dict[str, Any]:
    """A forward guard-only control; it cannot produce a real Ticket-07 packet."""
    value = parse_prepared_conditional(prepared)
    gate = parse_conditional_handoff(handoff)
    inputs, config = value.attempt.inputs, value.mapped_configuration
    if (
        gate.acceptance != inputs.acceptance
        or gate.owner_binding != value.owner_binding
        or gate.preparation_lineage_identity != inputs.preparation_lineage_identity
        or gate.analysis_session != inputs.completed_session
        or config.get("cninfo_operator_minimum_interval_seconds")
        != gate.cninfo_operator_minimum_interval_seconds
        or config.get("output_language") != gate.output_language
        or config.get("mainland_capability_routing_mode") != "qualified_v1_shadow"
    ):
        raise ValueError(
            "conditional handoff changes history, source, owner, session or sealed configuration"
        )
    control = build_conditional_inert_control(
        prepared, child_arguments=guard_arguments, policy_verifier=policy_verifier
    )
    control.pop("identity")
    control.update(
        operation="guard_only", handoff=gate.model_dump(mode="json"), live_child_arguments=None
    )
    return {
        **control,
        "identity": evidence_identity(
            conditional_container("control", value.attempt, identity=True), control
        ),
    }


def bind_conditional_guard_receipt(
    source_receipt: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    policy_verifier: PolicyQualifiedVerifier | None = None,
) -> dict[str, Any]:
    gate = parse_conditional_handoff(context["handoff"])
    if policy_verifier is None:
        from tradingagents.windows_policy_owner import RegisteredWindowsPolicyVerifier

        policy_verifier = RegisteredWindowsPolicyVerifier(gate.owner_binding.instance)
    policy_verifier.verify_downstream(gate.acceptance)
    body = {
        "contract_version": (
            "policy-qualified-runtime-protection-guard-v3"
            if isinstance(gate, CoverageHandoffContract)
            else "policy-qualified-runtime-protection-guard-v2"
        ),
        **_guard_payload(source_receipt, context, gate),
        "acceptance": gate.acceptance.model_dump(mode="json"),
        "owner_binding": gate.owner_binding.model_dump(mode="json"),
    }
    return {
        **body,
        "identity": evidence_identity(
            "policy-qualified-runtime-protection-guard:v3"
            if isinstance(gate, CoverageHandoffContract)
            else "policy-qualified-runtime-protection-guard:v2",
            body,
        ),
    }


def _owner_context(phase: str) -> dict[str, Any]:
    raw = os.environ.get("TRADINGAGENTS_PROTECTION_CONTEXT")
    if not raw:
        raise ValueError("current native protection owner is required")
    context = json.loads(base64.b64decode(raw, validate=True))
    if context.get("phase") != phase:
        raise ValueError("protection owner phase mismatch")
    return context


def _exchange_with_owner(context: Mapping[str, Any], message: Mapping[str, Any]) -> dict[str, Any]:
    """Use a local Windows named pipe, bound to the still-running parent process.

    No filesystem security operation or historical protector is invoked here.
    The server also checks that this connection belongs to its direct child.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    digest = context["attempt_identity"].rsplit(":", 1)[1]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid owner pipe identity")
    pipe = rf"\\.\pipe\TradingAgentsProtection-{digest}"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    with open(pipe, "r+b", buffering=0) as stream:
        owner = wintypes.ULONG()
        if (
            not kernel.GetNamedPipeServerProcessId(
                msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(owner)
            )
            or owner.value != context["owner_pid"]
        ):
            raise ValueError("native protection owner process mismatch")
        handle = kernel.OpenProcess(0x1000, False, owner.value)
        if not handle:
            raise ValueError("native owner process is unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if (
                not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
                or (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
                != context["owner_creation_time"]
            ):
                raise ValueError("native owner process identity was reused")
        finally:
            kernel.CloseHandle(handle)
        stream.write(canonical_bytes(message) + b"\n")
        response = stream.readline(65537)
    if len(response) > 65536:
        raise ValueError("owner response exceeds bound")
    conditional = context.get("handoff", {}).get("contract_version") in {
        "policy-qualified-runtime-protection-handoff-v2",
        "policy-qualified-runtime-protection-handoff-v3",
    }
    result = strict_json(response) if conditional else json.loads(response)
    if (
        result.get("attempt_identity") != context["attempt_identity"]
        or result.get("packet_identity") != context["packet_identity"]
        or result.get("phase") != context["phase"]
        or result.get("accepted") is not True
    ):
        raise ValueError("native owner rejected protected phase")
    if conditional and (
        set(result)
        != {
            "phase",
            "attempt_identity",
            "packet_identity",
            "accepted",
            "acceptance",
            "owner_binding",
            "execution_authorized",
        }
        or (
            read_model(DownstreamEnvelope, result["acceptance"])
            != read_model(DownstreamEnvelope, context["handoff"]["acceptance"])
            or NativeOwnerBinding.read(result["owner_binding"])
            != NativeOwnerBinding.read(context["handoff"]["owner_binding"])
            or result["execution_authorized"] is not False
        )
    ):
        raise ValueError("native guard response lost the full conditional caveat or owner binding")
    return result


def validate_protected_configuration(
    configuration: Mapping[str, Any], *, phase: str
) -> dict[str, Any]:
    """Compare the imported effective configuration with the deployment seal."""
    context = _owner_context(phase)
    actual = evidence_identity("runtime-protection-deployed-configuration:v1", dict(configuration))
    if actual != context.get("runtime_config_identity"):
        raise ValueError("effective runtime configuration differs from protected deployment")
    return context


def run_bound_guard(qualified_guard: Callable[[], Mapping[str, Any]]) -> None:
    """Wrap an existing qualified zero-I/O guard while its native owner holds protection."""
    from tradingagents.default_config import DEFAULT_CONFIG

    context = validate_protected_configuration(DEFAULT_CONFIG, phase="guard")
    bind = (
        bind_conditional_guard_receipt
        if context["handoff"].get("contract_version")
        in {
            "policy-qualified-runtime-protection-handoff-v2",
            "policy-qualified-runtime-protection-handoff-v3",
        }
        else bind_guard_receipt
    )
    _exchange_with_owner(context, bind(qualified_guard(), context))


def confirm_protected_step4(
    *,
    symbol: str,
    analysis_session: str,
    selected_analysts: Sequence[str],
    output_language: str,
    configuration: Mapping[str, Any],
) -> None:
    """Enforce the optional protected-launch gate at the existing CLI Step 4."""
    context = validate_protected_configuration(configuration, phase="step4")
    gate = HandoffContract.model_validate_json(canonical_bytes(context["handoff"]))
    actual = {
        "symbol": symbol,
        "analysis_session": analysis_session,
        "selected_analysts": list(selected_analysts),
        "output_language": output_language,
    }
    if any(actual[key] != gate.model_dump(mode="json")[key] for key in actual):
        raise ValueError("Step-4 questionnaire differs from the sealed handoff")
    if input("Type the exact protected packet identity for Step 4: ") != context["packet_identity"]:
        raise ValueError("Step-4 human packet identity mismatch")
    confirmation = input(
        "Confirm this Step-4 selection under separate fresh authorization (type YES): "
    )
    if confirmation != "YES":
        raise ValueError("literal Step-4 YES is required")
    _exchange_with_owner(
        context,
        {
            "phase": "step4",
            "attempt_identity": context["attempt_identity"],
            "packet_identity": context["packet_identity"],
            "questionnaire": actual,
            "literal_confirmation": confirmation,
            "runtime_config_identity": evidence_identity(
                "runtime-protection-deployed-configuration:v1", dict(configuration)
            ),
        },
    )
