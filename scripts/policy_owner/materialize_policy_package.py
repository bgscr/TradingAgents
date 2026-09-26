"""Materialize an unsigned exact-source policy proposal without starting a clock.

Reads retained evidence and a clean direct-child commit. Does not contact the
owner, create authentication, allocate stage identifiers, or execute any stage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_COMMIT = "e9c971e832ca6abb96c64c3515e90d95a37c9c5a"
PREDECESSOR_MANIFEST_SHA256 = "61610a461d14f91ea82df7b485646e734df37d81c0c579b55bcd7a1da9caab05"
PREDECESSOR_PROPOSAL_SHA256 = "75d5fe5cf86f9a8cdf8c96bb6b1872894bdc3ef10b759d59a69478c9bd581722"


def read_predecessor(root: Path) -> tuple[dict, bytes, bytes]:
    from tradingagents.policy_qualified_contract import BytePin, strict_json

    raw = (root / "PACKAGE-MANIFEST.json").read_bytes()
    if BytePin.of(raw).sha256 != PREDECESSOR_MANIFEST_SHA256:
        raise ValueError("the exact authorized predecessor manifest is required")
    manifest = strict_json(raw)
    entries = {item["relative_path"]: item for item in manifest["files"]}
    if len(entries) != len(manifest["files"]):
        raise ValueError("duplicate predecessor path")
    saved = {}
    for name, item in entries.items():
        path = root / name
        if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise ValueError("predecessor path escapes its package")
        value = path.read_bytes()
        BytePin.read({key: item[key] for key in ("byte_length", "sha256")}).check(value)
        saved[name] = value
    proposal = saved["PROPOSAL.md"]
    if BytePin.of(proposal).sha256 != PREDECESSOR_PROPOSAL_SHA256:
        raise ValueError("wrong predecessor proposal")
    retained = strict_json(saved["RETAINED-HISTORICAL-SCOPE.json"])
    for item in retained["evidence"]:
        BytePin.read({key: item[key] for key in ("byte_length", "sha256")}).check(
            Path(item["path"]).read_bytes()
        )
    return retained, raw, proposal


def original_registry(retained: dict, manifest_raw: bytes):
    from tradingagents.policy_owner_contract import OriginalAcquisitionRegistry
    from tradingagents.policy_qualified_contract import (
        BytePin,
        EvidenceRef,
        OriginalAcquisition,
        content_identity,
    )

    evidence = retained["evidence"]

    def item(name: str) -> dict:
        matches = [
            entry for entry in evidence if entry["path"].replace("\\", "/").split("/")[-1] == name
        ]
        if len(matches) != 1:
            raise ValueError("original evidence path is absent or ambiguous: " + name)
        return matches[0]

    def pin(entry):
        return BytePin.read({key: entry[key] for key in ("byte_length", "sha256")})

    def reference(entry):
        return EvidenceRef(identity=entry["declared_identity"], pin=pin(entry))

    acquisitions = []
    for phase in ("before", "after"):
        entry = item("historical-" + phase + ".json")
        captured = [value for value in retained["collection_payloads"] if value["phase"] == phase]
        if len(captured) != 1 or captured[0]["identity"] != entry["declared_identity"]:
            raise ValueError("original phase/duration provenance differs")
        # This is an immutable registry key for an existing acquisition, not
        # a new measurement identity derived from equal-looking bytes.
        observation = content_identity(
            "retained-original-acquisition-key:v1",
            {
                "path": entry["path"],
                "phase": phase,
                "pin": pin(entry).model_dump(mode="json"),
            },
        )
        acquisitions.append(
            OriginalAcquisition(
                observation_id=observation,
                original_path=entry["path"],
                phase=phase,
                origin="RETAINED_ORIGINAL_FILES",
                artifact=reference(entry),
                collector_pin=pin(item("capture_historical.py")),
                procedure_pin=pin(item("procedure-discovery.json")),
                collection_elapsed_seconds=float(captured[0]["elapsed_seconds"]),
                collection_utc=None,
                sample_utc=None,
                transition_utc=None,
            )
        )
    return OriginalAcquisitionRegistry.seal(
        contract_version="policy-owner-original-acquisition-registry-v1",
        source_package_manifest_pin=BytePin.of(manifest_raw),
        original_blocker=reference(item("qualification-blocked-condition-v1.json")),
        before=acquisitions[0],
        after=acquisitions[1],
        complete_delta_ledger=reference(item("historical-delta-detail-v1.json")),
        acquisition_registration_mode="EXACT_RETAINED_ORIGINAL_PAIR_ONLY",
        newly_acquired_observations_eligible=False,
    )


def budget_proposal(ledger: dict, historical_procedure: dict) -> dict:
    from tradingagents.policy_qualified_contract import STAGES
    from tradingagents.windows_sandbox_acceptance import (
        TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        Ticket06CommandLedger,
    )

    Ticket06CommandLedger.model_validate_json(json.dumps(ledger))
    entries = ledger["entries"]
    if ledger["ledger_identity"] != TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY or (
        len(entries) != 27
        or [item["ordinal"] for item in entries] != list(range(1, 28))
        or any(item["mandatory"] is not True for item in entries)
    ):
        raise ValueError("the complete unchanged 27-command canonical ledger is required")
    expected = [1800] * 19 + [10800] * 3 + [600] * 5
    if [item["timeout_policy"]["timeout_seconds"] for item in entries] != expected or (
        any(item["timeout_policy"]["termination_grace_seconds"] != 15 for item in entries)
        or historical_procedure["iso04_host_timeout_seconds"] != 14400
        or historical_procedure["ticket06_command_ledger_identity"] != ledger["ledger_identity"]
    ):
        raise ValueError("retained canonical timeout references changed")
    proposed = (14400, 7200, 1800, 14400, 14400, 14400)
    rationales = (
        "Proposed four-hour cap for source/runtime resealing and complete retained comparisons; no normative stage total was found.",
        "Proposed two-hour stage cap includes the existing CLI maximum launch timeouts of 600 and 3600 seconds plus 3000 seconds for host verification; existing defaults remain 180 and 1800 seconds.",
        "Proposed thirty-minute cap for an additive QCF17 baseline and its exact-byte verification; no normative stage total was found.",
        "Proposed reuse of the historical 14400-second outer cap as a NEW decision item. Individual command maxima sum to 69600 seconds and are not an additive grant. All 27 must pass within this outer cap; exhaustion remains terminal BLOCKED.",
        "Proposed four-hour cap for two sequential independent reviews and retained reports; no normative stage total was found.",
        "Proposed four-hour cap for complete post-review canonical/raw comparisons, physical evidence inventories and closure; no exclusion or predicate is relaxed.",
    )
    stages = [
        {"stage": stage, "maximum_execution_seconds": seconds, "maximum_cleanup_seconds": 900}
        for stage, seconds in zip(STAGES, proposed, strict=True)
    ]
    return {
        "contract_version": "policy-qualified-budget-proposal-v1",
        "status": "PROPOSED_REQUIRES_FRESH_EXACT_HUMAN_GRANT",
        "canonical_command_ledger_identity": ledger["ledger_identity"],
        "canonical_individual_timeout_seconds": expected,
        "canonical_termination_grace_seconds": [15] * 27,
        "sum_individual_timeout_seconds": sum(expected),
        "historical_outer_timeout_seconds_reference_only": 14400,
        "stage_budgets": stages,
        "stage_rationales": dict(zip(STAGES, rationales, strict=True)),
        "cleanup_rationale": "Each 900-second cleanup cap is separately proposed for containment/quiescence and evidence retention. It permits no historical cleanup or deletion and does not extend successful stage completion.",
        "finalization_maximum_seconds": 3600,
        "finalization_rationale": "Proposed one-hour cap for complete fresh owner/evidence checks and post-result receipt verification; no real result or receipt is issued by materialization.",
        "total_remaining_budget_seconds": sum(proposed) + 900 * len(STAGES) + 3600,
        "effective_utc": None,
        "stage_ids_allocated": False,
        "execution_authorized": False,
    }


def policy_objects(
    candidate,
    retained,
    registry,
    *,
    predecessor_pin,
    scope_pin,
    authority_pin,
    procedure_pin,
    coordinator_pin,
    consumer_pin,
    measurement_pin,
    budgets,
    coverage_scope=None,
    coverage_measurement=None,
):
    from tradingagents.policy_qualified_contract import (
        STAGES,
        AcceptancePolicy,
        Candidate,
        HistoricalExceptionScope,
        LineagePlan,
    )
    from tradingagents.windows_sandbox_acceptance import (
        TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    )

    candidate = Candidate.read(candidate)
    root = retained["historical_root"]
    if retained["saved_verification"]["changed_rows"] != 2571:
        raise ValueError("the reviewed operational historical scope is not the exact 2571-row pair")
    scope = HistoricalExceptionScope.seal(
        contract_version="historical-state-exception-v1",
        candidate=candidate,
        original_candidate=Candidate.read(
            {key: retained["candidate"][key] for key in ("commit", "tree", "parent")}
        ),
        original_blocker=registry.original_blocker,
        before=registry.before,
        after=registry.after,
        root=root["root"],
        before_root_identity=root["before_identity"],
        after_root_identity=root["after_identity"],
        complete_delta_ledger=registry.complete_delta_ledger,
        changed_rows=2571,
        changed_field="attributes",
        raw_before=0x10000010,
        raw_after=0x10,
        historical_equal=False,
        historical_cause="UNRESOLVED",
        prior_outcome="BLOCKED",
    )
    plan = LineagePlan.seal(
        contract_version="historical-state-lineage-plan-v1",
        candidate=candidate,
        stage_slots=STAGES,
        stage_budgets=budgets["stage_budgets"],
        finalization_maximum_seconds=budgets["finalization_maximum_seconds"],
        maximum_lineages=1,
        maximum_claims=1,
        claim_window_days=7,
        terminal_window_days=30,
        source_change_transfer_permitted=False,
        ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        procedure_pin=procedure_pin,
        coordinator_pin=coordinator_pin,
        consumer_contract_pin=consumer_pin,
        measurement_contract_pin=measurement_pin,
        pre_result_inventory_scope="complete-evidence-and-pre-result-stage-roots-v1",
    )
    policy_model = AcceptancePolicy
    extra = {}
    if coverage_scope is not None:
        from tradingagents.policy_coverage_contract import artifact_ref
        from tradingagents.policy_coverage_models import CoveragePolicy

        policy_model = CoveragePolicy
        extra = {
            "coverage_scope": artifact_ref(coverage_scope),
            "coverage_measurement": coverage_measurement,
            "coverage_rule": "KNOWN_RECORDED_FIELDS_UNKNOWN_HISTORY_STRICT_CURRENT_WINDOW",
        }
    policy = policy_model.seal(
        **extra,
        contract_version=(
            "historical-state-acceptance-policy-v2"
            if extra
            else "historical-state-acceptance-policy-v1"
        ),
        scope=scope,
        exception_identity=scope.exception_identity,
        lineage_plan=plan,
        predecessor_proposal_pin=predecessor_pin,
        scope_evidence_manifest_pin=scope_pin,
        authority_source_contract_pin=authority_pin,
        claim_window_days=7,
        terminal_window_days=30,
        maximum_claims=1,
        revocation_is_terminal=True,
        failure_consumes_attempt=True,
        new_discrepancy_is_terminal=True,
        mandatory_predicate_contract="canonical-ISO02-ISO03-QCF17-A-and-B-before-QCF18-all-27-commands-zero-current-delta-v1",
    )
    registry.validate_policy(policy)
    return scope, plan, policy


def exact_candidate(source: Path, expected_parent: str = BASE_COMMIT):
    from tradingagents.policy_qualified_contract import Candidate

    def git(*args):
        return (
            subprocess.run(
                [
                    "git",
                    "-c",
                    "safe.directory=" + source.as_posix(),
                    "--no-optional-locks",
                    "-C",
                    str(source),
                    *args,
                ],
                check=True,
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            .stdout.decode("utf-8")
            .strip()
        )

    if not re.fullmatch(r"[0-9a-f]{40}", expected_parent):
        raise ValueError("full explicitly authorized parent commit required")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("final materialization requires a clean exact source worktree")
    value = Candidate(
        commit=git("rev-parse", "HEAD"),
        tree=git("rev-parse", "HEAD^{tree}"),
        parent=git("rev-parse", "HEAD^"),
    )
    if value.parent != expected_parent or git(
        "rev-list", "--parents", "-n", "1", "HEAD"
    ).split() != [
        value.commit,
        expected_parent,
    ]:
        raise ValueError("one reviewed direct child of the exact authorized base is required")
    return value


def source_checkout_binding(raw: bytes, committed: bytes) -> dict:
    """Retain physical and canonical pins without obscuring CRLF checkout bytes."""
    from tradingagents.policy_qualified_contract import BytePin

    form = "IDENTICAL"
    if raw != committed:
        raw.decode("utf-8")
        if b"\0" in raw or raw.replace(b"\r\n", b"\n") != committed:
            raise ValueError("physical contract source differs from exact committed bytes")
        form = "UTF8_CRLF_CHECKOUT"
    return {"git_blob_pin": BytePin.of(committed).model_dump(mode="json"), "checkout_form": form}


def materialize(args) -> dict:
    from build_policy_owner import canonical, pin, write_new

    from tradingagents.policy_qualified_contract import (
        BytePin,
        EvidenceRef,
        ReviewedPolicyPackage,
        content_identity,
        contract_descriptor,
        strict_json,
    )

    source = Path(__file__).resolve().parents[2]
    expected_parent = getattr(args, "expected_parent", BASE_COMMIT)
    candidate = exact_candidate(source, expected_parent)
    output = args.output.resolve(strict=False)
    if output.exists() or output == source or output.is_relative_to(source):
        raise ValueError("a fresh external final-package directory is required")
    retained, predecessor_manifest, predecessor_proposal = read_predecessor(args.predecessor)
    registry = original_registry(retained, predecessor_manifest)
    retained_files = {Path(item["path"]).name: item for item in retained["evidence"]}
    ledger_raw = Path(retained_files["canonical-ticket06-ledger.json"]["path"]).read_bytes()
    procedure_raw = Path(retained_files["procedure-discovery.json"]["path"]).read_bytes()
    budgets = budget_proposal(strict_json(ledger_raw), strict_json(procedure_raw))
    coverage_version = getattr(args, "coverage_v2", False)
    coverage_scope = coverage_measurement = None
    files: dict[str, bytes] = {}

    def add(name, raw):
        files[name] = raw
        return BytePin.of(raw)

    def data(name, value):
        return add(name, canonical(value))

    predecessor_pin = add("predecessors/PROPOSAL.md", predecessor_proposal)
    add("predecessors/PACKAGE-MANIFEST.json", predecessor_manifest)
    scope_pin = add(
        "RETAINED-HISTORICAL-SCOPE.json",
        (args.predecessor / "RETAINED-HISTORICAL-SCOPE.json").read_bytes(),
    )
    add("ORIGINAL-ACQUISITION-REGISTRY.json", registry.to_bytes())
    budget_pin = data("PROPOSED-BUDGETS.json", budgets)
    ledger_pin = add("predecessors/canonical-ticket06-ledger.json", ledger_raw)
    historical_procedure_pin = add("predecessors/procedure-discovery.json", procedure_raw)
    for name in ("capture_historical.py",):
        add("predecessors/" + name, Path(retained_files[name]["path"]).read_bytes())
    # The bundle pins every file that implements these contracts, including the
    # forward native route. Its identity is new for this exact candidate.
    names = {
        "tradingagents/policy_qualified_contract.py",
        "tradingagents/policy_qualified_results.py",
        "tradingagents/policy_qualification_lifecycle.py",
        "tradingagents/policy_owner_contract.py",
        "tradingagents/windows_policy_owner.py",
        "tradingagents/policy_qualified_native.py",
        "tradingagents/policy_evidence_contract.py",
        "tradingagents/policy_evidence_acquisition.py",
        "tradingagents/policy_evidence_client.py",
        "tradingagents/policy_evidence_limits.py",
        "tradingagents/qualified_routing_composition.py",
        "tradingagents/runtime_protection.py",
        "tradingagents/runtime_protection_handoff.py",
        "cli/main.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
        "tradingagents/offline_runtime_bundle.py",
        "docs/contracts/policy-qualified-result-v1.md",
        "docs/contracts/windows-policy-owner-v1.md",
        "docs/contracts/policy-evidence-acquisition-v2.md",
        "scripts/runtime_protection/RuntimeProtectionHost.cs",
        "scripts/verify_ticket06_immutable_state.py",
        "scripts/run_iso03_windows_sandbox_qualification.py",
    }
    if coverage_version:
        names.update(
            {
                "tradingagents/policy_coverage_contract.py",
                "tradingagents/policy_coverage_native.py",
                "tradingagents/policy_coverage_models.py",
                "tradingagents/policy_coverage_verification.py",
                "tradingagents/policy_coverage_schema.py",
                "docs/contracts/policy-coverage-v2.md",
            }
        )
        names.update(
            path.relative_to(source).as_posix()
            for path in (source / "tradingagents").glob("policy_*.py")
        )
        names.update(
            path.relative_to(source).as_posix()
            for path in (source / "tests/fixtures").glob("*coverage*.py")
        )
        names.add("tests/fixtures/policy_owner/local_owner_fixture.py")
        names.update(
            "tests/fixtures/runtime_protection/" + name + ".cs"
            for name in (
                "CoverageEvidenceContracts",
                "EvidenceTransferContracts",
                "PolicyAuthenticationContracts",
            )
        )
    names.update(
        path.relative_to(source).as_posix()
        for path in (source / "scripts/policy_owner").iterdir()
        if path.is_file() and path.suffix in {".cs", ".py", ".ps1"}
    )
    source_files = []
    for name in sorted(names):
        raw = (source / name).read_bytes()
        committed = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=" + source.as_posix(),
                "--no-optional-locks",
                "-C",
                str(source),
                "cat-file",
                "blob",
                candidate.commit + ":" + name,
            ],
            capture_output=True,
            check=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        checkout = source_checkout_binding(raw, committed)
        file_pin = add("source/" + name, raw)
        source_files.append({"relative_path": name, **file_pin.model_dump(mode="json"), **checkout})
    if coverage_version:
        from tradingagents.policy_coverage_schema import coverage_contract_descriptor

        schema = coverage_contract_descriptor()
    else:
        schema = contract_descriptor()
    schema_pin = data("contract-schema.json", schema)
    consumer_pin = data(
        "CONTRACT-SOURCES.json",
        {
            "contract_version": "policy-qualified-exact-source-bundle-v2",
            "candidate": candidate.model_dump(mode="json"),
            "files": source_files,
            "contract_schema_pin": schema_pin.model_dump(mode="json"),
            "execution_authorized": False,
        },
    )
    coordinator_pin = data(
        "COORDINATOR-SOURCES.json",
        {
            "contract_version": "policy-qualified-coordinator-source-proposal-v1",
            "source_bundle_pin": consumer_pin.model_dump(mode="json"),
            "entrypoint": "scripts/policy_owner/owner_worker.py",
            "ledger_registration": "EXACT_PROTECTED_OWNER_INSTANCE_REQUIRED",
            "execution_authorized": False,
        },
    )
    measurement_pin = data(
        "MEASUREMENT-CONTRACT.json",
        {
            "contract_version": "policy-qualified-measurement-contract-proposal-v1",
            "source_bundle_pin": consumer_pin.model_dump(mode="json"),
            "raw_attribute_mask_ignored": 0,
            "historical_pair": registry.identity,
            "new_observations_require_distinct_authenticated_acquisition_registration": True,
            "new_comparisons_require_exact_equality": True,
            "unreadable_reparse_or_missing_evidence_is_terminal": True,
            "measurement_executed": False,
            "additive_capture_entrypoint": "scripts/policy_owner/acquire_evidence.py capture",
            "production_inventory_entrypoint": "scripts/policy_owner/acquire_evidence.py produce-source",
            "evidence_intake_contract": "policy-owner-evidence-intake-v2",
            "complete_artifact_bytes_maximum": 268435456,
            "execution_authorized": False,
        },
    )
    if coverage_version:
        from tradingagents.policy_coverage_contract import (
            CoverageMeasurement,
            artifact_ref,
            build_scope,
        )

        before_raw = Path(registry.before.original_path).read_bytes()
        after_raw = Path(registry.after.original_path).read_bytes()
        registry.before.artifact.pin.check(before_raw)
        registry.after.artifact.pin.check(after_raw)
        coverage_scope = build_scope(before_raw, after_raw, fixture_only=False)
        add("COVERAGE-SCOPE.json", coverage_scope.to_bytes())
        coverage_measurement = CoverageMeasurement.seal(
            contract_version="policy-coverage-measurement-v2",
            scope=artifact_ref(coverage_scope),
            candidate=candidate,
            supported_reparse_tags=(0xA0000003,),
            security_required=True,
            maximum_entries=2_000_000,
            maximum_read_bytes=64 * 1024**3,
            maximum_seconds=600,
            maximum_depth=128,
            maximum_reparse_bytes=16384,
            maximum_indirect_link_depth=0,
            target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
            known_fields_required=True,
        )
        measurement_pin = add("MEASUREMENT-CONTRACT.json", coverage_measurement.to_bytes())
    procedure_pin = data(
        "PROCEDURE-PROPOSAL.json",
        {
            "contract_version": "policy-qualified-procedure-proposal-v1",
            "canonical_ledger_pin": ledger_pin.model_dump(mode="json"),
            "historical_procedure_reference_pin": historical_procedure_pin.model_dump(mode="json"),
            "source_bundle_pin": consumer_pin.model_dump(mode="json"),
            "budget_proposal_pin": budget_pin.model_dump(mode="json"),
            "status": "PROPOSED_NOT_GRANTED",
            "effective_utc": None,
            "execution_authorized": False,
        },
    )
    authority_pin = add(
        "AUTHORITY-CONTRACT-PROPOSAL.md",
        (source / "docs/contracts/windows-policy-owner-v1.md").read_bytes()
        + (
            b"\n\n" + (source / "docs/contracts/policy-coverage-v2.md").read_bytes()
            if coverage_version
            else b""
        ),
    )
    scope, plan, policy = policy_objects(
        candidate,
        retained,
        registry,
        predecessor_pin=predecessor_pin,
        scope_pin=scope_pin,
        authority_pin=authority_pin,
        procedure_pin=procedure_pin,
        coordinator_pin=coordinator_pin,
        consumer_pin=consumer_pin,
        measurement_pin=measurement_pin,
        budgets=budgets,
        coverage_scope=coverage_scope,
        coverage_measurement=coverage_measurement,
    )
    add("HISTORICAL-EXCEPTION.json", scope.to_bytes())
    add("LINEAGE-PLAN.json", plan.to_bytes())
    policy_pin = add("ACCEPTANCE-POLICY.json", policy.to_bytes())
    package = ReviewedPolicyPackage.seal(
        contract_version="reviewed-policy-contract-package-v1",
        policy=EvidenceRef(identity=policy.acceptance_policy_identity, pin=policy_pin),
        scope_evidence_manifest_pin=scope_pin,
        contract_source_pin=consumer_pin,
    )
    add("REVIEWED-POLICY-PACKAGE.json", package.to_bytes())
    # Optional bindings are retained as evidence/proposals only. Their presence
    # cannot authenticate the issuer; the native owner must freshly resolve them.
    if args.setup_bindings:
        from tradingagents.policy_owner_contract import StorageBinding

        raw = args.setup_bindings.read_bytes()
        binding = StorageBinding.read(raw)
        if not binding.operational or binding.original_registry_pin != BytePin.of(
            canonical(registry.model_dump(mode="json"))
        ):
            raise ValueError(
                "final operational package cannot adopt a test store or another original registry"
            )
        add("OWNER-STORAGE-BINDING.json", raw)
    for name, path in (
        ("reviews/operational-trust.md", args.operational_review),
        ("reviews/authorization-native.md", args.enforcement_review),
    ):
        add(name, path.read_bytes())
    add("VALIDATION.json", args.validation.read_bytes())
    add(
        "FINAL-DECISION.md",
        decision_document(candidate, policy, package, budgets, bool(args.setup_bindings)).encode(
            "utf-8"
        ),
    )
    manifest = {
        "contract_version": "policy-owner-exact-decision-delivery-v1",
        "candidate": candidate.model_dump(mode="json"),
        "labels": ["UNSIGNED", "NOT_APPROVED"],
        "effective_utc": None,
        "reviewed_policy_package_identity": package.identity,
        "policy_identity": policy.acceptance_policy_identity,
        "plan_identity": plan.lineage_plan_identity,
        "exception_identity": scope.exception_identity,
        "historical_important": "UNRESOLVED",
        "files": [
            {"relative_path": name, **BytePin.of(raw).model_dump(mode="json")}
            for name, raw in sorted(files.items())
        ],
        "execution_authorized": False,
    }
    manifest["identity"] = content_identity("policy-owner-exact-decision-delivery:v1", manifest)
    if exact_candidate(source, expected_parent) != candidate:
        raise ValueError("source changed before final package publication")
    output.mkdir()
    for name, raw in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        write_new(path, raw)
    write_new(output / "PACKAGE-MANIFEST.json", canonical(manifest))
    return {
        "path": str(output),
        "manifest": pin(output / "PACKAGE-MANIFEST.json"),
        "candidate": candidate.model_dump(mode="json"),
        "package_identity": package.identity,
        "status": "UNSIGNED_NOT_APPROVED",
        "execution_authorized": False,
    }


def decision_document(candidate, policy, package, budgets, has_storage: bool) -> str:
    from tradingagents.policy_coverage_models import CoveragePolicy

    coverage_text = ""
    if isinstance(policy, CoveragePolicy):
        coverage_text = f"""
