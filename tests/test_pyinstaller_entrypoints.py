from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.setattr(module, "pick_main", lambda: 7)

    assert module.main() == 7


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
