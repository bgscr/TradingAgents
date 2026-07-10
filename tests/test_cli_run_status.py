import json
from types import SimpleNamespace

import pytest

from cli import main as cli_main


def _selections():
    return {
        "ticker": "688519.SS",
        "analysis_date": "2026-07-01",
        "asset_type": "stock",
        "analysts": ["market", "social"],
        "china_a_enhancement_preset": "all",
    }


def _run_selections():
    selections = _selections()
    selections.update(
        {
            "analysts": [
                SimpleNamespace(value="market"),
                SimpleNamespace(value="social"),
            ],
            "research_depth": 1,
            "shallow_thinker": "gpt-5-mini",
            "deep_thinker": "gpt-5",
            "backend_url": None,
            "llm_provider": "openai",
            "google_thinking_level": None,
            "openai_reasoning_effort": None,
            "anthropic_effort": None,
            "output_language": "English",
        }
    )
    return selections


class NoOpDisplay:
    def __init__(self):
        self.closed = False

    def start(self):
        return None

    def refresh(self, spinner_text=None):
        return None

    def publish_event(self, event):
        return None

    def report_ready(self, section_name, content, path):
        return None

    def close(self):
        self.closed = True


@pytest.mark.unit
def test_prepare_run_artifacts_writes_running_status(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    status_path = artifacts["status_file"]
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["ticker"] == "688519.SS"
    assert payload["analysis_date"] == "2026-07-01"
    assert payload["selected_analysts"] == ["market", "social"]
    assert payload["status"] == "running"
    assert payload["current_phase"] == "artifacts_prepared"
    assert payload["completed_at"] is None
    assert payload["error_summary"] is None
    assert payload["reports_written"] == []


@pytest.mark.unit
def test_update_run_status_marks_completed(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._update_run_status(
        artifacts,
        status="completed",
        current_phase="report_writing",
        reports_written=["reports/complete_report.md"],
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["current_phase"] == "report_writing"
    assert payload["completed_at"] is not None
    assert payload["reports_written"] == ["reports/complete_report.md"]


@pytest.mark.unit
def test_mark_run_failed_records_error_summary(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._mark_run_failed(
        artifacts,
        RuntimeError("stream stopped"),
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_phase"] == "graph_stream"
    assert payload["completed_at"] is None
    assert payload["error_summary"] == "RuntimeError: stream stopped"
    assert "Run failed during graph_stream: RuntimeError: stream stopped" in artifacts[
        "log_file"
    ].read_text(encoding="utf-8")
    assert artifacts["latest_log_file"].read_text(encoding="utf-8") == artifacts[
        "log_file"
    ].read_text(encoding="utf-8")


@pytest.mark.unit
def test_run_analysis_marks_failed_when_graph_initialization_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )

    def fail_graph_init(*args, **kwargs):
        raise RuntimeError("graph boot failed")

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", fail_graph_init)

    with pytest.raises(RuntimeError, match="graph boot failed"):
        cli_main.run_analysis()

    status_files = list(tmp_path.rglob("run_status.json"))
    assert len(status_files) == 1
    payload = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_phase"] == "graph_initializing"
    assert payload["error_summary"] == "RuntimeError: graph boot failed"


@pytest.mark.unit
def test_run_analysis_marks_failed_when_graph_stream_is_interrupted(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )

    class FakePropagator:
        def create_initial_state(self, *args, **kwargs):
            return {}

        def get_graph_args(self, *args, **kwargs):
            return {}

    class FakeStream:
        def stream(self, *args, **kwargs):
            raise KeyboardInterrupt("ctrl-c")
            yield {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        def resolve_instrument_context(self, *args, **kwargs):
            return "resolved identity"

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    display = NoOpDisplay()
    monkeypatch.setattr(cli_main, "create_run_display", lambda *args, **kwargs: display)

    with pytest.raises(KeyboardInterrupt, match="ctrl-c"):
        cli_main.run_analysis()

    status_files = list(tmp_path.rglob("run_status.json"))
    assert len(status_files) == 1
    payload = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_phase"] == "graph_stream"
    assert payload["error_summary"] == "KeyboardInterrupt: ctrl-c"
    assert display.closed is True
