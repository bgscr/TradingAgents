"""Offline contract checks, separate from pending real Windows Hello enrollment."""

from __future__ import annotations

import base64
import runpy
import sqlite3
from pathlib import Path

import pytest

from tradingagents.policy_owner_contract import evidence_intake_entries
from tradingagents.policy_qualified_contract import BytePin, EvidenceRef, content_identity
from tradingagents.windows_policy_owner import LocalWindowsPolicyOwner, require_service_token


@pytest.mark.windows
def test_current_user_cannot_be_the_registered_service_owner():
    # Native token query, no impersonation, account creation or enrollment.
    with pytest.raises(ValueError, match="exact dedicated virtual service account"):
        require_service_token("S-1-5-80-1-2-3-4-5")


def test_owner_database_starts_empty_and_rejects_identity_and_record_rewrite(tmp_path):
    initialize = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "scripts/policy_owner/prepare_policy_owner_setup.py"
        )
    )["initialize_owner_state"]
    path = tmp_path / "TEST-owner.sqlite3"
    initialize(path, "test-20010101-001", "TEST-unclaimed-ledger")
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT * FROM metadata").fetchall() == [
            ("test-20010101-001", "TEST-unclaimed-ledger", "")
        ]
        for table in ("approvals", "artifacts", "applied_records"):
            assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        for sql in (
            "UPDATE metadata SET instance='operational-20010101-001'",
            "UPDATE metadata SET ledger_id='replacement'",
            "DELETE FROM metadata",
            "INSERT INTO metadata VALUES ('replacement','replacement','')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(sql)
        database.execute("UPDATE metadata SET last_clock='2001-01-01T00:00:00Z'")
        database.execute("INSERT INTO applied_records VALUES ('TEST-auth-record', x'01')")
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE applied_records SET response=x'02'")
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("DELETE FROM applied_records")
    with pytest.raises(FileExistsError):
        initialize(path, "test-20010101-002", "TEST-replacement-ledger")


def test_authenticated_intake_batch_rejects_duplicate_nested_or_extra_entries():
    entry = {
        "contract_version": "policy-owner-evidence-intake-v1",
        "kind": "artifact",
        "key": "TEST-only",
        "value": {},
    }
    batch = {
        "contract_version": "policy-owner-evidence-intake-batch-v1",
        "entries": [entry],
        "execution_authorized": False,
    }
    assert evidence_intake_entries(batch) == (entry,)
    for entries in ([entry, entry], [batch], [], [{**entry, "execution_authorized": True}]):
        with pytest.raises(ValueError):
            evidence_intake_entries({**batch, "entries": entries})
    with pytest.raises(ValueError):
        evidence_intake_entries({**batch, "execution_authorized": True})


@pytest.mark.windows
def test_blob_publication_and_conflicting_intake_before_authentication(tmp_path):
    # Storage primitive only. Bypass constructor ONLY in this explicit offline
    # fixture; the real constructor remains token-gated and is tested above.
    # Nothing is enrolled and no operation reaches the native service.
    owner = object.__new__(LocalWindowsPolicyOwner)
    owner.root = tmp_path
    owner.anchor = {"instance": "test-20010101-001", "ledger_identity": "TEST-storage-only"}
    owner.records = []
    owner.db_path = tmp_path / "state" / "owner.sqlite3"
    (tmp_path / "state" / "temp").mkdir(parents=True)
    (tmp_path / "artifacts").mkdir()
    initialize = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "scripts/policy_owner/prepare_policy_owner_setup.py"
        )
    )["initialize_owner_state"]
    initialize(owner.db_path, owner.anchor["instance"], owner.anchor["ledger_identity"])
    interrupted = tmp_path / "state" / "temp" / "unpublished-TEST.tmp"
    interrupted.write_bytes(b"INTERRUPTED TEST WRITE")
    raw = b"TEST exact immutable bytes"
    identity = content_identity("test-artifact:v1", {"name": "fixture"})
    ref = owner.put(raw, identity)
    assert owner.put(raw, identity) == ref
    assert owner.read_artifact(ref) == raw
    assert interrupted.read_bytes() == b"INTERRUPTED TEST WRITE"
    assert sorted(path.name for path in (tmp_path / "artifacts").iterdir()) == [
        ref.pin.sha256 + ".blob"
    ]
    changed = b"DIFFERENT TEST BYTES"
    document = {
        "contract_version": "policy-owner-evidence-intake-v1",
        "kind": "artifact",
        "key": identity,
        "value": {
            "reference": EvidenceRef(identity=identity, pin=BytePin.of(changed)).model_dump(
                mode="json"
            ),
            "bytes_base64": base64.b64encode(changed).decode("ascii"),
        },
    }
    with pytest.raises(ValueError, match="cannot rebind an existing setup or owner artifact"):
        owner._validate_intake(document)
    assert owner.records == [] and owner.read_artifact(ref) == raw
