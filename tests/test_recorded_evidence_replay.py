import json
from datetime import date
from hashlib import sha256

import pytest
from typer.testing import CliRunner

from cli.main import app
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceState,
    SourceAcquisitionUnavailable,
)
from tradingagents.recorded_replay import (
    DEFAULT_RECORDED_FIXTURE_MANIFEST,
    load_recorded_run_fixtures,
    replay_recorded_run,
)
from tradingagents.terminal_contract import (
    RunLifecycleStatus,
    TerminalContract,
    TerminalOutcomeKind,
)


def _fixture_for(ticker: str):
    return next(
        fixture
        for fixture in load_recorded_run_fixtures()
        if fixture.recorded.run.ticker == ticker
    )


@pytest.mark.integration
def test_recorded_run_loader_verifies_content_addresses_and_preserves_raw_inputs():
    fixtures = load_recorded_run_fixtures(DEFAULT_RECORDED_FIXTURE_MANIFEST)

    assert {fixture.recorded.run.ticker for fixture in fixtures} == {
        "510500.SS",
        "600895.SS",
        "601658.SS",
    }
    assert {
        fixture.recorded.run.requested_date for fixture in fixtures
    } == {date(2026, 7, 19)}
    assert {
        fixture.recorded.run.ticker: len(
            fixture.recorded.evidence_state.source_artifacts
        )
        for fixture in fixtures
    } == {
        "510500.SS": 9,
        "600895.SS": 2,
        "601658.SS": 5,
    }
    assert {
        fixture.recorded.run.ticker: len(
            fixture.recorded.evidence_state.source_facts
        )
        for fixture in fixtures
    } == {
        "510500.SS": 13,
        "600895.SS": 15,
        "601658.SS": 32,
    }

    for fixture in fixtures:
        assert fixture.path.name == f"{fixture.content_sha256}.json"
        assert sha256(fixture.raw_bytes).hexdigest() == fixture.content_sha256
        assert b'"expected"' not in fixture.raw_bytes
        assert fixture.recorded.evidence_state.acquisition_outcomes
        assert fixture.expectation.fixture_sha256 == fixture.content_sha256
        assert fixture.expectation.ticker == fixture.recorded.run.ticker
        assert (
            fixture.expectation.requested_date
            == fixture.recorded.run.requested_date
        )


@pytest.mark.integration
def test_recorded_600895_run_fails_closed_before_any_model_or_directional_write(
    tmp_path,
):
    fixture = _fixture_for("600895.SS")

    result = replay_recorded_run(fixture, tmp_path)

    assert result.terminal.lifecycle_status is RunLifecycleStatus.COMPLETED
    assert (
        result.terminal.terminal_outcome_kind
        is TerminalOutcomeKind.ANALYSIS_OUTCOME
    )
    outcome = AnalysisOutcome.model_validate(
        result.final_state["analysis_outcome_contract"]
    )
    assert outcome.reason is AnalysisOutcomeReason.PREFLIGHT_BLOCKED
    assert outcome.diagnostic_codes == (
        AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,
    )
    assert result.counters.model_calls == 0
    assert result.counters.admission_gate_calls == 0
    assert result.counters.direction_selector_calls == 0
    assert result.counters.decision_gate_calls == 0
    assert result.counters.decision_writes == 0
    assert result.counters.signal_writes == 0
    assert result.counters.position_writes == 0
    assert result.counters.memory_writes == 0
    for forbidden in (
        "trading_decision",
        "final_trade_decision",
        "signal",
        "position",
    ):
        assert forbidden not in result.final_state

    assert result.report_path.is_file()
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["schema_version"] == "4.0"
    assert audit["run"]["ticker"] == "600895.SS"
    assert audit["run"]["trade_date"] == "2026-07-19"
    assert audit["terminal"]["terminal_outcome_kind"] == "analysis_outcome"
    assert audit["trading_decision"] is None
    assert audit["telemetry"]["acquisition"]["summary"]["attempts"] > 0
    assert audit["telemetry"]["acquisition"]["summary"]["attempts"] == len(
        audit["telemetry"]["acquisition"]["events"]
    )


