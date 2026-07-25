import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import main as cli_main
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    SourceAcquisitionUnavailable,
)
from tradingagents.run_telemetry import RunTelemetryLedger


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
    assert payload["lifecycle_status"] == "running"
    assert payload["current_phase"] == "artifacts_prepared"
    assert payload["active_phase"] == "artifacts_prepared"
    assert payload["terminal_outcome_kind"] is None
    assert payload["completed_at"] is None
    assert payload["error_summary"] is None
    assert payload["error_diagnostics"] is None
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
        terminal_outcome_kind="analysis_outcome",
        evidence_integrity_status="insufficient",
        reports_written=["reports/complete_report.md"],
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["lifecycle_status"] == "completed"
    assert payload["current_phase"] is None
    assert payload["active_phase"] is None
    assert payload["terminal_outcome_kind"] == "analysis_outcome"
    assert payload["evidence_integrity_status"] == "insufficient"
    assert payload["completed_at"] is not None
    assert payload["terminal_at"] == payload["completed_at"]
    assert payload["reports_written"] == ["reports/complete_report.md"]


@pytest.mark.unit
def test_mark_run_completed_publishes_canonical_identity_after_reports(
    tmp_path,
    monkeypatch,
):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path), "evidence_gate_mode": "enforce"},
        _selections(),
    )
    report_file = artifacts["report_dir"] / "complete_report.md"

    def fake_save(final_state, ticker, report_dir):
        assert final_state["configuration_digest"] == artifacts["configuration_digest"]
        assert ticker == "688519.SS"
        assert report_dir == artifacts["report_dir"]
        final_state.update(
            {
                "lifecycle_status": "completed",
                "terminal_outcome_kind": "analysis_outcome",
                "evidence_integrity_status": "insufficient",
                "run_id": "run:" + "a" * 64,
                "decision_audit_sha256": "b" * 64,
            }
        )
        return report_file

    monkeypatch.setattr(cli_main, "save_report_to_disk", fake_save)
    final_state = {}
    cli_main._write_run_reports(final_state, "688519.SS", artifacts)

    interim = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert interim["status"] == "running"
    assert interim["completed_at"] is None
    assert interim["reports_written"] == [str(report_file)]

    cli_main._mark_run_completed(final_state, artifacts)

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["active_phase"] is None
    assert payload["canonical_run_id"] == "run:" + "a" * 64
    assert payload["audit_digest"] == "b" * 64
    assert payload["terminal_outcome_kind"] == "analysis_outcome"
    assert payload["evidence_integrity_status"] == "insufficient"


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
    assert payload["lifecycle_status"] == "failed"
    assert payload["current_phase"] is None
    assert payload["active_phase"] is None
    assert payload["failed_phase"] == "graph_stream"
    assert payload["terminal_outcome_kind"] == "operational_failure"
    assert payload["operational_error_category"] == "graph_execution"
    assert payload["completed_at"] is None
    assert payload["failed_at"] is not None
    assert payload["terminal_at"] == payload["failed_at"]
    assert payload["error_summary"] == "RuntimeError: stream stopped"
    assert "Run failed during graph_stream: RuntimeError: stream stopped" in artifacts[
        "log_file"
    ].read_text(encoding="utf-8")
    assert artifacts["latest_log_file"].read_text(encoding="utf-8") == artifacts[
        "log_file"
    ].read_text(encoding="utf-8")


