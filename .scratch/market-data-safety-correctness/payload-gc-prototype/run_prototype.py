"""PROTOTYPE ONLY — deterministic payload publication/GC concurrency matrix.

Question: Which minimal SQLite/filesystem protocol preserves
"live payload row exists => canonical payload file exists" while the same
digest is concurrently republished around garbage collection?

This file is intentionally standalone and disposable. It never imports the
TradingAgents package and uses only TemporaryDirectory state.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

PAYLOAD_A = b'{"payload":"A"}\n'
PAYLOAD_B = b'{"payload":"B"}\n'
DIGEST_A = hashlib.sha256(PAYLOAD_A).hexdigest()
DIGEST_B = hashlib.sha256(PAYLOAD_B).hexdigest()
PAYLOADS = {DIGEST_A: PAYLOAD_A, DIGEST_B: PAYLOAD_B}


class PrototypeTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationResult:
    status: str
    detail: str = ""

    def render(self) -> str:
        return self.status if not self.detail else f"{self.status}:{self.detail}"


@dataclass(frozen=True)
class Observation:
    protocol: str
    interleaving: str
    database_row_state: str
    generation_tombstone_state: str
    payload_file_state: str
    publisher_result: str
    gc_result: str
    invariant_result: str
    eventual_cleanup_behavior: str
    eventual_database_row_state: str
    eventual_generation_tombstone_state: str
    eventual_payload_file_state: str
    eventual_invariant_result: str


class PauseHook:
    """A one-shot deterministic barrier at a named operation boundary."""

    def __init__(self, target: str):
        self.target = target
        self.arrived = threading.Event()
        self.resume = threading.Event()
        self._used = False

    def __call__(self, point: str) -> None:
        if point != self.target or self._used:
            return
        self._used = True
        self.arrived.set()
        if not self.resume.wait(timeout=10):
            raise PrototypeTimeout(f"barrier {point!r} was not released")


class ScratchStore:
    """Real SQLite metadata plus real temporary content-addressed files."""

    def __init__(self, root: Path, *, initialize: bool = True):
        self.root = root
        self.database_path = root / "metadata.sqlite3"
        self.lock_database_path = root / "payload-mutation-lock.sqlite3"
        self.payload_root = root / "payloads"
        self.quarantine_root = root / "quarantine"
        self.payload_root.mkdir(parents=True, exist_ok=True)
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._initialize()

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE digest_generations (
                    digest TEXT PRIMARY KEY,
                    current_generation INTEGER NOT NULL
                );
                CREATE TABLE payload_artifacts (
                    digest TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL
                );
                CREATE TABLE payload_references (
                    reference_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL REFERENCES payload_artifacts(digest)
                );
                CREATE TABLE payload_tombstones (
                    digest TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    owner_token TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    quarantine_path TEXT NOT NULL
                );
                """
            )
        with closing(self.connect_lock()) as connection:
            connection.execute(
                "CREATE TABLE mutex (id INTEGER PRIMARY KEY CHECK (id = 1))"
            )
            connection.execute("INSERT INTO mutex(id) VALUES (1)")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            timeout=0,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    def connect_lock(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.lock_database_path,
            isolation_level=None,
            timeout=0,
        )
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    def canonical_path(self, digest: str) -> Path:
        return self.payload_root / digest[:2] / f"{digest}.bin"

    def quarantine_path(self, digest: str, generation: int) -> Path:
        return self.quarantine_root / f"{digest}.g{generation}.trash"

    def ensure_file(self, digest: str) -> None:
        payload = PAYLOADS[digest]
        destination = self.canonical_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            actual = destination.read_bytes()
            if hashlib.sha256(actual).hexdigest() != digest:
                raise RuntimeError("canonical payload digest mismatch")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def seed_orphan(self, digest: str) -> None:
        self.ensure_file(digest)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO digest_generations(digest, current_generation) VALUES (?, 1)",
                (digest,),
            )
            connection.execute(
                "INSERT INTO payload_artifacts(digest, generation, byte_length) "
                "VALUES (?, 1, ?)",
                (digest, len(PAYLOADS[digest])),
            )
            connection.commit()

    def _allocate_generation(
        self,
        connection: sqlite3.Connection,
        digest: str,
    ) -> int:
        row = connection.execute(
            "SELECT current_generation FROM digest_generations WHERE digest = ?",
            (digest,),
        ).fetchone()
        generation = 1 if row is None else int(row[0]) + 1
        connection.execute(
            "INSERT INTO digest_generations(digest, current_generation) VALUES (?, ?) "
            "ON CONFLICT(digest) DO UPDATE SET current_generation = excluded.current_generation",
            (digest, generation),
        )
        return generation

    def register_reference_in_transaction(
        self,
        connection: sqlite3.Connection,
        digest: str,
        reference_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT generation, byte_length FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            generation = self._allocate_generation(connection, digest)
            connection.execute(
                "INSERT INTO payload_artifacts(digest, generation, byte_length) "
                "VALUES (?, ?, ?)",
                (digest, generation, len(PAYLOADS[digest])),
            )
        else:
            generation = int(row[0])
            if int(row[1]) != len(PAYLOADS[digest]):
                raise RuntimeError("payload metadata mismatch")
        connection.execute(
            "INSERT OR IGNORE INTO payload_references(reference_id, digest) VALUES (?, ?)",
            (reference_id, digest),
        )
        return generation

    def publish_unlocked(self, digest: str, reference_id: str) -> OperationResult:
        self.ensure_file(digest)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            generation = self.register_reference_in_transaction(
                connection,
                digest,
                reference_id,
            )
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower():
                return OperationResult("blocked", "metadata_writer")
            raise
        finally:
            connection.close()
        return OperationResult("published", f"g{generation}")

    def row_and_reference_count(self, digest: str) -> tuple[int | None, int]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT generation FROM payload_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
            references = int(
                connection.execute(
                    "SELECT COUNT(*) FROM payload_references WHERE digest = ?",
                    (digest,),
                ).fetchone()[0]
            )
        return (None if row is None else int(row[0]), references)

    def is_orphan_in_transaction(
        self,
        connection: sqlite3.Connection,
        digest: str,
    ) -> tuple[bool, int | None]:
        row = connection.execute(
            "SELECT generation FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()
        references = int(
            connection.execute(
                "SELECT COUNT(*) FROM payload_references WHERE digest = ?",
                (digest,),
            ).fetchone()[0]
        )
        if references:
            return False, None if row is None else int(row[0])
        if row is not None:
            return True, int(row[0])
        return self.canonical_path(digest).exists(), None

    def unlink_canonical(self, digest: str, unlink_mode: str) -> None:
        if unlink_mode == "fail_before":
            raise PermissionError("prototype injected unlink failure")
        self.canonical_path(digest).unlink(missing_ok=True)

    def state_fields(self, digests: tuple[str, ...]) -> tuple[str, str, str]:
        row_parts: list[str] = []
        tombstone_parts: list[str] = []
        file_parts: list[str] = []
        with closing(self.connect()) as connection:
            for label, digest in zip(("A", "B"), digests, strict=False):
                row = connection.execute(
                    "SELECT generation FROM payload_artifacts WHERE digest = ?",
                    (digest,),
                ).fetchone()
                references = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM payload_references WHERE digest = ?",
                        (digest,),
                    ).fetchone()[0]
                )
                tombstone = connection.execute(
                    "SELECT generation, owner_token, phase FROM payload_tombstones "
                    "WHERE digest = ?",
                    (digest,),
                ).fetchone()
                row_parts.append(
                    f"{label}:absent"
                    if row is None
                    else f"{label}:live:g{int(row[0])}:refs={references}"
                )
                tombstone_parts.append(
                    f"{label}:none"
                    if tombstone is None
                    else (
                        f"{label}:g{int(tombstone[0])}:"
                        f"{str(tombstone[1])}:{str(tombstone[2])}"
                    )
                )
                quarantine_count = len(list(self.quarantine_root.glob(f"{digest}.*")))
                file_parts.append(
                    f"{label}:canonical={'yes' if self.canonical_path(digest).exists() else 'no'}:"
                    f"quarantine={quarantine_count}"
                )
        return ";".join(row_parts), ";".join(tombstone_parts), ";".join(file_parts)

    def invariant(self) -> tuple[bool, str]:
        missing: list[str] = []
        with closing(self.connect()) as connection:
            rows = tuple(connection.execute("SELECT digest FROM payload_artifacts"))
        for (digest,) in rows:
            path = self.canonical_path(str(digest))
            if not path.exists():
                missing.append(str(digest))
                continue
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != str(digest):
                missing.append(str(digest))
        if missing:
            return False, "missing_or_invalid=" + ",".join(item[:8] for item in missing)
        return True, "all_live_rows_have_verified_files"


