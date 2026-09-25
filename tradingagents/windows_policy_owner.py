"""Registered Windows service owner and native-client adapter.

The service's protected worker uses LocalWindowsPolicyOwner. Applications use the
registered native entrypoint. All producer operations execute in the owner; an
application never receives a writable ledger or an authentication callback.
"""

from __future__ import annotations

import base64
import ctypes
import os
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from tradingagents.policy_owner_contract import (
    OriginalAcquisitionRegistry,
    PolicyDecisionProposal,
    RoleProposal,
    evidence_intake_entries,
)
from tradingagents.policy_qualification_lifecycle import LineageLedger
from tradingagents.policy_qualified_contract import (
    AcceptancePolicy,
    BoundEvidence,
    BytePin,
    Candidate,
    DownstreamEnvelope,
    EvidenceRef,
    ExecutionEvidence,
    LineageAllocation,
    LineageClaim,
    OperatorApproval,
    PolicyQualifiedExecutionResult,
    PostResultAcceptanceReceipt,
    RawComparison,
    ResultBindingCore,
    StageBudget,
    canonical_bytes,
    content_identity,
    strict_json,
    utc,
)
from tradingagents.policy_qualified_results import (
    AuthorityResolution,
    ObservationResolution,
    PolicyQualifiedVerifier,
)


