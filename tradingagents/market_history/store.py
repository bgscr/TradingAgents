from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd

from tradingagents.market_history.calendar import (
    MarketSessionCalendarPublication,
    PublishedSessionCalendar,
    StoredSessionCalendar,
)
from tradingagents.market_history.config import (
    MarketHistoryConfig,
    payload_mutation_sidecar_path,
    provider_request_authority_config,
)
from tradingagents.market_history.equivalence import (
    DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS,
    MainlandEquivalenceComparison,
    MainlandEquivalenceScenario,
)
from tradingagents.market_history.frames import normalized_frame_sha256
from tradingagents.market_history.models import (
    HistoryBundleProvenanceAudit,
    ProviderHistoryBundlePublication,
    PublishedHistoryBundle,
    ReconstructedMarketSnapshot,
    SnapshotPurpose,
)
from tradingagents.market_history.revisions import RevisionKind, revision_identity
from tradingagents.market_history.schema import (
    CREATE_MIGRATION_TABLE,
    MIGRATION_V1,
    MIGRATION_V2,
    MIGRATION_V3,
    MIGRATION_V4,
    MIGRATION_V5,
    MIGRATION_V6,
    MIGRATION_V7,
    MIGRATION_V8,
    MIGRATION_V9,
    SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from tradingagents.asset_configuration import RunAssetConfiguration
    from tradingagents.market_history.snapshot_identity import (
        CryptoProviderDatasetDescriptor,
    )

_PAYLOAD_ROOT_OWNER_FILENAME = ".market-history-database-owner"
_PAYLOAD_MUTATION_TIMEOUT_SECONDS = 30.0
_PAYLOAD_REFERENCE_TABLES = (
    "ingestion_runs",
    "raw_market_observation_revisions",
    "trading_status_revisions",
    "adjustment_factor_revisions",
    "provider_frame_revisions",
    "market_session_calendars",
)


def _noop_phase_hook(_phase: str, _operation_subject: str) -> None:
    return None


@contextmanager
def _acquire_payload_mutation_mutex(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    lock_path = payload_mutation_sidecar_path(database_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        lock_path,
        isolation_level=None,
        timeout=_PAYLOAD_MUTATION_TIMEOUT_SECONDS,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        connection.close()
        if "locked" in str(exc).lower():
            raise PayloadMutationTimeoutError(
                f"timed out acquiring payload-mutation mutex {lock_path}"
            ) from exc
        raise
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@dataclass(frozen=True)
class MarketHistoryStoreStatus:
    schema_version: int
    foreign_keys_enabled: bool
    journal_mode: str
    synchronous: str
    tables: frozenset[str]


@dataclass(frozen=True)
class PayloadArtifact:
    digest: str
    relative_path: Path
    byte_length: int
    media_type: str


class PayloadMaintenanceStatus(str, Enum):
    COLLECTED = "collected"
    SKIPPED = "skipped"
    MUTEX_TIMEOUT = "mutex_timeout"
    UNLINK_FAILED = "unlink_failed"


@dataclass(frozen=True)
class PayloadMaintenanceOutcome:
    digest: str
    status: PayloadMaintenanceStatus
    retryable: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class PayloadCollectionResult:
    outcomes: tuple[PayloadMaintenanceOutcome, ...]

    @property
    def collected_digests(self) -> tuple[str, ...]:
        return tuple(
            outcome.digest
            for outcome in self.outcomes
            if outcome.status is PayloadMaintenanceStatus.COLLECTED
        )

    @property
    def failures(self) -> tuple[PayloadMaintenanceOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status
            in {
                PayloadMaintenanceStatus.MUTEX_TIMEOUT,
                PayloadMaintenanceStatus.UNLINK_FAILED,
            }
        )


@dataclass(frozen=True)
class MarketHistoryBackup:
    path: Path
    manifest_path: Path
    created_at: str
    database_sha256: str


@dataclass(frozen=True)
class ProviderFramePublication:
    upstream_service_id: str
    upstream_service_name: str
    provider_dataset_id: str
    provider_name: str
    dataset_name: str
    adjustment_methodology: str
    strict_history_qualified: bool
    instrument_id: str
    canonical_symbol: str
    reference_market: str
    instrument_kind: str
    currency: str
    identity_revision: str
    snapshot_id: str
    requested_start: str
    requested_end: str
    effective_trading_date: str
    adjustment_basis: str
    frame_digest: str
    history_rows: int
    retrieved_at: str
    normalized_frame: bytes


@dataclass(frozen=True)
class StoredProviderFrame:
    frame_revision_id: str
    snapshot_id: str
    provider_dataset_id: str
    provider_name: str
    instrument_id: str
    canonical_symbol: str
    requested_start: str
    requested_end: str
    effective_trading_date: str
    adjustment_basis: str
    frame_digest: str
    history_rows: int
    payload_digest: str
    retrieved_at: str
    current_only: bool


@dataclass(frozen=True)
class LoadedProviderFrame:
    stored: StoredProviderFrame
    frame: pd.DataFrame


@dataclass(frozen=True)
class StoredHistoryBundleRef:
    bundle_revision_id: str
    observed_at: str


class MarketHistoryCorruptionError(RuntimeError):
    """Stored history cannot be trusted because durable bytes violate their digest."""


class ProviderRequestAuthorityUnavailableError(RuntimeError):
    """Persisted provider-request safety state cannot be opened or imported."""


class PayloadMutationTimeoutError(TimeoutError):
    """The cross-process payload-mutation mutex was not acquired in time."""


class PayloadLockOrderError(RuntimeError):
    """A payload mutator was entered after a main-database transaction began."""


class MarketHistoryStore:
    """Transactional operational index for durable point-in-time market history."""

    def __init__(
        self,
        config: MarketHistoryConfig,
        connection: sqlite3.Connection,
        *,
        phase_hook: Callable[[str, str], None] = _noop_phase_hook,
    ) -> None:
        self.config = config
        self._connection = connection
        self._phase_hook = phase_hook
        self._payload_mutation_connection: sqlite3.Connection | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        config: MarketHistoryConfig,
        *,
        _phase_hook: Callable[[str, str], None] = _noop_phase_hook,
    ) -> MarketHistoryStore:
        database_path = Path(config.database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, isolation_level=None, timeout=30.0)
        store = cls(config, connection, phase_hook=_phase_hook)
        try:
            store._configure_connection()
            store._apply_migrations()
            cls._claim_payload_root(config)
        except BaseException:
            connection.close()
            raise
        return store

    @classmethod
    def open_provider_request_authority(
        cls,
        config: MarketHistoryConfig,
    ) -> MarketHistoryStore:
        """Open the one coordinator database and import any legacy primary state."""

        authority_config = provider_request_authority_config(config)
        try:
            authority = cls.open(authority_config)
        except sqlite3.DatabaseError as exc:
            raise ProviderRequestAuthorityUnavailableError(
                "provider-request authority could not be opened at "
                f"{authority_config.database_path}"
            ) from exc
        try:
            authority._import_legacy_provider_request_state(config.database_path)
        except BaseException:
            authority.close()
            raise
        return authority

    def _import_legacy_provider_request_state(self, source_path: Path) -> None:
        source_path = Path(source_path)
        if not source_path.is_file() or source_path == self.config.database_path:
            return
        source_identity = os.path.normcase(str(source_path.resolve(strict=False)))
        imported = self._connection.execute(
            "SELECT 1 FROM provider_request_state_imports "
            "WHERE source_database_path = ?",
            (source_identity,),
        ).fetchone()
        if imported is not None:
            return

        attached = False
        try:
            self._connection.execute(
                "ATTACH DATABASE ? AS legacy_provider_requests",
                (str(source_path),),
            )
            attached = True
            source_tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM legacy_provider_requests.sqlite_master "
                    "WHERE type = 'table'"
                )
            }
            if "upstream_services" not in source_tables:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT INTO provider_request_state_imports "
                    "(source_database_path, imported_at) VALUES (?, ?)",
                    (source_identity, datetime.now(timezone.utc).isoformat()),
                )
                self._connection.commit()
                return

            def require_preserved_rows(
                table: str,
                where: str = "",
                columns: str = "*",
            ) -> None:
                conflicting = self._connection.execute(
                    f"SELECT {columns} FROM legacy_provider_requests.{table} {where} "
                    f"EXCEPT SELECT {columns} FROM main.{table} {where} LIMIT 1"
                ).fetchone()
                if conflicting is not None:
                    raise sqlite3.IntegrityError(
                        "conflicting legacy provider-request state in " f"{table}"
                    )

            self._connection.execute("BEGIN IMMEDIATE")
            for row in self._connection.execute(
                "SELECT upstream_service_id, service_name, account_scope, "
                "operator_ceiling_json, created_at "
                "FROM legacy_provider_requests.upstream_services"
            ):
                self._connection.execute(
                    "INSERT INTO upstream_services "
                    "(upstream_service_id, service_name, account_scope, "
                    "operator_ceiling_json, created_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(upstream_service_id) DO UPDATE SET "
                    "operator_ceiling_json = COALESCE("
                    "upstream_services.operator_ceiling_json, "
                    "excluded.operator_ceiling_json)",
                    row,
                )
            require_preserved_rows(
                "upstream_services",
                columns=(
                    "upstream_service_id, account_scope, operator_ceiling_json"
                ),
            )
            if "request_cooldowns" in source_tables:
                for row in self._connection.execute(
                    "SELECT upstream_service_id, cooldown_scope, cooldown_until, "
                    "reason, retry_after_seconds, updated_at "
                    "FROM legacy_provider_requests.request_cooldowns"
                ):
                    self._connection.execute(
                        "INSERT INTO request_cooldowns "
                        "(upstream_service_id, cooldown_scope, cooldown_until, reason, "
                        "retry_after_seconds, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(upstream_service_id, cooldown_scope) DO UPDATE SET "
                        "cooldown_until = MAX(request_cooldowns.cooldown_until, "
                        "excluded.cooldown_until), reason = CASE WHEN "
                        "excluded.cooldown_until > request_cooldowns.cooldown_until "
                        "THEN excluded.reason ELSE request_cooldowns.reason END, "
                        "retry_after_seconds = CASE WHEN excluded.cooldown_until > "
                        "request_cooldowns.cooldown_until THEN excluded.retry_after_seconds "
                        "ELSE request_cooldowns.retry_after_seconds END, "
                        "updated_at = MAX(request_cooldowns.updated_at, excluded.updated_at)",
                        row,
                    )
            coordinator_columns = {
                "request_leases": (
                    "request_key",
                    "upstream_service_id",
                    "owner_id",
                    "priority",
                    "acquired_at",
                    "expires_at",
                ),
                "request_queue": (
                    "request_key",
                    "upstream_service_id",
                    "priority",
                    "first_enqueued_at",
                    "updated_at",
                    "expires_at",
                ),
                "provider_request_sequences": (
                    "sequence_id",
                    "request_key",
                    "upstream_service_id",
                    "owner_id",
                    "started_at",
                    "completed_at",
                    "status",
                    "final_physical_attempt_count",
                    "result_payload",
                    "result_sha256",
                    "failure_outcome",
                    "failure_retryable",
                    "failure_status_code",
                    "failure_error_code",
                    "failure_retry_after_seconds",
                ),
            }
            optional_coordinator_columns = {
                "request_leases": ("capacity_scope",),
                "request_queue": (),
                "provider_request_sequences": (
                    "capacity_scope",
                    "failure_outcome_kind",
                ),
            }
            for table, base_columns in coordinator_columns.items():
                if table in source_tables:
                    source_column_names = {
                        str(row[1])
                        for row in self._connection.execute(
                            f"PRAGMA legacy_provider_requests.table_info({table})"
                        )
                    }
                    columns = (*base_columns, *(
                        column
                        for column in optional_coordinator_columns[table]
                        if column in source_column_names
                    ))
                    column_list = ", ".join(columns)
                    self._connection.execute(
                        f"INSERT OR IGNORE INTO {table} ({column_list}) "
                        f"SELECT {column_list} FROM legacy_provider_requests.{table}"
                    )
                    require_preserved_rows(table, columns=column_list)
            if "provider_request_attempts" in source_tables:
                attempt_columns = (
                    "sequence_id",
                    "attempt_index",
                    "request_key",
                    "upstream_service_id",
                    "upstream_service_name",
                    "owner_id",
                    "priority",
                    "operation",
                    "attempted_at",
                    "pacing_event",
                    "pacing_wait_seconds",
                    "outcome",
                    "retryable",
                    "status_code",
                    "error_code",
                    "retry_after_seconds",
                    "cooldown_changed",
                    "cooldown_until",
                    "final_physical_attempt_count",
                )
                source_attempt_columns = {
                    str(row[1])
                    for row in self._connection.execute(
                        "PRAGMA legacy_provider_requests."
                        "table_info(provider_request_attempts)"
                    )
                }
                if "attempt_event_id" in source_attempt_columns:
                    attempt_columns = (*attempt_columns, "attempt_event_id")
                if "terminal_outcome" in source_attempt_columns:
                    attempt_columns = (*attempt_columns, "terminal_outcome")
                if "capacity_scope" in source_attempt_columns:
                    attempt_columns = (*attempt_columns, "capacity_scope")
                if "terminal_outcome_kind" in source_attempt_columns:
                    attempt_columns = (*attempt_columns, "terminal_outcome_kind")
                column_list = ", ".join(attempt_columns)
                self._connection.execute(
                    "INSERT OR IGNORE INTO provider_request_attempts "
                    f"({column_list}) SELECT {column_list} "
                    "FROM legacy_provider_requests.provider_request_attempts"
                )
                require_preserved_rows(
                    "provider_request_attempts",
                    columns=column_list,
                )
            if "history_store_diagnostics" in source_tables:
                self._connection.execute(
                    "INSERT OR IGNORE INTO history_store_diagnostics "
                    "SELECT * FROM legacy_provider_requests.history_store_diagnostics "
                    "WHERE operation = 'provider_request' AND instrument_id IS NULL "
                    "AND provider_dataset_id IS NULL AND ingestion_run_id IS NULL"
                )
                require_preserved_rows(
                    "history_store_diagnostics",
                    "WHERE operation = 'provider_request' "
                    "AND instrument_id IS NULL AND provider_dataset_id IS NULL "
                    "AND ingestion_run_id IS NULL",
                )
            self._connection.execute(
                "INSERT INTO provider_request_state_imports "
                "(source_database_path, imported_at) VALUES (?, ?)",
                (source_identity, datetime.now(timezone.utc).isoformat()),
            )
            self._connection.commit()
        except sqlite3.DatabaseError as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ProviderRequestAuthorityUnavailableError(
                "legacy provider-request state could not be imported from "
                f"{source_path}"
            ) from exc
        finally:
            if attached and not self._connection.in_transaction:
                with suppress(sqlite3.DatabaseError):
                    self._connection.execute("DETACH DATABASE legacy_provider_requests")

    @staticmethod
    def _claim_payload_root(config: MarketHistoryConfig) -> None:
        """Bind one payload root to exactly one canonical history database."""

        payload_root = Path(config.payload_root)
        payload_root.mkdir(parents=True, exist_ok=True)
        owner_path = payload_root / _PAYLOAD_ROOT_OWNER_FILENAME
        expected_owner = os.path.normcase(str(config.database_path)).encode("utf-8") + b"\n"
        temporary_owner = payload_root / (
            f"{_PAYLOAD_ROOT_OWNER_FILENAME}.{uuid4().hex}.tmp"
        )
        try:
            with temporary_owner.open("xb") as handle:
                handle.write(expected_owner)
                handle.flush()
                os.fsync(handle.fileno())
            with suppress(FileExistsError):
                os.link(temporary_owner, owner_path)
            actual_owner = owner_path.read_bytes()
        finally:
            temporary_owner.unlink(missing_ok=True)
        if actual_owner != expected_owner:
            actual_text = actual_owner.decode("utf-8", errors="replace").strip()
            raise ValueError(
                "payload root is already owned by a different canonical database: "
                f"payload_root={payload_root}, owner={actual_text}"
            )

    def __enter__(self) -> MarketHistoryStore:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def status(self) -> MarketHistoryStoreStatus:
        self._require_open()
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        assert row is not None
        journal_mode = str(self._pragma_value("journal_mode")).lower()
        synchronous_code = int(self._pragma_value("synchronous"))
        synchronous = {0: "off", 1: "normal", 2: "full", 3: "extra"}.get(
            synchronous_code,
            str(synchronous_code),
        )
        tables = frozenset(
            str(item[0])
            for item in self._connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
            if not str(item[0]).startswith("sqlite_")
        )
        return MarketHistoryStoreStatus(
            schema_version=int(row[0]),
            foreign_keys_enabled=bool(self._pragma_value("foreign_keys")),
            journal_mode=journal_mode,
            synchronous=synchronous,
            tables=tables,
        )

    def install_payload(self, payload: bytes, *, media_type: str) -> PayloadArtifact:
        """Atomically install immutable bytes before publishing their database row."""
        self._require_open()
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not media_type.strip():
            raise ValueError("media_type must not be blank")
        digest = sha256(payload).hexdigest()
        relative_path = Path("sha256") / digest[:2] / f"{digest}.bin"
        destination = self._payload_path(relative_path)

        artifact = PayloadArtifact(
            digest=digest,
            relative_path=relative_path,
            byte_length=len(payload),
            media_type=media_type,
        )
        with self._payload_mutation_scope("publisher", digest):
            existing = self._connection.execute(
                "SELECT relative_path, byte_length, media_type "
                "FROM payload_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
            expected_metadata = (
                artifact.relative_path.as_posix(),
                artifact.byte_length,
                artifact.media_type,
            )
            if existing is not None:
                if existing != expected_metadata:
                    raise MarketHistoryCorruptionError(
                        f"payload metadata conflicts for sha256:{digest}"
                    )
                self._verify_payload_file(destination, digest, len(payload))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    self._verify_payload_file(destination, digest, len(payload))
                else:
                    self._atomic_install(destination, payload)
            self._phase_hook("publisher_file_installed", digest)

            if existing is None:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._connection.execute(
                        "INSERT INTO payload_artifacts "
                        "(digest, relative_path, byte_length, media_type, installed_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            artifact.digest,
                            artifact.relative_path.as_posix(),
                            artifact.byte_length,
                            artifact.media_type,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise
            self._phase_hook("publisher_payload_row_committed", digest)
            self._verify_registered_payload(artifact)
            return artifact

    def read_payload(self, digest: str) -> bytes:
        """Read and verify immutable bytes by their content digest."""
        self._require_open()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("digest must be 64 lowercase hexadecimal characters")
        row = self._connection.execute(
            "SELECT relative_path, byte_length FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown payload sha256:{digest}")
        path = self._payload_path(Path(str(row[0])))
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise MarketHistoryCorruptionError(
                f"cannot read committed payload {path}: {exc}"
            ) from exc
        self._verify_payload_bytes(payload, digest, int(row[1]))
        return payload

    def orphan_payload_digests(self) -> tuple[str, ...]:
        """Return installed payloads that no durable market-history record references."""
        self._require_open()
        return tuple(str(row[0]) for row in self._orphan_payload_rows())

    def _orphan_payload_rows(self) -> tuple[tuple[object, object], ...]:
        return tuple(
            self._connection.execute(
                "SELECT p.digest, p.relative_path FROM payload_artifacts AS p "
                "WHERE NOT EXISTS (SELECT 1 FROM ingestion_runs AS r "
                "WHERE r.payload_digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM raw_market_observation_revisions AS r "
                "WHERE r.payload_digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM trading_status_revisions AS r "
                "WHERE r.payload_digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM adjustment_factor_revisions AS r "
                "WHERE r.payload_digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM provider_frame_revisions AS r "
                "WHERE r.payload_digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM market_session_calendars AS r "
                "WHERE r.payload_digest = p.digest) "
                "ORDER BY p.digest"
            )
        )

    def collect_orphan_payloads(self) -> PayloadCollectionResult:
        """Remove payload rows and files that no durable market-history record references."""
        self._require_open()
        row_candidates = self._orphan_payload_rows()
        payload_root = Path(self.config.payload_root)
        file_candidates: tuple[Path, ...] = ()
        if payload_root.exists():
            file_candidates = tuple(sorted(payload_root.glob("sha256/*/*.bin")))

        outcomes: list[PayloadMaintenanceOutcome] = []
        row_candidate_paths: set[Path] = set()
        for digest_value, relative_path_value in row_candidates:
            digest = str(digest_value)
            relative_path = Path(str(relative_path_value))
            row_candidate_paths.add(self._payload_path(relative_path))
            outcomes.append(
                self._collect_registered_payload_candidate(
                    digest,
                    relative_path,
                )
            )

        for candidate in file_candidates:
            checked = self._checked_path(
                payload_root,
                candidate.relative_to(payload_root),
            )
            if checked in row_candidate_paths:
                continue
            outcomes.append(
                self._collect_unregistered_payload_candidate(
                    checked.stem,
                    checked,
                )
            )
        return PayloadCollectionResult(tuple(outcomes))

    def _collect_registered_payload_candidate(
        self,
        digest: str,
        relative_path: Path,
    ) -> PayloadMaintenanceOutcome:
        try:
            with self._payload_mutation_scope("gc", digest):
                connection = self._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT relative_path, byte_length "
                        "FROM payload_artifacts WHERE digest = ?",
                        (digest,),
                    ).fetchone()
                    if row is None or self._payload_has_reference(connection, digest):
                        connection.commit()
                        return PayloadMaintenanceOutcome(
                            digest,
                            PayloadMaintenanceStatus.SKIPPED,
                        )
                    if str(row[0]) != relative_path.as_posix():
                        raise MarketHistoryCorruptionError(
                            f"payload metadata path changed for sha256:{digest}"
                        )
                    self._verify_payload_file(
                        self._payload_path(relative_path),
                        digest,
                        int(row[1]),
                    )
                    connection.execute(
                        "DELETE FROM payload_artifacts WHERE digest = ?",
                        (digest,),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self._phase_hook("gc_row_delete_committed", digest)
                return self._unlink_payload_candidate(
                    digest,
                    self._payload_path(relative_path),
                )
        except PayloadMutationTimeoutError as exc:
            return PayloadMaintenanceOutcome(
                digest,
                PayloadMaintenanceStatus.MUTEX_TIMEOUT,
                retryable=True,
                detail=str(exc),
            )

    def _collect_unregistered_payload_candidate(
        self,
        digest: str,
        path: Path,
    ) -> PayloadMaintenanceOutcome:
        try:
            with self._payload_mutation_scope("gc", digest):
                connection = self._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT 1 FROM payload_artifacts WHERE digest = ?",
                        (digest,),
                    ).fetchone()
                    if row is not None or self._payload_has_reference(
                        connection,
                        digest,
                    ):
                        connection.commit()
                        return PayloadMaintenanceOutcome(
                            digest,
                            PayloadMaintenanceStatus.SKIPPED,
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                return self._unlink_payload_candidate(digest, path)
        except PayloadMutationTimeoutError as exc:
            return PayloadMaintenanceOutcome(
                digest,
                PayloadMaintenanceStatus.MUTEX_TIMEOUT,
                retryable=True,
                detail=str(exc),
            )

    def _unlink_payload_candidate(
        self,
        digest: str,
        path: Path,
    ) -> PayloadMaintenanceOutcome:
        self._phase_hook("gc_before_unlink", digest)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return PayloadMaintenanceOutcome(
                digest,
                PayloadMaintenanceStatus.UNLINK_FAILED,
                retryable=True,
                detail=f"{type(exc).__name__}: {exc}",
            )
        self._phase_hook("gc_after_unlink_before_mutex_release", digest)
        return PayloadMaintenanceOutcome(
            digest,
            PayloadMaintenanceStatus.COLLECTED,
        )

    @staticmethod
    def _payload_has_reference(
        connection: sqlite3.Connection,
        digest: str,
    ) -> bool:
        return any(
            connection.execute(
                f"SELECT 1 FROM {table} WHERE payload_digest = ? LIMIT 1",
                (digest,),
            ).fetchone()
            is not None
            for table in _PAYLOAD_REFERENCE_TABLES
        )

    def create_backup(self) -> MarketHistoryBackup:
        """Publish a verified database snapshot and every referenced payload together."""
        self._require_open()
        with self._payload_mutation_scope(None, "backup"):
            return self._create_backup_locked()

    def _create_backup_locked(self) -> MarketHistoryBackup:
        backup_root = Path(self.config.backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=".backup-", dir=backup_root))
        created_at = datetime.now(timezone.utc).isoformat()
        database_name = "market_history.sqlite3"
        database_path = temporary_root / database_name
        payload_entries: list[dict[str, object]] = []
        try:
            backup_connection = sqlite3.connect(database_path)
            try:
                self._connection.backup(backup_connection)
                rows = tuple(
                    backup_connection.execute(
                        "SELECT digest, relative_path, byte_length, media_type "
                        "FROM payload_artifacts ORDER BY digest"
                    )
                )
            finally:
                backup_connection.close()

            for digest, relative_path_text, byte_length, media_type in rows:
                relative_path = Path(str(relative_path_text))
                payload = self.read_payload(str(digest))
                if len(payload) != int(byte_length):
                    raise MarketHistoryCorruptionError(
                        f"payload length changed during backup for sha256:{digest}"
                    )
                backup_payload_path = temporary_root / "payloads" / relative_path
                backup_payload_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_new_file(backup_payload_path, payload)
                payload_entries.append(
                    {
                        "byte_length": int(byte_length),
                        "digest": str(digest),
                        "media_type": str(media_type),
                        "relative_path": relative_path.as_posix(),
                    }
                )

            database_bytes = database_path.read_bytes()
            database_sha256 = sha256(database_bytes).hexdigest()
            schema_version = self.status().schema_version
            manifest = {
                "created_at": created_at,
                "data_usage_mode": self.config.data_usage_mode.value,
                "database": {
                    "byte_length": len(database_bytes),
                    "filename": database_name,
                    "sha256": database_sha256,
                },
                "format_version": "1.0",
                "payloads": payload_entries,
                "schema_version": schema_version,
            }
            manifest_path = temporary_root / "manifest.json"
            self._write_new_file(
                manifest_path,
                (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            )
            final_path = backup_root / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                + f"-{uuid4().hex[:12]}"
            )
            os.replace(temporary_root, final_path)
            return MarketHistoryBackup(
                path=final_path,
                manifest_path=final_path / "manifest.json",
                created_at=created_at,
                database_sha256=database_sha256,
            )
        except BaseException:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise

    def publish_provider_frame(
        self,
        publication: ProviderFramePublication,
    ) -> StoredProviderFrame:
        digest = sha256(publication.normalized_frame).hexdigest()
        with self._payload_mutation_scope("publisher", digest):
            stored = self._publish_provider_frame_locked(publication)
            self._phase_hook("publisher_references_committed", digest)
            self._verify_registered_payload_digest(digest)
            return stored

    def _publish_provider_frame_locked(
        self,
        publication: ProviderFramePublication,
    ) -> StoredProviderFrame:
        """Publish one independently validated current-provider frame in shadow state."""
        self._require_open()
        if publication.history_rows < 1:
            raise ValueError("provider frame must contain at least one history row")
        if sha256(publication.normalized_frame).hexdigest() != publication.frame_digest:
            raise ValueError("normalized provider frame does not match frame_digest")
        payload = self.install_payload(
            publication.normalized_frame,
            media_type="text/csv; charset=utf-8",
        )
        frame_revision_id = revision_identity(
            RevisionKind.PROVIDER_FRAME,
            {
                "adjustment_basis": publication.adjustment_basis,
                "effective_trading_date": publication.effective_trading_date,
                "frame_digest": publication.frame_digest,
                "history_rows": publication.history_rows,
                "instrument_id": publication.instrument_id,
                "payload_digest": payload.digest,
                "provider_dataset_id": publication.provider_dataset_id,
                "requested_end": publication.requested_end,
                "requested_start": publication.requested_start,
                "snapshot_id": publication.snapshot_id,
            },
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT OR IGNORE INTO upstream_services "
                "(upstream_service_id, service_name, account_scope, created_at) "
                "VALUES (?, ?, '', ?)",
                (
                    publication.upstream_service_id,
                    publication.upstream_service_name,
                    publication.retrieved_at,
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO provider_datasets "
                "(provider_dataset_id, provider_name, upstream_service_id, dataset_name, "
                "adjustment_methodology, strict_history_qualified, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    publication.provider_dataset_id,
                    publication.provider_name,
                    publication.upstream_service_id,
                    publication.dataset_name,
                    publication.adjustment_methodology,
                    int(publication.strict_history_qualified),
                    publication.retrieved_at,
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO instruments "
                "(instrument_id, canonical_symbol, reference_market, instrument_kind, "
                "currency, identity_revision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    publication.instrument_id,
                    publication.canonical_symbol,
                    publication.reference_market,
                    publication.instrument_kind,
                    publication.currency,
                    publication.identity_revision,
                    publication.retrieved_at,
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO provider_frame_revisions "
                "(frame_revision_id, snapshot_id, provider_dataset_id, instrument_id, "
                "requested_start, requested_end, effective_trading_date, adjustment_basis, "
                "frame_digest, history_rows, payload_digest, retrieved_at, current_only) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    frame_revision_id,
                    publication.snapshot_id,
                    publication.provider_dataset_id,
                    publication.instrument_id,
                    publication.requested_start,
                    publication.requested_end,
                    publication.effective_trading_date,
                    publication.adjustment_basis,
                    publication.frame_digest,
                    publication.history_rows,
                    payload.digest,
                    publication.retrieved_at,
                ),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.get_provider_frame(publication.snapshot_id)

    def get_provider_frame(self, snapshot_id: str) -> StoredProviderFrame:
        self._require_open()
        row = self._connection.execute(
            "SELECT f.frame_revision_id, f.snapshot_id, f.provider_dataset_id, "
            "d.provider_name, f.instrument_id, i.canonical_symbol, f.requested_start, "
            "f.requested_end, f.effective_trading_date, f.adjustment_basis, "
            "f.frame_digest, f.history_rows, f.payload_digest, f.retrieved_at, "
            "f.current_only FROM provider_frame_revisions AS f "
            "JOIN provider_datasets AS d ON d.provider_dataset_id = f.provider_dataset_id "
            "JOIN instruments AS i ON i.instrument_id = f.instrument_id "
            "WHERE f.snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown provider frame snapshot {snapshot_id}")
        return StoredProviderFrame(
            frame_revision_id=str(row[0]),
            snapshot_id=str(row[1]),
            provider_dataset_id=str(row[2]),
            provider_name=str(row[3]),
            instrument_id=str(row[4]),
            canonical_symbol=str(row[5]),
            requested_start=str(row[6]),
            requested_end=str(row[7]),
            effective_trading_date=str(row[8]),
            adjustment_basis=str(row[9]),
            frame_digest=str(row[10]),
            history_rows=int(row[11]),
            payload_digest=str(row[12]),
            retrieved_at=str(row[13]),
            current_only=bool(row[14]),
        )

    def load_current_provider_frame(
        self,
        canonical_symbol: str,
        requested_start: str,
        requested_end: str,
        *,
        minimum_history_rows: int,
    ) -> LoadedProviderFrame:
        self._require_open()
        row = self._connection.execute(
            "SELECT f.snapshot_id FROM provider_frame_revisions AS f "
            "JOIN instruments AS i ON i.instrument_id = f.instrument_id "
            "WHERE i.canonical_symbol = ? AND f.requested_start = ? "
            "AND f.requested_end = ? AND f.current_only = 1 "
            "ORDER BY f.retrieved_at DESC LIMIT 1",
            (canonical_symbol, requested_start, requested_end),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"no stored current frame for {canonical_symbol} "
                f"from {requested_start} to {requested_end}"
            )
        stored = self.get_provider_frame(str(row[0]))
        if stored.history_rows < minimum_history_rows:
            raise KeyError(
                f"stored frame has {stored.history_rows} rows; "
                f"{minimum_history_rows} required"
            )
        text = self.read_payload(stored.payload_digest).decode("utf-8")
        frame = pd.read_csv(io.StringIO(text))
        frame["Date"] = pd.to_datetime(frame["Date"])
        for column in ("Open", "High", "Low", "Close"):
            if column in frame:
                frame[column] = frame[column].astype(float)
        if len(frame) != stored.history_rows:
            raise MarketHistoryCorruptionError("stored provider frame row count changed")
        if normalized_frame_sha256(frame) != stored.frame_digest:
            raise MarketHistoryCorruptionError("stored provider frame digest changed")
        return LoadedProviderFrame(stored=stored, frame=frame)

    def latest_qualified_history_bundle(
        self,
        canonical_symbol: str,
        requested_as_of: str,
    ) -> StoredHistoryBundleRef:
        """Select one complete strict-history bundle, never a current-only frame."""
        self._require_open()
        row = self._connection.execute(
            "SELECT b.bundle_revision_id, b.observed_at "
            "FROM history_bundle_revisions AS b "
            "JOIN provider_datasets AS d "
            "ON d.provider_dataset_id = b.provider_dataset_id "
            "JOIN instruments AS i ON i.instrument_id = b.instrument_id "
            "WHERE i.canonical_symbol = ? AND d.strict_history_qualified = 1 "
            "AND b.requested_as_of <= ? "
            "ORDER BY b.requested_as_of DESC, b.observed_at DESC, "
            "b.bundle_revision_id DESC LIMIT 1",
            (canonical_symbol, requested_as_of),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"no qualified history bundle for {canonical_symbol} "
                f"as of {requested_as_of}"
            )
        return StoredHistoryBundleRef(str(row[0]), str(row[1]))

    def record_mainland_equivalence(
        self,
        scenario: MainlandEquivalenceScenario,
        comparison: MainlandEquivalenceComparison,
        *,
        frame_revision_id: str | None = None,
        reconstructed_snapshot_id: str | None = None,
    ) -> str:
        self._require_open()
        if not isinstance(scenario, MainlandEquivalenceScenario):
            raise TypeError("scenario must be a MainlandEquivalenceScenario")
        compared_at = datetime.now(timezone.utc).isoformat()
        detail = {
            "adjustment_basis_matches": comparison.adjustment_basis_matches,
            "derived_facts_match": comparison.derived_facts_match,
            "effective_range_matches": comparison.effective_range_matches,
            "eligible_observations_match": comparison.eligible_observations_match,
            "frame_digest_matches": comparison.frame_digest_matches,
            "identity_matches": comparison.identity_matches,
            "lineage_matches": comparison.lineage_matches,
            "provider_matches": comparison.provider_matches,
        }
        result_id = revision_identity(
            RevisionKind.EQUIVALENCE_RESULT,
            {
                "compared_at": compared_at,
                "frame_revision_id": frame_revision_id,
                "passed": comparison.passed,
                "reconstructed_snapshot_id": reconstructed_snapshot_id,
                "scenario": scenario.value,
            },
        )
        self._connection.execute(
            "INSERT INTO shadow_equivalence_results "
            "(equivalence_result_id, scenario, frame_revision_id, reconstructed_snapshot_id, "
            "compared_at, identity_matches, provider_matches, adjustment_basis_matches, "
            "effective_range_matches, eligible_observations_match, frame_digest_matches, "
            "derived_facts_match, lineage_matches, passed, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                scenario.value,
                frame_revision_id,
                reconstructed_snapshot_id,
                compared_at,
                int(comparison.identity_matches),
                int(comparison.provider_matches),
                int(comparison.adjustment_basis_matches),
                int(comparison.effective_range_matches),
                int(comparison.eligible_observations_match),
                int(comparison.frame_digest_matches),
                int(comparison.derived_facts_match),
                int(comparison.lineage_matches),
                int(comparison.passed),
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            ),
        )
        return result_id

    def latest_mainland_equivalence_results(self) -> dict[str, bool]:
        self._require_open()
        latest: dict[str, bool] = {}
        for scenario, passed in self._connection.execute(
            "SELECT scenario, passed FROM shadow_equivalence_results "
            "ORDER BY compared_at, equivalence_result_id"
        ):
            latest[str(scenario)] = bool(passed)
        return latest

    def mainland_cutover_ready(self) -> bool:
        latest = self.latest_mainland_equivalence_results()
        return all(latest.get(scenario.value) is True for scenario in DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS)

    def publish_history_bundle(
        self,
        publication: ProviderHistoryBundlePublication,
        *,
        prior_bundle_revision_id: str | None = None,
    ) -> PublishedHistoryBundle:
        self._require_open()
        from tradingagents.market_history.bundle import publish_history_bundle

        digest = sha256(publication.raw_payload).hexdigest()
        with self._payload_mutation_scope("publisher", digest):
            published = publish_history_bundle(
                self,
                publication,
                prior_bundle_revision_id=prior_bundle_revision_id,
            )
            self._phase_hook("publisher_references_committed", digest)
            self._verify_registered_payload_digest(digest)
            return published

    def publish_session_calendar(
        self,
        publication: MarketSessionCalendarPublication,
    ) -> PublishedSessionCalendar:
        self._require_open()
        from tradingagents.market_history.calendar import publish_session_calendar

        digest = sha256(publication.raw_payload).hexdigest()
        with self._payload_mutation_scope("publisher", digest):
            published = publish_session_calendar(self, publication)
            self._phase_hook("publisher_references_committed", digest)
            self._verify_registered_payload_digest(digest)
            return published

    def read_history_bundle_provenance_audit(
        self,
        bundle_revision_id: str,
    ) -> HistoryBundleProvenanceAudit:
        self._require_open()
        from tradingagents.market_history.bundle import (
            read_history_bundle_provenance_audit,
        )

        return read_history_bundle_provenance_audit(self, bundle_revision_id)

    def read_current_session_calendar(
        self,
        reference_market: str,
    ) -> StoredSessionCalendar:
        self._require_open()
        from tradingagents.market_history.calendar import read_current_session_calendar

        return read_current_session_calendar(self, reference_market)

    def record_publication_watermark(self, **kwargs):
        self._require_open()
        from tradingagents.market_history.maintenance import (
            record_publication_watermark,
        )

        return record_publication_watermark(self, **kwargs)

    def current_publication_watermark(
        self,
        instrument_id,
        provider_dataset_id,
        expected_session_date,
    ):
        self._require_open()
        from tradingagents.market_history.maintenance import (
            current_publication_watermark,
        )

        return current_publication_watermark(
            self,
            instrument_id,
            provider_dataset_id,
            expected_session_date,
        )

    def list_due_reconciliations(self, as_of):
        self._require_open()
        from tradingagents.market_history.maintenance import list_due_reconciliations

        return list_due_reconciliations(self, as_of)

    def set_instrument_lifecycle_state(
        self,
        instrument_id,
        provider_dataset_id,
        lifecycle_state,
    ) -> None:
        self._require_open()
        from tradingagents.market_history.maintenance import (
            set_instrument_lifecycle_state,
        )

        set_instrument_lifecycle_state(
            self,
            instrument_id,
            provider_dataset_id,
            lifecycle_state,
        )

    def mark_reconciled(
        self,
        instrument_id,
        provider_dataset_id,
        reconciled_at,
    ) -> None:
        self._require_open()
        from tradingagents.market_history.maintenance import mark_reconciled

        mark_reconciled(
            self,
            instrument_id,
            provider_dataset_id,
            reconciled_at,
        )

    def maintenance_summary(self, *, as_of, now):
        self._require_open()
        from tradingagents.market_history.maintenance import maintenance_summary

        return maintenance_summary(self, as_of=as_of, now=now)

    def reconstruct_snapshot(
        self,
        bundle_revision_id: str,
        *,
        requested_date,
        purpose: SnapshotPurpose,
        replay_as_of: datetime | None = None,
        asset_configuration: RunAssetConfiguration | None = None,
        crypto_provider_dataset: CryptoProviderDatasetDescriptor | None = None,
    ) -> ReconstructedMarketSnapshot:
        self._require_open()
        from tradingagents.market_history.bundle import (
            read_pinned_snapshot,
            reconstruct_snapshot,
        )

        with self._payload_mutation_scope("publisher", bundle_revision_id):
            reconstructed = reconstruct_snapshot(
                self,
                bundle_revision_id,
                requested_date=requested_date,
                purpose=purpose,
                replay_as_of=replay_as_of,
                pin=True,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=crypto_provider_dataset,
            )
            self._phase_hook(
                "publisher_references_committed",
                bundle_revision_id,
            )
            read_pinned_snapshot(
                self,
                reconstructed.snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=crypto_provider_dataset,
            )
            return reconstructed

    def read_pinned_snapshot(
        self,
        snapshot_id: str,
        *,
        asset_configuration: RunAssetConfiguration | None = None,
        crypto_provider_dataset: CryptoProviderDatasetDescriptor | None = None,
    ) -> ReconstructedMarketSnapshot:
        self._require_open()
        from tradingagents.market_history.bundle import read_pinned_snapshot

        return read_pinned_snapshot(
            self,
            snapshot_id,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=crypto_provider_dataset,
        )

    @classmethod
    def restore_backup(cls, backup_path: Path, config: MarketHistoryConfig) -> None:
        """Verify a complete backup before atomically publishing its database."""
        backup_root = Path(backup_path)
        manifest_path = backup_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketHistoryCorruptionError(
                f"cannot read market-history backup manifest {manifest_path}: {exc}"
            ) from exc
        if manifest.get("format_version") != "1.0":
            raise MarketHistoryCorruptionError("unsupported market-history backup format")
        if manifest.get("data_usage_mode") != config.data_usage_mode.value:
            raise MarketHistoryCorruptionError(
                "backup data-usage mode does not match the restore configuration"
            )
        database = manifest.get("database")
        payloads = manifest.get("payloads")
        if not isinstance(database, dict) or not isinstance(payloads, list):
            raise MarketHistoryCorruptionError("market-history backup manifest is incomplete")
        source_database = cls._checked_path(
            backup_root,
            Path(str(database.get("filename", ""))),
        )
        cls._verify_backup_file(
            source_database,
            str(database.get("sha256", "")),
            database.get("byte_length"),
        )
        cls._verify_backup_database(source_database, manifest, payloads)
        verified_payloads: list[tuple[str, Path, Path, object]] = []
        for item in payloads:
            if not isinstance(item, dict):
                raise MarketHistoryCorruptionError(
                    "invalid payload entry in backup manifest"
                )
            digest = str(item.get("digest", ""))
            relative_path = Path(str(item.get("relative_path", "")))
            source_payload = cls._checked_path(
                backup_root / "payloads",
                relative_path,
            )
            byte_length = item.get("byte_length")
            cls._verify_backup_file(source_payload, digest, byte_length)
            verified_payloads.append(
                (digest, relative_path, source_payload, byte_length)
            )

        target_database = Path(config.database_path)
        if target_database.exists():
            raise FileExistsError(f"refusing to overwrite Market History Database {target_database}")
        with _acquire_payload_mutation_mutex(config.database_path):
            if target_database.exists():
                raise FileExistsError(
                    "refusing to overwrite Market History Database "
                    f"{target_database}"
                )
            cls._claim_payload_root(config)
            installed_payloads: list[Path] = []
            staged_database: Path | None = None
            installed_database = False
            try:
                for (
                    digest,
                    relative_path,
                    source_payload,
                    byte_length,
                ) in verified_payloads:
                    target_payload = cls._checked_path(
                        Path(config.payload_root),
                        relative_path,
                    )
                    if target_payload.exists():
                        cls._verify_backup_file(
                            target_payload,
                            digest,
                            byte_length,
                        )
                        continue
                    target_payload.parent.mkdir(parents=True, exist_ok=True)
                    cls._atomic_copy_install(
                        source_payload,
                        target_payload,
                        digest,
                        byte_length,
                    )
                    installed_payloads.append(target_payload)

                target_database.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=target_database.parent,
                    prefix=f".{target_database.name}.",
                    suffix=".restore",
                    delete=False,
                ) as handle:
                    staged_database = Path(handle.name)
                shutil.copyfile(source_database, staged_database)
                with staged_database.open("r+b") as handle:
                    os.fsync(handle.fileno())
                cls._verify_backup_file(
                    staged_database,
                    str(database.get("sha256", "")),
                    database.get("byte_length"),
                )
                os.replace(staged_database, target_database)
                staged_database = None
                installed_database = True
                cls._verify_backup_file(
                    target_database,
                    str(database.get("sha256", "")),
                    database.get("byte_length"),
                )
                cls._verify_backup_database(
                    target_database,
                    manifest,
                    payloads,
                )
                for (
                    digest,
                    relative_path,
                    _source_payload,
                    byte_length,
                ) in verified_payloads:
                    cls._verify_backup_file(
                        cls._checked_path(
                            Path(config.payload_root),
                            relative_path,
                        ),
                        digest,
                        byte_length,
                    )
            except BaseException:
                if staged_database is not None and staged_database.exists():
                    staged_database.unlink()
                if installed_database:
                    target_database.unlink(missing_ok=True)
                for installed in reversed(installed_payloads):
                    installed.unlink(missing_ok=True)
                raise

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA busy_timeout = 30000")

    def _apply_migrations(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(CREATE_MIGRATION_TABLE)
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            assert row is not None
            current_version = int(row[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    "Market History Database schema is newer than this runtime: "
                    f"database={current_version}, runtime={SCHEMA_VERSION}"
                )
            if current_version < 1:
                for statement in MIGRATION_V1:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        1,
                        "initial_point_in_time_market_history",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 1
            if current_version < 2:
                self._upgrade_legacy_equivalence_results()
                for statement in MIGRATION_V2:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        2,
                        "provider_request_priority_queue",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 2
            if current_version < 3:
                for statement in MIGRATION_V3:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        3,
                        "snapshot_v2_exact_pin_manifest",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 3
            if current_version < 4:
                for statement in MIGRATION_V4:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        4,
                        "incremental_provenance_membership_audit",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 4
            if current_version < 5:
                for statement in MIGRATION_V5:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        5,
                        "provider_physical_attempt_audit",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 5
            if current_version < 6:
                for statement in MIGRATION_V6:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        6,
                        "provider_request_single_flight_handoff",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 6
            if current_version < 7:
                for statement in MIGRATION_V7:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        7,
                        "provider_request_authority_import",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current_version = 7
            attempt_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(provider_request_attempts)"
                )
            }
            if current_version < 8 or not {
                "attempt_event_id",
                "terminal_outcome",
            }.issubset(attempt_columns):
                for statement_index, statement in enumerate(MIGRATION_V8):
                    if statement_index == 0 and "attempt_event_id" in attempt_columns:
                        continue
                    if statement_index == 1 and "terminal_outcome" in attempt_columns:
                        continue
                    self._connection.execute(statement)
                if current_version < 8:
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (
                            8,
                            "provider_physical_attempt_typed_lifecycle",
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    current_version = 8
            scoped_tables = {
                "request_leases": {
                    str(row[1])
                    for row in self._connection.execute(
                        "PRAGMA table_info(request_leases)"
                    )
                },
                "provider_request_sequences": {
                    str(row[1])
                    for row in self._connection.execute(
                        "PRAGMA table_info(provider_request_sequences)"
                    )
                },
                "provider_request_attempts": {
                    str(row[1])
                    for row in self._connection.execute(
                        "PRAGMA table_info(provider_request_attempts)"
                    )
                },
            }
            scoped_columns = (
                ("request_leases", "capacity_scope"),
                ("provider_request_sequences", "capacity_scope"),
                ("provider_request_attempts", "capacity_scope"),
                ("provider_request_sequences", "failure_outcome_kind"),
                ("provider_request_attempts", "terminal_outcome_kind"),
            )
            if current_version < 9 or any(
                column not in scoped_tables[table]
                for table, column in scoped_columns
            ):
                for statement, (table, column) in zip(
                    MIGRATION_V9,
                    scoped_columns,
                    strict=True,
                ):
                    if column in scoped_tables[table]:
                        continue
                    self._connection.execute(statement)
                if current_version < 9:
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (
                            9,
                            "provider_request_endpoint_capacity_scope",
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
            foreign_key_failures = tuple(
                self._connection.execute("PRAGMA foreign_key_check")
            )
            if foreign_key_failures:
                raise MarketHistoryCorruptionError(
                    "Market History Database migration failed foreign-key verification"
                )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _upgrade_legacy_equivalence_results(self) -> None:
        columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(shadow_equivalence_results)"
            )
        }
        if "scenario" in columns:
            return
        self._connection.execute(
            "ALTER TABLE shadow_equivalence_results "
            "RENAME TO shadow_equivalence_results_v1"
        )
        self._connection.execute(
            "CREATE TABLE shadow_equivalence_results ("
            "equivalence_result_id TEXT PRIMARY KEY, scenario TEXT NOT NULL, "
            "frame_revision_id TEXT REFERENCES provider_frame_revisions(frame_revision_id), "
            "reconstructed_snapshot_id TEXT, compared_at TEXT NOT NULL, "
            "identity_matches INTEGER NOT NULL CHECK (identity_matches IN (0, 1)), "
            "provider_matches INTEGER NOT NULL CHECK (provider_matches IN (0, 1)), "
            "adjustment_basis_matches INTEGER NOT NULL CHECK (adjustment_basis_matches IN (0, 1)), "
            "effective_range_matches INTEGER NOT NULL CHECK (effective_range_matches IN (0, 1)), "
            "eligible_observations_match INTEGER NOT NULL CHECK "
            "(eligible_observations_match IN (0, 1)), "
            "frame_digest_matches INTEGER NOT NULL CHECK (frame_digest_matches IN (0, 1)), "
            "derived_facts_match INTEGER NOT NULL CHECK (derived_facts_match IN (0, 1)), "
            "lineage_matches INTEGER NOT NULL CHECK (lineage_matches IN (0, 1)), "
            "passed INTEGER NOT NULL CHECK (passed IN (0, 1)), detail_json TEXT NOT NULL)"
        )
        self._connection.execute(
            "INSERT INTO shadow_equivalence_results "
            "SELECT equivalence_result_id, 'legacy_unclassified', frame_revision_id, "
            "reconstructed_snapshot_id, compared_at, identity_matches, provider_matches, "
            "adjustment_basis_matches, effective_range_matches, eligible_observations_match, "
            "frame_digest_matches, derived_facts_match, lineage_matches, passed, detail_json "
            "FROM shadow_equivalence_results_v1"
        )
        self._connection.execute("DROP TABLE shadow_equivalence_results_v1")

    def _pragma_value(self, name: str) -> object:
        row = self._connection.execute(f"PRAGMA {name}").fetchone()
        if row is None:
            raise RuntimeError(f"SQLite did not return PRAGMA {name}")
        return row[0]

    def _payload_path(self, relative_path: Path) -> Path:
        return self._checked_path(Path(self.config.payload_root), relative_path)

    @contextmanager
    def _payload_mutation_scope(
        self,
        operation: str | None,
        operation_subject: str,
    ) -> Iterator[None]:
        if self._payload_mutation_connection is not None:
            yield
            return
        if self._connection.in_transaction:
            raise PayloadLockOrderError(
                "payload-mutation mutex must be acquired before the "
                "Market History Database transaction"
            )

        if operation is not None:
            self._phase_hook(
                f"{operation}_mutex_waiting",
                operation_subject,
            )
        with _acquire_payload_mutation_mutex(
            self.config.database_path
        ) as connection:
            self._payload_mutation_connection = connection
            try:
                if operation is not None:
                    self._phase_hook(
                        f"{operation}_mutex_acquired",
                        operation_subject,
                    )
                yield
            finally:
                self._payload_mutation_connection = None

    def _verify_registered_payload(self, artifact: PayloadArtifact) -> None:
        row = self._connection.execute(
            "SELECT relative_path, byte_length, media_type "
            "FROM payload_artifacts WHERE digest = ?",
            (artifact.digest,),
        ).fetchone()
        expected = (
            artifact.relative_path.as_posix(),
            artifact.byte_length,
            artifact.media_type,
        )
        if row != expected:
            raise MarketHistoryCorruptionError(
                f"payload metadata conflicts for sha256:{artifact.digest}"
            )
        self._verify_payload_file(
            self._payload_path(artifact.relative_path),
            artifact.digest,
            artifact.byte_length,
        )

    def _verify_registered_payload_digest(self, digest: str) -> None:
        row = self._connection.execute(
            "SELECT relative_path, byte_length, media_type "
            "FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise MarketHistoryCorruptionError(
                f"payload metadata is missing for sha256:{digest}"
            )
        self._verify_registered_payload(
            PayloadArtifact(
                digest=digest,
                relative_path=Path(str(row[0])),
                byte_length=int(row[1]),
                media_type=str(row[2]),
            )
        )

    def _atomic_install(self, destination: Path, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            self._verify_payload_file(destination, sha256(payload).hexdigest(), len(payload))
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _write_new_file(path: Path, payload: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _atomic_copy_install(
        cls,
        source: Path,
        destination: Path,
        digest: str,
        byte_length: object,
    ) -> None:
        temporary_path: Path | None = None
        installed_destination = False
        try:
            with source.open("rb") as source_handle, tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".restore",
                delete=False,
            ) as target_handle:
                temporary_path = Path(target_handle.name)
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            cls._verify_backup_file(temporary_path, digest, byte_length)
            os.replace(temporary_path, destination)
            temporary_path = None
            installed_destination = True
            cls._verify_backup_file(destination, digest, byte_length)
        except BaseException:
            if installed_destination:
                destination.unlink(missing_ok=True)
            raise
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _checked_path(root: Path, relative_path: Path) -> Path:
        resolved_root = root.resolve()
        candidate = (resolved_root / relative_path).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise MarketHistoryCorruptionError(
                f"path escapes configured root: {relative_path}"
            )
        return candidate

    @staticmethod
    def _verify_backup_file(path: Path, digest: str, byte_length: object) -> None:
        try:
            expected_length = int(byte_length)
            payload = path.read_bytes()
        except (OSError, TypeError, ValueError) as exc:
            raise MarketHistoryCorruptionError(f"cannot verify backup file {path}: {exc}") from exc
        MarketHistoryStore._verify_payload_bytes(payload, digest, expected_length)

    @staticmethod
    def _verify_backup_database(
        database_path: Path,
        manifest: dict[str, object],
        payloads: list[object],
    ) -> None:
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise MarketHistoryCorruptionError(
                    f"backup database integrity check failed: {integrity}"
                )
            foreign_key_errors = tuple(connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_errors:
                raise MarketHistoryCorruptionError(
                    f"backup database foreign-key check failed: {foreign_key_errors}"
                )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            database_schema_version = int(row[0]) if row is not None else 0
            if database_schema_version != manifest.get("schema_version"):
                raise MarketHistoryCorruptionError(
                    "backup manifest schema version does not match its database"
                )
            if database_schema_version > SCHEMA_VERSION:
                raise MarketHistoryCorruptionError(
                    "backup database schema is newer than this runtime"
                )
            database_payloads = tuple(
                connection.execute(
                    "SELECT digest, relative_path, byte_length, media_type "
                    "FROM payload_artifacts ORDER BY digest"
                )
            )
            manifest_payloads = tuple(
                (
                    str(item.get("digest")),
                    str(item.get("relative_path")),
                    int(item.get("byte_length")),
                    str(item.get("media_type")),
                )
                for item in payloads
                if isinstance(item, dict)
            )
            if database_payloads != manifest_payloads or len(manifest_payloads) != len(payloads):
                raise MarketHistoryCorruptionError(
                    "backup payload manifest does not match its database"
                )
        except sqlite3.DatabaseError as exc:
            raise MarketHistoryCorruptionError(
                f"cannot validate backup database {database_path}: {exc}"
            ) from exc
        finally:
            connection.close()

    def _verify_payload_file(
        self,
        path: Path,
        expected_digest: str,
        expected_length: int,
    ) -> None:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise MarketHistoryCorruptionError(f"cannot read payload {path}: {exc}") from exc
        self._verify_payload_bytes(payload, expected_digest, expected_length)

    @staticmethod
    def _verify_payload_bytes(
        payload: bytes,
        expected_digest: str,
        expected_length: int,
    ) -> None:
        actual_digest = sha256(payload).hexdigest()
        if len(payload) != expected_length or actual_digest != expected_digest:
            raise MarketHistoryCorruptionError(
                "content-addressed payload failed verification: "
                f"expected sha256:{expected_digest} ({expected_length} bytes), "
                f"got sha256:{actual_digest} ({len(payload)} bytes)"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Market History Store is closed")
