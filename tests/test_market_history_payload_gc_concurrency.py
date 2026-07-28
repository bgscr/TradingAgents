from __future__ import annotations

import multiprocessing
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from tradingagents.market_history import (
    AdjustmentFactorObservation,
    DataUsageMode,
    InstrumentSpec,
    MarketHistoryConfig,
    MarketHistoryCorruptionError,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    PayloadMaintenanceStatus,
    PayloadMutationTimeoutError,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderFramePublication,
    ProviderHistoryBundlePublication,
    RawMarketObservation,
    SnapshotPinCorruptionError,
    SnapshotPurpose,
    TradingStatus,
    TradingStatusObservation,
    payload_mutation_sidecar_path,
)

_PROCESS_TIMEOUT_SECONDS = 20.0
_EXPECTED_PAYLOAD_REFERENCE_TABLES = frozenset(
    {
        "ingestion_runs",
        "raw_market_observation_revisions",
        "trading_status_revisions",
        "adjustment_factor_revisions",
        "provider_frame_revisions",
        "market_session_calendars",
    }
)


@dataclass(frozen=True)
class _CaseEvidence:
    case: str
    database_row_state: str
    durable_reference_state: str
    payload_file_state: str
    publisher_result: str
    gc_result: str
    mutex_result: str
    invariant_result: str
    eventual_cleanup_or_reuse: str


def _config_at(root: Path) -> MarketHistoryConfig:
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _provider_frame_publication(
    payload: bytes,
    *,
    snapshot_suffix: str,
) -> ProviderFramePublication:
    return ProviderFramePublication(
        upstream_service_id="upstream:ticket-06",
        upstream_service_name="Ticket 06 fixture service",
        provider_dataset_id="provider:ticket-06",
        provider_name="fixture",
        dataset_name="mainland-current-adjusted-v1",
        adjustment_methodology="qfq",
        strict_history_qualified=False,
        instrument_id="instrument:ticket-06",
        canonical_symbol="600519.SS",
        reference_market="XSHG",
        instrument_kind="equity",
        currency="CNY",
        identity_revision="mainland-routing-v1",
        snapshot_id=f"snapshot:{snapshot_suffix * 64}",
        requested_start="2026-07-24",
        requested_end="2026-07-24",
        effective_trading_date="2026-07-24",
        adjustment_basis="qfq",
        frame_digest=sha256(payload).hexdigest(),
        history_rows=1,
        retrieved_at="2026-07-24T10:00:00+00:00",
        normalized_frame=payload,
    )


def _history_bundle_publication() -> ProviderHistoryBundlePublication:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    return ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:ticket-06-bundle",
            upstream_service_name="Ticket 06 bundle service",
            provider_dataset_id="provider:ticket-06-bundle",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id="instrument:ticket-06-bundle",
            canonical_symbol="600519.SS",
            reference_market="shanghai",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"baostock","ticket":"06-bundle"}',
        observations=(
            RawMarketObservation(
                session_date=date(2026, 7, 24),
                open=Decimal("10.5"),
                high=Decimal("11.5"),
                low=Decimal("10"),
                close=Decimal("11"),
                volume=Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )


def _calendar_publication(
    bundle: ProviderHistoryBundlePublication,
) -> MarketSessionCalendarPublication:
    return MarketSessionCalendarPublication(
        provider=replace(
            bundle.provider,
            provider_dataset_id="provider:ticket-06-calendar",
            dataset_name="mainland-session-calendar-v1",
            adjustment_methodology="not-applicable",
            strict_history_qualified=False,
        ),
        reference_market=bundle.instrument.reference_market,
        timezone_name="Asia/Shanghai",
        observed_at=bundle.observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"calendar":"ticket-06"}',
        sessions=(
            MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
        ),
    )


def _payload_for_publication_path(publication_path: str) -> bytes:
    if publication_path == "provider_frame":
        return b"Date,Close\n2026-07-24,11\n"
    if publication_path == "provider_history_bundle":
        return _history_bundle_publication().raw_payload
    if publication_path == "calendar":
        return _calendar_publication(_history_bundle_publication()).raw_payload
    raise ValueError(f"unsupported publication path: {publication_path}")


def _publish_payload_path(
    store: MarketHistoryStore,
    publication_path: str,
    payload: bytes,
    snapshot_suffix: str,
) -> tuple[str, object]:
    if publication_path == "provider_frame":
        published = store.publish_provider_frame(
            _provider_frame_publication(
                payload,
                snapshot_suffix=snapshot_suffix,
            )
        )
    elif publication_path == "provider_history_bundle":
        published = store.publish_history_bundle(_history_bundle_publication())
    elif publication_path == "calendar":
        published = store.publish_session_calendar(
            _calendar_publication(_history_bundle_publication())
        )
    else:
        raise ValueError(f"unsupported publication path: {publication_path}")
    return sha256(payload).hexdigest(), published


def _canonical_payload_path(config: MarketHistoryConfig, digest: str) -> Path:
    return config.payload_root / "sha256" / digest[:2] / f"{digest}.bin"


def _database_row_state(config: MarketHistoryConfig, digest: str) -> str:
    connection = sqlite3.connect(config.database_path)
    try:
        row = connection.execute(
            "SELECT 1 FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()
    finally:
        connection.close()
    return "present" if row is not None else "absent"


def _durable_reference_count(config: MarketHistoryConfig, digest: str) -> int:
    connection = sqlite3.connect(config.database_path)
    try:
        return sum(
            int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE payload_digest = ?",
                    (digest,),
                ).fetchone()[0]
            )
            for table in _payload_reference_tables(connection)
        )
    finally:
        connection.close()


def _payload_file_state(config: MarketHistoryConfig, digest: str) -> str:
    path = _canonical_payload_path(config, digest)
    if not path.exists():
        return "absent"
    payload = path.read_bytes()
    return (
        "verified"
        if sha256(payload).hexdigest() == digest
        else f"invalid:{len(payload)}"
    )


def _live_payload_invariant(config: MarketHistoryConfig) -> str:
    connection = sqlite3.connect(config.database_path)
    try:
        rows = tuple(
            connection.execute(
                "SELECT digest, relative_path, byte_length FROM payload_artifacts"
            )
        )
        for table in _payload_reference_tables(connection):
            missing = connection.execute(
                f"SELECT r.payload_digest FROM {table} AS r "
                "LEFT JOIN payload_artifacts AS p ON p.digest = r.payload_digest "
                "WHERE p.digest IS NULL LIMIT 1"
            ).fetchone()
            assert missing is None, f"{table} references missing payload metadata"
    finally:
        connection.close()

    for digest_value, relative_path_value, byte_length_value in rows:
        digest = str(digest_value)
        payload = (config.payload_root / str(relative_path_value)).read_bytes()
        assert len(payload) == int(byte_length_value)
        assert sha256(payload).hexdigest() == digest
    return "verified"


def _payload_reference_tables(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        )
        if not str(row[0]).startswith("sqlite_")
    )
    discovered = frozenset(
        table
        for table in tables
        if any(
            str(foreign_key[2]) == "payload_artifacts"
            and str(foreign_key[3]) == "payload_digest"
            for foreign_key in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            )
        )
    )
    assert discovered == _EXPECTED_PAYLOAD_REFERENCE_TABLES
    return tuple(sorted(discovered))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _database_files_bytes(database_path: Path) -> dict[str, bytes]:
    paths = (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
        database_path.with_name(f"{database_path.name}-journal"),
    )
    return {path.name: path.read_bytes() for path in paths if path.is_file()}


