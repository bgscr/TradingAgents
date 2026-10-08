from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from hashlib import sha256
from itertools import combinations
from pathlib import Path

import pytest

import tradingagents.windows_sandbox_qualification as qualification_module
from scripts import verify_ticket06_immutable_state as immutable_verifier
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
)
from tradingagents.windows_sandbox_acceptance import (
    QUALIFIED_RUNNER_SOURCE_FILES,
    QUALIFIED_RUNNER_TARGET_FILES,
    TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY,
    TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    Ticket06AcceptanceStructuralQualification,
    qualify_ticket06_acceptance_runner_structure,
)
from tradingagents.windows_sandbox_qualification import (
    ISO03_PRECONSTRUCTION_FAILURE_RECEIPT_PREFIX,
    AdversarialProbeResult,
    AdversarialProbeTarget,
    GuestQualificationResult,
    HistoricalISO03ReportContract,
    HistoricalQualificationReportPin,
    ISO03PreconstructionFailureReceipt,
    ISO03QualificationStatus,
    LocalWriteResult,
    LocalWriteTarget,
    MappingVerification,
    MinimalSandboxLaunchContract,
    MinimalSandboxLaunchEvidence,
    NetworkVerification,
    ProbeMechanism,
    ProbeObservation,
    ProtectedHostInventory,
    QualificationStaticBindings,
    ReviewedHostHarnessFile,
    ReviewedHostHarnessManifest,
    WindowsHostPlatformFacts,
    WindowsHostQualificationState,
    _validate_historical_report_pins,
    _validate_historical_report_pins_in_inventory,
    build_guest_qualification_result,
    build_minimal_sandbox_launch_contract,
    build_protected_writer_quiescence_contract,
    build_protected_writer_quiescence_evidence,
    build_qualification_completion_marker,
    build_reviewed_host_harness_manifest,
    capture_protected_host_inventory,
    capture_windows_host_platform_facts,
    compare_protected_host_inventories,
    construct_minimal_sandbox_launch,
    execute_iso03_boundary_qualification,
    host_path_identity,
    launch_minimal_sandbox,
    load_historical_iso03_qualification_report,
    qualify_windows_sandbox_host,
    run_minimal_sandbox_launch,
    select_adversarial_probe_targets,
    stage_boundary_runner_input,
    validate_boundary_qualification_export,
    validate_boundary_runner_input,
    validate_iso03_historical_evidence_root_coverage,
    validate_verifier_root_ownership,
)
from tradingagents.windows_sandbox_runner import (
    ProtectedObjectKind,
    ProtectedRootBinding,
    ProtectedRootManifest,
    ProtectedRootRole,
    SandboxContractError,
    SandboxFailureReason,
    SandboxFeatureState,
    SandboxMappingBinding,
    SandboxMappingRole,
    SandboxPlatformReport,
    SandboxProcessResult,
    SandboxProcessTopologyEntry,
    WindowsSandboxPlatform,
    build_environment_policy,
    build_guest_layout,
    build_mapping_manifest,
    build_protected_root_manifest,
    default_protected_root_bindings,
    render_wsb_configuration,
)

_DIRECTORY_ROLES = {
    ProtectedRootRole.APPLICATION_PROFILE,
    ProtectedRootRole.PROVIDER_LEDGER_PAYLOADS,
    ProtectedRootRole.PROVIDER_STATE,
    ProtectedRootRole.CALENDAR_STATE,
    ProtectedRootRole.CALENDAR_PAYLOADS,
    ProtectedRootRole.CHECKPOINTS,
    ProtectedRootRole.REPORTS_LOGS,
    ProtectedRootRole.TICKET07_EVIDENCE,
    ProtectedRootRole.PRIOR_INVENTORIES,
    ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
    ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
    ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
}

_HISTORICAL_EVIDENCE_ROLES = (
    ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
    ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
    ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
)

_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY = (
    "ticket06-sandbox-command-ledger:v1:sha256:"
    "5444477692f8a4a517d6e78719c5d21d97550c6a08610bd88596ca76cb68ea23"
)


def _prior_historical_report_contract() -> HistoricalISO03ReportContract:
    return HistoricalISO03ReportContract(
        command_ledger_identity=_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        completion_marker_contract_identity=(
            TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY
        ),
    )


def _current_historical_report_contract() -> HistoricalISO03ReportContract:
    return HistoricalISO03ReportContract(
        command_ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        completion_marker_contract_identity=(
            TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY
        ),
    )


def _test_identity(namespace: str, value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{namespace}:sha256:{sha256(material).hexdigest()}"


def _protected_manifest(root: Path):
    bindings = []
    for role in ProtectedRootRole:
        path = root / role.value
        if role in _DIRECTORY_ROLES:
            path.mkdir(parents=True)
            (path / "retained.bin").write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.DIRECTORY
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.FILE
        bindings.append(ProtectedRootBinding(role, path, kind))
    return build_protected_root_manifest(tuple(bindings))


def _create_historical_provider_ledger(path: Path) -> None:
    path.unlink()
    fixture_sequence_ids = tuple(
        f"fixture-sequence-{index:04d}" for index in range(1020)
    )
    sequence_ids = (*immutable_verifier.INCIDENT_SEQUENCE_IDS, *fixture_sequence_ids)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE provider_request_sequences (
                sequence_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE provider_request_attempts (
                sequence_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL,
                attempt_event_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                upstream_service_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                PRIMARY KEY (sequence_id, attempt_index)
            );
            """
        )
        connection.executemany(
            "INSERT INTO provider_request_sequences VALUES (?, 'completed')",
            ((sequence_id,) for sequence_id in sequence_ids),
        )
        incident_attempt_ids = dict(immutable_verifier.INCIDENT_IDENTITIES)
        connection.executemany(
            "INSERT INTO provider_request_attempts VALUES (?, 1, ?, "
            "'market-snapshot', 'Yahoo Finance', 'available')",
            (
                (
                    sequence_id,
                    incident_attempt_ids.get(
                        sequence_id,
                        f"fixture-attempt-{sequence_id}-1",
                    ),
                )
                for sequence_id in sequence_ids
            ),
        )
        connection.executemany(
            "INSERT INTO provider_request_attempts VALUES (?, 2, ?, "
            "'market-snapshot', 'Yahoo Finance', 'available')",
            (
                (sequence_id, f"fixture-attempt-{sequence_id}-2")
                for sequence_id in fixture_sequence_ids[:12]
            ),
        )


def _append_authorized_live_provider_rows(path: Path) -> tuple[str, ...]:
    sequence_ids = tuple(
        f"authorized-live-sequence-{index:02d}" for index in range(11)
    )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO provider_request_sequences VALUES (?, 'completed')",
            ((sequence_id,) for sequence_id in sequence_ids),
        )
        connection.executemany(
            "INSERT INTO provider_request_attempts VALUES (?, 1, ?, "
            "'market-snapshot', 'BaoStock', 'available')",
            (
                (sequence_id, f"authorized-live-attempt-{index:02d}")
                for index, sequence_id in enumerate(sequence_ids)
            ),
        )
    return sequence_ids


def _create_semantic_calendar(path: Path) -> None:
    path.unlink()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE market_session_calendars (
                calendar_revision_id TEXT PRIMARY KEY,
                payload_digest TEXT NOT NULL
            );
            CREATE TABLE market_sessions (
                calendar_revision_id TEXT NOT NULL,
                session_date TEXT NOT NULL,
                session_status TEXT NOT NULL,
                PRIMARY KEY (calendar_revision_id, session_date)
            );
            INSERT INTO market_session_calendars VALUES ('calendar-1', 'digest-1');
            INSERT INTO market_sessions VALUES ('calendar-1', '2026-08-20', 'open');
            """
        )


def _materialize_required_default_roots(
    bindings: tuple[ProtectedRootBinding, ...],
) -> None:
    optional_companions = {
        ProtectedRootRole.PROVIDER_LEDGER_WAL,
        ProtectedRootRole.PROVIDER_LEDGER_SHM,
        ProtectedRootRole.CALENDAR_DATABASE_WAL,
        ProtectedRootRole.CALENDAR_DATABASE_SHM,
    }
    for binding in bindings:
        if binding.role in optional_companions:
            continue
        if binding.object_kind is ProtectedObjectKind.DIRECTORY:
            binding.path.mkdir(parents=True, exist_ok=True)
        else:
            binding.path.parent.mkdir(parents=True, exist_ok=True)
            if not binding.path.exists():
                binding.path.write_bytes(binding.role.value.encode("ascii"))


def _write_report_marker(
    root: Path,
    *,
    run_id: str,
    iso04: bool = False,
    trailing_n: bool = False,
) -> Path:
    report_name = (
        "iso04-ticket06-acceptance-report.json" if iso04 else "iso03-report.json"
    )
    contract_version = (
        "iso04-ticket06-acceptance-report-v1"
        if iso04
        else "iso03-host-qualification-report-v1"
    )
    identity_namespace = (
        "iso04-ticket06-acceptance-report:v1"
        if iso04
        else "iso03-host-qualification-report:v1"
    )
    payload = {
        "contract_version": contract_version,
        "run_id": run_id,
        "status": "fail" if iso04 else "pass",
    }
    payload["report_identity"] = _test_identity(identity_namespace, payload)
    marker = root / run_id / "host-audit" / report_name
    marker.parent.mkdir(parents=True)
    content = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    marker.write_bytes(content + (b"n" if trailing_n else b""))
    return marker


def _historical_manifest_with_markers(
    root: Path,
) -> tuple[object, tuple[HistoricalQualificationReportPin, ...]]:
    manifest = _protected_manifest(root)
    by_role = {binding.role: binding.path for binding in manifest.bindings}
    specifications = (
        (
            ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
            "iso03-20260814-016",
            False,
            False,
        ),
        (
            ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
            "iso03-20260814-017",
            False,
            False,
        ),
        (
            ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
            "iso03-20260814-018",
            False,
            False,
        ),
        (
            ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
            "iso04-20260815-ticket06-001",
            True,
            True,
        ),
        (
            ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
            "iso03-20260816-019",
            False,
            False,
        ),
    )
    pins = []
    for role, run_id, iso04, trailing_n in specifications:
        marker = _write_report_marker(
            by_role[role],
            run_id=run_id,
            iso04=iso04,
            trailing_n=trailing_n,
        )
        raw = marker.read_bytes()
        payload_bytes = raw[:-1] if trailing_n else raw
        payload = json.loads(payload_bytes)
        pins.append(
            HistoricalQualificationReportPin(
                role=role,
                run_id=run_id,
                relative_path=marker.relative_to(by_role[role]).as_posix(),
                byte_length=len(raw),
                sha256=sha256(raw).hexdigest(),
                report_identity=payload["report_identity"],
                current_iso03_schema=False,
            )
        )
    return manifest, tuple(pins)


def test_iso_03_host_inventory_is_deterministic_and_detects_rewrites(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    semantic = {
        "provider_ledger": {
            "attempts": {"count": 1036, "inventory_sha256": "a" * 64},
            "sequences": {"count": 1024, "inventory_sha256": "b" * 64},
        }
    }

    before = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )
    repeated = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    assert repeated == before
    assert len(before.roots) == len(tuple(ProtectedRootRole))
    assert before.inventory_identity.startswith(
        "windows-sandbox-protected-host-inventory:v1:sha256:"
    )

    target = tmp_path / "protected" / ProtectedRootRole.APPLICATION_PROFILE.value
    (target / "retained.bin").write_bytes(b"rewritten")
    after = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    delta = compare_protected_host_inventories(before, after)
    assert delta.equal is False
    assert (delta.appended, delta.deleted, delta.rewritten) == (0, 0, 1)


@pytest.mark.parametrize("mutation", ("append", "delete", "rewrite"))
def test_iso_03_current_provider_ledger_capture_is_self_relative_and_detects_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    by_role = {binding.role: binding.path for binding in manifest.bindings}
    provider_ledger = by_role[ProtectedRootRole.PROVIDER_LEDGER]
    calendar_database = by_role[ProtectedRootRole.CALENDAR_DATABASE]
    _create_historical_provider_ledger(provider_ledger)
    _create_semantic_calendar(calendar_database)

    with sqlite3.connect(provider_ledger) as connection:
        historical_counts = (
            connection.execute(
                "SELECT COUNT(*) FROM provider_request_sequences"
            ).fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM provider_request_attempts"
            ).fetchone()[0],
        )
    assert historical_counts == (1024, 1036)

    authorized_sequence_ids = _append_authorized_live_provider_rows(provider_ledger)

    def semantic_capture() -> dict[str, object]:
        return qualification_module._immutable_semantic_capture(
            manifest,
            capture=immutable_verifier.capture_immutable_state,
        )

    current = semantic_capture()
    sequences = current["provider_ledger"]["sequences"]
    attempts = current["provider_ledger"]["attempts"]
    assert (sequences["count"], attempts["count"]) == (1035, 1047)
    assert sequences["inventory_sha256"] == (
        "643dfdb9ac8ccc4bf4377e18451c3b931450587a38adfd71fba1a4790b438155"
    )
    assert attempts["inventory_sha256"] == (
        "0e96170bb4dac96fa382aacd46e32e444075971c9813756a975c5f26c95bcbbf"
    )
    assert semantic_capture() == current

    verifier_identity = "immutable-verifier:sha256:" + ("c" * 64)
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=semantic_capture,
        immutable_verifier_identity=verifier_identity,
    )
    with sqlite3.connect(provider_ledger) as connection:
        target = authorized_sequence_ids[-1]
        if mutation == "append":
            connection.execute(
                "INSERT INTO provider_request_sequences VALUES "
                "('qualification-window-append', 'completed')"
            )
            connection.execute(
                "INSERT INTO provider_request_attempts VALUES "
                "('qualification-window-append', 1, 'qualification-window-attempt', "
                "'market-snapshot', 'BaoStock', 'available')"
            )
        elif mutation == "delete":
            connection.execute(
                "DELETE FROM provider_request_attempts WHERE sequence_id = ?",
                (target,),
            )
            connection.execute(
                "DELETE FROM provider_request_sequences WHERE sequence_id = ?",
                (target,),
            )
        else:
            connection.execute(
                "UPDATE provider_request_sequences SET status = 'failed' "
                "WHERE sequence_id = ?",
                (target,),
            )
            connection.execute(
                "UPDATE provider_request_attempts SET outcome = 'provider_error' "
                "WHERE sequence_id = ?",
                (target,),
            )

    after = capture_protected_host_inventory(
        manifest,
        semantic_capture=semantic_capture,
        immutable_verifier_identity=verifier_identity,
    )
    delta = compare_protected_host_inventories(sentinel, after)

    assert delta.equal is False
    assert delta.semantic_equal is False


