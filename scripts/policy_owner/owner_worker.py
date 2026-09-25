"""Private, pinned verifier worker for the registered native Windows service.

This entrypoint cannot launch qualification or application processes. Windows
Hello, caller authentication, store protection and service registration are native
owner responsibilities, completed before this Python interpreter is started.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path


def bootstrap() -> tuple[Path, dict]:
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("trusted owner worker requires -I -B -S")
    root = Path(__file__).resolve().parents[2]
    if root.name != "source":
        raise ValueError("worker must be in a registered source depot")
    root = root.parent
    raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("worker request exceeds its bounded size")
    sys.path[:0] = [str(root / "source"), str(root / "dependencies")]
    # No environment-selected source, dependency, credential or output path is
    # honored. The interpreter was pinned by the native service before launch.
    for name in list(os.environ):
        if name not in {"SystemRoot", "SYSTEMROOT", "PATH", "TEMP", "TMP"}:
            os.environ.pop(name, None)
    os.environ.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "RUN_LIVE_PROVIDER_TESTS": "0",
            "RUN_LIVE_DEEPSEEK_TESTS": "0",
            "TRADINGAGENTS_PROJECT_ROOT": str(root / "state" / "runtime"),
            "TRADINGAGENTS_RESULTS_DIR": str(root / "state" / "runtime" / "results"),
            "TRADINGAGENTS_CACHE_DIR": str(root / "state" / "runtime" / "cache"),
            "TRADINGAGENTS_MEMORY_LOG_PATH": str(root / "state" / "runtime" / "memory.json"),
            "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH": str(
                root / "state" / "runtime" / "market.sqlite3"
            ),
            "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT": str(
                root / "state" / "runtime" / "payloads"
            ),
            "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT": str(root / "state" / "runtime" / "backups"),
        }
    )

    def writable(value: object) -> None:
        if isinstance(value, int):
            return
        path = Path(os.fsdecode(value)).resolve(strict=False)
        if not any(
            path == target or path.is_relative_to(target)
            for target in (root / "state", root / "artifacts")
        ):
            raise RuntimeError(
                "owner worker cannot write outside its registered state/artifact store"
            )

    def audit(event: str, values: tuple) -> None:
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.startfile",
            "os.startfile/2",
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.gethostbyname",
        }:
            raise RuntimeError(
                "owner verifier cannot launch processes, providers or network operations"
            )
        if event == "open":
            if values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                writable(values[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime"}:
            writable(values[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            writable(values[0])
            writable(values[1])
        elif event == "sqlite3.connect":
            from urllib.parse import unquote, urlparse

            target = str(values[0])
            if target.startswith("file:"):
                target = unquote(urlparse(target).path).lstrip("/")
            writable(target)

    sys.addaudithook(audit)
    from tradingagents.policy_qualified_contract import strict_json

    request = strict_json(raw)
    if (
        set(request)
        != {
            "contract_version",
            "anchor",
            "now_utc",
            "operation",
            "payload",
            "authenticated_records",
        }
        or request["contract_version"] != "policy-owner-trusted-worker-request-v1"
    ):
        raise ValueError("unknown native-to-worker contract")
    if Path(request["anchor"]["root"]).resolve(strict=True) != root:
        raise ValueError("worker was launched from a different registered source depot")
    return root, request


def dispatch(request: dict) -> dict:
    from dataclasses import asdict

    from tradingagents.policy_qualified_contract import (
        AcceptancePolicy,
        BoundEvidence,
        BytePin,
        DownstreamEnvelope,
        EvidenceRef,
        OperatorApproval,
        PolicyQualifiedExecutionResult,
        canonical_bytes,
        content_identity,
    )
    from tradingagents.policy_qualified_results import PolicyQualifiedVerifier, reference
    from tradingagents.windows_policy_owner import LocalWindowsPolicyOwner, encode_resolution

    owner = LocalWindowsPolicyOwner(
        request["anchor"], request["authenticated_records"], request["now_utc"]
    )
    owner.reconcile_authenticated_records()
    verifier = PolicyQualifiedVerifier(owner=owner, ledger=owner.ledger)
    operation, payload = request["operation"], request["payload"]

    def fields(*names: str) -> None:
        if not isinstance(payload, dict) or set(payload) != set(names):
            raise ValueError("owner operation payload has missing or unknown fields")

    if operation == "validate-authentication-document":
        fields("purpose", "document")
        return owner.authentication_validation_window(payload["purpose"], payload["document"])
    if operation == "apply-authenticated-record":
        return owner.apply_authenticated_record(payload)
    if operation == "resolve":
        fields("approval_identity")
        return {"authority": encode_resolution(owner.resolve(payload["approval_identity"]))}
    if operation == "read-artifact":
        fields("reference")
        artifact_bytes = owner.read_artifact(EvidenceRef.read(payload["reference"]))
        if len(artifact_bytes) > 32 * 1024 * 1024:
            raise ValueError(
                "large artifact export requires evidence-read-chunk-v2; inline downgrade rejected"
            )
        return {"bytes_base64": base64.b64encode(artifact_bytes).decode("ascii")}
    if operation == "artifact-reference-v2":
        fields("reference")
        reference = EvidenceRef.read(payload["reference"])
        owner.read_artifact(reference)
        return {
            "reference": reference.model_dump(mode="json"),
            "registered": True,
            "execution_authorized": False,
        }
    if operation == "read-pin":
        fields("pin")
        pinned_bytes = owner.read_pinned_file(BytePin.read(payload["pin"]))
        if len(pinned_bytes) > 32 * 1024 * 1024:
            raise ValueError(
                "large pinned evidence requires its registered reference and v2 chunk export"
            )
        return {"bytes_base64": base64.b64encode(pinned_bytes).decode("ascii")}
    if operation == "original-acquisition":
        fields("observation_id")
        return {
            "acquisition": owner.original_acquisition(payload["observation_id"]).model_dump(
                mode="json"
            )
        }
    if operation == "canonical-allocation":
        fields("lineage_plan_identity")
        return {
            "allocation": owner.canonical_allocation(payload["lineage_plan_identity"]).model_dump(
                mode="json"
            )
        }
    if operation == "observation":
        fields("observation_id")
        value = owner.observation(payload["observation_id"])
        result = asdict(value)
        result["artifact"] = value.artifact.model_dump(mode="json")
        result["measurement_contract_pin"] = value.measurement_contract_pin.model_dump(mode="json")
        return {"observation": result}
    if operation == "executed-stage":
        fields("lineage_identity", "kind")
        return {
            "bytes_base64": base64.b64encode(
                owner.executed_stage(payload["lineage_identity"], payload["kind"])
            ).decode("ascii")
        }
    if operation == "review-principal":
        fields("reference")
        return {"principal": owner.review_principal(EvidenceRef.read(payload["reference"]))}
    if operation == "registered-root":
        fields("lineage_identity", "role")
        if payload["role"] not in {"guest_export", "source_input", "runtime_bundle"}:
            raise ValueError("unknown retained qualification root role")
        return {"path": str(owner._root(payload["lineage_identity"], payload["role"]))}
    if operation in {
        "verify-envelope",
        "verify-attempt",
        "verify-materialization",
        "verify-inert-launch",
        "report",
    }:
        from tradingagents.policy_qualified_native import (
            InertFixtureLaunch,
            NativeOwnerBinding,
            NativePayloadProvenance,
            PreparedConditionalProtectionAttempt,
            seal_conditional_materialization_plan,
        )

        extra = {}
        native = operation in {"verify-attempt", "verify-materialization", "verify-inert-launch"}
        if native:
            from tradingagents.policy_qualified_contract import strict_json

            source = strict_json((owner.root / "native-consumer-source.json").read_bytes())
            if (
                source["identity"]
                != content_identity(
                    "policy-qualified-native-consumer-source:v1",
                    {key: value for key, value in source.items() if key != "identity"},
                )
                or payload.get("native_consumer_source_identity") != source["identity"]
            ):
                raise ValueError("native consumer source differs from the registered worker depot")
            binding = NativeOwnerBinding(
                contract_version="policy-qualified-native-owner-binding-v1",
                instance=owner.anchor["instance"],
                storage_binding_identity=owner.anchor["storage_binding_identity"],
                native_owner_sha256=owner.anchor["native_sha256"],
                worker_manifest_sha256=owner.anchor["worker_manifest_sha256"],
                native_consumer_source_identity=source["identity"],
                execution_authorized=False,
            )
        if operation == "verify-attempt":
            from tradingagents.runtime_protection import validate_conditional_attempt

            fields("attempt", "native_consumer_source_identity")
            attempt = validate_conditional_attempt(payload["attempt"], policy_verifier=verifier)
            envelope = attempt.inputs.acceptance
            extra["attempt"] = attempt.model_dump(mode="json")
        elif operation == "verify-materialization":
            fields("materialization", "native_consumer_source_identity")
            materialization = payload["materialization"]
            expected = seal_conditional_materialization_plan(
                **{
                    key: materialization[key]
                    for key in (
                        "attempt",
                        "runtime_content",
                        "app_content",
                        "source_locations",
                        "deployment_configuration",
                        "environment",
                        "working_directory",
                        "python_version",
                        "credential_bindings",
                    )
                },
                owner_binding=binding,
                policy_verifier=verifier,
            )
            if materialization != expected:
                raise ValueError(
                    "native materialization has an altered field, owner or complete identity"
                )
            from tradingagents.runtime_protection import ConditionalProtectionAttempt

            attempt = ConditionalProtectionAttempt.model_validate_json(
                canonical_bytes(materialization["attempt"])
            )
            envelope = attempt.inputs.acceptance
            extra["materialization_identity"] = materialization["identity"]
        elif operation == "verify-inert-launch":
            fields("prepared", "control", "native_consumer_source_identity")
            prepared = PreparedConditionalProtectionAttempt.model_validate_json(
                canonical_bytes(payload["prepared"])
            )
            control = payload["control"]
            if owner.anchor["operational"] or prepared.owner_binding != binding:
                raise ValueError(
                    "only the separately registered isolated test owner can authorize an inert fixture"
                )
            grant = InertFixtureLaunch.read(
                owner._registration("inert-launch", prepared.attempt.attempt_identity)
            )
            if (
                grant.instance != owner.anchor["instance"]
                or grant.prepared_identity != prepared.identity
                or grant.control_identity != control.get("identity")
                or grant.materialization_identity != prepared.materialization_identity
                or grant.runtime_content_identity != prepared.runtime_manifest_identity
                or grant.app_content_identity != prepared.app_manifest_identity
                or list(grant.child_arguments) != control.get("child_arguments")
                or grant.operation != control.get("operation")
            ):
                raise ValueError(
                    "native launch differs from the exact authenticated inert fixture control"
                )
            for ref in grant.reviewed_fixture_source:
                owner.read_artifact(ref)
            baselines = {entry.relative_path: entry for entry in prepared.baseline}
            relative = (
                Path(grant.child_arguments[2])
                .relative_to(Path(prepared.attempt.paths.payload_root))
                .as_posix()
            )
            if (
                baselines["runtime/venv/Scripts/python.exe"].content_sha256
                != grant.interpreter_sha256
                or baselines[relative].content_sha256 != grant.entrypoint_sha256
            ):
                raise ValueError(
                    "inert interpreter/entrypoint no longer matches its reviewed bytes"
                )
            attempt = prepared.attempt
            envelope = verifier.verify_downstream(attempt.inputs.acceptance)
            extra.update(
                control_identity=grant.control_identity,
                maximum_child_seconds=grant.maximum_child_seconds,
                inert_registration_identity=grant.identity,
            )
        else:
            fields("envelope")
            envelope = verifier.verify_downstream(DownstreamEnvelope.read(payload["envelope"]))
        if native:
            ancestry = NativePayloadProvenance.read(
                owner._registration("native-payload", attempt.inputs.preparation_lineage_identity)
            )
            result = PolicyQualifiedExecutionResult.read(owner.read_artifact(envelope.core.result))
            if (
                ancestry.candidate != envelope.core.candidate
                or ancestry.qualified_source != result.execution.source
                or ancestry.qualified_runtime != result.execution.runtime
                or ancestry.runtime_content.identity != attempt.inputs.runtime_content_identity
                or ancestry.app_content.identity != attempt.inputs.app_content_identity
                or ancestry.runtime_content.ancestry_identity
                != attempt.inputs.runtime_ancestry_identity
                or ancestry.app_content.ancestry_identity != attempt.inputs.app_ancestry_identity
            ):
                raise ValueError(
                    "native runtime/application ancestry is not registered to this exact qualified result"
                )
            owner.read_artifact(ancestry.collection_provenance)
            if operation == "verify-materialization" and (
                materialization["runtime_content"]
                != ancestry.runtime_content.model_dump(mode="json")
                or materialization["app_content"] != ancestry.app_content.model_dump(mode="json")
            ):
                raise ValueError("materialization replaced registered runtime/application bytes")
        # Re-resolve after complete evidence validation. The native caller gets
        # the full caveat plus fresh authority and durable consumption details.
        approval_identity = envelope.core.binding.approval_identity
        resolved = owner.resolve(approval_identity)
        owner.ledger.require_receipt(
            envelope.core.lineage, reference(envelope.receipt, "receipt_identity")
        )
        response = {
            "contract_version": "policy-owner-native-conditional-verification-v1",
            "instance": owner.anchor["instance"],
            "storage_binding_identity": owner.anchor["storage_binding_identity"],
            "envelope": envelope.model_dump(mode="json"),
            "status": "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY",
            "authority_state": resolved.state,
            "authority_state_reference": resolved.state_reference,
            "checked_at_utc": resolved.now_utc,
            "expires_utc": envelope.core.expires_utc,
            "execution_expires_utc": resolved.execution_expires_utc,
            "current_candidate": resolved.current_candidate.model_dump(mode="json"),
            "current_production_inventory_identity": resolved.current_production_inventory_identity,
            "ledger_identity": resolved.ledger_id,
            "ledger_path": str(resolved.ledger_path),
            "terminal_consumption": owner.ledger.require_terminal(
                envelope.core.lineage, envelope.core.result
            ),
            "receipt_identity": envelope.receipt.receipt_identity,
            "execution_authorized": False,
            "ticket07_or_live_authorized": False,
        }
        return {**response, **extra}
    if operation == "verify-result":
        fields("result_base64")
        result, core, resolved = verifier.verify_result(
            base64.b64decode(payload["result_base64"], validate=True)
        )
        return {
            "result": result.model_dump(mode="json"),
            "core": core.model_dump(mode="json"),
            "authority": encode_resolution(resolved),
        }
    if operation == "eligibility":
        fields("policy", "approval")
        return {
            "authority": encode_resolution(
                verifier.eligibility(
                    AcceptancePolicy.read(payload["policy"]),
                    OperatorApproval.read(payload["approval"]),
                )
            )
        }
    if operation == "claim":
        fields("policy", "approval", "allocation", "new_start_bridge")
        claim = verifier.claim(**payload)
        owner.put(claim.to_bytes(), claim.lineage_identity)
        return {"claim": claim.model_dump(mode="json"), "execution_authorized": False}
    if operation == "begin-stage":
        fields("policy", "approval", "claim", "stage", "new_start_bridge")
        return {"event_identity": verifier.begin_stage(**payload), "execution_authorized": False}
    if operation == "complete-stage":
        fields("policy", "approval", "claim", "stage", "evidence")
        payload["evidence"] = BoundEvidence.read(payload["evidence"])
        return {"event_identity": verifier.complete_stage(**payload), "execution_authorized": False}
    if operation == "finalize":
        fields("policy", "approval", "claim", "execution")
        result = verifier.finalize(**payload)
        owner.put(result.to_bytes(), result.result_identity)
        return {"result": result.model_dump(mode="json"), "execution_authorized": False}
    if operation == "issue-receipt":
        fields("result_base64")
        receipt = verifier.issue_receipt(base64.b64decode(payload["result_base64"], validate=True))
        owner.put(receipt.to_bytes(), receipt.receipt_identity)
        return {"receipt": receipt.model_dump(mode="json"), "execution_authorized": False}
    # No ledger mutation primitive is exposed outside the producer verifier.
    raise ValueError("unknown owner operation; no qualification process or live endpoint exists")


def main() -> int:
    try:
        _root, request = bootstrap()
        result = dispatch(request)
        from tradingagents.policy_qualified_contract import canonical_bytes

        sys.stdout.buffer.write(canonical_bytes(result))
        return 0
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
