"""Real client serialization/transport hooks; only external reply and clocks replaced."""

import asyncio
import json
import sqlite3
import threading

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import APITimeoutError
from test_deepseek_high_budget import budget, client, response

from tradingagents.analysis_budget import AnalysisBudget, AnalysisBudgetStopped
from tradingagents.dataflows.market_snapshot import get_active_run_telemetry


def test_async_real_client_send_has_same_safe_metadata_and_no_retry(monkeypatch):
    calls = []

    async def wire(transport, request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    with client(monkeypatch, [], timeout=15) as (llm, _), budget() as counter:
        ledger = get_active_run_telemetry()
        asyncio.run(
            llm.ainvoke(
                [HumanMessage(content="Synthetic")],
                config={"metadata": {"langgraph_node": "Market Analyst"}},
            )
        )
        record = ledger.finalize(terminal_route="analysis_outcome").model_transport.attempts[0]
        assert record.send_limits.graph_node == "Market Analyst"
        assert record.send_limits.stage == "analysis"
        assert counter.receipt()["model_attempts"] == len(calls) == 1
        assert record.configured_max_retries == 0 and calls[0]["max_tokens"] == 65536


def test_historical_transport_record_retains_unknown_timing():
    from tradingagents.run_telemetry import ModelTransportAttempt

    old = ModelTransportAttempt(
        contract_version="1.0",
        logical_call_id="model-call-000001",
        attempt_ordinal=0,
        configured_max_retries=0,
        provider_family="deepseek",
        model_id="deepseek-flash",
        endpoint_host="api.deepseek.com",
        elapsed_seconds=10,
        disposition="success",
        http_status=200,
    )
    assert old.send_limits is None
    assert ModelTransportAttempt.model_validate(old.model_dump(mode="json")).send_limits is None
    with pytest.raises(ValueError, match="historical transport records"):
        ModelTransportAttempt.model_validate(
            {**old.model_dump(), "send_limits": {"stage": "analysis"}}
        )


@pytest.mark.parametrize("failure", [False, True])
def test_actual_send_records_timeout_remaining_and_known_node(monkeypatch, tmp_path, failure):
    reply = httpx.ReadTimeout("secret-body-must-not-appear") if failure else response()
    with (
        client(monkeypatch, [reply], timeout=httpx.Timeout(15, connect=3)) as (llm, calls),
        budget() as counter,
    ):
        ledger = get_active_run_telemetry()
        try:
            llm.invoke(
                [HumanMessage(content="Synthetic")],
                config={"metadata": {"langgraph_node": "Conservative Analyst"}},
            )
        except APITimeoutError:
            assert failure
        projection = ledger.finalize(
            terminal_route="operational_failure" if failure else "analysis_outcome"
        )
        record = projection.model_transport.attempts[0]
        assert record.send_limits.httpx_timeout_seconds == {
            "connect": 3.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 15.0,
        }
        assert 0 < record.send_limits.budget_remaining_seconds <= 600
        assert record.send_limits.graph_node == "Conservative Analyst"
        assert record.send_limits.stage == "risk_debate"
        assert counter.receipt()["model_attempts"] == len(calls) == 1
        assert calls[0]["max_tokens"] == 65536 and calls[0]["reasoning_effort"] == "high"
        encoded = json.dumps(record.model_dump(mode="json"))
        assert "secret-body" not in encoded and "public-synthetic" not in encoded
        from cli.runtime_artifacts import RuntimeArtifactWriter

        metrics = tmp_path / "runtime_metrics.json"
        writer = RuntimeArtifactWriter(
            artifact_root=tmp_path / "artifacts",
            log_paths=[tmp_path / "message_tool.log"],
            metrics_path=metrics,
        )
        writer.record_terminal_summary(
            terminal_route=projection.terminal_route,
            run_telemetry=projection.model_dump(mode="json"),
        )
        writer.close()
        persisted = json.loads(metrics.read_text())["terminal"]["model_transport"]["attempts"][0]
        assert persisted["send_limits"] == record.send_limits.model_dump(mode="json")
        assert "secret-body" not in metrics.read_text()


def test_shared_absolute_deadline_counts_preparation_and_does_not_reset_on_binding():
    mono, wall = [100.0], [1000.0]
    b = AnalysisBudget(
        60,
        1200,
        clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
        session_deadline_epoch=1100,
        session_monotonic_deadline=200,
    )
    mono[0] += 25
    wall[0] += 25
    con = sqlite3.connect(":memory:")
    try:
        b.bind_checkpoint(
            con, threading.RLock(), "synthetic-session", "fixed-policy", has_checkpoint=False
        )
        assert b._deadline == 1100 and b.check() == 75
        assert b.admit("model") == 75
        assert b.receipt()["model_attempts"] == 1
        mono[0] += 75
        wall[0] += 75
        with pytest.raises(AnalysisBudgetStopped, match="deadline_exceeded"):
            b.admit("model")
        assert b.receipt()["model_attempts"] == 1
    finally:
        con.close()


def test_absolute_deadline_clock_rollback_cannot_extend_monotonic_budget():
    mono, wall = [100.0], [1000.0]
    b = AnalysisBudget(
        60,
        1200,
        clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
        session_deadline_epoch=1100,
        session_monotonic_deadline=200,
    )
    mono[0] += 101
    wall[0] -= 100
    with pytest.raises(AnalysisBudgetStopped, match="deadline_exceeded"):
        b.admit("model")
    assert b.receipt()["model_attempts"] == 0


@pytest.mark.parametrize("delay,remaining", [(0, 1), (0.75, 0.25), (1, 0), (2, 0)])
def test_handoff_before_budget_initialization_cannot_renew_shared_deadline(delay, remaining):
    # Parent had one second left. The wall clock moves backwards during handoff;
    # the same-machine monotonic deadline must survive receiver initialization.
    b = AnalysisBudget(
        60,
        1200,
        clock=lambda: 1199 + delay,
        wall_clock=lambda: 1900 + delay,
        session_deadline_epoch=2200,
        session_monotonic_deadline=1200,
    )
    if remaining:
        assert b.admit("model") == remaining
        assert b.receipt()["model_attempts"] == 1
    else:
        with pytest.raises(AnalysisBudgetStopped, match="deadline_exceeded"):
            b.admit("model")
        assert b.receipt()["model_attempts"] == 0


@pytest.mark.parametrize(
    "epoch,monotonic", [(2200, None), (None, 1200), (2200, float("inf")), (2200, True)]
)
def test_shared_deadline_requires_both_finite_clock_bounds(epoch, monotonic):
    from tradingagents.analysis_budget import inspect_budget_config

    with pytest.raises(ValueError, match="session.*deadline"):
        AnalysisBudget(60, 1200, session_deadline_epoch=epoch, session_monotonic_deadline=monotonic)
    issues = inspect_budget_config(
        {
            "analysis_budget_enabled": True,
            "market_history_mode": "current",
            "llm_provider": "deepseek",
            "analysis_session_deadline_epoch": epoch,
            "analysis_session_monotonic_deadline": monotonic,
        }
    )
    assert any("session" in field for field, _ in issues)


def test_default_budget_remains_600_and_old_checkpoint_policy_unchanged():
    b = AnalysisBudget(60, 600, clock=lambda: 0, wall_clock=lambda: 1000)
    assert b.check() == 600
    con = sqlite3.connect(":memory:")
    try:
        b.bind_checkpoint(
            con, threading.RLock(), "old-compatible", "fixed-policy", has_checkpoint=False
        )
        policy = json.loads(con.execute("select policy from analysis_budget_v1").fetchone()[0])
        assert "session_deadline_epoch" not in policy and b._deadline == 1600
        assert "session_monotonic_deadline" not in policy
    finally:
        con.close()


def test_new_deadline_cannot_reopen_existing_budget_with_more_time():
    con = sqlite3.connect(":memory:")
    try:
        first = AnalysisBudget(
            60,
            1200,
            clock=lambda: 0,
            wall_clock=lambda: 1000,
            session_deadline_epoch=1200,
            session_monotonic_deadline=200,
        )
        first.bind_checkpoint(con, threading.RLock(), "same-session", "fixed", has_checkpoint=False)
        first.admit("model")
        second = AnalysisBudget(
            60,
            1200,
            clock=lambda: 0,
            wall_clock=lambda: 1010,
            session_deadline_epoch=1300,
            session_monotonic_deadline=290,
        )
        with pytest.raises(AnalysisBudgetStopped, match="checkpoint_budget_mismatch"):
            second.bind_checkpoint(
                con, threading.RLock(), "same-session", "fixed", has_checkpoint=True
            )
        state = json.loads(con.execute("select state from analysis_budget_v1").fetchone()[0])
        assert state["counts"]["model"] == 1 and state["deadline"] == 1200
    finally:
        con.close()


def test_config_scope_preserves_original_monotonic_deadline_in_durable_policy():
    import time

    from tradingagents.analysis_budget import analysis_budget_scope

    epoch, monotonic = time.time() + 1200, time.monotonic() + 1200
    config = {
        "analysis_budget_enabled": True,
        "analysis_max_calls": 60,
        "analysis_timeout_seconds": 1200,
        "market_history_mode": "current",
        "llm_provider": "deepseek",
        "llm_max_retries": 0,
        "analysis_session_deadline_epoch": epoch,
        "analysis_session_monotonic_deadline": monotonic,
    }
    con = sqlite3.connect(":memory:")
    try:
        with analysis_budget_scope(config) as counter:
            assert counter._session_deadline_epoch == epoch
            assert counter._session_monotonic_deadline == monotonic
            counter.bind_checkpoint(con, threading.RLock(), "shared", "fixed", has_checkpoint=False)
            policy = json.loads(con.execute("select policy from analysis_budget_v1").fetchone()[0])
            assert policy["session_monotonic_deadline"] == monotonic
            assert policy["session_deadline_epoch"] == epoch
    finally:
        con.close()


def test_unknown_node_and_arbitrary_extension_text_is_not_retained(monkeypatch):
    with client(monkeypatch, [response()]) as (llm, calls), budget():
        ledger = get_active_run_telemetry()
        llm.invoke(
            [HumanMessage(content="Synthetic")],
            config={"metadata": {"langgraph_node": "Conservative Analyst secret-key-123"}},
        )
        record = ledger.finalize(terminal_route="analysis_outcome").model_transport.attempts[0]
        assert record.send_limits.graph_node is None and record.send_limits.stage is None
        assert "secret-key" not in record.model_dump_json()
        assert len(calls) == 1


def test_small_budget_records_the_actually_clipped_timeout(monkeypatch):
    from tradingagents.analysis_budget import analysis_budget_scope
    from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run

    config = {
        "llm_provider": "deepseek",
        "analysis_budget_enabled": True,
        "analysis_max_calls": 1,
        "analysis_timeout_seconds": 5,
        "llm_max_output_tokens": 65536,
        "checkpoint_enabled": False,
        "market_history_mode": "current",
        "mainland_capability_routing_mode": "legacy",
    }
    with (
        client(monkeypatch, [response()], timeout=httpx.Timeout(60)) as (llm, calls),
        authoritative_snapshot_run(),
        analysis_budget_scope(config),
    ):
        ledger = get_active_run_telemetry()
        llm.invoke([HumanMessage(content="Synthetic")])
        limits = (
            ledger.finalize(terminal_route="analysis_outcome")
            .model_transport.attempts[0]
            .send_limits
        )
        assert all(0 < v <= 5 for v in limits.httpx_timeout_seconds.values())
        assert set(limits.httpx_timeout_seconds.values()) == {limits.budget_remaining_seconds}
        assert len(calls) == 1