def test_iso_03_clean_close_keeps_absent_sqlite_companions_as_empty_protected_roots(
    tmp_path: Path,
) -> None:
    user_profile = tmp_path / "profile"
    repository_root = tmp_path / "repository"
    history_root = user_profile / ".tradingagents" / "market_history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=history_root / "market_history.sqlite3",
        payload_root=history_root / "payloads",
        backup_root=history_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    with MarketHistoryStore.open(config):
        pass
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        provider_ledger = authority.config.database_path

    bindings = default_protected_root_bindings(
        user_profile=user_profile,
        repository_root=repository_root,
    )
    _materialize_required_default_roots(bindings)
    companions = {
        ProtectedRootRole.PROVIDER_LEDGER_WAL: Path(f"{provider_ledger}-wal"),
        ProtectedRootRole.PROVIDER_LEDGER_SHM: Path(f"{provider_ledger}-shm"),
        ProtectedRootRole.CALENDAR_DATABASE_WAL: Path(f"{config.database_path}-wal"),
        ProtectedRootRole.CALENDAR_DATABASE_SHM: Path(f"{config.database_path}-shm"),
    }

    assert config.database_path.is_file()
    assert provider_ledger.is_file()
    assert all(not path.exists() for path in companions.values())

    manifest = build_protected_root_manifest(bindings)
    inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: {},
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )
    entries = {entry.role: entry for entry in manifest.entries}
    roots = {root.role: root for root in inventory.roots}

    assert entries[ProtectedRootRole.PROVIDER_LEDGER].concrete_object_required is True
    assert entries[ProtectedRootRole.CALENDAR_DATABASE].concrete_object_required is True
    for role, path in companions.items():
        assert entries[role].concrete_object_required is False
        assert roots[role].root_path_identity == host_path_identity(path)
        assert (roots[role].file_count, roots[role].byte_length, roots[role].files) == (
            0,
            0,
            (),
        )


def test_iso_03_repository_sqlite_companions_retain_full_byte_accounting(
    tmp_path: Path,
) -> None:
    user_profile = tmp_path / "profile"
    repository_root = tmp_path / "repository"
    history_root = user_profile / ".tradingagents" / "market_history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=history_root / "market_history.sqlite3",
        payload_root=history_root / "payloads",
        backup_root=history_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    with (
        MarketHistoryStore.open(config),
        MarketHistoryStore.open_provider_request_authority(config) as authority,
    ):
        bindings = default_protected_root_bindings(
            user_profile=user_profile,
            repository_root=repository_root,
        )
        _materialize_required_default_roots(bindings)
        companion_paths = {
            ProtectedRootRole.PROVIDER_LEDGER_WAL: Path(
                f"{authority.config.database_path}-wal"
            ),
            ProtectedRootRole.PROVIDER_LEDGER_SHM: Path(
                f"{authority.config.database_path}-shm"
            ),
            ProtectedRootRole.CALENDAR_DATABASE_WAL: Path(
                f"{config.database_path}-wal"
            ),
            ProtectedRootRole.CALENDAR_DATABASE_SHM: Path(
                f"{config.database_path}-shm"
            ),
        }
        assert all(path.is_file() for path in companion_paths.values())
        companion_content = {
            role: path.read_bytes() for role, path in companion_paths.items()
        }

        manifest = build_protected_root_manifest(bindings)
        inventory = capture_protected_host_inventory(
            manifest,
            semantic_capture=lambda: {},
            immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
        )
        entries = {entry.role: entry for entry in manifest.entries}
        roots = {root.role: root for root in inventory.roots}

        for role, content in companion_content.items():
            root = roots[role]
            assert entries[role].concrete_object_required is False
            assert (root.file_count, root.byte_length) == (1, len(content))
            assert len(root.files) == 1
            assert root.files[0].relative_path == "."
            assert root.files[0].sha256 == sha256(content).hexdigest()


