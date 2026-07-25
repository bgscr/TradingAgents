from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_direct_use_without_project_launcher_keeps_user_home_defaults(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "outside-project"
    user_home = tmp_path / "user-home"
    working_dir.mkdir()
    user_home.mkdir()
    env = os.environ.copy()
    for name in (
        "TRADINGAGENTS_PROJECT_ROOT",
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
    ):
        env.pop(name, None)
    env["HOME"] = str(user_home)
    env["USERPROFILE"] = str(user_home)
    code = (
        "import json;"
        "from tradingagents.default_config import DEFAULT_CONFIG as c;"
        "print(json.dumps([c['results_dir'],c['data_cache_dir'],c['memory_log_path']]))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=working_dir,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(user_home / ".tradingagents" / "logs"),
        str(user_home / ".tradingagents" / "cache"),
        str(user_home / ".tradingagents" / "memory" / "trading_memory.md"),
    ]