def _table_count(config: MarketHistoryConfig, table: str) -> int:
    connection = sqlite3.connect(config.database_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _probe_sidecar_mutex(config: MarketHistoryConfig) -> str:
    contender = sqlite3.connect(
        payload_mutation_sidecar_path(config.database_path),
        isolation_level=None,
        timeout=0,
    )
    try:
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            return "locked"
        contender.rollback()
        return "available"
    finally:
        contender.close()


def _wait_for(event: Any, label: str) -> None:
    assert event.wait(_PROCESS_TIMEOUT_SECONDS), f"timed out waiting for {label}"


def _finish_process(process: multiprocessing.Process, label: str) -> None:
    process.join(_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_PROCESS_TIMEOUT_SECONDS)
        pytest.fail(f"{label} did not terminate")
    assert process.exitcode == 0, f"{label} exited with {process.exitcode}"


def _queue_result(result_queue: Any, label: str) -> dict[str, object]:
    try:
        result = result_queue.get(timeout=_PROCESS_TIMEOUT_SECONDS)
    except Empty:
        pytest.fail(f"{label} did not report a result")
    assert isinstance(result, dict)
    return result


def _gc_process_worker(
    config: MarketHistoryConfig,
    pause_phase: str | None,
    waiting: Any,
    acquired: Any,
    reached_pause: Any,
    release_pause: Any,
    result_queue: Any,
) -> None:
    def phase_hook(phase: str, _digest: str) -> None:
        if phase == "gc_mutex_waiting":
            waiting.set()
        elif phase == "gc_mutex_acquired":
            acquired.set()
        if phase == pause_phase:
            reached_pause.set()
            if not release_pause.wait(_PROCESS_TIMEOUT_SECONDS):
                raise TimeoutError(f"timed out at {phase}")

    try:
        with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
            result = store.collect_orphan_payloads()
        result_queue.put(
            {
                "status": "ok",
                "outcomes": tuple(
                    (
                        outcome.digest,
                        outcome.status.value,
                        outcome.retryable,
                    )
                    for outcome in result.outcomes
                ),
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "status": "error",
                "type": type(exc).__name__,
                "detail": str(exc),
            }
        )


def _provider_frame_process_worker(
    config: MarketHistoryConfig,
    payload: bytes,
    snapshot_suffix: str,
    waiting: Any,
    acquired: Any,
    result_queue: Any,
    pause_phase: str | None = None,
    reached_pause: Any | None = None,
    release_pause: Any | None = None,
    timeout_seconds: float | None = None,
) -> None:
    _payload_publication_process_worker(
        config,
        "provider_frame",
        payload,
        snapshot_suffix,
        waiting,
        acquired,
        result_queue,
        pause_phase,
        reached_pause,
        release_pause,
        timeout_seconds,
    )


def _payload_publication_process_worker(
    config: MarketHistoryConfig,
    publication_path: str,
    payload: bytes,
    snapshot_suffix: str,
    waiting: Any,
    acquired: Any,
    result_queue: Any,
    pause_phase: str | None = None,
    reached_pause: Any | None = None,
    release_pause: Any | None = None,
    timeout_seconds: float | None = None,
) -> None:
    if timeout_seconds is not None:
        import tradingagents.market_history.store as store_module

        store_module._PAYLOAD_MUTATION_TIMEOUT_SECONDS = timeout_seconds

    def phase_hook(phase: str, _digest: str) -> None:
        if phase == "publisher_mutex_waiting":
            waiting.set()
        elif phase == "publisher_mutex_acquired":
            acquired.set()
        if phase == pause_phase:
            assert reached_pause is not None
            assert release_pause is not None
            reached_pause.set()
            if not release_pause.wait(_PROCESS_TIMEOUT_SECONDS):
                raise TimeoutError(f"timed out at {phase}")

    try:
        with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
            payload_digest, published = _publish_payload_path(
                store,
                publication_path,
                payload,
                snapshot_suffix,
            )
        result_queue.put(
            {
                "status": "published",
                "publication_path": publication_path,
                "payload_digest": payload_digest,
                "snapshot_id": getattr(published, "snapshot_id", None),
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "status": "error",
                "type": type(exc).__name__,
                "detail": str(exc),
            }
        )


def _exact_snapshot_process_worker(
    config: MarketHistoryConfig,
    waiting: Any,
    acquired: Any,
    result_queue: Any,
    timeout_seconds: float | None = None,
) -> None:
    if timeout_seconds is not None:
        import tradingagents.market_history.store as store_module

        store_module._PAYLOAD_MUTATION_TIMEOUT_SECONDS = timeout_seconds

    def phase_hook(phase: str, _subject: str) -> None:
        if phase == "publisher_mutex_waiting":
            waiting.set()
        elif phase == "publisher_mutex_acquired":
            acquired.set()

    try:
        bundle_publication = _history_bundle_publication()
        calendar_publication = _calendar_publication(bundle_publication)
        with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
            calendar = store.publish_session_calendar(calendar_publication)
            bundle = store.publish_history_bundle(bundle_publication)
            snapshot = store.reconstruct_snapshot(
                bundle.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=bundle_publication.retrieval_cutoff,
            )
            pinned = store.read_pinned_snapshot(snapshot.snapshot_id)
            store.read_payload(sha256(bundle_publication.raw_payload).hexdigest())
            store.read_payload(sha256(calendar_publication.raw_payload).hexdigest())
        result_queue.put(
            {
                "status": "published",
                "snapshot_id": pinned.snapshot_id,
                "bundle_revision_id": pinned.bundle_revision_id,
                "observation_revision_ids": pinned.observation_revision_ids,
                "trading_status_revision_ids": pinned.trading_status_revision_ids,
                "factor_revision_ids": pinned.factor_revision_ids,
                "calendar_revision_id": calendar.calendar_revision_id,
                "current_tradeability": pinned.current_tradeability,
                "provenance_class": pinned.provenance_class.value,
                "snapshot_id_version": pinned.snapshot_id_version,
                "bundle_payload_digest": sha256(
                    bundle_publication.raw_payload
                ).hexdigest(),
                "bundle_payload_length": len(bundle_publication.raw_payload),
                "calendar_payload_digest": sha256(
                    calendar_publication.raw_payload
                ).hexdigest(),
                "calendar_payload_length": len(calendar_publication.raw_payload),
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "status": "error",
                "type": type(exc).__name__,
                "detail": str(exc),
            }
        )


def _exact_pin_process_worker(
    config: MarketHistoryConfig,
    bundle_revision_id: str,
    replay_as_of: datetime,
    waiting: Any,
    acquired: Any,
    result_queue: Any,
) -> None:
    def phase_hook(phase: str, _subject: str) -> None:
        if phase == "publisher_mutex_waiting":
            waiting.set()
        elif phase == "publisher_mutex_acquired":
            acquired.set()

    try:
        with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
            snapshot = store.reconstruct_snapshot(
                bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=replay_as_of,
            )
        result_queue.put(
            {
                "status": "published",
                "snapshot_id": snapshot.snapshot_id,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "status": "error",
                "type": type(exc).__name__,
                "detail": str(exc),
            }
        )


@pytest.mark.unit
def test_payload_mutators_expose_a_deterministic_mutex_waiting_phase(
    tmp_path: Path,
) -> None:
    phases: list[str] = []
    config = _config_at(tmp_path / "history")

    with MarketHistoryStore.open(
        config,
        _phase_hook=lambda phase, _digest: phases.append(phase),
    ) as store:
        store.install_payload(b"ticket-06-orphan", media_type="application/json")
        publisher_phases = tuple(phases)
        phases.clear()
        store.collect_orphan_payloads()
        gc_phases = tuple(phases)

    assert publisher_phases == (
        "publisher_mutex_waiting",
        "publisher_mutex_acquired",
        "publisher_file_installed",
        "publisher_payload_row_committed",
    )
    assert gc_phases == (
        "gc_mutex_waiting",
        "gc_mutex_acquired",
        "gc_row_delete_committed",
        "gc_before_unlink",
        "gc_after_unlink_before_mutex_release",
    )


@pytest.mark.unit
def test_ordinary_orphan_collection_reports_only_after_unlink_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    payload = b"ticket-06-ordinary-orphan"
    digest = sha256(payload).hexdigest()
    phases: list[str] = []

    with MarketHistoryStore.open(
        config,
        _phase_hook=lambda phase, _digest: phases.append(phase),
    ) as store:
        store.install_payload(payload, media_type="application/json")
        phases.clear()
        collected = store.collect_orphan_payloads()
        repeated = store.collect_orphan_payloads()

    evidence = _CaseEvidence(
        case="ordinary_orphan_collection",
        database_row_state=_database_row_state(config, digest),
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result="not_run",
        gc_result=collected.outcomes[0].status.value,
        mutex_result="released_after_gc_after_unlink_before_mutex_release",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse=(
            "repeat_idempotent" if repeated.outcomes == () else "repeat_mutated"
        ),
    )

    assert phases == [
        "gc_mutex_waiting",
        "gc_mutex_acquired",
        "gc_row_delete_committed",
        "gc_before_unlink",
        "gc_after_unlink_before_mutex_release",
    ]
    assert evidence == _CaseEvidence(
        case="ordinary_orphan_collection",
        database_row_state="absent",
        durable_reference_state="count=0",
        payload_file_state="absent",
        publisher_result="not_run",
        gc_result=PayloadMaintenanceStatus.COLLECTED.value,
        mutex_result="released_after_gc_after_unlink_before_mutex_release",
        invariant_result="verified",
        eventual_cleanup_or_reuse="repeat_idempotent",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pause_phase", "intermediate_file_state"),
    (
        ("gc_row_delete_committed", "verified"),
        ("gc_before_unlink", "verified"),
        ("gc_after_unlink_before_mutex_release", "absent"),
    ),
)
def test_same_digest_republication_waits_for_gc_and_recovers_safely(
    tmp_path: Path,
    pause_phase: str,
    intermediate_file_state: str,
) -> None:
    payload = b"Date,Close\n2026-07-24,10\n"
    digest = sha256(payload).hexdigest()
    prepublication_config = _config_at(tmp_path / pause_phase / "before")
    with MarketHistoryStore.open(prepublication_config) as store:
        published_before = store.publish_provider_frame(
            _provider_frame_publication(payload, snapshot_suffix="p")
        )
        repeated_before = store.publish_provider_frame(
            _provider_frame_publication(payload, snapshot_suffix="p")
        )
        retained_before_gc = store.collect_orphan_payloads()
    assert published_before == repeated_before
    assert retained_before_gc.collected_digests == ()
    assert _database_row_state(prepublication_config, digest) == "present"
    assert _durable_reference_count(prepublication_config, digest) == 1
    assert _payload_file_state(prepublication_config, digest) == "verified"
    assert _live_payload_invariant(prepublication_config) == "verified"

    config = _config_at(tmp_path / pause_phase / "blocked")
    with MarketHistoryStore.open(config) as store:
        store.install_payload(payload, media_type="text/csv; charset=utf-8")

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            pause_phase,
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, pause_phase)

    assert _database_row_state(config, digest) == "absent"
    assert _durable_reference_count(config, digest) == 0
    assert _payload_file_state(config, digest) == intermediate_file_state

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_provider_frame_process_worker,
        args=(
            config,
            payload,
            "6",
            publisher_waiting,
            publisher_acquired,
            publisher_results,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, "publisher mutex waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert publisher_acquired.is_set() is False
    assert _database_row_state(config, digest) == "absent"
    assert _durable_reference_count(config, digest) == 0
    assert _payload_file_state(config, digest) == intermediate_file_state

    gc_release.set()
    _finish_process(gc_process, "GC process")
    _finish_process(publisher_process, "publisher process")
    gc_result = _queue_result(gc_results, "GC process")
    publisher_result = _queue_result(publisher_results, "publisher process")

    assert gc_result == {
        "status": "ok",
        "outcomes": ((digest, PayloadMaintenanceStatus.COLLECTED.value, False),),
    }
    assert publisher_result["status"] == "published"
    assert publisher_result["payload_digest"] == digest
    assert publisher_acquired.is_set() is True

    with MarketHistoryStore.open(config) as store:
        repeated = store.publish_provider_frame(
            _provider_frame_publication(payload, snapshot_suffix="6")
        )
        assert store.read_payload(digest) == payload

    evidence = _CaseEvidence(
        case=f"same_digest_republication_at_{pause_phase}",
        database_row_state=_database_row_state(config, digest),
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result=(
            "idempotent_before_then_published_and_idempotent_after"
            if repeated.payload_digest == digest
            else "unexpected"
        ),
        gc_result="collected_after_unlink",
        mutex_result="publisher_waited_for_global_mutex",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="verified_republication",
    )

    assert evidence == _CaseEvidence(
        case=f"same_digest_republication_at_{pause_phase}",
        database_row_state="present",
        durable_reference_state="count=1",
        payload_file_state="verified",
        publisher_result="idempotent_before_then_published_and_idempotent_after",
        gc_result="collected_after_unlink",
        mutex_result="publisher_waited_for_global_mutex",
        invariant_result="verified",
        eventual_cleanup_or_reuse="verified_republication",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "publication_path",
    ("provider_frame", "provider_history_bundle", "calendar"),
)
@pytest.mark.parametrize(
    ("pause_phase", "intermediate_row_state"),
    (
        ("publisher_file_installed", "absent"),
        ("publisher_payload_row_committed", "present"),
    ),
)
def test_publisher_termination_before_references_leaves_recoverable_residue(
    tmp_path: Path,
    publication_path: str,
    pause_phase: str,
    intermediate_row_state: str,
) -> None:
    config = _config_at(tmp_path / f"{publication_path}-{pause_phase}")
    payload = _payload_for_publication_path(publication_path)
    digest = sha256(payload).hexdigest()
    with MarketHistoryStore.open(config):
        pass

    context = multiprocessing.get_context("spawn")
    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_reached_pause = context.Event()
    publisher_release = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_payload_publication_process_worker,
        args=(
            config,
            publication_path,
            payload,
            "5",
            publisher_waiting,
            publisher_acquired,
            publisher_results,
            pause_phase,
            publisher_reached_pause,
            publisher_release,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_reached_pause, pause_phase)

    assert publisher_acquired.is_set() is True
    assert _database_row_state(config, digest) == intermediate_row_state
    assert _durable_reference_count(config, digest) == 0
    assert _payload_file_state(config, digest) == "verified"

    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            None,
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_waiting, "GC mutex waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert gc_acquired.is_set() is False
    assert _database_row_state(config, digest) == intermediate_row_state
    assert _durable_reference_count(config, digest) == 0
    assert _payload_file_state(config, digest) == "verified"

    publisher_process.terminate()
    publisher_process.join(_PROCESS_TIMEOUT_SECONDS)
    assert publisher_process.is_alive() is False
    assert publisher_process.exitcode not in {None, 0}
    _finish_process(gc_process, "GC process")
    gc_result = _queue_result(gc_results, "GC process")

    assert gc_result == {
        "status": "ok",
        "outcomes": ((digest, PayloadMaintenanceStatus.COLLECTED.value, False),),
    }
    assert _database_row_state(config, digest) == "absent"
    assert _durable_reference_count(config, digest) == 0
    assert _payload_file_state(config, digest) == "absent"

    with MarketHistoryStore.open(config) as store:
        recovered_digest, _recovered = _publish_payload_path(
            store,
            publication_path,
            payload,
            "5",
        )
        assert store.read_payload(digest) == payload

    expected_reference_count = {
        "provider_frame": 1,
        "provider_history_bundle": 4,
        "calendar": 2,
    }[publication_path]

    evidence = _CaseEvidence(
        case=f"publisher_terminated_{publication_path}_at_{pause_phase}",
        database_row_state=_database_row_state(config, digest),
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result=f"terminated_before_references:{pause_phase}",
        gc_result="collected_residue",
        mutex_result="gc_waited_until_process_termination",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse=(
            "collected_then_republished"
            if recovered_digest == digest
            else "unexpected"
        ),
    )

    assert evidence == _CaseEvidence(
        case=f"publisher_terminated_{publication_path}_at_{pause_phase}",
        database_row_state="present",
        durable_reference_state=f"count={expected_reference_count}",
        payload_file_state="verified",
        publisher_result=f"terminated_before_references:{pause_phase}",
        gc_result="collected_residue",
        mutex_result="gc_waited_until_process_termination",
        invariant_result="verified",
        eventual_cleanup_or_reuse="collected_then_republished",
    )


@pytest.mark.unit
def test_two_gc_workers_serialize_and_only_one_reports_collection(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "two-gc-workers")
    payload = b"ticket-06-two-gc-workers"
    digest = sha256(payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(payload, media_type="application/json")

    context = multiprocessing.get_context("spawn")
    first_waiting = context.Event()
    first_acquired = context.Event()
    first_reached_pause = context.Event()
    first_release = context.Event()
    first_results = context.Queue()
    first = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_mutex_acquired",
            first_waiting,
            first_acquired,
            first_reached_pause,
            first_release,
            first_results,
        ),
    )
    first.start()
    _wait_for(first_reached_pause, "first GC mutex acquired")

    second_waiting = context.Event()
    second_acquired = context.Event()
    second_reached_pause = context.Event()
    second_release = context.Event()
    second_results = context.Queue()
    second = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            None,
            second_waiting,
            second_acquired,
            second_reached_pause,
            second_release,
            second_results,
        ),
    )
    second.start()
    _wait_for(second_waiting, "second GC mutex waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert second_acquired.is_set() is False
    assert _database_row_state(config, digest) == "present"
    assert _payload_file_state(config, digest) == "verified"

    first_release.set()
    _finish_process(first, "first GC process")
    _finish_process(second, "second GC process")
    first_result = _queue_result(first_results, "first GC process")
    second_result = _queue_result(second_results, "second GC process")

    assert first_result == {
        "status": "ok",
        "outcomes": ((digest, PayloadMaintenanceStatus.COLLECTED.value, False),),
    }
    assert second_result == {
        "status": "ok",
        "outcomes": ((digest, PayloadMaintenanceStatus.SKIPPED.value, False),),
    }
    evidence = _CaseEvidence(
        case="two_concurrent_gc_workers",
        database_row_state=_database_row_state(config, digest),
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result="not_run",
        gc_result="one_collected_one_rechecked_and_skipped",
        mutex_result="second_waited_for_first",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="orphan_collected_once",
    )

    assert evidence == _CaseEvidence(
        case="two_concurrent_gc_workers",
        database_row_state="absent",
        durable_reference_state="count=0",
        payload_file_state="absent",
        publisher_result="not_run",
        gc_result="one_collected_one_rechecked_and_skipped",
        mutex_result="second_waited_for_first",
        invariant_result="verified",
        eventual_cleanup_or_reuse="orphan_collected_once",
    )


@pytest.mark.unit
def test_different_digest_publication_is_globally_serialized_or_times_out_typed(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "different-digests")
    gc_payload = b"ticket-06-gc-digest-a"
    publish_payload = b"Date,Close\n2026-07-24,12\n"
    timeout_payload = b"Date,Close\n2026-07-24,13\n"
    gc_digest = sha256(gc_payload).hexdigest()
    publish_digest = sha256(publish_payload).hexdigest()
    timeout_digest = sha256(timeout_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(gc_payload, media_type="application/json")

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_before_unlink",
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, "GC before unlink")

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_provider_frame_process_worker,
        args=(
            config,
            publish_payload,
            "7",
            publisher_waiting,
            publisher_acquired,
            publisher_results,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, "different-digest publisher waiting")

    timeout_waiting = context.Event()
    timeout_acquired = context.Event()
    timeout_results = context.Queue()
    timeout_process = context.Process(
        target=_provider_frame_process_worker,
        args=(
            config,
            timeout_payload,
            "8",
            timeout_waiting,
            timeout_acquired,
            timeout_results,
            None,
            None,
            None,
            0.0,
        ),
    )
    timeout_process.start()
    _wait_for(timeout_waiting, "typed-timeout publisher waiting")
    _finish_process(timeout_process, "typed-timeout publisher")
    timeout_result = _queue_result(timeout_results, "typed-timeout publisher")

    assert timeout_result["status"] == "error"
    assert timeout_result["type"] == "PayloadMutationTimeoutError"
    assert timeout_acquired.is_set() is False
    assert _database_row_state(config, timeout_digest) == "absent"
    assert _durable_reference_count(config, timeout_digest) == 0
    assert _payload_file_state(config, timeout_digest) == "absent"
    assert _probe_sidecar_mutex(config) == "locked"
    assert publisher_acquired.is_set() is False
    assert _database_row_state(config, publish_digest) == "absent"
    assert _payload_file_state(config, publish_digest) == "absent"

    gc_release.set()
    _finish_process(gc_process, "GC process")
    _finish_process(publisher_process, "different-digest publisher")
    gc_result = _queue_result(gc_results, "GC process")
    publisher_result = _queue_result(publisher_results, "different-digest publisher")

    assert gc_result["status"] == "ok"
    assert publisher_result["status"] == "published"
    evidence = _CaseEvidence(
        case="different_digest_publication_during_gc",
        database_row_state=(
            f"gc={_database_row_state(config, gc_digest)};"
            f"published={_database_row_state(config, publish_digest)};"
            f"timed_out={_database_row_state(config, timeout_digest)}"
        ),
        durable_reference_state=(
            f"published_count={_durable_reference_count(config, publish_digest)};"
            f"timed_out_count={_durable_reference_count(config, timeout_digest)}"
        ),
        payload_file_state=(
            f"gc={_payload_file_state(config, gc_digest)};"
            f"published={_payload_file_state(config, publish_digest)};"
            f"timed_out={_payload_file_state(config, timeout_digest)}"
        ),
        publisher_result="published_after_release;second_typed_timeout",
        gc_result="collected_digest_a",
        mutex_result="intentional_global_serialization",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="a_collected_b_published_timeout_untouched",
    )

    assert evidence == _CaseEvidence(
        case="different_digest_publication_during_gc",
        database_row_state="gc=absent;published=present;timed_out=absent",
        durable_reference_state="published_count=1;timed_out_count=0",
        payload_file_state="gc=absent;published=verified;timed_out=absent",
        publisher_result="published_after_release;second_typed_timeout",
        gc_result="collected_digest_a",
        mutex_result="intentional_global_serialization",
        invariant_result="verified",
        eventual_cleanup_or_reuse="a_collected_b_published_timeout_untouched",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pause_phase", "intermediate_file_state"),
    (
        ("gc_row_delete_committed", "verified"),
        ("gc_after_unlink_before_mutex_release", "absent"),
    ),
)
def test_gc_process_termination_releases_mutex_and_residue_is_reusable(
    tmp_path: Path,
    pause_phase: str,
    intermediate_file_state: str,
) -> None:
    config = _config_at(tmp_path / f"terminated-{pause_phase}")
    payload = b"Date,Close\n2026-07-24,14\n"
    digest = sha256(payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(payload, media_type="text/csv; charset=utf-8")

    context = multiprocessing.get_context("spawn")
    waiting = context.Event()
    acquired = context.Event()
    reached_pause = context.Event()
    release_pause = context.Event()
    results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            pause_phase,
            waiting,
            acquired,
            reached_pause,
            release_pause,
            results,
        ),
    )
    gc_process.start()
    _wait_for(reached_pause, pause_phase)

    assert _database_row_state(config, digest) == "absent"
    assert _payload_file_state(config, digest) == intermediate_file_state
    assert _probe_sidecar_mutex(config) == "locked"

    gc_process.terminate()
    gc_process.join(_PROCESS_TIMEOUT_SECONDS)
    assert gc_process.is_alive() is False
    assert gc_process.exitcode not in {None, 0}
    assert _probe_sidecar_mutex(config) == "available"

    with MarketHistoryStore.open(config) as store:
        recovered = store.publish_provider_frame(
            _provider_frame_publication(payload, snapshot_suffix="9")
        )
        assert store.read_payload(digest) == payload

    evidence = _CaseEvidence(
        case=f"gc_terminated_at_{pause_phase}",
        database_row_state=_database_row_state(config, digest),
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result="recovered_verified_publication",
        gc_result=f"terminated:{pause_phase}",
        mutex_result="sqlite_automatic_release_no_stale_owner",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse=(
            "residue_reused_or_reinstalled"
            if recovered.payload_digest == digest
            else "unexpected"
        ),
    )

    assert evidence == _CaseEvidence(
        case=f"gc_terminated_at_{pause_phase}",
        database_row_state="present",
        durable_reference_state="count=1",
        payload_file_state="verified",
        publisher_result="recovered_verified_publication",
        gc_result=f"terminated:{pause_phase}",
        mutex_result="sqlite_automatic_release_no_stale_owner",
        invariant_result="verified",
        eventual_cleanup_or_reuse="residue_reused_or_reinstalled",
    )