@pytest.mark.parametrize(
    (
        "initial_content",
        "final_content",
        "expected_equal",
        "expected_counts",
    ),
    (
        pytest.param(None, None, True, (0, 0, 0), id="absent-to-absent"),
        pytest.param(b"stable", b"stable", True, (0, 0, 0), id="present-stable"),
        pytest.param(None, b"created", False, (1, 0, 0), id="absent-to-present"),
        pytest.param(b"deleted", None, False, (0, 1, 0), id="present-to-absent"),
        pytest.param(b"before", b"after", False, (0, 0, 1), id="present-rewrite"),
    ),
)
def test_iso_03_sqlite_companion_lifecycle_transitions_remain_protected(
    tmp_path: Path,
    initial_content: bytes | None,
    final_content: bytes | None,
    expected_equal: bool,
    expected_counts: tuple[int, int, int],
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    target = next(
        binding.path
        for binding in manifest.bindings
        if binding.role is ProtectedRootRole.PROVIDER_LEDGER_WAL
    )
    if initial_content is None:
        target.unlink()
    else:
        target.write_bytes(initial_content)
    def capture():
        return capture_protected_host_inventory(
            manifest,
            semantic_capture=lambda: {},
            immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
        )

    before = capture()

    if final_content is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(final_content)
    after = capture()

    delta = compare_protected_host_inventories(before, after)
    assert delta.equal is expected_equal
    assert (delta.appended, delta.deleted, delta.rewritten) == expected_counts
    assert delta.changed_roles == (
        () if expected_equal else (ProtectedRootRole.PROVIDER_LEDGER_WAL,)
    )


def _representative_historical_companion_contract(
    evidence_name: str,
) -> tuple[ProtectedRootManifest, ProtectedHostInventory]:
    companion_content = {
        ProtectedRootRole.PROVIDER_LEDGER_WAL: b"",
        ProtectedRootRole.PROVIDER_LEDGER_SHM: b"p" * 32_768,
        ProtectedRootRole.CALENDAR_DATABASE_WAL: b"",
        ProtectedRootRole.CALENDAR_DATABASE_SHM: b"c" * 32_768,
    }
    root_path_identities = {
        role: _test_identity("host-path", [evidence_name, role.value])
        for role in ProtectedRootRole
    }
    recorded_entries = [
        {
            "role": role.value,
            "object_kind": (
                ProtectedObjectKind.DIRECTORY.value
                if role in _DIRECTORY_ROLES
                else ProtectedObjectKind.FILE.value
            ),
            "path_identity": root_path_identities[role],
            "concrete_object_required": True,
            "reparse_points_permitted": False,
        }
        for role in ProtectedRootRole
    ]
    manifest_payload = {
        "contract_version": "windows-sandbox-protected-roots-v3",
        "entries": recorded_entries,
    }
    recorded_manifest = ProtectedRootManifest(
        **manifest_payload,
        manifest_identity=_test_identity(
            "windows-sandbox-protected-roots:v3",
            manifest_payload,
        ),
    )
    roots = []
    files = []
    for role in ProtectedRootRole:
        content = companion_content.get(role)
        root_files = []
        if content is not None:
            row = {
                "relative_path": ".",
                "canonical_path_identity": root_path_identities[role],
                "byte_length": len(content),
                "sha256": sha256(content).hexdigest(),
            }
            root_files.append(row)
            files.append(
                {
                    key: value
                    for key, value in row.items()
                    if key != "relative_path"
                }
            )
        root_payload = {
            "role": role.value,
            "object_kind": (
                ProtectedObjectKind.DIRECTORY.value
                if role in _DIRECTORY_ROLES
                else ProtectedObjectKind.FILE.value
            ),
            "root_path_identity": root_path_identities[role],
            "file_count": len(root_files),
            "byte_length": sum(row["byte_length"] for row in root_files),
            "files": root_files,
        }
        roots.append(
            {
                **root_payload,
                "inventory_identity": _test_identity(
                    "windows-sandbox-protected-root-inventory:v1",
                    root_payload,
                ),
            }
        )
    files.sort(key=lambda row: row["canonical_path_identity"])
    semantic_inventory: dict[str, object] = {}
    inventory_payload = {
        "contract_version": "windows-sandbox-protected-host-inventory-v1",
        "protected_root_manifest_identity": recorded_manifest.manifest_identity,
        "immutable_verifier_identity": (
            "immutable-verifier:sha256:"
            + sha256(evidence_name.encode("ascii")).hexdigest()
        ),
        "roots": roots,
        "files": files,
        "file_count": len(files),
        "byte_length": sum(row["byte_length"] for row in files),
        "semantic_inventory": semantic_inventory,
        "semantic_inventory_identity": _test_identity(
            "ticket06-immutable-semantic-inventory:v1",
            semantic_inventory,
        ),
    }
    recorded_inventory = ProtectedHostInventory(
        **inventory_payload,
        inventory_identity=_test_identity(
            "windows-sandbox-protected-host-inventory:v1",
            inventory_payload,
        ),
    )
    return recorded_manifest, recorded_inventory


def test_iso_03_run_026_and_iso04_004_historical_companion_contracts_remain_readable() -> None:
    expected_identities = {
        "run-026": (
            "windows-sandbox-protected-roots:v3:sha256:"
            "7543b6e9696069f5595599bdcfec7f10da5f93ba2f047d8694a07f0d6dcfbcb8",
            "windows-sandbox-protected-host-inventory:v1:sha256:"
            "6ee5e6e8474e1967793e22c8499b437d8dea5ad53626e56fce045b1681e104f0",
        ),
        "ISO04-004": (
            "windows-sandbox-protected-roots:v3:sha256:"
            "0e3ee71c2e185d6fa1456cc32c712b4c7584f83b9da406bf02f5c43e551b6ddf",
            "windows-sandbox-protected-host-inventory:v1:sha256:"
            "12c06e8d85d0d7a7a8ab100df1ce8e64e8931b23cda0e0026d4857c54bbac031",
        ),
    }
    for evidence_name, expected in expected_identities.items():
        recorded_manifest, recorded_inventory = (
            _representative_historical_companion_contract(evidence_name)
        )
        loaded_manifest = ProtectedRootManifest.model_validate_json(
            recorded_manifest.model_dump_json()
        )
        loaded_inventory = ProtectedHostInventory.model_validate_json(
            recorded_inventory.model_dump_json()
        )
        loaded_roots = {root.role: root for root in loaded_inventory.roots}

        assert (
            loaded_manifest.manifest_identity,
            loaded_inventory.inventory_identity,
        ) == expected
        assert all(entry.concrete_object_required for entry in loaded_manifest.entries)
        assert (
            loaded_roots[ProtectedRootRole.PROVIDER_LEDGER_WAL].file_count,
            loaded_roots[ProtectedRootRole.PROVIDER_LEDGER_WAL].byte_length,
        ) == (1, 0)
        assert (
            loaded_roots[ProtectedRootRole.PROVIDER_LEDGER_SHM].file_count,
            loaded_roots[ProtectedRootRole.PROVIDER_LEDGER_SHM].byte_length,
        ) == (1, 32_768)


def test_iso_03_quiescence_requires_attestation_and_stable_prelaunch_inventory(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")

    def capture() -> dict[str, object]:
        return {}

    identity = "immutable-verifier:sha256:" + ("c" * 64)
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=capture,
        immutable_verifier_identity=identity,
    )

    with pytest.raises(SandboxContractError):
        build_protected_writer_quiescence_contract(operator_attested=False)
    contract = build_protected_writer_quiescence_contract(operator_attested=True)
    evidence = build_protected_writer_quiescence_evidence(
        contract=contract,
        sentinel=sentinel,
        immediate_before=sentinel,
    )

    assert evidence.prelaunch_delta.equal is True
    assert evidence.sentinel_inventory_identity == sentinel.inventory_identity

    target = tmp_path / "protected" / ProtectedRootRole.APPLICATION_PROFILE.value
    (target / "retained.bin").write_bytes(b"changed-before-launch")
    changed = capture_protected_host_inventory(
        manifest,
        semantic_capture=capture,
        immutable_verifier_identity=identity,
    )
    with pytest.raises(SandboxContractError):
        build_protected_writer_quiescence_evidence(
            contract=contract,
            sentinel=sentinel,
            immediate_before=changed,
        )


def test_iso_03_verifier_semantics_bind_nonempty_checkpoint_report_and_ticket_roots(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")

    def semantic_row(role: ProtectedRootRole) -> dict[str, object]:
        content = role.value.encode("ascii")
        rows = [
            {
                "byte_length": len(content),
                "relative_path": "retained.bin",
                "sha256": sha256(content).hexdigest(),
            }
        ]
        return {
            "byte_length": len(content),
            "file_count": 1,
            "inventory_sha256": sha256(
                json.dumps(
                    rows,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }

    semantic = {
        "checkpoints": semantic_row(ProtectedRootRole.CHECKPOINTS),
        "reports": semantic_row(ProtectedRootRole.REPORTS_LOGS),
        "ticket07_evidence": semantic_row(ProtectedRootRole.TICKET07_EVIDENCE),
    }
    inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    validate_verifier_root_ownership(inventory)

    narrowed = {
        **semantic,
        "checkpoints": {
            "byte_length": 0,
            "file_count": 0,
            "inventory_sha256": sha256(b"[]").hexdigest(),
        },
    }
    narrowed_inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: narrowed,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )
    with pytest.raises(SandboxContractError):
        validate_verifier_root_ownership(narrowed_inventory)


def test_iso_03_inventory_hashes_internal_symlinks_without_following_outward(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    prior = tmp_path / "protected" / ProtectedRootRole.PRIOR_INVENTORIES.value
    internal = prior / "current"
    try:
        internal.symlink_to(prior / "retained.bin")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    semantic: dict[str, object] = {}

    inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    prior_inventory = next(
        item for item in inventory.roots if item.role is ProtectedRootRole.PRIOR_INVENTORIES
    )
    assert tuple(item.relative_path for item in prior_inventory.files) == (
        "current",
        "retained.bin",
    )

    internal.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    internal.symlink_to(outside)
    with pytest.raises(SandboxContractError):
        capture_protected_host_inventory(
            manifest,
            semantic_capture=lambda: semantic,
            immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
        )


def _static_bindings() -> QualificationStaticBindings:
    return QualificationStaticBindings(
        qualification_run_identity="iso03-boundary-qualification-run:v1:sha256:" + ("1" * 64),
        source_commit_id="2" * 40,
        source_tree_hash="3" * 40,
        source_input_identity="windows-sandbox-reviewed-source:v1:sha256:" + ("4" * 64),
        source_manifest_identity="windows-sandbox-source-manifest:v1:sha256:" + ("5" * 64),
        source_archive_sha256="6" * 64,
        runtime_bundle_identity="windows-offline-runtime-bundle:v1:sha256:" + ("7" * 64),
        runtime_bundle_manifest_sha256="8" * 64,
        runtime_bundle_contract_identity=(
            "windows-sandbox-runtime-bundle-input:v1:sha256:" + ("9" * 64)
        ),
        runner_identity="iso01-windows-sandbox-runner:v1:sha256:" + ("a" * 64),
        wsb_config_identity="windows-sandbox-config:v1:sha256:" + ("b" * 64),
        protected_root_manifest_identity=(
            "windows-sandbox-protected-roots:v1:sha256:" + ("c" * 64)
        ),
        mapping_manifest_identity=(
            "windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)
        ),
        guest_workspace_identity=(
            "windows-sandbox-guest-workspace:v1:sha256:" + ("e" * 64)
        ),
        qualification_control_identity=(
            "windows-sandbox-qualification-control:v1:sha256:" + ("f" * 64)
        ),
        platform_qualification_identity=(
            "windows-sandbox-host-platform-qualification:v1:sha256:" + ("0" * 64)
        ),
        immutable_verifier_identity="immutable-verifier:sha256:" + ("1" * 64),
        host_before_inventory_identity=(
            "windows-sandbox-protected-host-inventory:v1:sha256:" + ("2" * 64)
        ),
        acceptance_runner_structure=Ticket06AcceptanceStructuralQualification.create(
            runner_input_identity=(
                "windows-sandbox-qualified-runner-input:v2:sha256:" + ("3" * 64)
            ),
            environment_policy_identity=(
                "windows-sandbox-environment-policy:v1:sha256:" + ("4" * 64)
            ),
            host_harness_manifest_identity=(
                "iso03-reviewed-host-harness:v1:sha256:" + ("5" * 64)
            ),
            host_acceptance_module_sha256="6" * 64,
            boundary_runner_identity=(
                "iso01-windows-sandbox-runner:v1:sha256:" + ("a" * 64)
            ),
            wsb_config_identity=(
                "windows-sandbox-config:v1:sha256:" + ("b" * 64)
            ),
            wsb_config_policy_identity=(
                "windows-sandbox-config-policy:v1:sha256:" + ("c" * 64)
            ),
            mapping_manifest_identity=(
                "windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)
            ),
            command_ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        ),
    )


def _reviewed_host_harness(
    *,
    implementation_base_commit_id: str,
    candidate_tree_hash: str,
) -> ReviewedHostHarnessManifest:
    paths = (
        "scripts/run_iso03_windows_sandbox_qualification.py",
        "tradingagents/offline_runtime_bundle.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
    )
    files = tuple(
        ReviewedHostHarnessFile(
            relative_path=path,
            byte_length=index + 1,
            sha256=f"{index + 1:x}" * 64,
        )
        for index, path in enumerate(paths)
    )
    payload = {
        "contract_version": "iso03-reviewed-host-harness-v1",
        "implementation_base_commit_id": implementation_base_commit_id,
        "candidate_tree_hash": candidate_tree_hash,
        "source_mode": "reviewed_index",
        "files": [item.model_dump(mode="json") for item in files],
    }
    return ReviewedHostHarnessManifest(
        **payload,
        manifest_identity=_test_identity("iso03-reviewed-host-harness:v1", payload),
    )


def _passing_guest_result() -> GuestQualificationResult:
    probes = tuple(
        AdversarialProbeResult(
            mechanism=mechanism,
            target_host_path=(
                rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"
            ),
            target_path_identity=host_path_identity(
                rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"
            ),
            observation=ProbeObservation.GUEST_LOCAL_ONLY,
            contained=True,
        )
        for mechanism in ProbeMechanism
    )
    writes = tuple(
        LocalWriteResult(target=target, succeeded=True)
        for target in LocalWriteTarget
    )
    return build_guest_qualification_result(
        _static_bindings(),
        mapping=MappingVerification(
            networking_disabled=True,
            read_only_input_mappings=True,
            exactly_one_writable_results_mapping=True,
            forbidden_host_mappings_absent=True,
            guest_read_only_write_checks_passed=True,
        ),
        network=NetworkVerification(
            ipv4_default_route_present=False,
            ipv6_default_route_present=False,
            external_route_usable=False,
        ),
        live_environment=(
            ("PYTHON_DOTENV_DISABLED", "1"),
            ("RUN_LIVE_DEEPSEEK_TESTS", "0"),
            ("RUN_LIVE_PROVIDER_TESTS", "0"),
            ("RUN_LIVE_TUSHARE_TESTS", "0"),
        ),
        probes=probes,
        local_writes=writes,
    )


_RECORDED_SOURCE_INPUT_IDENTITY = (
    "windows-sandbox-reviewed-source:v1:sha256:" + ("1" * 64)
)
_RECORDED_RUNTIME_BUNDLE_IDENTITY = (
    "windows-offline-runtime-bundle:v1:sha256:" + ("2" * 64)
)
_RECORDED_RUNNER_IDENTITY = (
    "iso01-windows-sandbox-runner:v1:sha256:" + ("3" * 64)
)
_RECORDED_WSB_CONFIG_IDENTITY = (
    "windows-sandbox-config:v1:sha256:" + ("4" * 64)
)


def _bind_recorded_structure(
    payload: dict[str, object],
    *,
    command_ledger_identity: str,
    host_harness: ReviewedHostHarnessManifest,
) -> None:
    structure = dict(payload["acceptance_runner_structure"])
    host_acceptance_module = next(
        item
        for item in host_harness.files
        if item.relative_path == "tradingagents/windows_sandbox_qualification.py"
    )
    structure.update(
        command_ledger_identity=command_ledger_identity,
        boundary_runner_identity=_RECORDED_RUNNER_IDENTITY,
        wsb_config_identity=_RECORDED_WSB_CONFIG_IDENTITY,
        host_harness_manifest_identity=host_harness.manifest_identity,
        host_acceptance_module_sha256=host_acceptance_module.sha256,
    )
    structure["qualification_identity"] = _test_identity(
        "ticket06-acceptance-runner-structure:v2",
        {
            key: value
            for key, value in structure.items()
            if key != "qualification_identity"
        },
    )
    payload.update(
        source_input_identity=_RECORDED_SOURCE_INPUT_IDENTITY,
        runtime_bundle_identity=_RECORDED_RUNTIME_BUNDLE_IDENTITY,
        runner_identity=_RECORDED_RUNNER_IDENTITY,
        wsb_config_identity=_RECORDED_WSB_CONFIG_IDENTITY,
        acceptance_runner_structure=structure,
    )


def _safe_historical_report_bytes(
    *,
    run_id: str,
    command_ledger_identity: str,
    mismatch: str | None = None,
) -> bytes:
    platform_report = SandboxPlatformReport.available(
        protected_client_supported=True
    )
    platform_qualification = qualify_windows_sandbox_host(
        platform_report=platform_report,
        facts=_supported_platform_facts(),
        minimal_launch=_minimal_launch(),
    )
    host_harness = _reviewed_host_harness(
        implementation_base_commit_id="a" * 40,
        candidate_tree_hash="b" * 40,
    )
    static = _static_bindings().model_dump(mode="json")
    _bind_recorded_structure(
        static,
        command_ledger_identity=command_ledger_identity,
        host_harness=host_harness,
    )
    static["platform_qualification_identity"] = (
        platform_qualification.qualification_identity
    )
    guest = _passing_guest_result().model_dump(mode="json")
    _bind_recorded_structure(
        guest,
        command_ledger_identity=(
            TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
            if mismatch == "ledger"
            else command_ledger_identity
        ),
        host_harness=host_harness,
    )
    guest["platform_qualification_identity"] = (
        platform_qualification.qualification_identity
    )
    if mismatch == "nested_runner_binding":
        mismatched_runner = (
            "iso01-windows-sandbox-runner:v1:sha256:" + ("8" * 64)
        )
        static["runner_identity"] = mismatched_runner
        guest["runner_identity"] = mismatched_runner
    guest_structure = dict(guest["acceptance_runner_structure"])
    if mismatch == "structure_identity":
        guest_structure["qualification_identity"] = (
            "ticket06-acceptance-runner-structure:v2:sha256:" + ("0" * 64)
        )
        guest["acceptance_runner_structure"] = guest_structure
    elif mismatch == "static_binding":
        guest["source_input_identity"] = (
            "windows-sandbox-reviewed-source:v1:sha256:" + ("9" * 64)
        )
    guest["result_identity"] = _test_identity(
        "windows-sandbox-boundary-qualification-result:v1",
        {
            key: value
            for key, value in guest.items()
            if key != "result_identity"
        },
    )
    if mismatch == "result_identity":
        guest["result_identity"] = (
            "windows-sandbox-boundary-qualification-result:v1:sha256:"
            + ("0" * 64)
        )

    static_structure = dict(static["acceptance_runner_structure"])
    payload: dict[str, object] = {
        "contract_version": "iso03-host-qualification-report-v1",
        "status": "fail",
        "run_id": run_id,
        "platform_report": platform_report.model_dump(mode="json"),
        "platform_qualification": platform_qualification.model_dump(mode="json"),
        "static_bindings": static,
        "runner_input_identity": static_structure["runner_input_identity"],
        "implementation_base_commit_id": (
            host_harness.implementation_base_commit_id
        ),
        "candidate_tree_hash": host_harness.candidate_tree_hash,
        "host_harness": host_harness.model_dump(mode="json"),
        "protected_writer_quiescence": None,
        "actual_sandbox_launch": False,
        "sandbox_returncode": None,
        "sandbox_timed_out": False,
        "sandbox_termination_confirmed": False,
        "sandbox_process_ids": [],
        "sandbox_process_before": [],
        "sandbox_process_observed": [],
        "sandbox_process_after": [],
        "guest_result": guest,
        "guest_export_identity": None,
        "host_before_inventory_identity": static[
            "host_before_inventory_identity"
        ],
        "host_after_inventory_identity": None,
        "host_delta": None,
        "semantic_before": None,
        "semantic_after": None,
        "failure_reason": "safe-historical-fixture",
        "ticket06_executed": False,
        "ticket07_executed": False,
        "provider_model_io": False,
        "calendar_refresh_executed": False,
        "baseline_frozen": False,
        "qualified_v1_enabled": False,
    }
    payload["report_identity"] = _test_identity(
        "iso03-host-qualification-report:v1",
        payload,
    )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _write_historical_report_pin(
    root: Path,
    *,
    role: ProtectedRootRole,
    run_id: str,
    raw: bytes,
    recorded_contract: HistoricalISO03ReportContract,
) -> HistoricalQualificationReportPin:
    marker = root / run_id / "host-audit" / "iso03-report.json"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(raw)
    payload = json.loads(raw)
    return HistoricalQualificationReportPin(
        role=role,
        run_id=run_id,
        relative_path=f"{run_id}/host-audit/iso03-report.json",
        byte_length=len(raw),
        sha256=sha256(raw).hexdigest(),
        report_identity=payload["report_identity"],
        current_iso03_schema=True,
        recorded_contract=recorded_contract,
    )


def _write_export(root: Path, result: GuestQualificationResult) -> None:
    root.mkdir()
    result_bytes = result.canonical_json_bytes() + b"\n"
    (root / "result.json").write_bytes(result_bytes)
    marker = build_qualification_completion_marker(
        result=result,
        result_bytes=result_bytes,
    )
    (root / "completion.json").write_bytes(marker.canonical_json_bytes() + b"\n")


def test_iso_03_bounded_result_validator_accepts_only_complete_pass(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    result = _passing_guest_result()
    _write_export(export, result)

    validated = validate_boundary_qualification_export(
        export,
        expected=_static_bindings(),
    )

    assert validated.result == result
    assert validated.host_qcf17_authority is False


@pytest.mark.parametrize("tamper", ("missing-marker", "extra-file", "malformed"))
def test_iso_03_bounded_result_validator_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    export = tmp_path / tamper
    _write_export(export, _passing_guest_result())
    if tamper == "missing-marker":
        (export / "completion.json").unlink()
    elif tamper == "extra-file":
        (export / "runtime.sqlite3").write_bytes(b"unsafe")
    else:
        (export / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SandboxContractError):
        validate_boundary_qualification_export(export, expected=_static_bindings())


def _supported_platform_facts() -> WindowsHostPlatformFacts:
    return WindowsHostPlatformFacts(
        edition_id="Professional",
        product_name="Microsoft Windows 11 Pro",
        build_number=26100,
        architecture="AMD64",
        hypervisor_present=True,
        virtualization_firmware_enabled=True,
        logical_processor_count=16,
        physical_memory_bytes=32 * 1024**3,
        system_drive_free_bytes=100 * 1024**3,
        host_is_virtual_machine=False,
        nested_virtualization_ready=False,
        systeminfo_identity="windows-systeminfo:sha256:" + ("3" * 64),
    )


def _successful_process_topology() -> tuple[SandboxProcessTopologyEntry, ...]:
    return (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=101,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=102,
        ),
    )


def _minimal_launch(*, succeeded: bool = True) -> MinimalSandboxLaunchEvidence:
    return MinimalSandboxLaunchEvidence.create(
        succeeded=succeeded,
        returncode=0 if succeeded else 1,
        timed_out=False,
        config_identity="windows-sandbox-minimal-config:v1:sha256:" + ("4" * 64),
        marker_sha256="5" * 64 if succeeded else None,
        launched_processes=_successful_process_topology() if succeeded else (),
    )


def test_iso_03_minimal_launch_contract_is_complete_deterministic_and_immutable(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"

    first = build_minimal_sandbox_launch_contract(
        evidence_root=evidence_root,
        run_id="iso03min",
    )
    second = build_minimal_sandbox_launch_contract(
        evidence_root=evidence_root,
        run_id="iso03min",
    )

    assert isinstance(first, MinimalSandboxLaunchContract)
    assert first == second
    assert first.run_root == evidence_root / "minimal-iso03min"
    assert first.results_export_path == first.run_root / "results-export"
    assert first.config_path == first.run_root / "minimal.wsb"
    assert first.marker_path == first.results_export_path / "minimal-launch.json"
    assert first.marker_bytes == (
        b'{"contract_version":"windows-sandbox-minimal-launch-marker-v1",'
        b'"run_id":"iso03min","status":"pass"}'
    )
    assert first.marker_sha256 == sha256(first.marker_bytes).hexdigest()
    assert len(first.mappings) == 1
    assert first.mappings[0].role == SandboxMappingRole.RESULTS_EXPORT
    assert first.mappings[0].host_path == first.results_export_path
    assert first.mappings[0].guest_path == r"C:\QCF17\Minimal"
    assert first.mappings[0].read_only is False
    assert first.mappings[0].fresh_empty is True
    assert first.mapping_bytes == second.mapping_bytes
    assert first.mapping_identity == second.mapping_identity
    assert first.wsb_bytes == second.wsb_bytes
    assert first.config_identity == second.config_identity
    assert first.contract_identity == second.contract_identity
    with pytest.raises(ValueError, match="frozen"):
        first.run_id = "iso03other"


def test_iso_03_minimal_launch_pass_requires_observed_launcher_image() -> None:
    without_launcher = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=102,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxServer.exe",
            process_id=103,
        ),
    )

    with pytest.raises(ValueError, match="closed process topology"):
        MinimalSandboxLaunchEvidence.create(
            succeeded=True,
            returncode=0,
            timed_out=False,
            config_identity=(
                "windows-sandbox-minimal-config:v1:sha256:" + ("4" * 64)
            ),
            marker_sha256="5" * 64,
            launched_processes=without_launcher,
        )


def test_iso_03_platform_qualification_requires_all_real_host_signals() -> None:
    qualification = qualify_windows_sandbox_host(
        platform_report=SandboxPlatformReport.available(protected_client_supported=True),
        facts=_supported_platform_facts(),
        minimal_launch=_minimal_launch(),
    )

    assert qualification.state is WindowsHostQualificationState.QUALIFIED
    assert qualification.supported_edition is True
    assert qualification.hypervisor_ready is True
    assert qualification.protected_client_ready is True
    assert qualification.minimal_launch_succeeded is True


@pytest.mark.parametrize(
    "facts",
    (
        _supported_platform_facts().model_copy(update={"edition_id": "Core"}),
        _supported_platform_facts().model_copy(update={"hypervisor_present": False}),
        _supported_platform_facts().model_copy(update={"virtualization_firmware_enabled": False}),
    ),
)
def test_iso_03_platform_qualification_does_not_infer_support_from_feature_flag(
    facts: WindowsHostPlatformFacts,
) -> None:
    qualification = qualify_windows_sandbox_host(
        platform_report=SandboxPlatformReport.available(protected_client_supported=True),
        facts=facts,
        minimal_launch=_minimal_launch(),
    )

    assert qualification.state is WindowsHostQualificationState.NOT_QUALIFIED


def test_iso_03_platform_fact_capture_uses_bounded_cim_and_systeminfo(
    tmp_path: Path,
) -> None:
    powershell = tmp_path / "powershell.exe"
    systeminfo = tmp_path / "systeminfo.exe"
    powershell.write_bytes(b"placeholder")
    systeminfo.write_bytes(b"placeholder")
    facts_payload = _supported_platform_facts().model_dump(
        mode="json", exclude={"systeminfo_identity"}
    )
    commands: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], timeout_seconds: int
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert timeout_seconds == 30
        output = (
            json.dumps(facts_payload).encode("utf-8")
            if Path(command[0]) == powershell
            else b"bounded-systeminfo-output"
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    facts = capture_windows_host_platform_facts(
        command_runner=run,
        powershell_path=powershell,
        systeminfo_path=systeminfo,
    )

    assert facts.edition_id == "Professional"
    assert facts.hypervisor_present is True
    assert facts.systeminfo_identity.startswith("windows-systeminfo:v1:sha256:")
    assert len(commands) == 2
    assert "Get-CimInstance" in commands[0][-1]


def test_iso_03_execute_returns_needs_info_when_host_fact_capture_fails(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=runs_root)

    def unavailable_facts() -> WindowsHostPlatformFacts:
        raise SandboxContractError(
            reason=SandboxFailureReason.PLATFORM_UNAVAILABLE,
        )

    report = execute_iso03_boundary_qualification(
        implementation_repository_root=tmp_path / "not-used-implementation",
        reviewed_source_repository_root=tmp_path / "not-used-source",
        protected_state_repository_root=tmp_path / "not-used-protected-state",
        historical_qualification_runs_root=tmp_path / "not-used-history",
        replacement_qualification_run_root=(
            tmp_path / "not-used-replacement"
        ),
        qualified_replacement_qualification_root=(
            tmp_path / "not-used-qualified-replacement"
        ),
        failed_iso04_ticket06_acceptance_root=(
            tmp_path / "not-used-failed-iso04"
        ),
        review_invalidated_qualification_root=(
            tmp_path / "not-used-review-invalidated"
        ),
        runs_root=runs_root,
        runtime_bundle_root=tmp_path / "not-used-runtime",
        run_id="iso03facts",
        user_profile=tmp_path / "not-used-profile",
        host_temp=tmp_path / "not-used-temp",
        host_venv=tmp_path / "not-used-venv",
        host_entrypoint_path=tmp_path / "not-used-entrypoint.py",
        protected_writers_quiesced=True,
        platform=platform,
        host_fact_capture=unavailable_facts,
    )

    assert report.status is ISO03QualificationStatus.NEEDS_INFO
    assert report.actual_sandbox_launch is False
    assert report.platform_report.diagnostic_code == "host-platform-facts-unavailable"


def test_iso_03_platform_fails_closed_before_launch_when_process_inventory_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: None)

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("process launch must not occur without process inventory")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.termination_confirmed is False


def test_iso_03_platform_fails_closed_before_launch_when_sandbox_is_already_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"
    preexisting = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=900,
        ),
    )
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: preexisting)

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("process launch must not occur while Sandbox is already active")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.launched_process_ids == ()
    assert result.before_processes == preexisting
    assert result.termination_confirmed is False


