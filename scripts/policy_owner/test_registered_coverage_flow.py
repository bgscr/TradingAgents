"""Real composition/native transfer with INVALID fixture authentication only.

Never usable against a registered store; no Windows Hello or native child launch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--dependencies", required=True, type=Path)
    p.add_argument("--native-fixture", required=True, type=Path)
    p.add_argument("--native-depot", type=Path)
    args = p.parse_args()
    source = Path(__file__).resolve().parents[2]
    out = args.output.absolute()
    if (
        out.exists()
        or out.is_relative_to(source)
        or out.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external disposable fixture root required")
    if args.native_fixture.name != "EvidenceTransferContracts.exe":
        raise ValueError("explicit isolated native fixture required")
    out.mkdir()
    sys.path[:0] = [str(source), str(args.dependencies)]
    proof_names = [
        *[p.relative_to(source).as_posix() for p in source.glob("tradingagents/policy_*.py")],
        "tradingagents/windows_policy_owner.py",
        "tradingagents/runtime_protection.py",
        "tradingagents/runtime_protection_handoff.py",
        "tradingagents/qualified_routing_composition.py",
        *[
            "scripts/policy_owner/" + name
            for name in (
                "owner_worker.py",
                "registered_test_flow.py",
                "registered_test_fixtures.py",
                "registered_coverage_test.py",
                "coverage_test_vectors.py",
                "predeployment_fixtures.py",
                "prepare_test_owner_inputs.py",
                "prepare_policy_owner_setup.py",
                "test_registered_coverage_flow.py",
                "registered_test_native.py",
                "registered_test_client.py",
            )
        ],
        "tests/fixtures/policy_owner/local_owner_fixture.py",
    ]

    def source_pins():
        return {
            name: {
                "byte_length": (source / name).stat().st_size,
                "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest(),
            }
            for name in sorted(set(proof_names))
        }

    initial_pins = source_pins()
    (out / "SOURCE-PINS-START.json").write_text(
        json.dumps(initial_pins, indent=2), encoding="utf-8"
    )
    from scripts.policy_owner.owner_worker import dispatch
    from scripts.policy_owner.prepare_test_owner_inputs import prepare
    from scripts.policy_owner.registered_test_flow import RegisteredTestCase
    from tests.fixtures.policy_owner.local_owner_fixture import SyntheticNativeBoundary
    from tradingagents import windows_policy_owner
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import canonical_bytes

    bound_evidence_process(seconds=900)
    report = {
        "classification": "ISOLATED_INVALID_NATIVE_AUTHORITY_FIXTURE_ONLY",
        "authentication_is_genuine": False,
        "operational_owner_contacted": False,
        "native_children_started": 0,
        "qualification": False,
        "execution_authorized": False,
        "documents": [],
        "calls": [],
    }

    def save():
        (out / "RESULT.json").write_bytes(canonical_bytes(report) + b"\n")

    def audit(event, values):
        if event in {
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "os.system",
            "os.startfile",
            "os.startfile/2",
        }:
            raise RuntimeError("fixture forbids external activity")
        if event == "subprocess.Popen" and (
            values[0] is None or Path(values[0]).resolve() != args.native_fixture.resolve()
        ):
            raise RuntimeError("fixture permits only the exact unregistered evidence fixture child")
        if (
            event == "open"
            and isinstance(values[0], (str, bytes))
            and values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        ) and not Path(os.fsdecode(values[0])).absolute().is_relative_to(out):
            raise RuntimeError("fixture write escaped owned output")

    sys.addaudithook(audit)
    prepare(
        SimpleNamespace(
            output=out / "inputs",
            instance="test-20300101-001",
            operator_sid="S-1-5-21-101-102-103-1001",
            account_name="SYNTHETIC\\INVALID",
            coverage_v2=True,
        )
    )
    template = json.loads((out / "inputs/TEST-after.json").read_bytes())["roots"][0]
    boundary = SyntheticNativeBoundary(out / "boundary", root_template=template)
    boundary.owner.now_utc = datetime.now(timezone.utc).isoformat()
    windows_policy_owner.LocalWindowsPolicyOwner = lambda *unused: boundary.owner

    class IsolatedRegisteredAdapter(windows_policy_owner.RegisteredWindowsPolicyVerifier):
        def __init__(self):
            self.instance = boundary.owner.anchor["instance"]

        def call(self, operation, payload):
            start = time.monotonic()
            boundary.owner.now_utc = datetime.now(timezone.utc).isoformat()
            if operation.startswith("evidence-") or operation == "fixture-transferred-document":
                response = subprocess.run(
                    [str(args.native_fixture), str(boundary.owner.root), self.instance],
                    executable=str(args.native_fixture),
                    input=canonical_bytes({"operation": operation, "payload": payload}),
                    capture_output=True,
                    timeout=35,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if response.returncode:
                    raise ValueError(response.stderr.decode("utf-8", "replace"))
                value = json.loads(response.stdout)
            else:
                value = dispatch(
                    {
                        "anchor": boundary.owner.anchor,
                        "authenticated_records": boundary.owner.records,
                        "now_utc": boundary.owner.now_utc,
                        "operation": operation,
                        "payload": payload,
                    }
                )
            report["calls"].append({"operation": operation, "seconds": time.monotonic() - start})
            save()
            return value

    def authenticate(operation, document):
        purpose = {"decide": "EXACT_POLICY_DECISION", "register-evidence": "REGISTER_EVIDENCE"}[
            operation
        ]
        document_number = len(report["documents"]) + 1
        (out / f"document-{document_number:03}.json").write_bytes(canonical_bytes(document))
        if purpose == "REGISTER_EVIDENCE":
            native = verifier.call("fixture-transferred-document", {"document": document})
            (out / f"native-document-{document_number:03}.json").write_bytes(
                canonical_bytes(native)
            )
        boundary.owner.now_utc = datetime.now(timezone.utc).isoformat()
        boundary.owner.validate_authentication_document(purpose, document)
        value = boundary.apply(purpose, document)
        journal = canonical_bytes(boundary.owner.records)
        (out / "INVALID-AUTHENTICATION-FIXTURE-JOURNAL.json").write_bytes(journal)
        report["invalid_fixture_journal_bytes"] = len(journal)
        report["maximum_genuine_wrapper_projection_bytes"] = (
            len(journal) + len(boundary.owner.records) * 262144
        )
        if report["maximum_genuine_wrapper_projection_bytes"] > 33554432:
            raise ValueError("complete fixture metadata route exceeds journal capacity")
        report["documents"].append(
            {
                "purpose": purpose,
                "bytes": len(canonical_bytes(document)),
                "genuine_authentication": False,
                "fixture_record": boundary.owner.records[-1]["identity"],
            }
        )
        save()
        return value

    root = out / "GENERATED-COVERAGE-TEST"
    root.mkdir()
    verifier = IsolatedRegisteredAdapter()
    case = RegisteredTestCase(
        root=root,
        instance=boundary.owner.anchor["instance"],
        role=boundary.role,
        storage_identity=boundary.owner.anchor["storage_binding_identity"],
        registry=boundary.owner.registry.value,
        authenticate=authenticate,
        verifier=verifier,
        artifact_reader=boundary.owner.read_artifact,
        coverage_v2=True,
    )
    proposal = case.proposal()
    (out / "proposed-test-document.json").write_bytes(proposal.to_bytes())
    case.start(proposal)
    result, envelope = case.finish()
    if args.native_depot:
        depot = args.native_depot.resolve(strict=True)
        if depot.is_relative_to(Path(os.environ["PROGRAMDATA"])):
            raise ValueError("native input rehearsal must use an uninstalled staged depot")
        from scripts.policy_owner.registered_test_client import build_test_blueprint
        from scripts.policy_owner.registered_test_native import collect_payload, exercise_native
        from tradingagents.policy_qualified_contract import content_identity
        from tradingagents.policy_qualified_native import NativeOwnerBinding
        from tradingagents.runtime_protection import validate_conditional_attempt

        registry = out / "SYNTHETIC-instrument-registry.json"
        registry.write_bytes(
            canonical_bytes(
                {
                    "schema_version": "1.0",
                    "registry_id": "synthetic-inert-client-registry-v1",
                    "rows": [
                        {
                            "canonical_symbol": "601328.SS",
                            "aliases": [],
                            "venue": "XSHG",
                            "instrument_kind": "equity",
                            "currency": "CNY",
                            "display_name": "SYNTHETIC offline configuration fixture only",
                            "provenance": {
                                "provider": "SYNTHETIC offline fixture",
                                "source_ref": "fixture:synthetic-instrument-no-acquisition",
                                "retrieved_at": "2026-09-01T00:00:00Z",
                            },
                        }
                    ],
                }
            )
        )
        os.environ["TUSHARE_TOKEN"] = "test-token"
        blueprint, _ = build_test_blueprint(out / "SYNTHETIC-client", registry)
        manifest = json.loads((depot / "worker-manifest.json").read_bytes())
        payload = collect_payload(
            owner_root=depot,
            manifest=manifest,
            fixture_root=out / "native-inputs",
            child_source=depot / "source/scripts/policy_owner/registered_test_child.py",
        )
        native_source = json.loads((depot / "native-consumer-source.json").read_bytes())
        binding = NativeOwnerBinding(
            contract_version="policy-qualified-native-owner-binding-v1",
            instance=case.instance,
            storage_binding_identity=case.storage_identity,
            native_owner_sha256=hashlib.sha256(
                (depot / "bin/PolicyOwner.exe").read_bytes()
            ).hexdigest(),
            worker_manifest_sha256=hashlib.sha256(
                (depot / "worker-manifest.json").read_bytes()
            ).hexdigest(),
            native_consumer_source_identity=native_source["identity"],
            execution_authorized=False,
        )
        namespace = {
            "identity": content_identity(
                "runtime-protection-namespace:v1", {"SYNTHETIC": "NOT_INITIALIZED"}
            ),
            "namespace_root": str(out / "SYNTHETIC-UNINITIALIZED-NAMESPACE"),
            "protection_source_identity": "runtime-protection-source:v1:sha256:"
            + hashlib.sha256(
                (source / "scripts/runtime_protection/RuntimeProtectionHost.cs").read_bytes()
            ).hexdigest(),
        }

        class NativeHumanGate(Exception):
            pass

        def pending_native(operation, document, **unused):
            if operation != "verify":
                raise ValueError("unexpected native operation before human gate")
            checked = validate_conditional_attempt(document, policy_verifier=case.verifier)
            (out / "NATIVE-PENDING-ATTEMPT.json").write_bytes(
                canonical_bytes(checked.model_dump(mode="json"))
            )
            raise NativeHumanGate(
                "namespace/registered native verification and children remain human-only"
            )

        try:
            exercise_native(
                case=case,
                result=result,
                envelope=envelope,
                blueprint=blueprint,
                payload=payload,
                namespace=namespace,
                owner_binding=binding,
                native_call=pending_native,
                artifact_root=out / "native-artifacts",
            )
        except NativeHumanGate:
            report["native_input_producer_and_intake_passed"] = True
            report["native_gate"] = "BEFORE_REGISTERED_NATIVE_VERIFY_NO_NAMESPACE_CREATED"
        else:
            raise ValueError("native human gate was unexpectedly bypassed")
        report["native_payload_members"] = sum(len(rows) for rows in payload["rows"].values())
    report.update(
        status="FIXTURE_FEASIBILITY_PASS",
        gap_markers=len(case.coverage_scope.gaps),
        reparse_records=len(case.coverage_scope.links),
        result_identity=result.result_identity,
        historical_coverage=envelope.core.history.coverage.historical_coverage,
        document_count=len(report["documents"]),
        native_transfer_calls=sum(r["operation"].startswith("evidence-") for r in report["calls"]),
    )
    if source_pins() != initial_pins:
        raise ValueError("registered composition source changed during proof")
    report["source_pins"] = initial_pins
    report["native_fixture_pin"] = {
        "path": str(args.native_fixture),
        "byte_length": args.native_fixture.stat().st_size,
        "sha256": hashlib.sha256(args.native_fixture.read_bytes()).hexdigest(),
    }
    save()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
