"""ENROLLMENT_ONLY admission-v2, with closed durable state and no v1 fallback.

Only the existing owner database receives new control tables and append-only
enrollment rows. Ledger copies are read-only; no ledger writer, reconciliation,
policy or qualification dependency is imported. A platform authority must retain
the same exclusive physical lease across all short SQLite transactions and the
human ceremony. JSON cannot manufacture that capability.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingagents.policy_owner_admission_crypto import RP_ID, b64, canonical, verify_proof

BUSINESS_CANDIDATE = "33337b24b5aa07d11c5b9c29485f8fb3604fc2e9"
ROLE_PIN = {"byte_length": 1912, "sha256": "4bb794118689e9d8d5090c5d6ab16dec8173203c98173f38e63e94d7bc22d70c"}
ROLE_IDENTITY = "policy-owner-project-role-proposal:v1:sha256:6dd4906ba66a270f74b6aad7f1ec95dc6239ee50339bb61c061971dbe8a060a0"
SCHEMA = "policy-owner-admission-control-v2"
RECORD_NAMESPACE = "policy-owner-authenticated-human-record:v2"
MAX_BYTES = 64 * 1024 * 1024
ACTIVE_PHASES = {"RESERVED", "CREATION_BEGUN", "PUBLICATION_INTENT", "RECORD_PUBLISHED",
                 "BLOB_PUBLISHED", "INDEXED", "APPLIED"}
PHASES = ACTIVE_PHASES | {"CLOSED", "COMPLETE_STOP", "BLOCKED"}
MIGRATION_PHASES = ("GUARD_ESTABLISHED", "FRESH_KERNEL_BOOT_OBSERVED", "LEGACY_STATE_CLASSIFIED",
                    "SCHEMA_MIGRATED_CLOSED", "NEW_BINDING_STAGED", "V2_ACTIVATED_CLOSED")
FAULT_POINTS = (
    "migration_before_schema", "migration_after_schema", "migration_after_binding",
    "migration_after_activation", "reserve_before_commit", "reserve_after_commit",
    "handoff_before_commit", "handoff_after_commit", "block_before_commit", "block_after_commit",
    "creation_before_commit", "creation_after_commit", "intent_before_commit", "intent_after_commit",
    "authentication_before_publish", "authentication_after_temp_flush", "authentication_after_atomic_publish",
    "authentication_after_publish", "record_after_commit",
    "blob_before_publish", "blob_after_temp_flush", "blob_after_atomic_publish", "blob_after_publish", "blob_phase_after_commit",
    "index_before_commit", "index_after_commit", "applied_before_commit", "applied_after_commit",
    "complete_before_commit", "complete_after_commit",
)
_CLASSIFICATION_ISSUER = object()
_SYNTHETIC_LEASES: dict[tuple[str, str], SyntheticAuthority] = {}


class AdmissionRejected(ValueError):
    pass


class PlatformValidationRequired(AdmissionRejected):
    pass


class InjectedCrash(BaseException):
    """Synthetic persistence-boundary fault; never admitted in production."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise AdmissionRejected(message)


def closed(value: object, fields: str, label: str) -> dict:
    require(type(value) is dict and set(value) == set(fields.split()), f"{label}: closed fields")
    return value


def strict_json(raw: bytes, *, canonical_required: bool = False) -> dict:
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "bounded JSON bytes required")

    def unique(items: list) -> dict:
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def invalid(value: str) -> None:
        raise AdmissionRejected(f"nonfinite JSON: {value}")

    value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=unique, parse_constant=invalid)
    require(type(value) is dict, "JSON object required")
    if canonical_required:
        require(raw == canonical(value), "canonical bytes required")
    return value


def pin(raw: bytes) -> dict:
    return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _blob(value: object) -> bytes:
    require(type(value) is bytes and len(value) <= MAX_BYTES, "bounded SQLite BLOB required")
    return value


def validate_pin(value: object) -> dict:
    value = closed(value, "byte_length sha256", "pin")
    require(type(value["byte_length"]) is int and 0 <= value["byte_length"] <= 2**40, "pin length")
    require(type(value["sha256"]) is str and re.fullmatch("[0-9a-f]{64}", value["sha256"]) is not None, "pin digest")
    return value


def identity(namespace: str, value: object) -> str:
    return namespace + ":sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def seal(namespace: str, value: dict) -> dict:
    require("identity" not in value, "cannot reseal an existing identity")
    return {**value, "identity": identity(namespace, value)}


def utc(value: object) -> datetime:
    require(type(value) is str and len(value) <= 40, "UTC text required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "UTC offset required")
    return parsed


def integer(value: object, maximum: int = 2**63 - 1) -> int:
    require(type(value) is int and 0 <= value <= maximum, "bounded integer required")
    return value


def _read(path: Path, maximum: int = MAX_BYTES) -> bytes:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            not getattr(info, "st_file_attributes", 0) & 0x400, "regular unaliased file required")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "file capacity exceeded")
    return raw


def _artifact_pin(path: Path) -> dict:
    """Large fixed v1 artifacts are hashed in bounded memory, not truncated."""
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            not getattr(info, "st_file_attributes", 0) & 0x400 and info.st_size <= 256 * 1024 * 1024,
            "bounded regular immutable artifact required")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while True:
            raw = stream.read(65536)
            if not raw:
                break
            total += len(raw)
            require(total <= 256 * 1024 * 1024, "artifact capacity exceeded")
            digest.update(raw)
    require(total == info.st_size, "artifact changed while reading copy")
    return {"byte_length": total, "sha256": digest.hexdigest()}


def validate_role(raw: bytes) -> dict:
    require(pin(raw) == ROLE_PIN, "original business role bytes changed")
    role = strict_json(raw, canonical_required=True)
    require(role.get("identity") == ROLE_IDENTITY and role["identity"] == identity(
        "policy-owner-project-role-proposal:v1", {k: v for k, v in role.items() if k != "identity"}),
        "original business role identity")
    return role


def make_binding(legacy_setup_binding: dict, implementation: dict) -> dict:
    closed(legacy_setup_binding, "administrators_and_system_are_trusted candidate_has_storage_write_access contract_version execution_authorized identity instance ledger_identity ledger_object_id ledger_path native_owner_pin operational operator_sid original_registry_pin owner_root owner_root_descriptor_pin owner_root_object_id owner_state_object_id owner_state_path registration_is_approval service_name service_sid trusted_worker_manifest_pin", "immutable legacy setup binding")
    require(legacy_setup_binding["contract_version"] == "policy-owner-storage-binding-v1" and
            legacy_setup_binding["identity"] == identity("policy-owner-storage-binding:v1",
                {k: v for k, v in legacy_setup_binding.items() if k != "identity"}), "legacy binding content identity")
    for field in ("candidate_has_storage_write_access", "execution_authorized", "registration_is_approval"):
        require(legacy_setup_binding[field] is False, "legacy registration is not authority")
    require(legacy_setup_binding["administrators_and_system_are_trusted"] is True and
            legacy_setup_binding["operational"] is True, "legacy trust/model identity")
    for field in ("native_owner_pin", "original_registry_pin", "owner_root_descriptor_pin", "trusted_worker_manifest_pin"):
        validate_pin(legacy_setup_binding[field])
    require(type(legacy_setup_binding["owner_root"]) is str and
            type(legacy_setup_binding["service_sid"]) is str and legacy_setup_binding["service_sid"].startswith("S-1-5-80-"),
            "legacy root and dedicated service SID")
    implementation = closed(implementation, "source_commit native_pin worker_manifest_pin build_id", "implementation")
    require(type(implementation["source_commit"]) is str and re.fullmatch("[0-9a-f]{40}", implementation["source_commit"]),
            "build-supplied implementation commit required")
    require(type(implementation["build_id"]) is str and
            re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]{0,127}", implementation["build_id"]), "build identity")
    validate_pin(implementation["native_pin"])
    validate_pin(implementation["worker_manifest_pin"])
    preserved = {key: legacy_setup_binding[legacy] for key, legacy in (
        ("root_object_id", "owner_root_object_id"), ("owner_state_object_id", "owner_state_object_id"),
        ("ledger_object_id", "ledger_object_id"))}
    return seal("policy-owner-admission-binding:v2", {
        "contract_version": "policy-owner-admission-binding-v2", "mode": "ENROLLMENT_ONLY",
        "legacy_setup_binding": strict_json(canonical(legacy_setup_binding)),
        "implementation": strict_json(canonical(implementation)),
        "business": {"candidate": BUSINESS_CANDIDATE, "role_pin": dict(ROLE_PIN), "role_identity": ROLE_IDENTITY},
        "preserved_objects": preserved, "control_schema": SCHEMA,
    })