def test_iso_03_platform_inventories_store_sandbox_and_utility_vm_process_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasklist = b"\r\n".join(
        (
            b'"WindowsSandbox.exe","101","Console","1","1,000 K"',
            b'"WindowsSandboxClient.exe","102","Console","1","1,000 K"',
            b'"WindowsSandboxServer.exe","103","Services","0","1,000 K"',
            b'"WindowsSandboxRemoteSession.exe","104","Console","1","1,000 K"',
            b'"vmmemWindowsSandbox","105","Services","0","1,000 K"',
            b'"unrelated.exe","999","Console","1","1,000 K"',
        )
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=tasklist,
        ),
    )

    topology = WindowsSandboxPlatform._sandbox_process_topology()

    assert topology is not None
    assert {(item.image_name, item.process_id) for item in topology} == {
        ("WindowsSandbox.exe", 101),
        ("WindowsSandboxClient.exe", 102),
        ("WindowsSandboxServer.exe", 103),
        ("WindowsSandboxRemoteSession.exe", 104),
        ("VmmemWindowsSandbox", 105),
    }


@pytest.mark.parametrize(
    "tasklist",
    (
        b'"WindowsSandbox.exe","not-a-pid","Console","1","1,000 K"',
        b'"WindowsSandboxClient.exe"',
        b'"WindowsSandbox.exe","0","Console","1","1,000 K"',
    ),
)
def test_iso_03_platform_rejects_malformed_relevant_process_rows(
    monkeypatch: pytest.MonkeyPatch,
    tasklist: bytes,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=tasklist,
        ),
    )

    assert WindowsSandboxPlatform._sandbox_process_topology() is None


