"""Offline synthetic cases only: no native fixtures or application imports."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.policy_coverage_diagnostics import capture_failure_diagnostic


class Unexpected:
    def __str__(self):
        raise AssertionError("must not stringify unexpected objects")

    __repr__ = __str__


class CaptureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("TA_CI_POLICY_COVERAGE_DIAGNOSTICS", None)

    def render(self, errors):
        return json.loads(capture_failure_diagnostic(errors))

    def test_known_guards_and_stages(self):
        errors = [
            {"root": "SECRET", "error": "held measurement object changed during acquisition"},
            {"path": "SECRET", "error": "directory membership changed during capture"},
            {"capture_stopped": "finite capture time exhausted", "first_unvisited_root": "SECRET"},
            {"complete_inventory_validation": "current acquisition completeness/method/time/provenance mismatch"},
            {"collector": "source changed during capture"},
        ]
        before = copy.deepcopy(errors)
        result = self.render(errors)
        self.assertEqual([row["stage"] for row in result["errors"]], [
            "root_or_ancestor", "entry", "capture_stopped", "complete_inventory_validation", "collector"
        ])
        self.assertTrue(all(row["message_kind"] == "known_guard" for row in result["errors"]))
        self.assertEqual(result["error_count"], 5)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(errors, before)

    def test_os_codes_drop_paths_credentials_and_multiline_suffixes(self):
        for prefix, code in (("WinError", 5), ("WinError", 32), ("Errno", 13), ("WinError", -1)):
            with self.subTest(prefix=prefix, code=code):
                result = self.render([{
                    "root": r"C:\Users\SECRET",
                    "path": "SECRET.txt",
                    "error": f"[{prefix} {code}] SECRET token=SECRET\n::warning::SECRET",
                    "runtime": {"password": "SECRET"},
                }])
                self.assertEqual(result["errors"], [{
                    "stage": "entry", "message_kind": prefix, "code": code,
                }])
                self.assertNotIn("SECRET", json.dumps(result))

    def test_unknown_text_and_appended_secrets_are_not_echoed(self):
        for message in (
            "held measurement object changed during acquisition SECRET",
            "SECRET [WinError 5] denied", "[WinError 5SECRET]", "[Errno 13]SECRET",
            "[WinError 4294967296] SECRET", "[Errno -2147483649] SECRET",
            "[WinError 12345678901] SECRET", "[WinError ５] SECRET",
            r"C:\Users\SECRET\credentials.json", "token=SECRET", "\n::error::SECRET",
        ):
            with self.subTest(message=message):
                result = self.render([{"error": message}])
                self.assertEqual(result["errors"][0]["message_kind"], "unknown")
                self.assertNotIn("SECRET", json.dumps(result))

    def test_unexpected_objects_and_fields_are_not_inspected(self):
        for errors in (Unexpected(), ("SECRET",), {"error": "SECRET"}, None):
            result = self.render(errors)
            self.assertEqual(result["errors_state"], "invalid")
            self.assertIsNone(result["error_count"])
        result = self.render([Unexpected(), {"error": Unexpected()}, {"SECRET": Unexpected()}])
        self.assertEqual([row["message_kind"] for row in result["errors"]], ["invalid"] * 3)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_builtin_subclasses_are_not_trusted(self):
        class StrangeList(list):
            def __len__(self):
                raise AssertionError("must not inspect subclass")

        class StrangeDict(dict):
            def __contains__(self, key):
                raise AssertionError("must not inspect subclass")

        class StrangeString(str):
            def __len__(self):
                raise AssertionError("must not inspect subclass")

        self.assertEqual(self.render(StrangeList())["errors_state"], "invalid")
        result = self.render([StrangeDict(), {"error": StrangeString("SECRET")}])
        self.assertTrue(all(row["message_kind"] == "invalid" for row in result["errors"]))

    def test_long_text_and_excess_errors_have_explicit_bounds(self):
        errors = [{"error": "SECRET" * 1000}] * 10000
        result = self.render(errors)
        self.assertEqual(result["error_count"], 10000)
        self.assertEqual(result["omitted_errors"], 9984)
        self.assertEqual(len(result["errors"]), 16)
        self.assertTrue(all(row["message_kind"] == "oversized" for row in result["errors"]))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 4096)

    def test_empty_errors_are_distinct_from_missing_or_unknown(self):
        self.assertEqual(self.render([])["errors_state"], "empty")
        self.assertEqual(self.render([])["error_count"], 0)
        self.assertEqual(self.render(None)["errors_state"], "invalid")
        self.assertEqual(self.render([{"error": "SECRET"}])["errors_state"], "present")

    def test_no_artifact_without_explicit_output(self):
        with patch.object(Path, "open", side_effect=AssertionError("unexpected write")):
            self.assertEqual(self.render([{"error": "SECRET"}])["artifact_status"], "disabled")

    def test_only_safe_json_is_appended_and_existing_records_remain(self):
        with tempfile.TemporaryDirectory(prefix="coverage-diagnostic-synthetic-") as directory:
            path = Path(directory) / "failures.jsonl"
            os.environ["TA_CI_POLICY_COVERAGE_DIAGNOSTICS"] = str(path)
            first = self.render([{"error": "[WinError 5] SECRET"}])
            first_bytes = path.read_bytes()
            second = self.render([{"collector": "source changed during capture"}])
            self.assertTrue(path.read_bytes().startswith(first_bytes))
            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], [first, second])
            self.assertEqual(first["artifact_status"], "written")
            self.assertNotIn("SECRET", path.read_text())
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["failures.jsonl"])

    def test_write_failure_retains_diagnostic_without_exception_text(self):
        os.environ["TA_CI_POLICY_COVERAGE_DIAGNOSTICS"] = "SECRET"
        with patch.object(Path, "open", side_effect=OSError("SECRET credentials/path")):
            result = self.render([{"error": "[WinError 32] SECRET"}])
        self.assertEqual(result["artifact_status"], "unavailable")
        self.assertEqual(result["errors"][0]["code"], 32)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_assertion_message_is_lazy_and_original_failure_remains(self):
        with patch.object(Path, "open", side_effect=AssertionError("unexpected write")):
            complete = True
            assert complete is True, capture_failure_diagnostic(Unexpected())
        complete = False
        with self.assertRaises(AssertionError) as failure:
            assert complete is True, capture_failure_diagnostic([{"error": "[WinError 5] SECRET"}])
        # Pytest assertion rewriting appends its explanation after our JSON.
        message = str(failure.exception)
        diagnostic, _ = json.JSONDecoder().raw_decode(message)
        self.assertEqual(diagnostic["errors"][0]["code"], 5)
        self.assertNotIn("SECRET", message)
