"""Real scheduler and prompt-toolkit paths; no provider transport is exercised."""
# ruff: noqa: SIM105, SIM117 -- explicit intentional stop and lifetime boundaries.
import asyncio
import contextlib
import io
import os
import socket
import sys
import threading
import unittest
import warnings
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cli.utils import get_ticker
from tradingagents.analysis_budget import (
    AnalysisBudgetStopped,
    active_budget,
    analysis_budget_scope,
    analysis_preparation_scope,
)

CONFIG = {"analysis_budget_enabled": True, "llm_provider": "deepseek",
          "market_history_mode": "current", "analysis_max_calls": 2,
          "analysis_timeout_seconds": 30, "data_cache_dir": "unused"}


class BudgetMenuEventLoopTests(unittest.TestCase):
    def test_real_ticker_menu_repeated_under_preparation(self):
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", RuntimeWarning)
            with analysis_preparation_scope(CONFIG):
                budget = active_budget()
                for _ in range(3):
                    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                        pipe.send_text("601328.SH\r")
                        self.assertEqual(get_ticker(), "601328.SS")
                self.assertEqual(budget.receipt()["admitted_attempts"], 0)
                self.assertIsNone(budget.receipt()["stop_reason"])
        self.assertFalse([item for item in observed if issubclass(item.category, RuntimeWarning)])

    def test_real_complete_cli_selection(self):
        import questionary
        from rich.console import Console

        import cli.main as cli
        fixed = dict(cli.DEFAULT_CONFIG, **CONFIG, backend_url="https://api.deepseek.com",
                     quick_think_llm="deepseek-flash", deep_think_llm="deepseek-v4-pro",
                     output_language="Chinese", max_debate_rounds=1, max_risk_discuss_rounds=1)
        environment = {"TRADINGAGENTS_" + key: "set" for key in (
            "OUTPUT_LANGUAGE", "MAX_DEBATE_ROUNDS", "MAX_RISK_ROUNDS", "LLM_PROVIDER",
            "QUICK_THINK_LLM", "DEEP_THINK_LLM")}
        environment["DEEPSEEK_API_KEY"] = "synthetic-not-a-credential"
        answers = iter(("601328.SH\r", "a\r"))
        original_ask = questionary.Question.unsafe_ask
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            def scripted_keypresses(question, *args, **kwargs):
                pipe.send_text(next(answers))
                return original_ask(question, *args, **kwargs)
            with (patch.dict(os.environ, environment), patch.object(cli, "DEFAULT_CONFIG", fixed),
                  patch.object(cli, "console", Console(file=io.StringIO())),
                  patch.object(cli, "require_runtime_profile_loading", lambda: None),
                  patch.object(sys, "stdin", io.StringIO("basic\n2026-10-16\n")),
                  patch.object(questionary.Question, "unsafe_ask", scripted_keypresses),
                  contextlib.redirect_stdout(io.StringIO()), analysis_preparation_scope(CONFIG)):
                selected = cli.get_user_selections()
                self.assertEqual(selected["ticker"], "601328.SS")
                self.assertEqual(selected["analysis_date"], "2026-10-16")
                self.assertEqual(selected["china_a_enhancement_preset"], "basic")
                self.assertEqual([a.value for a in selected["analysts"]], ["market", "social", "news", "fundamentals"])
                self.assertEqual(active_budget().receipt()["admitted_attempts"], 0)
        # Only test credentials/profile admission are substituted. All actual
        # selection, symbol normalization, asyncio creation and budget hooks run.

    def test_new_loop_threadsafe_wakeup_and_cleanup(self):
        async def run():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            worker = threading.Thread(target=lambda: loop.call_soon_threadsafe(future.set_result, 42))
            worker.start()
            value = await asyncio.wait_for(future, 3)
            worker.join()
            return value
        with analysis_preparation_scope(CONFIG):
            self.assertEqual(asyncio.run(run()), 42)
            self.assertEqual(active_budget().receipt()["admitted_attempts"], 0)

    def test_existing_running_loop_is_supported(self):
        async def run():
            with analysis_preparation_scope(CONFIG):
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                loop.call_soon_threadsafe(future.set_result, 7)
                return await future
        self.assertEqual(asyncio.run(run()), 7)

    def test_unrelated_local_and_external_connect_still_denied(self):
        for address in (("127.0.0.1", 9), ("192.0.2.1", 443)):
            with self.subTest(address=address), self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
                with socket.socket() as sock:
                    sock.connect(address)

    @unittest.skipUnless(sys.platform == "win32", "Windows socketpair fallback")
    def test_direct_socketpair_not_a_scheduler_exception(self):
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            socket.socketpair()

    def test_async_callback_network_rejection_remains_terminal(self):
        async def denied():
            with socket.socket() as sock:
                sock.connect(("127.0.0.1", 9))
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            try:
                asyncio.run(denied())
            except AnalysisBudgetStopped:
                pass
            with analysis_budget_scope(CONFIG):
                self.fail("swallowed stop started a new budget")

    def test_unrelated_worker_socket_denied(self):
        failures = []
        def worker():
            try:
                with socket.socket() as sock:
                    sock.connect(("127.0.0.1", 9))
            except AnalysisBudgetStopped as error:
                failures.append(error.reason)
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, ["uncovered_socket_transport"])

    def test_hooks_restored_after_exit(self):
        if sys.platform != "win32":
            self.skipTest("Windows loop implementation")
        from asyncio.proactor_events import BaseProactorEventLoop
        saved = (socket.socket.connect, BaseProactorEventLoop._make_self_pipe)
        with analysis_preparation_scope(CONFIG):
            pass
        self.assertEqual(saved, (socket.socket.connect, BaseProactorEventLoop._make_self_pipe))

    @unittest.skipUnless(sys.platform == "win32", "Windows socketpair fallback")
    def test_failed_pair_creation_restores_construction_scope(self):
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            loop = asyncio.new_event_loop()
            try:
                with patch.object(socket, "socketpair", side_effect=OSError("synthetic")):
                    with self.assertRaises(OSError):
                        loop._make_self_pipe()
                socket.socketpair()  # no leaked constructor-only permission
            finally:
                loop.close()

    def test_closed_loop_socket_loses_exception(self):
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            loop = asyncio.new_event_loop()
            writer = loop._csock
            loop.close()
            writer.send(b"not-a-wakeup")

    def test_replaced_loop_pair_is_not_exempt(self):
        with self.assertRaises(AnalysisBudgetStopped), analysis_preparation_scope(CONFIG):
            loop = asyncio.new_event_loop()
            saved = loop._csock
            try:
                with socket.socket() as replacement:
                    loop._csock = replacement
                    replacement.send(b"not-a-wakeup")
            finally:
                loop._csock = saved
                loop.close()

    def test_menu_then_budget_counts_no_free_calls(self):
        from tradingagents.analysis_budget import admitted_transport
        with analysis_preparation_scope(CONFIG):
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                pipe.send_text("601328.SH\r")
                self.assertEqual(get_ticker(), "601328.SS")
            with analysis_budget_scope(CONFIG) as budget:
                with admitted_transport("data"):
                    pass  # synthetic transport, actual admission accounting
                self.assertEqual(budget.receipt()["admitted_attempts"], 1)

if __name__ == "__main__":
    unittest.main()
