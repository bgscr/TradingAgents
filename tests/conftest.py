"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import builtins
import copy
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import tradingagents.dataflows.config as dataflows_config
import tradingagents.default_config as default_config

pytest_plugins = ("tests.live_test_guard",)


_APPLICATION_DEFAULT_CONFIG = copy.deepcopy(default_config.DEFAULT_CONFIG)
_APPLICATION_DATAFLOWS_CONFIG = dataflows_config.get_config()
_CANONICAL_DEFAULT_CONFIG = default_config.DEFAULT_CONFIG
_SESSION_DEFAULT_CONFIG: dict[str, Any] | None = None
_ORIGINAL_WRITABLE_ENVIRONMENT: dict[str, str | None] = {}
_PERSISTENCE_GUARD: pytest.MonkeyPatch | None = None
_WRITABLE_ENVIRONMENT_KEYS = (
    "TRADINGAGENTS_PROJECT_ROOT",
    "TRADINGAGENTS_RESULTS_DIR",
    "TRADINGAGENTS_CACHE_DIR",
    "TRADINGAGENTS_MEMORY_LOG_PATH",
    "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH",
    "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT",
    "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT",
)
_CHILD_GUARD_ENVIRONMENT = "TRADINGAGENTS_PYTEST_PERSISTENCE_GUARD"
_CHILD_PROTECTED_ROOTS_ENVIRONMENT = "TRADINGAGENTS_PYTEST_PROTECTED_ROOTS"
_ISOLATION_ENVIRONMENT_KEYS = (
    *_WRITABLE_ENVIRONMENT_KEYS,
    "PYTHON_DOTENV_DISABLED",
    "PYTHONPATH",
    _CHILD_GUARD_ENVIRONMENT,
    _CHILD_PROTECTED_ROOTS_ENVIRONMENT,
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_TEST_INSTRUMENT_REGISTRY_PATH = (
    _REPOSITORY_ROOT / "config" / "instrument_identity_registry.json"
)
_TEST_INSTRUMENT_REGISTRY_SHA256 = (
    _REPOSITORY_ROOT / "config" / "instrument_identity_registry.sha256"
).read_text(encoding="ascii").split(maxsplit=1)[0]
_CHILD_GUARD_PATH = _REPOSITORY_ROOT / "tests" / "pytest_persistence_sitecustomize"
_PROTECTED_TICKET07_ROOT = (
    _REPOSITORY_ROOT
    / ".scratch"
    / "mainland-qualified-routing-composition-factory"
    / "ticket07-evidence"
)


class PytestPersistenceIsolationError(RuntimeError):
    """A deterministic test attempted to write application-owned state."""


def pytest_collection_modifyitems(items):
    if os.name == "nt":
        return
    windows = pytest.mark.skip(reason="requires Windows filesystem/native contracts; exercised by Windows CI")
    for item in items:
        # This existing test intentionally exercises cmd.exe child-environment
        # propagation, so it belongs to the Windows CI job.
        command_wrapper = (
            item.path.name == "test_pytest_persistence_isolation.py"
            and item.name == "test_wrapper_subprocess_with_empty_roots_is_repaired"
        )
        if item.get_closest_marker("windows") is not None or command_wrapper:
            item.add_marker(windows)


def _directory_relative_path(path, directory_fd=None):
    candidate = Path(path)
    if directory_fd is None or candidate.is_absolute():
        return candidate
    try:
        parent = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
    except (OSError, AttributeError) as exc:
        raise PytestPersistenceIsolationError("cannot resolve directory descriptor safely") from exc
    if not parent.is_absolute() or str(parent).endswith(" (deleted)"):
        raise PytestPersistenceIsolationError("directory descriptor has no stable absolute path")
    return parent / candidate


def _canonical_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _application_protected_roots() -> tuple[Path, ...]:
    canonical_home = _canonical_path(Path.home() / ".tradingagents")
    candidates = (
        canonical_home,
        _canonical_path(_REPOSITORY_ROOT / ".env"),
        _canonical_path(_REPOSITORY_ROOT / ".env.enterprise"),
        _canonical_path(_REPOSITORY_ROOT / "config"),
        _canonical_path(_REPOSITORY_ROOT / "logs"),
        _canonical_path(_REPOSITORY_ROOT / "data" / "cache"),
        _canonical_path(_REPOSITORY_ROOT / "data" / "memory"),
        _canonical_path(_REPOSITORY_ROOT / "data" / "market_history"),
        _canonical_path(_REPOSITORY_ROOT / "tradingagents" / "config"),
        _canonical_path(
            _REPOSITORY_ROOT
            / ".scratch"
            / "mainland-qualified-routing-composition-factory"
            / "ticket06-post-cal-evidence"
            / "persistence-isolation-audit-start.json"
        ),
        _canonical_path(
            _REPOSITORY_ROOT
            / ".scratch"
            / "mainland-qualified-routing-composition-factory"
            / "ticket06-post-cal-evidence"
            / "persistence-isolation-immutable-baseline.json"
        ),
        _canonical_path(
            _REPOSITORY_ROOT
            / ".scratch"
            / "mainland-qualified-routing-composition-factory"
            / "ticket06-post-cal-evidence"
            / "preflight-immutable-inventory.json"
        ),
        _canonical_path(_APPLICATION_DEFAULT_CONFIG["results_dir"]),
        _canonical_path(_APPLICATION_DEFAULT_CONFIG["data_cache_dir"]),
        _canonical_path(_APPLICATION_DEFAULT_CONFIG["memory_log_path"]).parent,
        _canonical_path(
            _APPLICATION_DEFAULT_CONFIG["market_history_database_path"]
        ).parent,
        _canonical_path(_APPLICATION_DEFAULT_CONFIG["market_history_payload_root"]),
        _canonical_path(_APPLICATION_DEFAULT_CONFIG["market_history_backup_root"]),
        _PROTECTED_TICKET07_ROOT.resolve(strict=False),
    )
    return tuple(dict.fromkeys(candidates))


_APPLICATION_PROTECTED_ROOTS = _application_protected_roots()


def _child_guard_environment(python_path: str) -> dict[str, str]:
    search_paths = dict.fromkeys(
        (
            str(_CHILD_GUARD_PATH),
            str(_REPOSITORY_ROOT),
            *python_path.split(os.pathsep),
        )
    )
    return {
        "PYTHONPATH": os.pathsep.join(path for path in search_paths if path),
        _CHILD_GUARD_ENVIRONMENT: "1",
        _CHILD_PROTECTED_ROOTS_ENVIRONMENT: json.dumps(
            [str(path) for path in _APPLICATION_PROTECTED_ROOTS]
        ),
    }


def _protected_root_for(path: Path) -> Path | None:
    resolved = path.expanduser().resolve(strict=False)
    return next(
        (
            root
            for root in _APPLICATION_PROTECTED_ROOTS
            if resolved == root or resolved.is_relative_to(root)
        ),
        None,
    )


def _reject_protected_write(path: str | os.PathLike[str]) -> None:
    candidate = _canonical_path(path)
    protected_root = _protected_root_for(candidate)
    if protected_root is not None:
        raise PytestPersistenceIsolationError(
            "pytest blocked a write to application-owned persistent state: "
            f"path={candidate}, protected_root={protected_root}"
        )


def _mode_can_write(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _sqlite_path(database: object) -> Path | None:
    if not isinstance(database, (str, os.PathLike)):
        return None
    value = os.fspath(database)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if value == ":memory:":
        return None
    if value.startswith("file:"):
        value = value.removeprefix("file:").split("?", 1)[0]
    return _canonical_path(value)


def _sqlite_is_explicitly_read_only(database: object, uri: bool) -> bool:
    return (
        uri
        and isinstance(database, str)
        and database.startswith("file:")
        and "mode=ro" in database.partition("?")[2].split("&")
    )


def _path_is_within(path: Path, root: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    resolved_root = root.expanduser().resolve(strict=False)
    return resolved == resolved_root or resolved.is_relative_to(resolved_root)


def _child_has_isolated_default_root(environment: dict[str, str]) -> bool:
    if environment.get("TRADINGAGENTS_PROJECT_ROOT"):
        return True
    current_root = _canonical_path(os.environ["TRADINGAGENTS_PROJECT_ROOT"])
    return any(
        _path_is_within(_canonical_path(value), current_root)
        for name in ("USERPROFILE", "HOME")
        if (value := environment.get(name))
    )


def _subprocess_path(value: str, cwd: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _reject_protected_subprocess_environment(
    environment: dict[str, str],
    *,
    cwd: Path,
) -> None:
    for name in _WRITABLE_ENVIRONMENT_KEYS:
        value = environment.get(name)
        if not value:
            continue
        candidates = (_subprocess_path(value, cwd),)
        if name == "TRADINGAGENTS_PROJECT_ROOT":
            project_root = candidates[0]
            candidates = (
                project_root / "logs",
                project_root / "data" / "cache",
                project_root / "data" / "memory",
                project_root / "data" / "market_history",
            )
        for candidate in candidates:
            _reject_protected_write(candidate)


def _isolated_subprocess_environment(
    environment: dict[str, str] | None,
    *,
    cwd: Path,
    repair_missing_roots: bool,
) -> dict[str, str]:
    child_environment = dict(os.environ if environment is None else environment)
    if not child_environment.get("PYTHON_DOTENV_DISABLED"):
        child_environment["PYTHON_DOTENV_DISABLED"] = "1"
    if repair_missing_roots:
        if _child_has_isolated_default_root(child_environment):
            for name in _WRITABLE_ENVIRONMENT_KEYS[1:]:
                if not child_environment.get(name):
                    child_environment.pop(name, None)
        else:
            for name in _WRITABLE_ENVIRONMENT_KEYS:
                if not child_environment.get(name):
                    child_environment[name] = os.environ[name]
    child_environment.update(
        _child_guard_environment(child_environment.get("PYTHONPATH", ""))
    )
    _reject_protected_subprocess_environment(child_environment, cwd=cwd)
    return child_environment


def _isolated_runtime_config(root: Path) -> dict[str, Any]:
    config = copy.deepcopy(_APPLICATION_DEFAULT_CONFIG)
    history_root = root / "data" / "market_history"
    config.update(
        {
            "results_dir": str(root / "logs"),
            "data_cache_dir": str(root / "data" / "cache"),
            "memory_log_path": str(root / "data" / "memory" / "trading_memory.md"),
            "market_history_database_path": str(
                history_root / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
            "instrument_identity_registry_path": str(
                _TEST_INSTRUMENT_REGISTRY_PATH
            ),
            "instrument_identity_registry_sha256": (
                _TEST_INSTRUMENT_REGISTRY_SHA256
            ),
        }
    )
    return config


def _writable_environment(config: dict[str, Any], root: Path) -> dict[str, str]:
    return {
        "TRADINGAGENTS_PROJECT_ROOT": str(root),
        "TRADINGAGENTS_RESULTS_DIR": str(config["results_dir"]),
        "TRADINGAGENTS_CACHE_DIR": str(config["data_cache_dir"]),
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(config["memory_log_path"]),
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH": str(
            config["market_history_database_path"]
        ),
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT": str(
            config["market_history_payload_root"]
        ),
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT": str(
            config["market_history_backup_root"]
        ),
    }


def _replace_canonical_default(config: dict[str, Any]) -> None:
    _CANONICAL_DEFAULT_CONFIG.clear()
    _CANONICAL_DEFAULT_CONFIG.update(copy.deepcopy(config))
    default_config.DEFAULT_CONFIG = _CANONICAL_DEFAULT_CONFIG


def _install_persistence_guard() -> pytest.MonkeyPatch:
    """Reject application-state mutation before test-module collection."""

    from tradingagents.market_history.store import MarketHistoryStore

    guard = pytest.MonkeyPatch()
    original_store_open = MarketHistoryStore.open.__func__
    original_sqlite_connect = sqlite3.connect
    original_builtin_open = builtins.open
    original_path_open = Path.open
    original_os_open = os.open
    original_popen = subprocess.Popen

    def guarded_store_open(cls, config, **kwargs):
        for path in (config.database_path, config.payload_root, config.backup_root):
            _reject_protected_write(path)
        return original_store_open(cls, config, **kwargs)

    def guarded_sqlite_connect(database, *args, **kwargs):
        path = _sqlite_path(database)
        uri = bool(kwargs.get("uri", False))
        if (
            path is not None
            and _protected_root_for(path) is not None
            and not _sqlite_is_explicitly_read_only(database, uri)
        ):
            _reject_protected_write(path)
        return original_sqlite_connect(database, *args, **kwargs)

    def guarded_builtin_open(file, mode="r", *args, **kwargs):
        if _mode_can_write(str(mode)) and isinstance(file, (str, os.PathLike)):
            _reject_protected_write(file)
        return original_builtin_open(file, mode, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        if _mode_can_write(str(mode)):
            _reject_protected_write(self)
        return original_path_open(self, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags and isinstance(path, (str, os.PathLike)):
            _reject_protected_write(_directory_relative_path(path, kwargs.get("dir_fd")))
        return original_os_open(path, flags, *args, **kwargs)

    def guard_one_path(operation):
        def guarded(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)):
                _reject_protected_write(_directory_relative_path(path, kwargs.get("dir_fd")))
            return operation(path, *args, **kwargs)

        return guarded

    def guard_two_paths(operation, *, protect_source: bool = True):
        def guarded(source, destination, *args, **kwargs):
            if protect_source and isinstance(source, (str, os.PathLike)):
                _reject_protected_write(_directory_relative_path(source, kwargs.get("src_dir_fd")))
            if isinstance(destination, (str, os.PathLike)):
                _reject_protected_write(_directory_relative_path(
                    destination, kwargs.get("dst_dir_fd", kwargs.get("dir_fd"))
                ))
            return operation(source, destination, *args, **kwargs)

        return guarded

    def guarded_popen(*args, **kwargs):
        positional = list(args)
        cwd_value = positional[9] if len(positional) > 9 else kwargs.get("cwd")
        cwd = _canonical_path(cwd_value) if cwd_value is not None else Path.cwd()
        if cwd_value is not None:
            _reject_protected_write(cwd)
        environment = positional[10] if len(positional) > 10 else kwargs.get("env")
        isolated_environment = _isolated_subprocess_environment(
            environment,
            cwd=cwd,
            repair_missing_roots=True,
        )
        if len(positional) > 10:
            positional[10] = isolated_environment
        else:
            kwargs["env"] = isolated_environment
        return original_popen(*positional, **kwargs)

    guard.setattr(MarketHistoryStore, "open", classmethod(guarded_store_open))
    guard.setattr(sqlite3, "connect", guarded_sqlite_connect)
    guard.setattr(builtins, "open", guarded_builtin_open)
    guard.setattr(Path, "open", guarded_path_open)
    guard.setattr(os, "open", guarded_os_open)
    for name in (
        "chmod",
        "mkdir",
        "remove",
        "rmdir",
        "truncate",
        "unlink",
        "utime",
    ):
        guard.setattr(os, name, guard_one_path(getattr(os, name)))
    for name in ("link", "rename", "replace"):
        guard.setattr(os, name, guard_two_paths(getattr(os, name)))
    guard.setattr(
        os,
        "symlink",
        guard_two_paths(os.symlink, protect_source=False),
    )
    # Python 3.10 caches these OS functions in pathlib before this guard is
    # installed. Route that accessor through the same checks as direct calls.
    accessor = getattr(Path, "_accessor", None)
    if accessor is not None:
        for name in ("chmod", "mkdir", "unlink", "rmdir", "link", "rename", "replace", "symlink"):
            if hasattr(accessor, name):
                guard.setattr(accessor, name, getattr(os, name))
    guard.setattr(shutil, "rmtree", guard_one_path(shutil.rmtree))
    guard.setattr(shutil, "move", guard_two_paths(shutil.move))
    for name in ("copy", "copy2", "copyfile", "copytree"):
        guard.setattr(
            shutil,
            name,
            guard_two_paths(getattr(shutil, name), protect_source=False),
        )
    guard.setattr(subprocess, "Popen", guarded_popen)
    return guard


@pytest.fixture()
def application_default_config_snapshot() -> dict[str, Any]:
    """Expose a copy for fail-closed tests without sharing mutable state."""

    return copy.deepcopy(_APPLICATION_DEFAULT_CONFIG)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Isolate writable defaults before pytest imports any test module."""

    global _ORIGINAL_WRITABLE_ENVIRONMENT, _PERSISTENCE_GUARD
    global _SESSION_DEFAULT_CONFIG

    temp_factory = getattr(session.config, "_tmp_path_factory", None)
    if temp_factory is None:
        raise pytest.UsageError("pytest temporary-path factory is unavailable")
    runtime_root = temp_factory.mktemp("tradingagents-runtime", numbered=False)
    _SESSION_DEFAULT_CONFIG = _isolated_runtime_config(runtime_root)
    _ORIGINAL_WRITABLE_ENVIRONMENT = {
        name: os.environ.get(name) for name in _ISOLATION_ENVIRONMENT_KEYS
    }
    os.environ.update(_writable_environment(_SESSION_DEFAULT_CONFIG, runtime_root))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ.update(_child_guard_environment(os.environ.get("PYTHONPATH", "")))
    _replace_canonical_default(_SESSION_DEFAULT_CONFIG)
    dataflows_config._config = copy.deepcopy(_SESSION_DEFAULT_CONFIG)
    _PERSISTENCE_GUARD = _install_persistence_guard()


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore caller state only after every pytest/session finalizer has run."""

    global _PERSISTENCE_GUARD

    del config
    if _PERSISTENCE_GUARD is not None:
        _PERSISTENCE_GUARD.undo()
        _PERSISTENCE_GUARD = None
    _replace_canonical_default(_APPLICATION_DEFAULT_CONFIG)
    dataflows_config._config = copy.deepcopy(_APPLICATION_DATAFLOWS_CONFIG)
    for name, value in _ORIGINAL_WRITABLE_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    if _SESSION_DEFAULT_CONFIG is None:
        raise RuntimeError("pytest writable-root isolation was not initialized")
    isolated_default = _isolated_runtime_config(tmp_path)
    for name, value in _writable_environment(isolated_default, tmp_path).items():
        monkeypatch.setenv(name, value)
    _replace_canonical_default(isolated_default)
    dataflows_config._config = copy.deepcopy(isolated_default)
    try:
        yield
    finally:
        _replace_canonical_default(_SESSION_DEFAULT_CONFIG)
        dataflows_config._config = copy.deepcopy(_SESSION_DEFAULT_CONFIG)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