class Protocol:
    name = "base"

    def __init__(self, store: ScratchStore):
        self.store = store

    def publish(self, digest: str, reference_id: str) -> OperationResult:
        return self.store.publish_unlocked(digest, reference_id)

    def gc(
        self,
        digest: str,
        *,
        worker: str,
        hook: Callable[[str], None] | None = None,
        unlink_mode: str = "normal",
        interrupt_after_unlink: bool = False,
        takeover: bool = False,
    ) -> OperationResult:
        raise NotImplementedError

    @staticmethod
    def _hit(hook: Callable[[str], None] | None, point: str) -> None:
        if hook is not None:
            hook(point)


class DeleteCommitUnlinkRecheck(Protocol):
    name = "1-delete-commit-unlink-recheck"

    def gc(self, digest: str, *, worker: str, hook=None, unlink_mode="normal", interrupt_after_unlink=False, takeover=False) -> OperationResult:
        del takeover
        self._hit(hook, "before_delete")
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            orphan, _generation = self.store.is_orphan_in_transaction(connection, digest)
            if not orphan:
                connection.commit()
                return OperationResult("skipped", "referenced_or_absent")
            connection.execute("DELETE FROM payload_artifacts WHERE digest = ?", (digest,))
            connection.commit()
        finally:
            connection.close()
        self._hit(hook, "after_row_delete")
        self._hit(hook, "before_unlink")
        try:
            self.store.unlink_canonical(digest, unlink_mode)
        except OSError as exc:
            return OperationResult("unlink_failed", type(exc).__name__)
        self._hit(hook, "after_unlink")
        if interrupt_after_unlink:
            return OperationResult("interrupted", "after_unlink")
        generation, references = self.store.row_and_reference_count(digest)
        if generation is not None:
            return OperationResult(
                "race_detected_too_late",
                f"g{generation}:refs={references}",
            )
        return OperationResult("collected", worker)