def test_iso_03_platform_timeout_terminates_and_confirms_new_sandbox_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"

    class FakeProcess:
        pid = 150
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 15
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -1

    launched = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=150,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=200,
        ),
    )
    process_sets = iter(((), launched, ()))
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: next(process_sets))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    terminated: list[int] = []
    monkeypatch.setattr(platform, "_terminate_process_tree", terminated.append)

    result = platform.launch(config, timeout_seconds=0)

    assert result.timed_out is True
    assert result.termination_confirmed is True
    assert result.launched_process_ids == (150, 200)
    assert result.launched_processes == launched
    assert result.after_processes == ()
    assert terminated == [150]


def test_iso_03_platform_concurrent_launcher_is_recorded_but_never_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"

    class FakeProcess:
        pid = 150
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 15
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -1

    concurrent = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=150,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=200,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=151,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=201,
        ),
    )
    process_sets = iter(((), concurrent, ()))
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: next(process_sets))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    terminated: list[int] = []
    monkeypatch.setattr(platform, "_terminate_process_tree", terminated.append)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.termination_confirmed is True
    assert set(result.launched_processes) == set(concurrent)
    assert result.after_processes == ()
    assert terminated == [150]
    assert 151 not in terminated


def test_iso_03_protected_state_root_is_independent_of_runner_and_source_repositories(
    tmp_path: Path,
) -> None:
    implementation = tmp_path / "implementation"
    reviewed_source = tmp_path / "reviewed-source"
    protected_state = tmp_path / "actual-protected-state"
    historical_runs = tmp_path / "historical-sandbox-runs"
    replacement_run = tmp_path / "replacement-sandbox-run"
    qualified_replacement = tmp_path / "qualified-replacement-sandbox-run"
    failed_iso04_acceptance = tmp_path / "failed-iso04-ticket06-acceptance"
    review_invalidated = tmp_path / "review-invalidated-sandbox-run"
    user_profile = tmp_path / "profile"
    for root in (
        implementation,
        reviewed_source,
        protected_state,
        historical_runs,
        replacement_run,
        qualified_replacement,
        failed_iso04_acceptance,
        review_invalidated,
        user_profile,
    ):
        root.mkdir()

    bindings = default_protected_root_bindings(
        user_profile=user_profile,
        repository_root=protected_state,
        historical_qualification_runs_root=historical_runs,
        replacement_qualification_run_root=replacement_run,
        qualified_replacement_qualification_root=qualified_replacement,
        failed_iso04_ticket06_acceptance_root=failed_iso04_acceptance,
        review_invalidated_qualification_root=review_invalidated,
    )
    by_role = {binding.role: binding.path for binding in bindings}

    assert by_role[ProtectedRootRole.CHECKPOINTS] == (
        protected_state / "data" / "cache" / "checkpoints"
    )
    assert by_role[ProtectedRootRole.REPORTS_LOGS] == protected_state / "logs"
    assert by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(protected_state)
    assert by_role[ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS] == historical_runs
    assert (
        by_role[ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION]
        == replacement_run
    )
    assert (
        by_role[ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION]
        == qualified_replacement
    )
    assert (
        by_role[ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE]
        == failed_iso04_acceptance
    )
    assert (
        by_role[ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION]
        == review_invalidated
    )
    assert not by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(
        implementation
    )
    assert not by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(
        reviewed_source
    )


