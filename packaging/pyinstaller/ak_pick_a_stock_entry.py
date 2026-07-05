from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ak_pick_a_stock import main as pick_main


def main() -> int:
    return pick_main()


if __name__ == "__main__":
    sys.exit(main())