@pytest.mark.unit
def test_concurrent_same_digest_publishers_are_idempotent_for_separate_references(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "concurrent-publishers")
    payload = b"Date,Close\n2026-07-24,15\n"
    digest = sha256(payload).hexdigest()
    first_publication = _provider_frame_publication(payload, snapshot_suffix="a")
    second_publication = _provider_frame_publication(payload, snapshot_suffix="b")
    with MarketHistoryStore.open(config) as store:
        first_before = store.publish_provider_frame(first_publication)

    blocker = sqlite3.connect(
        payload_mutation_sidecar_path(config.database_path),
        isolation_level=None,
    )
    blocker.execute("BEGIN IMMEDIATE")
    context = multiprocessing.get_context("spawn")
    processes: list[multiprocessing.Process] = []
    results: list[Any] = []
    acquired_events: list[Any] = []
    try:
        for suffix in ("a", "b"):
            waiting = context.Event()
            acquired = context.Event()
            result_queue = context.Queue()
            process = context.Process(
                target=_provider_frame_process_worker,
                args=(
                    config,
                    payload,
                    suffix,
                    waiting,
                    acquired,
                    result_queue,
                ),
            )
            process.start()
            _wait_for(waiting, f"publisher {suffix} mutex waiting")
            processes.append(process)
            results.append(result_queue)
            acquired_events.append(acquired)

        assert all(event.is_set() is False for event in acquired_events)
        assert _probe_sidecar_mutex(config) == "locked"
    finally:
        blocker.rollback()
        blocker.close()

    for index, process in enumerate(processes):
        _finish_process(process, f"publisher {index}")
    process_results = tuple(
        _queue_result(result_queue, f"publisher {index}")
        for index, result_queue in enumerate(results)
    )
    assert all(result["status"] == "published" for result in process_results)
    assert all(event.is_set() is True for event in acquired_events)

    with MarketHistoryStore.open(config) as store:
        first_after = store.publish_provider_frame(first_publication)
        second_after = store.publish_provider_frame(second_publication)
        assert store.read_payload(digest) == payload
        payload_rows = store._connection.execute(
            "SELECT COUNT(*) FROM payload_artifacts WHERE digest = ?",
            (digest,),
        ).fetchone()[0]

    evidence = _CaseEvidence(
        case="idempotent_and_concurrent_publication",
        database_row_state=f"payload_rows={payload_rows}",
        durable_reference_state=f"count={_durable_reference_count(config, digest)}",
        payload_file_state=_payload_file_state(config, digest),
        publisher_result=(
            "prepublished_then_two_concurrent_then_repeated"
            if first_before == first_after
            and first_after.payload_digest == second_after.payload_digest
            else "unexpected"
        ),
        gc_result="not_run",
        mutex_result="both_waited_then_serialized",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="one_payload_two_valid_references",
    )

    assert evidence == _CaseEvidence(
        case="idempotent_and_concurrent_publication",
        database_row_state="payload_rows=1",
        durable_reference_state="count=2",
        payload_file_state="verified",
        publisher_result="prepublished_then_two_concurrent_then_repeated",
        gc_result="not_run",
        mutex_result="both_waited_then_serialized",
        invariant_result="verified",
        eventual_cleanup_or_reuse="one_payload_two_valid_references",
    )


