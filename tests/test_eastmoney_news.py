"""Synthetic, standard-library-only tests; no product/package import or real data."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "tradingagents/dataflows/eastmoney_news.py"
SPEC = importlib.util.spec_from_file_location("_isolated_eastmoney_news", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def article(**overrides):
    row = {"title": "601328 interim report", "date": "2026-06-30 12:30:00",
           "code": "202606301234", "content": "", "mediaName": ""}
    row.update(overrides)
    return row


def envelope(rows=None, **overrides):
    value = {"result": {"cmsArticleWebOld": [article()] if rows is None else rows}}
    value.update(overrides)
    return value


def wire(payload, callback="requested"):
    return (callback + "(" + json.dumps(payload, ensure_ascii=False) + ");").encode()


def parse(body=None, **overrides):
    options = {"expected_callback": "requested", "ticker": "601328",
               "start_date": "2026-06-01", "end_date": "2026-06-30"}
    options.update(overrides)
    return MODULE.parse_news_page(wire(envelope()) if body is None else body, **options)


class NewsPageTests(unittest.TestCase):
    def rejected(self, body, reason, **options):
        with self.assertRaises(MODULE.NewsResponseError) as caught:
            parse(body, **options)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason)
        return caught.exception

    def test_minimal_sdk_envelope_is_not_supplier_success(self):
        page = parse(wire(envelope([article(mediaName="", content="")])) )
        self.assertEqual(page.business_status, "not_supplied")
        self.assertEqual(page.coverage_status, "unknown")
        self.assertTrue(page.callback_matches)
        self.assertEqual(page.rows[0]["code"], "202606301234")
        self.assertEqual(page.rows[0]["url"], "https://finance.eastmoney.com/a/202606301234.html")

    def test_other_identifier_bom_whitespace_optional_semicolon(self):
        for suffix in (b")", b");", b") \n; "):
            body = b"\xef\xbb\xbf \nother_$1 ( " + json.dumps(envelope()).encode() + suffix
            page = parse(body)
            self.assertFalse(page.callback_matches)

    def test_unknown_extensions_are_not_closed_canonical_fields(self):
        payload = envelope([article(providerExtension={"v": 1})], extension="new")
        payload["result"]["future"] = [1, 2]
        page = parse(wire(payload))
        self.assertEqual(len(page.rows), 1)
        self.assertNotIn("providerExtension", page.rows[0])

    def test_explicit_local_failure_markers(self):
        for marker in ({"success": False}, {"error": "secret failure"},
                       {"status": "ERROR"}, {"status": "failed"}):
            self.rejected(wire(envelope(**marker)), "business_failure")

    def test_status_semantics_are_not_borrowed_from_tushare(self):
        for marker in ({"code": 0}, {"code": 200}, {"success": True},
                       {"status": "ok"}, {"status": None}, {"error": None}):
            self.rejected(wire(envelope(**marker)), "business_status_unverified")

    def test_result_level_status_and_error_envelope(self):
        payload = envelope()
        payload["result"]["code"] = 0
        self.rejected(wire(payload), "business_status_unverified")
        self.rejected(wire({"error": "provider error"}), "business_failure")

    def test_hostile_wrappers_and_trailing_code(self):
        raw = json.dumps(envelope()).encode()
        for prefix, suffix in ((b"a.b(", b")"), (b"a[0](", b")"),
                               (b"eval(", b");run()"), (b"function(", b")"),
                               (b"x(/*", b"*/)"), (b"x(", b"); // comment")):
            with self.assertRaises(MODULE.NewsResponseError):
                parse(prefix + raw + suffix)

    def test_duplicate_keys_and_nonfinite_values(self):
        self.rejected(b'x({"x":1,"x":2})', "duplicate_json_key")
        for value in ("NaN", "Infinity", "-Infinity", "1e999"):
            self.rejected(('x({"extra":' + value + '})').encode(), "nonfinite_json_number")

    def test_body_and_request_validation(self):
        for body, reason in (("not bytes", "body_type"), (b"\xff", "encoding_invalid"),
                             (b"x" * (1024 * 1024 + 1), "body_limit")):
            self.rejected(body, reason)
        self.rejected(wire(envelope()), "expected_callback_invalid", expected_callback="a.b")
        for options in ({"ticker": "invalid"}, {"instrument_aliases": ("",)},
                        {"start_date": "20260601"}, {"end_date": "2026-05-31"}):
            self.rejected(wire(envelope()), "request_invalid", **options)

    def test_empty_and_bad_schema(self):
        self.rejected(b"x([])", "root_not_object")
        for value in ({}, {"result": []}, {"result": {}},
                      {"result": {"cmsArticleWebOld": {}}}):
            self.rejected(wire(value), "schema_invalid")
        error = self.rejected(wire(envelope([])), "no_usable_articles")
        self.assertEqual(sum(error.excluded_counts.values()), 0)

    def test_required_fields_and_optional_types(self):
        for field in ("title", "code"):
            for bad in (None, "", 3, []):
                error = self.rejected(wire(envelope([article(**{field: bad})])), "no_usable_articles")
                self.assertEqual(error.excluded_counts["malformed"], 1)
        for field in ("content", "mediaName"):
            row = article()
            del row[field]
            self.assertEqual(parse(wire(envelope([row]))).rows[0][field], "")
            error = self.rejected(wire(envelope([article(**{field: []})])), "no_usable_articles")
            self.assertEqual(error.excluded_counts["malformed"], 1)

    def test_article_code_and_local_keyword_do_not_prove_stock_identity(self):
        row = article(title="unrelated stock", code="601328", keyword="601328", **{"关键词": "601328"})
        error = self.rejected(wire(envelope([row])), "no_usable_articles")
        self.assertEqual(error.excluded_counts["wrong_stock"], 1)

    def test_numeric_symbol_cannot_match_larger_identifier(self):
        for title in ("16013280", "ABC601328XYZ", "600000 report"):
            error = self.rejected(wire(envelope([article(title=title)])), "no_usable_articles")
            self.assertEqual(error.excluded_counts["wrong_stock"], 1)

    def test_authoritative_alias_and_html_highlights(self):
        row = article(title="交通银行半年报")
        self.assertEqual(len(parse(wire(envelope([row])), instrument_aliases=("交通银行",)).rows), 1)
        self.rejected(wire(envelope([row])), "no_usable_articles")
        row = article(title="<em>601328</em> announcement")
        self.assertEqual(parse(wire(envelope([row]))).rows[0]["title"], "601328 announcement")
        self.assertEqual(len(parse(ticker="601328.SH").rows), 1)

    def test_encoded_markup_attributes_do_not_prove_stock_association(self):
        for title in ('&lt;img alt="601328"&gt;unrelated',
                      '<img alt=">" data-stock="601328">unrelated',
                      '<script>601328</script>unrelated',
                      '<style>601328</style>unrelated', '<!--601328-->unrelated'):
            error = self.rejected(wire(envelope([article(title=title)])), "no_usable_articles")
            self.assertEqual(error.excluded_counts["wrong_stock"], 1)
        row = article(title="&lt;em&gt;601328&lt;/em&gt; &amp; progress")
        self.assertEqual(parse(wire(envelope([row]))).rows[0]["title"], "601328 & progress")

    def test_inclusive_shanghai_dates_and_timezone_conversion(self):
        rows = [article(date="2026-06-01"), article(date="2026-06-30 23:59:59"),
                article(date="2026-05-31T16:00:00Z"), article(date="2026-06-30T16:00:00Z")]
        page = parse(wire(envelope(rows)))
        self.assertEqual(len(page.rows), 3)
        self.assertEqual(page.excluded_counts["outside_window"], 1)
        self.assertEqual(page.rows[2]["date"], "2026-06-01 00:00:00")

    def test_undated_invalid_and_future_rows_excluded(self):
        for date in (None, "", "not-a-date", "2026-02-30", "20260630", 123):
            error = self.rejected(wire(envelope([article(date=date)])), "no_usable_articles")
            self.assertEqual(error.excluded_counts["undated"], 1)
        error = self.rejected(wire(envelope([article(date="2027-01-01")])), "no_usable_articles")
        self.assertEqual(error.excluded_counts["outside_window"], 1)

    def test_partial_rows_keep_each_exclusion_count_and_unknown_coverage(self):
        rows = [article(), None, article(date=""), article(title="unrelated"), article(date="2020-01-01")]
        page = parse(wire(envelope(rows, total=1, pageIndex=1, pageSize=10)))
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.excluded_counts, {"malformed": 1, "undated": 1, "wrong_stock": 1, "outside_window": 1})
        self.assertEqual(page.coverage_status, "unknown")


if __name__ == "__main__":
    unittest.main()
