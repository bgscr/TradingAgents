"""Install the pytest persistence guard in every Python child process."""

import json
import os
from pathlib import Path

if os.environ.get("TRADINGAGENTS_PYTEST_PERSISTENCE_GUARD") == "1":
    from tests import conftest

    conftest._APPLICATION_PROTECTED_ROOTS = tuple(
        Path(value)
        for value in json.loads(
            os.environ["TRADINGAGENTS_PYTEST_PROTECTED_ROOTS"]
        )
    )
    _PERSISTENCE_GUARD = conftest._install_persistence_guard()
