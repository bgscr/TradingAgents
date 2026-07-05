from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORTABLE_ENV_NAMES = (
    "TRADINGAGENTS_RESULTS_DIR",
    "TRADINGAGENTS_CACHE_DIR",
    "TRADINGAGENTS_MEMORY_LOG_PATH",
    "AK_PICK_OUTPUT_PATH",
)


@pytest.fixture(autouse=True)
def _isolate_entrypoint_environment(monkeypatch):
    for name in PORTABLE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tradingagents_entry_invokes_typer_app(monkeypatch):
    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "tradingagents_entry.py",
        "tradingagents_entry_for_test",
    )
    calls = []

    def fake_app():
        calls.append("called")

    monkeypatch.setattr(module, "get_app", lambda: fake_app)

    assert module.main() is None
    assert calls == ["called"]


def test_ak_pick_entry_returns_picker_exit_code(monkeypatch):
    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "ak_pick_a_stock_entry.py",
        "ak_pick_a_stock_entry_for_test",
    )
    monkeypatch.setattr(module, "get_picker_main", lambda: lambda: 7)

    assert module.main() == 7


def test_pyinstaller_spec_collects_akshare_data_files():
    spec_text = (
        ROOT / "packaging" / "pyinstaller" / "tradingagents_portable.spec"
    ).read_text(encoding="utf-8")
    data_collection_block = spec_text.split("datas = []", 1)[1].split(
        "tradingagents_analysis",
        1,
    )[0]

    assert '"akshare"' in data_collection_block


def test_entrypoints_are_importable_from_script_directory(monkeypatch):
    script_dir = ROOT / "packaging" / "pyinstaller"
    original_path = list(sys.path)
    script_path = str(script_dir)
    root_path = str(ROOT)
    monkeypatch.setattr(
        sys,
        "path",
        [script_path, *[path for path in original_path if path not in {script_path, root_path}]],
    )

    _load_module(script_dir / "tradingagents_entry.py", "tradingagents_entry_script_path_test")
    _load_module(script_dir / "ak_pick_a_stock_entry.py", "ak_pick_entry_script_path_test")

    assert root_path in sys.path


def test_tradingagents_entry_sets_portable_defaults_before_cli_import(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tradingagents.exe"))

    cli_package = types.ModuleType("cli")
    cli_main = types.ModuleType("cli.main")

    def fake_app():
        return None

    cli_main.app = fake_app
    monkeypatch.setitem(sys.modules, "cli", cli_package)
    monkeypatch.setitem(sys.modules, "cli.main", cli_main)

    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "tradingagents_entry.py",
        "tradingagents_entry_portable_defaults_test",
    )

    assert module.get_app() is fake_app
    assert os.environ["TRADINGAGENTS_RESULTS_DIR"] == str(exe_dir / "reports" / "runs")
    assert os.environ["TRADINGAGENTS_CACHE_DIR"] == str(exe_dir / "data" / "cache")
    assert os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] == str(
        exe_dir / "data" / "memory" / "trading_memory.md"
    )


def test_tradingagents_entry_uses_app_dir_as_cwd_when_frozen(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    outside_dir = tmp_path / "outside"
    exe_dir.mkdir()
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tradingagents.exe"))

    _load_module(
        ROOT / "packaging" / "pyinstaller" / "tradingagents_entry.py",
        "tradingagents_entry_portable_cwd_test",
    )

    assert Path.cwd() == exe_dir


def test_tradingagents_entry_preserves_launcher_environment(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tradingagents.exe"))
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", "launcher-results")
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", "launcher-cache")
    monkeypatch.setenv("TRADINGAGENTS_MEMORY_LOG_PATH", "launcher-memory.md")

    _load_module(
        ROOT / "packaging" / "pyinstaller" / "tradingagents_entry.py",
        "tradingagents_entry_preserve_env_test",
    )

    assert os.environ["TRADINGAGENTS_RESULTS_DIR"] == "launcher-results"
    assert os.environ["TRADINGAGENTS_CACHE_DIR"] == "launcher-cache"
    assert os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] == "launcher-memory.md"


def test_ak_pick_entry_sets_output_path_before_picker_import(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "ak_pick_a_stock.exe"))

    class FakePicker(types.ModuleType):
        def __getattr__(self, name: str):
            if name == "main":
                assert os.environ["AK_PICK_OUTPUT_PATH"] == str(
                    exe_dir / "reports" / "ak_candidates.csv"
                )
                return lambda: 0
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "ak_pick_a_stock", FakePicker("ak_pick_a_stock"))

    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "ak_pick_a_stock_entry.py",
        "ak_pick_entry_portable_defaults_test",
    )

    assert module.get_picker_main()() == 0
    assert module.main() == 0


def test_ak_pick_entry_preserves_launcher_output_path(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "ak_pick_a_stock.exe"))
    monkeypatch.setenv("AK_PICK_OUTPUT_PATH", "launcher-ak-candidates.csv")

    class FakePicker(types.ModuleType):
        def __getattr__(self, name: str):
            if name == "main":
                assert os.environ["AK_PICK_OUTPUT_PATH"] == "launcher-ak-candidates.csv"
                return lambda: 0
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "ak_pick_a_stock", FakePicker("ak_pick_a_stock"))

    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "ak_pick_a_stock_entry.py",
        "ak_pick_entry_preserve_env_test",
    )

    assert module.get_picker_main()() == 0
    assert module.main() == 0
