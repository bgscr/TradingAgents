"""Symbol normalization and market-data error types for vendor calls.

Yahoo Finance (the default vendor) uses specific ticker conventions that
differ from the broker / TradingView / MT5 style symbols users often type:

    user types        Yahoo wants       why
    ---------------   ---------------   -----------------------------------
    XAUUSD, XAUUSD+   GC=F              gold has no forex pair on Yahoo;
                                        it is quoted as a COMEX future
    EURUSD            EURUSD=X          spot forex pairs take a ``=X`` suffix
    BTCUSD            BTC-USD           crypto pairs use a ``-`` separator
    SPX500, US500     ^GSPC             index CFDs map to Yahoo index symbols

Passing the raw broker symbol to Yahoo returns an empty result, which the
agents previously received as free text and could hallucinate a price
around (see issue #781). Centralizing the mapping here means every yfinance
entry point resolves symbols the same way, and new instruments are added by
appending a table row rather than editing call sites.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from tradingagents.dataflows.crypto_universe import (
    CRYPTO_PAIR_QUOTES,
    SUPPORTED_CRYPTO_BASES,
)

# NoMarketDataError lives in the vendor-error taxonomy (errors.py); re-exported
# here for the many call sites that import it alongside normalize_symbol.
from .errors import NoMarketDataError as NoMarketDataError

logger = logging.getLogger(__name__)


_CN_A_SHANGHAI_PREFIXES = ("600", "601", "603", "605", "688", "900")
_CN_A_SHENZHEN_PREFIXES = ("000", "001", "002", "003", "300", "301", "200")
_MAINLAND_SHANGHAI_INDEX_PREFIXES = ("000", "880", "930", "931", "932")
_MAINLAND_SHENZHEN_INDEX_PREFIXES = ("399",)
_CN_A_RE = re.compile(r"^(?P<code>\d{6})(?:\.(?P<suffix>SS|SH|SZ))?$")


@dataclass(frozen=True)
class ChinaAInstrument:
    raw_input: str
    yahoo_symbol: str
    akshare_code: str
    baostock_code: str
    market: str
    exchange: str


@dataclass(frozen=True)
class MainlandInstrument(ChinaAInstrument):
    instrument_kind: str
    capabilities: frozenset[str]


# ISO-4217 codes common enough to appear in retail forex pairs. A bare
# six-letter symbol whose halves are BOTH in this set is treated as a spot
# forex pair and given Yahoo's ``=X`` suffix.
_FOREX_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
        "CNY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN",
        "MXN", "ZAR", "TRY", "INR", "KRW", "BRL", "RUB", "THB",
    }
)

# Explicit aliases for instruments whose broker symbol does not map to a
# Yahoo symbol by rule. Metals/energy resolve to their front-month future;
# index CFD names resolve to the underlying Yahoo index symbol. Extend by
# adding rows — no call site changes required.
_ALIASES = {
    # Precious metals (spot names -> COMEX/NYMEX futures)
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "XAG": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Energy
    "WTICOUSD": "CL=F", "USOIL": "CL=F", "WTI": "CL=F",
    "BCOUSD": "BZ=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COPPER": "HG=F", "XCUUSD": "HG=F",
    # Index CFDs -> Yahoo index symbols
    "SPX500": "^GSPC", "US500": "^GSPC", "SPX": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "USTEC": "^NDX",
    "US30": "^DJI", "DJI30": "^DJI", "WS30": "^DJI",
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DE40": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
    "FRA40": "^FCHI", "EU50": "^STOXX50E", "HK50": "^HSI",
}

# Yahoo symbols may contain letters, digits, and these structural characters.
_YAHOO_SAFE = re.compile(r"^[A-Za-z0-9._\-\^=]+$")


# Crypto quote currencies recognized for syntactic classification. Quote
# currency is identity-defining, so only USD pairs may normalize to Yahoo's
# ``<BASE>-USD`` symbol; stablecoin-quoted pairs remain unchanged.


def _infer_china_a_exchange(code: str, suffix: str | None) -> str | None:
    if suffix in {"SS", "SH"}:
        return "shanghai" if code.startswith(_CN_A_SHANGHAI_PREFIXES) else None
    if suffix == "SZ":
        return "shenzhen" if code.startswith(_CN_A_SHENZHEN_PREFIXES) else None
    if code.startswith(_CN_A_SHANGHAI_PREFIXES):
        return "shanghai"
    if code.startswith(_CN_A_SHENZHEN_PREFIXES):
        return "shenzhen"
    return None


def resolve_mainland_instrument(raw: str) -> MainlandInstrument | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    original = raw.strip()
    match = _CN_A_RE.fullmatch(original.upper().rstrip("+"))
    if match is None:
        return None
    code = match.group("code")
    suffix = match.group("suffix")
    if suffix in {"SH", "SS"} and code.startswith(
        _MAINLAND_SHANGHAI_INDEX_PREFIXES
    ):
        exchange = "shanghai"
        instrument_kind = "index"
    elif suffix == "SZ" and code.startswith(_MAINLAND_SHENZHEN_INDEX_PREFIXES):
        exchange = "shenzhen"
        instrument_kind = "index"
    elif code.startswith("5") and suffix in {None, "SH", "SS"}:
        exchange = "shanghai"
        instrument_kind = "fund"
    elif code.startswith(("15", "16")) and suffix in {None, "SZ"}:
        exchange = "shenzhen"
        instrument_kind = "fund"
    else:
        exchange = _infer_china_a_exchange(code, suffix)
        if exchange is None:
            if suffix in {"SH", "SS"}:
                exchange = "shanghai"
            elif suffix == "SZ":
                exchange = "shenzhen"
            else:
                return None
            instrument_kind = "unknown"
        else:
            instrument_kind = "equity"
    yahoo_suffix = ".SS" if exchange == "shanghai" else ".SZ"
    baostock_prefix = "sh" if exchange == "shanghai" else "sz"
    return MainlandInstrument(
        raw_input=original,
        yahoo_symbol=f"{code}{yahoo_suffix}",
        akshare_code=code,
        baostock_code=f"{baostock_prefix}.{code}",
        market="cn_a",
        exchange=exchange,
        instrument_kind=instrument_kind,
        capabilities=(
            frozenset({"ohlcv", "technical_indicators"})
            if instrument_kind in {"fund", "index", "unknown"}
            else frozenset(
                {
                    "ohlcv",
                    "technical_indicators",
                    "fundamentals",
                    "china_enhancements",
                }
            )
        ),
    )


def resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None:
    """Compatibility path for callers migrating to ``resolve_mainland_instrument``.

    Existing equities retain the original ``ChinaAInstrument`` value shape;
    funds and other explicitly suffixed mainland instruments return the richer
    subclass so capability-aware callers can route them safely.
    """
    resolved = resolve_mainland_instrument(raw)
    if resolved is None:
        return None
    if resolved.instrument_kind != "equity":
        return resolved
    return ChinaAInstrument(
        raw_input=resolved.raw_input,
        yahoo_symbol=resolved.yahoo_symbol,
        akshare_code=resolved.akshare_code,
        baostock_code=resolved.baostock_code,
        market=resolved.market,
        exchange=resolved.exchange,
    )


def crypto_base(raw: str) -> str | None:
    """Return the crypto base (e.g. ``BTC``) for a known USD/USDT/USDC-quoted
    crypto symbol in any form the pipeline may hold — ``BTC-USD``, ``BTCUSD``,
    ``BTC-USDT`` — or None for non-crypto symbols. Purely syntactic.
    """
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().rstrip("+").replace("-", "")
    for quote in CRYPTO_PAIR_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            return base if base in SUPPORTED_CRYPTO_BASES else None
    return None


def _normalize_crypto(s: str) -> str | None:
    """Return ``<BASE>-USD`` only for a known USD-quoted crypto pair."""
    compact = s.rstrip("+").replace("-", "")
    if not compact.endswith("USD"):
        return None
    base = compact.removesuffix("USD")
    return f"{base}-USD" if base in SUPPORTED_CRYPTO_BASES else None


def normalize_symbol(raw: str) -> str:
    """Map a user/broker symbol to its canonical Yahoo Finance symbol.

    Resolution order (first match wins):
      1. China A-share rule: six-digit mainland tickers -> Yahoo ``.SS``/``.SZ``.
      2. Explicit alias table (metals, energy, index CFDs).
      3. Crypto rule: a known crypto base quoted in USD (dashed or not) ->
         ``BASE-USD``. USDT/USDC pairs remain distinct and unchanged.
      4. Forex rule: six letters that are two ISO currency codes -> ``PAIR=X``.
      5. Otherwise the upper-cased symbol is returned unchanged (plain
         equities, ETFs, Yahoo-native symbols like ``GC=F`` or ``^GSPC``).

    A trailing ``+`` (broker CFD marker, e.g. ``XAUUSD+``) is stripped before
    matching. The function is purely syntactic — it performs no network
    calls — so it is safe to apply on every request.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw

    s = raw.strip().upper()
    # Broker CFD/qualifier suffixes Yahoo never uses.
    s = s.rstrip("+")

    china_a = resolve_china_a_symbol(s)
    if china_a is not None:
        canonical = china_a.yahoo_symbol
        if canonical != raw.strip().upper():
            logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
        return canonical

    crypto = _normalize_crypto(s)
    if s in _ALIASES:
        canonical = _ALIASES[s]
    elif crypto is not None:
        canonical = crypto
    elif len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        canonical = f"{s}=X"
    else:
        canonical = s

    if canonical != raw.strip().upper():
        logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
    return canonical


def is_yahoo_safe(symbol: str) -> bool:
    """True when ``symbol`` only contains characters Yahoo symbols use."""
    return bool(symbol) and _YAHOO_SAFE.fullmatch(symbol) is not None
