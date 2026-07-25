"""Test checkpoint resume: crash mid-analysis, re-run resumes from last node."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)

# Mutable flag to simulate crash on first run
_should_crash = False


class _SimpleState(TypedDict):
    count: int


def _node_a(state: _SimpleState) -> dict:
    return {"count": state["count"] + 1}


def _node_b(state: _SimpleState) -> dict:
    if _should_crash:
        raise RuntimeError("simulated mid-analysis crash")
    return {"count": state["count"] + 10}


def _build_graph() -> StateGraph:
    builder = StateGraph(_SimpleState)
    builder.add_node("analyst", _node_a)
    builder.add_node("trader", _node_b)
    builder.set_entry_point("analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


class TestCheckpointResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_crash_and_resume(self):
        """Crash at 'trader' node, then resume from checkpoint."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Run 1: crash at trader node
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        # Checkpoint should exist at step 1 (analyst completed)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))
        step = checkpoint_step(self.tmpdir, self.ticker, self.date)
        self.assertEqual(step, 1)

        # Run 2: resume — trader succeeds this time
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke(None, config=cfg)

        # analyst added 1, trader added 10 → 11
        self.assertEqual(result["count"], 11)

    def test_clear_checkpoint_allows_fresh_start(self):
        """After clearing, the graph starts from scratch."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # Create a checkpoint by crashing
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Clear it
        clear_checkpoint(self.tmpdir, self.ticker, self.date)
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # Fresh run succeeds from scratch
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config=cfg)

        self.assertEqual(result["count"], 11)

    def test_clear_checkpoint_surfaces_storage_failure(self):
        """A failed cleanup must not masquerade as a cleared checkpoint."""
        checkpoint_dir = Path(self.tmpdir) / "checkpoints"
        checkpoint_dir.mkdir()
        db_path = checkpoint_dir / f"{self.ticker}.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE writes (unexpected_column TEXT)")
            conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")

        with self.assertRaisesRegex(sqlite3.OperationalError, "thread_id"):
            clear_checkpoint(self.tmpdir, self.ticker, self.date)


    def test_different_date_starts_fresh(self):
        """A different date must NOT resume from an existing checkpoint."""
        global _should_crash
        builder = _build_graph()
        date2 = "2026-04-21"

        # Run with date1 — crash to leave a checkpoint
        _should_crash = True
        tid1 = thread_id(self.ticker, self.date)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # date2 should have no checkpoint
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, date2))

        # Run with date2 — should start fresh and succeed
        _should_crash = False
        tid2 = thread_id(self.ticker, date2)
        self.assertNotEqual(tid1, tid2)

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})

        # Fresh run: analyst +1, trader +10 = 11
        self.assertEqual(result["count"], 11)

        # Original date checkpoint still exists (untouched)
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

    def test_trading_graph_checkpoint_scope_resumes_and_clears_completed_run(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        events = []
        should_crash = True

        def analyst(state):
            events.append("analyst")
            return {"count": state["count"] + 1}

        def trader(state):
            events.append("trader")
            if should_crash:
                raise RuntimeError("simulated mid-analysis crash")
            return {"count": state["count"] + 10}

        workflow = StateGraph(_SimpleState)
        workflow.add_node("analyst", analyst)
        workflow.add_node("trader", trader)
        workflow.set_entry_point("analyst")
        workflow.add_edge("analyst", "trader")
        workflow.add_edge("trader", END)

        graph = object.__new__(TradingAgentsGraph)
        graph.config = {
            "checkpoint_enabled": True,
            "data_cache_dir": self.tmpdir,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
        }
        graph.selected_analysts = ("market",)
        graph.decision_policy = None
        graph.decision_horizon = None
        graph.workflow = workflow
        graph.graph = graph.workflow.compile()
        signature = graph._run_signature("stock")

        with graph.checkpoint_scope(self.ticker, self.date, "stock") as session:
            config = session.graph_config
            self.assertFalse(session.resume_from_checkpoint)
            self.assertEqual(
                config["configurable"]["thread_id"],
                thread_id(self.ticker, self.date, signature),
            )
            with self.assertRaises(RuntimeError):
                graph.graph.invoke({"count": 0}, config=config)

        self.assertTrue(
            has_checkpoint(self.tmpdir, self.ticker, self.date, signature)
        )
        self.assertEqual(events, ["analyst", "trader"])

        should_crash = False
        with graph.checkpoint_scope(self.ticker, self.date, "stock") as session:
            config = session.graph_config
            self.assertTrue(session.resume_from_checkpoint)
            graph_input = None if session.resume_from_checkpoint else {"count": 0}
            result = graph.graph.invoke(graph_input, config=config)
            graph.clear_run_checkpoint(self.ticker, self.date, "stock")

        self.assertEqual(result["count"], 11)
        self.assertEqual(events, ["analyst", "trader", "trader"])
        self.assertFalse(
            has_checkpoint(self.tmpdir, self.ticker, self.date, signature)
        )


class TestCheckpointSignature(unittest.TestCase):
    """A different graph shape (analyst selection / depth / asset mode) must not
    resume the previous run's checkpoint (#1089)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_empty_signature_is_legacy_id(self):
        self.assertEqual(
            thread_id(self.ticker, self.date),
            thread_id(self.ticker, self.date, ""),
        )

    def test_signature_changes_thread_id(self):
        legacy = thread_id(self.ticker, self.date)
        sig_a = thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        sig_b = thread_id(self.ticker, self.date, "analysts=market|asset=stock")
        self.assertNotEqual(sig_a, sig_b)          # different graph shapes differ
        self.assertNotEqual(legacy, sig_a)         # signature-keyed differs from legacy
        self.assertEqual(                          # same inputs are stable
            sig_a, thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        )

    def test_symbol_aliases_share_canonical_checkpoint_identity(self):
        alias = "BTCUSD"
        canonical = "BTC-USD"
        signature = "analysts=market|asset=crypto"
        builder = _build_graph()
        config = {
            "configurable": {
                "thread_id": thread_id(alias, self.date, signature),
            }
        }

        global _should_crash
        _should_crash = True
        with get_checkpointer(self.tmpdir, alias) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=config)

        self.assertEqual(
            thread_id(alias, self.date, signature),
            thread_id(canonical, self.date, signature),
        )
        self.assertTrue(
            has_checkpoint(self.tmpdir, canonical, self.date, signature)
        )

    def test_different_signature_starts_fresh(self):
        global _should_crash
        builder = _build_graph()
        sig1 = "analysts=market,news,fundamentals|asset=stock"
        sig2 = "analysts=market|asset=stock"       # dropped analysts -> different graph

        _should_crash = True
        tid1 = thread_id(self.ticker, self.date, sig1)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))
        # A different graph shape has no checkpoint to resume from.
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date, sig2))

        _should_crash = False
        tid2 = thread_id(self.ticker, self.date, sig2)
        self.assertNotEqual(tid1, tid2)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})
        self.assertEqual(result["count"], 11)
        # sig1's checkpoint remains untouched.
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))

    def test_run_signature_captures_graph_shape(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # Build a bare instance to exercise the pure helper without heavy __init__.
        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        base = g._run_signature("stock")
        self.assertIn("admission_binding=1", base)
        legacy = base.replace("|admission_binding=1", "")
        self.assertNotEqual(
            thread_id(self.ticker, self.date, legacy),
            thread_id(self.ticker, self.date, base),
        )

        self.assertNotEqual(base, g._run_signature("crypto"))     # asset mode
        g.selected_analysts = ("market",)
        self.assertNotEqual(base, g._run_signature("stock"))      # analyst selection
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 3, "max_risk_discuss_rounds": 1}
        self.assertNotEqual(base, g._run_signature("stock"))      # debate depth
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 5}
        self.assertNotEqual(base, g._run_signature("stock"))      # risk depth
        # Stable for identical inputs.
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        self.assertEqual(base, g._run_signature("stock"))

    def test_run_signature_captures_policy_and_horizon_contract(self):
        from tradingagents.decision_policy import DecisionHorizon, HorizonUnit
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market",)
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        g.decision_policy = type("Policy", (), {"registry_digest": "registry:a"})()
        g.decision_horizon = None
        without_horizon = g._run_signature("stock")

        g.decision_horizon = DecisionHorizon(count=5, unit=HorizonUnit.TRADING_DAYS)
        with_horizon = g._run_signature("stock")

        self.assertIn("evidence_schema=4", with_horizon)
        self.assertIn("decision_schema=1", with_horizon)
        self.assertIn("registry=registry:a", with_horizon)
        self.assertIn("horizon=5:trading_days", with_horizon)
        self.assertNotEqual(without_horizon, with_horizon)


if __name__ == "__main__":
    unittest.main()
