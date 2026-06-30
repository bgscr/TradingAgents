"""Vendor router must respect the configured chain and never silently hide a
broken primary.

Regressions for #988 (explicit single-vendor config still fell back to others),
#289 (fallback ran for unchosen vendors), and #989 (serious primary failures
were swallowed without a trace).
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import akshare_data, interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.symbol_utils import NoMarketDataError


def _reset_config():
    # Hard reset: set_config() merges, so empty DEFAULT dicts (e.g. tool_vendors)
    # don't clear keys leaked by other tests. Replace the global outright.
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _no_data(symbol, *a, **k):
    raise NoMarketDataError(symbol, symbol, "no rows")


def _returns(value):
    def impl(symbol, *a, **k):
        return value
    return impl


def _raises(exc):
    def impl(symbol, *a, **k):
        raise exc
    return impl


@pytest.mark.unit
class VendorRoutingTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def _route(self, vendors_for_get_stock_data):
        missing = set(vendors_for_get_stock_data) - set(interface.VENDOR_METHODS["get_stock_data"])
        self.assertFalse(missing, f"Missing production get_stock_data vendors: {sorted(missing)}")
        return mock.patch.dict(
            interface.VENDOR_METHODS["get_stock_data"],
            vendors_for_get_stock_data,
            clear=False,
        )

    def test_explicit_single_vendor_does_not_fall_back(self):
        # #988: with yfinance pinned, a healthy alpha_vantage must NOT be used.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        av = mock.Mock(side_effect=_returns("AV_DATA"))
        with self._route({"yfinance": _no_data, "alpha_vantage": av}):
            result = interface.route_to_vendor("get_stock_data", "FAKE", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        av.assert_not_called()  # the unchosen vendor was never tried

    def test_explicit_multi_vendor_falls_back_within_chain(self):
        # Listing both vendors opts in to ordered fallback.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def test_successful_fallback_does_not_emit_warning(self):
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with self._route({
            "yfinance": _raises(ConnectionError("temporary network failure")),
            "alpha_vantage": _returns("AV_DATA"),
        }), self.assertNoLogs("tradingagents.dataflows.interface", level="WARNING"):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")

        self.assertEqual(result, "AV_DATA")

    def test_primary_error_is_logged_not_masked(self):
        # #989: primary errors + fallback no-data -> NO_DATA, but the failure
        # must be visible in logs (broken primary not hidden).
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with self._route({"yfinance": _raises(ValueError("boom")), "alpha_vantage": _no_data}), \
                self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm:
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        joined = "\n".join(cm.output)
        self.assertIn("boom", joined)            # the real error surfaced in logs
        self.assertIn("yfinance", joined)

    def test_unknown_configured_vendor_raises(self):
        set_config({"data_vendors": {"core_stock_apis": "bogus_vendor"}})
        with self.assertRaises(ValueError) as ctx:
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("bogus_vendor", str(ctx.exception))

    def test_default_sentinel_uses_all_vendors(self):
        # No explicit choice ("default") keeps the resilient full-chain behavior.
        set_config({"data_vendors": {"core_stock_apis": "default"}})
        with self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def _route_method(self, method, vendors):
        missing = set(vendors) - set(interface.VENDOR_METHODS[method])
        self.assertFalse(missing, f"Missing production {method} vendors: {sorted(missing)}")
        return mock.patch.dict(interface.VENDOR_METHODS[method], vendors, clear=False)

    def test_optional_category_degrades_instead_of_raising(self):
        # An optional enrichment vendor (FRED macro) that raises must NOT abort
        # the run — the router returns a sentinel so the analysis proceeds.
        set_config({"data_vendors": {"macro_data": "fred"}})
        with self._route_method(
            "get_macro_indicators", {"fred": _raises(ValueError("FRED 400: bad series"))}
        ):
            result = interface.route_to_vendor("get_macro_indicators", "cpi", "2026-01-01")
        self.assertIn("DATA_UNAVAILABLE", result)
        self.assertIn("macro_data", result)

    def test_core_category_still_raises_on_error(self):
        # A core category (single configured vendor) propagates the error so a
        # broken primary is loud, not silently degraded.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        with self._route({"yfinance": _raises(ValueError("boom"))}), \
                self.assertRaises(ValueError):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")

    def test_china_a_symbol_uses_market_specific_vendor_chain(self):
        set_config({
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            }
        })
        calls = []

        def akshare(symbol, *a, **k):
            calls.append(("akshare", symbol))
            return "AK_DATA"

        def baostock(symbol, *a, **k):
            calls.append(("baostock", symbol))
            return "BS_DATA"

        with self._route({
            "akshare": akshare,
            "baostock": baostock,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SH", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "AK_DATA")
        self.assertEqual(calls, [("akshare", "601138.SH")])

    def test_china_a_market_chain_falls_back_in_order(self):
        set_config({
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            }
        })
        with self._route({
            "akshare": _no_data,
            "baostock": _returns("BS_DATA"),
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SS", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "BS_DATA")

    def test_china_a_technical_indicators_fall_back_to_baostock(self):
        set_config({
            "market_data_vendors": {
                "cn_a": {
                    "technical_indicators": "akshare,baostock,yfinance",
                }
            }
        })
        calls = []

        def akshare(symbol, *a, **k):
            calls.append(("akshare", symbol))
            raise ConnectionError("Remote end closed connection without response")

        def baostock(symbol, *a, **k):
            calls.append(("baostock", symbol))
            return "BS_INDICATORS"

        with self._route_method(
            "get_indicators",
            {
                "akshare": akshare,
                "baostock": baostock,
                "yfinance": _returns("YF_INDICATORS"),
            },
        ):
            result = interface.route_to_vendor(
                "get_indicators", "600895.SH", "rsi", "2026-06-30", 30
            )

        self.assertEqual(result, "BS_INDICATORS")
        self.assertEqual(calls, [("akshare", "600895.SH"), ("baostock", "600895.SH")])

    def test_non_china_symbol_ignores_market_specific_vendor_chain(self):
        set_config({
            "data_vendors": {"core_stock_apis": "yfinance"},
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            },
        })
        akshare = mock.Mock(side_effect=_returns("AK_DATA"))
        with self._route({
            "akshare": akshare,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "AAPL", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "YF_DATA")
        akshare.assert_not_called()

    def test_tool_vendor_override_still_wins_for_china_a_symbol(self):
        set_config({
            "tool_vendors": {"get_stock_data": "yfinance"},
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            },
        })
        akshare = mock.Mock(side_effect=_returns("AK_DATA"))
        with self._route({
            "akshare": akshare,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SH", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "YF_DATA")
        akshare.assert_not_called()

    def test_real_vendor_table_registers_configured_china_a_vendors(self):
        cfg = config_module.get_config()
        expected_methods = {
            "get_stock_data": "core_stock_apis",
            "get_indicators": "technical_indicators",
            "get_fundamentals": "fundamental_data",
            "get_balance_sheet": "fundamental_data",
            "get_cashflow": "fundamental_data",
            "get_income_statement": "fundamental_data",
            "get_news": "news_data",
            "get_insider_transactions": "news_data",
        }

        for method, category in expected_methods.items():
            configured = {
                vendor.strip()
                for vendor in cfg["market_data_vendors"]["cn_a"][category].split(",")
                if vendor.strip()
            }
            registered = set(interface.VENDOR_METHODS[method])
            self.assertTrue(
                configured.issubset(registered),
                f"{method} missing configured China vendors: {sorted(configured - registered)}",
            )

    def test_default_china_a_technical_indicator_chain_includes_baostock(self):
        cfg = config_module.get_config()
        vendors = [
            vendor.strip()
            for vendor in cfg["market_data_vendors"]["cn_a"]["technical_indicators"].split(",")
            if vendor.strip()
        ]

        self.assertEqual(vendors, ["akshare", "baostock", "yfinance"])

    def test_akshare_price_and_indicator_placeholders_are_replaced(self):
        self.assertIs(
            interface.VENDOR_METHODS["get_stock_data"]["akshare"],
            akshare_data.get_stock_data,
        )
        self.assertIs(
            interface.VENDOR_METHODS["get_indicators"]["akshare"],
            akshare_data.get_stock_stats_indicators_window,
        )


if __name__ == "__main__":
    unittest.main()