This is the DISTINCT v2 coverage proposal, not the old original-root-equality policy.
Exact coverage scope: `{policy.coverage_scope.identity}`; its complete bytes/pin
are COVERAGE-SCOPE.json and PACKAGE-MANIFEST.json. Measurement:
`{policy.coverage_measurement.identity}`. All seven enumeration-failure markers,
their separate directory rows and all 5,322 retained reparse records stay visible.

The proposed human design/risk decision must separately adopt known-recorded-field
matching, permanently UNKNOWN historical coverage and strict preservation within
a genuinely new complete B0 window. This does not prove historical completeness,
object identity continuity, unchanged hidden contents or historical link targets.
It cannot retroactively complete the original acquisition or erase any BLOCKED
outcome. The 2,571-row attributes exception supplies none of this different authority.

Only exact declared, supported mount-point/junction objects are measurable under
the proposed bounded no-follow/contained-target contract. Current unreadability,
missing security data, changed known fields, unsupported/escaping/cyclic/undeclared
links and missing required target coverage remain blocking. Actual historical
roots have NOT been measured by this development campaign. Their readability,
current tags, containment and preservation remain unknown.

B0, its assessment, acquisition IDs and future irreversible claim remain
UNMATERIALIZED. No test fixture can supply these real inputs. Operational mode
rejects generated fixture observations. No prior approval or dates transfer.
"""
    rows = "\n".join(
        f"| {item['stage']} | {item['maximum_execution_seconds']} | {item['maximum_cleanup_seconds']} |"
        for item in budgets["stage_budgets"]
    )
    return f"""# Exact candidate decision proposal

