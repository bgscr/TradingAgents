"""Fresh-process probe for observable CLI checkpoint configuration."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import main as cli_main
from tradingagents.run_telemetry import RunTelemetryLedger


class _ProbeStop(RuntimeError):
    pass


class _NoOpDisplay:
    def start(self):
        return None

    def refresh(self, spinner_text=None):
        return None

    def publish_event(self, event):
        return None

    def report_ready(self, section_name, content, path):
        return None

    def close(self):
        return None


def _selections() -> dict:
    return {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-24",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="market")],
        "research_depth": 1,
        "shallow_thinker": "probe-quick",
        "deep_thinker": "probe-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
        "china_a_enhancement_preset": "basic",
    }


def main() -> int:
    output_path = Path(os.environ["CHECKPOINT_PROBE_OUTPUT"])
    work_dir = output_path.parent
    observed: dict[str, object] = {}

    class _FakePropagator:
        @staticmethod
        def get_graph_args(*args, **kwargs):
            return {}

    class _FailingStream:
        @staticmethod
        def stream(_initial_state, **kwargs):
            observed["stream_thread_id_present"] = bool(
                kwargs.get("config", {})
                .get("configurable", {})
                .get("thread_id")
            )
            raise _ProbeStop("probe reached graph execution")

    class _FakeTradingAgentsGraph:
        def __init__(self, *args, config, **kwargs):
            self.config = config
            self.propagator = _FakePropagator()
            self.graph = _FailingStream()
            observed["config_checkpoint_enabled"] = config["checkpoint_enabled"]

        @contextmanager
        def checkpoint_scope(self, ticker, trade_date, asset_type="stock"):
            graph_config = {}
            if self.config["checkpoint_enabled"]:
                graph_config = {
                    "configurable": {
                        "thread_id": f"{ticker}:{trade_date}:{asset_type}",
                    }
                }
            yield cli_main.CheckpointSession(graph_config, False)

        @staticmethod
        def resolve_evidence_state(*args, **kwargs):
            return None

        @staticmethod
        def create_initial_state(*args, **kwargs):
            return {
                "messages": [],
                "run_id": "run:" + ("0" * 64),
            }

    @contextmanager
    def _snapshot_run():
        yield SimpleNamespace(telemetry_ledger=RunTelemetryLedger())

    cli_main.DEFAULT_CONFIG = dict(
        cli_main.DEFAULT_CONFIG,
        results_dir=str(work_dir / "results"),
        data_cache_dir=str(work_dir / "cache"),
    )
    cli_main.get_user_selections = _selections
    cli_main.TradingAgentsGraph = _FakeTradingAgentsGraph
    cli_main.create_run_display = lambda *args, **kwargs: _NoOpDisplay()
    cli_main.authoritative_snapshot_run = _snapshot_run

    result = CliRunner().invoke(cli_main.app, ["analyze", *sys.argv[1:]])
    if not isinstance(result.exception, _ProbeStop):
        observed["unexpected_exit_code"] = result.exit_code
        observed["unexpected_exception"] = repr(result.exception)
    output_path.write_text(json.dumps(observed, sort_keys=True), encoding="utf-8")
    return 0 if isinstance(result.exception, _ProbeStop) else 1


if __name__ == "__main__":
    raise SystemExit(main())