@pytest.mark.parametrize(
    ("first_role", "second_role"),
    tuple(combinations(_HISTORICAL_EVIDENCE_ROLES, 2)),
)
@pytest.mark.parametrize("overlap_kind", ("equal", "nested"))
def test_iso_03_historical_evidence_roots_cannot_overlap(
    tmp_path: Path,
    first_role: ProtectedRootRole,
    second_role: ProtectedRootRole,
    overlap_kind: str,
) -> None:
    shared = tmp_path / "shared-qualified-evidence"
    shared.mkdir()
    second_path = shared
    if overlap_kind == "nested":
        second_path = shared / "nested"
        second_path.mkdir()
    complete = _protected_manifest(tmp_path / "protected")
    replacement_paths = {
        first_role: shared,
        second_role: second_path,
    }
    manifest = build_protected_root_manifest(
        tuple(
            ProtectedRootBinding(
                role=binding.role,
                path=replacement_paths.get(binding.role, binding.path),
                object_kind=binding.object_kind,
            )
            for binding in complete.bindings
        )
    )

    with pytest.raises(SandboxContractError) as captured:
        validate_iso03_historical_evidence_root_coverage(manifest)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_MANIFEST_OVERLAP
    )


def test_iso_03_historical_evidence_roots_require_identity_bound_reports(
    tmp_path: Path,
) -> None:
    manifest, pins = _historical_manifest_with_markers(tmp_path / "protected")

    _validate_historical_report_pins(manifest, pins)