**UNSIGNED / NOT_APPROVED. No selection or effective clock exists.**

Candidate `{candidate.commit}`, tree `{candidate.tree}`, direct parent `{candidate.parent}`.
Reviewed policy package `{package.identity}`.
Policy `{policy.acceptance_policy_identity}`; exception `{policy.exception_identity}`;
plan `{policy.lineage_plan.lineage_plan_identity}`. Exact bytes and predecessor bindings
are listed in PACKAGE-MANIFEST.json. No earlier candidate's approval is transferred.

The issuer must use the registered native Windows Hello flow to evidence their
project role and explicitly adopt prospective replacement policy, campaign and
versioned project-contract scopes. Observed SID, administrator privileges, a
hash, or this unsigned document establishes none of those scopes. No authority
over an unidentified third-party or higher-level policy is asserted.

The exact original pair retains 2571 raw attribute differences
`0x10000010 -> 0x00000010` (`0x10000000` unchanged comparison), equality false,
cause UNRESOLVED, historical Important unresolved, and every old BLOCKED outcome.
Only the registered original acquisitions can qualify for the proposed exception.
New discrepancies and unchanged mandatory predicate failures remain terminal.
{coverage_text}

One lineage and one permanently consumed claim are proposed, with a seven-day
claim window and thirty-day terminal window after authentic effectiveness.
Revocation, expiration, failure and source change do not free or transfer a claim.

