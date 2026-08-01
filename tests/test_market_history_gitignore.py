from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IGNORE_PATTERN = "/data/market_history/"


def _check_ignore(path: str, *, verbose: bool = False) -> subprocess.CompletedProcess[str]:
    flag = "-v" if verbose else "-q"
    return subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=",
            "check-ignore",
            flag,
            "--no-index",
            "--",
            path,
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def test_default_runtime_market_history_is_gitignored_without_hiding_other_data() -> None:
    tracked_before = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    generated_paths = (
        "data/market_history/market_history.sqlite3",
        "data/market_history/market_history.sqlite3-wal",
        "data/market_history/market_history.sqlite3-shm",
        "data/market_history/market_history.sqlite3-journal",
        "data/market_history/market_history.sqlite3.payload-mutation.sqlite3",
        "data/market_history/market_history.sqlite3.payload-mutation.sqlite3-wal",
        "data/market_history/market_history.sqlite3.payload-mutation.sqlite3-shm",
        "data/market_history/market_history.sqlite3.payload-mutation.sqlite3-journal",
        "data/market_history/market_history.sqlite3.provider-requests.sqlite3",
        "data/market_history/payloads/sha256/ab/abcdef.bin",
        "data/market_history/payloads/sha256/ab/.abcdef.bin.install.tmp",
        "data/market_history/backups/20260727T120000Z/manifest.json",
        "data/market_history/backups/.backup-staging/market_history.sqlite3",
        "data/market_history/.market_history.sqlite3.restore-staging.restore",
        "data/market_history/maintenance/sweep-state.json",
    )
    for path in generated_paths:
        result = _check_ignore(path, verbose=True)
        assert result.returncode == 0, f"expected generated path to be ignored: {path}"
        assert RUNTIME_IGNORE_PATTERN in result.stdout

    visible_paths = (
        "data/market_history.sqlite3",
        "data/market_history-fixtures/source.csv",
        "examples/data/market_history/market_history.sqlite3",
        "tests/fixtures/market_history/market_history.sqlite3",
        "config/market_history.toml",
        "config/crypto_identity_registry.json",
        "config/crypto_identity_registry.sha256",
        "tradingagents/config/crypto_identity_registry.json",
        "tradingagents/config/crypto_identity_registry.sha256",
        "docs/market_history/README.md",
    )
    for path in visible_paths:
        result = _check_ignore(path)
        assert result.returncode == 1, f"expected intentional path to stay visible: {path}"

    for governed_path, existing_pattern in (
        ("reports/market_history/audit.json", "reports/"),
        ("logs/market-history.log", "/logs/"),
    ):
        result = _check_ignore(governed_path, verbose=True)
        assert result.returncode == 0
        assert existing_pattern in result.stdout
        assert RUNTIME_IGNORE_PATTERN not in result.stdout

    tracked_after = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert tracked_after == tracked_before


def test_default_runtime_cli_smoke_preserves_clean_worktree(tmp_path: Path) -> None:
    smoke_repo = tmp_path / "smoke-repo"
    smoke_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=smoke_repo, check=True)
    shutil.copy2(REPO_ROOT / ".gitignore", smoke_repo / ".gitignore")
    tracked_placeholder = smoke_repo / "data" / "market_history" / "README.md"
    tracked_placeholder.parent.mkdir(parents=True)
    tracked_placeholder.write_text("tracked runtime documentation\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=smoke_repo, check=True)
    subprocess.run(
        ["git", "add", "--force", "data/market_history/README.md"],
        cwd=smoke_repo,
        check=True,
    )
    baseline = _git_status(smoke_repo)

    env = os.environ.copy()
    env["TRADINGAGENTS_PROJECT_ROOT"] = str(smoke_repo)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(REPO_ROOT), env.get("PYTHONPATH")))
    )
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import os",
                    "from pathlib import Path",
                    "from typer.testing import CliRunner",
                    "from cli import main as cli_main",
                    "from tradingagents.market_history import MarketHistoryConfig, MarketHistoryStore",
                    "def deterministic_analysis(*, checkpoint=None):",
                    "    config = MarketHistoryConfig.from_mapping(cli_main.DEFAULT_CONFIG)",
                    "    expected = Path(os.environ['TRADINGAGENTS_PROJECT_ROOT']) / 'data' / 'market_history' / 'market_history.sqlite3'",
                    "    assert config.database_path == expected",
                    "    with MarketHistoryStore.open(config) as store:",
                    "        store.install_payload(b'ticket-11-smoke', media_type='application/octet-stream')",
                    "        store.create_backup()",
                    "    with MarketHistoryStore.open_provider_request_authority(config):",
                    "        pass",
                    "cli_main.run_analysis = deterministic_analysis",
                    "result = CliRunner().invoke(cli_main.app, ['analyze'])",
                    "assert result.exit_code == 0, repr(result.exception)",
                )
            ),
        ],
        cwd=smoke_repo,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    assert smoke.returncode == 0, f"stdout:\n{smoke.stdout}\n\nstderr:\n{smoke.stderr}"

    runtime_root = smoke_repo / "data" / "market_history"
    assert (runtime_root / "market_history.sqlite3").is_file()
    assert (runtime_root / "market_history.sqlite3.payload-mutation.sqlite3").is_file()
    assert (runtime_root / "market_history.sqlite3.provider-requests.sqlite3").is_file()
    assert any((runtime_root / "payloads" / "sha256").rglob("*.bin"))
    assert any((runtime_root / "backups").glob("*/manifest.json"))
    assert _git_status(smoke_repo) == baseline
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", "data/market_history/README.md"],
        cwd=smoke_repo,
        check=False,
        capture_output=True,
    ).returncode == 0
