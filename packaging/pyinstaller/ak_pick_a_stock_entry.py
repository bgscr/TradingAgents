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
    os.environ.setdefault(
        "AK_PICK_OUTPUT_PATH",
        str(_app_dir() / "reports" / "ak_candidates.csv"),
    )


_set_portable_defaults()

from ak_pick_a_stock import main as pick_main


def main() -> int:
    return pick_main()


if __name__ == "__main__":
    sys.exit(main())
