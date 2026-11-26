"""Checkpoint location is independent of data cache; all state is synthetic."""
import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from tradingagents import analysis_budget
from tradingagents.graph.checkpointer import checkpoint_step, get_checkpointer, thread_id


class State(TypedDict):
    value: int


def workflow():
    graph = StateGraph(State)
    graph.add_node("advance", lambda state: {"value": state["value"] + 1})
    graph.set_entry_point("advance")
    graph.add_edge("advance", END)
    return graph


def test_explicit_location_preserves_shared_cache_and_legacy_checkpoint(tmp_path):
    cache = tmp_path / "shared-cache"
    legacy = cache / "checkpoints/601328.SS.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"synthetic old checkpoint: never opened")
    financial = cache / "current-financial-statements.sqlite3"
    financial.write_bytes(b"synthetic cache: not moved")
    expected = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (legacy, financial)}
    config = {"data_cache_dir": str(cache), "checkpoint_data_dir": str(tmp_path / "session-state")}
    selected = analysis_budget.checkpoint_storage_directory(config)
    tid = thread_id("601328.SS", "2026-10-16", "synthetic")
    with get_checkpointer(selected, "601328.SS") as saver:
        result = workflow().compile(checkpointer=saver).invoke(
            {"value": 2}, {"configurable": {"thread_id": tid}})
    assert result == {"value": 3}
    assert checkpoint_step(selected, "601328.SS", "2026-10-16", "synthetic") == 1
    assert Path(selected, "checkpoints/601328.SS.db").is_file()
    assert config["data_cache_dir"] == str(cache)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in expected} == expected


def test_unset_option_preserves_legacy_location_and_budget_binding(tmp_path):
    config = {"data_cache_dir": str(tmp_path / "shared-cache")}
    assert analysis_budget.checkpoint_storage_directory(config) == config["data_cache_dir"]
    binding = analysis_budget.checkpoint_budget_binding(config, "unchanged-graph")
    config["checkpoint_data_dir"] = None
    assert analysis_budget.checkpoint_storage_directory(config) == config["data_cache_dir"]
    assert analysis_budget.checkpoint_budget_binding(config, "unchanged-graph") == binding


@pytest.mark.parametrize("value", ["", "relative", "../escape", "C:relative", " ", 1,
                                   True, [], "//server/share", "\\\\server\\share", "C:/bad\npath",
                                   pytest.param("/\\server/share", marks=pytest.mark.skipif(
                                       os.name != "nt", reason="Windows mixed-separator UNC")),
                                   "\\/server/share", "\\\\?\\C:\\device", "C:/file:stream"])
def test_invalid_explicit_location_fails_before_open(value):
    config = {"data_cache_dir": "unused", "checkpoint_data_dir": value}
    with pytest.raises(ValueError, match="must_be_absolute_local_path"):
        analysis_budget.checkpoint_storage_directory(config)
    assert ("checkpoint_data_dir", "must_be_absolute_local_path") in analysis_budget.inspect_budget_config(config)


def test_lexical_alias_has_same_binding_without_creating_directory(tmp_path):
    first = {"data_cache_dir": "cache", "checkpoint_data_dir": str(tmp_path / "state")}
    alias = dict(first, checkpoint_data_dir=str(tmp_path / "unused/../state"))
    assert analysis_budget.checkpoint_storage_directory(first) == os.path.normcase(str(tmp_path / "state"))
    assert analysis_budget.checkpoint_budget_binding(first, "g") == analysis_budget.checkpoint_budget_binding(alias, "g")
    assert not (tmp_path / "state").exists() and not (tmp_path / "unused").exists()


def test_environment_override_keeps_data_cache_separate(tmp_path, monkeypatch):
    from tradingagents.default_config import _apply_env_overrides
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_DATA_DIR", str(tmp_path / "session-state"))
    config = _apply_env_overrides({"checkpoint_data_dir": None, "data_cache_dir": "unchanged-cache"})
    assert config["checkpoint_data_dir"] == str(tmp_path / "session-state")
    assert config["data_cache_dir"] == "unchanged-cache"


