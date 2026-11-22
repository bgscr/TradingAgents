"""Exercise the real entry lifecycle with synthetic CLI/dependency boundaries.

No application import, credential access, network, database or real analysis.
The reader and new receipt/terminal integration are real; provider/graph work is not.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import test_analysis_stop_summary as reader_tests

ROOT = Path(__file__).absolute().parents[1]


class AnalysisBudgetStopped(RuntimeError):
    reason = "critical_evidence_failure"


class CalendarStopped(RuntimeError):
    code = "synthetic_calendar_failure"
    details = None


class APIConnectionError(RuntimeError):
    pass


class LauncherSummaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = reader_tests.StopSummaryTests("test_historical_failure_shape_is_explained_without_changing_evidence")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        spec = importlib.util.spec_from_file_location("bounded_entry_integration", ROOT / "scripts/analyze_mainland_bounded.py")
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)
        modules = {name: ModuleType(name) for name in (
            "tradingagents", "tradingagents.analysis_budget", "tradingagents.market_history",
            "tradingagents.market_history.calendar_preparation", "tradingagents.statement_acceptance",
        )}
        modules["tradingagents"].analysis_budget = modules["tradingagents.analysis_budget"]
        modules["tradingagents.analysis_budget"].analysis_preparation_scope = lambda _: contextlib.nullcontext()
        modules["tradingagents.market_history.calendar_preparation"].CalendarStopped = CalendarStopped
        modules["tradingagents.statement_acceptance"]._retain_shared_ceiling = lambda _: contextlib.nullcontext()
        modules["summarize_analysis_stop"] = self.fixture.tool
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.session = self.entry.Session(self.fixture.session, source=self.fixture.project)
        self.session.precheck = lambda config: config
        self.session.refresh = lambda: (0, 0)
        self.session.check_calendar_handoff = lambda _: 0
        self.session.check_bank_identity = lambda: None
        self.session.clock = SimpleNamespace(remaining=lambda: 100)
        self.captured_status = self.fixture.run / "run_status.json"
        self.cli_closed = False
        self.cli_calls = 0

        @contextlib.contextmanager
        def hooks(_cli, _config, artifacts):
            if self.captured_status is not None:
                artifacts["status_file"] = self.captured_status
            yield

        self.session.analysis_hooks = hooks
        self.cli = SimpleNamespace(get_user_selections=self.selection, app=self.fail_cli)
        self.config = {"analysis_budget_enabled": False, "analysis_max_calls": 60,
                       "analysis_calendar_calls": 0, "analysis_calendar_receipt_sha256": "synthetic"}

    @staticmethod
    def selection():
        return {
            "asset_type": "stock", "research_depth": 1, "llm_provider": "deepseek",
            "backend_url": "https://api.deepseek.com", "shallow_thinker": "deepseek-flash",
            "deep_thinker": "deepseek-v4-pro", "output_language": "Chinese",
            "china_a_enhancement_preset": "basic", "ticker": "000001.SZ",
            "analysis_date": "2026-11-07",
            "analysts": [SimpleNamespace(value=name) for name in ("market", "social", "news", "fundamentals")],
        }

    def fail_cli(self, **_kwargs):
        self.cli_calls += 1
        try:
            raise AnalysisBudgetStopped("Authorization: Bearer PRIVATE_INPUT https://private.invalid")
        finally:
            self.cli_closed = True

    def execute(self):
        return self.session.execute(self.cli, self.config, verify_sources=lambda: None)

    def test_stopped_receipt_and_terminal_output_include_summary_after_cli_cleanup(self):
        original = self.fixture.tool.summarize_stop

        def after_cleanup(*args, **kwargs):
            self.assertTrue(self.cli_closed)
            return original(*args, **kwargs)

        with patch.object(self.fixture.tool, "summarize_stop", side_effect=after_cleanup):
            value = self.execute()
        self.assertEqual(self.cli_calls, 1)
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(value["exception_type"], "AnalysisBudgetStopped")
        self.assertEqual(value["primary_error"]["budget_reason"], "critical_evidence_failure")
        self.assertFalse(value["resume"] or value["automatic_resume"] or value["manual_resume_entry"])
        self.assertEqual(value["stop_summary"]["failure_stage"], "finalizer")
        self.assertEqual(value["stop_summary"]["transport_error_type"], "RemoteProtocolError")
        persisted = json.loads((self.session.root / "result.json").read_text())
        self.assertEqual(persisted["stop_summary"], value["stop_summary"])
        terminal = self.entry.terminal_receipt(value, self.session.root)
        self.assertEqual(terminal["stop_summary"], value["stop_summary"])
        self.assertNotIn("PRIVATE_INPUT", json.dumps(persisted))
        self.assertNotIn("private.invalid", json.dumps(terminal))
        self.assertNotIn("calendar_handoff", terminal)

    def test_summary_failure_cannot_replace_original_stop_or_retry(self):
        with patch.object(self.fixture.tool, "summarize_stop", side_effect=RuntimeError("PRIVATE_INPUT")):
            value = self.execute()
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(value["primary_error"]["budget_reason"], "critical_evidence_failure")
        self.assertEqual(value["stop_summary"], {
            "status": "diagnostic_unavailable", "reason": "summary_unavailable",
        })
        self.assertNotIn("PRIVATE_INPUT", json.dumps(value))
        self.assertEqual(self.cli_calls, 1)

    def test_research_transport_failure_without_critical_marker_has_safe_summary(self):
        self.fixture.operational_failure()

        def fail_transport(**_kwargs):
            self.cli_calls += 1
            try:
                raise APIConnectionError("SYNTHETIC_PRIVATE_INPUT https://private.invalid")
            finally:
                self.cli_closed = True

        self.cli.app = fail_transport
        value = self.execute()
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(value["exception_type"], "APIConnectionError")
        summary = value["stop_summary"]
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["reason"], "model_transport_failure")
        self.assertEqual(summary["transport_error_type"], "RemoteProtocolError")
        self.assertEqual(summary["failure_stage"], "research_debate")
        self.assertEqual(summary["failure_node"], "Bear Researcher")
        self.assertIsNone(summary["analyst"])
        self.assertFalse(summary["subsequent_analysis_sends_allowed"])
        self.assertFalse(value["resume"] or value["automatic_resume"] or value["manual_resume_entry"])
        self.assertEqual(self.cli_calls, 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_INPUT", json.dumps(value))
        self.assertNotIn("private.invalid", json.dumps(value))
        persisted = json.loads((self.session.root / "result.json").read_text())
        self.assertEqual(persisted["stop_summary"], summary)
        self.assertEqual(self.entry.terminal_receipt(value, self.session.root)["stop_summary"], summary)

    def test_missing_current_artifacts_do_not_reuse_stale_capture(self):
        self.captured_status = None
        self.session.analysis_artifacts = {"status_file": self.fixture.run / "run_status.json"}
        with patch.object(self.fixture.tool, "summarize_stop") as reader:
            value = self.execute()
        reader.assert_not_called()
        self.assertEqual(value["stop_summary"]["reason"], "run_artifacts_unavailable")

    def test_other_session_capture_is_rejected_before_reading(self):
        self.captured_status = Path(str(self.captured_status).replace("a" * 32, "b" * 32))
        with patch.object(self.fixture.tool, "summarize_stop") as reader:
            value = self.execute()
        reader.assert_not_called()
        self.assertEqual(value["stop_summary"]["reason"], "run_artifacts_mismatch")

    def test_wrong_status_filename_is_rejected_before_reading(self):
        self.captured_status = self.fixture.run / "other.json"
        with patch.object(self.fixture.tool, "summarize_stop") as reader:
            value = self.execute()
        reader.assert_not_called()
        self.assertEqual(value["stop_summary"]["reason"], "run_artifacts_mismatch")

    def test_preanalysis_failure_does_not_open_analysis_artifacts(self):
        def fail(_config):
            raise RuntimeError("PRIVATE_INPUT")

        self.session.precheck = fail
        with patch.object(self.fixture.tool, "summarize_stop") as reader:
            value = self.execute()
        reader.assert_not_called()
        self.assertEqual(value["phase"], "precheck")
        self.assertNotIn("stop_summary", value)
        self.assertNotIn("stop_summary", self.entry.terminal_receipt(value, self.session.root))
        self.assertEqual(self.cli_calls, 0)

    def test_completed_analysis_has_no_failure_summary(self):
        self.session.analyze = lambda *_: {"status": "completed", "terminal_outcome_kind": "analysis_outcome"}
        with patch.object(self.fixture.tool, "summarize_stop") as reader:
            value = self.execute()
        reader.assert_not_called()
        self.assertNotIn("stop_summary", value)
        self.assertNotIn("stop_summary", self.entry.terminal_receipt(value, self.session.root))

    def test_result_write_failure_remains_a_failure(self):
        original = self.entry.write_once

        def deny_result(root, name, value):
            if name == "result.json":
                raise OSError("synthetic receipt write failure")
            return original(root, name, value)

        with patch.object(self.entry, "write_once", side_effect=deny_result), self.assertRaises(OSError):
            self.execute()
        self.assertFalse((self.session.root / "result.json").exists())
        self.assertEqual(self.cli_calls, 1)

    def test_bound_summary_helper_drift_is_rejected(self):
        scripts = self.fixture.project / "scripts"
        scripts.mkdir()
        helper = scripts / "summarize_analysis_stop.py"
        helper.write_bytes((ROOT / "scripts/summarize_analysis_stop.py").read_bytes())
        manifest = {"contract": "bounded-mainland-release-v1", "deployment_root": str(self.fixture.project),
                    "files": [{"root": "project", "relative": "scripts/summarize_analysis_stop.py",
                               "sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}]}
        self.entry.verify_release(manifest, source=self.fixture.project, python_root=self.fixture.project)
        with helper.open("ab") as stream:
            stream.write(b"\n# synthetic drift\n")
        with self.assertRaisesRegex(self.entry.Stop, "bound_source_changed"):
            self.entry.verify_release(manifest, source=self.fixture.project, python_root=self.fixture.project)


if __name__ == "__main__":
    unittest.main()
