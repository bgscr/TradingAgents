"""Tests for symbol normalization and the no-data routing sentinel."""

import unittest

import pytest

from tradingagents.dataflows.symbol_utils import (
    NoMarketDataError,
    crypto_base,
    is_yahoo_safe,
    normalize_symbol,
    resolve_china_a_symbol,
    resolve_mainland_instrument,
)


@pytest.mark.unit
class TestNormalizeSymbol(unittest.TestCase):
    def test_plain_equities_unchanged(self):
        for sym in ("AAPL", "MSFT", "TSM", "BRK.B", "0700.HK", "^GSPC", "GC=F"):
            self.assertEqual(normalize_symbol(sym), sym)

    def test_lowercases_are_upper(self):
        self.assertEqual(normalize_symbol("aapl"), "AAPL")
        self.assertEqual(normalize_symbol("  msft  "), "MSFT")

    def test_metal_aliases_map_to_futures(self):
        self.assertEqual(normalize_symbol("XAUUSD"), "GC=F")
        self.assertEqual(normalize_symbol("XAUUSD+"), "GC=F")   # broker CFD suffix
        self.assertEqual(normalize_symbol("xauusd+"), "GC=F")
        self.assertEqual(normalize_symbol("GOLD"), "GC=F")
        self.assertEqual(normalize_symbol("XAGUSD"), "SI=F")

    def test_energy_and_index_aliases(self):
        self.assertEqual(normalize_symbol("USOIL"), "CL=F")
        self.assertEqual(normalize_symbol("SPX500"), "^GSPC")
        self.assertEqual(normalize_symbol("NAS100"), "^NDX")
        self.assertEqual(normalize_symbol("US30"), "^DJI")

    def test_forex_pairs_get_x_suffix(self):
        self.assertEqual(normalize_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(normalize_symbol("GBPJPY"), "GBPJPY=X")
        self.assertEqual(normalize_symbol("eurusd"), "EURUSD=X")

    def test_crypto_pairs_get_dash_usd(self):
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(normalize_symbol("ETHUSD"), "ETH-USD")

    def test_six_letter_non_currency_left_alone(self):
        # GOOGLE-style 6-letter tickers that aren't two currency codes
        # must not be mangled into a fake forex pair.
        self.assertEqual(normalize_symbol("ABCDEF"), "ABCDEF")

    def test_empty_input_passthrough(self):
        self.assertEqual(normalize_symbol(""), "")


@pytest.mark.unit
class TestChinaASymbols(unittest.TestCase):
    def test_shanghai_fund_resolves_with_market_only_capabilities(self):
        instrument = resolve_mainland_instrument("512210.SH")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.yahoo_symbol, "512210.SS")
        self.assertEqual(instrument.akshare_code, "512210")
        self.assertEqual(instrument.baostock_code, "sh.512210")
        self.assertEqual(instrument.exchange, "shanghai")
        self.assertEqual(instrument.instrument_kind, "fund")
        self.assertEqual(
            instrument.capabilities,
            frozenset({"ohlcv", "technical_indicators"}),
        )

    def test_bare_shanghai_fund_prefix_resolves_without_guessing_company_kind(self):
        instrument = resolve_mainland_instrument("512210")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.yahoo_symbol, "512210.SS")
        self.assertEqual(instrument.instrument_kind, "fund")

    def test_bare_shenzhen_fund_prefix_resolves_as_fund(self):
        instrument = resolve_mainland_instrument("159915")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.yahoo_symbol, "159915.SZ")
        self.assertEqual(instrument.akshare_code, "159915")
        self.assertEqual(instrument.baostock_code, "sz.159915")
        self.assertEqual(instrument.exchange, "shenzhen")
        self.assertEqual(instrument.instrument_kind, "fund")

    def test_explicit_mainland_indices_resolve_without_company_capabilities(self):
        expected = {
            "000001.SH": ("000001.SS", "shanghai"),
            "399001.SZ": ("399001.SZ", "shenzhen"),
        }

        for raw, (yahoo_symbol, exchange) in expected.items():
            with self.subTest(raw=raw):
                instrument = resolve_mainland_instrument(raw)

                self.assertIsNotNone(instrument)
                self.assertEqual(instrument.yahoo_symbol, yahoo_symbol)
                self.assertEqual(instrument.exchange, exchange)
                self.assertEqual(instrument.instrument_kind, "index")
                self.assertEqual(
                    instrument.capabilities,
                    frozenset({"ohlcv", "technical_indicators"}),
                )

    def test_generalized_equity_resolution_preserves_legacy_identifiers(self):
        generalized = resolve_mainland_instrument("601138.SH")
        legacy = resolve_china_a_symbol("601138.SH")

        self.assertIsNotNone(generalized)
        self.assertEqual(generalized.yahoo_symbol, legacy.yahoo_symbol)
        self.assertEqual(generalized.akshare_code, legacy.akshare_code)
        self.assertEqual(generalized.baostock_code, legacy.baostock_code)
        self.assertEqual(generalized.exchange, legacy.exchange)
        self.assertEqual(generalized.instrument_kind, "equity")
        self.assertEqual(
            generalized.capabilities,
            frozenset({
                "ohlcv",
                "technical_indicators",
                "fundamentals",
                "china_enhancements",
            }),
        )

    def test_explicit_exchange_suffix_is_authoritative_for_unknown_prefix(self):
        instrument = resolve_mainland_instrument("123456.SH")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.yahoo_symbol, "123456.SS")
        self.assertEqual(instrument.exchange, "shanghai")
        self.assertEqual(instrument.instrument_kind, "unknown")
        self.assertEqual(
            instrument.capabilities,
            frozenset({"ohlcv", "technical_indicators"}),
        )

    def test_compatibility_resolver_delegates_funds_during_migration(self):
        instrument = resolve_china_a_symbol("512210.SS")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.yahoo_symbol, "512210.SS")
        self.assertEqual(instrument.instrument_kind, "fund")

    def test_shanghai_suffixes_resolve_to_same_instrument(self):
        for raw in ("601138.SS", "601138.SH", "601138"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "601138.SS")
            self.assertEqual(instrument.akshare_code, "601138")
            self.assertEqual(instrument.baostock_code, "sh.601138")
            self.assertEqual(instrument.market, "cn_a")
            self.assertEqual(instrument.exchange, "shanghai")
            self.assertEqual(normalize_symbol(raw), "601138.SS")

    def test_second_shanghai_example_resolves(self):
        for raw in ("600895.SS", "600895.SH", "600895"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "600895.SS")
            self.assertEqual(instrument.akshare_code, "600895")
            self.assertEqual(instrument.baostock_code, "sh.600895")
            self.assertEqual(normalize_symbol(raw), "600895.SS")

    def test_shenzhen_suffix_and_bare_code_resolve(self):
        for raw in ("000001.SZ", "000001"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "000001.SZ")
            self.assertEqual(instrument.akshare_code, "000001")
            self.assertEqual(instrument.baostock_code, "sz.000001")
            self.assertEqual(instrument.exchange, "shenzhen")
            self.assertEqual(normalize_symbol(raw), "000001.SZ")

    def test_unknown_bare_six_digit_code_is_not_guessed(self):
        self.assertIsNone(resolve_china_a_symbol("123456"))
        self.assertEqual(normalize_symbol("123456"), "123456")

    def test_explicit_suffix_resolves_unknown_code_without_guessing_kind(self):
        expected = {"123456.SH": "123456.SS", "123456.SZ": "123456.SZ"}
        for raw, canonical in expected.items():
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.instrument_kind, "unknown")
            self.assertEqual(normalize_symbol(raw), canonical)

    def test_existing_non_china_symbols_keep_current_behavior(self):
        self.assertEqual(normalize_symbol("XAUUSD"), "GC=F")
        self.assertEqual(normalize_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(normalize_symbol("0700.HK"), "0700.HK")


@pytest.mark.unit
class TestNoMarketDataError(unittest.TestCase):
    def test_message_includes_resolution(self):
        err = NoMarketDataError("XAUUSD+", "GC=F", "no rows")
        self.assertIn("XAUUSD+", str(err))
        self.assertIn("GC=F", str(err))
        self.assertEqual(err.symbol, "XAUUSD+")
        self.assertEqual(err.canonical, "GC=F")

    def test_canonical_defaults_to_symbol(self):
        err = NoMarketDataError("FOOBAR")
        self.assertEqual(err.canonical, "FOOBAR")


@pytest.mark.unit
class TestIsYahooSafe(unittest.TestCase):
    def test_accepts_structural_chars(self):
        for sym in ("AAPL", "GC=F", "^GSPC", "BRK.B", "BTC-USD"):
            self.assertTrue(is_yahoo_safe(sym))

    def test_rejects_slash_and_space(self):
        for sym in ("a/b", "AA PL", ""):
            self.assertFalse(is_yahoo_safe(sym))


@pytest.mark.unit
class TestCryptoBase(unittest.TestCase):
    def test_resolves_known_crypto_forms(self):
        for raw in ("BTC-USD", "BTCUSD", "btc-usdt", "BTC-USDC", "BTCUSD+"):
            self.assertEqual(crypto_base(raw), "BTC")
        self.assertEqual(crypto_base("ETH-USD"), "ETH")
        self.assertEqual(crypto_base("sol-usd"), "SOL")

    def test_non_crypto_returns_none(self):
        # Plain equities, class shares, and real tickers that alias elsewhere
        # (GOLD -> gold future on the Yahoo path) must NOT read as crypto.
        for raw in ("AAPL", "BRK-B", "GOLD", "XYZ-USD", "EURUSD", "", None):
            self.assertIsNone(crypto_base(raw))

    def test_agrees_with_normalize_symbol(self):
        # crypto_base is the shared primitive behind the -USD normalization.
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(crypto_base("BTCUSD"), "BTC")


if __name__ == "__main__":
    unittest.main()
