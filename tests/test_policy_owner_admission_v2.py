"""Standalone synthetic admission-v2 batch; never use repository pytest fixtures.

Run only once under the separately approved frozen batch with -I -B -S and an
explicit NEW artifact root. All synthetic files are retained. No native API,
process launch, registry, service, Hello, network or installed store is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.machinery
import json
import shutil
import sqlite3
import struct
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# This retained synthetic batch has its own isolated interpreter and artifact
# lifecycle. Ordinary pytest must not collect its uninitialized unittest cases.
__test__ = False

CORE = None
CRYPTO = None
ARTIFACT_ROOT = None
NEXT_CASE = 0


def _load() -> None:
    global CORE, CRYPTO
    source = Path(__file__).resolve().parents[1]
    if "tradingagents" in sys.modules:
        raise RuntimeError("standalone test requires an untouched namespace")
    package = types.ModuleType("tradingagents")
    package.__path__ = [str(source / "tradingagents")]
    package.__package__ = "tradingagents"
    package.__spec__ = importlib.machinery.ModuleSpec("tradingagents", None, is_package=True)
    sys.modules["tradingagents"] = package
    for leaf in ("policy_owner_admission_crypto", "policy_owner_admission_v2",
                 "policy_owner_admission_windows", "policy_owner_admission_migration"):
        name = "tradingagents." + leaf
        path = source / "tradingagents" / (leaf + ".py")
        raw = path.read_bytes()
        module = types.ModuleType(name)
        module.__file__ = str(path)
        module.__package__ = "tradingagents"
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, origin=str(path))
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    CORE = sys.modules["tradingagents.policy_owner_admission_v2"]
    CRYPTO = sys.modules["tradingagents.policy_owner_admission_crypto"]


def _new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)


def _owner_database(path: Path, instance: str, ledger: str) -> None:
    _new(path, b"")
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE metadata (instance TEXT NOT NULL, ledger_id TEXT NOT NULL, last_clock TEXT NOT NULL);
            CREATE TABLE artifacts (identity TEXT PRIMARY KEY, reference BLOB NOT NULL);
            CREATE TABLE approvals (identity TEXT PRIMARY KEY, policy TEXT NOT NULL, approval BLOB NOT NULL, authentication_identity TEXT NOT NULL UNIQUE);
            CREATE TABLE applied_records (identity TEXT PRIMARY KEY, response BLOB NOT NULL);
            CREATE TRIGGER metadata_no_identity_update BEFORE UPDATE OF instance, ledger_id ON metadata BEGIN SELECT RAISE(ABORT, 'immutable owner identity'); END;
            CREATE TRIGGER metadata_no_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT, 'immutable owner identity'); END;
        """)
        for table in ("artifacts", "approvals", "applied_records"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only owner records'); END")
        connection.execute("INSERT INTO metadata VALUES (?,?,'')", (instance, ledger))
        connection.execute("CREATE TRIGGER metadata_no_insert BEFORE INSERT ON metadata BEGIN SELECT RAISE(ABORT, 'single owner identity'); END")
        connection.commit()
    finally:
        connection.close()


def _sign(challenge: bytes) -> tuple[dict, dict]:
    # Publicly documented synthetic private scalar 1, never a real credential.
    credential = b"ADMISSION_V2_SYNTHETIC_CREDENTIAL"
    x, y = CRYPTO.G
    public = struct.pack("<II", 0x31534345, 32) + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    cose = b"\xa5\x01\x02\x03\x26\x20\x01\x21\x58\x20" + x.to_bytes(32, "big") + b"\x22\x58\x20" + y.to_bytes(32, "big")
    rp = hashlib.sha256(CRYPTO.RP_ID.encode()).digest()
    registered = rp + b"\x45" + bytes(4) + bytes(16) + len(credential).to_bytes(2, "big") + credential + cose
    assertion = rp + b"\x05" + (1).to_bytes(4, "big")
    client = CRYPTO.client_json(challenge)
    message = assertion + hashlib.sha256(client).digest()
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    nonce = int.from_bytes(hashlib.sha256(b"SYNTHETIC_NONSECRET_NONCE" + message).digest(), "big") % (CRYPTO.N - 1) + 1
    point = CRYPTO._multiply(nonce, CRYPTO.G)
    r = point[0] % CRYPTO.N
    s = pow(nonce, -1, CRYPTO.N) * (digest + r) % CRYPTO.N

    def integer(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if raw[0] & 0x80:
            raw = b"\0" + raw
        return b"\x02" + bytes([len(raw)]) + raw

    pair = integer(r) + integer(s)
    signature = b"\x30" + bytes([len(pair)]) + pair
    def encode(raw):
        return base64.b64encode(raw).decode("ascii")
    return ({"id": encode(credential), "authenticator_data": encode(registered), "public_key": encode(public)},
            {"credential_id": encode(credential), "authenticator_data": encode(assertion),
             "signature": encode(signature), "client_data": encode(client)})


class Fixture:
    def __init__(self) -> None:
        global NEXT_CASE
        NEXT_CASE += 1
        self.root = ARTIFACT_ROOT / f"admission-v2-synthetic-{NEXT_CASE:04d}"
        self.root.mkdir()
        _new(self.root / "SYNTHETIC-ONLY", b"ADMISSION_V2_SYNTHETIC_NO_PRODUCT\n")
        self.store_root, self.copies = self.root / "store", self.root / "copies"
        for base in (self.store_root, self.copies):
            base.mkdir()
            for name in ("state", "authentication", "artifacts", "temp"):
                (base / name).mkdir()
        (self.store_root / "state" / "temp").mkdir()
        self.role_raw = (Path(__file__).parent / "fixtures" / "policy_owner_admission_v2" / "role-proposal.json").read_bytes()
        self.role = CORE.validate_role(self.role_raw)
        instance, ledger = self.role["instance"], "synthetic-fixed-ledger"
        self.owner = self.store_root / "state" / "owner.sqlite3"
        self.ledger = self.store_root / "state" / "lineage.sqlite3"
        _owner_database(self.owner, instance, ledger)
        # Match actual v1 EvidenceRef.to_bytes exactly: canonical JSON + LF.
        role_reference = CORE.canonical({"identity": CORE.ROLE_IDENTITY, "pin": CORE.ROLE_PIN}) + b"\n"
        connection = sqlite3.connect(self.owner)
        connection.execute("INSERT INTO artifacts VALUES (?,?)", (CORE.ROLE_IDENTITY, role_reference))
        connection.commit()
        connection.close()
        _new(self.store_root / "artifacts" / (CORE.ROLE_PIN["sha256"] + ".blob"), self.role_raw)
        _new(self.ledger, b"")
        connection = sqlite3.connect(self.ledger)
        connection.execute("CREATE TABLE metadata(schema_version TEXT,ledger_id TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('synthetic-v1',?)", (ledger,))
        connection.execute("CREATE TABLE synthetic_ledger_events(identity TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        legacy = {
            "administrators_and_system_are_trusted": True, "candidate_has_storage_write_access": False,
            "contract_version": "policy-owner-storage-binding-v1", "execution_authorized": False,
            "instance": instance, "ledger_identity": ledger, "ledger_object_id": "synthetic:ledger",
            "ledger_path": str(self.ledger), "native_owner_pin": CORE.pin(b"legacy-native-synthetic"),
            "operational": True, "operator_sid": self.role["operator_sid"],
            "original_registry_pin": CORE.pin(b"synthetic-original-registry"), "owner_root": str(self.store_root),
            "owner_root_descriptor_pin": CORE.pin(b"synthetic-descriptor"), "owner_root_object_id": "synthetic:root",
            "owner_state_object_id": "synthetic:owner", "owner_state_path": str(self.owner),
            "registration_is_approval": False, "service_name": "SYNTHETIC_NEVER_INSTALLED",
            "service_sid": "S-1-5-80-1-2-3-4-5", "trusted_worker_manifest_pin": CORE.pin(b"legacy-worker-synthetic"),
        }
        legacy = CORE.seal("policy-owner-storage-binding:v1", legacy)
        self.binding = CORE.make_binding(legacy, {"source_commit": "a" * 40,
            "native_pin": CORE.pin(b"new-native-synthetic"), "worker_manifest_pin": CORE.pin(b"new-worker-synthetic"),
            "build_id": "SYNTHETIC_TEST_ONLY"})
        self.frontend = {"pid": 12345, "creation_filetime": 133000000000000000,
                         "image_pin": self.binding["implementation"]["native_pin"]}
        self.now = datetime.now(timezone.utc)
        self.authority = CORE.SyntheticAuthority(self.root)
        self.store = CORE.EnrollmentOnlyStore(CORE.StorePaths(self.store_root), self.binding, self.role_raw,
                                              self.authority, clock=lambda: self.now)
        for source, name in ((self.owner, "baseline-owner.sqlite3"), (self.ledger, "baseline-lineage.sqlite3")):
            shutil.copyfile(source, self.copies / name)
        self.baseline = {"owner": CORE.pin((self.copies / "baseline-owner.sqlite3").read_bytes()),
                         "ledger": CORE.pin((self.copies / "baseline-lineage.sqlite3").read_bytes())}
        self.original_ledger = self.ledger.read_bytes()
        self.original_object = self.owner.stat().st_ino
        self.copy_current()

    def copy_current(self) -> None:
        for source, name in ((self.owner, "owner.sqlite3"), (self.ledger, "lineage.sqlite3")):
            target = self.copies / name
            if target.exists():
                target.unlink()  # Only this explicitly synthetic copy, never source.
            shutil.copyfile(source, target)
        for path in (self.store_root / "authentication").iterdir():
            shutil.copyfile(path, self.copies / "authentication" / path.name)
        for path in (self.store_root / "artifacts").iterdir():
            shutil.copyfile(path, self.copies / "artifacts" / path.name)

    def classify(self):
        return CORE.classify_legacy_copies(self.copies, self.binding, self.role_raw, self.authority, self.baseline)

    def activate(self) -> None:
        classification = self.classify()
        if classification.kind != "ELIGIBLE":
            raise AssertionError(classification.evidence)
        self.store.migrate_closed(classification)
        self.store.stage_binding()
        self.store.activate_closed()

    def reserve(self) -> dict:
        return self.store.reserve(self.store.inspect()["epoch"], (self.now + timedelta(minutes=10)).isoformat(), self.frontend)

    def install_synthetic_legacy_enrollment(self, *, derived: bool = True) -> dict:
        legacy = self.binding["legacy_setup_binding"]
        nonce = "b" * 32
        challenge = {"contract_version": "policy-owner-human-challenge-v1", "instance": legacy["instance"],
                     "storage_binding_identity": legacy["identity"], "operator_sid": legacy["operator_sid"],
                     "purpose": "ENROLL_PROJECT_ROLE", "document_sha256": CORE.ROLE_PIN["sha256"], "nonce": nonce,
                     "issued_utc": (self.now - timedelta(minutes=1)).isoformat(),
                     "expires_utc": (self.now + timedelta(minutes=9)).isoformat()}
        digest = hashlib.sha256(CORE.canonical(challenge)).digest()
        registration, proof = _sign(digest)
        record = CORE.seal("policy-owner-authenticated-human-record:v1", {
            "contract_version": "policy-owner-authenticated-human-record-v1", "instance": legacy["instance"],
            "operator_sid": legacy["operator_sid"], "purpose": "ENROLL_PROJECT_ROLE", "document": self.role,
            "challenge_id": nonce, "challenge": base64.b64encode(digest).decode("ascii"),
            "challenge_document": challenge, "credential": registration, "proof": proof,
            "authenticator_counter": 1, "recorded_at_utc": self.now.isoformat(),
            "storage_binding_identity": legacy["identity"], "predecessor": None,
            "elevated_native_frontend_pin": legacy["native_owner_pin"]["sha256"], "execution_authorized": False})
        raw = CORE.canonical(record)
        _new(self.store_root / "authentication" / "00000000.json", raw)
        if derived:
            _new(self.store_root / "artifacts" / (CORE.pin(raw)["sha256"] + ".blob"), raw)
            connection = sqlite3.connect(self.owner)
            connection.execute("INSERT INTO artifacts VALUES (?,?)", (record["identity"],
                CORE.canonical({"identity": record["identity"], "pin": CORE.pin(raw)}) + b"\n"))
            connection.execute("INSERT INTO applied_records VALUES (?,?)", (record["identity"], CORE.canonical({
                "status": "AUTHENTICATED_OWNER_RECORD_RETAINED", "authentication_identity": record["identity"],
                "execution_authorized": False})))
            connection.commit()
            connection.close()
        self.copy_current()
        return record

    def begun(self, challenge: dict) -> tuple[dict, dict]:
        receipt = self.store.begin_creation(challenge["epoch"], challenge["attempt_id"], self.frontend)
        self.authority.attest_synthetic_creation(self.frontend, challenge, receipt["creation_receipt"])
        return _sign(base64.b64decode(challenge["challenge"]))

    def accept(self, challenge: dict, registration: dict, proof: dict) -> dict:
        return self.store.accept(challenge["epoch"], challenge["attempt_id"], registration, proof, self.frontend)

    def new_lease(self):
        self.authority.live = False
        self.authority = CORE.SyntheticAuthority(self.root)
        self.store = CORE.EnrollmentOnlyStore(CORE.StorePaths(self.store_root), self.binding, self.role_raw,
                                              self.authority, clock=lambda: self.now)
        return self.store

    def assert_preserved(self, test: unittest.TestCase) -> None:
        test.assertEqual(self.ledger.read_bytes(), self.original_ledger)
        test.assertEqual(self.owner.stat().st_ino, self.original_object)
        connection = sqlite3.connect(self.owner.as_uri() + "?mode=ro", uri=True)
        try:
            test.assertEqual(connection.execute("SELECT instance,ledger_id,last_clock FROM metadata").fetchall(),
                             [(self.role["instance"], "synthetic-fixed-ledger", "")])
            test.assertEqual(connection.execute("SELECT COUNT(*) FROM approvals").fetchone(), (0,))
        finally:
            connection.close()


class AdmissionTests(unittest.TestCase):
    def test_v2_clock_regression_blocks_without_rewinding_floor(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        registration, proof = fixture.begun(challenge)
        floor = fixture.now.isoformat(timespec="microseconds")
        fixture.now -= timedelta(minutes=1)
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.accept(challenge, registration, proof)
        connection = sqlite3.connect(fixture.owner.as_uri()+"?mode=ro", uri=True)
        self.assertEqual(connection.execute("SELECT phase,last_utc FROM admission_v2_control").fetchone(), ("BLOCKED", floor))
        connection.close()

    def test_two_concurrent_frontends_can_reserve_only_one_attempt(self):
        fixture = Fixture()
        fixture.activate()
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve(pid):
            barrier.wait(timeout=2)
            try:
                value = fixture.store.reserve(0, (fixture.now+timedelta(minutes=10)).isoformat(),
                                              {**fixture.frontend, "pid": pid})
                outcomes.append(("reserved", value))
            except (CORE.AdmissionRejected, sqlite3.Error) as error:
                outcomes.append(("rejected", type(error).__name__))

        threads = [threading.Thread(target=reserve, args=(pid,), daemon=True) for pid in (12345, 54321)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "synthetic reservation thread exceeded bound")
        self.assertEqual(sorted(value[0] for value in outcomes), ["rejected", "reserved"])
        connection = sqlite3.connect(fixture.owner.as_uri()+"?mode=ro", uri=True)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM admission_v2_attempts").fetchone(), (1,))
        connection.close()

    def test_acceptance_rejects_changed_raw_outputs_and_snapshot_binding(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        registration, proof = fixture.begun(challenge)
        fixture.accept(challenge, registration, proof)
        raw = fixture.store.paths.authentication.read_bytes()
        record = CORE.strict_json(raw, canonical_required=True)
        context = {key: challenge["challenge_document"][key] for key in
                   ("migration_cut_identity", "classified_state_identity")}
        wrong = dict(context)
        wrong["classified_state_identity"] = "wrong-classified-state"
        with self.assertRaises(CORE.AdmissionRejected):
            CORE.validate_v2_record(record, fixture.binding, fixture.role_raw, **wrong)
        # Deliberate corruption of only this synthetic fixture; no real record.
        fixture.store.paths.authentication.write_bytes(raw + b"\n")
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.verify_complete()

    def test_real_v1_reference_format_and_existing_acceptance_without_reenroll(self):
        fixture = Fixture()
        record = fixture.install_synthetic_legacy_enrollment()
        classification = fixture.classify()
        self.assertEqual(classification.kind, "EXISTING", classification.evidence)
        before = fixture.owner.read_bytes()
        self.assertEqual(classification.evidence["existing_authentication_identity"], record["identity"])
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.migrate_closed(classification)
        self.assertEqual(fixture.owner.read_bytes(), before)
        fixture.assert_preserved(self)

    def test_accepted_but_not_derived_v1_remains_blocked(self):
        fixture = Fixture()
        fixture.install_synthetic_legacy_enrollment(derived=False)
        classification = fixture.classify()
        self.assertEqual(classification.kind, "BLOCKED")
        self.assertEqual(classification.evidence["accepted_authentication"], "VERIFIED_LEGACY_V1")
        before = fixture.owner.read_bytes()
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.migrate_closed(classification)
        self.assertEqual(fixture.owner.read_bytes(), before)

    def test_closed_handoff_is_explicit_and_requires_released_prior_lease(self):
        fixture = Fixture()
        fixture.activate()
        old = fixture.authority
        other = CORE.SyntheticAuthority(fixture.root)
        contender = CORE.EnrollmentOnlyStore(CORE.StorePaths(fixture.store_root), fixture.binding, fixture.role_raw,
                                             other, clock=lambda: fixture.now)
        with self.assertRaises(CORE.AdmissionRejected):
            contender.adopt_closed(0)
        old.live = False
        result = contender.adopt_closed(0)
        self.assertEqual(result["epoch"], 1)
        challenge = contender.reserve(1, (fixture.now + timedelta(minutes=10)).isoformat(), fixture.frontend)
        self.assertEqual(challenge["epoch"], 2)
        with self.assertRaises(CORE.AdmissionRejected):
            contender.adopt_closed(2)

    def test_complete_new_enrollment_preserves_business_and_ledger(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        registration, proof = fixture.begun(challenge)
        result = fixture.accept(challenge, registration, proof)
        self.assertEqual(result["phase"], "COMPLETE_STOP")
        record = CORE.strict_json(fixture.store.paths.authentication.read_bytes(), canonical_required=True)
        _new(ARTIFACT_ROOT / "interop-vector.json", CORE.canonical({
            "context": "SYNTHETIC_TEST_ONLY", "challenge": challenge,
            "creation_receipt": record["creation_receipt"], "operator_sid": fixture.role["operator_sid"],
            "expected_user_handle": challenge["user_handle"],
            "registration": registration, "proof": proof,
        }))
        self.assertEqual(fixture.store.verify_complete()["status"], "CONTEXT_ESTABLISHED_STOP")
        self.assertEqual(fixture.new_lease().verify_complete()["status"], "CONTEXT_ESTABLISHED_STOP")
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.reserve(2, (fixture.now + timedelta(minutes=10)).isoformat(), fixture.frontend)
        fixture.assert_preserved(self)

    def test_two_frontends_stale_epoch_and_duplicate_requests(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.reserve(0, (fixture.now + timedelta(minutes=10)).isoformat(),
                                  {**fixture.frontend, "pid": 54321})
        self.assertEqual(fixture.store.inspect()["phase"], "RESERVED")
        registration, proof = fixture.begun(challenge)
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.accept(0, challenge["attempt_id"], registration, proof, fixture.frontend)
        self.assertEqual(fixture.store.inspect()["phase"], "CREATION_BEGUN")
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.begin_creation(challenge["epoch"], challenge["attempt_id"], fixture.frontend)
        fixture.accept(challenge, registration, proof)
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.accept(challenge, registration, proof)
        self.assertEqual(fixture.store.inspect()["phase"], "COMPLETE_STOP")

    def test_late_proof_blocks_without_publication(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        registration, proof = fixture.begun(challenge)
        fixture.now += timedelta(minutes=11)
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.accept(challenge, registration, proof)
        self.assertEqual(fixture.store.inspect()["phase"], "BLOCKED")
        self.assertFalse(fixture.store.paths.authentication.exists())

    def test_changed_lease_blocks_interrupted_active_attempt(self):
        fixture = Fixture()
        fixture.activate()
        fixture.reserve()
        self.assertEqual(fixture.new_lease().inspect()["phase"], "BLOCKED")
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.reserve()

    def test_creation_parameters_are_not_proven_by_signature(self):
        fixture = Fixture()
        fixture.activate()
        challenge = fixture.reserve()
        fixture.store.begin_creation(challenge["epoch"], challenge["attempt_id"], fixture.frontend)
        registration, proof = _sign(base64.b64decode(challenge["challenge"]))
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.accept(challenge, registration, proof)
        self.assertEqual(fixture.store.inspect()["phase"], "BLOCKED")
        self.assertFalse(fixture.store.paths.authentication.exists())

    def test_h2_is_distinct_and_bound_to_same_principal(self):
        one, two = Fixture(), Fixture()
        one.activate()
        two.activate()
        first, second = one.reserve(), two.reserve()
        self.assertNotEqual(first["user_handle"], second["user_handle"])
        self.assertEqual(len(base64.b64decode(first["user_handle"])), 32)
        self.assertEqual(first["challenge_document"]["operator_sid"], one.role["operator_sid"])
        self.assertEqual(first["challenge_document"]["business_candidate"], CORE.BUSINESS_CANDIDATE)

    def test_crypto_negative_flags_der_cose_and_client_binding(self):
        challenge = hashlib.sha256(b"SYNTHETIC_V2_VECTOR").digest()
        registration, proof = _sign(challenge)
        self.assertEqual(CRYPTO.verify_proof(challenge, registration, proof), 1)
        for field, raw in (("authenticator_data", bytes(37)), ("signature", b"\x30\x00"),
                           ("client_data", b'{"signature_valid":true}')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                CRYPTO.verify_proof(challenge, registration,
                    {**proof, field: base64.b64encode(raw).decode("ascii")})
        changed = bytearray(base64.b64decode(registration["authenticator_data"]))
        changed[-1] ^= 1
        with self.assertRaises(ValueError):
            CRYPTO.verify_proof(challenge, {**registration, "authenticator_data": base64.b64encode(changed).decode("ascii")}, proof)
        with self.assertRaises(ValueError):
            CRYPTO.verify_proof(challenge, registration, {**proof, "signature_valid": True})

    def test_closed_binding_and_nonfinite_bool_inputs(self):
        fixture = Fixture()
        with self.assertRaises(CORE.AdmissionRejected):
            CORE.validate_binding({**fixture.binding, "verified": True})
        with self.assertRaises(CORE.AdmissionRejected):
            CORE.strict_json(b'{"x":NaN}')
        with self.assertRaises(CORE.AdmissionRejected):
            CORE.strict_json(b'{"x":1,"x":2}')
        fixture.activate()
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.reserve(True, (fixture.now + timedelta(minutes=10)).isoformat(), fixture.frontend)

    def test_migration_partial_is_closed_and_no_fallback(self):
        fixture = Fixture()
        fixture.store.migrate_closed(fixture.classify())
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.reserve()
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.migrate_closed(fixture.classify())
        fixture.new_lease()
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.stage_binding()
        self.assertEqual(fixture.store.inspect()["gate"], "CLOSED")

    def test_cut_declarations_and_orphans_cannot_grant_eligibility(self):
        fixture = Fixture()
        fixture.authority.cut_phases = ("SCM_STOPPED", "CURRENT_PROCESS_SCAN_EMPTY")
        self.assertEqual(fixture.classify().kind, "BLOCKED")
        fixture.authority.cut_phases = CORE.MIGRATION_PHASES[:3]
        _new(fixture.copies / "temp" / "orphan", b"partial")
        self.assertEqual(fixture.classify().kind, "BLOCKED")
        fabricated = CORE.LegacyClassification("ELIGIBLE", {"classification": "ELIGIBLE"}, fixture.authority, fixture.binding["identity"])
        with self.assertRaises(CORE.AdmissionRejected):
            fixture.store.migrate_closed(fabricated)

    def test_advanced_legacy_clock_is_not_initial_empty_clock_requirement(self):
        fixture = Fixture()
        connection = sqlite3.connect(fixture.copies / "owner.sqlite3")
        connection.execute("UPDATE metadata SET last_clock=?", ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),))
        connection.commit()
        connection.close()
        self.assertEqual(fixture.classify().kind, "ELIGIBLE")

    def test_formal_worker_has_no_synthetic_fallback_or_legacy_dispatch(self):
        fixture = Fixture()
        frame = {"contract_version": "policy-owner-trusted-worker-request-v2", "mode": "ENROLLMENT_ONLY",
                 "anchor_v2": fixture.binding, "lease_id": fixture.authority.lease_owner_id,
                 "operation": "inspect-v2", "payload": {}}
        with self.assertRaises(CORE.AdmissionRejected):
            CORE.dispatch_v2(frame, fixture.store_root, fixture.authority)
        for operation in ("worker", "apply-authenticated-record", "resolve", "claim", "reconciliation", "migrate-closed-v2"):
            with self.subTest(operation=operation), self.assertRaises(CORE.AdmissionRejected):
                CORE.dispatch_v2({**frame, "operation": operation}, fixture.store_root, fixture.authority)
        worker = Path(__file__).parents[1] / "scripts" / "policy_owner" / "owner_worker.py"
        source = worker.read_text(encoding="utf-8")
        for forbidden in ("LocalWindowsPolicyOwner", "reconcile_authenticated_records", "policy_qualified", "windows_policy_owner"):
            self.assertNotIn(forbidden, source)

    def test_each_persistence_boundary_crash_preserves_and_blocks(self):
        for point in CORE.FAULT_POINTS:
            if point.startswith(("handoff_", "block_")):
                continue
            with self.subTest(point=point):
                fixture = Fixture()

                def fault(current, selected=point):
                    if current == selected:
                        raise CORE.InjectedCrash(selected)

                fixture.store.fault = fault
                with self.assertRaises(CORE.InjectedCrash):
                    fixture.activate()
                    challenge = fixture.reserve()
                    registration, proof = fixture.begun(challenge)
                    fixture.accept(challenge, registration, proof)
                fixture.store.fault = None
                fixture.new_lease()
                if point == "migration_before_schema":
                    with self.assertRaises(sqlite3.Error):
                        fixture.store.inspect()
                elif point in {"migration_after_schema", "migration_after_binding", "migration_after_activation", "reserve_before_commit"}:
                    self.assertEqual(fixture.store.inspect()["gate"], "CLOSED")
                    with self.assertRaises(CORE.AdmissionRejected):
                        fixture.reserve()
                elif point == "complete_after_commit":
                    self.assertEqual(fixture.store.verify_complete()["status"], "CONTEXT_ESTABLISHED_STOP")
                else:
                    self.assertEqual(fixture.store.inspect()["phase"], "BLOCKED")
                    with self.assertRaises(CORE.AdmissionRejected):
                        fixture.store.verify_complete()
                fixture.assert_preserved(self)

    def test_closed_handoff_and_block_durable_faults(self):
        for point in (p for p in CORE.FAULT_POINTS if p.startswith(("handoff_", "block_"))):
            with self.subTest(point=point):
                fixture = Fixture()
                fixture.activate()
                if point.startswith("block_"):
                    fixture.reserve()
                fixture.new_lease()

                def fault(current, selected=point):
                    if current == selected:
                        raise CORE.InjectedCrash(selected)

                fixture.store.fault = fault
                with self.assertRaises(CORE.InjectedCrash):
                    if point.startswith("block_"):
                        fixture.store.inspect()
                    else:
                        fixture.store.adopt_closed(0)
                fixture.store.fault = None
                fixture.new_lease()
                state = fixture.store.inspect()
                self.assertEqual(state["gate"], "CLOSED")
                self.assertEqual(state["phase"], "BLOCKED" if point.startswith("block_") else "CLOSED")
                with self.assertRaises(CORE.AdmissionRejected):
                    fixture.reserve()
                fixture.assert_preserved(self)

    def test_maintenance_protocol_order_stops_and_activation_publication(self):
        from tradingagents.policy_owner_admission_migration import MaintenanceCoordinator

        class FakeStore:
            def __init__(self, backend):
                self.backend = backend

            def migrate_closed(self, classification):
                if classification.kind != "ELIGIBLE":
                    raise AssertionError("noneligible migration")
                self.backend.event("migrate")
                return {"status": "SCHEMA_MIGRATED_CLOSED"}

            def stage_binding(self):
                self.backend.event("stage_binding")
                return {"status": "NEW_BINDING_STAGED"}

            def activate_closed(self):
                self.backend.event("activate")
                return {"status": "V2_ACTIVATED_CLOSED"}

        class FakePlatform:
            context = "SYNTHETIC_PROTOCOL_TEST"

            def __init__(self, *, kind="ELIGIBLE", fail=None, same_boot=False):
                self.events = []
                self.kind, self.fail, self.same_boot = kind, fail, same_boot
                self.closed = False

            def event(self, name):
                self.events.append(name)
                if self.fail == name:
                    raise RuntimeError("SYNTHETIC_PROTOCOL_FAULT " + name)

            def validate_authorization(self, step): self.event("authorize_" + step)
            def prepare_control_new(self): self.event("new_control")
            def open_control_existing(self): self.event("existing_control")
            def acquire_gate(self): self.event("exclusive_gate")
            def observe_boot(self):
                self.event("observe_boot")
                return "old" if self.same_boot else "new"
            def journal(self, phase, evidence): self.event("journal_" + phase)
            def change_startup(self, start): self.event("change_startup_" + str(start))
            def observe_startup(self, start):
                self.event("observe_startup_" + str(start))
                return {"synthetic_start": start}
            def observe_legacy_startup(self):
                self.event("observe_exact_legacy_startup_3")
                return {"synthetic_original_start": 3}
            def save_guard(self, old_boot, observed):
                self.event("save_guard")
                return {"identity": "synthetic-guard"}
            def load_guard(self):
                self.event("load_guard")
                return {"identity": "synthetic-guard", "old_boot_id": "old"}
            def save_boot(self, guard, boot, observed):
                self.event("save_boot")
                return {"identity": "synthetic-boot"}
            def capture_copies(self, guard, boot):
                self.event("capture_copies")
                return "SYNTHETIC_NO_REAL_PATH"
            def classify(self, copies):
                self.event("classify")
                return types.SimpleNamespace(kind=self.kind, evidence={"context": self.context, "classification": self.kind})
            def save_classification(self, classification): self.event("save_classification")
            def release_copy_fences(self): self.event("release_copy_fences_keep_gate")
            def store(self): return FakeStore(self)
            def stage_registration(self): self.event("stage_registration")
            def verify_registration(self): self.event("verify_registration")
            def finalize_activation(self, observed):
                self.event("finalize_activation")
                return {"identity": "synthetic-activation"}
            def close(self):
                self.events.append("close_owned_handles")
                self.closed = True

        prepare = FakePlatform()
        with self.assertRaises(CORE.AdmissionRejected):
            MaintenanceCoordinator(prepare)
        result = MaintenanceCoordinator(prepare, synthetic_protocol_test=True).run("prepare_guard")
        self.assertTrue(result["status"].startswith("GUARD_ESTABLISHED"))
        self.assertLess(prepare.events.index("observe_exact_legacy_startup_3"), prepare.events.index("journal_GUARD_CHANGE_INTENT"))
        self.assertLess(prepare.events.index("journal_GUARD_CHANGE_INTENT"), prepare.events.index("change_startup_4"))
        self.assertNotIn("change_startup_3", prepare.events)
        successful = FakePlatform()
        result = MaintenanceCoordinator(successful, synthetic_protocol_test=True).run("post_boot")
        self.assertTrue(result["status"].startswith("V2_ACTIVATED_CLOSED"))
        for first, second in (("classify", "migrate"), ("migrate", "stage_registration"),
                              ("stage_registration", "stage_binding"), ("activate", "change_startup_3"),
                              ("observe_startup_3", "finalize_activation"),
                              ("finalize_activation", "journal_V2_ACTIVATED_CLOSED")):
            self.assertLess(successful.events.index(first), successful.events.index(second))
        for kind in ("EXISTING", "BLOCKED"):
            fake = FakePlatform(kind=kind)
            stopped = MaintenanceCoordinator(fake, synthetic_protocol_test=True).run("post_boot")
            self.assertTrue(stopped["status"].startswith("STOP_" + kind))
            self.assertNotIn("migrate", fake.events)
            self.assertNotIn("stage_registration", fake.events)
            self.assertNotIn("change_startup_3", fake.events)
        same = FakePlatform(same_boot=True)
        with self.assertRaises(CORE.AdmissionRejected):
            MaintenanceCoordinator(same, synthetic_protocol_test=True).run("post_boot")
        self.assertNotIn("capture_copies", same.events)
        for step, failure in (("prepare_guard", "change_startup_4"), ("prepare_guard", "save_guard"),
                              *(("post_boot", point) for point in ("save_boot", "capture_copies", "classify", "migrate",
                                  "stage_registration", "activate", "change_startup_3", "finalize_activation"))):
            with self.subTest(step=step, failure=failure):
                fake = FakePlatform(fail=failure)
                with self.assertRaises(RuntimeError):
                    MaintenanceCoordinator(fake, synthetic_protocol_test=True).run(step)
                self.assertTrue(fake.closed)
                self.assertEqual(fake.events[-2:], [failure, "close_owned_handles"])


def main() -> int:
    global ARTIFACT_ROOT
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("standalone synthetic batch requires -I -B -S")
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True, type=Path)
    args = parser.parse_args()
    if not args.artifact_root.is_absolute():
        raise RuntimeError("absolute new synthetic evidence root required")
    args.artifact_root.mkdir()
    ARTIFACT_ROOT = args.artifact_root
    _load()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdmissionTests)
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    report = {"context": "SYNTHETIC_TEST_ONLY", "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "synthetic_stores": NEXT_CASE, "windows_platform_or_product_calls": False, "product_execution": False,
              "installed_store_access": False, "retry": False}
    _new(ARTIFACT_ROOT / "synthetic-result.json", json.dumps(report, sort_keys=True).encode("utf-8"))
    print(json.dumps(report, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
