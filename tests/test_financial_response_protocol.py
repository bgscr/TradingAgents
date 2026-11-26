"""Standard-library tests of whole source modules, not package integration.

Run directly with -I -S -B. No product package initializer, HTTP dependency,
credential source, or real database is imported. Cache tests use only a caller
supplied temporary directory. The ordinary transport tests are a separate seam.
"""

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_source(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protocol = load_source(
    "financial_response_protocol_under_test",
    "tradingagents/financial_response_completeness.py",
)
cache_api = load_source(
    "current_statement_cache_under_test",
    "tradingagents/dataflows/current_statement_cache.py",
)


def response():
    return {
        "code": 0,
        "msg": "",
        "data": {
            "fields": ["ts_code", "ann_date", "end_date", "amount"],
            "items": [["601328.SH", "20260828", "20260630", 12]],
        },
    }


def legacy_summary():
    return {
        "contract": "financial-response-completeness-v1",
        "has_more": "false", "unknown_top_keys": 0, "unknown_data_keys": 0,
        "row_count": 1, "total": 1, "status": "provider_declared_complete",
    }


class FinancialResponseProtocolTests(unittest.TestCase):
    def test_corrupt_retained_flag_is_a_fixed_validation_failure(self):
        summary = protocol.inspect_response_completeness({
            "code": 0, "data": {"fields": ["amount"], "items": [[1]]},
        })
        summary["has_more"] = ["false"]
        with self.assertRaisesRegex(ValueError, "evidence_missing_or_invalid"):
            protocol.validate_retained_completeness(summary, 1)

    def test_documented_minimum_is_usable_without_invented_totals(self):
        response = {
            "code": 0,
            "msg": "",
            "data": {
                "fields": ["ts_code", "ann_date", "end_date"],
                "items": [["601328.SH", "20260828", "20260630"]],
            },
        }
        summary = protocol.inspect_response_completeness(response)
        self.assertEqual(summary["status"], "usable_response")
        self.assertEqual(summary["coverage"], "query_response_only")
        self.assertEqual(summary["has_more"], "missing")
        self.assertIsNone(summary["total"])
        self.assertEqual(summary["row_count"], 1)

    def test_unknown_envelope_metadata_does_not_poison_valid_table(self):
        payload = response()
        payload["extension-not-a-protocol-claim"] = {"opaque": "do-not-retain"}
        payload["data"]["another-extension"] = ["do-not-retain"]
        before = copy.deepcopy(payload)
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["status"], "usable_response")
        self.assertEqual(summary["unknown_top_keys"], 1)
        self.assertEqual(summary["unknown_data_keys"], 1)
        self.assertNotIn("do-not-retain", json.dumps(summary))
        self.assertNotIn("extension", json.dumps(summary))
        self.assertEqual(payload, before)

    def test_top_level_paging_names_are_uninterpreted_extensions(self):
        payload = response()
        payload.update(has_more=True, truncated=True, total="quota", total_count=None,
                       offset=7, limit=0)
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["status"], "usable_response")
        self.assertEqual(summary["unknown_top_keys"], 6)
        self.assertEqual(summary["has_more"], "missing")
        self.assertEqual(summary["truncated"], "missing")
        for key in ("total", "offset", "limit"):
            self.assertIsNone(summary[key])
        self.assertNotIn("quota", json.dumps(summary))

    def test_column_order_is_not_a_schema_identity_requirement(self):
        payload = response()
        payload["data"]["fields"].reverse()
        payload["data"]["items"][0].reverse()
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["row_count"], 1)
        self.assertEqual(summary["status"], "usable_response")

    def test_additional_scalar_column_and_null_optional_value_are_accepted(self):
        payload = response()
        payload["data"]["fields"].extend(["new_financial_column", "optional_value"])
        payload["data"]["items"][0].extend([5, None])
        self.assertEqual(protocol.inspect_response_completeness(payload)["row_count"], 1)

    def test_table_assessment_does_not_invent_required_business_field_coverage(self):
        payload = {"code": 0, "data": {"fields": ["optional"], "items": [[None]]}}
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["coverage"], "query_response_only")
        self.assertNotEqual(summary["status"], "provider_declared_complete")
        # Identity/critical-field eligibility remains the adapter's separate gate.

    def test_empty_table_is_a_valid_response_not_a_fabricated_period(self):
        payload = response()
        payload["data"]["items"] = []
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["row_count"], 0)
        self.assertIsNone(summary["total"])
        self.assertEqual(summary["coverage"], "query_response_only")

    def test_malformed_envelopes_are_rejected(self):
        for payload in (None, [], {}, {"code": False}, {"code": "0"}, {"code": 2 ** 40}):
            with (self.subTest(payload_type=type(payload).__name__),
                  self.assertRaisesRegex(protocol.FinancialResponseIncomplete, "invalid_envelope")):
                protocol.inspect_response_completeness(payload)

    def test_business_error_does_not_become_a_successful_table(self):
        payload = response()
        payload["code"] = 2002
        payload["msg"] = "do-not-retain"
        with self.assertRaises(protocol.FinancialResponseIncomplete) as caught:
            protocol.inspect_response_completeness(payload)
        self.assertEqual(str(caught.exception), "financial_response_business_rejection")
        self.assertNotIn("do-not-retain", str(caught.exception) + repr(caught.exception.summary))

    def test_required_table_shape_and_row_width_are_enforced(self):
        cases = (
            None, {}, {"fields": [], "items": []},
            {"fields": ["x", "x"], "items": [[1, 2]]},
            {"fields": ["x", 1], "items": [[1, 2]]},
            {"fields": [""], "items": [[1]]},
            {"fields": ["x"], "items": {}},
            {"fields": ["x"], "items": [[1, 2]]},
            {"fields": ["x"], "items": [[]]},
            {"fields": ["x"], "items": [{"x": 1}]},
        )
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(protocol.FinancialResponseIncomplete):
                protocol.inspect_response_completeness({"code": 0, "data": data})

    def test_optional_compatible_pagination_signals_are_retained(self):
        payload = response()
        payload["data"].update(
            has_more=False, truncated=False, offset=0, limit=10, total=1, total_count=1,
        )
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["has_more"], "false")
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["offset"], 0)
        self.assertEqual(summary["limit"], 10)
        self.assertEqual(summary["unknown_top_keys"], 0)
        self.assertEqual(summary["coverage"], "query_response_only")

    def test_reaching_declared_limit_is_not_a_claim_that_all_history_is_complete(self):
        payload = response()
        payload["data"]["limit"] = 1
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["status"], "usable_response")
        self.assertEqual(summary["coverage"], "query_response_only")
        self.assertIsNone(summary["total"])

    def test_continuation_truncation_and_malformed_flags_are_rejected(self):
        for key in ("has_more", "truncated"):
            for value in (True, "false", 0, None, []):
                payload = response()
                payload["data"][key] = value
                with (self.subTest(key=key, value_type=type(value).__name__),
                      self.assertRaises(protocol.FinancialResponseIncomplete)):
                    protocol.inspect_response_completeness(payload)

    def test_envelope_extension_cannot_override_data_continuation(self):
        payload = response()
        payload["has_more"] = False
        payload["data"]["has_more"] = True
        with self.assertRaisesRegex(protocol.FinancialResponseIncomplete, "has_more_true"):
            protocol.inspect_response_completeness(payload)

    def test_present_invalid_or_inconsistent_counts_are_rejected(self):
        cases = (
            ("total", None), ("total", True), ("total", "1"), ("total", -1),
            ("total", 0), ("total", 2), ("offset", 1), ("offset", True),
            ("limit", False), ("limit", 0), ("limit", -1),
        )
        for key, value in cases:
            payload = response()
            payload["data"][key] = value
            with (self.subTest(key=key, value_type=type(value).__name__),
                  self.assertRaises(protocol.FinancialResponseIncomplete)):
                protocol.inspect_response_completeness(payload)

    def test_conflicting_data_total_aliases_are_rejected(self):
        payload = response()
        payload["data"].update(total=1, total_count=2)
        with self.assertRaisesRegex(protocol.FinancialResponseIncomplete, "conflicting_total"):
            protocol.inspect_response_completeness(payload)

    def test_data_signals_remain_distinct_from_same_named_envelope_extensions(self):
        payload = response()
        payload.update(has_more=True, total="quota", limit=0)
        payload["data"].update(has_more=False, total=1, limit=10)
        summary = protocol.inspect_response_completeness(payload)
        self.assertEqual(summary["status"], "usable_response")
        self.assertEqual(summary["has_more"], "false")
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["limit"], 10)
        self.assertEqual(summary["unknown_top_keys"], 3)

    def test_valid_v2_is_read_without_mutation_or_extra_coverage_claim(self):
        summary = protocol.inspect_response_completeness(response())
        before = json.dumps(summary, sort_keys=True)
        self.assertIs(protocol.validate_retained_completeness(summary, 1), summary)
        self.assertEqual(json.dumps(summary, sort_keys=True), before)
        self.assertEqual(summary["contract"], "financial-response-assessment-v2")

    def test_legacy_v1_retains_original_version_and_meaning(self):
        summary = legacy_summary()
        before = json.dumps(summary, sort_keys=True)
        self.assertIs(protocol.validate_retained_completeness(summary, 1), summary)
        self.assertEqual(json.dumps(summary, sort_keys=True), before)
        self.assertEqual(summary["contract"], "financial-response-completeness-v1")

    def test_legacy_v1_does_not_gain_new_acceptance_rules(self):
        for changes in ({"total": None}, {"has_more": "missing"}, {"unknown_data_keys": 1}):
            summary = {**legacy_summary(), **changes}
            with (self.subTest(changes=changes),
                  self.assertRaisesRegex(ValueError, "evidence_missing_or_invalid")):
                protocol.validate_retained_completeness(summary, 1)

    def test_missing_or_relabelled_legacy_evidence_is_not_upgraded(self):
        relabelled = {**legacy_summary(), "contract": "financial-response-assessment-v2"}
        for summary in (None, {}, relabelled):
            with (self.subTest(value_type=type(summary).__name__),
                  self.assertRaisesRegex(ValueError, "evidence_missing_or_invalid")):
                protocol.validate_retained_completeness(summary, 1)

    def test_retained_v2_contradictions_and_unknown_internal_keys_are_rejected(self):
        changes = (
            {"status": "provider_declared_complete"}, {"coverage": "full_history"},
            {"has_more": "true"}, {"truncated": "true"}, {"total": 2},
            {"offset": 1}, {"limit": 0}, {"row_count": True},
            {"unknown_top_keys": -1}, {"unknown_data_keys": 33}, {"unbound": 1},
        )
        for change in changes:
            summary = {**protocol.inspect_response_completeness(response()), **change}
            with (self.subTest(changed_key=next(iter(change))),
                  self.assertRaisesRegex(ValueError, "evidence_missing_or_invalid")):
                protocol.validate_retained_completeness(summary, 1)


class DurableSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "")
        if (
            not temporary_root.is_absolute()
            or not temporary_root.is_relative_to(ROOT / "reports")
            or not temporary_root.resolve(strict=True).is_relative_to(ROOT / "reports")
        ):
            raise RuntimeError("set TEMP/TMP to a dedicated directory under project reports")
        temporary = tempfile.TemporaryDirectory(dir=temporary_root, prefix="response-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "cache.sqlite3"
        self.scope = cache_api.StatementCacheScope(
            "synthetic-account", "synthetic-instrument", "balancesheet",
            "synthetic-transport", "current-tushare-statements-v1",
        )
        self.start, self.end = date(2020, 1, 1), date(2026, 10, 16)
        self.observed = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def read(self, cache, fetch, *, now=None):
        return cache.read_through(
            self.scope, self.start, self.end, now or self.observed, fetch,
        )

    def test_v2_summary_survives_reopen_without_fetch_or_timestamp_refresh(self):
        summary = protocol.inspect_response_completeness(response())
        payload = json.dumps({"summary": summary, "observed_at": self.observed.isoformat()}).encode()
        first = self.read(cache_api.CurrentStatementCache(self.database), lambda _lo, _hi: payload)

        def unexpected_fetch(_lo, _hi):
            self.fail("fresh cache must not fetch")

        second = self.read(
            cache_api.CurrentStatementCache(self.database), unexpected_fetch,
            now=self.observed + timedelta(hours=1),
        )
        self.assertEqual(first.chunks[0].payload, second.chunks[0].payload)
        self.assertEqual(second.chunks[0].checked_at, self.observed)
        self.assertEqual(second.fetched_ranges, ())
        retained = json.loads(second.chunks[0].payload)
        protocol.validate_retained_completeness(retained["summary"], 1)
        self.assertEqual(retained["observed_at"], self.observed.isoformat())
        self.assertEqual(retained["summary"]["coverage"], "query_response_only")

    def test_v1_reopen_does_not_rewrite_bytes_or_upgrade_contract(self):
        payload = json.dumps({"summary": legacy_summary()}).encode()
        self.read(cache_api.CurrentStatementCache(self.database), lambda _lo, _hi: payload)
        result = self.read(
            cache_api.CurrentStatementCache(self.database), lambda _lo, _hi: self.fail("unexpected fetch"),
            now=self.observed + timedelta(hours=1),
        )
        self.assertEqual(result.chunks[0].payload, payload)
        summary = json.loads(result.chunks[0].payload)["summary"]
        protocol.validate_retained_completeness(summary, 1)
        self.assertEqual(summary["contract"], "financial-response-completeness-v1")
        self.assertEqual(result.chunks[0].checked_at, self.observed)

    def test_failed_expiry_refresh_retains_old_observation_time_and_bytes(self):
        payload = json.dumps({"summary": legacy_summary()}).encode()
        self.read(cache_api.CurrentStatementCache(self.database), lambda _lo, _hi: payload)

        def incomplete(_lo, _hi):
            table = response()
            table["data"]["has_more"] = True
            protocol.inspect_response_completeness(table)
            self.fail("incomplete response must not be admitted")

        result = self.read(
            cache_api.CurrentStatementCache(self.database), incomplete,
            now=self.observed + timedelta(days=2),
        )
        self.assertIsInstance(result.failure, protocol.FinancialResponseIncomplete)
        self.assertEqual(result.fetched_ranges, ())
        self.assertEqual(result.stale_ranges, ((self.start, self.end),))
        self.assertEqual(result.chunks[0].checked_at, self.observed)
        self.assertEqual(result.chunks[0].payload, payload)


if __name__ == "__main__":
    unittest.main()
