"""Installed SDK integration and actual CLI startup boundaries (synthetic input)."""

import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.diagnostic_runtime import safe_environment
from tests.test_news_diagnostic import synthetic_get

SOURCE = Path(__file__).resolve().parents[1]


def _run_cli(tmp_path, flags, replay):
    # Negative cases lacking -B must not write bytecode into the source/runtime.
    cache_flags = [] if "-B" in flags else ["-X", f"pycache_prefix={tmp_path / 'bytecode'}"]
    return subprocess.run(
        [sys.executable, *flags, *cache_flags,
         str(SOURCE / "scripts/diagnose_public_news.py"),
         "--output", str(tmp_path / "output"), "--replay", str(replay),
         "--start", "2026-10-05", "--end", "2026-10-12"],
        cwd=tmp_path, env=safe_environment(tmp_path), capture_output=True, text=True,
        timeout=60, check=False,
    )


@pytest.mark.parametrize("flags", [[], ["-S", "-B"], ["-I", "-B"], ["-I", "-S"]],
                         ids=["ordinary", "missing-I", "missing-S", "missing-B"])
def test_real_cli_requires_isolated_startup_before_output(tmp_path, flags):
    result = _run_cli(tmp_path, flags, tmp_path / "missing-input")
    assert result.returncode == 2
    assert "use Python -I -S -B" in result.stderr
    assert not (tmp_path / "output").exists()


def test_real_isolated_cli_replays_installed_sdk_with_offline_guard(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    source = tmp_path / "input"
    source.mkdir()
    captured = diagnostic.capture(
        source, get=synthetic_get(), start="2026-10-05", end="2026-10-12",
    )
    assert captured["capture_passed"]
    receipt = (source / "capture.json").read_bytes()
    result = _run_cli(tmp_path, ["-I", "-S", "-B"], source)
    assert result.returncode == 0, result.stderr
    replay = json.loads((tmp_path / "output/result.json").read_text(encoding="utf-8"))
    assert replay["acceptance_passed"]
    assert replay["same_sdk"] and replay["same_sdk_frame"] and replay["same_artifact"]
    assert replay["guard_counts"]["credentials"] == 0
    assert replay["guard_counts"]["outside_write"] == 0
    assert replay["guard_counts"]["subprocess"] == 0
    # urllib3 may probe local IPv6 support on import; the real guard rejects it.
    assert all(event == {"event": "socket.bind", "module": "connection.py", "function": "_has_ipv6"}
               for event in replay["blocked_network"])
    reservation = json.loads((tmp_path / "output/reservation.json").read_text())
    assert reservation["physical_request_limit"] == 0
    assert reservation["live"] is False
    assert (source / "capture.json").read_bytes() == receipt


def test_installed_sdk_projection_uses_production_review_identity(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    actual = hashlib.sha256(inspect.getsource(diagnostic.akshare_data.ak.stock_news_em).encode()).hexdigest()
    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "synthetic metadata"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["sdk_sha256"] == actual
    assert result["checks"]["sdk"]["status"] == "passed"
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["body_saved"] and not result["capture_passed"]
    if actual == diagnostic.PROJECTION_SDK_SHA256:
        assert result["projection_saved"]
        replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
        assert replay["projection_replay_passed"]
        assert replay["same_sdk_frame"] and replay["same_artifact"]
    else:
        assert not result["projection_saved"]
        assert result["checks"]["projection"]["reason"] == "sdk_projection_not_reviewed"
        assert not (tmp_path / "public-projection.txt").exists()
