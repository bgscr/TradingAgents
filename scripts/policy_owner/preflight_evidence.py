"""Non-consuming fixed preflight, optionally reading native journal capacity only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depot", type=Path, required=True)
    parser.add_argument("--worker-manifest", type=Path, required=True)
    parser.add_argument("--worker-manifest-sha256", required=True)
    parser.add_argument("--native-owner", type=Path, required=True)
    parser.add_argument("--native-owner-sha256", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--setup-proposal", type=Path, required=True)
    parser.add_argument("--setup-proposal-sha256", required=True)
    parser.add_argument("--documents", type=Path, nargs="*", default=[])
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--descriptor", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check-registered-capacity",
        action="store_true",
        help="Optional capacity-only native read on this exact installed instance; no ledger/challenge",
    )
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("preflight requires -I -B -S")
    depot = args.depot.resolve(strict=True)
    if args.output.exists():
        raise FileExistsError("preflight output already exists")
    if (
        Path(sys.executable).resolve() != depot / "python/python.exe"
        or Path(__file__).resolve() != depot / "source/scripts/policy_owner/preflight_evidence.py"
    ):
        raise ValueError("preflight must use its actual delivered interpreter and source")
    if args.worker_manifest.absolute() != depot / "worker-manifest.json":
        raise ValueError("worker manifest is outside the exact depot")

    def digest(path):
        info = path.lstat()
        if info.st_nlink != 1 or info.st_file_attributes & 0x400:
            raise ValueError("preflight refuses aliases/reparse objects")
        with path.open("rb") as stream:
            value = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if (info.st_size, info.st_mtime_ns, info.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise ValueError("preflight input changed during read")
        return info.st_size, value

    if digest(args.worker_manifest)[1] != args.worker_manifest_sha256:
        raise ValueError("worker manifest pin mismatch")
    if digest(args.setup_proposal)[1] != args.setup_proposal_sha256:
        raise ValueError("setup proposal pin mismatch")
    proposal = json.loads(args.setup_proposal.read_bytes())
    if proposal.get("instance") != args.instance or proposal.get("root") != str(
        Path(os.environ["PROGRAMDATA"]) / ("TA-PolicyOwner-" + args.instance)
    ):
        raise ValueError("setup proposal exact instance mismatch")
    if (
        proposal.get("contract_version") != "policy-owner-setup-proposal-v1"
        or proposal.get("execution_authorized") is not False
        or proposal.get("registration_is_approval") is not False
    ):
        raise ValueError("setup proposal contract/authority mismatch")
    members = {row["relative_path"]: row for row in proposal["files"]}
    if len(members) != len(proposal["files"]):
        raise ValueError("setup proposal duplicate member")
    for relative, path in [
        ("worker-manifest.json", args.worker_manifest),
        ("bin/PolicyOwner.exe", args.native_owner),
        (
            "bin/ConditionalRuntimeProtectionHost.exe",
            depot / "bin/ConditionalRuntimeProtectionHost.exe",
        ),
        ("native-consumer-source.json", depot / "native-consumer-source.json"),
    ]:
        entry = members.get(relative)
        if entry is None or digest(path) != (entry["byte_length"], entry["sha256"]):
            raise ValueError("setup/native/helper immutable binding mismatch: " + relative)
    manifest = json.loads(args.worker_manifest.read_bytes())
    if (
        set(manifest) != {"contract_version", "files", "execution_authorized"}
        or manifest["contract_version"] != "policy-owner-worker-depot-v1"
        or manifest["execution_authorized"] is not False
    ):
        raise ValueError("worker manifest contract mismatch")
    names = set()
    for entry in manifest["files"]:
        name = entry["relative_path"]
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or name.casefold() in names
        ):
            raise ValueError("worker member path/duplicate mismatch")
        names.add(name.casefold())
        path = depot / name
        if members.get(name) != entry:
            raise ValueError("worker/setup member binding mismatch: " + name)
        if not path.resolve(strict=True).is_relative_to(depot) or digest(path) != (
            entry["byte_length"],
            entry["sha256"],
        ):
            raise ValueError("delivered source/runtime/dependency byte mismatch: " + name)

    def reject_scan_error(error):
        raise error

    for subtree in ("python", "dependencies", "source"):
        for directory, folders, files in os.walk(
            depot / subtree, followlinks=False, onerror=reject_scan_error
        ):
            for name in folders:
                if (Path(directory) / name).lstat().st_file_attributes & 0x400:
                    raise ValueError("reparse directory in delivered closure")
            for name in files:
                if (Path(directory) / name).relative_to(depot).as_posix().casefold() not in names:
                    raise ValueError("unlisted file in delivered source/runtime/dependency closure")
    if args.native_owner.resolve(strict=True) != depot / "bin/PolicyOwner.exe":
        raise ValueError("native preflight image is outside its exact depot binding")
    if digest(args.native_owner)[1] != args.native_owner_sha256:
        raise ValueError("native owner pin mismatch")
    # This explicit one-argument native operation returns constants before any
    # registration, service, role, UI or storage interface is constructed.
    result = subprocess.run(
        [str(args.native_owner), "evidence-capabilities"],
        capture_output=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError(
            "native fixed-capacity interface failed: " + result.stderr.decode("utf-8", "replace")
        )
    native = json.loads(result.stdout)
    sys.path[:0] = [str(depot / "source"), str(depot / "dependencies")]
    from tradingagents.policy_evidence_acquisition import new_evidence_output
    from tradingagents.policy_evidence_contract import (
        CHUNK_BYTES,
        MAX_ARTIFACT_BYTES,
        MAX_DESCRIPTOR_BYTES,
        ArtifactDescriptor,
        validate_artifact,
    )
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_owner_contract import evidence_intake_entries
    from tradingagents.policy_qualified_contract import canonical_bytes, strict_json

    bound_evidence_process(seconds=300)
    if native.get("contract_version") != "policy-owner-evidence-capabilities-v2" or (
        native["maximum_artifact_bytes"],
        native["maximum_chunk_bytes"],
        native["maximum_descriptor_bytes"],
    ) != (MAX_ARTIFACT_BYTES, CHUNK_BYTES, MAX_DESCRIPTOR_BYTES):
        raise ValueError("native/library capacity contract mismatch")
    documents = [strict_json(path.read_bytes()) for path in args.documents]
    for document in documents:
        evidence_intake_entries(document)
    # Conservative metadata reserve for full signed wrappers, native proofs and
    # UTF-8 control headers. No future proof/identity or human receipt is created.
    envelope = native["maximum_authentication_envelope_bytes"]
    if envelope != 256 * 1024:
        raise ValueError("native authentication-envelope contract mismatch")
    projected_journal = sum(len(canonical_bytes(document)) + envelope + 1 for document in documents)
    maximum_document = max((len(canonical_bytes(document)) for document in documents), default=0)
    projected_request = projected_journal + maximum_document + 1024 * 1024
    if (
        projected_journal > native["authentication_journal_bytes"]
        or projected_request > native["owner_frame_bytes"]
    ):
        raise ValueError("complete proposed metadata route exceeds journal/request capacity")
    capacity_snapshot = None
    if args.check_registered_capacity:
        if depot != Path(os.environ["PROGRAMDATA"]) / ("TA-PolicyOwner-" + args.instance):
            raise ValueError(
                "registered capacity read requires the exact installed depot; fixture/staging cannot impersonate it"
            )
        checked = subprocess.run(
            [str(args.native_owner), "request", args.instance],
            input=canonical_bytes(
                {"operation": "evidence-capacity-v2", "payload": {"documents": documents}}
            ),
            capture_output=True,
            timeout=105,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if checked.returncode:
            raise ValueError(
                "registered capacity unavailable/rejected: "
                + checked.stderr.decode("utf-8", "replace")
            )
        capacity_snapshot = strict_json(checked.stdout)
        if (
            capacity_snapshot.get("contract_version") != "policy-owner-evidence-capacity-v2"
            or capacity_snapshot.get("instance") != args.instance
            or capacity_snapshot.get("challenge_created") is not False
            or capacity_snapshot.get("ledger_contents_read") is not False
            or capacity_snapshot.get("ledger_state_queried") is not False
            or capacity_snapshot.get("execution_authorized") is not False
        ):
            raise ValueError("registered capacity response lost its non-consuming binding")
    if (args.artifact is None) != (args.descriptor is None):
        raise ValueError("artifact and descriptor must be provided together")
    if args.descriptor:
        descriptor = ArtifactDescriptor.read(args.descriptor.read_bytes())
        if descriptor.instance != args.instance:
            raise ValueError("preflight exact instance mismatch")
        validate_artifact(args.artifact, descriptor)
    origins = []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path:
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(depot):
                raise ValueError("module import escaped delivered closure: " + name)
            if resolved.relative_to(depot).as_posix().casefold() not in names:
                raise ValueError("module has no delivered manifest member: " + name)
            origins.append(
                {"module": name, "relative_path": resolved.relative_to(depot).as_posix()}
            )
    report = {
        "status": "FIXED_EVIDENCE_BINDINGS_VALIDATED_NO_AUTHORITY",
        "instance": args.instance,
        "worker_members_checked": len(names),
        "native_capabilities": native,
        "module_origins": origins,
        "setup_proposal_sha256": args.setup_proposal_sha256,
        "projected_metadata_journal_bytes": projected_journal,
        "projected_metadata_request_bytes": projected_request,
        "projection_is_not_an_actual_authenticated_journal": True,
        "owner_rpc_called": args.check_registered_capacity,
        "registered_capacity_snapshot": capacity_snapshot,
        "existing_registered_journal_capacity": "VERIFIED_SNAPSHOT_NOT_RESERVED"
        if capacity_snapshot is not None
        else "NOT_QUERIED_UNKNOWN",
        "namespace_initialized": False,
        "claim_created": False,
        "human_authentication_requested": False,
        "remaining": [
            "genuine enrollment/role/storage if not already present",
            "exact descriptor consent and registration",
            "fresh current authority and canonical admission checks at their permitted time",
        ],
        "execution_authorized": False,
    }
    with new_evidence_output(args.output) as stream:
        stream.write(canonical_bytes(report) + b"\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "worker_members_checked": len(names),
                "owner_rpc_called": args.check_registered_capacity,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
