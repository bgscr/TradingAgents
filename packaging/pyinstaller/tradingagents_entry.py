from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _REPO_ROOT


def _set_portable_defaults() -> None:
    app_dir = _app_dir()
    os.environ.setdefault("TRADINGAGENTS_RESULTS_DIR", str(app_dir / "reports" / "runs"))
    os.environ.setdefault("TRADINGAGENTS_CACHE_DIR", str(app_dir / "data" / "cache"))
    os.environ.setdefault(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        str(app_dir / "data" / "memory" / "trading_memory.md"),
    )


_set_portable_defaults()


def get_app():
    from cli.main import app

    return app


def main() -> None:
    app = get_app()
    app()


if __name__ == "__main__":
    main()
