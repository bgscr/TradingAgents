from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingagents.dataflows.config import get_config as get_dataflows_config
from tradingagents.default_config import DEFAULT_CONFIG as IMPORTED_DEFAULT_CONFIG
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    ProviderRequestCoordinator,
    RequestPriority,
)

SHALLOW_IMPORTED_DEFAULT_CONFIG = copy.copy(IMPORTED_DEFAULT_CONFIG)
DEEP_IMPORTED_DEFAULT_CONFIG = copy.deepcopy(IMPORTED_DEFAULT_CONFIG)
COLLECTION_TIME_DATAFLOWS_CONFIG = get_dataflows_config()


def _multiprocessing_write_probe(
    path: str,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        Path(path).write_text("must-not-write", encoding="utf-8")
    except RuntimeError as exc:
        result_queue.put(str(exc))
    else:
        result_queue.put("write-succeeded")


def _default_history_config() -> MarketHistoryConfig:
    return MarketHistoryConfig.from_mapping(IMPORTED_DEFAULT_CONFIG)


def _coordinator_counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
    ) as connection:
        row = connection.execute(
            "SELECT (SELECT COUNT(*) FROM provider_request_sequences), "
            "(SELECT COUNT(*) FROM provider_request_attempts)"
        ).fetchone()
    if row is None:
        raise AssertionError("coordinator count query returned no row")
    return int(row[0]), int(row[1])


def test_module_imported_default_config_uses_pytest_history_root(
    tmp_path: Path,
) -> None:
    """A collection-time config reference must not retain application state."""

    history_config = MarketHistoryConfig.from_mapping(IMPORTED_DEFAULT_CONFIG)

    assert history_config.database_path.is_relative_to(tmp_path)