class TombstoneProtocol(Protocol):
    name = "2-tombstone-owner-phase"

    def publish(self, digest: str, reference_id: str) -> OperationResult:
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tombstone = connection.execute(
                "SELECT 1 FROM payload_tombstones WHERE digest = ?",
                (digest,),
            ).fetchone()
            if tombstone is not None:
                connection.rollback()
                return OperationResult("blocked", "tombstone")
            self.store.ensure_file(digest)
            generation = self.store.register_reference_in_transaction(
                connection,
                digest,
                reference_id,
            )
            connection.commit()
            return OperationResult("published", f"g{generation}")
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower():
                return OperationResult("blocked", "metadata_writer")
            raise
        finally:
            connection.close()

    def _prepare_tombstone(
        self,
        digest: str,
        worker: str,
        takeover: bool,
    ) -> tuple[OperationResult | None, str]:
        connection = self.store.connect()
        token = f"{worker}-token"
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT generation, owner_token, phase, quarantine_path "
                "FROM payload_tombstones WHERE digest = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                if not takeover and str(existing[1]) != token:
                    connection.commit()
                    return OperationResult("skipped", "tombstone_owned"), token
                if takeover and str(existing[1]) != token:
                    connection.execute(
                        "UPDATE payload_tombstones SET owner_token = ? WHERE digest = ?",
                        (token, digest),
                    )
                connection.commit()
                return None, token

            orphan, generation = self.store.is_orphan_in_transaction(connection, digest)
            if not orphan:
                connection.commit()
                return OperationResult("skipped", "referenced_or_absent"), token
            if generation is None:
                row = connection.execute(
                    "SELECT current_generation FROM digest_generations WHERE digest = ?",
                    (digest,),
                ).fetchone()
                generation = 1 if row is None else int(row[0])
            quarantine = self.store.quarantine_path(digest, generation)
            connection.execute(
                "INSERT INTO payload_tombstones"
                "(digest, generation, owner_token, phase, quarantine_path) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (digest, generation, token, str(quarantine)),
            )
            connection.execute("DELETE FROM payload_artifacts WHERE digest = ?", (digest,))
            connection.commit()
            return None, token
        finally:
            connection.close()

    def gc(self, digest: str, *, worker: str, hook=None, unlink_mode="normal", interrupt_after_unlink=False, takeover=False) -> OperationResult:
        self._hit(hook, "before_delete")
        prepared, token = self._prepare_tombstone(digest, worker, takeover)
        if prepared is not None:
            return prepared
        self._hit(hook, "after_row_delete")

        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_token, phase, quarantine_path FROM payload_tombstones "
                "WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.commit()
                return OperationResult("skipped", "tombstone_finalized")
            if str(row[0]) != token:
                connection.commit()
                return OperationResult("fenced", "owner_changed")
            phase = str(row[1])
            quarantine = Path(str(row[2]))
            if phase == "pending":
                canonical = self.store.canonical_path(digest)
                if canonical.exists():
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(canonical, quarantine)
                elif not quarantine.exists():
                    # A prior owner may have completed unlink but crashed before
                    # recording the phase. No publisher can run while tombstoned.
                    pass
                connection.execute(
                    "UPDATE payload_tombstones SET phase = 'renamed' "
                    "WHERE digest = ? AND owner_token = ?",
                    (digest, token),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        self._hit(hook, "before_unlink")
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT quarantine_path FROM payload_tombstones WHERE digest = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return OperationResult("skipped", "tombstone_finalized")
        quarantine = Path(str(row[0]))
        try:
            if unlink_mode == "fail_before":
                raise PermissionError("prototype injected unlink failure")
            quarantine.unlink(missing_ok=True)
        except OSError as exc:
            return OperationResult("unlink_failed_tombstoned", type(exc).__name__)
        self._hit(hook, "after_unlink")
        if interrupt_after_unlink:
            return OperationResult("interrupted_tombstoned", "after_unlink")

        with closing(self.store.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM payload_tombstones WHERE digest = ? AND owner_token = ?",
                (digest, token),
            ).rowcount
            connection.commit()
        if deleted != 1:
            return OperationResult("fenced", "finalize_owner_changed")
        return OperationResult("collected", worker)


class GenerationCheckProtocol(Protocol):
    name = "3-generation-check-before-unlink"

    def gc(self, digest: str, *, worker: str, hook=None, unlink_mode="normal", interrupt_after_unlink=False, takeover=False) -> OperationResult:
        del takeover
        self._hit(hook, "before_delete")
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            orphan, generation = self.store.is_orphan_in_transaction(connection, digest)
            if not orphan:
                connection.commit()
                return OperationResult("skipped", "referenced_or_absent")
            if generation is None:
                row = connection.execute(
                    "SELECT current_generation FROM digest_generations WHERE digest = ?",
                    (digest,),
                ).fetchone()
                generation = 1 if row is None else int(row[0])
            connection.execute("DELETE FROM payload_artifacts WHERE digest = ?", (digest,))
            connection.commit()
        finally:
            connection.close()
        self._hit(hook, "after_row_delete")

        with closing(self.store.connect()) as connection:
            current = connection.execute(
                "SELECT generation FROM payload_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
        if current is not None and int(current[0]) != generation:
            return OperationResult(
                "skipped",
                f"generation_changed:g{generation}->g{int(current[0])}",
            )
        self._hit(hook, "before_unlink")
        try:
            self.store.unlink_canonical(digest, unlink_mode)
        except OSError as exc:
            return OperationResult("unlink_failed", type(exc).__name__)
        self._hit(hook, "after_unlink")
        if interrupt_after_unlink:
            return OperationResult("interrupted", "after_unlink")
        return OperationResult("collected", f"{worker}:checked_g{generation}")


class MainDatabaseTransactionProtocol(Protocol):
    name = "4-main-db-transaction-through-unlink"

    def publish(self, digest: str, reference_id: str) -> OperationResult:
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.store.ensure_file(digest)
            generation = self.store.register_reference_in_transaction(
                connection,
                digest,
                reference_id,
            )
            connection.commit()
            return OperationResult("published", f"g{generation}")
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower():
                return OperationResult("blocked", "metadata_writer")
            raise
        finally:
            connection.close()

    def gc(self, digest: str, *, worker: str, hook=None, unlink_mode="normal", interrupt_after_unlink=False, takeover=False) -> OperationResult:
        del takeover
        self._hit(hook, "before_delete")
        connection = self.store.connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    return OperationResult("blocked", "metadata_writer")
                raise
            orphan, _generation = self.store.is_orphan_in_transaction(connection, digest)
            if not orphan:
                connection.commit()
                return OperationResult("skipped", "referenced_or_absent")
            connection.execute("DELETE FROM payload_artifacts WHERE digest = ?", (digest,))
            self._hit(hook, "after_row_delete")
            self._hit(hook, "before_unlink")
            try:
                self.store.unlink_canonical(digest, unlink_mode)
            except OSError as exc:
                connection.rollback()
                return OperationResult("unlink_failed_rolled_back", type(exc).__name__)
            self._hit(hook, "after_unlink")
            if interrupt_after_unlink:
                connection.rollback()
                return OperationResult("interrupted_rolled_back", "after_unlink")
            connection.commit()
            return OperationResult("collected", worker)
        finally:
            connection.close()


class SidecarSQLiteMutexProtocol(Protocol):
    name = "5-sidecar-sqlite-mutation-mutex"

    def _acquire(self) -> sqlite3.Connection | None:
        connection = self.store.connect_lock()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            connection.close()
            if "locked" in str(exc).lower():
                return None
            raise
        return connection

    def publish(self, digest: str, reference_id: str) -> OperationResult:
        lock_connection = self._acquire()
        if lock_connection is None:
            return OperationResult("blocked", "payload_mutation_mutex")
        try:
            self.store.ensure_file(digest)
            connection = self.store.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                generation = self.store.register_reference_in_transaction(
                    connection,
                    digest,
                    reference_id,
                )
                connection.commit()
            finally:
                connection.close()
            lock_connection.commit()
            return OperationResult("published", f"g{generation}")
        finally:
            lock_connection.close()

    def gc(self, digest: str, *, worker: str, hook=None, unlink_mode="normal", interrupt_after_unlink=False, takeover=False) -> OperationResult:
        del takeover
        self._hit(hook, "before_delete")
        lock_connection = self._acquire()
        if lock_connection is None:
            return OperationResult("blocked", "payload_mutation_mutex")
        try:
            connection = self.store.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                orphan, _generation = self.store.is_orphan_in_transaction(connection, digest)
                if not orphan:
                    connection.commit()
                    lock_connection.commit()
                    return OperationResult("skipped", "referenced_or_absent")
                connection.execute("DELETE FROM payload_artifacts WHERE digest = ?", (digest,))
                connection.commit()
            finally:
                connection.close()
            self._hit(hook, "after_row_delete")
            self._hit(hook, "before_unlink")
            try:
                self.store.unlink_canonical(digest, unlink_mode)
            except OSError as exc:
                lock_connection.commit()
                return OperationResult("unlink_failed_row_absent", type(exc).__name__)
            self._hit(hook, "after_unlink")
            if interrupt_after_unlink:
                lock_connection.rollback()
                return OperationResult("interrupted_lock_released", "after_unlink")
            lock_connection.commit()
            return OperationResult("collected", worker)
        finally:
            lock_connection.close()


PROTOCOLS: tuple[type[Protocol], ...] = (
    DeleteCommitUnlinkRecheck,
    TombstoneProtocol,
    GenerationCheckProtocol,
    MainDatabaseTransactionProtocol,
    SidecarSQLiteMutexProtocol,
)

INTERLEAVINGS = (
    "ordinary_orphan_collection",
    "republication_before_row_deletion",
    "republication_after_row_deletion_before_unlink",
    "republication_immediately_before_unlink",
    "republication_immediately_after_unlink",
    "two_concurrent_gc_workers",
    "publisher_and_gc_different_digests",
    "unlink_failure_before_removal",
    "interrupted_after_successful_unlink",
)


class GcTask:
    def __init__(
        self,
        protocol: Protocol,
        digest: str,
        pause_point: str,
        *,
        worker: str = "gc1",
    ):
        self.hook = PauseHook(pause_point)
        self.result: OperationResult | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = protocol.gc(
                    digest,
                    worker=worker,
                    hook=self.hook,
                )
            except BaseException as exc:  # prototype must surface the full failure
                self.error = exc

        self.thread = threading.Thread(target=run, name=f"prototype-{worker}")
        self.thread.start()
        if not self.hook.arrived.wait(timeout=10):
            raise PrototypeTimeout(
                f"GC did not reach deterministic barrier {pause_point!r}"
            )

    def finish(self) -> OperationResult:
        self.hook.resume.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise PrototypeTimeout("GC thread did not terminate")
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def publish_with_retry(
    protocol: Protocol,
    digest: str,
    reference_id: str,
    initial: OperationResult,
) -> str:
    result = initial
    prefix = result.render()
    if result.status == "blocked":
        result = protocol.publish(digest, reference_id)
        prefix += "->" + result.render()
    if result.status != "published":
        return prefix
    before = protocol.store.row_and_reference_count(digest)
    repeated = protocol.publish(digest, reference_id)
    after = protocol.store.row_and_reference_count(digest)
    idempotent = repeated.status == "published" and before == after
    return prefix + (";repeat=idempotent" if idempotent else ";repeat=FAILED")


def verify_publisher_while_paused(
    protocol: Protocol,
    digest: str,
    reference_id: str,
    initial: OperationResult,
) -> str:
    """Verify successful idempotence before the paused GC may resume."""

    if initial.status != "published":
        return initial.render()
    before = protocol.store.row_and_reference_count(digest)
    repeated = protocol.publish(digest, reference_id)
    after = protocol.store.row_and_reference_count(digest)
    idempotent = repeated.status == "published" and before == after
    return initial.render() + (
        ";repeat=idempotent" if idempotent else ";repeat=FAILED"
    )


def finish_blocked_publisher(
    protocol: Protocol,
    digest: str,
    reference_id: str,
    initial: OperationResult,
    rendered_while_paused: str,
) -> str:
    if initial.status != "blocked":
        return rendered_while_paused
    return publish_with_retry(protocol, digest, reference_id, initial)


def eventual_description(store: ScratchStore, digests: tuple[str, ...]) -> str:
    descriptions: list[str] = []
    for label, digest in zip(("A", "B"), digests, strict=False):
        generation, references = store.row_and_reference_count(digest)
        canonical = store.canonical_path(digest).exists()
        with closing(store.connect()) as connection:
            tombstone = connection.execute(
                "SELECT 1 FROM payload_tombstones WHERE digest = ?",
                (digest,),
            ).fetchone()
        if generation is not None and references and canonical:
            descriptions.append(f"{label}:published_retained")
        elif generation is not None and references and not canonical:
            descriptions.append(f"{label}:BROKEN_reference_without_file")
        elif generation is None and not canonical and tombstone is None:
            descriptions.append(f"{label}:orphan_collected")
        else:
            descriptions.append(f"{label}:residual_state")
    return ";".join(descriptions)


def run_case(protocol_type: type[Protocol], interleaving: str) -> Observation:
    with tempfile.TemporaryDirectory(prefix="payload-gc-prototype-") as temporary:
        store = ScratchStore(Path(temporary))
        store.seed_orphan(DIGEST_A)
        protocol = protocol_type(store)
        digests = (DIGEST_A,)
        publisher_result = "not_run"
        gc_result = "not_run"

        if interleaving == "ordinary_orphan_collection":
            gc_result = protocol.gc(DIGEST_A, worker="gc1").render()

        elif interleaving == "republication_before_row_deletion":
            task = GcTask(protocol, DIGEST_A, "before_delete")
            initial = protocol.publish(DIGEST_A, "publisher:A")
            publisher_result = verify_publisher_while_paused(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
            )
            gc_result = task.finish().render()
            publisher_result = finish_blocked_publisher(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
                publisher_result,
            )

        elif interleaving == "republication_after_row_deletion_before_unlink":
            task = GcTask(protocol, DIGEST_A, "after_row_delete")
            initial = protocol.publish(DIGEST_A, "publisher:A")
            publisher_result = verify_publisher_while_paused(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
            )
            gc_result = task.finish().render()
            publisher_result = finish_blocked_publisher(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
                publisher_result,
            )

        elif interleaving == "republication_immediately_before_unlink":
            task = GcTask(protocol, DIGEST_A, "before_unlink")
            initial = protocol.publish(DIGEST_A, "publisher:A")
            publisher_result = verify_publisher_while_paused(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
            )
            gc_result = task.finish().render()
            publisher_result = finish_blocked_publisher(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
                publisher_result,
            )

        elif interleaving == "republication_immediately_after_unlink":
            task = GcTask(protocol, DIGEST_A, "after_unlink")
            initial = protocol.publish(DIGEST_A, "publisher:A")
            publisher_result = verify_publisher_while_paused(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
            )
            gc_result = task.finish().render()
            publisher_result = finish_blocked_publisher(
                protocol,
                DIGEST_A,
                "publisher:A",
                initial,
                publisher_result,
            )

        elif interleaving == "two_concurrent_gc_workers":
            task = GcTask(protocol, DIGEST_A, "after_row_delete", worker="gc1")
            second = protocol.gc(DIGEST_A, worker="gc2")
            first = task.finish()
            if second.status in {"blocked", "skipped"}:
                retried = protocol.gc(
                    DIGEST_A,
                    worker="gc2-retry",
                    takeover=False,
                )
                second_rendered = second.render() + "->" + retried.render()
            else:
                second_rendered = second.render()
            gc_result = f"gc1={first.render()};gc2={second_rendered}"

        elif interleaving == "publisher_and_gc_different_digests":
            digests = (DIGEST_A, DIGEST_B)
            task = GcTask(protocol, DIGEST_A, "before_unlink")
            initial = protocol.publish(DIGEST_B, "publisher:B")
            publisher_result = verify_publisher_while_paused(
                protocol,
                DIGEST_B,
                "publisher:B",
                initial,
            )
            gc_result = task.finish().render()
            publisher_result = finish_blocked_publisher(
                protocol,
                DIGEST_B,
                "publisher:B",
                initial,
                publisher_result,
            )

        elif interleaving == "unlink_failure_before_removal":
            gc_result = protocol.gc(
                DIGEST_A,
                worker="gc1",
                unlink_mode="fail_before",
            ).render()

        elif interleaving == "interrupted_after_successful_unlink":
            gc_result = protocol.gc(
                DIGEST_A,
                worker="gc1",
                interrupt_after_unlink=True,
            ).render()

        else:
            raise AssertionError(interleaving)

        rows, tombstones, files = store.state_fields(digests)
        invariant_ok, invariant_detail = store.invariant()

        recovery_results: list[str] = []
        for digest in digests:
            recovery = protocol.gc(
                digest,
                worker="recovery",
                takeover=isinstance(protocol, TombstoneProtocol),
            )
            recovery_results.append(recovery.render())
        eventual_rows, eventual_tombstones, eventual_files = store.state_fields(digests)
        eventual_ok, eventual_detail = store.invariant()
        cleanup = (
            eventual_description(store, digests)
            + ";recovery="
            + ",".join(recovery_results)
        )

        return Observation(
            protocol=protocol.name,
            interleaving=interleaving,
            database_row_state=rows,
            generation_tombstone_state=tombstones,
            payload_file_state=files,
            publisher_result=publisher_result,
            gc_result=gc_result,
            invariant_result=("PASS:" if invariant_ok else "FAIL:") + invariant_detail,
            eventual_cleanup_behavior=cleanup,
            eventual_database_row_state=eventual_rows,
            eventual_generation_tombstone_state=eventual_tombstones,
            eventual_payload_file_state=eventual_files,
            eventual_invariant_result=("PASS:" if eventual_ok else "FAIL:")
            + eventual_detail,
        )


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def run_sidecar_cross_process_child(
    root: str,
    arrived,
    resume,
    output,
) -> None:
    """Child process: pause selected-protocol GC immediately before unlink."""

    store = ScratchStore(Path(root), initialize=False)
    protocol = SidecarSQLiteMutexProtocol(store)

    def hook(point: str) -> None:
        if point != "before_unlink":
            return
        arrived.set()
        if not resume.wait(timeout=10):
            raise PrototypeTimeout("cross-process barrier was not released")

    try:
        result = protocol.gc(DIGEST_A, worker="gc-child", hook=hook)
        output.put(("ok", result.render()))
    except BaseException as exc:
        output.put(("error", f"{type(exc).__name__}:{exc}"))


def run_cross_process_probe() -> list[dict[str, str]]:
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="payload-gc-prototype-process-") as temporary:
        store = ScratchStore(Path(temporary))
        store.seed_orphan(DIGEST_A)
        arrived = context.Event()
        resume = context.Event()
        output = context.Queue()
        process = context.Process(
            target=run_sidecar_cross_process_child,
            args=(temporary, arrived, resume, output),
            name="prototype-gc-process",
        )
        process.start()
        if not arrived.wait(timeout=10):
            process.terminate()
            process.join(timeout=10)
            raise PrototypeTimeout("child GC did not reach cross-process barrier")

        protocol = SidecarSQLiteMutexProtocol(store)
        initial = protocol.publish(DIGEST_A, "publisher:parent")
        blocked_state = ";".join(store.state_fields((DIGEST_A,)))
        resume.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            raise PrototypeTimeout("child GC did not terminate")
        child_status, child_result = output.get()
        output.close()
        output.join_thread()
        if child_status != "ok":
            raise RuntimeError(child_result)

        publisher_result = publish_with_retry(
            protocol,
            DIGEST_A,
            "publisher:parent",
            initial,
        )
        rows, tombstones, files = store.state_fields((DIGEST_A,))
        invariant_ok, invariant_detail = store.invariant()
        ordered_probe = {
            "protocol": protocol.name,
            "interleaving": "separate_process_republication_immediately_before_unlink",
            "blocked_publisher_attempt": initial.render(),
            "state_while_child_holds_mutex": blocked_state,
            "child_gc_result": child_result,
            "publisher_result": publisher_result,
            "database_row_state": rows,
            "generation_tombstone_state": tombstones,
            "payload_file_state": files,
            "invariant_result": ("PASS:" if invariant_ok else "FAIL:")
            + invariant_detail,
        }

    with tempfile.TemporaryDirectory(prefix="payload-gc-prototype-crash-") as temporary:
        store = ScratchStore(Path(temporary))
        store.seed_orphan(DIGEST_A)
        arrived = context.Event()
        resume = context.Event()
        output = context.Queue()
        process = context.Process(
            target=run_sidecar_cross_process_child,
            args=(temporary, arrived, resume, output),
            name="prototype-crashed-gc-process",
        )
        process.start()
        if not arrived.wait(timeout=10):
            process.terminate()
            process.join(timeout=10)
            raise PrototypeTimeout("crash child did not reach cross-process barrier")
        process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            raise PrototypeTimeout("crash child did not terminate")
        output.close()
        output.join_thread()

        state_after_crash = ";".join(store.state_fields((DIGEST_A,)))
        protocol = SidecarSQLiteMutexProtocol(store)
        initial = protocol.publish(DIGEST_A, "publisher:after-crash")
        publisher_result = publish_with_retry(
            protocol,
            DIGEST_A,
            "publisher:after-crash",
            initial,
        )
        rows, tombstones, files = store.state_fields((DIGEST_A,))
        invariant_ok, invariant_detail = store.invariant()
        crash_probe = {
            "protocol": protocol.name,
            "interleaving": "separate_process_termination_after_row_delete_before_unlink",
            "state_after_gc_process_termination": state_after_crash,
            "publisher_result": publisher_result,
            "database_row_state": rows,
            "generation_tombstone_state": tombstones,
            "payload_file_state": files,
            "invariant_result": ("PASS:" if invariant_ok else "FAIL:")
            + invariant_detail,
        }

    return [ordered_probe, crash_probe]


def render_markdown(
    observations: list[Observation],
    cross_process: list[dict[str, str]],
) -> str:
    lines = [
        "# Payload GC concurrency prototype results",
        "",
        "PROTOTYPE ONLY. Each row used fresh temporary SQLite/filesystem state and deterministic event barriers.",
        "",
        "## Protocol summary",
        "",
        "| Protocol | Immediate invariant failures | Eventual invariant failures |",
        "| --- | ---: | ---: |",
    ]
    for protocol in (item.name for item in PROTOCOLS):
        subset = [item for item in observations if item.protocol == protocol]
        immediate = sum(item.invariant_result.startswith("FAIL") for item in subset)
        eventual = sum(item.eventual_invariant_result.startswith("FAIL") for item in subset)
        lines.append(f"| {protocol} | {immediate} | {eventual} |")

    for protocol in (item.name for item in PROTOCOLS):
        lines.extend(
            [
                "",
                f"## {protocol}",
                "",
                "| Interleaving | DB row state | Generation/tombstone | File state | Publisher | GC | Invariant | Eventual cleanup | Eventual state | Eventual invariant |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in (row for row in observations if row.protocol == protocol):
            eventual_state = (
                item.eventual_database_row_state
                + ";"
                + item.eventual_generation_tombstone_state
                + ";"
                + item.eventual_payload_file_state
            )
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(value)
                    for value in (
                        item.interleaving,
                        item.database_row_state,
                        item.generation_tombstone_state,
                        item.payload_file_state,
                        item.publisher_result,
                        item.gc_result,
                        item.invariant_result,
                        item.eventual_cleanup_behavior,
                        eventual_state,
                        item.eventual_invariant_result,
                    )
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## Selected-protocol separate-process probe",
            "",
            "| Field | Result |",
            "| --- | --- |",
        ]
    )
    for index, probe in enumerate(cross_process, start=1):
        lines.append(f"| probe | {index} |")
        for key, value in probe.items():
            lines.append(f"| {markdown_cell(key)} | {markdown_cell(value)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    observations: list[Observation] = []
    total = len(PROTOCOLS) * len(INTERLEAVINGS)
    print(f"PROTOTYPE ONLY - running {total} deterministic scenarios")
    for protocol in PROTOCOLS:
        print(f"\n{protocol.name}")
        for interleaving in INTERLEAVINGS:
            observation = run_case(protocol, interleaving)
            observations.append(observation)
            marker = "PASS" if observation.invariant_result.startswith("PASS") else "FAIL"
            print(f"  {marker:4}  {interleaving}")

    print("\nselected-protocol separate-process probe")
    cross_process = run_cross_process_probe()
    for probe in cross_process:
        print(f"  {probe['invariant_result'].split(':', 1)[0]:4}  "
              f"{probe['interleaving']}")

    output_root = Path(__file__).resolve().parent
    (output_root / "results.json").write_text(
        json.dumps(
            {
                "matrix": [asdict(item) for item in observations],
                "cross_process": cross_process,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_root / "RESULTS.md").write_text(
        render_markdown(observations, cross_process),
        encoding="utf-8",
    )
    print(f"\nWrote {output_root / 'RESULTS.md'}")
    print(f"Wrote {output_root / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