def require_service_token(service_sid: str) -> None:
    """Check the actual OS token; no input record can assert service membership."""
    if os.name != "nt":
        raise ValueError("registered Windows owner requires Windows")
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    api.OpenProcessToken.restype = wintypes.BOOL
    api.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.GetTokenInformation.restype = wintypes.BOOL
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    api.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(wintypes.HANDLE(-1), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 4096:
            raise ValueError("worker token user is unavailable")
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if text.value != service_sid or not service_sid.startswith("S-1-5-80-"):
                raise ValueError("worker requires the exact dedicated virtual service account")
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _record_reference(value: dict) -> EvidenceRef:
    return EvidenceRef(identity=value["identity"], pin=BytePin.of(canonical_bytes(value)))


class ImmutableAcquisitionRegistry:
    """Load the single externally registered original pair, without capture APIs."""

    def __init__(self, raw: bytes, expected: BytePin) -> None:
        BytePin.read(expected).check(raw)
        self.value = OriginalAcquisitionRegistry.read(raw)

    def original(self, observation_id: str):
        values = (self.value.before, self.value.after)
        matches = [entry for entry in values if entry.observation_id == observation_id]
        if len(matches) != 1:
            raise ValueError("observation is not in the immutable original-acquisition registry")
        return matches[0]


class LocalWindowsPolicyOwner:
    """Concrete TrustedPolicyOwner, usable only by the registered Windows token.

    The native service revalidates signatures and serializes all requests. Its
    protected HKLM anchor pins store object identities and its entire worker
    depot before starting this process. SYSTEM/administrators remain trusted.
    """

    def __init__(self, anchor: dict, records: list[dict], now_utc: str) -> None:
        require_service_token(anchor["service_sid"])
        self.anchor, self.records, self.now_utc = anchor, records, now_utc
        self.root = Path(anchor["root"]).resolve(strict=True)
        if Path(__file__).resolve().parents[1] != self.root / "source":
            raise ValueError("candidate source cannot act as the registered owner worker")
        utc(now_utc)
        raw = (self.root / "original-acquisitions.json").read_bytes()
        self.registry = ImmutableAcquisitionRegistry(
            raw, BytePin(byte_length=len(raw), sha256=anchor["original_registry_sha256"])
        )
        self.ledger = LineageLedger(
            self.root / "state" / "lineage.sqlite3", anchor["ledger_identity"]
        )
        self.db_path = self.root / "state" / "owner.sqlite3"
        with self.connection() as connection, connection:
            floor = connection.execute("SELECT last_clock FROM metadata").fetchone()[0]
            if floor and utc(now_utc) < utc(floor):
                raise ValueError("durable owner clock regressed")
            connection.execute("UPDATE metadata SET last_clock=?", (now_utc,))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        metadata = connection.execute("SELECT instance, ledger_id FROM metadata").fetchall()
        if metadata != [(self.anchor["instance"], self.anchor["ledger_identity"])]:
            connection.close()
            raise ValueError("unregistered owner database")
        try:
            yield connection
        finally:
            connection.close()

    def fresh_time(self) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT last_clock FROM metadata").fetchone()[0]
            if previous and utc(now) < utc(previous):
                raise ValueError("durable owner clock regressed")
            connection.execute("UPDATE metadata SET last_clock=?", (now,))
        self.now_utc = now
        return now

    def _blob(self, pin: BytePin) -> bytes:
        pin = BytePin.read(pin)
        path = self.root / "artifacts" / (pin.sha256 + ".blob")
        status = path.lstat()
        if (
            not path.is_file()
            or path.is_symlink()
            or status.st_nlink != 1
            or (getattr(status, "st_file_attributes", 0) & 0x400)
        ):
            raise ValueError("owner artifact is not a regular unaliased immutable file")
        raw = path.read_bytes()
        pin.check(raw)
        return raw

    def put(self, raw: bytes, identity: str) -> EvidenceRef:
        """Owner-only, create-new bytes plus append-only semantic registration."""
        ref = EvidenceRef(identity=identity, pin=BytePin.of(raw))
        path = self.root / "artifacts" / (ref.pin.sha256 + ".blob")
        if path.exists():
            ref.pin.check(self._blob(ref.pin))
        else:
            temporary = self.root / "state" / "temp" / ("unpublished-" + uuid.uuid4().hex + ".tmp")
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            kernel.MoveFileExW.restype = wintypes.BOOL
            if not kernel.MoveFileExW(str(temporary), str(path), 8):
                error = ctypes.get_last_error()
                if error not in {80, 183}:
                    raise ctypes.WinError(error)
                ref.pin.check(self._blob(ref.pin))
        with self.connection() as connection, connection:
            existing = connection.execute(
                "SELECT reference FROM artifacts WHERE identity=?", (identity,)
            ).fetchone()
            if existing is not None and EvidenceRef.read(existing[0]) != ref:
                raise ValueError("registered artifact identity cannot be rebound")
            connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?, ?)", (identity, ref.to_bytes())
            )
        return ref

    def read_artifact(self, reference: EvidenceRef) -> bytes:
        ref = EvidenceRef.read(reference)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT reference FROM artifacts WHERE identity=?", (ref.identity,)
            ).fetchall()
        if len(rows) != 1 or EvidenceRef.read(rows[0][0]) != ref:
            raise ValueError(
                "artifact is not registered with its exact semantic and byte identities"
            )
        return self._blob(ref.pin)

    def read_pinned_file(self, pin: BytePin) -> bytes:
        # Files are admitted by the immutable setup manifest or a separately
        # authenticated evidence intake; a matching arbitrary filesystem path is
        # not searched or imported on demand.
        with self.connection() as connection:
            rows = connection.execute("SELECT reference FROM artifacts").fetchall()
        if not any(EvidenceRef.read(row[0]).pin == pin for row in rows):
            raise ValueError("contract/source file is not registered")
        return self._blob(pin)

    def original_acquisition(self, observation_id: str):
        return self.registry.original(observation_id)

    def _intakes(self) -> Iterator[dict]:
        for record in self.records:
            if record["purpose"] == "REGISTER_EVIDENCE":
                yield from evidence_intake_entries(record["document"])

    def _registration(self, kind: str, key: str) -> dict:
        matches = [
            entry["value"]
            for entry in self._intakes()
            if entry["kind"] == kind and entry["key"] == key
        ]
        if len(matches) != 1:
            raise ValueError("missing, duplicate or conflicting authenticated owner registration")
        return dict(matches[0])

    def canonical_allocation(self, lineage_plan_identity: str) -> LineageAllocation:
        return LineageAllocation.read(self._registration("allocation", lineage_plan_identity))

    def observation(self, observation_id: str) -> ObservationResolution:
        if observation_id in {
            self.registry.value.before.observation_id,
            self.registry.value.after.observation_id,
        }:
            raise ValueError("original acquisition cannot impersonate a new observation")
        value = dict(self._registration("observation", observation_id))
        if set(value) != set(ObservationResolution.__dataclass_fields__):
            raise ValueError("new observation registration has an unknown field set")
        value["artifact"] = EvidenceRef.read(value["artifact"])
        value["measurement_contract_pin"] = BytePin.read(value["measurement_contract_pin"])
        return ObservationResolution(**value)

    def executed_stage(self, lineage_identity: str, kind: str) -> bytes:
        value = self._registration("executed-stage", lineage_identity + "|" + kind)
        return self.read_artifact(EvidenceRef.read(value))

    def review_principal(self, reference: EvidenceRef) -> str:
        value = self._registration("review-provenance", reference.identity)
        if set(value) != {"reference", "reviewer_principal", "source_session_evidence"} or (
            EvidenceRef.read(value["reference"]) != reference
        ):
            raise ValueError("review provenance does not bind this exact review artifact")
        self.read_artifact(EvidenceRef.read(value["source_session_evidence"]))
        return value["reviewer_principal"]

    def _root(self, lineage: str, role: str) -> Path:
        value = self._registration("roots", lineage)
        if set(value) != {"guest_export", "source_input", "runtime_bundle"}:
            raise ValueError("qualification roots are not completely registered")
        return Path(value[role])

    def guest_export_root(self, lineage_identity: str) -> Path:
        return self._root(lineage_identity, "guest_export")

    def source_input_root(self, lineage_identity: str) -> Path:
        return self._root(lineage_identity, "source_input")

    def runtime_bundle_root(self, lineage_identity: str) -> Path:
        return self._root(lineage_identity, "runtime_bundle")

    def _role(self) -> tuple[RoleProposal, dict]:
        if not self.records or self.records[0]["purpose"] != "ENROLL_PROJECT_ROLE":
            raise ValueError("no authenticated project-role enrollment exists")
        record = self.records[0]
        role = RoleProposal.read(record["document"])
        if (
            role.instance != self.anchor["instance"]
            or role.operator_sid != self.anchor["operator_sid"]
        ):
            raise ValueError("enrolled project role does not match the registered operator")
        return role, record

    def _decision(
        self, approval_identity: str
    ) -> tuple[OperatorApproval, PolicyDecisionProposal, dict]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT approval, authentication_identity FROM approvals WHERE identity=?",
                (approval_identity,),
            ).fetchone()
        if row is None:
            raise ValueError("exact package decision is missing or unregistered")
        approval = OperatorApproval.read(row[0])
        records = [record for record in self.records if record["identity"] == row[1]]
        if len(records) != 1 or records[0]["purpose"] != "EXACT_POLICY_DECISION":
            raise ValueError("approval has no freshly verified Windows authentication record")
        proposal = PolicyDecisionProposal.read(records[0]["document"])
        role, _ = self._role()
        if (
            proposal.selection != "APPROVE"
            or proposal.registered_role_identity != role.identity
            or (proposal.storage_binding_identity != self.anchor["storage_binding_identity"])
        ):
            raise ValueError("wrong project role, scope, storage or rejected human decision")
        if (
            proposal.prospective_replacement_scope != role.prospective_project_policy_scope
            or proposal.campaign_specification_adoption != role.campaign_specification_scope
            or proposal.project_contract_adoption != role.project_contract_scope
        ):
            raise ValueError("decision exceeds the enrolled project authority scopes")
        return approval, proposal, records[0]

    def _authority_artifact(self, record: dict, scope: str, body: dict) -> EvidenceRef:
        payload = {
            "contract_version": "policy-owner-authenticated-authority-v1",
            "authentication": _record_reference(record).model_dump(mode="json"),
            "scope": scope,
            "body": body,
        }
        payload["identity"] = content_identity("policy-owner-authenticated-authority:v1", payload)
        return self.put(canonical_bytes(payload), payload["identity"])

    def resolve(self, approval_identity: str) -> AuthorityResolution:
        self.fresh_time()
        approval, proposal, decision = self._decision(approval_identity)
        role, enrollment = self._role()
        self.registry.value.validate_policy(proposal.policy)
        revoked = any(
            record["purpose"] == "REVOKE_DECISION"
            and record["document"].get("approval_identity") == approval_identity
            for record in self.records
        )
        if revoked or utc(self.now_utc) >= utc(approval.expires_utc):
            self.ledger.invalidate(
                approval_identity,
                approval.lineage_plan_identity,
                reason="REVOKED" if revoked else "EXPIRED",
                at=self.now_utc,
            )
        sources = [entry["value"] for entry in self._intakes() if entry["kind"] == "current-source"]
        if not sources:
            raise ValueError("no current source is registered")
        current = sources[-1]
        if set(current) != {"candidate", "lineage_plan_identity", "production_inventory_identity"}:
            raise ValueError("current verified source registration is incomplete")
        execution = proposal.execution
        permissions = [
            self._authority_artifact(
                decision,
                "prospective-project-policy-replacement",
                {"scope": proposal.prospective_replacement_scope},
            ),
            self._authority_artifact(
                decision,
                "campaign-specification-adoption",
                {"scope": proposal.campaign_specification_adoption},
            ),
            self._authority_artifact(
                decision, "project-contract-adoption", {"scope": proposal.project_contract_adoption}
            ),
            self._authority_artifact(
                decision,
                "separate-bounded-qualification-execution",
                execution.model_dump(mode="json"),
            ),
        ]
        return AuthorityResolution(
            approval_bytes=approval.to_bytes(),
            authenticated_principal=approval.issuer_principal,
            authenticated_channel_session=decision["identity"],
            decision_artifact_bytes=canonical_bytes(decision),
            issuer_authority_bytes=canonical_bytes(enrollment),
            reviewed_package_manifest_bytes=proposal.reviewed_package.to_bytes(),
            originating_policy_disposition="PERMITTED_BY_PROSPECTIVE_REPLACEMENT",
            originating_or_replacement_authority=permissions[0],
            campaign_replacement_authority=permissions[1],
            project_contract_authority=permissions[2],
            qualification_execution_authority=permissions[3],
            execution_candidate=execution.candidate,
            execution_lineage_plan_identity=execution.lineage_plan_identity,
            execution_effective_utc=execution.effective_utc,
            execution_expires_utc=execution.expires_utc,
            execution_stage_budgets=execution.stage_budgets,
            execution_finalization_maximum_seconds=execution.finalization_maximum_seconds,
            approved_policy_identity=proposal.policy.acceptance_policy_identity,
            current_candidate=Candidate.read(current["candidate"]),
            current_lineage_plan_identity=current["lineage_plan_identity"],
            current_production_inventory_identity=current["production_inventory_identity"],
            ledger_id=self.anchor["ledger_identity"],
            ledger_path=self.ledger.path,
            state="REVOKED" if revoked else "ACTIVE",
            state_reference=content_identity(
                "policy-owner-fresh-state:v1",
                {
                    "record": decision["identity"],
                    "last_record": self.records[-1]["identity"],
                    "now_utc": self.now_utc,
                    "storage": self.anchor["storage_binding_identity"],
                },
            ),
            now_utc=self.now_utc,
        )

    def validate_authentication_document(self, purpose: str, document: dict) -> None:
        if purpose == "ENROLL_PROJECT_ROLE":
            role = RoleProposal.read(document)
            if (
                self.records
                or role.instance != self.anchor["instance"]
                or role.operator_sid != self.anchor["operator_sid"]
            ):
                raise ValueError("invalid or repeated project-role enrollment")
            return
        role, _ = self._role()
        if purpose == "EXACT_POLICY_DECISION":
            proposal = PolicyDecisionProposal.read(document)
            if (
                proposal.instance != self.anchor["instance"]
                or proposal.registered_role_identity != role.identity
                or (proposal.storage_binding_identity != self.anchor["storage_binding_identity"])
            ):
                raise ValueError("decision proposal has wrong issuer, role or registered storage")
            if (
                proposal.prospective_replacement_scope != role.prospective_project_policy_scope
                or proposal.campaign_specification_adoption != role.campaign_specification_scope
                or proposal.project_contract_adoption != role.project_contract_scope
            ):
                raise ValueError(
                    "exact decision exceeds the human-enrolled project authority scopes"
                )
            self.registry.value.validate_policy(proposal.policy)
            self.read_artifact(proposal.reviewed_package.policy)
        elif purpose == "REVOKE_DECISION":
            if (
                set(document) != {"contract_version", "approval_identity", "reason"}
                or document["contract_version"] != "policy-owner-revocation-v1"
                or not document["reason"]
            ):
                raise ValueError("invalid exact revocation document")
            self._decision(document["approval_identity"])
        elif purpose == "REGISTER_EVIDENCE":
            for entry in evidence_intake_entries(document):
                self._validate_intake(entry)
        else:
            raise ValueError("unsupported owner authentication purpose")

    def _validate_intake(self, document: dict) -> None:
        kind, key, value = document["kind"], document["key"], document["value"]
        if (
            kind
            not in {
                "artifact",
                "allocation",
                "observation",
                "executed-stage",
                "review-provenance",
                "roots",
                "current-source",
                "native-payload",
                "inert-launch",
            }
            or not key
        ):
            raise ValueError("unknown acquisition/registration kind or empty key")
        if kind == "observation" and (
            key
            in {
                self.registry.value.before.observation_id,
                self.registry.value.after.observation_id,
            }
            or value.get("origin") != "NEW_QUALIFICATION"
        ):
            raise ValueError("new observations cannot be enrolled as retained originals")
        if any(entry["kind"] == kind and entry["key"] == key for entry in self._intakes()):
            raise ValueError("immutable owner registration already exists")
        if kind == "artifact":
            if set(value) != {"reference", "bytes_base64"}:
                raise ValueError("artifact intake must include exact bytes and reference")
            ref = EvidenceRef.read(value["reference"])
            if key != ref.identity:
                raise ValueError("artifact intake key differs from its exact reference")
            ref.pin.check(base64.b64decode(value["bytes_base64"], validate=True))
            with self.connection() as connection:
                existing = connection.execute(
                    "SELECT reference FROM artifacts WHERE identity=?", (ref.identity,)
                ).fetchone()
            if existing is not None and EvidenceRef.read(existing[0]) != ref:
                raise ValueError(
                    "authenticated intake cannot rebind an existing setup or owner artifact"
                )
        elif kind == "observation":
            if (
                set(value) != set(ObservationResolution.__dataclass_fields__)
                or value["observation_id"] != key
                or value["phase"] not in {"before", "after"}
            ):
                raise ValueError("new observation acquisition binding is incomplete")
            EvidenceRef.read(value["artifact"])
            BytePin.read(value["measurement_contract_pin"])
            utc(value["observed_at_utc"])
            for name in ("lineage_plan_identity", "allocation_identity", "stage"):
                if not isinstance(value[name], str) or not value[name]:
                    raise ValueError("new observation has an empty acquisition field")
        elif kind == "current-source":
            if set(value) != {
                "candidate",
                "lineage_plan_identity",
                "production_inventory_identity",
            }:
                raise ValueError("current source intake is incomplete")
            Candidate.read(value["candidate"])
            if not all(
                isinstance(value[name], str) and value[name]
                for name in ("lineage_plan_identity", "production_inventory_identity")
            ):
                raise ValueError("current source intake lacks exact plan/inventory identities")
        elif kind == "executed-stage":
            EvidenceRef.read(value)
            if "|" not in key:
                raise ValueError("executed stage registration lacks lineage and stage")
        elif kind == "review-provenance":
            if (
                set(value) != {"reference", "reviewer_principal", "source_session_evidence"}
                or EvidenceRef.read(value["reference"]).identity != key
            ):
                raise ValueError("review provenance scope is incomplete")
            EvidenceRef.read(value["source_session_evidence"])
            if not isinstance(value["reviewer_principal"], str) or not value["reviewer_principal"]:
                raise ValueError("review principal evidence is missing")
        elif kind == "roots":
            from tradingagents.runtime_protection import _windows_path

            if set(value) != {"guest_export", "source_input", "runtime_bundle"}:
                raise ValueError("qualification roots are incomplete")
            for path in value.values():
                _windows_path(path)
        elif kind == "allocation":
            allocation = LineageAllocation.read(value)
            if key != allocation.lineage_plan.lineage_plan_identity:
                raise ValueError("canonical allocation plan mismatch")
            approval, proposal, _ = self._decision(allocation.approval_identity)
            if (
                allocation.candidate != approval.candidate
                or allocation.lineage_plan != proposal.policy.lineage_plan
            ):
                raise ValueError("allocation is not for the exact approved source and plan")
        elif kind == "native-payload":
            from tradingagents.policy_qualified_native import NativePayloadProvenance

            provenance = NativePayloadProvenance.read(value)
            if provenance.preparation_lineage_identity != key:
                raise ValueError("native payload is registered to a different preparation lineage")
        elif kind == "inert-launch":
            from tradingagents.policy_qualified_native import InertFixtureLaunch

            grant = InertFixtureLaunch.read(value)
            if (
                self.anchor["operational"]
                or grant.instance != self.anchor["instance"]
                or grant.attempt_identity != key
            ):
                raise ValueError(
                    "inert fixture authorization is limited to its isolated test owner and exact attempt"
                )

    def _apply_record_effects(self, record: dict) -> dict:
        if not self.records or record != self.records[-1]:
            raise ValueError("caller cannot insert its own authentication record")
        purpose, document = record["purpose"], record["document"]
        records = self.records
        try:
            self.records = records[:-1]
            self.validate_authentication_document(purpose, document)
        finally:
            self.records = records
        self.put(canonical_bytes(record), record["identity"])
        if purpose == "EXACT_POLICY_DECISION":
            proposal = PolicyDecisionProposal.read(document)
            if proposal.selection == "REJECT":
                return {
                    "status": "REJECTED",
                    "authentication_identity": record["identity"],
                    "execution_authorized": False,
                }
            role, enrollment = self._role()
            effective = utc(record["recorded_at_utc"])
            policy = proposal.policy
            approval = OperatorApproval.seal(
                contract_version="historical-state-operator-approval-v1",
                acceptance_policy_identity=policy.acceptance_policy_identity,
                exception_identity=policy.exception_identity,
                lineage_plan_identity=policy.lineage_plan.lineage_plan_identity,
                candidate=policy.scope.candidate,
                scope_evidence_manifest_pin=policy.scope_evidence_manifest_pin,
                reviewed_package_manifest_pin=BytePin.of(proposal.reviewed_package.to_bytes()),
                issuer_principal="windows-webauthn:"
                + role.operator_sid
                + ":"
                + self.anchor["instance"]
                + ":"
                + sha256(
                    base64.b64decode(enrollment["credential"]["id"], validate=True)
                ).hexdigest(),
                issuer_authority_pin=BytePin.of(canonical_bytes(enrollment)),
                decision_artifact_pin=BytePin.of(canonical_bytes(record)),
                channel_session_reference=record["identity"],
                selection="APPROVE",
                effective_utc=effective.isoformat(),
                claim_by_utc=(effective + timedelta(days=7)).isoformat(),
                expires_utc=(effective + timedelta(days=30)).isoformat(),
            )
            with self.connection() as connection, connection:
                expected = (
                    policy.acceptance_policy_identity,
                    approval.to_bytes(),
                    record["identity"],
                )
                existing = connection.execute(
                    "SELECT policy, approval, authentication_identity FROM approvals WHERE identity=?",
                    (approval.approval_identity,),
                ).fetchone()
                if existing is not None and existing != expected:
                    raise ValueError(
                        "exact authenticated approval derivation conflicts with retained state"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO approvals VALUES (?, ?, ?, ?)",
                    (
                        approval.approval_identity,
                        policy.acceptance_policy_identity,
                        approval.to_bytes(),
                        record["identity"],
                    ),
                )
            self.put(approval.to_bytes(), approval.approval_identity)
            self.put(proposal.reviewed_package.to_bytes(), proposal.reviewed_package.identity)
            return {
                "status": "AUTHENTICATED_EXACT_DECISION_RECORDED",
                "approval": approval.model_dump(mode="json"),
                "execution_authorized": False,
            }
        if purpose == "REGISTER_EVIDENCE":
            for entry in evidence_intake_entries(document):
                self._apply_intake(entry)
        elif purpose == "REVOKE_DECISION":
            approval, _, _ = self._decision(document["approval_identity"])
            self.ledger.invalidate(
                approval.approval_identity,
                approval.lineage_plan_identity,
                reason="REVOKED",
                at=self.now_utc,
            )
        return {
            "status": "AUTHENTICATED_OWNER_RECORD_RETAINED",
            "authentication_identity": record["identity"],
            "execution_authorized": False,
        }

    def _apply_intake(self, document: dict) -> None:
        if document["kind"] == "artifact":
            value = document["value"]
            ref = EvidenceRef.read(value["reference"])
            self.put(base64.b64decode(value["bytes_base64"], validate=True), ref.identity)
        elif document["kind"] == "current-source":
            current = document["value"]
            with self.connection() as connection:
                approvals = connection.execute("SELECT approval FROM approvals").fetchall()
            for row in approvals:
                approval = OperatorApproval.read(row[0])
                if (
                    approval.candidate != Candidate.read(current["candidate"])
                    or approval.lineage_plan_identity != current["lineage_plan_identity"]
                ):
                    self.ledger.invalidate(
                        approval.approval_identity,
                        approval.lineage_plan_identity,
                        reason="REVOKED",
                        at=self.now_utc,
                    )

    def apply_authenticated_record(self, record: dict) -> dict:
        if not self.records or record != self.records[-1]:
            raise ValueError("caller cannot insert its own authentication record")
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT response FROM applied_records WHERE identity=?", (record["identity"],)
            ).fetchone()
        if existing is not None:
            return strict_json(existing[0])
        response = self._apply_record_effects(record)
        with self.connection() as connection, connection:
            connection.execute(
                "INSERT INTO applied_records VALUES (?, ?)",
                (record["identity"], canonical_bytes(response)),
            )
        return response

    def reconcile_authenticated_records(self) -> None:
        # Resume only idempotent derivation from retained human proofs. Never
        # retry a claim, stage, terminal result or failed qualification.
        records = self.records
        try:
            for index, record in enumerate(records):
                self.records = records[: index + 1]
                self.apply_authenticated_record(record)
        finally:
            self.records = records


