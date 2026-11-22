"""Stdlib-only synthetic tests; no application imports, credentials, or network."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
TOOL = ROOT / "scripts" / "summarize_analysis_stop.py"


class StopSummaryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("stop_summary_test", TOOL)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        parent = Path(os.environ.get("TRADINGAGENTS_TEST_TEMP_ROOT") or tempfile.gettempdir())
        self.temp = tempfile.TemporaryDirectory(prefix="stop-summary-", dir=parent)
        self.project = Path(self.temp.name)
        self.assertTrue(self.project.resolve().is_relative_to(parent.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.session = self.project / "reports" / "bounded-runs" / ("a" * 32)
        self.run = self.session / "analysis" / "000001.SZ" / "2026-11-07" / "runs" / "20261107_010000"
        self.run.mkdir(parents=True)
        self.write(self.run / "run_status.json", {
            "run_id": self.run.name, "artifact_run_id": self.run.name,
            "ticker": "000001.SZ", "analysis_date": "2026-11-07",
            "lifecycle_status": "failed", "status": "failed",
        })
        self.write(self.session / "critical-stop.json", {
            "stage": "analyst_terminal_route", "analyst": "market",
            "reason": "critical_evidence_failure", "subsequent_analysis_sends_allowed": False,
        })
        self.failure = {
            "contract_version": "1.1", "disposition": "terminal_transport_failure",
            "exception_class": "RemoteProtocolError", "exception_family": "remote_protocol",
            "elapsed_seconds": 60.562197, "http_status": None,
            "send_limits": {"graph_node": "Market Analyst"},
        }
        self.metrics = {"terminal": {"model_transport": {
            "contract_version": "1.1", "attempts": [self.failure],
        }}}
        self.save_metrics()
        self.add_diagnostic("transport_error")

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def save_metrics(self):
        self.write(self.run / "runtime_metrics.json", self.metrics)

    def add_diagnostic(self, outcome, **overrides):
        payload = {"analyst": "market", "stage": "finalizer", "attempt": 1, "outcome": outcome}
        payload.update(overrides)
        raw = json.dumps({"version": 1, "kind": "analyst_output",
                          "qualification_authority": False, "payload": payload}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        target = self.session / "analysis" / "runtime_artifacts" / "sha256" / digest[:2] / (digest + ".json.gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(gzip.compress(raw))
        with (self.run / "message_tool.log").open("a", encoding="utf-8") as log:
            log.write(f"[Diagnostic] artifact=sha256:{digest} bytes={len(raw)}\n")
        self.artifact = target

    def summary(self):
        return self.tool.summarize_stop(self.run, project_root=self.project)

    def operational_failure(self, node="Bear Researcher", stage="research_debate"):
        (self.session / "critical-stop.json").unlink()
        self.failure["send_limits"] = {"contract_version": "1.0", "graph_node": node, "stage": stage}
        self.save_metrics()
        self.error_record = {
            "contract_version": "operational-failure-detail-v1",
            "failed_phase": "graph_stream", "operational_error_category": "graph_execution",
            "exception_diagnostics": {
                "contract_version": "1.0", "chain_truncated": False, "cycle_detected": False,
                "chain": [{"exception_type": name, "message": "SYNTHETIC_PRIVATE_INPUT"}
                          for name in ("APIConnectionError", "RemoteProtocolError", "RemoteProtocolError")],
            },
        }
        self.save_operational_error()

    def save_operational_error(self):
        raw = json.dumps(self.error_record).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.error_artifact = self.session / "analysis/runtime_artifacts/sha256" / digest[:2] / (digest + ".json.gz")
        self.error_artifact.parent.mkdir(parents=True, exist_ok=True)
        self.error_artifact.write_bytes(gzip.compress(raw))
        path = self.run / "run_status.json"
        status = json.loads(path.read_text())
        status.update(terminal_outcome_kind="operational_failure", failed_phase="graph_stream",
                      operational_error_category="graph_execution", error_diagnostics={
                          "contract_version": "operational-failure-public-v1",
                          "outcome": "operational_failure", "category": "graph_execution",
                          "phase": "graph_stream", "detail_artifact": "artifact=sha256:" + digest,
                      })
        self.write(path, status)

    def test_historical_failure_shape_is_explained_without_changing_evidence(self):
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        value = self.summary()
        self.assertEqual(value["failure_stage"], "finalizer")
        self.assertEqual(value["transport_error_type"], "RemoteProtocolError")
        self.assertEqual(value["transport_error_family"], "remote_protocol")
        self.assertEqual(value["elapsed_seconds"], 60.562197)
        self.assertIsNone(value["http_status"])
        self.assertFalse(value["subsequent_analysis_sends_allowed"])
        self.assertEqual(value["limitations"], ["underlying_transport_cause_unresolved"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()})

    def test_research_failure_uses_final_typed_error_without_opening_messages(self):
        self.operational_failure()
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        original = self.tool._read_bytes

        def read(project, path):
            self.assertNotEqual(path.name, "message_tool.log")
            return original(project, path)

        with patch.object(self.tool, "_read_bytes", side_effect=read):
            value = self.summary()
        self.assertEqual(value["contract"], "analysis-stop-summary-v2")
        self.assertEqual(value["reason"], "model_transport_failure")
        self.assertEqual(value["failure_stage"], "research_debate")
        self.assertEqual(value["failure_node"], "Bear Researcher")
        self.assertEqual(value["transport_error_type"], "RemoteProtocolError")
        self.assertIsNone(value["analyst"])
        self.assertFalse(value["qualification_authority"])
        self.assertFalse(value["subsequent_analysis_sends_allowed"])
        self.assertNotIn("SYNTHETIC_PRIVATE_INPUT", json.dumps(value))
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()})

    def test_operational_stages_use_only_known_node_stage_pairs(self):
        self.operational_failure()
        for node, stage in (("Bull Researcher", "research_debate"), ("Research Manager", "research_debate"),
                            ("Trader", "trading"), ("Neutral Analyst", "risk_debate"),
                            ("Portfolio Manager", "portfolio_synthesis"), ("Market Analyst", "analysis")):
            with self.subTest(node=node):
                self.failure["send_limits"].update(graph_node=node, stage=stage)
                self.save_metrics()
                value = self.summary()
                self.assertEqual((value["failure_node"], value["failure_stage"]), (node, stage))

    def test_operational_failure_rejects_missing_unknown_or_mismatched_transport(self):
        self.operational_failure()
        limits = self.failure["send_limits"]
        for owner, key, invalid in ((limits, "graph_node", "PRIVATE https://private.invalid"),
                                    (limits, "stage", "analysis"), (limits, "contract_version", "future"),
                                    (self.failure, "contract_version", "future"),
                                    (self.failure, "contract_version", "1.0"),
                                    (self.failure, "disposition", "success"),
                                    (self.failure, "exception_class", "PRIVATE"),
                                    (self.failure, "exception_family", "PRIVATE")):
            with self.subTest(key=key):
                original = owner[key]
                owner[key] = invalid
                self.save_metrics()
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, "unsupported_transport_record"):
                    self.summary()
                owner[key] = original
        (self.run / "runtime_metrics.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.summary()

    def test_operational_failure_requires_final_graph_failure_status(self):
        self.operational_failure()
        path = self.run / "run_status.json"
        original = json.loads(path.read_text())
        for key, invalid in (("status", "completed"), ("lifecycle_status", "completed"),
                             ("terminal_outcome_kind", "analysis_outcome"), ("failed_phase", "save_report"),
                             ("operational_error_category", "report_write")):
            with self.subTest(key=key):
                self.write(path, {**original, key: invalid})
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, "unsupported_stop_record"):
                    self.summary()
        for key in ("contract_version", "outcome", "phase", "category"):
            with self.subTest(public_key=key):
                public = {**original["error_diagnostics"], key: "PRIVATE"}
                self.write(path, {**original, "error_diagnostics": public})
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, "unsupported_stop_record"):
                    self.summary()

    def test_present_but_invalid_critical_marker_never_falls_back(self):
        self.operational_failure()
        for raw in (b"{}", b"invalid json"):
            with self.subTest(raw=raw):
                (self.session / "critical-stop.json").write_bytes(raw)
                with patch.object(self.tool, "_operational_stop") as fallback, self.assertRaises(ValueError):
                    self.summary()
                fallback.assert_not_called()

    def test_operational_error_cause_must_match_last_transport(self):
        self.operational_failure()
        diagnostic = self.error_record["exception_diagnostics"]
        for types in (("ValueError",), ("APIConnectionError", "ConnectError"),
                      ("RemoteProtocolError",), ("PRIVATE https://private.invalid",), ()):
            with self.subTest(types=types):
                diagnostic["chain"] = [{"exception_type": name} for name in types]
                self.save_operational_error()
                with self.assertRaises(self.tool.SummaryUnavailable):
                    self.summary()

    def test_operational_diagnostic_contract_and_complete_chain_are_required(self):
        self.operational_failure()
        diagnostic = self.error_record["exception_diagnostics"]
        for owner, key, invalid in ((self.error_record, "contract_version", "future"),
                                    (self.error_record, "failed_phase", "other"),
                                    (self.error_record, "operational_error_category", "other"),
                                    (diagnostic, "contract_version", "future"),
                                    (diagnostic, "chain_truncated", True), (diagnostic, "cycle_detected", True)):
            with self.subTest(key=key):
                original = owner[key]
                owner[key] = invalid
                self.save_operational_error()
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, "unsupported_operational_diagnostic"):
                    self.summary()
                owner[key] = original

    def test_operational_detail_rejects_paths_hash_mismatch_and_oversize(self):
        self.operational_failure()
        path = self.run / "run_status.json"
        original = json.loads(path.read_text())
        for ref in ("../../.env", "https://private.invalid", "artifact=sha256:" + "a" * 64 + " trailing"):
            with self.subTest(ref=ref):
                public = {**original["error_diagnostics"], "detail_artifact": ref}
                self.write(path, {**original, "error_diagnostics": public})
                with patch.object(self.tool, "_artifact") as opened:
                    with self.assertRaisesRegex(self.tool.SummaryUnavailable, "invalid_operational_diagnostic"):
                        self.summary()
                    opened.assert_not_called()
        self.write(path, original)
        for damaged, reason in ((gzip.compress(b"{}"), "diagnostic_hash_mismatch"),
                                (gzip.compress(b"x" * (self.tool.MAX_BYTES + 1)), "oversize_diagnostic")):
            with self.subTest(reason=reason):
                self.error_artifact.write_bytes(damaged)
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, reason):
                    self.summary()

    def test_operational_summary_and_cli_keep_arbitrary_fields_private(self):
        self.operational_failure()
        secret = "SYNTHETIC_PRIVATE_INPUT https://private.invalid"
        self.failure.update(endpoint_host=secret, exception_message=secret, request_id=secret)
        self.failure.update(elapsed_seconds=secret, http_status=True)
        self.save_metrics()
        value = self.summary()
        self.assertIsNone(value["elapsed_seconds"])
        self.assertIsNone(value["http_status"])
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.error_artifact.write_bytes(b"broken gzip PRIVATE")
        output = io.StringIO()
        original = self.tool.summarize_stop
        with (
            patch.object(self.tool, "summarize_stop", side_effect=lambda _: original(self.run, project_root=self.project)),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(self.tool.main(["--run-dir", "unused"]), 2)
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_only_closed_fields_are_exposed_even_when_inputs_contain_secrets(self):
        secret = "SYNTHETIC_PRIVATE_VALUE"
        self.failure.update(endpoint_host="private.invalid", request_id=secret,
                            exception_class=secret, exception_family=secret,
                            exception_message="Authorization: Bearer " + secret,
                            elapsed_seconds=secret, http_status=secret)
        self.metrics["headers"] = {"Authorization": "Bearer " + secret}
        self.save_metrics()
        self.add_diagnostic("transport_error", stage="https://private.invalid/" + secret,
                            error_type=secret, request_body=secret)
        output = json.dumps(self.summary())
        for forbidden in (secret, "private.invalid", "Authorization", "Bearer", "http://", "https://"):
            self.assertNotIn(forbidden, output)

    def test_old_failure_is_not_attributed_after_a_later_success(self):
        self.metrics["terminal"]["model_transport"]["attempts"].append({
            **self.failure, "disposition": "success", "http_status": 200,
        })
        self.save_metrics()
        self.add_diagnostic("success")
        value = self.summary()
        self.assertIsNone(value["failure_stage"])
        self.assertIsNone(value["transport_error_type"])

    def test_other_analyst_transport_is_not_attributed_to_market(self):
        self.failure["send_limits"]["graph_node"] = "News Analyst"
        self.save_metrics()
        self.assertIsNone(self.summary()["transport_error_type"])

    def test_social_route_uses_the_existing_sentiment_diagnostic_name(self):
        stop_path = self.session / "critical-stop.json"
        stop = json.loads(stop_path.read_text())
        stop["analyst"] = "social"
        self.write(stop_path, stop)
        self.failure["send_limits"]["graph_node"] = "Sentiment Analyst"
        self.save_metrics()
        self.add_diagnostic("transport_error", analyst="sentiment")
        value = self.summary()
        self.assertEqual(value["analyst"], "social")
        self.assertEqual(value["failure_stage"], "finalizer")
        self.assertEqual(value["transport_error_type"], "RemoteProtocolError")

    def test_unknown_contract_is_not_interpreted(self):
        self.metrics["terminal"]["model_transport"]["contract_version"] = "unrecognized"
        self.save_metrics()
        self.assertIsNone(self.summary()["transport_error_type"])

    def test_missing_metrics_preserves_recorded_finalizer_stage(self):
        (self.run / "runtime_metrics.json").unlink()
        value = self.summary()
        self.assertEqual(value["failure_stage"], "finalizer")
        self.assertIn("transport_record_unreadable", value["limitations"])

    def test_corrupt_or_mismatched_gzip_never_claims_a_stage(self):
        for damaged in (b"not gzip", b"\x1f\x8b\x08\x00", gzip.compress(b"{}")):
            with self.subTest(damaged=damaged[:4]):
                self.artifact.write_bytes(damaged)
                value = self.summary()
                self.assertIsNone(value["failure_stage"])
                self.assertEqual(value["transport_error_type"], "RemoteProtocolError")
                self.assertIn("diagnostic_record_unreadable", value["limitations"])

    def test_decompression_is_bounded(self):
        self.artifact.write_bytes(gzip.compress(b"x" * (self.tool.MAX_BYTES + 1)))
        value = self.summary()
        self.assertIsNone(value["failure_stage"])
        self.assertIn("diagnostic_record_unreadable", value["limitations"])

    def test_duplicate_and_nonfinite_metrics_are_rejected(self):
        for raw in (b'{"terminal":{},"terminal":{}}', b'{"number":NaN}'):
            with self.subTest(raw=raw):
                (self.run / "runtime_metrics.json").write_bytes(raw)
                self.assertIn("transport_record_unreadable", self.summary()["limitations"])

    def test_wrong_run_identity_and_nonterminal_status_are_rejected(self):
        path = self.run / "run_status.json"
        original = json.loads(path.read_text())
        for field, value, reason in (("run_id", "other", "run_identity_mismatch"),
                                     ("status", "completed", "unsupported_stop_record")):
            with self.subTest(field=field):
                self.write(path, {**original, field: value})
                with self.assertRaisesRegex(self.tool.SummaryUnavailable, reason):
                    self.summary()

    def test_path_traversal_and_unrelated_locations_are_rejected_before_open(self):
        for path in (self.project / ".env", self.run / ".." / self.run.name):
            with self.subTest(path=path), patch.object(self.tool.os, "open") as opened:
                with self.assertRaises(self.tool.SummaryUnavailable):
                    self.tool.summarize_stop(path, project_root=self.project)
                opened.assert_not_called()

    def test_reparse_flag_is_rejected_before_open(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(
                st_mode=0o100644, st_file_attributes=0x400)), patch.object(self.tool.os, "open") as opened:
            with self.assertRaisesRegex(self.tool.SummaryUnavailable, "linked_evidence"):
                self.summary()
            opened.assert_not_called()

    def test_hardlinked_evidence_is_rejected(self):
        os.link(self.run / "run_status.json", self.run / "alias.json")
        with self.assertRaisesRegex(self.tool.SummaryUnavailable, "non_regular_evidence"):
            self.summary()

    def test_substitution_after_ancestor_check_is_rejected_before_content_read(self):
        target = self.run / "runtime_metrics.json"
        unrelated = self.project / "unrelated-synthetic.json"
        unrelated.write_bytes(b"SYNTHETIC_PRIVATE_CONTENT")
        original_lstat, original_open, original_fdopen = Path.lstat, os.open, os.fdopen
        leaf_checks = 0
        reads = []

        def substituted_lstat(path, *args, **kwargs):
            nonlocal leaf_checks
            if path == target:
                leaf_checks += 1
                if leaf_checks > 1:
                    return original_lstat(unrelated)
            return original_lstat(path, *args, **kwargs)

        def substituted_open(path, flags):
            return original_open(unrelated if Path(path) == target else path, flags)

        class ObservedStream:
            def __init__(self, descriptor, mode):
                self.stream = original_fdopen(descriptor, mode)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                reads.append(size)
                return self.stream.read(size)

        with (
            patch.object(Path, "lstat", substituted_lstat),
            patch.object(self.tool.os, "open", substituted_open),
            patch.object(self.tool.os, "fdopen", ObservedStream),
            self.assertRaisesRegex(self.tool.SummaryUnavailable, "evidence_changed"),
        ):
            self.tool._read_bytes(self.project, target)
        self.assertEqual(reads, [])

    def test_cli_uses_only_the_offline_reader_and_prints_safe_json(self):
        scripts = self.project / "scripts"
        scripts.mkdir()
        copy = scripts / TOOL.name
        shutil.copyfile(TOOL, copy)
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        result = subprocess.run([sys.executable, "-I", "-B", str(copy), "--run-dir", str(self.run)],
                                capture_output=True, text=True, timeout=15, env=environment, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["transport_error_type"], "RemoteProtocolError")
        self.assertNotIn(str(self.project), result.stdout)

    def test_cli_read_error_does_not_print_the_exception_or_path(self):
        with patch.object(self.tool, "summarize_stop", side_effect=OSError("PRIVATE /address/token")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(self.tool.main(["--run-dir", "unused"]), 2)
            self.assertEqual(json.loads(output.getvalue()), {
                "status": "diagnostic_unavailable", "reason": "unreadable_or_invalid_evidence",
            })


if __name__ == "__main__":
    unittest.main()
