from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def get_app():
    from cli.main import app

    return app


def main() -> None:
    app = get_app()
    app()


if __name__ == "__main__":
    main()
