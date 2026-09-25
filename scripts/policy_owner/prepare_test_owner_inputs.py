"""Materialize labelled inert measurement inputs and a proposed real-user test role.

Creates no principal, authentication, APPROVE record, allocation or claim. The
actual operator still has to confirm setup and authenticate with Windows Hello.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def prepare(args):
    from tradingagents.policy_owner_contract import OriginalAcquisitionRegistry, RoleProposal
    from tradingagents.policy_qualified_contract import (
        BytePin,
        EvidenceRef,
        OriginalAcquisition,
        canonical_bytes,
        content_identity,
    )

    output = args.output.resolve(strict=False)
    source = Path(__file__).resolve().parents[2]
    if output.exists() or output.is_relative_to(source) or not args.instance.startswith("test-"):
        raise ValueError(
            "test input preparation needs a fresh external directory and test instance"
        )
    output.mkdir()
    catalog = []

    def save(namespace, payload, name):
        value = {**payload, "identity": content_identity(namespace, payload)}
        raw = canonical_bytes(value)
        path = output / name
        with path.open("xb") as stream:
            stream.write(raw)
        ref = EvidenceRef(identity=value["identity"], pin=BytePin.of(raw))
        catalog.append({"reference": ref.model_dump(mode="json"), "path": str(path)})
        return ref, value

    procedure, _ = save(
        "policy-owner-inert-measurement-fixture:v1",
        {
            "label": "SYNTHETIC MEASUREMENTS ONLY — no historical acquisition or authentication",
            "instance": args.instance,
            "execution_authorized": False,
        },
        "TEST-measurement-contract.json",
    )
    inventories, acquisitions = [], []
    root = str(output / "TEST-data-only-root-never-created")
    for phase, attributes in (("before", 0x10000010), ("after", 0x10)):
        entries = [
            {
                "path": ".",
                "kind": "directory",
                "attributes": attributes,
                "mtime_ns": 101,
                "ctime_ns": 100,
                "descriptor_sha256": "f" * 64,
            }
        ]
        roots = [
            {
                "path": root,
                "entries": entries,
                "file_count": 0,
                "unreadable_count": 0,
                "security_descriptors_requested": True,
                "inventory_identity": content_identity(
                    "crash-protection-historical-root-inventory:v1", entries
                ),
            }
        ]
        payload = {
            "phase": phase,
            "roots": roots,
            "elapsed_seconds": 1.0,
            "file_count": 0,
            "unreadable_count": 0,
            "no_acl_write_or_recovery": True,
        }
        if phase == "after":
            payload.update(equal=False, changed_roots=[root])
        ref, value = save(
            "crash-protection-historical-inventory:v1", payload, "TEST-" + phase + ".json"
        )
        inventories.append(value)
        acquisitions.append(
            OriginalAcquisition(
                observation_id="TEST-" + args.instance + "-original-" + phase,
                original_path=str(output / ("TEST-" + phase + ".json")),
                phase=phase,
                origin="RETAINED_ORIGINAL_FILES",
                artifact=ref,
                collector_pin=procedure.pin,
                procedure_pin=procedure.pin,
                collection_elapsed_seconds=1.0,
                collection_utc=None,
                sample_utc=None,
                transition_utc=None,
            )
        )
    before, after = (item["roots"][0] for item in inventories)
    delta, _ = save(
        "crash-protection-historical-delta-detail:v1",
        {
            "status": "DIFFERENCE_REQUIRES_CLASSIFICATION",
            "source_modified": False,
            "iso02_started": False,
            "no_repair_performed": True,
            "roots": [
                {
                    "root": root,
                    "before_identity": before["inventory_identity"],
                    "after_identity": after["inventory_identity"],
                    "changes": [
                        {
                            "relative_path": ".",
                            "kind": "directory",
                            "before": before["entries"][0],
                            "after": after["entries"][0],
                            "changed_fields": ["attributes"],
                        }
                    ],
                }
            ],
        },
        "TEST-delta.json",
    )
    blocker, _ = save(
        "crash-protection-source-requalification-blocked:v1",
        {
            "label": "ISOLATED SYNTHETIC MEASUREMENT FIXTURE",
            "status": "BLOCKED",
            "candidate": {"commit": "1" * 40, "tree": "2" * 40, "parent": "3" * 40},
            "historical_inventory": {"equal": False, "changed_roots": [root]},
            "difference": {
                "root": root,
                "changed_directory_count": 1,
                "changed_field": "attributes",
                "before_hex": "0x10000010",
                "after_hex": "0x00000010",
                "changed_bit": "0x10000000",
            },
            "artifacts": [{"path": item["path"], **item["reference"]["pin"]} for item in catalog],
        },
        "TEST-blocker.json",
    )
    registry = OriginalAcquisitionRegistry.seal(
        contract_version="policy-owner-original-acquisition-registry-v1",
        source_package_manifest_pin=procedure.pin,
        original_blocker=blocker,
        before=acquisitions[0],
        after=acquisitions[1],
        complete_delta_ledger=delta,
        acquisition_registration_mode="EXACT_RETAINED_ORIGINAL_PAIR_ONLY",
        newly_acquired_observations_eligible=False,
    )
    role = RoleProposal.seal(
        contract_version="policy-owner-project-role-proposal-v1",
        instance=args.instance,
        operator_sid=args.operator_sid,
        observed_account_name=args.account_name,
        role="PERSONAL_RESEARCH_CAMPAIGN_ISSUING_OPERATOR",
        authority_basis="Proposed by the issuing operator for explicitly isolated adapter and inert native integration tests; requires the actual operator's Windows Hello enrollment.",
        prospective_project_policy_scope="ISOLATED TEST OWNER ONLY: synthetic measurement policy tests; no operational historical exception approval.",
        campaign_specification_scope="ISOLATED TEST OWNER ONLY: exercise qualification contracts with synthetic stage evidence; no ISO or Ticket-07 execution.",
        project_contract_scope="ISOLATED TEST OWNER ONLY: evaluate the forward conditional consumer with inert local children and no live authority.",
        third_party_or_higher_policy_authority_claimed=False,
        exact_package_approved=False,
        qualification_execution_authorized=False,
        live_execution_authorized=False,
    )
    for name, raw in {
        "original-acquisitions.json": registry.to_bytes(),
        "role-proposal.json": role.to_bytes(),
        "artifact-catalog.json": canonical_bytes(
            {
                "contract_version": "policy-owner-setup-artifact-catalog-v1",
                "artifacts": catalog,
                "execution_authorized": False,
            }
        ),
    }.items():
        with (output / name).open("xb") as stream:
            stream.write(raw)
    return {
        "output": str(output),
        "registry_identity": registry.identity,
        "role_proposal_identity": role.identity,
        "human_authenticated": False,
        "principal_enrolled": False,
        "execution_authorized": False,
    }


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("use an existing interpreter with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dependencies", required=True, type=Path)
    for name in ("instance", "operator-sid", "account-name"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    sys.path[:0] = [
        str(Path(__file__).resolve().parents[2]),
        str(args.dependencies.resolve(strict=True)),
    ]
    print(json.dumps(prepare(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