def validate_binding(value: dict) -> dict:
    closed(value, "contract_version mode legacy_setup_binding implementation business preserved_objects control_schema identity", "v2 binding")
    require(value == make_binding(value["legacy_setup_binding"], value["implementation"]), "v2 binding identity/fields mismatch")
    return value


def frontend_value(value: dict, binding: dict) -> dict:
    closed(value, "pid creation_filetime image_pin", "frontend")
    require(integer(value["pid"], 0xFFFFFFFF) > 0 and integer(value["creation_filetime"], 2**64 - 1) > 0,
            "actual frontend creation instance required")
    require(validate_pin(value["image_pin"]) == binding["implementation"]["native_pin"], "frontend implementation pin mismatch")
    return value


def validate_v2_record(record: dict, binding: dict, role_raw: bytes, *,
                       migration_cut_identity: str, classified_state_identity: str) -> dict:
    closed(record, "contract_version mode purpose instance operator_sid document legacy_binding_identity binding_identity challenge challenge_document credential proof authenticator_counter recorded_at_utc predecessor frontend creation_receipt credential_generation execution_authorized identity", "v2 record")
    legacy = binding["legacy_setup_binding"]
    expected = {"contract_version": "policy-owner-authenticated-human-record-v2", "mode": "ENROLLMENT_ONLY",
                "purpose": "ENROLL_PROJECT_ROLE", "instance": legacy["instance"], "operator_sid": legacy["operator_sid"],
                "legacy_binding_identity": legacy["identity"], "binding_identity": binding["identity"],
                "predecessor": None, "execution_authorized": False}
    for key, value in expected.items():
        require(type(record[key]) is type(value) and record[key] == value, "v2 record binding/semantics")
    require(canonical(record["document"]) == role_raw and pin(role_raw) == ROLE_PIN, "v2 exact business role")
    require(record["identity"] == identity(RECORD_NAMESPACE, {k: v for k, v in record.items() if k != "identity"}),
            "v2 record identity")
    challenge = closed(record["challenge_document"], "contract_version mode instance operator_sid binding_identity legacy_binding_identity epoch attempt state_digest migration_cut_identity classified_state_identity role_pin role_identity business_candidate frontend credential_generation user_handle rp_id issued_utc deadline_utc lease_owner_id", "v2 challenge")
    for key, value in {"contract_version": "policy-owner-human-challenge-v2", "mode": "ENROLLMENT_ONLY",
                       "instance": legacy["instance"], "operator_sid": legacy["operator_sid"],
                       "binding_identity": binding["identity"], "legacy_binding_identity": legacy["identity"],
                       "role_pin": ROLE_PIN, "role_identity": ROLE_IDENTITY,
                       "business_candidate": BUSINESS_CANDIDATE, "rp_id": RP_ID}.items():
        require(type(challenge[key]) is type(value) and challenge[key] == value, "v2 signed challenge scope")
    epoch = integer(challenge["epoch"])
    require(epoch > 0 and type(challenge["attempt"]) is str and
            re.fullmatch("[0-9a-f]{64}", challenge["attempt"]) is not None, "v2 epoch/attempt")
    require(type(challenge["lease_owner_id"]) is str and re.fullmatch("[0-9a-f]{64}", challenge["lease_owner_id"]), "v2 signed lease")
    require(challenge["credential_generation"] == record["credential_generation"] == challenge["attempt"], "v2 generation binding")
    expected_handle = base64.b64encode(hashlib.sha256(b"TA.PolicyOwner.H2.v2\0" + canonical({
        "rp": RP_ID, "operator_sid": legacy["operator_sid"], "instance": legacy["instance"],
        "attempt_nonce": challenge["attempt"]})).digest()).decode("ascii")
    require(challenge["user_handle"] == expected_handle, "H2 exact derivation")
    require(challenge["migration_cut_identity"] == migration_cut_identity and
            challenge["classified_state_identity"] == classified_state_identity,
            "signed challenge differs from actual durable migration/classified state")
    require(challenge["state_digest"] == identity("policy-owner-admission-state:v2", {
        "epoch": epoch, "attempt": challenge["attempt"], "phase": "RESERVED",
        "binding_identity": binding["identity"], "gate": "CLOSED",
        "migration_cut_identity": migration_cut_identity,
        "classified_state_identity": classified_state_identity}), "v2 state digest")
    frontend_value(record["frontend"], binding)
    require(record["frontend"] == challenge["frontend"], "v2 frontend creation instance")
    challenge_bytes = hashlib.sha256(canonical(challenge)).digest()
    require(hmac.compare_digest(b64(record["challenge"]), challenge_bytes), "v2 signed challenge digest")
    response = {"challenge_id": challenge["attempt"], "challenge": record["challenge"],
                "challenge_document": challenge, "rp_id": RP_ID, "user_handle": expected_handle,
                "attempt_id": challenge["attempt"], "epoch": epoch, "binding_identity": binding["identity"]}
    require(record["creation_receipt"] == identity("policy-owner-credential-creation-intent:v2", {
        "challenge": response, "frontend": record["frontend"], "user_handle": expected_handle}), "v2 creation intent receipt")
    issued, accepted, deadline = utc(challenge["issued_utc"]), utc(record["recorded_at_utc"]), utc(challenge["deadline_utc"])
    require(issued <= accepted < deadline and timedelta(0) < deadline-issued <= timedelta(minutes=10), "v2 acceptance deadline")
    require(integer(record["authenticator_counter"], 0xFFFFFFFF) == verify_proof(challenge_bytes, record["credential"], record["proof"]), "v2 signature/counter")
    return record


@dataclass(frozen=True)
class StorePaths:
    root: Path

    @property
    def owner_db(self) -> Path:
        return self.root / "state" / "owner.sqlite3"

    @property
    def authentication(self) -> Path:
        return self.root / "authentication" / "00000000.json"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"


