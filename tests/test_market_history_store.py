from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

import tradingagents.market_history.store as store_module
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.market_history import (
    DataUsageMode,
    MainlandEquivalenceScenario,
    MarketHistoryConfig,
    MarketHistoryCorruptionError,
    MarketHistoryMode,
    MarketHistoryStore,
    PayloadCollectionResult,
    PayloadLockOrderError,
    PayloadMaintenanceStatus,
    PayloadMutationTimeoutError,
    ProviderFramePublication,
    RevisionKind,
    SnapshotEquivalenceSubject,
    canonical_market_history_database_path,
    compare_snapshot_equivalence,
    payload_mutation_sidecar_path,
    revision_identity,
)
from tradingagents.market_history.schema import (
    CREATE_MIGRATION_TABLE,
    MIGRATION_V1,
    MIGRATION_V2,
)


def _config_at(
    root: Path,
    *,
    data_usage_mode: DataUsageMode = DataUsageMode.PERSONAL_RESEARCH,
) -> MarketHistoryConfig:
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=data_usage_mode,
    )


def _backup_with_payload(root: Path):
    config = _config_at(root)
    with MarketHistoryStore.open(config) as store:
        artifact = store.install_payload(b"abc", media_type="application/json")
        backup = store.create_backup()
    return backup, artifact


