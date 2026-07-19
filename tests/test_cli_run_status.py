import json
from contextlib import contextmanager
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
def test_run_artifacts_record_sanitized_model_observability(tmp_path):
    config = {
        "results_dir": str(tmp_path),
        "llm_provider": "OpenAI",
        "quick_think_llm": "gpt-5-mini-2026-07-01",
        "deep_think_llm": "gpt-5-2026-07-01",
        "backend_url": "https://user"
        + ":secret@example.test:8443/v1?api_key=secret",
    }

    artifacts = cli_main._prepare_run_artifacts(config, _selections())
    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))

    assert payload["llm_provider"] == "openai"
    assert payload["quick_think_model"] == "gpt-5-mini-2026-07-01"
    assert payload["deep_think_model"] == "gpt-5-2026-07-01"
    assert payload["backend_url_host"] == "example.test"
    assert payload["structured_output_policy_version"] == "analyst_submission_evidence_v1"
    assert "secret" not in json.dumps(payload)


@pytest.mark.unit
def test_submission_observability_records_only_safe_reason_classes(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._record_analyst_submission_observability(
        artifacts,
        {
            "sources": [
                {
                    "source_id": "analyst.market.submission",
                    "status": "unavailable",
                    "required": True,
                    "detail": "validation_error: provider body must not persist",
                },
                {
                    "source_id": "analyst.news.submission",
                    "status": "available",
                    "required": True,
                    "detail": "finalized_structured",
                }
            ]
        },
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["analyst_submissions"] == {
        "market": {"status": "unavailable", "reason_class": "validation_error"},
        "news": {"status": "available", "mode": "finalized_structured"},
    }
    assert payload["structured_output"]["fallback_reason_classes"] == {
        "market": "validation_error"
    }
    assert "provider body" not in json.dumps(payload)


@pytest.mark.unit
def test_unavailable_submission_never_marks_analyst_completed_or_finished(tmp_path):
    buffer = cli_main.MessageBuffer()
    buffer.init_for_analysis(["market", "social"])
    tracker = cli_main.AnalystWallTimeTracker(
        cli_main.build_analyst_execution_plan(["market", "social"])
    )

    cli_main.update_analyst_statuses(
        buffer,
        {
            "market_report": "ANALYSIS_UNAVAILABLE: structured submission failed.",
            "evidence_state": {
                "sources": [
                    {
                        "source_id": "analyst.market.submission",
                        "status": "unavailable",
                        "required": True,
                        "detail": "validation_error",
                    }
                ]
            },
        },
        wall_time_tracker=tracker,
    )

    assert buffer.agent_status["Market Analyst"] == "failed"
    assert buffer.agent_status["Sentiment Analyst"] == "in_progress"
    assert "market" not in tracker.get_wall_times()

    # Later LangGraph chunks may omit evidence_state; the terminal submission
    # failure must remain authoritative over the accumulated report text.
    cli_main.update_analyst_statuses(
        buffer,
        {"investment_debate_state": {"bull_history": "draft"}},
        wall_time_tracker=tracker,
    )
    assert buffer.agent_status["Market Analyst"] == "failed"
    assert buffer.agent_status["Sentiment Analyst"] == "in_progress"

    cli_main._complete_non_analyst_agents(buffer, admission_blocked=True)
    assert buffer.agent_status["Market Analyst"] == "failed"
    assert buffer.agent_status["Bull Researcher"] == "skipped"


@pytest.mark.unit
def test_sentiment_submission_uses_social_wire_key_without_legacy_fallback():
    buffer = cli_main.MessageBuffer()
    buffer.init_for_analysis(["social"])
    tracker = cli_main.AnalystWallTimeTracker(
        cli_main.build_analyst_execution_plan(["social"])
    )

    cli_main.update_analyst_statuses(
        buffer,
        {
            "sentiment_report": "ANALYSIS_UNAVAILABLE: structured output unsupported.",
            "evidence_state": {
                "sources": [
                    {
                        "source_id": "analyst.sentiment.submission",
                        "status": "unavailable",
                        "required": True,
                        "detail": "unsupported",
                    }
                ]
            },
        },
        wall_time_tracker=tracker,
    )

    assert buffer.agent_status["Sentiment Analyst"] == "unavailable"
    assert "social" not in tracker.get_wall_times()


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
    snapshot_scope_events = []

    @contextmanager
    def snapshot_scope():
        snapshot_scope_events.append("entered")
        try:
            yield
        finally:
            snapshot_scope_events.append("exited")

    monkeypatch.setattr(cli_main, "authoritative_snapshot_run", snapshot_scope)
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

        def resolve_evidence_state(self, *args, **kwargs):
            return None

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
    assert snapshot_scope_events == ["entered", "exited"]


@pytest.mark.unit
def test_run_analysis_marks_failed_when_display_start_fails(tmp_path, monkeypatch):
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
            yield {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        def resolve_instrument_context(self, *args, **kwargs):
            return "resolved identity"

        def resolve_evidence_state(self, *args, **kwargs):
            return None

    class FailingDisplay(NoOpDisplay):
        def start(self):
            raise RuntimeError("display failed")

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *args, **kwargs: FailingDisplay(),
    )

    with pytest.raises(RuntimeError, match="display failed"):
        cli_main.run_analysis()

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    payload = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_summary"] == "RuntimeError: display failed"
    assert "Run failed during graph_initializing" in (
        run_dir / "message_tool.log"
    ).read_text(encoding="utf-8")
    assert (run_dir / "runtime_metrics.json").exists()


@pytest.mark.unit
def test_run_analysis_marks_failed_when_display_construction_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = SimpleNamespace()
            self.graph = SimpleNamespace()

    def fail_display(*args, **kwargs):
        raise RuntimeError("display construction failed")

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    monkeypatch.setattr(cli_main, "create_run_display", fail_display)

    with pytest.raises(RuntimeError, match="display construction failed"):
        cli_main.run_analysis()

    run_dir = next(tmp_path.rglob("run_status.json")).parent
    payload = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_summary"] == "RuntimeError: display construction failed"
    assert "Run failed during graph_initializing" in (
        run_dir / "message_tool.log"
    ).read_text(encoding="utf-8")
    assert (run_dir / "runtime_metrics.json").exists()
