"""Durable, exclusive one-lineage consumption for a trusted coordinator.

No implicit database creation, reset, retry, or stage execution. Provisioning is
an explicit owner operation before approval. The registered ledger must live in
the owner's protected storage; these transactions do not authenticate its owner
or defend against an administrator replacing the entire storage volume.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tradingagents.policy_coverage_models import read_model
from tradingagents.policy_qualified_contract import (
    STAGES,
    EvidenceRef,
    LineageClaim,
    canonical_bytes,
    content_identity,
    strict_json,
    utc,
)

_SCHEMA = "policy-qualified-lineage-ledger-v1"


class LineageLedger:
    """An already provisioned ledger, selected by trusted owner configuration."""

    def __init__(self, path: Path, ledger_id: str) -> None:
        self.path = path.resolve(strict=True)
        self.ledger_id = ledger_id
        if path.is_symlink() or not self.path.is_file():
            raise ValueError("ledger must be an existing regular owner file")
        with self._connection() as connection:
            self._metadata(connection)

    @classmethod
    def provision(cls, path: Path, ledger_id: str) -> LineageLedger:
        """Explicit trusted-owner setup; never called by parsing or admission.

        A failed provision remains in place and unusable. It is not cleaned up or
        recreated by this module. Tests use only labelled synthetic temp ledgers.
        """
        if not ledger_id or not path.parent.is_dir():
            raise ValueError("owner ledger identity and existing parent are required")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE metadata (schema_version TEXT NOT NULL, ledger_id TEXT NOT NULL);
                CREATE TABLE claims (
                    approval TEXT PRIMARY KEY, plan TEXT NOT NULL UNIQUE,
                    lineage TEXT NOT NULL UNIQUE, payload BLOB NOT NULL
                );
                CREATE TABLE events (
                    lineage TEXT NOT NULL REFERENCES claims(lineage), ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL, recorded_at TEXT NOT NULL, payload BLOB NOT NULL,
                    predecessor TEXT NOT NULL, identity TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(lineage, ordinal)
                );
                CREATE TABLE invalidations (
                    approval TEXT PRIMARY KEY, plan TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE allocations (
                    kind TEXT NOT NULL, value TEXT NOT NULL,
                    lineage TEXT NOT NULL REFERENCES claims(lineage),
                    PRIMARY KEY(kind, value)
                );
                CREATE TRIGGER claims_no_update BEFORE UPDATE ON claims BEGIN
                    SELECT RAISE(ABORT, 'append-only claims'); END;
                CREATE TRIGGER claims_no_delete BEFORE DELETE ON claims BEGIN
                    SELECT RAISE(ABORT, 'append-only claims'); END;
                CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'append-only events'); END;
                CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'append-only events'); END;
                CREATE TRIGGER invalidations_no_update BEFORE UPDATE ON invalidations BEGIN
                    SELECT RAISE(ABORT, 'terminal invalidation'); END;
                CREATE TRIGGER invalidations_no_delete BEFORE DELETE ON invalidations BEGIN
                    SELECT RAISE(ABORT, 'terminal invalidation'); END;
                CREATE TRIGGER allocations_no_update BEFORE UPDATE ON allocations BEGIN
                    SELECT RAISE(ABORT, 'consumed allocation'); END;
                CREATE TRIGGER allocations_no_delete BEFORE DELETE ON allocations BEGIN
                    SELECT RAISE(ABORT, 'consumed allocation'); END;
            """)
            connection.execute("INSERT INTO metadata VALUES (?, ?)", (_SCHEMA, ledger_id))
            connection.commit()
        finally:
            connection.close()
        return cls(path, ledger_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # mode=rw is essential: a missing or moved ledger must not become empty.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._metadata(connection)
            yield connection
        finally:
            connection.close()

    def _metadata(self, connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT schema_version, ledger_id FROM metadata").fetchall() != [
            (_SCHEMA, self.ledger_id)
        ]:
            raise ValueError("missing/conflicting owner ledger registration")

    @staticmethod
    def _not_invalidated(connection: sqlite3.Connection, approval: str, plan: str) -> None:
        if connection.execute(
            "SELECT 1 FROM invalidations WHERE approval=? OR plan=?", (approval, plan)
        ).fetchone():
            raise ValueError("approval/plan permanently invalidated; no retry")

    def _events(self, connection: sqlite3.Connection, claim: LineageClaim) -> list[dict]:
        row = connection.execute(
            "SELECT payload FROM claims WHERE lineage=?", (claim.lineage_identity,)
        ).fetchone()
        if row is None or read_model(LineageClaim, row[0]) != claim:
            raise ValueError("missing or conflicting consumed lineage")
        records = []
        previous = claim.lineage_identity
        for ordinal, kind, recorded_at, payload, predecessor, identity in connection.execute(
            "SELECT ordinal, kind, recorded_at, payload, predecessor, identity FROM events "
            "WHERE lineage=? ORDER BY ordinal",
            (claim.lineage_identity,),
        ):
            body = {
                "lineage": claim.lineage_identity,
                "ordinal": ordinal,
                "kind": kind,
                "recorded_at": recorded_at,
                "payload": strict_json(payload),
                "predecessor": predecessor,
            }
            if (
                ordinal != len(records)
                or predecessor != previous
                or identity != content_identity("policy-qualified-lineage-event:v1", body)
            ):
                raise ValueError("lineage ledger is incomplete or altered")
            utc(recorded_at)
            records.append({**body, "identity": identity})
            previous = identity
        if not records or records[0]["kind"] != "CLAIMED":
            raise ValueError("lineage reservation lacks its consumed-attempt record")
        return records

    def _append(
        self,
        connection: sqlite3.Connection,
        claim: LineageClaim,
        records: list[dict],
        kind: str,
        at: str,
        payload: object,
    ) -> str:
        utc(at)
        if records and utc(at) < utc(records[-1]["recorded_at"]):
            raise ValueError("lineage clock moved backwards")
        body = {
            "lineage": claim.lineage_identity,
            "ordinal": len(records),
            "kind": kind,
            "recorded_at": at,
            "payload": payload,
            "predecessor": records[-1]["identity"] if records else claim.lineage_identity,
        }
        identity = content_identity("policy-qualified-lineage-event:v1", body)
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                claim.lineage_identity,
                len(records),
                kind,
                at,
                canonical_bytes(payload),
                body["predecessor"],
                identity,
            ),
        )
        return identity

    def claim(self, value: LineageClaim) -> None:
        claim = read_model(LineageClaim, value)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._not_invalidated(
                connection, claim.approval_identity, claim.lineage_plan.lineage_plan_identity
            )
            try:
                connection.execute(
                    "INSERT INTO claims VALUES (?, ?, ?, ?)",
                    (
                        claim.approval_identity,
                        claim.lineage_plan.lineage_plan_identity,
                        claim.lineage_identity,
                        claim.to_bytes(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("approval or lineage plan already consumed; no retry") from exc
            roots = [stage.root.casefold() for stage in claim.stages]
            roots.append(claim.pre_result_evidence_root.casefold())
            previous_roots = [
                row[0]
                for row in connection.execute("SELECT value FROM allocations WHERE kind='root'")
            ]
            if any(
                a == b or a.startswith(b + "\\") or b.startswith(a + "\\")
                for a in roots
                for b in previous_roots
            ):
                raise ValueError("stage/evidence root was already reserved by a consumed claim")
            allocated = [("root", root) for root in roots]
            allocated.extend(("run_id", stage.run_id.casefold()) for stage in claim.stages)
            allocated.append(("baseline", claim.additive_baseline_version.casefold()))
            try:
                connection.executemany(
                    "INSERT INTO allocations VALUES (?, ?, ?)",
                    ((kind, value, claim.lineage_identity) for kind, value in allocated),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("stage ID/baseline allocation was already consumed") from exc
            self._append(connection, claim, [], "CLAIMED", claim.claimed_at_utc, {})

    def events(self, claim: LineageClaim) -> tuple[dict, ...]:
        with self._connection() as connection:
            return tuple(self._events(connection, read_model(LineageClaim, claim)))

    def transition(
        self, claim: LineageClaim, *, kind: str, at: str, evidence: EvidenceRef | None = None
    ) -> str:
        """Atomic compare-and-append. A started stage can never be started twice."""
        claim = read_model(LineageClaim, claim)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if kind not in {"FAILED", "EXPIRED", "REVOKED"}:
                self._not_invalidated(
                    connection, claim.approval_identity, claim.lineage_plan.lineage_plan_identity
                )
            records = self._events(connection, claim)
            last = records[-1]["kind"]
            if last in {"FAILED", "EXPIRED", "REVOKED", "TERMINAL", "RECEIPT"}:
                raise ValueError("lineage is terminal; no retry or replacement")
            if kind not in {"FAILED", "EXPIRED", "REVOKED"}:
                expected = ["CLAIMED"]
                for stage in STAGES:
                    expected.extend(("START:" + stage, "COMPLETE:" + stage))
                expected.append("TERMINAL")
                if last not in expected or kind != expected[expected.index(last) + 1]:
                    raise ValueError("stage omitted, repeated, reordered or concurrently claimed")
            needs_evidence = kind.startswith("COMPLETE:") or kind == "TERMINAL"
            if needs_evidence != (evidence is not None):
                raise ValueError("state transition evidence mismatch")
            return self._append(
                connection,
                claim,
                records,
                kind,
                at,
                evidence.model_dump(mode="json") if evidence else {},
            )

    def require_active(self, claim: LineageClaim) -> tuple[dict, ...]:
        records = self.events(claim)
        if records[-1]["kind"] in {"FAILED", "EXPIRED", "REVOKED", "TERMINAL", "RECEIPT"}:
            raise ValueError("lineage is terminal")
        return records

    def require_terminal(self, claim: LineageClaim, result: EvidenceRef) -> str:
        with self._connection() as connection:
            records = self._events(connection, claim)
            self._not_invalidated(
                connection, claim.approval_identity, claim.lineage_plan.lineage_plan_identity
            )
        last = records[-2] if records[-1]["kind"] == "RECEIPT" else records[-1]
        if last["kind"] != "TERMINAL" or EvidenceRef.read(last["payload"]) != result:
            raise ValueError("result lacks the exact durable terminal consumption")
        return last["identity"]

    def record_receipt(
        self, claim: LineageClaim, result: EvidenceRef, receipt: EvidenceRef, *, at: str
    ) -> str:
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._not_invalidated(
                connection, claim.approval_identity, claim.lineage_plan.lineage_plan_identity
            )
            records = self._events(connection, claim)
            if (
                records[-1]["kind"] != "TERMINAL"
                or EvidenceRef.read(records[-1]["payload"]) != result
            ):
                raise ValueError(
                    "post-result receipt requires an unreceipted exact terminal result"
                )
            return self._append(
                connection, claim, records, "RECEIPT", at, receipt.model_dump(mode="json")
            )

    def require_receipt(self, claim: LineageClaim, receipt: EvidenceRef) -> None:
        with self._connection() as connection:
            records = self._events(connection, claim)
            self._not_invalidated(
                connection, claim.approval_identity, claim.lineage_plan.lineage_plan_identity
            )
        if records[-1]["kind"] != "RECEIPT" or EvidenceRef.read(records[-1]["payload"]) != receipt:
            raise ValueError("missing or fabricated post-result acceptance receipt")

    def invalidate(self, approval: str, plan: str, *, reason: str, at: str) -> None:
        if reason not in {"REVOKED", "EXPIRED"}:
            raise ValueError("only known terminal authority states can invalidate a plan")
        utc(at)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO invalidations VALUES (?, ?, ?, ?)",
                (approval, plan, reason, at),
            )

    def require_not_invalidated(self, approval: str, plan: str) -> None:
        with self._connection() as connection:
            self._not_invalidated(connection, approval, plan)

    def expire_unclaimed(self, approval: str, plan: str, *, at: str) -> None:
        """An observed claim deadline cannot be undone by clock rollback.

        An already consumed lineage retains its separate thirty-day terminal
        window; a duplicate claim request cannot expire that legitimate lineage.
        """
        utc(at)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM claims WHERE approval=? OR plan=?", (approval, plan)
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT OR IGNORE INTO invalidations VALUES (?, ?, ?, ?)",
                    (approval, plan, "EXPIRED", at),
                )
