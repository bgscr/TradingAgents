"""Installed production parser and actual isolated CLI, synthetic input only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.diagnostic_runtime import safe_environment
from tests.test_news_diagnostic import synthetic_get

SOURCE = Path(__file__).resolve().parents[1]


def _run_cli(tmp_path, flags, replay, ticker="600895.SH"):
    # Negative cases lacking -B must not write bytecode into the source/runtime.
    cache_flags = [] if "-B" in flags else ["-X", f"pycache_prefix={tmp_path / 'bytecode'}"]
    return subprocess.run(
        [sys.executable, *flags, *cache_flags,
         str(SOURCE / "scripts/diagnose_public_news.py"),
         "--output", str(tmp_path / "output"), "--replay", str(replay),
         "--start", "2026-10-05", "--end", "2026-10-12", "--ticker", ticker, "--transport", "direct"],
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


@pytest.mark.parametrize("ticker,callback_matches", [
    ("600895.SH", True), ("600919.SH", True), ("600919.SH", False),
])
def test_real_isolated_cli_replays_current_parser_with_offline_guard(tmp_path, ticker, callback_matches):
    from tradingagents import news_diagnostic as diagnostic

    source = tmp_path / "input"
    source.mkdir()
    synthetic = synthetic_get(symbol=ticker[:6])

    def get(url, *, params, **kwargs):
        response = synthetic(url, params=params, **kwargs)
        if not callback_matches:
            response.content = response.content.replace(params["cb"].encode(), b"syntheticOtherCallback", 1)
        return response

    captured = diagnostic.capture(source, transport="direct", ticker=ticker, get=get, start="2026-10-05", end="2026-10-12",
    )
    assert captured["capture_passed"]
    receipt = (source / "capture.json").read_bytes()
    result = _run_cli(tmp_path, ["-I", "-S", "-B"], source, ticker=ticker)
    assert result.returncode == 0, result.stderr
    replay = json.loads((tmp_path / "output/result.json").read_text(encoding="utf-8"))
    summary = json.loads(result.stdout)
    assert summary["callback_matches"] is callback_matches
    assert replay["callback_matches"] is callback_matches
    assert "syntheticOtherCallback" not in result.stdout
    assert replay["acceptance_passed"]
    assert replay["ticker"] == ticker and replay["same_ticker"]
    assert replay["same_source_bindings"] and replay["same_adapter_frame_sha256"] and replay["same_artifact_sha256"]
    assert replay["guard_counts"]["credentials"] == 0
    assert replay["guard_counts"]["outside_write"] == 0
    assert replay["guard_counts"]["subprocess"] == 0
    # urllib3 may probe local IPv6 support on import; the real guard rejects it.
    assert all(event == {"event": "socket.bind", "module": "connection.py", "function": "_has_ipv6"}
               for event in replay["blocked_network"])
    reservation = json.loads((tmp_path / "output/reservation.json").read_text())
    assert reservation["physical_request_limit"] == 0
    assert reservation["live"] is False
    assert reservation["ticker"] == ticker
    assert reservation["symbol"] == ticker[:6]
    assert reservation["window"] == {"start": "2026-10-05", "end": "2026-10-12"}
    assert (source / "capture.json").read_bytes() == receipt


def test_installed_parser_does_not_accept_discarded_metadata_projection(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(tmp_path, transport="direct", ticker="600895.SH", get=synthetic_get(extra={"message": "synthetic metadata"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["source_bindings"] == diagnostic._source_bindings()
    assert result["checks"]["retention"]["reason"] == "unknown_envelope_fields"
    assert result["checks"]["parser"]["status"] == "not_started"
    assert result["checks"]["production"]["status"] == "failed"
    assert not result.get("artifact_sha256")
    assert not result["body_saved"] and not result["capture_passed"]
    assert not result["projection_saved"]
    assert not (tmp_path / "public-projection.txt").exists()
    assert not diagnostic.replay(tmp_path, transport="direct", ticker="600895.SH", start="2026-10-05", end="2026-10-12")["acceptance_passed"]