def _provider_frame_publication(
    payload: bytes,
    *,
    snapshot_suffix: str = "1",
) -> ProviderFramePublication:
    return ProviderFramePublication(
        upstream_service_id="upstream:fixture",
        upstream_service_name="Fixture service",
        provider_dataset_id="provider:fixture",
        provider_name="fixture",
        dataset_name="mainland-current-adjusted-v1",
        adjustment_methodology="qfq",
        strict_history_qualified=False,
        instrument_id="instrument:fixture",
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


@pytest.mark.unit
def test_default_history_config_keeps_live_mainland_analysis_authoritative() -> None:
    config = MarketHistoryConfig.from_mapping(DEFAULT_CONFIG)

    assert config.mode is MarketHistoryMode.SHADOW
    assert config.data_usage_mode is DataUsageMode.PERSONAL_RESEARCH
    assert config.database_path.name == "market_history.sqlite3"
    assert config.payload_root == config.database_path.parent / "payloads"
    assert config.backup_root == config.database_path.parent / "backups"
    assert isinstance(config.database_path, Path)


@pytest.mark.unit
def test_market_history_database_path_is_canonical_across_configuration_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "history"
    direct = _config_at(root)
    aliased = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "nested" / ".." / "market_history.sqlite3",
        payload_root=root / "nested" / ".." / "payloads",
        backup_root=root / "nested" / ".." / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    assert direct.database_path == aliased.database_path
    assert direct.database_path == canonical_market_history_database_path(
        root / "." / "market_history.sqlite3"
    )
    assert payload_mutation_sidecar_path(
        direct.database_path
    ) == payload_mutation_sidecar_path(aliased.database_path)
    assert payload_mutation_sidecar_path(direct.database_path) == (
        direct.database_path.with_name(
            f"{direct.database_path.name}.payload-mutation.sqlite3"
        )
    )


@pytest.mark.unit
def test_provider_frame_publication_holds_sidecar_through_final_reference_commit(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    payload = b"Date,Close\n2026-07-24,10\n"
    phases: list[str] = []
    store: MarketHistoryStore

    def phase_hook(phase: str, _digest: str) -> None:
        phases.append(phase)
        if phase not in {
            "publisher_mutex_acquired",
            "publisher_references_committed",
        }:
            return
        assert store._connection.in_transaction is False
        contender = sqlite3.connect(
            payload_mutation_sidecar_path(config.database_path),
            isolation_level=None,
            timeout=0,
        )
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()

    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        first = store.publish_provider_frame(_provider_frame_publication(payload))
        second = store.publish_provider_frame(_provider_frame_publication(payload))

    assert first == second
    assert phases == [
        "publisher_mutex_waiting",
        "publisher_mutex_acquired",
        "publisher_file_installed",
        "publisher_payload_row_committed",
        "publisher_references_committed",
    ] * 2
    released = sqlite3.connect(
        payload_mutation_sidecar_path(config.database_path),
        isolation_level=None,
        timeout=0,
    )
    try:
        released.execute("BEGIN IMMEDIATE")
        released.rollback()
    finally:
        released.close()


@pytest.mark.unit
def test_payload_mutation_rejects_reverse_main_database_first_lock_order(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    with MarketHistoryStore.open(config) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(PayloadLockOrderError, match="must be acquired before"):
                store.install_payload(
                    b"reverse-lock-order",
                    media_type="application/json",
                )
        finally:
            store._connection.rollback()

        assert store._connection.execute(
            "SELECT 1 FROM payload_artifacts"
        ).fetchone() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "interrupt_phase",
    (
        "publisher_file_installed",
        "publisher_payload_row_committed",
    ),
)
def test_interrupted_provider_frame_publication_leaves_no_durable_reference(
    tmp_path: Path,
    interrupt_phase: str,
) -> None:
    config = _config_at(tmp_path / "history")
    payload = b"Date,Close\n2026-07-24,10\n"
    interrupted = False

    class InjectedInterruption(RuntimeError):
        pass

    def phase_hook(phase: str, _digest: str) -> None:
        nonlocal interrupted
        if phase == interrupt_phase and not interrupted:
            interrupted = True
            raise InjectedInterruption(phase)

    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        with pytest.raises(InjectedInterruption, match=interrupt_phase):
            store.publish_provider_frame(_provider_frame_publication(payload))

        assert store._connection.execute(
            "SELECT 1 FROM provider_frame_revisions"
        ).fetchone() is None
        cleanup = store.collect_orphan_payloads()

    assert cleanup.collected_digests == (sha256(payload).hexdigest(),)


@pytest.mark.unit
def test_distinct_canonical_databases_cannot_share_one_payload_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "history"
    shared_payload_root = root / "payloads"
    first = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "first.sqlite3",
        payload_root=shared_payload_root,
        backup_root=root / "first-backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    second = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "second.sqlite3",
        payload_root=shared_payload_root,
        backup_root=root / "second-backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    with MarketHistoryStore.open(first):
        pass

    with pytest.raises(ValueError, match="payload root.*canonical database"):
        MarketHistoryStore.open(second)


@pytest.mark.unit
def test_opening_a_new_store_publishes_the_durable_schema_atomically(tmp_path: Path) -> None:
    root = tmp_path / "market-history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    with MarketHistoryStore.open(config) as store:
        status = store.status()

    assert config.database_path.is_file()
    assert status.schema_version == 9
    assert status.foreign_keys_enabled is True
    assert status.journal_mode == "wal"
    assert status.synchronous == "full"
    assert {
        "adjustment_factor_revisions",
        "history_bundle_adjustment_factor_revisions",
        "history_bundle_observation_revisions",
        "history_bundle_publication_membership_audit",
        "history_bundle_revisions",
        "history_bundle_trading_status_revisions",
        "history_store_diagnostics",
        "ingestion_runs",
        "instrument_provider_state",
        "instruments",
        "market_session_calendars",
        "market_sessions",
        "payload_artifacts",
        "provider_datasets",
        "provider_frame_revisions",
        "publication_watermarks",
        "raw_market_observation_revisions",
        "request_cooldowns",
        "request_leases",
        "request_queue",
        "schema_migrations",
        "shadow_equivalence_results",
        "snapshot_factor_pins",
        "snapshot_observation_pins",
        "snapshot_pins",
        "trading_status_revisions",
        "upstream_services",
    }.issubset(status.tables)


@pytest.mark.unit
def test_provider_payload_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    root = tmp_path / "market-history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    with MarketHistoryStore.open(config) as store:
        first = store.install_payload(b"abc", media_type="application/json")
        second = store.install_payload(b"abc", media_type="application/json")
        restored = store.read_payload(first.digest)

    assert first == second
    assert first.digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert first.relative_path == Path("sha256/ba/ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad.bin")
    assert restored == b"abc"


@pytest.mark.unit
@pytest.mark.parametrize(
    "corrupt_bytes",
    (
        None,
        b"abd",
        b"a",
    ),
    ids=("missing", "wrong-digest", "wrong-length"),
)
def test_committed_payload_corruption_fails_closed_without_repair(
    tmp_path: Path,
    corrupt_bytes: bytes | None,
) -> None:
    config = _config_at(tmp_path / "history")
    with MarketHistoryStore.open(config) as store:
        artifact = store.install_payload(b"abc", media_type="application/json")
        payload_path = config.payload_root / artifact.relative_path
        if corrupt_bytes is None:
            payload_path.unlink()
        else:
            payload_path.write_bytes(corrupt_bytes)

        with pytest.raises(MarketHistoryCorruptionError):
            store.install_payload(b"abc", media_type="application/json")
        with pytest.raises(MarketHistoryCorruptionError):
            store.read_payload(artifact.digest)
        with pytest.raises(MarketHistoryCorruptionError):
            store.collect_orphan_payloads()
        assert store._connection.execute(
            "SELECT 1 FROM payload_artifacts WHERE digest = ?",
            (artifact.digest,),
        ).fetchone() == (1,)

        if corrupt_bytes is None:
            assert not payload_path.exists()
        else:
            assert payload_path.read_bytes() == corrupt_bytes


@pytest.mark.unit
def test_sidecar_timeout_is_typed_and_mutates_no_history_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config_at(tmp_path / "history")
    with MarketHistoryStore.open(config) as store:
        blocker = sqlite3.connect(
            payload_mutation_sidecar_path(config.database_path),
            isolation_level=None,
        )
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(store_module, "_PAYLOAD_MUTATION_TIMEOUT_SECONDS", 0)
        database_before = _database_files_bytes(config.database_path)
        payloads_before = _tree_bytes(config.payload_root)
        try:
            with pytest.raises(PayloadMutationTimeoutError):
                store.install_payload(
                    b'{"blocked":"publication"}',
                    media_type="application/json",
                )
        finally:
            blocker.rollback()
            blocker.close()

        assert _database_files_bytes(config.database_path) == database_before
        assert _tree_bytes(config.payload_root) == payloads_before


@pytest.mark.unit
def test_orphan_collection_commits_row_deletion_before_unlink(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    phases: list[str] = []
    store: MarketHistoryStore
    payload_path: Path

    def phase_hook(phase: str, digest: str) -> None:
        phases.append(phase)
        if phase in {"gc_row_delete_committed", "gc_before_unlink"}:
            row = store._connection.execute(
                "SELECT 1 FROM payload_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
            assert row is None
            assert payload_path.is_file()
        elif phase == "gc_after_unlink_before_mutex_release":
            assert not payload_path.exists()

    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        artifact = store.install_payload(b"orphan", media_type="application/json")
        payload_path = config.payload_root / artifact.relative_path
        phases.clear()

        result = store.collect_orphan_payloads()
        repeat = store.collect_orphan_payloads()

    assert isinstance(result, PayloadCollectionResult)
    assert result.collected_digests == (artifact.digest,)
    assert result.outcomes[0].status is PayloadMaintenanceStatus.COLLECTED
    assert result.outcomes[0].retryable is False
    assert repeat.outcomes == ()
    assert phases == [
        "gc_mutex_waiting",
        "gc_mutex_acquired",
        "gc_row_delete_committed",
        "gc_before_unlink",
        "gc_after_unlink_before_mutex_release",
    ]


@pytest.mark.unit
def test_unlink_failure_is_typed_retryable_and_not_reported_as_collected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config_at(tmp_path / "history")
    original_unlink = Path.unlink
    failed_once = False

    with MarketHistoryStore.open(config) as store:
        artifact = store.install_payload(
            b"retryable-orphan",
            media_type="application/json",
        )
        payload_path = config.payload_root / artifact.relative_path

        def fail_first_payload_unlink(path: Path, *args, **kwargs):
            nonlocal failed_once
            if path == payload_path and not failed_once:
                failed_once = True
                raise PermissionError("injected unlink failure")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_first_payload_unlink)
        failed = store.collect_orphan_payloads()

        assert failed.collected_digests == ()
        assert len(failed.failures) == 1
        assert failed.failures[0].status is PayloadMaintenanceStatus.UNLINK_FAILED
        assert failed.failures[0].retryable is True
        assert store._connection.execute(
            "SELECT 1 FROM payload_artifacts WHERE digest = ?",
            (artifact.digest,),
        ).fetchone() is None
        assert payload_path.is_file()

        recovered = store.collect_orphan_payloads()

    assert recovered.collected_digests == (artifact.digest,)
    assert not payload_path.exists()


@pytest.mark.unit
def test_unregistered_file_sweep_rechecks_registration_and_references_while_locked(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    payload = b"Date,Close\n2026-07-24,10\n"
    digest = sha256(payload).hexdigest()
    payload_path = (
        config.payload_root / "sha256" / digest[:2] / f"{digest}.bin"
    )
    published = False
    store: MarketHistoryStore

    def phase_hook(phase: str, candidate_digest: str) -> None:
        nonlocal published
        if (
            phase == "gc_mutex_acquired"
            and candidate_digest == digest
            and not published
        ):
            published = True
            store.publish_provider_frame(_provider_frame_publication(payload))

    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        payload_path.parent.mkdir(parents=True)
        payload_path.write_bytes(payload)

        result = store.collect_orphan_payloads()
        stored = store.get_provider_frame(f"snapshot:{'1' * 64}")

    assert result.collected_digests == ()
    assert result.outcomes[0].status is PayloadMaintenanceStatus.SKIPPED
    assert stored.payload_digest == digest
    assert payload_path.read_bytes() == payload


@pytest.mark.unit
def test_gc_sidecar_timeout_is_retryable_and_mutates_no_history_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config_at(tmp_path / "history")
    with MarketHistoryStore.open(config) as store:
        artifact = store.install_payload(
            b"blocked-orphan",
            media_type="application/json",
        )
        blocker = sqlite3.connect(
            payload_mutation_sidecar_path(config.database_path),
            isolation_level=None,
        )
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(store_module, "_PAYLOAD_MUTATION_TIMEOUT_SECONDS", 0)
        database_before = _database_files_bytes(config.database_path)
        payloads_before = _tree_bytes(config.payload_root)
        try:
            result = store.collect_orphan_payloads()
        finally:
            blocker.rollback()
            blocker.close()

        assert _database_files_bytes(config.database_path) == database_before
        assert _tree_bytes(config.payload_root) == payloads_before

    assert result.collected_digests == ()
    assert len(result.failures) == 1
    assert result.failures[0].digest == artifact.digest
    assert result.failures[0].status is PayloadMaintenanceStatus.MUTEX_TIMEOUT
    assert result.failures[0].retryable is True


@pytest.mark.unit
def test_backup_restores_database_and_referenced_payloads_together(tmp_path: Path) -> None:
    payload = b"Date,Close\n2026-07-24,10\n"
    source_root = tmp_path / "source"
    source_config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=source_root / "market_history.sqlite3",
        payload_root=source_root / "payloads",
        backup_root=source_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    with MarketHistoryStore.open(source_config) as store:
        stored = store.publish_provider_frame(
            _provider_frame_publication(payload)
        )
        backup = store.create_backup()

    restored_root = tmp_path / "restored"
    restored_config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=restored_root / "market_history.sqlite3",
        payload_root=restored_root / "payloads",
        backup_root=restored_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    MarketHistoryStore.restore_backup(backup.path, restored_config)

    with MarketHistoryStore.open(restored_config) as restored:
        status = restored.status()
        restored_payload = restored.read_payload(stored.payload_digest)
        restored_frame = restored.get_provider_frame(stored.snapshot_id)

    assert backup.manifest_path.is_file()
    assert "payload-mutation" not in backup.manifest_path.read_text(
        encoding="utf-8"
    )
    assert status.schema_version == 9
    assert restored_payload == payload
    assert restored_frame == stored


@pytest.mark.unit
def test_restore_sidecar_timeout_leaves_canonical_target_untouched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    restored = _config_at(tmp_path / "restored")
    sidecar_path = payload_mutation_sidecar_path(restored.database_path)
    sidecar_path.parent.mkdir(parents=True)
    blocker = sqlite3.connect(sidecar_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(store_module, "_PAYLOAD_MUTATION_TIMEOUT_SECONDS", 0)
    try:
        with pytest.raises(PayloadMutationTimeoutError):
            MarketHistoryStore.restore_backup(backup.path, restored)
    finally:
        blocker.rollback()
        blocker.close()

    assert not restored.database_path.exists()
    assert not restored.payload_root.exists()


@pytest.mark.unit
def test_restore_rejects_tampered_database_before_installing_anything(
    tmp_path: Path,
) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    (backup.path / "market_history.sqlite3").write_bytes(b"tampered")
    restored = _config_at(tmp_path / "restored")

    with pytest.raises(MarketHistoryCorruptionError, match="failed verification"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert not restored.database_path.exists()
    assert not restored.payload_root.exists()


@pytest.mark.unit
def test_restore_rejects_manifest_paths_that_escape_the_backup_root(
    tmp_path: Path,
) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["filename"] = "../outside.sqlite3"
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restored = _config_at(tmp_path / "restored")

    with pytest.raises(MarketHistoryCorruptionError, match="path escapes configured root"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert not restored.database_path.exists()


@pytest.mark.unit
def test_restore_never_overwrites_an_existing_database(tmp_path: Path) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    restored = _config_at(tmp_path / "restored")
    restored.database_path.parent.mkdir(parents=True)
    restored.database_path.write_bytes(b"keep-existing-database")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert restored.database_path.read_bytes() == b"keep-existing-database"


@pytest.mark.unit
def test_restore_rejects_a_backup_from_another_data_usage_mode(
    tmp_path: Path,
) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    restored = _config_at(
        tmp_path / "restored",
        data_usage_mode=DataUsageMode.PRODUCTION,
    )

    with pytest.raises(MarketHistoryCorruptionError, match="data-usage mode"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert not restored.database_path.exists()


@pytest.mark.unit
def test_restore_rejects_tampered_referenced_payloads(tmp_path: Path) -> None:
    backup, artifact = _backup_with_payload(tmp_path / "source")
    payload_path = backup.path / "payloads" / artifact.relative_path
    payload_path.write_bytes(b"tampered")
    restored = _config_at(tmp_path / "restored")

    with pytest.raises(MarketHistoryCorruptionError, match="failed verification"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert not restored.database_path.exists()
    assert not restored.payload_root.exists()


@pytest.mark.unit
def test_restore_removes_a_payload_corrupted_during_canonical_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup, _artifact = _backup_with_payload(tmp_path / "source")
    restored = _config_at(tmp_path / "restored")
    real_replace = store_module.os.replace

    def corrupt_payload_after_replace(source, destination) -> None:
        real_replace(source, destination)
        target = Path(destination)
        if target.suffix == ".bin":
            target.write_bytes(b"corrupted-after-replace")

    monkeypatch.setattr(store_module.os, "replace", corrupt_payload_after_replace)

    with pytest.raises(MarketHistoryCorruptionError, match="failed verification"):
        MarketHistoryStore.restore_backup(backup.path, restored)

    assert not restored.database_path.exists()
    assert list(restored.payload_root.rglob("*.bin")) == []


@pytest.mark.unit
def test_failed_publication_leaves_only_a_collectible_payload_orphan(
    tmp_path: Path,
) -> None:
    config = _config_at(tmp_path / "history")
    first_payload = b"Date,Close\n2026-07-24,10\n"
    orphan_payload = b"Date,Close\n2026-07-24,20\n"

    def publication(
        *,
        upstream_service_id: str,
        provider_dataset_id: str,
        instrument_id: str,
        snapshot_suffix: str,
        payload: bytes,
    ) -> ProviderFramePublication:
        return ProviderFramePublication(
            upstream_service_id=upstream_service_id,
            upstream_service_name="Shared service name",
            provider_dataset_id=provider_dataset_id,
            provider_name="fixture",
            dataset_name="mainland-current-adjusted-v1",
            adjustment_methodology="qfq",
            strict_history_qualified=False,
            instrument_id=instrument_id,
            canonical_symbol=f"6005{snapshot_suffix}.SS",
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

    with MarketHistoryStore.open(config) as store:
        retained = store.publish_provider_frame(
            publication(
                upstream_service_id="upstream:retained",
                provider_dataset_id="provider:retained",
                instrument_id="instrument:retained",
                snapshot_suffix="1",
                payload=first_payload,
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.publish_provider_frame(
                publication(
                    upstream_service_id="upstream:missing-after-ignore",
                    provider_dataset_id="provider:failed",
                    instrument_id="instrument:failed",
                    snapshot_suffix="2",
                    payload=orphan_payload,
                )
            )

        orphan_digest = sha256(orphan_payload).hexdigest()
        assert store.read_payload(orphan_digest) == orphan_payload
        collected = store.collect_orphan_payloads().collected_digests

        assert collected == (orphan_digest,)
        with pytest.raises(KeyError):
            store.read_payload(orphan_digest)
        assert store.read_payload(retained.payload_digest) == first_payload


@pytest.mark.unit
def test_v1_equivalence_rows_migrate_without_losing_their_frame_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "market-history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    normalized_frame = b"Date,Open,High,Low,Close,Volume\n2026-07-24,10,11,9,10.5,100\n"
    frame_digest = sha256(normalized_frame).hexdigest()
    subject = SnapshotEquivalenceSubject(
        instrument_identity_id="registry:mainland-v1",
        provider_dataset_id="provider:legacy",
        adjustment_basis="qfq",
        effective_dates=("2026-07-24",),
        eligible_observation_ids=("row-1",),
        frame_sha256=frame_digest,
        derived_fact_digests=("facts",),
        lineage_digests=("lineage",),
    )
    comparison = compare_snapshot_equivalence(subject, subject)
    with MarketHistoryStore.open(config) as store:
        stored = store.publish_provider_frame(
            ProviderFramePublication(
                upstream_service_id="upstream:legacy",
                upstream_service_name="Legacy Provider",
                provider_dataset_id="provider:legacy",
                provider_name="legacy",
                dataset_name="mainland-current-adjusted-v1",
                adjustment_methodology="qfq",
                strict_history_qualified=False,
                instrument_id="instrument:legacy",
                canonical_symbol="600519.SS",
                reference_market="XSHG",
                instrument_kind="equity",
                currency="CNY",
                identity_revision="mainland-routing-v1",
                snapshot_id=f"snapshot:{'a' * 64}",
                requested_start="2026-07-24",
                requested_end="2026-07-24",
                effective_trading_date="2026-07-24",
                adjustment_basis="qfq",
                frame_digest=frame_digest,
                history_rows=1,
                retrieved_at="2026-07-24T10:00:00+00:00",
                normalized_frame=normalized_frame,
            )
        )
        store.record_mainland_equivalence(
            MainlandEquivalenceScenario.PRIMARY_PROVIDER,
            comparison,
            frame_revision_id=stored.frame_revision_id,
        )

    connection = sqlite3.connect(config.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP INDEX request_queue_by_service_priority")
        connection.execute("DROP TABLE request_queue")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
        for column in (
            "history_rows",
            "effective_trading_date",
            "normalization_version",
            "manifest_json",
            "manifest_digest",
            "identity_version",
        ):
            connection.execute(f"ALTER TABLE snapshot_pins DROP COLUMN {column}")
        connection.execute(
            "ALTER TABLE shadow_equivalence_results "
            "RENAME TO shadow_equivalence_results_v2"
        )
        connection.execute(
            "CREATE TABLE shadow_equivalence_results ("
            "equivalence_result_id TEXT PRIMARY KEY, "
            "frame_revision_id TEXT NOT NULL "
            "REFERENCES provider_frame_revisions(frame_revision_id), "
            "reconstructed_snapshot_id TEXT, compared_at TEXT NOT NULL, "
            "identity_matches INTEGER NOT NULL, provider_matches INTEGER NOT NULL, "
            "adjustment_basis_matches INTEGER NOT NULL, "
            "effective_range_matches INTEGER NOT NULL, "
            "eligible_observations_match INTEGER NOT NULL, "
            "frame_digest_matches INTEGER NOT NULL, derived_facts_match INTEGER NOT NULL, "
            "lineage_matches INTEGER NOT NULL, passed INTEGER NOT NULL, "
            "detail_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO shadow_equivalence_results "
            "SELECT equivalence_result_id, frame_revision_id, reconstructed_snapshot_id, "
            "compared_at, identity_matches, provider_matches, adjustment_basis_matches, "
            "effective_range_matches, eligible_observations_match, frame_digest_matches, "
            "derived_facts_match, lineage_matches, passed, detail_json "
            "FROM shadow_equivalence_results_v2"
        )
        connection.execute("DROP TABLE shadow_equivalence_results_v2")
        connection.commit()
    finally:
        connection.close()

    with MarketHistoryStore.open(config) as migrated:
        status = migrated.status()
        results = migrated.latest_mainland_equivalence_results()

    assert status.schema_version == 9
    assert "request_queue" in status.tables
    assert results == {"legacy_unclassified": True}


@pytest.mark.unit
def test_failed_snapshot_v2_migration_leaves_v2_database_usable_and_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config_at(tmp_path / "history")
    config.database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(config.database_path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(CREATE_MIGRATION_TABLE)
        for statement in MIGRATION_V1:
            connection.execute(statement)
        for statement in MIGRATION_V2:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (1, 'initial_point_in_time_market_history', '2026-07-24T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (2, 'provider_request_priority_queue', '2026-07-24T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO history_store_diagnostics "
            "(diagnostic_id, occurred_at, operation, severity, code, detail) "
            "VALUES ('migration-control', '2026-07-24T00:00:00+00:00', "
            "'migration-test', 'info', 'control', 'preserve me')"
        )
        connection.commit()
    finally:
        connection.close()

    original_migration = store_module.MIGRATION_V3
    monkeypatch.setattr(
        store_module,
        "MIGRATION_V3",
        (
            "ALTER TABLE snapshot_pins ADD COLUMN identity_version TEXT",
            "INVALID SQL FOR ATOMIC MIGRATION TEST",
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        MarketHistoryStore.open(config)

    connection = sqlite3.connect(config.database_path)
    try:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(snapshot_pins)")
        }
        diagnostic = connection.execute(
            "SELECT detail FROM history_store_diagnostics "
            "WHERE diagnostic_id = 'migration-control'"
        ).fetchone()
        foreign_key_failures = tuple(connection.execute("PRAGMA foreign_key_check"))
    finally:
        connection.close()

    assert version == (2,)
    assert "identity_version" not in columns
    assert diagnostic == ("preserve me",)
    assert foreign_key_failures == ()

    monkeypatch.setattr(store_module, "MIGRATION_V3", original_migration)
    with MarketHistoryStore.open(config) as upgraded:
        assert upgraded.status().schema_version == 9
        assert upgraded._connection.execute(
            "SELECT detail FROM history_store_diagnostics "
            "WHERE diagnostic_id = 'migration-control'"
        ).fetchone() == ("preserve me",)


@pytest.mark.unit
def test_raw_observation_revision_identity_is_canonical_and_content_addressed() -> None:
    fields = {
        "provider_dataset_id": "baostock.cn-a.daily-v1",
        "instrument_id": "mainland:600519.SS:equity",
        "session_date": "2026-07-24",
        "open": "1415.00",
        "high": "1425.00",
        "low": "1401.00",
        "close": "1418.00",
        "volume": "3500000",
    }

    identity = revision_identity(RevisionKind.RAW_MARKET_OBSERVATION, fields)
    reordered = revision_identity(
        RevisionKind.RAW_MARKET_OBSERVATION,
        dict(reversed(tuple(fields.items()))),
    )

    assert identity == reordered
    assert identity == (
        "raw-market-observation=sha256:"
        "3aa2f458bfdf9f2a47f7390f1e538c5b987cb00887d67f35469a0f383aabd82f"
    )
