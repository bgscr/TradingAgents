"""Synthetic budget-resume, dispatch and WAL contracts; no live services."""
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from tradingagents import budget_data, budget_readiness
from tradingagents.analysis_budget import (
    AnalysisBudget,
    AnalysisBudgetStopped,
    inspect_budget_config,
)
from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)


@contextmanager
def active_for_test(module, value):
    old = module._ACTIVE
    module._ACTIVE = value
    try:
        yield
    finally:
        module._ACTIVE = old

CONFIG = {"analysis_budget_enabled": True, "analysis_max_calls": 3, "analysis_timeout_seconds": 600,
              "llm_provider": "deepseek", "market_history_mode": "current", "checkpoint_enabled": True,
              "mainland_capability_routing_mode": "legacy", "financial_cache_only": True,
              "tool_vendors": dict.fromkeys(("get_balance_sheet", "get_income_statement", "get_cashflow"), "tushare")}

class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def connect(self, name="checkpoint.db"):
        conn = sqlite3.connect(self.root / name)
        self.addCleanup(conn.close)
        return conn

    def budget(self, conn, *, existing=False, now=100, maximum=3, binding="policy-a"):
        budget = AnalysisBudget(maximum, 600, clock=lambda: 0, wall_clock=lambda: now,
                                checkpoint_required=True)
        budget.bind_checkpoint(conn, Lock(), "thread-a", binding, has_checkpoint=existing)
        return budget

    def test_checkpoint_config_no_longer_rejected(self):
        self.assertEqual(inspect_budget_config(CONFIG), ())

    def test_wal_committed_state_is_visible(self):
        writer = self.connect("market.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE value (n INTEGER)")
        writer.execute("INSERT INTO value VALUES (42)")
        writer.commit()
        with budget_readiness._readonly_database(self.root / "market.db") as reader:
            self.assertEqual(reader.execute("SELECT n FROM value").fetchone(), (42,))

    def test_existing_empty_wal_is_not_discarded(self):
        writer = self.connect("market.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE value (n INTEGER)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # Synthetic fixture only.
        wal = self.root / "market.db-wal"
        self.assertTrue(wal.exists())
        self.assertEqual(wal.stat().st_size, 0)
        with budget_readiness._readonly_database(self.root / "market.db") as reader:
            self.assertEqual(reader.execute("SELECT COUNT(n) FROM value").fetchone(), (0,))
        self.assertTrue(wal.exists())

    def test_invalid_wal_and_shm_fail_before_sql(self):
        for suffix, raw in (("-wal", b"invalid"), ("-shm", b"")):
            path = self.root / (suffix + ".db")
            writer = self.connect(path.name)
            writer.execute("CREATE TABLE value(n)")
            writer.commit()
            Path(str(path) + suffix).write_bytes(raw)
            with self.assertRaises(budget_readiness._ReadFailure), budget_readiness._readonly_database(path):
                self.fail("invalid coordination file accepted")

    def test_disabled_source_cannot_invoke_provider(self):
        budget = AnalysisBudget(3, 600, allowed_data_vendors=("tushare", "akshare"))
        from tradingagents import analysis_budget
        with active_for_test(analysis_budget, budget):
            for method, vendor in (("get_global_news", "alpha_vantage"),
                                   ("get_macro_indicators", "fred"),
                                   ("get_prediction_markets", "polymarket")):
                result = budget_data.invoke_budget_data(method, vendor,
                    lambda: self.fail("disabled provider executed"))
                self.assertIn("DATA_UNAVAILABLE", result)
                self.assertIn("provider=" + vendor, result)
        self.assertEqual(budget.receipt()["admitted_attempts"], 0)

    def test_admission_commits_before_send_and_reopen(self):
        connection = self.connect()
        first = self.budget(connection)
        first.admit("model")
        # A second connection sees the committed reservation before a send.
        second_connection = self.connect()
        second = self.budget(second_connection, existing=True)
        self.assertEqual(second.receipt()["model_attempts"], 1)
        second.admit("data")
        third = self.budget(self.connect(), existing=True)
        self.assertEqual(third.receipt()["admitted_attempts"], 2)

    def test_exception_does_not_refund_and_interruption_survives(self):
        first = self.budget(self.connect())
        first.admit("data")
        first.record_interruption()
        resumed = self.budget(self.connect(), existing=True)
        self.assertEqual(resumed.receipt()["admitted_attempts"], 1)
        self.assertEqual(resumed.receipt()["last_failure"], "interrupted")

    def test_exhaustion_is_terminal_across_reopen(self):
        first = self.budget(self.connect(), maximum=1)
        first.admit("model")
        with self.assertRaises(AnalysisBudgetStopped):
            first.admit("data")
        with self.assertRaisesRegex(AnalysisBudgetStopped, "call_limit_exceeded"):
            self.budget(self.connect(), existing=True, maximum=1)

    def test_no_deadline_renewal(self):
        self.budget(self.connect(), now=100)
        resumed = self.budget(self.connect(), existing=True, now=699)
        self.assertEqual(resumed.check(), 1)
        with self.assertRaisesRegex(AnalysisBudgetStopped, "deadline_exceeded"):
            self.budget(self.connect(), existing=True, now=701)

    def test_clock_rollback_is_rejected(self):
        self.budget(self.connect(), now=100)
        with self.assertRaisesRegex(AnalysisBudgetStopped, "clock_rollback"):
            self.budget(self.connect(), existing=True, now=99)

    def test_existing_checkpoint_without_ledger_is_rejected(self):
        with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_missing"):
            self.budget(self.connect(), existing=True)

    def test_changed_policy_cannot_reset_counts(self):
        first = self.budget(self.connect())
        first.admit("data")
        for kwargs in ({"maximum": 4}, {"binding": "policy-b"}):
            with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_mismatch"):
                self.budget(self.connect(), existing=True, **kwargs)
        self.assertEqual(self.budget(self.connect(), existing=True).receipt()["data_attempts"], 1)

    def test_pending_checkpoint_rejects_admission(self):
        budget = AnalysisBudget(3, 600, checkpoint_required=True)
        with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_unbound"):
            budget.admit("data")

    def test_commit_failure_never_grants_transport(self):
        conn = self.connect()
        budget = self.budget(conn)
        conn.execute("PRAGMA query_only=ON")
        with self.assertRaises(AnalysisBudgetStopped):
            budget.admit("data")
        conn.execute("PRAGMA query_only=OFF")
        for operation in (budget.check, lambda: budget.admit("data")):
            with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_persistence_failed"):
                operation()
        with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_persistence_failed"):
            budget.record_interruption()
        with self.assertRaisesRegex(AnalysisBudgetStopped, "checkpoint_budget_persistence_failed"):
            self.budget(self.connect(), existing=True)

    def test_cli_unwind_marks_interruption_not_completion(self):
        conn = self.connect()
        budget = AnalysisBudget(3, 600, wall_clock=lambda: 100, checkpoint_required=True)
        stack = ExitStack()
        stack.enter_context(budget.checkpoint_session(conn, Lock(), "thread-a", "policy-a", has_checkpoint=False))
        budget.admit("model")
        failure = RuntimeError("synthetic provider disconnect")
        stack.__exit__(type(failure), failure, failure.__traceback__)
        self.assertEqual(budget.receipt()["last_failure"], "interrupted")
        self.assertIsNone(budget.receipt()["stop_reason"])
        self.assertEqual(self.budget(self.connect(), existing=True).receipt()["model_attempts"], 1)

    def test_completed_tombstone_survives_missing_graph_state(self):
        budget = self.budget(self.connect())
        budget.admit("model")
        budget.complete()
        with self.assertRaisesRegex(AnalysisBudgetStopped, "completed"):
            self.budget(self.connect(), existing=False)

    def test_completion_commits_before_cleanup_failure(self):
        conn = self.connect()
        budget = AnalysisBudget(3, 600, wall_clock=lambda: 100, checkpoint_required=True)
        def cleanup():
            with self.assertRaisesRegex(AnalysisBudgetStopped, "completed"):
                self.budget(self.connect(), existing=True)
            raise RuntimeError("synthetic cleanup failure")
        with self.assertRaisesRegex(RuntimeError, "cleanup failure"), budget.checkpoint_session(conn, Lock(), "thread-a", "policy-a", has_checkpoint=False):
            budget.admit("model")
            budget.complete_before_cleanup(cleanup)
        with self.assertRaisesRegex(AnalysisBudgetStopped, "completed"):
            self.budget(self.connect(), existing=False)

    def test_successful_cleanup_and_context_exit_are_idempotent(self):
        conn = self.connect()
        budget = AnalysisBudget(3, 600, wall_clock=lambda: 100, checkpoint_required=True)
        calls = []
        with budget.checkpoint_session(conn, Lock(), "thread-a", "policy-a", has_checkpoint=False):
            budget.admit("model")
            budget.complete_before_cleanup(lambda: calls.append("cleared"))
        self.assertEqual(calls, ["cleared"])
        self.assertEqual(budget.receipt()["model_attempts"], 1)
        self.assertTrue(budget.completion_committed)

    def test_context_finishes_before_connection_closes(self):
        conn = self.connect()
        budget = AnalysisBudget(3, 600, wall_clock=lambda: 100, checkpoint_required=True)
        with budget.checkpoint_session(conn, Lock(), "thread-a", "policy-a", has_checkpoint=False):
            budget.admit("model")
        conn.close()
        self.assertTrue(budget._checkpoint_finished)
        self.assertTrue(budget.receipt()["persistent"])
        self.assertEqual(budget.receipt()["stop_reason"], "completed")

    def test_missing_graph_state_does_not_reset_orphan_ledger(self):
        budget = self.budget(self.connect())
        budget.admit("model")
        self.assertEqual(self.budget(self.connect(), existing=False).receipt()["model_attempts"], 1)

    def test_two_connections_share_atomic_allowance(self):
        first = self.budget(self.connect(), maximum=1)
        second = self.budget(self.connect(), maximum=1)
        first.admit("model")
        with self.assertRaisesRegex(AnalysisBudgetStopped, "call_limit_exceeded"):
            second.admit("data")
        self.assertEqual(second.receipt()["admitted_attempts"], 1)

    def test_nonoptional_disabled_source_is_terminal(self):
        from tradingagents import analysis_budget
        budget = AnalysisBudget(3, 600, allowed_data_vendors=("tushare", "akshare"))
        with active_for_test(analysis_budget, budget), self.assertRaisesRegex(AnalysisBudgetStopped, "disabled_data_vendor"):
            budget_data.invoke_budget_data("get_news", "alpha_vantage", lambda: self.fail("called"))
        with self.assertRaises(AnalysisBudgetStopped):
            budget.admit("model")

    def test_snapshot_disabled_provider_rejected_before_implementation(self):
        from tradingagents import analysis_budget
        budget = AnalysisBudget(3, 600, allowed_data_vendors=("akshare",))
        with active_for_test(analysis_budget, budget), self.assertRaisesRegex(AnalysisBudgetStopped, "disabled_data_vendor"):
            budget_data.require_budget_snapshot("tushare", object())

    def test_wal_concurrent_commit_is_rejected(self):
        writer = self.connect("market.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE value (n INTEGER)")
        writer.execute("INSERT INTO value VALUES (1)")
        writer.commit()
        with self.assertRaises(budget_readiness._ReadFailure), budget_readiness._readonly_database(self.root / "market.db") as reader:
            self.assertEqual(reader.execute("SELECT n FROM value").fetchone(), (1,))
            writer.execute("INSERT INTO value VALUES (2)")
            writer.commit()
            self.assertEqual(reader.execute("SELECT COUNT(n) FROM value").fetchone(), (1,))

    def test_readonly_rejects_write_attach_and_vacuum(self):
        writer = self.connect("market.db")
        writer.execute("CREATE TABLE value (n INTEGER)")
        writer.commit()
        with budget_readiness._readonly_database(self.root / "market.db") as reader:
            for sql in ("INSERT INTO value VALUES (2)", "ATTACH ':memory:' AS other", "VACUUM"):
                with self.assertRaises(sqlite3.DatabaseError):
                    reader.execute(sql)

    def test_missing_and_rollback_journal_are_rejected(self):
        path = self.root / "missing.db"
        with self.assertRaises(budget_readiness._ReadFailure), budget_readiness._readonly_database(path):
            self.fail("missing database opened")
        self.assertFalse(path.exists())
        self.connect("missing.db").execute("CREATE TABLE value(n)")
        Path(str(path) + "-journal").write_bytes(b"uncertain")
        with self.assertRaises(budget_readiness._ReadFailure), budget_readiness._readonly_database(path):
            self.fail("journal accepted")

    def test_financial_cache_wal_reopen_preserves_time(self):
        path = self.root / "financial.db"
        cache = CurrentStatementCache(path)
        scope = StatementCacheScope("synthetic", "r", "income", "e", "n")
        when = datetime.now(timezone.utc) - timedelta(minutes=1)
        start, end = date(2020, 1, 1), date(2026, 10, 16)
        cache.read_through(scope=scope, range_start=start, range_end=end, now=when,
                           fetch=lambda *args: b'{"synthetic":true}')
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE observations SET checked_at=checked_at")
        writer.commit()
        result = CurrentStatementCache(path, read_only=True)._load(scope.key, start, end)
        self.assertIsNone(result[1])
        self.assertEqual(result[0][0].checked_at, when)

