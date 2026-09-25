"""Explicit synthetic native-record boundary; cannot pass native ReadRecords.

Uses real owner validation/storage after an intentionally invalid TEST authority
boundary. No Windows Hello proof, actual enrolled identity or installed path.
"""

import base64
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingagents.policy_owner_contract import (
    OriginalAcquisitionRegistry,
    PolicyDecisionProposal,
    QualificationExecutionProposal,
    RoleProposal,
)
from tradingagents.policy_qualification_lifecycle import LineageLedger
from tradingagents.policy_qualified_contract import (
    BytePin,
    ReviewedPolicyPackage,
    canonical_bytes,
    content_identity,
)
from tradingagents.windows_policy_owner import ImmutableAcquisitionRegistry, LocalWindowsPolicyOwner


class SyntheticNativeBoundary:
    def __init__(self, root, instance="test-20300101-001", *, root_template=None, candidate=None):
        if not instance.startswith("test-"):
            raise ValueError("synthetic authority is TEST-only")
        root = Path(root)
        root.mkdir()
        source = Path(__file__).resolve().parents[3]
        scope_root = root / "SYNTHETIC-POLICY"
        scope_root.mkdir()
        self.now = datetime.now(timezone.utc)
        self.case = case = runpy.run_path(
            str(source / "scripts/policy_owner/predeployment_fixtures.py")
        )["SyntheticCase"](
            scope_root,
            root_template=root_template,
            clock_utc=self.now.isoformat(),
            candidate=candidate,
        )
        store = root / "UNREGISTERED-STORE"
        (store / "state/temp").mkdir(parents=True)
        (store / "artifacts").mkdir()
        (store / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
            b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
        )
        initialize = runpy.run_path(
            str(source / "scripts/policy_owner/prepare_policy_owner_setup.py")
        )["initialize_owner_state"]
        initialize(store / "state/owner.sqlite3", instance, "SYNTHETIC-OWNER-LEDGER")
        # Only test code bypasses this constructor. The production constructor
        # requires exact service token and installed source ancestry, unchanged.
        self.owner = owner = object.__new__(LocalWindowsPolicyOwner)
        owner.root = store
        owner.db_path = store / "state/owner.sqlite3"
        owner.ledger = LineageLedger.provision(
            store / "state/lineage.sqlite3", "SYNTHETIC-OWNER-LEDGER"
        )
        owner.anchor = {
            "instance": instance,
            "operational": False,
            "ledger_identity": "SYNTHETIC-OWNER-LEDGER",
            "operator_sid": "S-1-5-21-101-102-103-1001",
            "storage_binding_identity": "policy-owner-storage-binding:v1:sha256:" + "4" * 64,
        }
        owner.now_utc = self.now.isoformat()
        owner.records = []
        registry = OriginalAcquisitionRegistry.seal(
            contract_version="policy-owner-original-acquisition-registry-v1",
            source_package_manifest_pin=BytePin.of(b"SYNTHETIC PACKAGE ONLY"),
            acquisition_registration_mode="EXACT_RETAINED_ORIGINAL_PAIR_ONLY",
            before=case.policy.scope.before,
            after=case.policy.scope.after,
            complete_delta_ledger=case.policy.scope.complete_delta_ledger,
            original_blocker=case.policy.scope.original_blocker,
            newly_acquired_observations_eligible=False,
        )
        owner.registry = ImmutableAcquisitionRegistry(
            registry.to_bytes(), BytePin.of(registry.to_bytes())
        )
        for (identity, _digest), raw in case.owner.artifacts.items():
            owner.put(raw, identity)
        for digest, raw in case.owner.pinned.items():
            owner.put(raw, "synthetic-procedure-bytes:v1:sha256:" + digest)
        self.role = RoleProposal.seal(
            contract_version="policy-owner-project-role-proposal-v1",
            instance=instance,
            operator_sid=owner.anchor["operator_sid"],
            observed_account_name="SYNTHETIC\\INVALID",
            role="PERSONAL_RESEARCH_CAMPAIGN_ISSUING_OPERATOR",
            authority_basis="SYNTHETIC ONLY, NO HUMAN AUTHENTICATION",
            prospective_project_policy_scope="SYNTHETIC replacement only",
            campaign_specification_scope="SYNTHETIC campaign only",
            project_contract_scope="SYNTHETIC contract only",
            third_party_or_higher_policy_authority_claimed=False,
            exact_package_approved=False,
            qualification_execution_authorized=False,
            live_execution_authorized=False,
        )
        self.apply(
            "ENROLL_PROJECT_ROLE",
            self.role.model_dump(mode="json"),
            at=self.now - timedelta(hours=1),
        )
        package = ReviewedPolicyPackage.seal(
            contract_version="reviewed-policy-contract-package-v1",
            policy=owner.put(case.policy.to_bytes(), case.policy.acceptance_policy_identity),
            scope_evidence_manifest_pin=case.policy.scope_evidence_manifest_pin,
            contract_source_pin=case.policy.lineage_plan.consumer_contract_pin,
        )
        self.policy = case.policy
        proposal = PolicyDecisionProposal.seal(
            contract_version="policy-owner-exact-decision-proposal-v1",
            instance=instance,
            registered_role_identity=self.role.identity,
            storage_binding_identity=owner.anchor["storage_binding_identity"],
            reviewed_package=package,
            policy=case.policy,
            prospective_replacement_scope=self.role.prospective_project_policy_scope,
            campaign_specification_adoption=self.role.campaign_specification_scope,
            project_contract_adoption=self.role.project_contract_scope,
            execution=QualificationExecutionProposal(
                selection="AUTHORIZE_BOUNDED_QUALIFICATION",
                candidate=case.candidate,
                lineage_plan_identity=case.policy.lineage_plan.lineage_plan_identity,
                stage_budgets=case.policy.lineage_plan.stage_budgets,
                finalization_maximum_seconds=case.policy.lineage_plan.finalization_maximum_seconds,
                maximum_lineages=1,
                maximum_claims=1,
                effective_utc=(self.now - timedelta(hours=1)).isoformat(),
                expires_utc=(self.now + timedelta(days=30)).isoformat(),
                ticket07_or_live_authorized=False,
            ),
            selection="APPROVE",
            historical_cause="UNRESOLVED",
            historical_equal=False,
            historical_outcome="BLOCKED",
            result_execution_authorized=False,
            ticket07_or_live_authorized=False,
        )
        response = self.apply(
            "EXACT_POLICY_DECISION",
            proposal.model_dump(mode="json"),
            at=self.now - timedelta(minutes=59),
        )
        from tradingagents.policy_qualified_contract import OperatorApproval

        self.approval = OperatorApproval.read(response["approval"])

    def record(self, purpose, document, *, at=None):
        body = {
            "contract_version": "SYNTHETIC_INVALID_NATIVE_RECORD_NOT_A_PROOF",
            "fixture_only": True,
            "purpose": purpose,
            "document": document,
            "recorded_at_utc": (at or datetime.now(timezone.utc)).isoformat(),
            "credential": {
                "id": base64.b64encode(b"INVALID SYNTHETIC NON-HUMAN CREDENTIAL").decode()
            },
            "predecessor": self.owner.records[-1]["identity"] if self.owner.records else None,
        }
        body["identity"] = content_identity("synthetic-invalid-native-record:v1", body)
        return body

    def apply(self, purpose, document, *, at=None):
        record = self.record(purpose, document, at=at)
        self.owner.records.append(record)
        return self.owner.apply_authenticated_record(record)

    def seed_contract_bytes(self, descriptor, raw):
        """Unit-fixture setup ONLY. Native publication is tested by real native calls elsewhere."""
        import uuid

        transfer = uuid.uuid4().hex
        base = self.owner.root / "state/temp" / ("evidence-import-" + transfer)
        Path(str(base) + ".json").write_bytes(
            canonical_bytes(
                {"transfer_id": transfer, "descriptor": descriptor.model_dump(mode="json")}
            )
        )
        Path(str(base) + ".complete").write_bytes(
            canonical_bytes({"transfer_id": transfer, "descriptor_identity": descriptor.identity})
        )
        (self.owner.root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob")).write_bytes(
            raw
        )
        return {
            "contract_version": "policy-owner-evidence-intake-v2",
            "kind": "artifact-reference",
            "key": descriptor.artifact.identity,
            "value": {"transfer_id": transfer, "descriptor": descriptor.model_dump(mode="json")},
        }
