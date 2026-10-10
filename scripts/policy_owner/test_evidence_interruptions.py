"""Actual owned-process interruption/tampering/timeout tests on disposable stores."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "dependencies", "native-fixture"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--child", choices=["during", "after", "timeout"])
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(source), str(args.dependencies)]
    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import CHUNK_BYTES, ArtifactDescriptor
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, canonical_bytes

    output = args.output.absolute()
    if output.is_relative_to(source) or output.is_relative_to(Path(os.environ["PROGRAMDATA"])):
        raise ValueError("disposable external fixture only")
    if args.child == "timeout":
        bound_evidence_process(seconds=1, memory_bytes=64 * 1024**2)
        print("OWNED_TIMEOUT_PROBE_STARTED", flush=True)
        time.sleep(10)
        raise AssertionError("finite job timer did not stop this child")
    bound_evidence_process(seconds=180)
    instance = "test-20300101-903"

    def request(root, operation, payload, *, reject=None):
        result = subprocess.run(
            [str(args.native_fixture), str(root), instance],
            input=canonical_bytes({"operation": operation, "payload": payload}),
            capture_output=True,
            timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if reject:
            assert result.returncode != 0 and reject in result.stderr.decode("utf-8", "replace"), (
                result.stderr
            )
            return {"exit": result.returncode, "error": result.stderr.decode("utf-8", "replace")}
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    def call(root):
        return lambda operation, payload: request(root, operation, payload)

    def store(root):
        (root / "state/temp").mkdir(parents=True)
        (root / "artifacts").mkdir()
        (root / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
            b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
        )

    if args.child:
        descriptor = ArtifactDescriptor.read((output / "descriptor.json").read_bytes())

        def progress(value):
            with (output / "events.jsonl").open("ab") as stream:
                stream.write(canonical_bytes(value) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            if args.child == "during" and value["offset"] > 0:
                os._exit(91)  # actual abrupt owned-process loss after a real native write

        result = stage_artifact(
            call(output / "STORE"), output / "payload.bin", descriptor, progress=progress
        )
        (output / "published.json").write_bytes(canonical_bytes(result))
        os._exit(92)  # actual loss after byte publication, before ANY authentication
    if output.exists():
        raise ValueError("fresh fixture required")
    output.mkdir()
    cases = []
    for mode in ("during", "after", "tamper", "bounds"):
        root = output / mode
        root.mkdir()
        store(root / "STORE")
        raw = os.urandom(2 * CHUNK_BYTES + 37)
        path = root / "payload.bin"
        path.write_bytes(raw)
        pin = BytePin.of(raw)
        now = datetime.now(timezone.utc)
        descriptor = ArtifactDescriptor.seal(
            contract_version="policy-owner-artifact-descriptor-v2",
            operation="REGISTER_EVIDENCE",
            instance=instance,
            role_identity="policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
            approval_identity="historical-state-operator-approval:v1:sha256:" + "2" * 64,
            candidate={"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
            lineage_plan_identity="historical-state-lineage-plan:v1:sha256:" + "3" * 64,
            acquisition_identity="SYNTHETIC-" + mode,
            stage="ISO02",
            phase="evidence",
            artifact_schema="policy-evidence-bytes:v1",
            artifact_identity_field="byte_digest",
            artifact={"identity": "policy-evidence-bytes:v1:sha256:" + pin.sha256, "pin": pin},
            issued_utc=now.isoformat(),
            expires_utc=(now + timedelta(hours=1)).isoformat(),
        )
        (root / "descriptor.json").write_bytes(descriptor.to_bytes())
        if mode in {"during", "after"}:
            child = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-S",
                    str(Path(__file__)),
                    "--output",
                    str(root),
                    "--dependencies",
                    str(args.dependencies),
                    "--native-fixture",
                    str(args.native_fixture),
                    "--child",
                    mode,
                ],
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (root / "child.stdout").write_bytes(child.stdout)
            (root / "child.stderr").write_bytes(child.stderr)
            assert child.returncode == (91 if mode == "during" else 92), child.stderr
            events = [
                json.loads(line) for line in (root / "events.jsonl").read_bytes().splitlines()
            ]
            transfer = events[0]["transfer_id"]
            base = root / "STORE/state/temp" / ("evidence-import-" + transfer)
            if mode == "during":
                assert (
                    Path(str(base) + ".part").stat().st_size == CHUNK_BYTES
                    and not Path(str(base) + ".complete").exists()
                )
                result = request(
                    root / "STORE",
                    "evidence-verify-complete-v2",
                    {"transfer_id": transfer, "descriptor": descriptor.model_dump(mode="json")},
                    reject="Cannot open",
                )
            else:
                result = request(
                    root / "STORE",
                    "evidence-verify-complete-v2",
                    {"transfer_id": transfer, "descriptor": descriptor.model_dump(mode="json")},
                )
                assert result["authenticated"] is False
            cases.append(
                {
                    "case": "actual-process-loss-" + mode,
                    "exit": child.returncode,
                    "verification": result,
                    "accepted_authentication": False,
                }
            )
        elif mode == "tamper":
            changed = False

            def tamper(value, path=path):
                nonlocal changed
                if value["offset"] == CHUNK_BYTES:
                    with path.open("r+b") as stream:
                        stream.seek(CHUNK_BYTES)
                        stream.write(b"TAMPERED")
                    changed = True

            try:
                stage_artifact(call(root / "STORE"), path, descriptor, progress=tamper)
            except ValueError as error:
                assert changed and "changed" in str(error)
                cases.append(
                    {"case": "changed-source-during-real-native-import", "rejection": str(error)}
                )
            else:
                raise AssertionError("changed source admitted")
            assert not list((root / "STORE/state/temp").glob("*.complete"))
        else:
            transfer = request(
                root / "STORE",
                "evidence-import-begin-v2",
                {"descriptor": descriptor.model_dump(mode="json")},
            )["transfer_id"]
            result = request(
                root / "STORE",
                "evidence-import-chunk-v2",
                {
                    "transfer_id": transfer,
                    "offset": 0,
                    "bytes_base64": base64.b64encode(raw[: CHUNK_BYTES + 1]).decode("ascii"),
                },
                reject="Chunk length",
            )
            cases.append({"case": "one-byte-over-chunk-bound", "rejection": result})
            unavailable = output / "MISSING-STORE-COMPONENTS"
            unavailable.mkdir()
            (unavailable / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
                b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
            )
            result = request(
                unavailable,
                "evidence-import-begin-v2",
                {"descriptor": descriptor.model_dump(mode="json")},
                reject="Cannot open",
            )
            cases.append({"case": "unavailable-store", "rejection": result})
            # An export alias must reject before any native read, not write through it.
            target = root / "ALIAS-TARGET"
            target.mkdir()
            alias = root / "junction"
            linked = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                capture_output=True,
                timeout=10,
            )
            assert linked.returncode == 0, linked.stderr
            calls = []
            try:
                export_artifact(
                    lambda *args, calls=calls: calls.append(args),
                    descriptor.artifact,
                    alias / "must-not-write",
                )
            except ValueError as error:
                assert "reparse" in str(error) and calls == []
            else:
                raise AssertionError("export followed an aliased output")
            assert list(target.iterdir()) == []
            cases.append(
                {"case": "export-junction-before-request", "no_requests": True, "no_files": True}
            )
    started = time.monotonic()
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(Path(__file__)),
            "--output",
            str(output),
            "--dependencies",
            str(args.dependencies),
            "--native-fixture",
            str(args.native_fixture),
            "--child",
            "timeout",
        ],
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    elapsed = time.monotonic() - started
    assert child.returncode == 124 and elapsed < 10, (child.returncode, child.stderr)
    cases.append(
        {
            "case": "actual-owned-job-timeout",
            "exit": child.returncode,
            "elapsed_seconds": elapsed,
            "stdout": child.stdout.decode(),
        }
    )
    report = {
        "status": "PASS_ACTUAL_INTERRUPTION_TAMPER_TIMEOUT_FIXTURES",
        "cases": cases,
        "human_authentication": False,
        "real_authority": False,
        "execution_authorized": False,
    }
    (output / "result.json").write_bytes(canonical_bytes(report))
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