def encode_resolution(value: AuthorityResolution) -> dict:
    result = asdict(value)
    for key in (
        "approval_bytes",
        "decision_artifact_bytes",
        "issuer_authority_bytes",
        "reviewed_package_manifest_bytes",
    ):
        result[key] = base64.b64encode(getattr(value, key)).decode("ascii")
    for key in (
        "originating_or_replacement_authority",
        "campaign_replacement_authority",
        "project_contract_authority",
        "qualification_execution_authority",
        "execution_candidate",
        "current_candidate",
    ):
        result[key] = getattr(value, key).model_dump(mode="json")
    result["execution_stage_budgets"] = [
        item.model_dump(mode="json") for item in value.execution_stage_budgets
    ]
    result["ledger_path"] = str(value.ledger_path)
    return result


def decode_resolution(value: dict) -> AuthorityResolution:
    value = dict(value)
    if set(value) != set(AuthorityResolution.__dataclass_fields__):
        raise ValueError("unknown native authority resolution shape")
    for key in (
        "approval_bytes",
        "decision_artifact_bytes",
        "issuer_authority_bytes",
        "reviewed_package_manifest_bytes",
    ):
        value[key] = base64.b64decode(value[key], validate=True)
    for key in (
        "originating_or_replacement_authority",
        "campaign_replacement_authority",
        "project_contract_authority",
        "qualification_execution_authority",
    ):
        value[key] = EvidenceRef.read(value[key])
    for key in ("execution_candidate", "current_candidate"):
        value[key] = Candidate.read(value[key])
    value["execution_stage_budgets"] = tuple(
        StageBudget.read(item) for item in value["execution_stage_budgets"]
    )
    value["ledger_path"] = Path(value["ledger_path"])
    return AuthorityResolution(**value)