@pytest.mark.unit
def test_unlink_failure_is_retryable_and_later_locked_sweep_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_at(tmp_path / "unlink-failure")
    payload = b"ticket-06-unlink-failure"
    digest = sha256(payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        artifact = store.install_payload(payload, media_type="application/json")
        payload_path = config.payload_root / artifact.relative_path
        original_unlink = Path.unlink
        failed_once = False

        def fail_first_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            nonlocal failed_once
            if path == payload_path and not failed_once:
                failed_once = True
                raise PermissionError("ticket-06 injected unlink failure")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_first_unlink)
        failed = store.collect_orphan_payloads()

        assert failed.collected_digests == ()
        assert len(failed.failures) == 1
        assert failed.failures[0].status is PayloadMaintenanceStatus.UNLINK_FAILED
        assert failed.failures[0].retryable is True
        assert _database_row_state(config, digest) == "absent"
        assert _durable_reference_count(config, digest) == 0
        assert _payload_file_state(config, digest) == "verified"
        evidence = _CaseEvidence(
            case="unlink_failure",
            database_row_state="absent",
            durable_reference_state="count=0",
            payload_file_state="verified",
            publisher_result="not_run",
            gc_result="unlink_failed_retryable_not_collected",
            mutex_result="failure_returned_before_mutex_release",
            invariant_result=_live_payload_invariant(config),
            eventual_cleanup_or_reuse="later_locked_sweep_collected",
        )

        recovered = store.collect_orphan_payloads()

    assert recovered.collected_digests == (digest,)
    assert _database_row_state(config, digest) == "absent"
    assert _payload_file_state(config, digest) == "absent"
    assert evidence == _CaseEvidence(
        case="unlink_failure",
        database_row_state="absent",
        durable_reference_state="count=0",
        payload_file_state="verified",
        publisher_result="not_run",
        gc_result="unlink_failed_retryable_not_collected",
        mutex_result="failure_returned_before_mutex_release",
        invariant_result="verified",
        eventual_cleanup_or_reuse="later_locked_sweep_collected",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "publication_path",
    ("provider_frame", "provider_history_bundle", "calendar"),
)
def test_payload_publication_paths_wait_for_independent_gc_boundary(
    tmp_path: Path,
    publication_path: str,
) -> None:
    config = _config_at(tmp_path / f"gc-boundary-{publication_path}")
    gc_payload = f"ticket-06-gc-boundary-{publication_path}".encode()
    gc_digest = sha256(gc_payload).hexdigest()
    publication_payload = _payload_for_publication_path(publication_path)
    publication_digest = sha256(publication_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(gc_payload, media_type="application/json")

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_before_unlink",
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, f"{publication_path} GC before unlink")

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_payload_publication_process_worker,
        args=(
            config,
            publication_path,
            publication_payload,
            "f",
            publisher_waiting,
            publisher_acquired,
            publisher_results,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, f"{publication_path} publisher waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert publisher_acquired.is_set() is False
    assert _database_row_state(config, publication_digest) == "absent"
    assert _payload_file_state(config, publication_digest) == "absent"

    gc_release.set()
    _finish_process(gc_process, f"{publication_path} GC")
    _finish_process(publisher_process, f"{publication_path} publisher")
    gc_result = _queue_result(gc_results, f"{publication_path} GC")
    publisher_result = _queue_result(
        publisher_results,
        f"{publication_path} publisher",
    )
    expected_reference_count = {
        "provider_frame": 1,
        "provider_history_bundle": 4,
        "calendar": 2,
    }[publication_path]

    assert gc_result["status"] == "ok"
    assert publisher_result["status"] == "published"
    assert publisher_result["publication_path"] == publication_path
    evidence = _CaseEvidence(
        case=f"{publication_path}_during_independent_gc",
        database_row_state=(
            f"gc={_database_row_state(config, gc_digest)};"
            f"publisher={_database_row_state(config, publication_digest)}"
        ),
        durable_reference_state=(
            f"count={_durable_reference_count(config, publication_digest)}"
        ),
        payload_file_state=(
            f"gc={_payload_file_state(config, gc_digest)};"
            f"publisher={_payload_file_state(config, publication_digest)}"
        ),
        publisher_result="published_after_gc_release",
        gc_result="collected_independent_orphan",
        mutex_result="publisher_waited_at_public_boundary",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="gc_clean_publisher_verified",
    )
    assert evidence == _CaseEvidence(
        case=f"{publication_path}_during_independent_gc",
        database_row_state="gc=absent;publisher=present",
        durable_reference_state=f"count={expected_reference_count}",
        payload_file_state="gc=absent;publisher=verified",
        publisher_result="published_after_gc_release",
        gc_result="collected_independent_orphan",
        mutex_result="publisher_waited_at_public_boundary",
        invariant_result="verified",
        eventual_cleanup_or_reuse="gc_clean_publisher_verified",
    )


@pytest.mark.unit
def test_every_high_level_publication_path_holds_mutex_through_references(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "high-level-paths")
    bundle_publication = _history_bundle_publication()
    calendar_publication = _calendar_publication(bundle_publication)
    provider_payload = b"Date,Close\n2026-07-24,16\n"
    phases: list[str] = []
    store: MarketHistoryStore

    def phase_hook(phase: str, _subject: str) -> None:
        phases.append(phase)
        if phase == "publisher_references_committed":
            assert store._connection.in_transaction is False
            assert _probe_sidecar_mutex(config) == "locked"

    observed: dict[str, tuple[str, ...]] = {}
    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        store.publish_provider_frame(
            _provider_frame_publication(provider_payload, snapshot_suffix="c")
        )
        observed["provider_frame"] = tuple(phases)
        phases.clear()

        calendar = store.publish_session_calendar(calendar_publication)
        observed["calendar"] = tuple(phases)
        phases.clear()

        bundle = store.publish_history_bundle(bundle_publication)
        observed["provider_history_bundle"] = tuple(phases)
        phases.clear()

        snapshot = store.reconstruct_snapshot(
            bundle.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=bundle_publication.retrieval_cutoff,
        )
        observed["exact_snapshot_pin"] = tuple(phases)
        pinned = store.read_pinned_snapshot(snapshot.snapshot_id)

    payload_path_phases = (
        "publisher_mutex_waiting",
        "publisher_mutex_acquired",
        "publisher_file_installed",
        "publisher_payload_row_committed",
        "publisher_references_committed",
    )
    assert observed == {
        "provider_frame": payload_path_phases,
        "calendar": payload_path_phases,
        "provider_history_bundle": payload_path_phases,
        "exact_snapshot_pin": (
            "publisher_mutex_waiting",
            "publisher_mutex_acquired",
            "publisher_references_committed",
        ),
    }
    assert pinned.calendar_revision_id == calendar.calendar_revision_id
    assert len(pinned.observation_revision_ids) == 1
    assert len(pinned.trading_status_revision_ids) == 1
    assert len(pinned.factor_revision_ids) == 1
    assert _table_count(config, "snapshot_pins") == 1
    provider_digest = sha256(provider_payload).hexdigest()
    calendar_digest = sha256(calendar_publication.raw_payload).hexdigest()
    bundle_digest = sha256(bundle_publication.raw_payload).hexdigest()
    evidence = _CaseEvidence(
        case="all_high_level_publication_paths",
        database_row_state=(
            f"payload_rows={_table_count(config, 'payload_artifacts')};pin_count=1"
        ),
        durable_reference_state=(
            f"provider_frame={_durable_reference_count(config, provider_digest)};"
            f"calendar={_durable_reference_count(config, calendar_digest)};"
            f"bundle={_durable_reference_count(config, bundle_digest)}"
        ),
        payload_file_state=(
            f"provider_frame={_payload_file_state(config, provider_digest)};"
            f"calendar={_payload_file_state(config, calendar_digest)};"
            f"bundle={_payload_file_state(config, bundle_digest)}"
        ),
        publisher_result="four_paths_committed_and_exact_pin_readable",
        gc_result="not_run",
        mutex_result="sidecar_locked_through_every_final_reference_commit",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="exact_membership_retained",
    )
    assert evidence == _CaseEvidence(
        case="all_high_level_publication_paths",
        database_row_state="payload_rows=3;pin_count=1",
        durable_reference_state="provider_frame=1;calendar=2;bundle=4",
        payload_file_state=(
            "provider_frame=verified;calendar=verified;bundle=verified"
        ),
        publisher_result="four_paths_committed_and_exact_pin_readable",
        gc_result="not_run",
        mutex_result="sidecar_locked_through_every_final_reference_commit",
        invariant_result="verified",
        eventual_cleanup_or_reuse="exact_membership_retained",
    )


@pytest.mark.unit
def test_exact_snapshot_publication_waits_for_independent_gc_then_reconstructs(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "exact-snapshot-success")
    gc_payload = b"ticket-06-exact-snapshot-gc"
    gc_digest = sha256(gc_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(gc_payload, media_type="application/json")

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_before_unlink",
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, "exact-snapshot GC before unlink")

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_exact_snapshot_process_worker,
        args=(config, publisher_waiting, publisher_acquired, publisher_results),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, "exact-snapshot publisher waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert publisher_acquired.is_set() is False
    assert _table_count(config, "snapshot_pins") == 0
    assert _table_count(config, "history_bundle_revisions") == 0
    assert _table_count(config, "market_session_calendars") == 0

    gc_release.set()
    _finish_process(gc_process, "exact-snapshot GC")
    _finish_process(publisher_process, "exact-snapshot publisher")
    gc_result = _queue_result(gc_results, "exact-snapshot GC")
    publisher_result = _queue_result(
        publisher_results,
        "exact-snapshot publisher",
    )

    assert gc_result["status"] == "ok"
    assert publisher_result["status"] == "published"
    assert publisher_acquired.is_set() is True
    assert publisher_result["current_tradeability"] == "tradeable"
    assert publisher_result["provenance_class"] == "observed_point_in_time"
    assert publisher_result["snapshot_id_version"] == "v2"
    assert publisher_result["bundle_payload_length"] == len(
        _history_bundle_publication().raw_payload
    )
    assert publisher_result["calendar_payload_length"] == len(
        _calendar_publication(_history_bundle_publication()).raw_payload
    )
    snapshot_id = str(publisher_result["snapshot_id"])
    with MarketHistoryStore.open(config) as store:
        pinned = store.read_pinned_snapshot(snapshot_id)
        assert pinned.bundle_revision_id == publisher_result["bundle_revision_id"]
        assert pinned.observation_revision_ids == publisher_result[
            "observation_revision_ids"
        ]
        assert pinned.trading_status_revision_ids == publisher_result[
            "trading_status_revision_ids"
        ]
        assert pinned.factor_revision_ids == publisher_result["factor_revision_ids"]
        assert pinned.calendar_revision_id == publisher_result["calendar_revision_id"]

    bundle_digest = str(publisher_result["bundle_payload_digest"])
    calendar_digest = str(publisher_result["calendar_payload_digest"])
    evidence = _CaseEvidence(
        case="exact_snapshot_publication_during_collection",
        database_row_state=(
            f"gc={_database_row_state(config, gc_digest)};"
            f"bundle={_database_row_state(config, bundle_digest)};"
            f"calendar={_database_row_state(config, calendar_digest)};"
            f"pin_count={_table_count(config, 'snapshot_pins')}"
        ),
        durable_reference_state=(
            f"bundle_count={_durable_reference_count(config, bundle_digest)};"
            f"calendar_count={_durable_reference_count(config, calendar_digest)}"
        ),
        payload_file_state=(
            f"gc={_payload_file_state(config, gc_digest)};"
            f"bundle={_payload_file_state(config, bundle_digest)};"
            f"calendar={_payload_file_state(config, calendar_digest)}"
        ),
        publisher_result="exact_membership_reconstructed",
        gc_result="collected_independent_orphan",
        mutex_result="snapshot_workflow_waited_for_gc",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="gc_clean_snapshot_fully_verified",
    )

    assert evidence == _CaseEvidence(
        case="exact_snapshot_publication_during_collection",
        database_row_state="gc=absent;bundle=present;calendar=present;pin_count=1",
        durable_reference_state="bundle_count=4;calendar_count=2",
        payload_file_state="gc=absent;bundle=verified;calendar=verified",
        publisher_result="exact_membership_reconstructed",
        gc_result="collected_independent_orphan",
        mutex_result="snapshot_workflow_waited_for_gc",
        invariant_result="verified",
        eventual_cleanup_or_reuse="gc_clean_snapshot_fully_verified",
    )


@pytest.mark.unit
def test_exact_pin_failure_after_gc_release_leaves_no_partial_membership(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "exact-pin-failure")
    bundle_publication = _history_bundle_publication()
    calendar_publication = _calendar_publication(bundle_publication)
    gc_payload = b"ticket-06-exact-pin-failure-gc"
    gc_digest = sha256(gc_payload).hexdigest()
    bundle_digest = sha256(bundle_publication.raw_payload).hexdigest()
    calendar_digest = sha256(calendar_publication.raw_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        calendar = store.publish_session_calendar(calendar_publication)
        bundle = store.publish_history_bundle(bundle_publication)
        store.install_payload(gc_payload, media_type="application/json")
        store._connection.execute(
            "DELETE FROM market_sessions WHERE calendar_revision_id = ?",
            (calendar.calendar_revision_id,),
        )

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_before_unlink",
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, "exact-pin failure GC before unlink")

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_exact_pin_process_worker,
        args=(
            config,
            bundle.bundle_revision_id,
            bundle_publication.retrieval_cutoff,
            publisher_waiting,
            publisher_acquired,
            publisher_results,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, "exact-pin failure publisher waiting")

    assert _probe_sidecar_mutex(config) == "locked"
    assert publisher_acquired.is_set() is False
    assert _table_count(config, "snapshot_pins") == 0

    gc_release.set()
    _finish_process(gc_process, "exact-pin failure GC")
    _finish_process(publisher_process, "exact-pin failure publisher")
    gc_result = _queue_result(gc_results, "exact-pin failure GC")
    publisher_result = _queue_result(
        publisher_results,
        "exact-pin failure publisher",
    )

    assert gc_result["status"] == "ok"
    assert publisher_result["status"] == "error"
    assert publisher_result["type"] == "SnapshotPinCorruptionError"
    assert "calendar" in str(publisher_result["detail"])
    assert publisher_acquired.is_set() is True
    assert _table_count(config, "snapshot_pins") == 0
    assert _table_count(config, "snapshot_observation_pins") == 0
    assert _table_count(config, "snapshot_factor_pins") == 0
    evidence = _CaseEvidence(
        case="exact_pin_failure_during_collection",
        database_row_state=(
            f"gc={_database_row_state(config, gc_digest)};"
            f"bundle={_database_row_state(config, bundle_digest)};"
            f"calendar={_database_row_state(config, calendar_digest)};pin_count=0"
        ),
        durable_reference_state=(
            f"bundle_count={_durable_reference_count(config, bundle_digest)};"
            f"calendar_count={_durable_reference_count(config, calendar_digest)}"
        ),
        payload_file_state=(
            f"gc={_payload_file_state(config, gc_digest)};"
            f"bundle={_payload_file_state(config, bundle_digest)};"
            f"calendar={_payload_file_state(config, calendar_digest)}"
        ),
        publisher_result="typed_failure_no_partial_pin",
        gc_result="collected_independent_orphan",
        mutex_result="pin_attempt_waited_then_failed_under_own_mutex",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="prior_payload_references_remain_verified",
    )
    assert evidence == _CaseEvidence(
        case="exact_pin_failure_during_collection",
        database_row_state="gc=absent;bundle=present;calendar=present;pin_count=0",
        durable_reference_state="bundle_count=4;calendar_count=2",
        payload_file_state="gc=absent;bundle=verified;calendar=verified",
        publisher_result="typed_failure_no_partial_pin",
        gc_result="collected_independent_orphan",
        mutex_result="pin_attempt_waited_then_failed_under_own_mutex",
        invariant_result="verified",
        eventual_cleanup_or_reuse="prior_payload_references_remain_verified",
    )


@pytest.mark.unit
def test_exact_snapshot_timeout_during_gc_leaves_no_partial_pin_or_reference(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "exact-snapshot-timeout")
    gc_payload = b"ticket-06-exact-snapshot-timeout-gc"
    gc_digest = sha256(gc_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(gc_payload, media_type="application/json")

    context = multiprocessing.get_context("spawn")
    gc_waiting = context.Event()
    gc_acquired = context.Event()
    gc_reached_pause = context.Event()
    gc_release = context.Event()
    gc_results = context.Queue()
    gc_process = context.Process(
        target=_gc_process_worker,
        args=(
            config,
            "gc_mutex_acquired",
            gc_waiting,
            gc_acquired,
            gc_reached_pause,
            gc_release,
            gc_results,
        ),
    )
    gc_process.start()
    _wait_for(gc_reached_pause, "timeout GC mutex acquired")

    publisher_waiting = context.Event()
    publisher_acquired = context.Event()
    publisher_results = context.Queue()
    publisher_process = context.Process(
        target=_exact_snapshot_process_worker,
        args=(
            config,
            publisher_waiting,
            publisher_acquired,
            publisher_results,
            0.0,
        ),
    )
    publisher_process.start()
    _wait_for(publisher_waiting, "exact-snapshot timeout waiting")
    _finish_process(publisher_process, "exact-snapshot timeout publisher")
    publisher_result = _queue_result(
        publisher_results,
        "exact-snapshot timeout publisher",
    )

    assert publisher_result["status"] == "error"
    assert publisher_result["type"] == "PayloadMutationTimeoutError"
    assert publisher_acquired.is_set() is False
    assert _table_count(config, "snapshot_pins") == 0
    assert _table_count(config, "history_bundle_revisions") == 0
    assert _table_count(config, "market_session_calendars") == 0
    assert _durable_reference_count(config, gc_digest) == 0
    assert _database_row_state(config, gc_digest) == "present"
    assert _payload_file_state(config, gc_digest) == "verified"

    gc_release.set()
    _finish_process(gc_process, "timeout GC")
    gc_result = _queue_result(gc_results, "timeout GC")
    assert gc_result["status"] == "ok"

    evidence = _CaseEvidence(
        case="exact_snapshot_timeout_during_collection",
        database_row_state=(
            f"payload_rows={_table_count(config, 'payload_artifacts')};"
            f"pin_count={_table_count(config, 'snapshot_pins')}"
        ),
        durable_reference_state="count=0",
        payload_file_state=_payload_file_state(config, gc_digest),
        publisher_result="typed_timeout_no_partial_pin",
        gc_result="orphan_collected_after_release",
        mutex_result="publisher_never_acquired",
        invariant_result=_live_payload_invariant(config),
        eventual_cleanup_or_reuse="clean_empty_store",
    )

    assert evidence == _CaseEvidence(
        case="exact_snapshot_timeout_during_collection",
        database_row_state="payload_rows=0;pin_count=0",
        durable_reference_state="count=0",
        payload_file_state="absent",
        publisher_result="typed_timeout_no_partial_pin",
        gc_result="orphan_collected_after_release",
        mutex_result="publisher_never_acquired",
        invariant_result="verified",
        eventual_cleanup_or_reuse="clean_empty_store",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "corruption_mode",
    ("missing", "wrong_digest", "wrong_length"),
)
def test_committed_reference_corruption_fails_closed_without_repair(
    tmp_path: Path,
    corruption_mode: str,
) -> None:
    config = _config_at(tmp_path / f"corruption-{corruption_mode}")
    payload = b"Date,Close\n2026-07-24,17\n"
    digest = sha256(payload).hexdigest()
    publication = _provider_frame_publication(payload, snapshot_suffix="d")
    with MarketHistoryStore.open(config) as store:
        published = store.publish_provider_frame(publication)
        payload_path = _canonical_payload_path(config, digest)
        if corruption_mode == "missing":
            payload_path.unlink()
        elif corruption_mode == "wrong_digest":
            payload_path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
        else:
            payload_path.write_bytes(payload + b"x")
        corrupted_state = _payload_file_state(config, digest)

        with pytest.raises(MarketHistoryCorruptionError):
            store.read_payload(digest)
        with pytest.raises(MarketHistoryCorruptionError):
            store.publish_provider_frame(publication)
        with pytest.raises(MarketHistoryCorruptionError):
            store.create_backup()
        gc_result = store.collect_orphan_payloads()

    assert published.payload_digest == digest
    assert gc_result.collected_digests == ()
    assert all(
        outcome.status is PayloadMaintenanceStatus.SKIPPED
        for outcome in gc_result.outcomes
    )
    assert _database_row_state(config, digest) == "present"
    assert _durable_reference_count(config, digest) == 1
    assert _payload_file_state(config, digest) == corrupted_state
    evidence = _CaseEvidence(
        case=f"committed_reference_corruption_{corruption_mode}",
        database_row_state="present",
        durable_reference_state="count=1",
        payload_file_state=corrupted_state,
        publisher_result="failed_closed_without_repair",
        gc_result="referenced_not_collected",
        mutex_result="released_after_typed_corruption",
        invariant_result="corruption_detected_fail_closed",
        eventual_cleanup_or_reuse="manual_evidence_recovery_required",
    )
    assert evidence == _CaseEvidence(
        case=f"committed_reference_corruption_{corruption_mode}",
        database_row_state="present",
        durable_reference_state="count=1",
        payload_file_state=corrupted_state,
        publisher_result="failed_closed_without_repair",
        gc_result="referenced_not_collected",
        mutex_result="released_after_typed_corruption",
        invariant_result="corruption_detected_fail_closed",
        eventual_cleanup_or_reuse="manual_evidence_recovery_required",
    )


@pytest.mark.unit
def test_sidecar_timeout_leaves_database_and_payload_tree_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tradingagents.market_history.store as store_module

    config = _config_at(tmp_path / "byte-identical-timeout")
    orphan_payload = b"ticket-06-timeout-orphan"
    orphan_digest = sha256(orphan_payload).hexdigest()
    blocked_payload = b"Date,Close\n2026-07-24,18\n"
    blocked_digest = sha256(blocked_payload).hexdigest()
    with MarketHistoryStore.open(config) as store:
        store.install_payload(orphan_payload, media_type="application/json")
        sidecar_path = payload_mutation_sidecar_path(config.database_path)
        blocker = sqlite3.connect(
            sidecar_path,
            isolation_level=None,
        )
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(store_module, "_PAYLOAD_MUTATION_TIMEOUT_SECONDS", 0)
        database_before = _database_files_bytes(config.database_path)
        sidecar_before = _database_files_bytes(sidecar_path)
        payloads_before = _tree_bytes(config.payload_root)
        backups_before = _tree_bytes(config.backup_root)
        try:
            with pytest.raises(PayloadMutationTimeoutError):
                store.publish_provider_frame(
                    _provider_frame_publication(blocked_payload, snapshot_suffix="e")
                )
            gc_timeout = store.collect_orphan_payloads()
            database_after = _database_files_bytes(config.database_path)
            sidecar_after = _database_files_bytes(sidecar_path)
            payloads_after = _tree_bytes(config.payload_root)
            backups_after = _tree_bytes(config.backup_root)
        finally:
            blocker.rollback()
            blocker.close()

        assert database_after == database_before
        assert sidecar_after == sidecar_before
        assert payloads_after == payloads_before
        assert backups_after == backups_before
        assert gc_timeout.collected_digests == ()
        assert len(gc_timeout.failures) == 1
        assert gc_timeout.failures[0].status is PayloadMaintenanceStatus.MUTEX_TIMEOUT
        assert gc_timeout.failures[0].retryable is True
        assert _database_row_state(config, blocked_digest) == "absent"
        assert _payload_file_state(config, blocked_digest) == "absent"
        evidence = _CaseEvidence(
            case="sidecar_timeout_byte_identical",
            database_row_state=_database_row_state(config, orphan_digest),
            durable_reference_state=f"count={_durable_reference_count(config, orphan_digest)}",
            payload_file_state=_payload_file_state(config, orphan_digest),
            publisher_result="typed_timeout_no_mutation",
            gc_result="typed_retryable_timeout_no_mutation",
            mutex_result="independent_sidecar_holder",
            invariant_result=_live_payload_invariant(config),
            eventual_cleanup_or_reuse="later_locked_sweep_collected",
        )
        recovered = store.collect_orphan_payloads()

    assert recovered.collected_digests == (orphan_digest,)
    assert evidence == _CaseEvidence(
        case="sidecar_timeout_byte_identical",
        database_row_state="present",
        durable_reference_state="count=0",
        payload_file_state="verified",
        publisher_result="typed_timeout_no_mutation",
        gc_result="typed_retryable_timeout_no_mutation",
        mutex_result="independent_sidecar_holder",
        invariant_result="verified",
        eventual_cleanup_or_reuse="later_locked_sweep_collected",
    )


@pytest.mark.unit
def test_backup_restore_verifies_exact_pin_and_sidecar_cannot_mask_missing_evidence(
    tmp_path: Path,
) -> None:
    source_config = _config_at(tmp_path / "backup-source")
    bundle_publication = _history_bundle_publication()
    calendar_publication = _calendar_publication(bundle_publication)
    with MarketHistoryStore.open(source_config) as store:
        store.publish_session_calendar(calendar_publication)
        bundle = store.publish_history_bundle(bundle_publication)
        snapshot = store.reconstruct_snapshot(
            bundle.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=bundle_publication.retrieval_cutoff,
        )
        backup = store.create_backup()

    restored_config = _config_at(tmp_path / "backup-restored")
    MarketHistoryStore.restore_backup(backup.path, restored_config)
    with MarketHistoryStore.open(restored_config) as restored:
        pinned = restored.read_pinned_snapshot(snapshot.snapshot_id)
        payload_rows = tuple(
            restored._connection.execute(
                "SELECT digest, relative_path FROM payload_artifacts ORDER BY digest"
            )
        )
        for digest, _relative_path in payload_rows:
            restored.read_payload(str(digest))
        invariant_before_fault = _live_payload_invariant(restored_config)

        bundle_payload = next(
            (str(digest), Path(str(relative_path)))
            for digest, relative_path in payload_rows
            if str(digest) == sha256(bundle_publication.raw_payload).hexdigest()
        )
        (restored_config.payload_root / bundle_payload[1]).unlink()
        with pytest.raises(SnapshotPinCorruptionError, match="payload"):
            restored.read_pinned_snapshot(snapshot.snapshot_id)

    manifest_text = backup.manifest_path.read_text(encoding="utf-8")
    evidence = _CaseEvidence(
        case="backup_restore_and_missing_evidence_control",
        database_row_state=f"payload_rows={len(payload_rows)};pin_count=1",
        durable_reference_state=(
            f"bundle_count={_durable_reference_count(restored_config, bundle_payload[0])}"
        ),
        payload_file_state="all_verified_before_injected_missing_file",
        publisher_result=(
            "exact_pin_restored"
            if pinned.snapshot_id == snapshot.snapshot_id
            else "unexpected"
        ),
        gc_result="not_run",
        mutex_result="sidecar_excluded_from_backup_evidence",
        invariant_result=f"{invariant_before_fault}_then_missing_detected",
        eventual_cleanup_or_reuse="strict_read_failed_closed_after_fault",
    )

    assert "payload-mutation" not in manifest_text
    assert payload_mutation_sidecar_path(restored_config.database_path).exists()
    assert evidence == _CaseEvidence(
        case="backup_restore_and_missing_evidence_control",
        database_row_state="payload_rows=2;pin_count=1",
        durable_reference_state="bundle_count=4",
        payload_file_state="all_verified_before_injected_missing_file",
        publisher_result="exact_pin_restored",
        gc_result="not_run",
        mutex_result="sidecar_excluded_from_backup_evidence",
        invariant_result="verified_then_missing_detected",
        eventual_cleanup_or_reuse="strict_read_failed_closed_after_fault",
    )