def test_iso_03_historical_current_schema_report_uses_recorded_prior_ledger(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    historical_root = next(
        binding.path
        for binding in manifest.bindings
        if binding.role
        is ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION
    )
    raw = _safe_historical_report_bytes(
        run_id="iso03-prior-ledger",
        command_ledger_identity=_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    )
    before_sha256 = sha256(raw).hexdigest()
    pin = _write_historical_report_pin(
        historical_root,
        role=ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
        run_id="iso03-prior-ledger",
        raw=raw,
        recorded_contract=_prior_historical_report_contract(),
    )

    assert (
        _PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY
        != TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
    )
    _validate_historical_report_pins(manifest, (pin,))
    report = load_historical_iso03_qualification_report(
        raw,
        recorded_contract=_prior_historical_report_contract(),
    )

    assert report.static_bindings is not None
    assert report.guest_result is not None
    assert (
        report.static_bindings.acceptance_runner_structure.command_ledger_identity
        == _PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY
    )
    assert report.static_bindings.source_input_identity == (
        _RECORDED_SOURCE_INPUT_IDENTITY
    )
    assert report.static_bindings.runtime_bundle_identity == (
        _RECORDED_RUNTIME_BUNDLE_IDENTITY
    )
    assert report.static_bindings.runner_identity == _RECORDED_RUNNER_IDENTITY
    assert report.static_bindings.wsb_config_identity == (
        _RECORDED_WSB_CONFIG_IDENTITY
    )
    marker = historical_root / pin.relative_path
    assert marker.read_bytes() == raw
    assert sha256(marker.read_bytes()).hexdigest() == before_sha256


def test_iso_03_historical_reader_rejects_tampered_ledger_identity(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    historical_root = next(
        binding.path
        for binding in manifest.bindings
        if binding.role
        is ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION
    )
    raw = _safe_historical_report_bytes(
        run_id="iso03-tampered-ledger",
        command_ledger_identity=(
            "ticket06-sandbox-command-ledger:v1:sha256:" + ("5" * 64)
        ),
    )
    pin = _write_historical_report_pin(
        historical_root,
        role=ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
        run_id="iso03-tampered-ledger",
        raw=raw,
        recorded_contract=_prior_historical_report_contract(),
    )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins(manifest, (pin,))

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


@pytest.mark.parametrize(
    "mismatch",
    (
        "ledger",
        "structure_identity",
        "result_identity",
        "static_binding",
        "nested_runner_binding",
    ),
)
def test_iso_03_historical_reader_rejects_internal_identity_disagreement(
    tmp_path: Path,
    mismatch: str,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    historical_root = next(
        binding.path
        for binding in manifest.bindings
        if binding.role
        is ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION
    )
    run_id = f"iso03-mismatch-{mismatch}"
    raw = _safe_historical_report_bytes(
        run_id=run_id,
        command_ledger_identity=_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY,
        mismatch=mismatch,
    )
    pin = _write_historical_report_pin(
        historical_root,
        role=ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
        run_id=run_id,
        raw=raw,
        recorded_contract=_prior_historical_report_contract(),
    )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins(manifest, (pin,))

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


def test_iso_03_historical_reader_rejects_unsupported_report_contract() -> None:
    raw = _safe_historical_report_bytes(
        run_id="iso03-unsupported-version",
        command_ledger_identity=_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    )
    unsupported = _prior_historical_report_contract().model_copy(
        update={"report_contract_version": "iso03-host-qualification-report-v2"}
    )

    with pytest.raises(SandboxContractError) as captured:
        load_historical_iso03_qualification_report(
            raw,
            recorded_contract=unsupported,
        )

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


@pytest.mark.parametrize(
    ("command_ledger_identity", "valid"),
    (
        (_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY, False),
        (TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY, True),
    ),
)
def test_iso_03_current_structural_candidate_requires_current_ledger(
    command_ledger_identity: str,
    valid: bool,
) -> None:
    current = _static_bindings().acceptance_runner_structure
    values = {
        "runner_input_identity": current.runner_input_identity,
        "environment_policy_identity": current.environment_policy_identity,
        "host_harness_manifest_identity": current.host_harness_manifest_identity,
        "host_acceptance_module_sha256": current.host_acceptance_module_sha256,
        "boundary_runner_identity": current.boundary_runner_identity,
        "wsb_config_identity": current.wsb_config_identity,
        "wsb_config_policy_identity": current.wsb_config_policy_identity,
        "mapping_manifest_identity": current.mapping_manifest_identity,
        "command_ledger_identity": command_ledger_identity,
        "result_schema_identity": current.result_schema_identity,
    }

    if valid:
        candidate = Ticket06AcceptanceStructuralQualification.create(**values)
        assert (
            candidate.command_ledger_identity
            == TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
        )
    else:
        with pytest.raises(
            ValueError,
            match="qualified Ticket 06 command ledger identity mismatch",
        ):
            Ticket06AcceptanceStructuralQualification.create(**values)


def test_iso_03_historical_inventory_supports_multiple_ledger_generations(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    by_role = {binding.role: binding.path for binding in manifest.bindings}
    old_run_id = "iso03-old-generation"
    current_run_id = "iso03-current-generation"
    pins = (
        _write_historical_report_pin(
            by_role[
                ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION
            ],
            role=(
                ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION
            ),
            run_id=old_run_id,
            raw=_safe_historical_report_bytes(
                run_id=old_run_id,
                command_ledger_identity=_PRIOR_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            ),
            recorded_contract=_prior_historical_report_contract(),
        ),
        _write_historical_report_pin(
            by_role[
                ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION
            ],
            role=ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
            run_id=current_run_id,
            raw=_safe_historical_report_bytes(
                run_id=current_run_id,
                command_ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            ),
            recorded_contract=_current_historical_report_contract(),
        ),
    )

    _validate_historical_report_pins(manifest, pins)
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: {},
        immutable_verifier_identity="test-verifier",
    )
    _validate_historical_report_pins_in_inventory(sentinel, pins)


def test_iso_03_current_report_markers_require_the_closed_report_schema(
    tmp_path: Path,
) -> None:
    manifest, pins = _historical_manifest_with_markers(tmp_path / "protected")
    current_schema_pins = (
        *pins[:-1],
        replace(pins[-1], current_iso03_schema=True),
    )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins(manifest, current_schema_pins)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


def test_iso_03_sentinel_binds_the_same_historical_report_pins(
    tmp_path: Path,
) -> None:
    manifest, pins = _historical_manifest_with_markers(tmp_path / "protected")
    _validate_historical_report_pins(manifest, pins)
    marker = (
        next(
            binding.path
            for binding in manifest.bindings
            if binding.role
            is ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION
        )
        / pins[-1].relative_path
    )
    marker.write_bytes(b"review-invalidated-marker-replaced-before-sentinel")
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: {},
        immutable_verifier_identity="test-verifier",
    )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins_in_inventory(sentinel, pins)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


def test_iso_03_sentinel_rejects_an_extra_historical_report_marker(
    tmp_path: Path,
) -> None:
    manifest, pins = _historical_manifest_with_markers(tmp_path / "protected")
    _validate_historical_report_pins(manifest, pins)
    review_root = next(
        binding.path
        for binding in manifest.bindings
        if binding.role
        is ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION
    )
    _write_report_marker(review_root, run_id="iso03-20260816-020")
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: {},
        immutable_verifier_identity="test-verifier",
    )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins_in_inventory(sentinel, pins)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


@pytest.mark.parametrize("tamper", ("empty", "wrong_identity"))
def test_iso_03_historical_evidence_roots_reject_wrong_or_empty_markers(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest, pins = _historical_manifest_with_markers(tmp_path / "protected")
    review_root = next(
        binding.path
        for binding in manifest.bindings
        if binding.role
        is ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION
    )
    marker = review_root / "iso03-20260816-019" / "host-audit" / "iso03-report.json"
    if tamper == "empty":
        marker.unlink()
        (review_root / "retained.bin").unlink()
    else:
        payload = json.loads(marker.read_bytes())
        payload["report_identity"] = "iso03-host-qualification-report:v1:sha256:" + (
            "0" * 64
        )
        marker.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    with pytest.raises(SandboxContractError) as captured:
        _validate_historical_report_pins(manifest, pins)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )


@pytest.mark.parametrize(
    "missing_name",
    (
        "historical_qualification_runs_root",
        "replacement_qualification_run_root",
        "qualified_replacement_qualification_root",
        "failed_iso04_ticket06_acceptance_root",
        "review_invalidated_qualification_root",
    ),
)
def test_iso_03_programmatic_execution_requires_all_historical_evidence_roots(
    tmp_path: Path,
    missing_name: str,
) -> None:
    arguments = {
        "implementation_repository_root": tmp_path / "implementation",
        "reviewed_source_repository_root": tmp_path / "reviewed-source",
        "protected_state_repository_root": tmp_path / "protected-state",
        "historical_qualification_runs_root": tmp_path / "historical",
        "replacement_qualification_run_root": tmp_path / "replacement",
        "qualified_replacement_qualification_root": tmp_path / "qualified",
        "failed_iso04_ticket06_acceptance_root": tmp_path / "failed-iso04",
        "review_invalidated_qualification_root": tmp_path / "review-invalidated",
        "runs_root": tmp_path / "runs",
        "runtime_bundle_root": tmp_path / "runtime",
        "run_id": "iso03-required-roots",
        "user_profile": tmp_path / "profile",
        "host_temp": tmp_path / "temp",
        "host_venv": tmp_path / "venv",
        "host_entrypoint_path": tmp_path / "entrypoint.py",
    }
    arguments.pop(missing_name)

    with pytest.raises(TypeError, match=missing_name):
        execute_iso03_boundary_qualification(**arguments)


class _MinimalLaunchPlatform:
    def __init__(
        self,
        *,
        evidence_root: Path,
        write_marker: bool = True,
        termination_confirmed: bool = True,
    ) -> None:
        self.evidence_root = evidence_root
        self.write_marker = write_marker
        self.termination_confirmed = termination_confirmed
        self.config_xml = ""
        self.launch_calls = 0

    def probe(self) -> SandboxPlatformReport:
        return SandboxPlatformReport.available(protected_client_supported=True)

    def launch(
        self, config_path: Path, *, timeout_seconds: int
    ) -> SandboxProcessResult:
        self.launch_calls += 1
        assert timeout_seconds == 30
        self.config_xml = config_path.read_text(encoding="utf-8")
        if self.write_marker:
            marker = {
                "contract_version": "windows-sandbox-minimal-launch-marker-v1",
                "run_id": "iso03min",
                "status": "pass",
            }
            path = (
                self.evidence_root
                / "minimal-iso03min"
                / "results-export"
                / "minimal-launch.json"
            )
            path.write_text(
                json.dumps(marker, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
        return SandboxProcessResult(
            returncode=0,
            timed_out=False,
            launched_processes=_successful_process_topology(),
            termination_confirmed=self.termination_confirmed,
        )


def test_iso_03_minimal_launch_construction_materializes_without_launch(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=evidence_root)

    contract = construct_minimal_sandbox_launch(
        evidence_root=evidence_root,
        run_id="iso03min",
    )

    assert isinstance(contract, MinimalSandboxLaunchContract)
    assert platform.launch_calls == 0
    assert contract.run_root.is_dir()
    assert contract.results_export_path.is_dir()
    assert tuple(contract.results_export_path.iterdir()) == ()
    assert contract.config_path.read_bytes() == contract.wsb_bytes
    assert contract.mapping_identity.startswith(
        "windows-sandbox-minimal-mapping:v1:sha256:"
    )
    assert contract.config_identity.startswith(
        "windows-sandbox-minimal-config:v1:sha256:"
    )


def test_iso_03_separately_constructed_minimal_contract_launches(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=evidence_root)
    contract = construct_minimal_sandbox_launch(
        evidence_root=evidence_root,
        run_id="iso03min",
    )

    result = launch_minimal_sandbox(
        contract=contract,
        platform=platform,
        timeout_seconds=30,
    )

    assert platform.launch_calls == 1
    assert result.succeeded is True
    assert result.config_identity == contract.config_identity
    assert result.marker_sha256 == contract.marker_sha256
    assert result.launched_processes == _successful_process_topology()


@pytest.mark.parametrize(
    "tamper",
    (
        "contract_version",
        "contract_wsb_bytes",
        "mapping_identity",
        "mapping_topology",
        "contract_identity",
        "results_export_path",
    ),
)
def test_iso_03_minimal_launch_rejects_tampered_contract_before_launch(
    tmp_path: Path,
    tamper: str,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=evidence_root)
    contract = construct_minimal_sandbox_launch(
        evidence_root=evidence_root,
        run_id="iso03min",
    )
    if tamper == "contract_version":
        contract = contract.model_copy(update={"contract_version": "tampered"})
    elif tamper == "contract_wsb_bytes":
        contract = contract.model_copy(update={"wsb_bytes": contract.wsb_bytes + b" "})
    elif tamper == "mapping_identity":
        contract = contract.model_copy(
            update={
                "mapping_identity": (
                    "windows-sandbox-minimal-mapping:v1:sha256:" + ("0" * 64)
                )
            }
        )
    elif tamper == "mapping_topology":
        contract = contract.model_copy(update={"mappings": ()})
    elif tamper == "contract_identity":
        contract = contract.model_copy(
            update={
                "contract_identity": (
                    "windows-sandbox-minimal-launch-contract:v1:sha256:"
                    + ("0" * 64)
                )
            }
        )
    else:
        contract = contract.model_copy(
            update={"results_export_path": contract.run_root / "other-export"}
        )

    with pytest.raises(SandboxContractError):
        launch_minimal_sandbox(
            contract=contract,
            platform=platform,
            timeout_seconds=30,
        )

    assert platform.launch_calls == 0


@pytest.mark.parametrize("tamper", ("config_bytes", "nonempty_export"))
def test_iso_03_minimal_launch_rejects_tampered_artifacts_before_launch(
    tmp_path: Path,
    tamper: str,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=evidence_root)
    contract = construct_minimal_sandbox_launch(
        evidence_root=evidence_root,
        run_id="iso03min",
    )
    if tamper == "config_bytes":
        contract.config_path.write_bytes(contract.wsb_bytes + b" ")
    else:
        (contract.results_export_path / "unexpected.txt").write_bytes(b"unexpected")

    with pytest.raises(SandboxContractError):
        launch_minimal_sandbox(
            contract=contract,
            platform=platform,
            timeout_seconds=30,
        )

    assert platform.launch_calls == 0


def test_iso_03_fatal_preconstruction_failure_writes_closed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    protected_state = tmp_path / "protected-state"
    protected_state.mkdir()
    protected = _protected_manifest(tmp_path / "protected")
    implementation_commit = "a" * 40
    candidate_tree = "b" * 40
    host_harness = _reviewed_host_harness(
        implementation_base_commit_id=implementation_commit,
        candidate_tree_hash=candidate_tree,
    )

    monkeypatch.setattr(
        qualification_module,
        "_committed_git_hex",
        lambda *_args: implementation_commit,
    )
    monkeypatch.setattr(
        qualification_module,
        "_reviewed_index_tree_hash",
        lambda *_args: candidate_tree,
    )
    monkeypatch.setattr(
        qualification_module,
        "build_reviewed_host_harness_manifest",
        lambda *_args, **_kwargs: host_harness,
    )
    monkeypatch.setattr(
        qualification_module,
        "build_protected_root_manifest",
        lambda _bindings: protected,
    )

    def fail_historical_evidence(_manifest: object) -> None:
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
        )

    def forbidden_host_before(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("HOST BEFORE must not run after historical failure")

    def forbidden_construction(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("boundary construction must not run")

    monkeypatch.setattr(
        qualification_module,
        "validate_iso03_historical_evidence_root_coverage",
        fail_historical_evidence,
    )
    monkeypatch.setattr(
        qualification_module,
        "capture_protected_host_inventory",
        forbidden_host_before,
    )
    monkeypatch.setattr(
        qualification_module,
        "WindowsSandboxBoundaryQualificationRunnerService",
        forbidden_construction,
    )

    reviewed_source = tmp_path / "reviewed-source"
    runtime_bundle = tmp_path / "runtime-bundle"
    run_id = "iso03min"
    platform = _MinimalLaunchPlatform(evidence_root=runs_root)
    with pytest.raises(SandboxContractError) as captured:
        execute_iso03_boundary_qualification(
            implementation_repository_root=Path(__file__).resolve().parents[1],
            reviewed_source_repository_root=reviewed_source,
            protected_state_repository_root=protected_state,
            historical_qualification_runs_root=tmp_path / "history",
            replacement_qualification_run_root=tmp_path / "replacement",
            qualified_replacement_qualification_root=tmp_path / "qualified",
            failed_iso04_ticket06_acceptance_root=tmp_path / "failed-iso04",
            review_invalidated_qualification_root=tmp_path / "invalidated",
            runs_root=runs_root,
            runtime_bundle_root=runtime_bundle,
            run_id=run_id,
            user_profile=tmp_path / "profile",
            host_temp=tmp_path / "temp",
            host_venv=tmp_path / "venv",
            host_entrypoint_path=tmp_path / "entrypoint.py",
            minimal_timeout_seconds=30,
            protected_writers_quiesced=True,
            platform=platform,
            host_fact_capture=_supported_platform_facts,
        )

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )
    receipt_path = runs_root / (
        f"{ISO03_PRECONSTRUCTION_FAILURE_RECEIPT_PREFIX}{run_id}.json"
    )
    raw = receipt_path.read_bytes()
    receipt = ISO03PreconstructionFailureReceipt.model_validate_json(raw)
    assert raw == json.dumps(
        receipt.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    assert receipt.run_id == run_id
    assert receipt.stage == "historical_evidence_validation"
    assert (
        receipt.failure_reason
        is SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
    )
    assert receipt.reviewed_source_repository_path_identity == (
        host_path_identity(reviewed_source)
    )
    assert receipt.runtime_bundle_root_path_identity == host_path_identity(
        runtime_bundle
    )
    assert receipt.minimal_sandbox_launch_succeeded is True
    assert receipt.minimal_sandbox_process_ids == (101, 102)
    assert receipt.reviewed_source_input_identity is None
    assert receipt.runtime_bundle_identity is None
    assert receipt.runner_input_identity is None
    assert receipt.protected_writer_quiescence_contract_identity.startswith(
        "windows-sandbox-protected-writer-quiescence:v1:sha256:"
    )
    assert receipt.host_before_inventory_identity is None
    assert receipt.host_before_inventory_materialized is False
    assert receipt.boundary_sandbox_constructed is False
    assert receipt.boundary_sandbox_launched is False
    assert not (runs_root / run_id).exists()


@pytest.mark.parametrize("write_marker", (True, False))
def test_iso_03_minimal_launch_requires_exact_guest_marker(
    tmp_path: Path,
    write_marker: bool,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    platform = _MinimalLaunchPlatform(
        evidence_root=evidence,
        write_marker=write_marker,
    )

    result = run_minimal_sandbox_launch(
        platform=platform,
        evidence_root=evidence,
        run_id="iso03min",
        timeout_seconds=30,
    )

    assert result.succeeded is write_marker
    assert platform.launch_calls == 1
    assert "<Networking>Disable</Networking>" in platform.config_xml
    assert "<ProtectedClient>Enable</ProtectedClient>" in platform.config_xml
    assert platform.config_xml.count("<MappedFolder>") == 1


def test_iso_03_minimal_launch_rejects_unconfirmed_sandbox_termination(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    platform = _MinimalLaunchPlatform(
        evidence_root=evidence,
        termination_confirmed=False,
    )

    result = run_minimal_sandbox_launch(
        platform=platform,
        evidence_root=evidence,
        run_id="iso03min",
        timeout_seconds=30,
    )

    assert result.succeeded is False
    assert result.termination_confirmed is False


def test_iso_03_boundary_runner_input_is_closed_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_repository = Path(__file__).resolve().parents[1]
    repository = tmp_path / "repository"
    runner_sources = repository / "scripts" / "windows_sandbox"
    runner_sources.mkdir(parents=True)
    for relative_path, _target_name in QUALIFIED_RUNNER_SOURCE_FILES:
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_repository / relative_path).read_bytes())
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "-c", "core.autocrlf=false", "add", "."),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=ISO03 Test",
            "-c",
            "user.email=iso03@example.invalid",
            "commit",
            "-qm",
            "runner fixture",
        ),
        check=True,
    )
    committed_bootstrap = (runner_sources / "bootstrap.ps1").read_bytes()
    (runner_sources / "bootstrap.ps1").write_bytes(b"dirty worktree must not enter\n")
    redirected = tmp_path / "redirected-git-directory"
    redirected.mkdir()
    subprocess.run(("git", "-C", str(redirected), "init", "-q"), check=True)
    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))
    staged = tmp_path / "runner"
    manifest = stage_boundary_runner_input(repository, staged)

    validate_boundary_runner_input(staged, expected=manifest)
    assert manifest.source_mode == "reviewed_index"
    assert (staged / "bootstrap.ps1").read_bytes() == committed_bootstrap
    assert tuple(item.relative_path for item in manifest.files) == (
        QUALIFIED_RUNNER_TARGET_FILES
    )
    bootstrap = (staged / "bootstrap.ps1").read_text(encoding="utf-8")
    assert '$property.Name -ne "workspace_repository"' in bootstrap

    layout = build_guest_layout()
    boundary_environment = build_environment_policy(
        run_live_environment=(
            "RUN_LIVE_CUSTOM_TESTS",
            "RUN_LIVE_DEEPSEEK_TESTS",
            "RUN_LIVE_ENVIRONMENT",
            "RUN_LIVE_PROVIDER_TESTS",
            "RUN_LIVE_TUSHARE_TESTS",
        ),
        layout=layout,
    )
    mapping_root = tmp_path / "mappings"
    guest_paths = {
        SandboxMappingRole.SOURCE: r"C:\QCF17\Input\Source",
        SandboxMappingRole.RUNNER: r"C:\QCF17\Input\Runner",
        SandboxMappingRole.CONTROL: r"C:\QCF17\Input\Control",
        SandboxMappingRole.RUNTIME_BUNDLE: r"C:\QCF17\Input\Runtime",
        SandboxMappingRole.RESULTS_EXPORT: r"C:\QCF17\Export",
    }
    mapping_bindings = []
    for index, role in enumerate(SandboxMappingRole, start=1):
        host_path = mapping_root / role.value
        host_path.mkdir(parents=True)
        mapping_bindings.append(
            SandboxMappingBinding(
                role=role,
                host_path=host_path,
                guest_path=guest_paths[role],
                read_only=role is not SandboxMappingRole.RESULTS_EXPORT,
                content_identity=(
                    f"iso03-test-{role.value}:v1:sha256:{index:064x}"
                ),
                fresh_empty=role is SandboxMappingRole.RESULTS_EXPORT,
            )
        )
    mapping_manifest = build_mapping_manifest(
        tuple(mapping_bindings),
        protected_manifest=_protected_manifest(tmp_path / "protected"),
        forbidden_host_roots=(),
    )
    configuration = render_wsb_configuration(
        mapping_manifest,
        protected_client_supported=True,
    )
    host_acceptance_module = (
        source_repository / "tradingagents" / "windows_sandbox_qualification.py"
    )
    host_acceptance_sha256 = sha256(host_acceptance_module.read_bytes()).hexdigest()
    structure = qualify_ticket06_acceptance_runner_structure(
        staged,
        expected_runner_identity=manifest.runner_input_identity,
        base_environment_variables=boundary_environment.variables,
        mapping_manifest=mapping_manifest,
        wsb_configuration=configuration,
        boundary_runner_identity=(
            "iso01-windows-sandbox-runner:v1:sha256:" + ("7" * 64)
        ),
        host_harness_manifest_identity=(
            "iso03-reviewed-host-harness:v1:sha256:" + ("8" * 64)
        ),
        host_acceptance_module_path=host_acceptance_module,
        expected_host_acceptance_module_sha256=host_acceptance_sha256,
    )
    assert structure.ticket06_executed is False
    assert structure.command_ledger_identity == TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
    assert structure.mandatory_command_count == 27
    assert structure.full_suite_a_entry_id == "full_suite_a"
    assert structure.full_suite_b_entry_id == "full_suite_b"
    assert structure.result_schema_validated is True
    assert structure.completion_marker_write_order == "last"
    assert structure.additional_host_mapping_required is False
    assert structure.network_permission_required is False
    assert structure.mapping_manifest_identity == (
        mapping_manifest.mapping_manifest_identity
    )
    assert structure.wsb_config_identity == configuration.config_identity
    assert structure.host_acceptance_module_sha256 == host_acceptance_sha256
    assert structure.offline_review_executable_closed is True
    assert structure.acceptance_orchestrator_write_order_validated is True
    assert structure.host_acceptance_mapping_reuse_validated is True
    assert structure.host_acceptance_export_validation_path_validated is True

    (staged / "run_boundary_qualification.py").write_text(
        "raise SystemExit('tampered')\n", encoding="utf-8"
    )
    with pytest.raises(SandboxContractError):
        validate_boundary_runner_input(staged, expected=manifest)