class RegisteredWindowsPolicyVerifier(PolicyQualifiedVerifier):
    """The application's native IPC adapter; ledger mutation stays in the owner."""

    def __init__(self, instance: str) -> None:
        super().__init__()
        import re
        import winreg

        if not re.fullmatch(r"(?:operational|test)-[0-9]{8}-[0-9]{3}", instance):
            raise ValueError("invalid registered Windows owner instance")
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            "SOFTWARE\\TradingAgents\\PolicyOwner\\" + instance,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            anchor = strict_json(winreg.QueryValueEx(key, "AnchorJson")[0])
        self.instance, self.executable = instance, Path(anchor["native_path"])
        self.pin = anchor["native_sha256"]

    def call(self, operation: str, payload: dict) -> dict:
        # The native client independently validates HKLM ACLs, immutable store
        # registration and the connected dedicated virtual service account process before
        # it sends anything. Python verification is not native pre-start trust.
        if sha256(self.executable.read_bytes()).hexdigest() != self.pin:
            raise ValueError("registered native owner client changed")
        completed = subprocess.run(
            [str(self.executable), "request", self.instance],
            input=canonical_bytes({"operation": operation, "payload": payload}),
            capture_output=True,
            check=False,
            timeout=65,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise ValueError(
                "registered native owner rejected request: "
                + completed.stderr.decode("utf-8", "replace")
            )
        return strict_json(completed.stdout)

    def verify_downstream(self, value: object) -> DownstreamEnvelope:
        envelope = DownstreamEnvelope.read(value)
        result = self.call("verify-envelope", {"envelope": envelope.model_dump(mode="json")})
        verified = DownstreamEnvelope.read(result["envelope"])
        if verified != envelope:
            raise ValueError("native owner changed the complete conditional envelope")
        return verified

    def current_source(self, envelope: object) -> tuple[Candidate, str]:
        value = DownstreamEnvelope.read(envelope)
        result = self.call("verify-envelope", {"envelope": value.model_dump(mode="json")})
        if DownstreamEnvelope.read(result["envelope"]) != value:
            raise ValueError("current-source response lost the conditional envelope")
        return Candidate.read(result["current_candidate"]), result[
            "current_production_inventory_identity"
        ]

    def report(self, envelope: object) -> dict:
        value = self.verify_downstream(envelope)
        return {
            "status": "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY",
            "acceptance": value.model_dump(mode="json"),
            "execution_authorized": False,
        }

    def eligibility(self, policy: object, approval: object) -> AuthorityResolution:
        result = self.call(
            "eligibility",
            {
                "policy": AcceptancePolicy.read(policy).model_dump(mode="json"),
                "approval": OperatorApproval.read(approval).model_dump(mode="json"),
            },
        )
        return decode_resolution(result["authority"])

    def claim(
        self, policy: object, approval: object, allocation: object, *, new_start_bridge: object
    ) -> LineageClaim:
        payload = {
            "policy": AcceptancePolicy.read(policy).model_dump(mode="json"),
            "approval": OperatorApproval.read(approval).model_dump(mode="json"),
            "allocation": LineageAllocation.read(allocation).model_dump(mode="json"),
            "new_start_bridge": RawComparison.read(new_start_bridge).model_dump(mode="json"),
        }
        return LineageClaim.read(self.call("claim", payload)["claim"])

    def begin_stage(
        self,
        policy: object,
        approval: object,
        claim: object,
        *,
        stage: str,
        new_start_bridge: object,
    ) -> str:
        return self.call(
            "begin-stage",
            {
                "policy": AcceptancePolicy.read(policy).model_dump(mode="json"),
                "approval": OperatorApproval.read(approval).model_dump(mode="json"),
                "claim": LineageClaim.read(claim).model_dump(mode="json"),
                "stage": stage,
                "new_start_bridge": RawComparison.read(new_start_bridge).model_dump(mode="json"),
            },
        )["event_identity"]

    def complete_stage(
        self,
        policy: object,
        approval: object,
        claim: object,
        *,
        stage: str,
        evidence: BoundEvidence,
    ) -> str:
        return self.call(
            "complete-stage",
            {
                "policy": AcceptancePolicy.read(policy).model_dump(mode="json"),
                "approval": OperatorApproval.read(approval).model_dump(mode="json"),
                "claim": LineageClaim.read(claim).model_dump(mode="json"),
                "stage": stage,
                "evidence": BoundEvidence.read(evidence).model_dump(mode="json"),
            },
        )["event_identity"]

    def finalize(
        self, *, policy: object, approval: object, claim: object, execution: object
    ) -> PolicyQualifiedExecutionResult:
        result = self.call(
            "finalize",
            {
                "policy": AcceptancePolicy.read(policy).model_dump(mode="json"),
                "approval": OperatorApproval.read(approval).model_dump(mode="json"),
                "claim": LineageClaim.read(claim).model_dump(mode="json"),
                "execution": ExecutionEvidence.read(execution).model_dump(mode="json"),
            },
        )
        return PolicyQualifiedExecutionResult.read(result["result"])

    def verify_result(self, raw: bytes):
        result = self.call(
            "verify-result", {"result_base64": base64.b64encode(raw).decode("ascii")}
        )
        return (
            PolicyQualifiedExecutionResult.read(result["result"]),
            ResultBindingCore.read(result["core"]),
            decode_resolution(result["authority"]),
        )

    def issue_receipt(self, result_bytes: bytes) -> PostResultAcceptanceReceipt:
        value = self.call(
            "issue-receipt", {"result_base64": base64.b64encode(result_bytes).decode("ascii")}
        )
        return PostResultAcceptanceReceipt.read(value["receipt"])