class PlatformAuthority(ABC):
    """Actual OS adapter capability, never decoded from a request dictionary."""
    context = "PRODUCTION"

    @property
    @abstractmethod
    def lease_owner_id(self) -> str:
        raise PlatformValidationRequired("exclusive native lease identity required")

    @abstractmethod
    def assert_live(self, binding: dict, epoch: int | None, attempt: str | None, phase: str) -> None:
        """Verify service token, pinned registration, owner object and same held lease."""

    @abstractmethod
    def assert_cut(self, binding: dict) -> dict:
        """Return observed guard/fresh-kernel-boot evidence, not JSON assertions."""

    def assert_classification_cut(self, binding: dict) -> dict:
        """Physical cut prerequisite; a coordinator may not yet claim classification."""
        return self.assert_cut(binding)

    @abstractmethod
    def assert_copies(self, root: Path, binding: dict) -> None:
        """Attest complete new copies and their fixed baseline source provenance."""

    @abstractmethod
    def assert_closed_handoff(self, binding: dict, previous_lease_id: str) -> None:
        """Prove the old closed holder released its actual protected lease."""

    @abstractmethod
    def verify_frontend(self, frontend: dict, binding: dict) -> None:
        """Check real native pipe client handle PID, creation time and pinned image."""

    @abstractmethod
    def verify_creation_parameters(self, frontend: dict, challenge: dict, receipt: str) -> None:
        """Check actual native MakeCredential parameters, including the H2 user ID."""

    @abstractmethod
    def publish_immutable(self, path: Path, raw: bytes) -> None:
        """Durably publish new bytes atomically without replacement or cleanup."""


class SyntheticAuthority(PlatformAuthority):
    """Deliberate test capability limited to an explicitly marked synthetic root."""
    context = "SYNTHETIC_TEST_ONLY"

    def __init__(self, root: Path, *, lease_owner_id: str | None = None) -> None:
        require(root.name.startswith("admission-v2-synthetic-"), "synthetic root name required")
        require(_read(root / "SYNTHETIC-ONLY", 128) == b"ADMISSION_V2_SYNTHETIC_NO_PRODUCT\n", "synthetic sentinel")
        self.root = root.absolute()
        self._lease = lease_owner_id or secrets.token_hex(32)
        self.live = True
        self.cut_phases = MIGRATION_PHASES[:3]
        self.creation_attestations: set[tuple[str, str, str]] = set()
        self._publication_fault: Callable[[str], None] | None = None
        self._publication_prefix = ""
        require((str(self.root), self._lease) not in _SYNTHETIC_LEASES, "synthetic lease ID cannot be reused")
        _SYNTHETIC_LEASES[(str(self.root), self._lease)] = self

    @property
    def lease_owner_id(self) -> str:
        return self._lease

    def assert_live(self, binding: dict, epoch: int | None, attempt: str | None, phase: str) -> None:
        require(self.live and binding["legacy_setup_binding"]["owner_root"] == str(self.root / "store"), "synthetic lease/root")

    def assert_cut(self, binding: dict) -> dict:
        self.assert_live(binding, None, None, "MIGRATION")
        require(tuple(self.cut_phases) == MIGRATION_PHASES[:3], "synthetic complete maintenance cut required")
        return {"context": self.context, "phases": list(self.cut_phases), "boot_id": "synthetic-fresh-boot",
                "guard_identity": "synthetic-guard", "lease_owner_id": self._lease}

    def assert_copies(self, root: Path, binding: dict) -> None:
        self.assert_live(binding, None, None, "COPY_CLASSIFICATION")
        require(root.absolute() == self.root / "copies", "only this synthetic copied store")

    def assert_closed_handoff(self, binding: dict, previous_lease_id: str) -> None:
        self.assert_live(binding, None, None, "CLOSED_HANDOFF")
        previous = _SYNTHETIC_LEASES.get((str(self.root), previous_lease_id))
        require(previous is not None and previous is not self and not previous.live,
                "synthetic old physical lease not affirmatively released")

    def verify_frontend(self, frontend: dict, binding: dict) -> None:
        self.assert_live(binding, None, None, "FRONTEND")
        frontend_value(frontend, binding)

    def attest_synthetic_creation(self, frontend: dict, challenge: dict, receipt: str) -> None:
        self.creation_attestations.add((identity("synthetic-frontend", frontend), challenge["user_handle"], receipt))

    def verify_creation_parameters(self, frontend: dict, challenge: dict, receipt: str) -> None:
        require((identity("synthetic-frontend", frontend), challenge["user_handle"], receipt) in self.creation_attestations,
                "synthetic actual-parameter seam was not explicitly exercised")

    def publish_immutable(self, path: Path, raw: bytes) -> None:
        require(path.is_relative_to(self.root / "store") and self.live, "synthetic publication path/lease")
        temporary = self.root / "store" / "state" / "temp" / ("test-unpublished-" + secrets.token_hex(16))
        with temporary.open("xb", buffering=0) as stream:
            require(stream.write(raw) == len(raw), "synthetic short publication")
            os.fsync(stream.fileno())
        if self._publication_fault is not None:
            self._publication_fault(self._publication_prefix + "_after_temp_flush")
        # Synthetic-only, no Windows APIs: hard-link publication is atomic and
        # fails if target exists. Remove only this successful new temp link.
        os.link(temporary, path)
        if self._publication_fault is not None:
            self._publication_fault(self._publication_prefix + "_after_atomic_publish")
        temporary.unlink()


@dataclass(frozen=True)
class LegacyClassification:
    kind: str
    evidence: dict
    authority: PlatformAuthority
    binding_identity: str
    _issuer: object = None
    _evidence_identity: str = ""


def _copy_connection(path: Path) -> sqlite3.Connection:
    require(path.is_absolute(), "absolute copy path required")
    _read(path)
    for suffix in ("-journal", "-wal", "-shm"):
        require(not path.with_name(path.name + suffix).exists(), "copy sidecar present")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _snapshot(path: Path) -> tuple[list, dict[str, list]]:
    connection = _copy_connection(path)
    try:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "copy integrity")
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        result = {}
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            require(re.fullmatch("[A-Za-z_][A-Za-z0-9_]*", name) is not None, "unexpected copied table name")
            rows = connection.execute('SELECT * FROM "' + name + '"').fetchmany(4097)
            require(len(rows) <= 4096, "copied table row bound")
            result[name] = rows
        return schema, result
    finally:
        connection.close()


def _legacy_record(raw: bytes, binding: dict, role_raw: bytes, now: datetime) -> dict:
    record = strict_json(raw, canonical_required=True)
    closed(record, "contract_version instance operator_sid purpose document challenge_id challenge challenge_document credential proof authenticator_counter recorded_at_utc storage_binding_identity predecessor elevated_native_frontend_pin execution_authorized identity", "legacy record")
    legacy = binding["legacy_setup_binding"]
    require(record["contract_version"] == "policy-owner-authenticated-human-record-v1" and
            record["purpose"] == "ENROLL_PROJECT_ROLE" and canonical(record["document"]) == role_raw,
            "legacy record exact role")
    for key, expected in {"instance": legacy["instance"], "operator_sid": legacy["operator_sid"],
                          "storage_binding_identity": legacy["identity"], "predecessor": None,
                          "elevated_native_frontend_pin": legacy["native_owner_pin"]["sha256"],
                          "execution_authorized": False}.items():
        require(type(record[key]) is type(expected) and record[key] == expected, "legacy record old binding only")
    require(record["identity"] == identity("policy-owner-authenticated-human-record:v1",
            {k: v for k, v in record.items() if k != "identity"}), "legacy record identity")
    challenge = closed(record["challenge_document"], "contract_version instance storage_binding_identity operator_sid purpose document_sha256 nonce issued_utc expires_utc", "legacy challenge")
    require(challenge["contract_version"] == "policy-owner-human-challenge-v1" and
            challenge["nonce"] == record["challenge_id"] and challenge["document_sha256"] == ROLE_PIN["sha256"], "legacy challenge identity")
    for key in ("instance", "storage_binding_identity", "operator_sid", "purpose"):
        require(challenge[key] == record[key], "legacy challenge binding")
    digest = hashlib.sha256(canonical(challenge)).digest()
    require(hmac.compare_digest(digest, b64(record["challenge"])), "legacy challenge digest")
    issued, accepted, expires = utc(challenge["issued_utc"]), utc(record["recorded_at_utc"]), utc(challenge["expires_utc"])
    require(issued <= accepted < expires and expires-issued == timedelta(minutes=10) and accepted <= now,
            "legacy challenge historical window")
    require(integer(record["authenticator_counter"], 0xFFFFFFFF) == verify_proof(
        digest, record["credential"], record["proof"]), "legacy counter")
    return record