def read_ledger(saver):
    policy, state = saver.conn.execute("SELECT policy, state FROM analysis_budget_v1 WHERE thread=?", ("same-thread",)).fetchone()
    return policy, json.loads(state)


def test_reopen_keeps_reservations_deadline_and_terminal_failure(tmp_path):
    config = {"data_cache_dir": str(tmp_path / "cache"), "checkpoint_data_dir": str(tmp_path / "session")}
    directory = analysis_budget.checkpoint_storage_directory(config)
    binding = analysis_budget.checkpoint_budget_binding(config, "same-graph")
    with get_checkpointer(directory, "601328.SS") as saver:
        budget = analysis_budget.AnalysisBudget(2, 600, checkpoint_required=True, wall_clock=lambda: 1000)
        budget.bind_checkpoint(saver.conn, saver.lock, "same-thread", binding, has_checkpoint=False)
        budget.admit("model")
        budget.record_interruption()
        initial = read_ledger(saver)
    with get_checkpointer(directory, "601328.SS") as saver:
        resumed = analysis_budget.AnalysisBudget(2, 600, checkpoint_required=True, wall_clock=lambda: 1010)
        resumed.bind_checkpoint(saver.conn, saver.lock, "same-thread", binding, has_checkpoint=False)
        assert resumed.receipt()["admitted_attempts"] == 1
        resumed.admit("data")
        with pytest.raises(analysis_budget.AnalysisBudgetStopped, match="call_limit_exceeded"):
            resumed.admit("model")
        final = read_ledger(saver)
        assert final[0] == initial[0]
        assert final[1]["deadline"] == initial[1]["deadline"] == 1600
        assert final[1]["counts"] == {"model": 1, "data": 1}
        assert final[1]["last_failure"] == "interrupted"
    with get_checkpointer(directory, "601328.SS") as saver:
        stopped = analysis_budget.AnalysisBudget(2, 600, checkpoint_required=True, wall_clock=lambda: 1020)
        with pytest.raises(analysis_budget.AnalysisBudgetStopped, match="call_limit_exceeded"):
            stopped.bind_checkpoint(saver.conn, saver.lock, "same-thread", binding, has_checkpoint=False)
        assert read_ledger(saver)[1]["counts"] == final[1]["counts"]


def test_copied_database_rejects_new_directory_policy_without_reset(tmp_path):
    first = {"data_cache_dir": str(tmp_path / "cache"), "checkpoint_data_dir": str(tmp_path / "first")}
    second = dict(first, checkpoint_data_dir=str(tmp_path / "second"))
    with get_checkpointer(analysis_budget.checkpoint_storage_directory(first), "601328.SS") as saver:
        budget = analysis_budget.AnalysisBudget(2, 600, wall_clock=lambda: 1000)
        budget.bind_checkpoint(saver.conn, saver.lock, "same-thread", analysis_budget.checkpoint_budget_binding(first, "g"), has_checkpoint=False)
        budget.admit("data")
        before = read_ledger(saver)
    source = Path(first["checkpoint_data_dir"]) / "checkpoints/601328.SS.db"
    target = Path(second["checkpoint_data_dir"]) / "checkpoints/601328.SS.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())  # Only this test's closed synthetic DB.
    with get_checkpointer(analysis_budget.checkpoint_storage_directory(second), "601328.SS") as saver:
        moved = analysis_budget.AnalysisBudget(2, 600, wall_clock=lambda: 1010)
        with pytest.raises(analysis_budget.AnalysisBudgetStopped, match="checkpoint_budget_mismatch"):
            moved.bind_checkpoint(saver.conn, saver.lock, "same-thread", analysis_budget.checkpoint_budget_binding(second, "g"), has_checkpoint=False)
        assert read_ledger(saver) == before