def test_collection_time_default_copies_remain_pytest_owned(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    basetemp = tmp_path_factory.getbasetemp()

    for config in (SHALLOW_IMPORTED_DEFAULT_CONFIG, DEEP_IMPORTED_DEFAULT_CONFIG):
        history_config = MarketHistoryConfig.from_mapping(config)
        assert history_config.database_path.is_relative_to(basetemp)
        with MarketHistoryStore.open(history_config):
            pass


def test_collection_time_dataflows_config_uses_pytest_history_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    history_config = MarketHistoryConfig.from_mapping(
        COLLECTION_TIME_DATAFLOWS_CONFIG
    )

    assert history_config.database_path.is_relative_to(
        tmp_path_factory.getbasetemp()
    )


@pytest.mark.parametrize(
    "copier",
    (
        pytest.param(copy.copy, id="shallow-copy"),
        pytest.param(copy.deepcopy, id="deep-copy"),
    ),
)
def test_application_default_snapshot_is_rejected_before_coordinator_open(
    application_default_config_snapshot: dict[str, object],
    copier: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    history_config = MarketHistoryConfig.from_mapping(
        copier(application_default_config_snapshot)
    )

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        MarketHistoryStore.open_provider_request_authority(history_config)


@pytest.mark.parametrize(
    "config_key",
    (
        "results_dir",
        "data_cache_dir",
        "memory_log_path",
        "market_history_payload_root",
    ),
)
def test_application_default_filesystem_roots_are_fail_closed(
    application_default_config_snapshot: dict[str, object],
    config_key: str,
) -> None:
    configured = Path(str(application_default_config_snapshot[config_key]))
    target = configured if configured.suffix else configured / "pytest-write-probe"

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        target.write_text("must-not-be-written", encoding="utf-8")


@pytest.mark.parametrize(
    "operation",
    ("mkdir", "unlink", "rename", "move", "chmod"),
)
def test_application_default_filesystem_mutations_are_fail_closed(
    application_default_config_snapshot: dict[str, object],
    operation: str,
    tmp_path: Path,
) -> None:
    protected_root = Path(
        str(application_default_config_snapshot["market_history_payload_root"])
    )
    protected_target = protected_root / "pytest-mutation-probe"
    safe_source = tmp_path / "safe-source"
    safe_source.write_text("safe", encoding="utf-8")

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        if operation == "mkdir":
            protected_target.mkdir()
        elif operation == "unlink":
            protected_target.unlink()
        elif operation == "rename":
            protected_target.rename(tmp_path / "renamed")
        elif operation == "move":
            shutil.move(safe_source, protected_target)
        else:
            protected_target.chmod(0o600)


def test_application_default_sqlite_writes_are_fail_closed(
    application_default_config_snapshot: dict[str, object],
) -> None:
    database_path = str(
        application_default_config_snapshot["market_history_database_path"]
    )

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        sqlite3.connect(database_path)


def test_historical_ticket07_evidence_root_is_fail_closed() -> None:
    evidence_root = (
        Path(__file__).resolve().parents[1]
        / ".scratch"
        / "mainland-qualified-routing-composition-factory"
        / "ticket07-evidence"
    )

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        (evidence_root / "pytest-write-probe").write_text(
            "must-not-be-written",
            encoding="utf-8",
        )


def test_repository_environment_file_is_fail_closed() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        env_path.touch()


@pytest.mark.parametrize(
    "relative_path",
    (
        ".env.enterprise",
        "config/instrument_identity_registry.json",
        "config/instrument_identity_registry.sha256",
        "tradingagents/config/crypto_identity_registry.json",
        "tradingagents/config/crypto_identity_registry.sha256",
        (
            ".scratch/mainland-qualified-routing-composition-factory/"
            "ticket06-post-cal-evidence/persistence-isolation-audit-start.json"
        ),
        (
            ".scratch/mainland-qualified-routing-composition-factory/"
            "ticket06-post-cal-evidence/"
            "persistence-isolation-immutable-baseline.json"
        ),
    ),
)
def test_repository_authority_and_audit_files_are_fail_closed(
    relative_path: str,
) -> None:
    target = Path(__file__).resolve().parents[1] / relative_path

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        target.touch()


def test_helper_with_omitted_runtime_root_records_fake_attempt_in_pytest_db(
    tmp_path: Path,
) -> None:
    history_config = _default_history_config()
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert history_config.database_path.is_relative_to(tmp_path)
    with MarketHistoryStore.open_provider_request_authority(history_config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        result = coordinator.execute_direct_physical_request(
            request_key="history:SOL-USD:pytest-isolation-probe",
            upstream_service_id="upstream:yahoo",
            owner_id="pytest-isolation-probe",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="market-snapshot",
            physical_request=lambda: "fake-yahoo-frame",
        )
        coordinator_database = store.config.database_path

    assert result.value == "fake-yahoo-frame"
    assert result.physical_attempt_count == 1
    assert _coordinator_counts(coordinator_database) == (1, 1)


def test_explicit_custom_temporary_store_remains_supported(tmp_path: Path) -> None:
    root = tmp_path / "custom-history"
    config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "custom.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    with MarketHistoryStore.open(config) as store:
        assert store.config == config

    assert config.database_path.is_file()


def test_subprocess_inherits_isolated_writable_roots(tmp_path: Path) -> None:
    code = "\n".join(
        (
            "import json",
            "from datetime import datetime, timedelta, timezone",
            "from tradingagents.default_config import DEFAULT_CONFIG",
            "from tradingagents.market_history import MarketHistoryConfig, MarketHistoryStore",
            "from tradingagents.market_history import ProviderRequestCoordinator, RequestPriority",
            "config = MarketHistoryConfig.from_mapping(DEFAULT_CONFIG)",
            "now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)",
            "with MarketHistoryStore.open_provider_request_authority(config) as store:",
            "    coordinator = ProviderRequestCoordinator(store)",
            "    coordinator.register_upstream_service('upstream:yahoo', 'Yahoo Finance')",
            "    result = coordinator.execute_direct_physical_request(",
            "        request_key='history:SOL-USD:subprocess-isolation-probe',",
            "        upstream_service_id='upstream:yahoo',",
            "        owner_id='subprocess-isolation-probe',",
            "        priority=RequestPriority.INTERACTIVE_MAINLAND,",
            "        now=lambda: now,",
            "        sleep=lambda _seconds: None,",
            "        lease_duration=timedelta(seconds=30),",
            "        operation='market-snapshot',",
            "        physical_request=lambda: 'fake-yahoo-frame',",
            "    )",
            "    path = str(store.config.database_path)",
            "print(json.dumps({'path': path, 'attempts': result.physical_attempt_count}))",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    database_path = Path(observed["path"])
    assert database_path.is_relative_to(tmp_path)
    assert observed["attempts"] == 1
    assert _coordinator_counts(database_path) == (1, 1)


def test_subprocess_with_omitted_roots_is_repaired(tmp_path: Path) -> None:
    environment = os.environ.copy()
    for name in (
        "TRADINGAGENTS_PROJECT_ROOT",
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT",
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT",
        "HOME",
        "USERPROFILE",
    ):
        environment.pop(name, None)
    code = (
        "from tradingagents.default_config import DEFAULT_CONFIG;"
        "print(DEFAULT_CONFIG['market_history_database_path'])"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).is_relative_to(tmp_path)


def test_subprocess_with_empty_roots_is_repaired(tmp_path: Path) -> None:
    environment = os.environ.copy()
    for name in (
        "TRADINGAGENTS_PROJECT_ROOT",
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT",
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT",
    ):
        environment[name] = ""
    code = (
        "from tradingagents.default_config import DEFAULT_CONFIG;"
        "print(DEFAULT_CONFIG['market_history_database_path'])"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).is_relative_to(tmp_path)


def test_wrapper_subprocess_with_empty_roots_is_repaired(tmp_path: Path) -> None:
    environment = os.environ.copy()
    for name in (
        "TRADINGAGENTS_PROJECT_ROOT",
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT",
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT",
    ):
        environment[name] = ""
    probe = tmp_path / "wrapper_probe.py"
    probe.write_text(
        "from tradingagents.default_config import DEFAULT_CONFIG\n"
        "print(DEFAULT_CONFIG['market_history_database_path'])\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", sys.executable, str(probe)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).is_relative_to(tmp_path)


def test_safe_child_project_root_normalizes_empty_subordinate_roots(
    tmp_path: Path,
) -> None:
    child_project = tmp_path / "child-project"
    environment = os.environ.copy()
    environment["TRADINGAGENTS_PROJECT_ROOT"] = str(child_project)
    for name in (
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH",
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT",
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT",
    ):
        environment[name] = ""
    code = "\n".join(
        (
            "import json",
            "from tradingagents.default_config import DEFAULT_CONFIG",
            "keys = ('results_dir', 'data_cache_dir', 'memory_log_path', "
            "'market_history_database_path', 'market_history_payload_root', "
            "'market_history_backup_root')",
            "print(json.dumps([DEFAULT_CONFIG[key] for key in keys]))",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    for value in json.loads(completed.stdout):
        assert value
        assert Path(value).is_relative_to(child_project)


def test_subprocess_empty_dotenv_disable_cannot_reload_dotenv(
    tmp_path: Path,
) -> None:
    canary = "TRADINGAGENTS_PYTEST_DOTENV_CANARY"
    (tmp_path / ".env").write_text(f"{canary}=loaded\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop(canary, None)
    environment["PYTHON_DOTENV_DISABLED"] = ""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import os; print(os.environ.get({canary!r}, 'missing'))",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "missing"


def test_preimported_dataflows_config_is_reset_before_nested_collection(
    application_default_config_snapshot: dict[str, object],
    tmp_path: Path,
) -> None:
    child_basetemp = tmp_path / "nested-pytest"
    node_id = (
        "tests/test_pytest_persistence_isolation.py::"
        "test_collection_time_dataflows_config_uses_pytest_history_root"
    )
    stale_config = repr(application_default_config_snapshot)
    code = "\n".join(
        (
            "import copy",
            "import pytest",
            "import tradingagents.dataflows.config as dataflows_config",
            f"dataflows_config._config = copy.deepcopy({stale_config})",
            f"raise SystemExit(pytest.main(['-q', '--basetemp', {str(child_basetemp)!r}, {node_id!r}]))",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_subprocess_explicit_application_path_is_fail_closed(
    application_default_config_snapshot: dict[str, object],
) -> None:
    environment = os.environ.copy()
    environment["TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH"] = str(
        application_default_config_snapshot["market_history_database_path"]
    )

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        subprocess.run(
            [sys.executable, "-c", "raise AssertionError('must not spawn')"],
            env=environment,
            check=False,
        )


def test_subprocess_relative_application_path_is_fail_closed() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["TRADINGAGENTS_RESULTS_DIR"] = "logs"

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        subprocess.run(
            [sys.executable, "-c", "raise AssertionError('must not spawn')"],
            cwd=repository_root,
            env=environment,
            check=False,
        )


def test_subprocess_protected_working_directory_is_fail_closed() -> None:
    evidence_root = (
        Path(__file__).resolve().parents[1]
        / ".scratch"
        / "mainland-qualified-routing-composition-factory"
        / "ticket07-evidence"
    )

    with pytest.raises(RuntimeError, match="pytest blocked a write"):
        subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; Path('x').touch()"],
            cwd=evidence_root,
            check=False,
        )


def test_python_child_rejects_embedded_application_path(
    application_default_config_snapshot: dict[str, object],
) -> None:
    protected_path = Path(
        str(application_default_config_snapshot["market_history_payload_root"])
    ) / "child-write-probe"
    code = (
        "from pathlib import Path; "
        f"Path({str(protected_path)!r}).write_text('must-not-write', encoding='utf-8')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert "pytest blocked a write" in completed.stderr
    assert not protected_path.exists()


def test_multiprocessing_spawn_inherits_fail_closed_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_protected_root = tmp_path / "synthetic-protected-root"
    target = synthetic_protected_root / "spawn-write-probe"
    protected_roots = json.loads(
        os.environ["TRADINGAGENTS_PYTEST_PROTECTED_ROOTS"]
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_PYTEST_PROTECTED_ROOTS",
        json.dumps([*protected_roots, str(synthetic_protected_root)]),
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_multiprocessing_write_probe,
        args=(str(target), result_queue),
    )

    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("spawned persistence probe did not terminate")
    result = result_queue.get(timeout=5)
    result_queue.close()
    result_queue.join_thread()

    assert process.exitcode == 0
    assert "pytest blocked a write" in result
    assert not target.exists()