def classify_legacy_copies(root: Path, binding: dict, role_raw: bytes,
                           authority: PlatformAuthority, baseline_pins: dict) -> LegacyClassification:
    """Inspect only complete new copies authenticated by the maintenance adapter."""
    validate_binding(binding)
    validate_role(role_raw)
    require(isinstance(authority, PlatformAuthority), "typed platform authority required")
    authority.assert_copies(root, binding)
    closed(baseline_pins, "owner ledger", "baseline pins")
    for key, filename in (("owner", "baseline-owner.sqlite3"), ("ledger", "baseline-lineage.sqlite3")):
        require(pin(_read(root / filename)) == validate_pin(baseline_pins[key]), "fixed baseline copy pin")
    evidence = {"context": authority.context, "classification": "BLOCKED", "accepted_authentication": "UNKNOWN",
                "legacy_binding_identity": binding["legacy_setup_binding"]["identity"], "reason": None,
                "old_credential_existence": "UNKNOWN_NO_REUSE_AUTHORIZED"}
    try:
        authority.assert_classification_cut(binding)
        owner_schema, owner = _snapshot(root / "owner.sqlite3")
        base_schema, baseline = _snapshot(root / "baseline-owner.sqlite3")
        require(owner_schema == base_schema and set(owner) == {"metadata", "artifacts", "approvals", "applied_records"}, "legacy owner schema differs")
        ledger = _snapshot(root / "lineage.sqlite3")
        require(ledger == _snapshot(root / "baseline-lineage.sqlite3"), "ledger differs from immutable baseline")
        legacy = binding["legacy_setup_binding"]
        require(len(owner["metadata"]) == len(baseline["metadata"]) == 1 and
                owner["metadata"][0][:2] == baseline["metadata"][0][:2] == (legacy["instance"], legacy["ledger_identity"]), "owner metadata binding")
        floor = owner["metadata"][0][2]
        if floor:
            require(utc(floor) <= datetime.now(timezone.utc), "owner monotonic clock is future/unknown")
        require(not list((root / "temp").iterdir()), "unpublished/orphan temporary state")
        auth_names = sorted(p.name for p in (root / "authentication").iterdir())
        expected_artifacts = {row[0]: _blob(row[1]) for row in baseline["artifacts"]}
        expected_applied = {row[0]: _blob(row[1]) for row in baseline["applied_records"]}
        require(not baseline["approvals"] and not expected_applied and not owner["approvals"], "unexpected approval/initial applied rows")
        if auth_names:
            require(auth_names == ["00000000.json"], "unexpected authentication sequence")
            raw = _read(root / "authentication" / "00000000.json")
            record = _legacy_record(raw, binding, role_raw, datetime.now(timezone.utc))
            evidence["accepted_authentication"] = "VERIFIED_LEGACY_V1"
            reference = {"identity": record["identity"], "pin": pin(raw)}
            expected_artifacts[record["identity"]] = canonical(reference) + b"\n"
            expected_applied[record["identity"]] = canonical({"status": "AUTHENTICATED_OWNER_RECORD_RETAINED",
                "authentication_identity": record["identity"], "execution_authorized": False})
            evidence["existing_authentication_identity"] = record["identity"]
            kind = "EXISTING"
        else:
            evidence["accepted_authentication"] = "NO_ACCEPTANCE_IN_COMPLETE_CUT"
            kind = "ELIGIBLE"
        require({r[0]: _blob(r[1]) for r in owner["artifacts"]} == expected_artifacts and
                {r[0]: _blob(r[1]) for r in owner["applied_records"]} == expected_applied,
                "partial/unexpected authentication derivation")
        blobs = {}
        for raw_reference in expected_artifacts.values():
            require(raw_reference.endswith(b"\n"), "v1 EvidenceRef canonical+LF required")
            reference = strict_json(raw_reference[:-1], canonical_required=True)
            closed(reference, "identity pin", "artifact reference")
            artifact_pin = validate_pin(reference["pin"])
            filename = artifact_pin["sha256"] + ".blob"
            require(_artifact_pin(root / "artifacts" / filename) == artifact_pin, "artifact blob mismatch")
            blobs[filename] = artifact_pin
        require(sorted(p.name for p in (root / "artifacts").iterdir()) == sorted(blobs), "orphan/missing artifact files")
        evidence["classification"] = kind
        evidence["owner_copy_pin"] = pin(_read(root / "owner.sqlite3"))
        evidence["ledger_copy_pin"] = pin(_read(root / "lineage.sqlite3"))
        evidence["baseline_pins"] = baseline_pins
        evidence["owner_schema_identity"] = identity("policy-owner-copy-schema:v2", owner_schema)
        evidence["authentication_files"] = {name: pin(_read(root / "authentication" / name)) for name in auth_names}
        evidence["artifact_files"] = blobs
        evidence["temporary_files"] = []
    except (ValueError, OSError, sqlite3.Error) as error:
        kind = "BLOCKED"
        evidence["classification"] = "BLOCKED"
        evidence["reason"] = f"{type(error).__name__}: {error}"
    return LegacyClassification(kind, evidence, authority, binding["identity"], _CLASSIFICATION_ISSUER,
                                identity("policy-owner-admission-classified-state:v2", evidence))


_DDL = (
    "CREATE TABLE admission_v2_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL CHECK(epoch>=0), phase TEXT NOT NULL, attempt TEXT, lease_owner_id TEXT NOT NULL, deadline_utc TEXT, challenge BLOB, creation_receipt TEXT, record BLOB, binding BLOB NOT NULL, last_utc TEXT NOT NULL, blocked_reason TEXT)",
    "CREATE TABLE admission_v2_attempts (nonce TEXT PRIMARY KEY, user_handle TEXT NOT NULL UNIQUE, challenge BLOB NOT NULL)",
    "CREATE TABLE admission_v2_events (sequence INTEGER PRIMARY KEY, epoch INTEGER NOT NULL, attempt TEXT, phase TEXT NOT NULL, evidence BLOB NOT NULL)",
    "CREATE TABLE admission_v2_migration (singleton INTEGER PRIMARY KEY CHECK(singleton=1), phase TEXT NOT NULL, cut_identity TEXT NOT NULL, classification TEXT NOT NULL, evidence BLOB NOT NULL)",
    "CREATE TRIGGER admission_v2_control_no_delete BEFORE DELETE ON admission_v2_control BEGIN SELECT RAISE(ABORT, 'durable closed gate'); END",
    "CREATE TRIGGER admission_v2_attempts_no_update BEFORE UPDATE ON admission_v2_attempts BEGIN SELECT RAISE(ABORT, 'attempt nonce is immutable'); END",
    "CREATE TRIGGER admission_v2_attempts_no_delete BEFORE DELETE ON admission_v2_attempts BEGIN SELECT RAISE(ABORT, 'attempt nonce is immutable'); END",
    "CREATE TRIGGER admission_v2_events_no_update BEFORE UPDATE ON admission_v2_events BEGIN SELECT RAISE(ABORT, 'control evidence is immutable'); END",
    "CREATE TRIGGER admission_v2_events_no_delete BEFORE DELETE ON admission_v2_events BEGIN SELECT RAISE(ABORT, 'control evidence is immutable'); END",
    "CREATE TRIGGER admission_v2_migration_no_evidence_update BEFORE UPDATE OF cut_identity,classification,evidence ON admission_v2_migration BEGIN SELECT RAISE(ABORT, 'migration evidence is immutable'); END",
    "CREATE TRIGGER admission_v2_migration_no_delete BEFORE DELETE ON admission_v2_migration BEGIN SELECT RAISE(ABORT, 'migration evidence is immutable'); END",
)


