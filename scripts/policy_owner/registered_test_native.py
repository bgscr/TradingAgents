"""Real producer/materialization and inert consumer calls for a TEST result."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from scripts.policy_owner.registered_test_flow import write_new
from tradingagents.policy_qualified_contract import canonical_bytes, content_identity
from tradingagents.policy_qualified_native import (
    ConditionalNativeCompletion,
    CoverageNativeCompletion,
    InertFixtureLaunch,
    NativeOwnerBinding,
    NativePayloadProvenance,
    build_conditional_inert_control,
    parse_prepared_conditional,
    seal_conditional_materialization_plan,
)
from tradingagents.runtime_protection import (
    ContentManifest,
    bind_production_preparation,
    evidence_identity,
    make_content_manifest,
    seal_child_environment,
)
from tradingagents.runtime_protection_handoff import (
    ConditionalHandoffContract,
    CoverageHandoffContract,
    build_conditional_guard_control,
)


def collect_payload(*, owner_root, manifest, fixture_root, child_source):
    """Read exact deployed inputs; generated TEST configuration stays separate."""
    fixture_root = Path(fixture_root)
    fixture_root.mkdir()
    interpreter_entry = next(
        e for e in manifest["files"] if e["relative_path"] == "python/python.exe"
    )
    if sha256(Path(sys.executable).read_bytes()).hexdigest() != interpreter_entry["sha256"]:
        raise ValueError("client interpreter version cannot describe a different deployed runtime")
    versioned_dll = next(
        e for e in manifest["files"] if e["relative_path"] == "python/python313.dll"
    )
    if (
        sha256((Path(sys.base_prefix) / "python313.dll").read_bytes()).hexdigest()
        != versioned_dll["sha256"]
    ):
        raise ValueError("client Python DLL differs from the deployed native payload version")
    generated_config = fixture_root / "SYNTHETIC-default_config.py"
    write_new(
        generated_config,
        b'''"""SYNTHETIC TEST configuration bridge; no normal app startup."""
import json
from pathlib import Path
DEFAULT_CONFIG = json.loads((Path(__file__).resolve().parents[2] / "configuration.json").read_bytes())
''',
    )
    locations, rows, native_images = {}, {"runtime": [], "app": []}, {"runtime": [], "app": []}

    def add(role, relative, path, expected=None):
        raw = path.read_bytes()
        row = {
            "relative_path": relative,
            "byte_length": len(raw),
            "sha256": sha256(raw).hexdigest(),
        }
        if expected is not None and (
            row["byte_length"] != expected["byte_length"] or row["sha256"] != expected["sha256"]
        ):
            raise ValueError("deployed native payload input changed: " + str(path))
        key = role + "/" + relative
        if key in locations:
            raise ValueError("duplicate native payload member")
        locations[key] = str(path)
        rows[role].append(row)
        if raw[:2] == b"MZ":
            native_images[role].append(relative)

    for entry in manifest["files"]:
        relative = entry["relative_path"]
        path = owner_root / relative
        if relative.startswith("python/"):
            add("runtime", "base/" + relative.removeprefix("python/"), path, entry)
        elif relative.startswith("dependencies/"):
            add(
                "runtime",
                "venv/Lib/site-packages/" + relative.removeprefix("dependencies/"),
                path,
                entry,
            )
        elif (
            relative.startswith(("source/tradingagents/", "source/cli/"))
            and relative != "source/tradingagents/default_config.py"
        ):
            add("app", relative.removeprefix("source/"), path, entry)
    launcher = "python/Lib/venv/scripts/nt/venvlauncher.exe"
    launcher_entry = next(e for e in manifest["files"] if e["relative_path"] == launcher)
    add("runtime", "venv/Scripts/python.exe", owner_root / launcher, launcher_entry)
    add("app", "tradingagents/default_config.py", generated_config)
    add("app", "SYNTHETIC-native-child.py", child_source)
    version = ".".join(map(str, sys.version_info[:3]))
    for role in ("runtime", "app"):
        # Detect malformed members before any human decision or consumed claim.
        make_content_manifest(
            role=role,
            source_commit="a" * 40,
            source_tree="b" * 40,
            ancestry_identity="synthetic-unbound-payload:v1:sha256:" + "0" * 64,
            files=rows[role],
            native_images=native_images[role],
            python_version=version if role == "runtime" else None,
        )
    return {
        "rows": rows,
        "native_images": native_images,
        "locations": locations,
        "python_version": version,
    }


def exercise_native(
    *,
    case,
    result,
    envelope,
    blueprint,
    payload,
    namespace,
    owner_binding,
    native_call,
    artifact_root,
):
    """No fabricated preparation identity or replacement native host is used."""
    from tradingagents.qualified_routing_composition import (
        ConditionalProductionQualifiedRoutingSourceBinding,
        CoverageProductionQualifiedRoutingSourceBinding,
        seal_production_qualified_routing_preparation,
    )

    artifact_root = Path(artifact_root)
    artifact_root.mkdir()
    verifier = case.verifier
    binding = NativeOwnerBinding.read(owner_binding)
    from tradingagents.policy_coverage_models import CoverageEnvelope

    coverage = isinstance(envelope, CoverageEnvelope)
    source_model = (
        CoverageProductionQualifiedRoutingSourceBinding
        if coverage
        else ConditionalProductionQualifiedRoutingSourceBinding
    )
    source = source_model(
        contract_version="production-policy-qualified-routing-source-binding-v2"
        if coverage
        else "production-policy-qualified-routing-source-binding-v1",
        commit_id=case.candidate.commit,
        tree_hash=case.candidate.tree,
        production_inventory_identity="production-python-modules:v1:sha256:" + "d" * 64,
        qualified_source_identity=result.result_identity,
        acceptance=envelope,
    )
    preparation = seal_production_qualified_routing_preparation(
        blueprint=blueprint,
        qualified_source=source,
        current_source=source,
        policy_verifier=verifier,
    )
    write_new(
        artifact_root / "TEST-production-preparation.json",
        canonical_bytes(preparation.model_dump(mode="json")),
    )
    manifests = {}
    for role, ancestor in (("runtime", result.execution.runtime), ("app", result.execution.source)):
        manifests[role] = make_content_manifest(
            role=role,
            source_commit=case.candidate.commit,
            source_tree=case.candidate.tree,
            ancestry_identity=ancestor.identity,
            files=payload["rows"][role],
            native_images=payload["native_images"][role],
            python_version=payload["python_version"] if role == "runtime" else None,
        )
    lineage_identity = content_identity(
        "synthetic-native-preparation-lineage:v1",
        {
            "label": "SYNTHETIC NATIVE FIXTURE ONLY; NOT TICKET-07 PREPARATION",
            "result": result.result_identity,
            "preparation": preparation.preparation_identity,
            "namespace": namespace["identity"],
        },
    )
    collection_body = {
        "label": "ACTUAL DEPLOYED BYTES USED AS TEST PAYLOAD; SYNTHETIC QUALIFICATION ANCESTRY ONLY",
        "registered_owner": binding.model_dump(mode="json"),
        "runtime": manifests["runtime"],
        "app": manifests["app"],
        "source_locations": payload["locations"],
        "execution_authorized": False,
    }
    if coverage:
        # Retain the complete labelled collection and its inner canonical ID,
        # but authenticate large collection bytes through the existing opaque
        # artifact descriptor rather than putting the inventory in the journal.
        collection_raw = (
            canonical_bytes(
                {
                    **collection_body,
                    "identity": content_identity(
                        "synthetic-native-payload-collection:v1", collection_body
                    ),
                }
            )
            + b"\n"
        )
        collection = case.owner.store_raw(
            collection_raw, "policy-evidence-bytes:v1:sha256:" + sha256(collection_raw).hexdigest()
        )
    else:
        collection = case.owner.save("synthetic-native-payload-collection:v1", collection_body)
    provenance = NativePayloadProvenance.seal(
        contract_version="policy-qualified-native-payload-provenance-v1",
        candidate=case.candidate,
        preparation_lineage_identity=lineage_identity,
        qualified_source=result.execution.source,
        qualified_runtime=result.execution.runtime,
        runtime_content=ContentManifest.model_validate_json(canonical_bytes(manifests["runtime"])),
        app_content=ContentManifest.model_validate_json(canonical_bytes(manifests["app"])),
        collection_provenance=collection,
        execution_authorized=False,
    )
    child_path = Path(payload["locations"]["app/SYNTHETIC-native-child.py"])
    child_ref = case.owner.store_raw(
        child_path.read_bytes(),
        content_identity(
            "synthetic-native-fixture-source:v1",
            {"sha256": sha256(child_path.read_bytes()).hexdigest()},
        ),
    )
    case.flush(
        (case.owner.entry("native-payload", lineage_identity, provenance.model_dump(mode="json")),)
    )
    environment = {
        "SystemRoot": r"C:\Windows",
        "SystemDrive": "C:",
        "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
        "PYTHON_DOTENV_DISABLED": "1",
        "RUN_LIVE_CUSTOM_TESTS": "0",
        "RUN_LIVE_DEEPSEEK_TESTS": "0",
        "RUN_LIVE_ENVIRONMENT": "0",
        "RUN_LIVE_PROVIDER_TESTS": "0",
        "RUN_LIVE_TUSHARE_TESTS": "0",
        "PATH": r"{private_runtime_root}\base;{private_runtime_root}\venv\Scripts;C:\Windows\System32",
    }
    config = {
        "project_dir": str(artifact_root / "SYNTHETIC-unmapped-project"),
        "crypto_identity_registry_path": str(artifact_root / "SYNTHETIC-unopened-registry.json"),
        "crypto_identity_registry_checksum_path": str(
            artifact_root / "SYNTHETIC-unopened-registry.sha256"
        ),
        "output_language": "English",
        "mainland_capability_routing_mode": "qualified_v1_shadow",
        "cninfo_operator_minimum_interval_seconds": 1,
    }
    outputs = []
    for ordinal, operation in enumerate(("inert", "guard_only"), start=1):
        attempt = bind_production_preparation(
            preparation,
            policy_verifier=verifier,
            namespace_root=namespace["namespace_root"],
            namespace_identity=namespace["identity"],
            preparation_lineage_identity=lineage_identity,
            completed_session="2001-01-01",
            prospective_thread="f" * 16,
            prospective_run="run:" + "e" * 64,
            runtime_content_identity=manifests["runtime"]["identity"],
            app_content_identity=manifests["app"]["identity"],
            runtime_ancestry_identity=result.execution.runtime.identity,
            app_ancestry_identity=result.execution.source.identity,
            deployment_configuration_identity=evidence_identity(
                "runtime-protection-configuration:v1", config
            ),
            environment_identity=seal_child_environment(
                environment, working_directory=str(artifact_root), credential_bindings=[]
            )["identity"],
            protection_source_identity=namespace["protection_source_identity"],
            attempt_ordinal=ordinal,
        )
        verified = native_call(
            "verify", attempt.model_dump(mode="json"), label=operation + "-verify"
        )
        if verified["attempt"] != attempt.model_dump(mode="json") or verified[
            "owner_binding"
        ] != binding.model_dump(mode="json"):
            raise ValueError("actual native verifier lost the complete attempt/owner binding")
        plan = seal_conditional_materialization_plan(
            attempt=attempt,
            owner_binding=binding,
            policy_verifier=verifier,
            runtime_content=manifests["runtime"],
            app_content=manifests["app"],
            source_locations=payload["locations"],
            deployment_configuration=config,
            environment=environment,
            working_directory=str(artifact_root),
            python_version=payload["python_version"],
            credential_bindings=[],
        )
        prepared = parse_prepared_conditional(
            native_call("prepare", plan, label=operation + "-prepare")
        )
        arguments = [
            "-I",
            "-B",
            str(Path(attempt.paths.private_app_root) / "SYNTHETIC-native-child.py"),
            operation,
            case.instance,
        ]
        if operation == "inert":
            control = build_conditional_inert_control(
                prepared, child_arguments=arguments, policy_verifier=verifier
            )
        else:
            cninfo = blueprint.factory_contract.cninfo_binding
            gate_model = CoverageHandoffContract if coverage else ConditionalHandoffContract
            gate = gate_model(
                contract_version="policy-qualified-runtime-protection-handoff-v3"
                if coverage
                else "policy-qualified-runtime-protection-handoff-v2",
                preparation_lineage_identity=lineage_identity,
                plan_version="1.5",
                plan_signature=preparation.plan_signature,
                cninfo_profile_identity=cninfo.profile.profile_identity,
                cninfo_source_policy_identity=cninfo.profile.source_policy_identity,
                cninfo_operator_minimum_interval_seconds=1,
                checkpoint_identity=content_identity(
                    "synthetic-unopened-checkpoint:v1", {"label": "NONEXISTENT TEST CHECKPOINT"}
                ),
                history_identity=content_identity(
                    "synthetic-unopened-history:v1", {"label": "NONEXISTENT TEST HISTORY"}
                ),
                calendar_revision_id="SYNTHETIC-no-calendar-read",
                valid_before_exclusive=(
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
                symbol="DISPOSABLE_FIXTURE",
                analysis_session="2001-01-01",
                selected_analysts=("fundamentals",),
                output_language="English",
                legacy_authoritative=True,
                qualified_shadow_authoritative=False,
                execution_authorized=False,
                acceptance=envelope,
                owner_binding=binding,
            )
            control = build_conditional_guard_control(
                prepared.model_dump(mode="json"),
                guard_arguments=arguments,
                handoff=gate.model_dump(mode="json"),
                policy_verifier=verifier,
            )
        interpreter = next(
            r for r in payload["rows"]["runtime"] if r["relative_path"] == "venv/Scripts/python.exe"
        )
        grant = InertFixtureLaunch.seal(
            contract_version="policy-owner-inert-fixture-launch-v1",
            instance=case.instance,
            attempt_identity=attempt.attempt_identity,
            prepared_identity=prepared.identity,
            control_identity=control["identity"],
            materialization_identity=prepared.materialization_identity,
            runtime_content_identity=prepared.runtime_manifest_identity,
            app_content_identity=prepared.app_manifest_identity,
            interpreter_sha256=interpreter["sha256"],
            entrypoint_sha256=child_ref.pin.sha256,
            child_arguments=tuple(arguments),
            operation=operation,
            maximum_child_seconds=120,
            reviewed_fixture_source=(child_ref, collection),
            label="ISOLATED_NATIVE_INERT_FIXTURE_ONLY",
            ticket07_or_live_authorized=False,
            qualification_execution_authorized=False,
        )
        case.flush(
            (
                case.owner.entry(
                    "inert-launch", attempt.attempt_identity, grant.model_dump(mode="json")
                ),
            )
        )
        completion_model = CoverageNativeCompletion if coverage else ConditionalNativeCompletion
        completion = completion_model.read(
            native_call(
                "run-inert",
                prepared.model_dump(mode="json"),
                control=control,
                label=operation + "-run",
            )
        )
        if (
            completion.state != "COMPLETED_RESTORED"
            or completion.child_launches != 1
            or completion.failure_count != 0
            or completion.acceptance != envelope
            or completion.owner_binding != binding
        ):
            raise ValueError(
                "native fixture did not restore its exact baseline with the complete conditional binding"
            )
        outputs.append({"operation": operation, "completion": completion.model_dump(mode="json")})
    return {
        "label": "REGISTERED TEST RESULT AND ACTUAL INERT NATIVE BOUNDARY ONLY",
        "results": outputs,
        "synthetic_stage_data": True,
        "operational": False,
        "execution_authorized": False,
    }