@pytest.mark.unit
def test_mark_run_failed_records_sanitized_chained_error_diagnostics(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_PROVIDER_TOKEN", "known-environment-secret")
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    root_error = ConnectionError(
        "POST https://user:password@api.example.test/v1/orders?token=query-secret "
        f"failed in {Path.home() / 'private'}\n"
        "token=known-environment-secret"
    )
    failure = RuntimeError(
        "analysis wrapper failed; Authorization: Bearer header-secret"
    )
    failure.__cause__ = root_error

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    diagnostics = payload["error_diagnostics"]
    assert diagnostics["contract_version"] == "1.0"
    assert diagnostics["chain"] == [
        {
            "exception_type": "RuntimeError",
            "message": "analysis wrapper failed; Authorization: <redacted>",
            "message_truncated": False,
            "relationship": "outermost",
        },
        {
            "exception_type": "ConnectionError",
            "message": (
                f"POST api.example.test failed in {Path('<home>') / 'private'} "
                "token=<redacted>"
            ),
            "message_truncated": False,
            "relationship": "cause",
        },
    ]
    assert diagnostics["root_cause"] == diagnostics["chain"][-1]
    assert diagnostics["chain_truncated"] is False
    assert diagnostics["cycle_detected"] is False
    serialized = json.dumps(payload)
    for secret in (
        "password",
        "query-secret",
        "known-environment-secret",
        "header-secret",
        str(Path.home()),
        "/v1/orders",
    ):
        assert secret not in serialized


@pytest.mark.unit
def test_mark_run_failed_redacts_common_structured_secret_renderings(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failure = RuntimeError(
        'provider rejected Bearer standalone-secret; '
        '{"api_key": "json-key-secret", "password": "json-password-secret", '
        '"cookie": "session=json-cookie-secret"}'
    )

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    message = payload["error_diagnostics"]["root_cause"]["message"]
    assert message == (
        'provider rejected Bearer <redacted>; '
        '{"api_key": "<redacted>", "password": "<redacted>", '
        '"cookie": "<redacted>"}'
    )
    for secret in (
        "standalone-secret",
        "json-key-secret",
        "json-password-secret",
        "json-cookie-secret",
    ):
        assert secret not in json.dumps(payload)


@pytest.mark.unit
def test_mark_run_failed_redacts_prefixed_secret_fields(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failure = RuntimeError(
        '{"OPENAI_API_KEY": "openai-secret", '
        '"x-api-key": "header-key-secret", '
        '"github_token": "github-secret", '
        '"db_password": "database-secret", '
        '"private_key": "pem-secret", '
        '"AWS_SECRET_ACCESS_KEY": "aws-secret", '
        '"secret_key": "secret-key-value", '
        '"service_credentials": "credential-secret", '
        '"openaiApiKey": "camel-key-secret", '
        '"dbPassword": "camel-password-secret"}'
    )

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    message = payload["error_diagnostics"]["root_cause"]["message"]
    assert message == (
        '{"OPENAI_API_KEY": "<redacted>", '
        '"x-api-key": "<redacted>", '
        '"github_token": "<redacted>", '
        '"db_password": "<redacted>", '
        '"private_key": "<redacted>", '
        '"AWS_SECRET_ACCESS_KEY": "<redacted>", '
        '"secret_key": "<redacted>", '
        '"service_credentials": "<redacted>", '
        '"openaiApiKey": "<redacted>", '
        '"dbPassword": "<redacted>"}'
    )
    for secret in (
        "openai-secret",
        "header-key-secret",
        "github-secret",
        "database-secret",
        "pem-secret",
        "aws-secret",
        "secret-key-value",
        "credential-secret",
        "camel-key-secret",
        "camel-password-secret",
    ):
        assert secret not in json.dumps(payload)


@pytest.mark.unit
def test_mark_run_failed_redacts_unquoted_secret_values_with_whitespace(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failure = RuntimeError(
        "password=my secret phrase; "
        "private_key=-----BEGIN PRIVATE KEY-----\n"
        "pem-body-secret\n"
        "-----END PRIVATE KEY-----; provider failed"
    )

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    message = payload["error_diagnostics"]["root_cause"]["message"]
    assert message == "password=<redacted>; private_key=<redacted>; provider failed"
    serialized = json.dumps(payload)
    assert "my secret phrase" not in serialized
    assert "pem-body-secret" not in serialized


@pytest.mark.unit
def test_mark_run_failed_redacts_escaped_quotes_inside_secret_values(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failure = RuntimeError(
        '{"api_key": "prefix\\\"double-tail-secret", '
        "'password': 'prefix\\'single-tail-secret', "
        '"message": "failed"}'
    )

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    message = payload["error_diagnostics"]["root_cause"]["message"]
    assert message == (
        '{"api_key": "<redacted>", '
        "'password': '<redacted>', "
        '"message": "failed"}'
    )
    serialized = json.dumps(payload)
    assert "double-tail-secret" not in serialized
    assert "single-tail-secret" not in serialized


@pytest.mark.unit
def test_mark_run_failed_redacts_container_secret_values(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failure = RuntimeError(
        '{"credentials": ["prefix", "array-tail-secret"], '
        '"private_key": {"primary": ["prefix", "object-tail-secret"]}, '
        '"service_token": ("prefix", "tuple-tail-secret"), '
        '"message": "failed"}'
    )

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    message = payload["error_diagnostics"]["root_cause"]["message"]
    assert message == (
        '{"credentials": <redacted>, '
        '"private_key": <redacted>, '
        '"service_token": <redacted>, '
        '"message": "failed"}'
    )
    serialized = json.dumps(payload)
    assert "array-tail-secret" not in serialized
    assert "object-tail-secret" not in serialized
    assert "tuple-tail-secret" not in serialized


@pytest.mark.unit
def test_mark_run_failed_logs_sanitized_outer_and_root_cause(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    root_error = TimeoutError(
        "request to https://user:password@feed.example.test/private timed out"
    )
    failure = RuntimeError("analysis stream stopped")
    failure.__cause__ = root_error

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    log_text = artifacts["log_file"].read_text(encoding="utf-8")
    assert (
        "Run failed during graph_stream: RuntimeError: analysis stream stopped; "
        "root cause: TimeoutError: request to feed.example.test timed out"
    ) in log_text
    assert "/private" not in log_text
    assert "password" not in log_text
    assert artifacts["latest_log_file"].read_text(encoding="utf-8") == log_text


@pytest.mark.unit
def test_error_diagnostics_bound_implicit_context_chain_and_message_length(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    failures = [RuntimeError(f"level-{index}") for index in range(9)]
    failures.append(RuntimeError("x" * 600))
    for outer, context in zip(failures[:-1], failures[1:], strict=True):
        outer.__context__ = context

    cli_main._mark_run_failed(
        artifacts,
        failures[0],
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    diagnostics = payload["error_diagnostics"]
    assert len(diagnostics["chain"]) == 8
    assert [entry["message"] for entry in diagnostics["chain"][:-1]] == [
        f"level-{index}" for index in range(7)
    ]
    assert diagnostics["chain"][-1]["relationship"] == "context"
    assert diagnostics["chain"][-1]["message_truncated"] is True
    assert len(diagnostics["chain"][-1]["message"]) == 500
    assert diagnostics["chain"][-1]["message"].endswith("…")
    assert diagnostics["root_cause"] == diagnostics["chain"][-1]
    assert diagnostics["chain_truncated"] is True
    assert diagnostics["cycle_detected"] is False


@pytest.mark.unit
def test_error_diagnostics_detect_exception_chain_cycles(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    outer = RuntimeError("outer")
    inner = ValueError("inner")
    outer.__cause__ = inner
    inner.__cause__ = outer

    cli_main._mark_run_failed(
        artifacts,
        outer,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    diagnostics = payload["error_diagnostics"]
    assert [entry["exception_type"] for entry in diagnostics["chain"]] == [
        "RuntimeError",
        "ValueError",
    ]
    assert diagnostics["root_cause"] == diagnostics["chain"][-1]
    assert diagnostics["chain_truncated"] is False
    assert diagnostics["cycle_detected"] is True


@pytest.mark.unit
def test_error_diagnostics_do_not_follow_suppressed_context(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )
    hidden_context = ValueError("suppressed provider payload")
    failure = RuntimeError("public failure")
    failure.__context__ = hidden_context
    failure.__suppress_context__ = True

    cli_main._mark_run_failed(
        artifacts,
        failure,
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["error_diagnostics"]["chain"] == [
        {
            "exception_type": "RuntimeError",
            "message": "public failure",
            "message_truncated": False,
            "relationship": "outermost",
        }
    ]
    assert "suppressed provider payload" not in json.dumps(payload)


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
    assert payload["current_phase"] is None
    assert payload["failed_phase"] == "graph_initializing"
    assert payload["operational_error_category"] == "configuration"
    assert payload["error_summary"] == "RuntimeError: graph boot failed"


@pytest.mark.unit
def test_run_analysis_marks_failed_when_graph_stream_is_interrupted(
    tmp_path, monkeypatch
):
    snapshot_scope_events = []
    telemetry_ledger = RunTelemetryLedger()

    @contextmanager
    def snapshot_scope():
        snapshot_scope_events.append("entered")
        try:
            yield SimpleNamespace(telemetry_ledger=telemetry_ledger)
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
            telemetry_ledger.record_acquisition(
                SimpleNamespace(
                    tool_call_id="news-circuit",
                    tool_name="get_news",
                ),
                SourceAcquisitionUnavailable(
                    provider="news-primary",
                    capability="instrument_news",
                    source_ref="news:688519.SS",
                    attempt=1,
                    retrieved_at="2026-07-22T18:00:00Z",
                    retryable=False,
                    reason=AcquisitionUnavailableReason.CIRCUIT_OPEN,
                ),
            )
            raise KeyboardInterrupt("ctrl-c")
            yield {}

    class FakeTradingAgentsGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        def create_initial_state(self, *args, **kwargs):
            return self.propagator.create_initial_state(*args, **kwargs)

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
    assert payload["current_phase"] is None
    assert payload["failed_phase"] == "graph_stream"
    assert payload["operational_error_category"] == "graph_execution"
    assert payload["error_summary"] == "KeyboardInterrupt: ctrl-c"
    assert display.closed is True
    assert snapshot_scope_events == ["entered", "exited"]
    metrics = json.loads(
        next(tmp_path.rglob("runtime_metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["terminal"]["acquisition"]["attempts"] == 1
    assert metrics["terminal"]["acquisition"]["circuit_breaker_events"] == 1


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