class EnrollmentOnlyStore:
    def __init__(self, paths: StorePaths, binding: dict, role_raw: bytes, authority: PlatformAuthority, *,
                 clock: Callable[[], datetime] | None = None, fault: Callable[[str], None] | None = None) -> None:
        self.binding = validate_binding(binding)
        self.role = validate_role(role_raw)
        self.role_raw = role_raw
        require(isinstance(authority, PlatformAuthority), "typed OS/test authority required")
        require(authority.context in {"PRODUCTION", "PRODUCTION_MAINTENANCE", "SYNTHETIC_TEST_ONLY"}, "authority context")
        if clock is not None or fault is not None:
            require(type(authority) is SyntheticAuthority, "test seams unavailable in production")
        self.authority, self.paths = authority, paths
        legacy = binding["legacy_setup_binding"]
        require(paths.root.is_absolute() and str(paths.root) == legacy["owner_root"] and
                str(paths.owner_db) == legacy["owner_state_path"] and
                str(paths.root / "state" / "lineage.sqlite3") == legacy["ledger_path"],
                "store paths differ from immutable legacy identity")
        require(self.role["instance"] == binding["legacy_setup_binding"]["instance"] and
                self.role["operator_sid"] == binding["legacy_setup_binding"]["operator_sid"], "business principal binding")
        require(re.fullmatch("[0-9a-f]{64}", authority.lease_owner_id) is not None, "physical lease owner identity")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault = fault
        authority.assert_live(binding, None, None, "OPEN")

    def _now(self) -> str:
        value = self.clock()
        require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timedelta(0), "trusted UTC clock")
        return value.isoformat(timespec="microseconds")

    def _fault(self, point: str) -> None:
        if self.fault is not None:
            require(point in FAULT_POINTS and type(self.authority) is SyntheticAuthority, "closed test fault point")
            self.fault(point)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.authority.assert_live(self.binding, None, None, "CONNECT")
        connection = sqlite3.connect(self.paths.owner_db.as_uri() + "?mode=rw", uri=True, timeout=0,
                                     isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            legacy = self.binding["legacy_setup_binding"]
            rows = connection.execute("SELECT instance,ledger_id,last_clock FROM metadata").fetchall()
            require(len(rows) == 1 and rows[0][:2] == (legacy["instance"], legacy["ledger_identity"]), "preserved owner metadata")
            if rows[0][2]:
                require(utc(rows[0][2]) <= utc(self._now()), "legacy durable clock regression")
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _row(connection: sqlite3.Connection) -> dict:
        expected_schema = {statement.split()[2]: " ".join(statement.rstrip(";").split()) for statement in _DDL}
        actual_schema = {name: " ".join(sql.rstrip(";").split()) for name, sql in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE 'admission_v2_%'").fetchall()}
        require(actual_schema == expected_schema, "closed v2 schema/trigger mismatch")
        cursor = connection.execute("SELECT * FROM admission_v2_control WHERE singleton=1")
        values = cursor.fetchall()
        require(len(values) == 1, "one durable admission control required")
        row = dict(zip((item[0] for item in cursor.description), values[0], strict=True))
        require(row["phase"] in PHASES and type(row["epoch"]) is int and 0 <= row["epoch"] <= 2**63-1,
                "control phase/epoch")
        require(row["attempt"] is None or (type(row["attempt"]) is str and re.fullmatch("[0-9a-f]{64}", row["attempt"])),
                "control attempt encoding")
        return row

    def _migration_context(self, connection: sqlite3.Connection) -> dict:
        rows = connection.execute("SELECT cut_identity,evidence FROM admission_v2_migration WHERE singleton=1").fetchall()
        require(len(rows) == 1, "durable migration evidence required")
        evidence = strict_json(_blob(rows[0][1]), canonical_required=True)
        require(evidence["legacy_binding_identity"] == self.binding["legacy_setup_binding"]["identity"],
                "migration classification belongs to different legacy binding")
        return {"migration_cut_identity": rows[0][0],
                "classified_state_identity": identity("policy-owner-admission-classified-state:v2", evidence)}

    def _check_row(self, row: dict, epoch: int, attempt: str | None, phase: str,
                   *, completed_read: bool = False) -> None:
        integer(epoch)
        self.authority.assert_live(self.binding, epoch, attempt, phase)
        require(row["binding"] == canonical(self.binding), "durable binding mismatch")
        require(row["epoch"] == epoch and row["attempt"] == attempt and row["phase"] == phase, "stale epoch/attempt/phase CAS")
        require(row["lease_owner_id"] == self.authority.lease_owner_id or
                (completed_read and phase == "COMPLETE_STOP"), "exclusive lease owner changed")
        require(utc(row["last_utc"]) <= utc(self._now()), "v2 durable clock regression")

    def _event(self, connection: sqlite3.Connection, epoch: int, attempt: str | None, phase: str, evidence: dict) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM admission_v2_events").fetchone()[0]
        connection.execute("INSERT INTO admission_v2_events VALUES (?,?,?,?,?)",
                           (sequence, epoch, attempt, phase, canonical(evidence)))

    def _advance(self, epoch: int, attempt: str, phase: str, target: str, *,
                 action: Callable[[sqlite3.Connection], None] | None = None,
                 record: bytes | None = None, receipt: str | None = None,
                 before_fault: str | None = None, after_fault: str | None = None) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            self._check_row(row, epoch, attempt, phase)
            migration_context = self._migration_context(connection)
            challenge = strict_json(_blob(row["challenge"]), canonical_required=True)
            require(all(challenge["challenge_document"][key] == value for key, value in migration_context.items()),
                    "effect CAS lost migration/classification binding")
            require(row["deadline_utc"] is not None and utc(self._now()) < utc(row["deadline_utc"]), "attempt expired; no late effect")
            if action is not None:
                self.authority.assert_live(self.binding, epoch, attempt, phase)
                action(connection)
            next_epoch = epoch + 1 if target == "COMPLETE_STOP" else epoch
            cursor = connection.execute(
                "UPDATE admission_v2_control SET epoch=?,phase=?,last_utc=?,record=COALESCE(?,record),creation_receipt=COALESCE(?,creation_receipt) WHERE singleton=1 AND epoch=? AND attempt=? AND phase=? AND lease_owner_id=?",
                (next_epoch, target, self._now(), record, receipt, epoch, attempt, phase, self.authority.lease_owner_id))
            require(cursor.rowcount == 1, "atomic admission CAS failed")
            self._event(connection, next_epoch, attempt, target, {"previous_phase": phase, "execution_authorized": False})
            if before_fault:
                self._fault(before_fault)
            connection.commit()
        if after_fault:
            self._fault(after_fault)
        return {"epoch": next_epoch, "attempt": attempt, "phase": target}

    def _block(self, reason: str, expected: tuple[int, str | None] | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            if expected is not None and (row["epoch"], row["attempt"]) != expected:
                connection.rollback()
                return
            if row["phase"] in {"BLOCKED", "COMPLETE_STOP"}:
                connection.rollback()
                return
            self.authority.assert_live(self.binding, row["epoch"], row["attempt"], row["phase"])
            observed_now = self._now()
            clock_regressed = utc(observed_now) < utc(row["last_utc"])
            durable_floor = row["last_utc"] if clock_regressed else observed_now
            updated = connection.execute(
                "UPDATE admission_v2_control SET epoch=epoch+1,phase='BLOCKED',last_utc=?,blocked_reason=? WHERE singleton=1 AND epoch=? AND attempt IS ? AND phase=?",
                (durable_floor, reason[:4096], row["epoch"], row["attempt"], row["phase"]))
            require(updated.rowcount == 1, "BLOCKED CAS failed")
            self._event(connection, row["epoch"] + 1, row["attempt"], "BLOCKED", {
                "reason": reason[:4096], "clock_regression_observed": clock_regressed})
            self._fault("block_before_commit")
            connection.commit()
        self._fault("block_after_commit")

    def migrate_closed(self, classification: LegacyClassification) -> dict:
        require(type(classification) is LegacyClassification and classification.authority is self.authority and
                classification.binding_identity == self.binding["identity"] and
                classification._issuer is _CLASSIFICATION_ISSUER and
                classification._evidence_identity == identity("policy-owner-admission-classified-state:v2", classification.evidence) and
                classification.kind == classification.evidence.get("classification"), "classification capability mismatch")
        require(classification.kind == "ELIGIBLE", "EXISTING/BLOCKED classification cannot mutate owner DB or stage a v2 binding")
        cut = self.authority.assert_cut(self.binding)
        require(type(cut) is dict and cut.get("phases") == list(MIGRATION_PHASES[:3]) and
                cut.get("lease_owner_id") == self.authority.lease_owner_id, "complete actual maintenance cut required")
        if self.authority.context == "SYNTHETIC_TEST_ONLY":
            cut_identity = identity("policy-owner-admission-cut:v2", cut)
        else:
            raw_cut = {k: v for k, v in cut.items() if k not in {"context", "lease_owner_id", "identity"}}
            cut_identity = identity("policy-owner-admission-maintenance-cut:v2", raw_cut)
            require(cut.get("identity") == cut_identity, "stable durable raw maintenance cut identity required")
        self._fault("migration_before_schema")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require(not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'admission_v2_%'").fetchall(),
                    "migration already started; no retry/resume")
            for statement in _DDL:
                connection.execute(statement)
            phase = "CLOSED"
            connection.execute("INSERT INTO admission_v2_control VALUES (1,0,?,NULL,?,NULL,NULL,NULL,NULL,?,?,?)",
                               (phase, self.authority.lease_owner_id, canonical(self.binding), self._now(),
                                None if phase == "CLOSED" else classification.kind))
            connection.execute("INSERT INTO admission_v2_migration VALUES (1,?,?,?,?)",
                               (MIGRATION_PHASES[3], cut_identity, classification.kind, canonical(classification.evidence)))
            self._event(connection, 0, None, phase, {"migration": MIGRATION_PHASES[3], "cut_identity": cut_identity})
            connection.commit()
        self._fault("migration_after_schema")
        return {"status": "SCHEMA_MIGRATED_CLOSED", "classification": classification.kind, "cut_identity": cut_identity,
                "execution_authorized": False}

    def stage_binding(self) -> dict:
        return self._migration_advance(MIGRATION_PHASES[3], MIGRATION_PHASES[4], "migration_after_binding")

    def activate_closed(self) -> dict:
        return self._migration_advance(MIGRATION_PHASES[4], MIGRATION_PHASES[5], "migration_after_activation")

    def _migration_advance(self, expected: str, target: str, fault: str) -> dict:
        self.authority.assert_cut(self.binding)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            require(row["phase"] == "CLOSED" and row["attempt"] is None,
                    "migration gate must remain closed")
            self._check_row(row, row["epoch"], None, row["phase"])
            cursor = connection.execute("UPDATE admission_v2_migration SET phase=? WHERE singleton=1 AND phase=?",
                                        (target, expected))
            require(cursor.rowcount == 1, "migration phase CAS")
            self._event(connection, row["epoch"], None, row["phase"], {"migration": target})
            connection.commit()
        self._fault(fault)
        return {"status": target, "gate": "CLOSED", "execution_authorized": False}

    def inspect(self) -> dict:
        with self._connection() as connection:
            row = self._row(connection)
            require(row["binding"] == canonical(self.binding), "durable binding mismatch")
            migration = connection.execute("SELECT phase,classification FROM admission_v2_migration WHERE singleton=1").fetchall()
            require(len(migration) == 1, "migration state missing")
        interrupted = row["phase"] in ACTIVE_PHASES and (
            row["lease_owner_id"] != self.authority.lease_owner_id or utc(self._now()) >= utc(row["deadline_utc"]))
        if interrupted:
            self._block("INTERRUPTED_EXPIRED_OR_UNKNOWN_PRIOR_LEASE_NO_RESUME")
            return self.inspect()
        return {"mode": "ENROLLMENT_ONLY", "gate": "CLOSED", "epoch": row["epoch"], "attempt": row["attempt"],
                "phase": row["phase"], "migration_phase": migration[0][0], "classification": migration[0][1],
                "binding_identity": self.binding["identity"], "execution_authorized": False,
                "historical_native_exit": "UNKNOWN_NOT_AN_ADMISSION_SUBSTITUTE"}

    def adopt_closed(self, expected_epoch: int) -> dict:
        """Private maintenance/service handoff only, never an authentication."""
        integer(expected_epoch)
        require(expected_epoch < 2**63-1, "admission epoch exhausted")
        state = self.inspect()
        require(state["phase"] == "CLOSED" and state["classification"] == "ELIGIBLE" and
                state["migration_phase"] == MIGRATION_PHASES[-1] and state["attempt"] is None,
                "handoff requires an unused eligible activated closed gate")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            require(row["epoch"] == expected_epoch and row["phase"] == "CLOSED" and row["attempt"] is None and
                    row["binding"] == canonical(self.binding), "closed handoff CAS precondition")
            require(connection.execute("SELECT COUNT(*) FROM admission_v2_attempts").fetchone() == (0,),
                    "a prior attempt forbids closed adoption")
            require(row["lease_owner_id"] != self.authority.lease_owner_id, "same lease does not need handoff")
            self.authority.assert_live(self.binding, expected_epoch, None, "CLOSED")
            self.authority.assert_closed_handoff(self.binding, row["lease_owner_id"])
            changed = connection.execute(
                "UPDATE admission_v2_control SET epoch=epoch+1,lease_owner_id=?,last_utc=? WHERE singleton=1 AND epoch=? AND attempt IS NULL AND phase='CLOSED' AND lease_owner_id=?",
                (self.authority.lease_owner_id, self._now(), expected_epoch, row["lease_owner_id"]))
            require(changed.rowcount == 1, "closed handoff atomic CAS")
            self._event(connection, expected_epoch + 1, None, "CLOSED", {"previous_lease_owner_id": row["lease_owner_id"],
                "new_lease_owner_id": self.authority.lease_owner_id, "authentication_authorized": False})
            self._fault("handoff_before_commit")
            connection.commit()
        self._fault("handoff_after_commit")
        return {"status": "CLOSED_HANDOFF_COMPLETE", "epoch": expected_epoch + 1, "gate": "CLOSED",
                "execution_authorized": False}

    def reserve(self, expected_epoch: int, deadline_utc: str, frontend: dict) -> dict:
        integer(expected_epoch)
        require(expected_epoch < 2**63-2, "admission epoch exhausted")
        self.authority.verify_frontend(frontend_value(frontend, self.binding), self.binding)
        state = self.inspect()
        require(state["phase"] == "CLOSED" and state["migration_phase"] == MIGRATION_PHASES[-1] and
                state["classification"] == "ELIGIBLE", "not an eligible activated closed gate")
        now, deadline = utc(self._now()), utc(deadline_utc)
        require(now < deadline <= now + timedelta(minutes=10), "bounded fresh attempt deadline")
        allocated_epoch, allocated_attempt = expected_epoch, None
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection)
                self._check_row(row, expected_epoch, None, "CLOSED")
                require(connection.execute("SELECT COUNT(*) FROM admission_v2_attempts").fetchone() == (0,),
                        "one enrollment attempt only; no new credential after partial state")
                require(not self.paths.authentication.exists(), "existing first authentication blocks new enrollment")
                require(connection.execute("SELECT COUNT(*) FROM applied_records").fetchone() == (0,), "existing applied authentication")
                nonce = secrets.token_hex(32)
                epoch = expected_epoch + 1
                allocated_epoch, allocated_attempt = epoch, nonce
                migration_context = self._migration_context(connection)
                legacy = self.binding["legacy_setup_binding"]
                user_handle = base64.b64encode(hashlib.sha256(b"TA.PolicyOwner.H2.v2\0" + canonical({
                    "rp": RP_ID, "operator_sid": legacy["operator_sid"], "instance": legacy["instance"],
                    "attempt_nonce": nonce})).digest()).decode("ascii")
                state_digest = identity("policy-owner-admission-state:v2", {"epoch": epoch, "attempt": nonce,
                    "phase": "RESERVED", "binding_identity": self.binding["identity"], "gate": "CLOSED", **migration_context})
                challenge_document = {
                    "contract_version": "policy-owner-human-challenge-v2", "mode": "ENROLLMENT_ONLY",
                    "instance": legacy["instance"], "operator_sid": legacy["operator_sid"],
                    "binding_identity": self.binding["identity"], "legacy_binding_identity": legacy["identity"],
                    "epoch": epoch, "attempt": nonce, "state_digest": state_digest, "role_pin": dict(ROLE_PIN),
                    **migration_context,
                    "role_identity": ROLE_IDENTITY, "business_candidate": BUSINESS_CANDIDATE,
                    "frontend": frontend, "credential_generation": nonce, "user_handle": user_handle,
                    "rp_id": RP_ID, "issued_utc": self._now(), "deadline_utc": deadline_utc,
                    "lease_owner_id": self.authority.lease_owner_id,
                }
                challenge = {"challenge_id": nonce, "challenge": base64.b64encode(hashlib.sha256(canonical(challenge_document)).digest()).decode("ascii"),
                             "challenge_document": challenge_document, "rp_id": RP_ID, "user_handle": user_handle,
                             "attempt_id": nonce, "epoch": epoch, "binding_identity": self.binding["identity"]}
                connection.execute("INSERT INTO admission_v2_attempts VALUES (?,?,?)", (nonce, user_handle, canonical(challenge)))
                changed = connection.execute("UPDATE admission_v2_control SET epoch=?,phase='RESERVED',attempt=?,deadline_utc=?,challenge=?,last_utc=? WHERE singleton=1 AND epoch=? AND attempt IS NULL AND phase='CLOSED' AND lease_owner_id=?",
                    (epoch, nonce, deadline_utc, canonical(challenge), self._now(), expected_epoch, self.authority.lease_owner_id))
                require(changed.rowcount == 1, "reservation CAS")
                self._event(connection, epoch, nonce, "RESERVED", {"state_digest": state_digest, "gate": "CLOSED"})
                self._fault("reserve_before_commit")
                connection.commit()
            self._fault("reserve_after_commit")
            return challenge
        except Exception as error:
            self._try_block(error, allocated_epoch, allocated_attempt)
            raise

    def begin_creation(self, expected_epoch: int, attempt: str, frontend: dict) -> dict:
        self.authority.verify_frontend(frontend_value(frontend, self.binding), self.binding)
        with self._connection() as connection:
            row = self._row(connection)
            self._check_row(row, expected_epoch, attempt, "RESERVED")
            challenge = strict_json(_blob(row["challenge"]), canonical_required=True)
        require(frontend == challenge["challenge_document"]["frontend"], "frontend changed before creation")
        receipt = identity("policy-owner-credential-creation-intent:v2", {"challenge": challenge,
            "frontend": frontend, "user_handle": challenge["user_handle"]})
        try:
            self._advance(expected_epoch, attempt, "RESERVED", "CREATION_BEGUN", receipt=receipt,
                          before_fault="creation_before_commit", after_fault="creation_after_commit")
            return {"status": "CREATION_BEGUN", "creation_receipt": receipt, "user_handle": challenge["user_handle"],
                    "epoch": expected_epoch, "attempt": attempt, "execution_authorized": False}
        except Exception as error:
            self._try_block(error, expected_epoch, attempt)
            raise

    def _try_block(self, error: Exception, epoch: int, attempt: str | None) -> None:
        # No authority to repair/reopen. The interrupted durable phase is
        # retained and a future different lease must mark it BLOCKED.
        with contextlib.suppress(Exception):
            self._block(type(error).__name__ + ": " + str(error), (epoch, attempt))

    def _publish(self, path: Path, raw: bytes, epoch: int, attempt: str, phase: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            self._check_row(row, epoch, attempt, phase)
            migration_context = self._migration_context(connection)
            challenge = strict_json(_blob(row["challenge"]), canonical_required=True)
            require(all(challenge["challenge_document"][key] == value for key, value in migration_context.items()),
                    "publication lost migration/classification binding")
            require(utc(self._now()) < utc(row["deadline_utc"]), "publication deadline expired")
            self.authority.assert_live(self.binding, epoch, attempt, phase)
            # OS adapter uses new temporary bytes + durable no-replace atomic
            # publication. Interrupted intent/orphan temp remains BLOCKED.
            if type(self.authority) is SyntheticAuthority:
                self.authority._publication_fault = self._fault
                self.authority._publication_prefix = "authentication" if path == self.paths.authentication else "blob"
            self.authority.publish_immutable(path, raw)
            require(_read(path) == raw, "immutable publication readback mismatch")
            connection.commit()

    def accept(self, expected_epoch: int, attempt: str, registration: dict, proof: dict, frontend: dict) -> dict:
        try:
            self.inspect()
            with self._connection() as connection:
                row = self._row(connection)
                self._check_row(row, expected_epoch, attempt, "CREATION_BEGUN")
                challenge = strict_json(_blob(row["challenge"]), canonical_required=True)
                migration_context = self._migration_context(connection)
            self.authority.verify_frontend(frontend_value(frontend, self.binding), self.binding)
            require(frontend == challenge["challenge_document"]["frontend"], "different frontend creation instance")
            self.authority.verify_creation_parameters(frontend, challenge, row["creation_receipt"])
            require(utc(self._now()) < utc(row["deadline_utc"]), "late authentication rejected")
            counter = verify_proof(b64(challenge["challenge"]), registration, proof)
            legacy = self.binding["legacy_setup_binding"]
            record = seal(RECORD_NAMESPACE, {
                "contract_version": "policy-owner-authenticated-human-record-v2", "mode": "ENROLLMENT_ONLY",
                "purpose": "ENROLL_PROJECT_ROLE", "instance": legacy["instance"], "operator_sid": legacy["operator_sid"],
                "document": self.role, "legacy_binding_identity": legacy["identity"], "binding_identity": self.binding["identity"],
                "challenge": challenge["challenge"], "challenge_document": challenge["challenge_document"],
                "credential": registration, "proof": proof, "authenticator_counter": counter,
                "recorded_at_utc": self._now(), "predecessor": None, "frontend": frontend,
                "creation_receipt": row["creation_receipt"], "credential_generation": attempt,
                "execution_authorized": False,
            })
            raw = canonical(record)
            validate_v2_record(record, self.binding, self.role_raw, **migration_context)
            self._advance(expected_epoch, attempt, "CREATION_BEGUN", "PUBLICATION_INTENT", record=raw,
                          before_fault="intent_before_commit", after_fault="intent_after_commit")
            self._fault("authentication_before_publish")
            self._publish(self.paths.authentication, raw, expected_epoch, attempt, "PUBLICATION_INTENT")
            self._fault("authentication_after_publish")
            self._advance(expected_epoch, attempt, "PUBLICATION_INTENT", "RECORD_PUBLISHED", after_fault="record_after_commit")
            self._fault("blob_before_publish")
            self._publish(self.paths.artifacts / (pin(raw)["sha256"] + ".blob"), raw,
                          expected_epoch, attempt, "RECORD_PUBLISHED")
            self._fault("blob_after_publish")
            self._advance(expected_epoch, attempt, "RECORD_PUBLISHED", "BLOB_PUBLISHED", after_fault="blob_phase_after_commit")
            reference = canonical({"identity": record["identity"], "pin": pin(raw)})
            self._advance(expected_epoch, attempt, "BLOB_PUBLISHED", "INDEXED",
                action=lambda db: db.execute("INSERT INTO artifacts VALUES (?,?)", (record["identity"], reference)),
                before_fault="index_before_commit", after_fault="index_after_commit")
            response = {"status": "ENROLLMENT_V2_COMPLETE_STOP", "authentication_identity": record["identity"],
                        "binding_identity": self.binding["identity"], "role_identity": ROLE_IDENTITY,
                        "execution_authorized": False}
            self._advance(expected_epoch, attempt, "INDEXED", "APPLIED",
                action=lambda db: db.execute("INSERT INTO applied_records VALUES (?,?)", (record["identity"], canonical(response))),
                before_fault="applied_before_commit", after_fault="applied_after_commit")
            self._verify_effects(record, raw, response, expected_epoch, attempt, "APPLIED")
            self._advance(expected_epoch, attempt, "APPLIED", "COMPLETE_STOP",
                          before_fault="complete_before_commit", after_fault="complete_after_commit")
            return {**response, "phase": "COMPLETE_STOP", "epoch": expected_epoch + 1, "attempt": attempt,
                    "context": self.authority.context}
        except Exception as error:
            self._try_block(error, expected_epoch, attempt)
            raise

    def _verify_effects(self, record: dict, raw: bytes, response: dict, epoch: int, attempt: str | None, phase: str) -> None:
        self.authority.assert_live(self.binding, epoch, attempt, phase)
        require(_read(self.paths.authentication) == raw and
                _read(self.paths.artifacts / (pin(raw)["sha256"] + ".blob")) == raw, "accepted immutable bytes incomplete")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            self._check_row(row, epoch, attempt, phase, completed_read=(phase == "COMPLETE_STOP"))
            require(connection.execute("SELECT reference FROM artifacts WHERE identity=?", (record["identity"],)).fetchall() ==
                    [(canonical({"identity": record["identity"], "pin": pin(raw)}),)],
                    "exact artifact derivation missing")
            require(connection.execute("SELECT response FROM applied_records WHERE identity=?", (record["identity"],)).fetchall() ==
                    [(canonical(response),)], "exact applied response missing")
            connection.rollback()

    def verify_complete(self) -> dict:
        state = self.inspect()
        require(state["phase"] == "COMPLETE_STOP", "incomplete enrollment is not an acceptance")
        with self._connection() as connection:
            row = self._row(connection)
            migration_context = self._migration_context(connection)
        record = strict_json(_blob(row["record"]), canonical_required=True)
        validate_v2_record(record, self.binding, self.role_raw, **migration_context)
        require(record["credential_generation"] == row["attempt"] and
                record["challenge_document"]["epoch"] + 1 == row["epoch"], "completed control-to-record binding")
        response = {"status": "ENROLLMENT_V2_COMPLETE_STOP", "authentication_identity": record["identity"],
                    "binding_identity": self.binding["identity"], "role_identity": ROLE_IDENTITY, "execution_authorized": False}
        self._verify_effects(record, _blob(row["record"]), response, row["epoch"], row["attempt"], "COMPLETE_STOP")
        return {"status": "CONTEXT_ESTABLISHED_STOP", "next": "STOP_FOR_SEPARATE_HUMAN_DECISION",
                "context": self.authority.context,
                "mode": "ENROLLMENT_ONLY", "role_identity": ROLE_IDENTITY, "authentication_identity": record["identity"],
                "binding_identity": self.binding["identity"], "historical_native_exit": "UNKNOWN",
                "execution_authorized": False}


def production_authority_from_os(frame: dict, root: Path) -> PlatformAuthority:
    """No boolean/native-claim fallback while the actual OS adapter is unqualified."""
    raise PlatformValidationRequired(
        "actual dedicated service token, protected v2 registration, native pipe origin and continuously held lease adapter required; request JSON cannot supply authority"
    )


def dispatch_v2(frame: dict, root: Path, authority: PlatformAuthority | None = None) -> dict:
    closed(frame, "contract_version mode anchor_v2 lease_id operation payload", "worker frame")
    require(frame["contract_version"] == "policy-owner-trusted-worker-request-v2" and frame["mode"] == "ENROLLMENT_ONLY",
            "legacy/generic worker endpoints are disabled")
    require(frame["operation"] in {"inspect-v2", "adopt-closed-v2", "reserve-v2", "begin-creation-v2", "accept-v2", "verify-complete-v2"},
            "unknown or forbidden enrollment-only operation")
    binding = validate_binding(frame["anchor_v2"])
    require(str(root) == binding["legacy_setup_binding"]["owner_root"], "worker registered root mismatch")
    if authority is None:
        authority = production_authority_from_os(frame, root)
    require(not isinstance(authority, SyntheticAuthority) and authority.context == "PRODUCTION", "no TEST fallback in formal worker")
    require(frame["lease_id"] == authority.lease_owner_id, "worker physical lease identity mismatch")
    store = EnrollmentOnlyStore(StorePaths(root), binding, _read(root / "role-proposal.json"), authority)
    payload, operation = frame["payload"], frame["operation"]
    if operation == "adopt-closed-v2":
        closed(payload, "expected_epoch", "private closed handoff")
        return store.adopt_closed(payload["expected_epoch"])
    if operation in {"inspect-v2", "verify-complete-v2"}:
        closed(payload, "", "inspection")
        return store.inspect() if operation == "inspect-v2" else store.verify_complete()
    if operation == "reserve-v2":
        closed(payload, "expected_epoch deadline_utc frontend", "reserve")
        return store.reserve(**payload)
    if operation == "begin-creation-v2":
        closed(payload, "expected_epoch attempt frontend", "creation")
        return store.begin_creation(**payload)
    if operation == "accept-v2":
        closed(payload, "expected_epoch attempt registration proof frontend", "accept")
        return store.accept(**payload)
    raise AdmissionRejected("unreachable unknown enrollment-only operation")
