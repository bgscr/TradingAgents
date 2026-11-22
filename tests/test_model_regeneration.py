"""Synthetic production-send tests: real SDK, fake HTTP, no network or secrets."""
# ruff: noqa: SIM117 -- distinguish terminal-signal assertions from run lifecycle scopes.
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from openai import APIConnectionError, APIStatusError

from tests.test_analysis_budget import (
    clear_model_client_cache,
    config,
    httpx_families,
    install_network_denial,
)
from tradingagents.analysis_budget import (
    AnalysisBudget,
    AnalysisBudgetStopped,
    analysis_budget_scope,
)
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.llm_clients.openai_client import OpenAIClient

INCOMPLETE = "peer closed connection without sending complete message body (incomplete chunked read)"
SECRET = "synthetic-secret-must-not-be-retained"


@pytest.fixture
def run(monkeypatch):
    install_network_denial(monkeypatch)
    monkeypatch.setattr(os, "environ", {"DEEPSEEK_API_KEY": "synthetic", "NO_PROXY": "*"})
    clear_model_client_cache()

    @contextmanager
    def harness(actions, *, maximum=8, enabled=True, storage_failure=None):
        calls, closes, holder = [], [], {}

        class Connection(sqlite3.Connection):
            fail = None

            def execute(self, statement, *args, **kwargs):
                if self.fail and statement.startswith(self.fail):
                    raise sqlite3.OperationalError(SECRET)
                return super().execute(statement, *args, **kwargs)

            def rollback(self):
                if self.fail:
                    raise sqlite3.OperationalError(SECRET)
                return super().rollback()

        for module in httpx_families():
            def send(_transport, request, family=module):
                index = len(calls)
                calls.append(bytes(request.content))
                action = actions[index]
                if storage_failure == "claim":
                    connection.fail = "INSERT INTO analysis_model_regeneration_v2"
                elif storage_failure == "finish" and index == 1:
                    connection.fail = "UPDATE analysis_model_regeneration_v2"
                if action in {401, 403, 429, 500}:
                    return family.Response(action, request=request, json={"error": {"message": SECRET}})
                if action == "invalid_json":
                    return family.Response(200, request=request, content=b"not-json")
                if action == "success":
                    return family.Response(200, request=request, json={"id": "synthetic", "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "合成结果"}, "finish_reason": "stop"}]})

                class Broken(family.SyncByteStream):
                    def __iter__(self):
                        yield ('{"unknown":"' + SECRET).encode()
                        if action == "deadline":
                            holder["budget"]._started -= 120
                        if action == "cancel":
                            raise AnalysisBudgetStopped("synthetic_cancel")
                        if action == "timeout":
                            raise family.ReadTimeout(SECRET)
                        if action == "connection":
                            raise family.ConnectError(SECRET)
                        raise family.RemoteProtocolError(SECRET if action == "unknown" else INCOMPLETE)

                    def close(self):
                        closes.append(index)
                        if action == "cleanup":
                            raise RuntimeError(SECRET)

                return family.Response(500 if action == "status" else 200, request=request, stream=Broken())

            monkeypatch.setattr(module.HTTPTransport, "handle_request", send)

        llm = OpenAIClient("deepseek-flash", provider="deepseek", max_retries=0).get_llm()
        assert llm.root_client.max_retries == 0
        connection = sqlite3.connect(":memory:", check_same_thread=False, factory=Connection)
        try:
            with authoritative_snapshot_run() as snapshot, analysis_budget_scope(config(
                analysis_max_calls=maximum, checkpoint_enabled=True, llm_max_retries=0,
                analysis_model_regeneration_enabled=enabled)) as budget:
                holder["budget"] = budget
                with budget.checkpoint_session(connection, threading.Lock(), "synthetic-thread", "binding", has_checkpoint=False):
                    yield SimpleNamespace(llm=llm, budget=budget, snapshot=snapshot, calls=calls,
                                          closes=closes, connection=connection)
        finally:
            llm.root_client.close()
            connection.close()
            clear_model_client_cache()

    yield harness
    clear_model_client_cache()


def receipt(h, logical_call="model-call-000001"):
    row = h.connection.execute("SELECT state FROM analysis_model_regeneration_v2 WHERE logical_call=?",
                               (logical_call,)).fetchone()
    return None if row is None else json.loads(row[0])


def invoke(h):
    return h.llm.invoke("合成提示：600919.SH，截至2026-11-13；不执行任何工具。")


def test_exact_interruption_success_same_request_and_budget(run):
    with run(["incomplete", "success"]) as h:
        deadline = h.budget._deadline
        assert invoke(h).content == "合成结果"
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 2
        assert h.calls[0] == h.calls[1]
        assert h.closes == [0]
        value = receipt(h)
        assert value["first_session_model_count"] == 1
        assert value["final_session_model_count"] == 2
        assert value["deadline"] == h.budget._deadline == deadline
        assert value["outcome"] == "response_complete"
        assert value["failed_request_usage"] == "unknown"
        telemetry = h.snapshot.telemetry_ledger.finalize().model_dump(mode="json")
        assert telemetry["model_transport"]["summary"]["physical_attempts"] == 2
        assert SECRET not in json.dumps([value, telemetry, h.budget.receipt()])


@pytest.mark.parametrize("second", ["incomplete", "unknown"])
def test_second_failure_stops_and_keeps_first_receipt(run, second):
    with run(["incomplete", second]) as h:
        with pytest.raises(APIConnectionError):
            invoke(h)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 2
        assert h.closes == [0, 1]
        assert receipt(h)["first_error"] == "remote_protocol_incomplete_chunked"
        assert receipt(h)["outcome"] == "failed"
        assert SECRET not in json.dumps(receipt(h))


@pytest.mark.parametrize("failure", ["unknown", "timeout", "connection", "status", "cleanup"])
def test_non_target_and_cleanup_failure_never_resend(run, failure):
    with run([failure]) as h:
        with pytest.raises(APIConnectionError):
            invoke(h)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
        assert receipt(h) is None


@pytest.mark.parametrize("failure,maximum,reason", [
    ("incomplete", 1, "call_limit_exceeded"),
    ("deadline", 8, "deadline_exceeded"),
    ("cancel", 8, "synthetic_cancel"),
])
def test_budget_deadline_cancel_forbid_resend(run, failure, maximum, reason):
    with pytest.raises(AnalysisBudgetStopped, match=reason):
        with run([failure], maximum=maximum) as h:
            try:
                invoke(h)
            finally:
                assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
                assert receipt(h) is None


def test_two_logical_calls_recover_third_cannot_and_receipts_remain_distinct(run):
    with run(["incomplete", "success", "incomplete", "success", "incomplete"]) as h:
        assert invoke(h).content == "合成结果"
        first = receipt(h)
        assert invoke(h).content == "合成结果"
        second = receipt(h, "model-call-000002")
        assert second["first_session_model_count"] == 3
        assert second["final_session_model_count"] == 4
        assert second["failed_request_usage"] == "unknown"
        assert first["deadline"] == second["deadline"] == h.budget._deadline
        with pytest.raises(APIConnectionError):
            invoke(h)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 5
        assert h.calls[0] == h.calls[1] and h.calls[2] == h.calls[3]
        assert receipt(h) == first
        assert receipt(h, "model-call-000002") == second
        assert receipt(h, "model-call-000003") is None
        value = h.budget.receipt()
        assert value["model_regeneration"]["maximum"] == 2
        assert value["model_regeneration_decisions"][-1] == {
            "session_model_count": 5, "reason": "session_limit_reached", "response_closed": True}
        assert SECRET not in json.dumps(value)


def test_default_behavior_stays_zero_retry(run):
    with run(["incomplete"], enabled=False) as h:
        with pytest.raises(APIConnectionError):
            invoke(h)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
        assert "model_regeneration" not in h.budget.receipt()


def test_thread_context_uses_same_run_budget(run):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    with run(["incomplete", "success"]) as h:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(copy_context().run, invoke, h).result().content == "合成结果"
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 2
        assert receipt(h)["outcome"] == "response_complete"


@pytest.mark.parametrize("same_logical_call", [False, True])
def test_claim_is_atomic_across_threads_and_budget_instances(run, same_logical_call):
    from concurrent.futures import ThreadPoolExecutor

    with run([]) as h:
        # Claim-only synthetic contention exercises the journal, not a transport.
        h.budget.admit("model")
        peers = [AnalysisBudget(h.budget.max_calls, h.budget.timeout_seconds,
                                model_regeneration_enabled=True) for _ in range(3)]
        for peer in peers:
            peer.bind_checkpoint(h.connection, h.budget._journal[1], "synthetic-thread", "binding", has_checkpoint=True)
        ids = ["model-call-000001"] * 3 if same_logical_call else [f"model-call-{n:06d}" for n in range(1, 4)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            values = list(executor.map(lambda pair: pair[0].claim_model_regeneration(pair[1]), zip(peers, ids, strict=True)))
        assert sorted(values) == ([False, False, True] if same_logical_call else [False, True, True])
        assert h.budget.receipt()["model_attempts"] == 1
        assert h.calls == []


def test_same_logical_call_cannot_claim_again_after_second_failure(run):
    with run(["incomplete", "incomplete"]) as h:
        with pytest.raises(APIConnectionError):
            invoke(h)
        first = receipt(h)
        assert not h.budget.claim_model_regeneration("model-call-000001")
        assert receipt(h) == first
        assert len(h.calls) == 2
        assert h.budget.receipt()["model_regeneration_decisions"][-1]["reason"] == "logical_call_limit_reached"


@pytest.mark.parametrize("boundary", ["deadline", "budget", "cancel"])
def test_boundary_after_claim_prevents_send_without_refunding_opportunity(run, monkeypatch, boundary):
    with pytest.raises(AnalysisBudgetStopped):
        with run(["incomplete"], maximum=2) as h:
            original = h.budget.claim_model_regeneration

            def claim(logical_call):
                result = original(logical_call)
                assert result
                if boundary == "deadline":
                    h.budget._started -= 120
                elif boundary == "budget":
                    h.budget.admit("data")
                else:
                    h.budget.stop("synthetic_cancel")
                return result

            monkeypatch.setattr(h.budget, "claim_model_regeneration", claim)
            try:
                invoke(h)
            finally:
                assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
                assert receipt(h)["failed_request_usage"] == "unknown"
                assert receipt(h)["outcome"] == ("reserved" if boundary == "cancel" else "stopped")


def test_opportunities_are_atomic_between_independent_sqlite_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    database = tmp_path / "synthetic-budget.sqlite3"
    connections = [sqlite3.connect(database, check_same_thread=False) for _ in range(3)]
    try:
        budgets = [AnalysisBudget(8, 60, model_regeneration_enabled=True) for _ in connections]
        for index, (budget, connection) in enumerate(zip(budgets, connections, strict=True)):
            budget.bind_checkpoint(connection, threading.Lock(), "synthetic-thread", "binding", has_checkpoint=index > 0)
            if index == 0:
                budget.admit("model")
        barrier = threading.Barrier(3)

        def claim(index):
            barrier.wait()
            return budgets[index].claim_model_regeneration(f"model-call-{index + 1:06d}")

        with ThreadPoolExecutor(max_workers=3) as executor:
            assert sorted(executor.map(claim, range(3))) == [False, True, True]
        assert connections[0].execute("SELECT COUNT(*) FROM analysis_model_regeneration_v2").fetchone()[0] == 2
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("has_record", [False, True])
def test_old_policy_is_rejected_unchanged_without_new_allowance(has_record):
    with sqlite3.connect(":memory:") as connection:
        old = AnalysisBudget(8, 60, model_regeneration_enabled=True)
        old.bind_checkpoint(connection, threading.Lock(), "old-thread", "binding", has_checkpoint=False)
        old.admit("model")
        policy, state = connection.execute("SELECT policy, state FROM analysis_budget_v1").fetchone()
        policy = json.loads(policy)
        policy["model_regeneration"] = "post200-incomplete-chunked-once-v1"
        connection.execute("UPDATE analysis_budget_v1 SET policy=?", (json.dumps(policy, sort_keys=True, separators=(",", ":")),))
        connection.execute("DROP TABLE analysis_model_regeneration_v2")
        connection.execute("CREATE TABLE analysis_model_regeneration_v1 (thread TEXT PRIMARY KEY, state TEXT NOT NULL)")
        if has_record:
            connection.execute("INSERT INTO analysis_model_regeneration_v1 VALUES (?, ?)", ("old-thread", SECRET))
        connection.commit()
        before = list(connection.iterdump())
        new = AnalysisBudget(8, 60, model_regeneration_enabled=True)
        with pytest.raises(AnalysisBudgetStopped, match="checkpoint_budget_mismatch"):
            new.bind_checkpoint(connection, threading.Lock(), "old-thread", "binding", has_checkpoint=True)
        assert list(connection.iterdump()) == before
        assert new._journal is None
        assert connection.execute("SELECT state FROM analysis_budget_v1").fetchone()[0] == state


def test_legacy_record_denies_even_if_policy_was_replaced(run):
    with run([]) as h:
        h.connection.execute("CREATE TABLE analysis_model_regeneration_v1 (thread TEXT PRIMARY KEY, state TEXT NOT NULL)")
        h.connection.execute("INSERT INTO analysis_model_regeneration_v1 VALUES (?, ?)", ("synthetic-thread", SECRET))
        h.connection.commit()
        h.budget.admit("model")
        assert not h.budget.claim_model_regeneration("model-call-000001")
        assert h.connection.execute("SELECT state FROM analysis_model_regeneration_v1").fetchone()[0] == SECRET
        assert receipt(h) is None
        assert h.budget.receipt()["model_regeneration_decisions"][-1]["reason"] == "legacy_record_present"
        assert SECRET not in json.dumps(h.budget.receipt())


@pytest.mark.parametrize("failure,reason,closed", [
    ("cleanup", "response_close_failed", False),
    ("unknown", "non_target_failure", True),
    ("timeout", "non_target_failure", True),
])
def test_closed_diagnostic_does_not_replace_first_error_or_expose_text(run, failure, reason, closed):
    with run([failure]) as h:
        with pytest.raises(APIConnectionError) as error:
            invoke(h)
        assert type(error.value.__cause__).__name__ == ("ReadTimeout" if failure == "timeout" else "RemoteProtocolError")
        value = h.budget.receipt()
        assert value["model_regeneration_decisions"][-1] == {
            "session_model_count": 1, "reason": reason, "response_closed": closed}
        assert SECRET not in json.dumps(value)


@pytest.mark.parametrize("field", ["reason", "closed"])
def test_diagnostics_reject_uncontrolled_values(run, field):
    with run([]) as h:
        with pytest.raises(ValueError):
            h.budget.record_model_regeneration_decision(SECRET if field == "reason" else "admitted",
                                                        SECRET if field == "closed" else True)
        assert SECRET not in json.dumps(h.budget.receipt())


@pytest.mark.parametrize("failure", ["incomplete", "cancel", "budget"])
@pytest.mark.parametrize("cleanup", ["ok", "error", "cancel", "budget"])
def test_async_failure_closes_preserves_stop_and_never_regenerates(run, monkeypatch, failure, cleanup):
    from asyncio import CancelledError

    import sniffio

    with run([]) as h:
        responses = []
        errors = []
        for module in httpx_families():
            async def send(_transport, request, family=module):
                h.calls.append(bytes(request.content))

                class Broken(family.AsyncByteStream):
                    async def __aiter__(self):
                        yield ('{"unknown":"' + SECRET).encode()
                        error = (CancelledError(SECRET) if failure == "cancel" else
                                 AnalysisBudgetStopped("synthetic_cancel") if failure == "budget" else
                                 family.RemoteProtocolError(INCOMPLETE))
                        errors.append(error)
                        raise error

                    async def aclose(self):
                        h.closes.append(0)
                        if cleanup == "error":
                            raise RuntimeError(SECRET)
                        if cleanup == "cancel":
                            raise CancelledError(SECRET)
                        if cleanup == "budget":
                            raise AnalysisBudgetStopped("synthetic_cleanup_stop")

                response = family.Response(200, request=request, stream=Broken())
                responses.append(response)
                return response

            monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", send)

        async def invoke_once():
            try:
                await h.llm.root_async_client._client.post("https://api.deepseek.com/chat/completions",
                    json={"model": "deepseek-flash", "messages": [{"role": "user", "content": "合成输入"}]})
            finally:
                await h.llm.root_async_client.close()
                # Fallback fixture cleanup, after production's close observation.
                assert h.closes == [0]
                for response in responses:
                    await response.aclose()

        # As in test_analysis_budget, fake async I/O completes inline. Exercise
        # the factory-created production HTTPX transport (not the async graph)
        # without constructing a Windows socketpair or an SDK worker thread.
        activation = h.llm._activate_model_transport_call()
        coroutine = invoke_once()
        token = sniffio.current_async_library_cvar.set("asyncio")
        try:
            expected = (CancelledError if failure == "cancel" else
                        AnalysisBudgetStopped if failure == "budget" else
                        CancelledError if cleanup == "cancel" else
                        AnalysisBudgetStopped if cleanup == "budget" else
                        tuple(module.RemoteProtocolError for module in httpx_families()))
            with pytest.raises(expected) as stopped:
                coroutine.send(None)
            if failure == "incomplete" and cleanup in {"cancel", "budget"}:
                assert stopped.value.__cause__ is errors[0]
            else:
                assert stopped.value is errors[0]
        finally:
            coroutine.close()
            sniffio.current_async_library_cvar.reset(token)
            h.llm._deactivate_model_transport_call(activation)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
        assert receipt(h) is None
        assert SECRET not in json.dumps([h.budget.receipt(), h.snapshot.telemetry_ledger.finalize().model_dump(mode="json")])


def test_missing_thread_telemetry_context_forbids_recovery(run):
    from concurrent.futures import ThreadPoolExecutor

    with run(["incomplete"]) as h:
        with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(APIConnectionError):
            executor.submit(invoke, h).result()
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
        assert receipt(h) is None


@pytest.mark.parametrize("failure", [401, 403, 429, 500, "invalid_json"])
def test_complete_error_or_invalid_payload_never_regenerates(run, failure):
    with run([failure]) as h:
        # Without JSON media type this SDK returns text; its result converter
        # rejects it with AttributeError, after the complete physical response.
        expected = AttributeError if failure == "invalid_json" else APIStatusError
        with pytest.raises(expected):
            invoke(h)
        assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
        assert receipt(h) is None


def test_claim_storage_and_rollback_failure_forbid_second_send(run):
    with pytest.raises(AnalysisBudgetStopped, match="model_regeneration_persistence_failed"):
        with run(["incomplete"], storage_failure="claim") as h:
            try:
                invoke(h)
            finally:
                assert len(h.calls) == h.budget.receipt()["model_attempts"] == 1
                assert h.budget._fatal_reason == "model_regeneration_persistence_failed"


def test_finish_storage_and_rollback_failure_preserve_send_error(run):
    with pytest.raises(APIConnectionError) as stopped:
        with run(["incomplete", "unknown"], storage_failure="finish") as h:
            try:
                invoke(h)
            finally:
                assert len(h.calls) == h.budget.receipt()["model_attempts"] == 2
                assert h.budget._fatal_reason == "model_regeneration_persistence_failed"
                assert receipt(h)["outcome"] == "reserved"  # Last durable result, no invented success.
    assert type(stopped.value.__cause__).__name__ == "RemoteProtocolError"


def test_legacy_telemetry_cannot_claim_controlled_retry():
    from pydantic import ValidationError

    from tradingagents.run_telemetry import ModelTransportAttempt

    value = {"logical_call_id": "model-call-000001", "attempt_ordinal": 1, "configured_max_retries": 0,
             "retry_index": 0, "provider_family": "deepseek", "model_id": "synthetic", "endpoint_host": "api.deepseek.com",
             "elapsed_seconds": 1, "disposition": "success", "http_status": 200}
    with pytest.raises(ValidationError):
        ModelTransportAttempt(**value)
    with pytest.raises(ValidationError):
        ModelTransportAttempt(**value, controlled_regeneration=True)
    assert ModelTransportAttempt(**value, controlled_regeneration=True, contract_version="1.2")