def test_iso_03_reviewed_host_harness_rejects_dirty_executing_module_bytes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "candidate"
    relative_paths = (
        "scripts/run_iso03_windows_sandbox_qualification.py",
        "tradingagents/offline_runtime_bundle.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
    )
    loaded_paths: dict[str, Path] = {}
    for relative_path in relative_paths:
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# committed {relative_path}\n", encoding="utf-8")
        loaded_paths[relative_path] = path
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "-c", "core.autocrlf=false", "add", "."),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=ISO03 Test",
            "-c",
            "user.email=iso03@example.invalid",
            "commit",
            "-qm",
            "host harness fixture",
        ),
        check=True,
    )
    entrypoint = loaded_paths[
        "scripts/run_iso03_windows_sandbox_qualification.py"
    ]

    manifest = build_reviewed_host_harness_manifest(
        repository,
        entrypoint_path=entrypoint,
        loaded_paths=loaded_paths,
    )

    assert manifest.implementation_base_commit_id
    assert manifest.candidate_tree_hash
    loaded_paths["tradingagents/windows_sandbox_qualification.py"].write_text(
        "# dirty host authority\n",
        encoding="utf-8",
    )
    with pytest.raises(SandboxContractError):
        build_reviewed_host_harness_manifest(
            repository,
            entrypoint_path=entrypoint,
            loaded_paths=loaded_paths,
        )


def test_iso_03_probe_plan_binds_python_i_and_sqlite_to_exact_protected_files(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    targets = select_adversarial_probe_targets(manifest, run_id="iso03plan")
    by_mechanism = {item.mechanism: item for item in targets}
    by_role = {item.role: item.path for item in manifest.bindings}

    assert tuple(item.mechanism for item in targets) == tuple(ProbeMechanism)
    assert Path(
        by_mechanism[ProbeMechanism.PYTHON_ISOLATED].target_host_path
    ) == by_role[ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE]
    assert Path(by_mechanism[ProbeMechanism.SQLITE].target_host_path) == by_role[
        ProtectedRootRole.PROVIDER_LEDGER
    ]


def test_iso_03_export_rejects_a_different_exact_path_probe_plan(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    result = _passing_guest_result()
    _write_export(export, result)
    expected_targets = tuple(
        AdversarialProbeTarget(
            mechanism=item.mechanism,
            target_host_path=item.target_host_path,
            target_path_identity=item.target_path_identity,
        )
        for item in result.probes
    )
    changed_path = r"C:\Users\56922\.tradingagents\different.probe"
    tampered = (
        AdversarialProbeTarget(
            mechanism=expected_targets[0].mechanism,
            target_host_path=changed_path,
            target_path_identity=host_path_identity(changed_path),
        ),
        *expected_targets[1:],
    )

    with pytest.raises(SandboxContractError):
        validate_boundary_qualification_export(
            export,
            expected=_static_bindings(),
            expected_probe_targets=tampered,
        )