@pytest.mark.integration
def test_recorded_601658_run_preserves_distinct_facts_and_blocks_before_selection(
    tmp_path,
):
    fixture = _fixture_for("601658.SS")
    facts = {
        fact.fact_id: fact.raw_text
        for fact in fixture.recorded.evidence_state.source_facts
    }

    assert fixture.recorded.legacy_selection is not None
    assert fixture.recorded.legacy_selection.rating == "Buy"
    assert facts[
        "fact:3e57bc313dbc7d96353b7cfd16efae062684fd8d464ddda53466c287033cc34e"
    ] == "0.592067"
    assert facts[
        "fact:a2e9e53c68fd6f1b0151ddaf3aa5e7a462575ce6149f4e7ce4235d477d1bed23"
    ] == "0.386"
    assert facts[
        "fact:95a15bb6d085f20d3650db4cdeaca1588e9220298ca66b966adaee987a67d994"
    ] == "macdh | 0.02"

    result = replay_recorded_run(fixture, tmp_path)

    assert result.blocked_stage == fixture.expectation.expected_blocked_stage
    assert result.blocked_stage == "preflight"
    assert result.counters.preflight_gate_calls == 1
    assert result.counters.admission_gate_calls == 0
    assert result.counters.direction_selector_calls == 0
    assert result.counters.decision_gate_calls == 0
    assert result.counters.model_calls == 0
    assert result.counters.decision_writes == 0
    assert result.terminal.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME
    assert result.report_path.is_file()
    assert result.audit_path.is_file()


@pytest.mark.integration
def test_recorded_510500_run_normalizes_rate_limit_and_insufficient_history(
    tmp_path,
):
    fixture = _fixture_for("510500.SS")
    recorded_evidence = fixture.recorded.evidence_state

    assert recorded_evidence.instrument_identity is None
    assert recorded_evidence.market_snapshot is not None
    assert recorded_evidence.market_snapshot.history_rows == 129
    assert any(
        "Too Many Requests" in fact.raw_text
        for fact in recorded_evidence.source_facts
    )

    result = replay_recorded_run(fixture, tmp_path)
    replayed_evidence = EvidenceState.model_validate(
        result.final_state["evidence_state"]
    )
    unavailable = tuple(
        outcome
        for outcome in replayed_evidence.acquisition_outcomes
        if isinstance(outcome, SourceAcquisitionUnavailable)
    )

    assert not any(
        "Too Many Requests" in fact.raw_text
        for fact in replayed_evidence.source_facts
    )
    assert any(
        outcome.reason is AcquisitionUnavailableReason.RATE_LIMITED
        and outcome.http_status == 429
        for outcome in unavailable
    )
    history_outcome = next(
        outcome
        for outcome in unavailable
        if outcome.reason is AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
    )
    assert history_outcome.calculation_readiness is not None
    assert history_outcome.calculation_readiness.available_observations == 129
    assert history_outcome.calculation_readiness.required_observations == 200
    assert result.blocked_stage == "preflight"
    assert result.counters.model_calls == 0
    assert result.counters.admission_gate_calls == 0
    assert result.counters.direction_selector_calls == 0
    assert result.counters.decision_writes == 0
    assert result.counters.memory_writes == 0
    assert result.terminal.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME
    assert result.report_path.is_file()
    assert result.audit_path.is_file()


@pytest.mark.integration
def test_recorded_fixture_cli_matches_programmatic_terminal_contract(tmp_path):
    fixture = _fixture_for("510500.SS")
    programmatic = replay_recorded_run(fixture, tmp_path / "programmatic")

    cli_output = tmp_path / "cli"
    invoked = CliRunner().invoke(
        app,
        [
            "replay-recorded",
            "510500.SS",
            "--manifest",
            str(DEFAULT_RECORDED_FIXTURE_MANIFEST),
            "--output-directory",
            str(cli_output),
        ],
    )

    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.stdout)
    cli_terminal = TerminalContract.model_validate(payload["terminal_contract"])
    assert cli_terminal == programmatic.terminal
    assert payload["blocked_stage"] == programmatic.blocked_stage
    assert payload["counters"] == programmatic.counters.model_dump(mode="json")
    assert (cli_output / "complete_report.md").is_file()
    assert (cli_output / "decision-audit.json").is_file()