| Stage | Proposed execution seconds | Proposed cleanup seconds |
| --- | ---: | ---: |
{rows}

Finalization: {budgets["finalization_maximum_seconds"]} seconds. Complete future
budget: {budgets["total_remaining_budget_seconds"]} seconds, which must fit strictly
before both policy and separate execution-authority expiry. PROPOSED-BUDGETS.json
explains each nonnormative value. Historical caps and individual command maxima
are references only; they are not renewed authority. All 27 mandatory commands,
QCF17 A and B before QCF18, complete inventories, and zero current discrepancies
remain required even if the proposed outer cap expires sooner.

{"A storage binding is retained in OWNER-STORAGE-BINDING.json; it requires fresh native registration/role verification and must match the intended operational instance." if has_storage else "Operational owner enrollment/storage binding remains pending; no observed account or test store is substituted for it."}
ORIGINAL-ACQUISITION-REGISTRY.json binds the exact retained files. A protected
registered ledger must remain empty until separately authorized consumption.
Administrators and SYSTEM are trusted storage administrators; candidate children
use the enforced restricted Windows token. Setup and enrollment approve no policy.

The conditional native consumer validates the complete binding and fresh owner
state before an inert child resumes, retaining ancestry/environment protections.
Real Ticket-07/provider/model startup has no supported live authorization route
in this implementation and remains rejected. The conditional qualification result
itself retains execution_authorized=false and unresolved history. Legacy v1 is
unchanged and cannot receive a coerced conditional result.

The remaining human decision must separately select the evidenced authority
scopes, exact policy/package adoption, and (only if desired) bounded qualification
execution using these exact budgets and a fresh time window. This file selects
none of them. No stage IDs, allocation, claim, real qualification, Ticket-07 packet,
transition receipt or live authorization is produced by this delivery.
"""


def main() -> int:
    if os.name != "nt" or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    ):
        raise ValueError("use an existing Windows interpreter with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "output",
        "predecessor",
        "dependencies",
        "operational-review",
        "enforcement-review",
        "validation",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--setup-bindings", type=Path)
    parser.add_argument(
        "--coverage-v2",
        action="store_true",
        help="Prepare the distinct unsigned coverage contract; no adoption or authority",
    )
    parser.add_argument(
        "--expected-parent",
        default=BASE_COMMIT,
        help="Full authorized direct-parent commit; an unsigned input, never approval transfer",
    )
    args = parser.parse_args()
    sys.path[:0] = [
        str(Path(__file__).resolve().parents[2]),
        str(Path(__file__).resolve().parent),
        str(args.dependencies.resolve(strict=True)),
    ]
    print(json.dumps(materialize(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
