"""Authoritative Supported Crypto Universe policy."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SUPPORTED_CRYPTO_UNIVERSE = frozenset(
    {
        "BTC-USD",
        "ETH-USD",
        "SOL-USD",
        "XRP-USD",
        "ADA-USD",
        "DOGE-USD",
        "LTC-USD",
        "BCH-USD",
        "DOT-USD",
        "AVAX-USD",
        "LINK-USD",
    }
)
CRYPTO_REGISTRY_ID = "crypto-ccc-identity-registry-v1"
CRYPTO_REFERENCE_MARKET = "CCC"
CRYPTO_QUOTE_CURRENCY = "USD"
SUPPORTED_CRYPTO_BASES = frozenset(
    symbol.removesuffix(f"-{CRYPTO_QUOTE_CURRENCY}")
    for symbol in SUPPORTED_CRYPTO_UNIVERSE
)
_CANONICAL_PAIR_PATTERN = re.compile(
    r"^(?P<base>[A-Z0-9]+)-(?P<quote>[A-Z0-9]+)$"
)
CRYPTO_PAIR_QUOTES = ("USDT", "USDC", "USD")


class CryptoUniversePolicyError(ValueError):
    """A registry candidate violates the Supported Crypto Universe policy."""

    def __init__(self, diagnostic_code: str, detail: str) -> None:
        super().__init__(detail)
        self.diagnostic_code = diagnostic_code


def canonical_supported_crypto_symbol(symbol: object) -> str | None:
    """Return the admitted canonical identity for a spelling-only USD alias."""
    if not isinstance(symbol, str):
        return None
    normalized = symbol.strip().upper()
    if normalized in SUPPORTED_CRYPTO_UNIVERSE:
        return normalized
    if normalized.endswith("USD") and "-" not in normalized:
        candidate = f"{normalized[:-3]}-USD"
        if candidate in SUPPORTED_CRYPTO_UNIVERSE:
            return candidate
    return None


def is_supported_crypto_symbol(symbol: object) -> bool:
    """Return whether ``symbol`` names an admitted canonical identity or alias."""
    return canonical_supported_crypto_symbol(symbol) is not None


def is_crypto_pair_syntax(symbol: object) -> bool:
    """Classify crypto-shaped input without treating it as supported identity."""
    if not isinstance(symbol, str):
        return False
    normalized = symbol.strip().upper()
    if canonical_supported_crypto_symbol(normalized) is not None:
        return True
    pair = _alias_pair(normalized)
    if pair is None:
        return False
    base, quote = pair
    return quote in CRYPTO_PAIR_QUOTES and (
        "-" in normalized or base in SUPPORTED_CRYPTO_BASES
    )


def _row_value(row: object, field: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(field)
    return getattr(row, field, None)


def _alias_pair(alias: str) -> tuple[str, str] | None:
    if "-" in alias:
        match = _CANONICAL_PAIR_PATTERN.fullmatch(alias)
        if match is None:
            return None
        return match.group("base"), match.group("quote")
    for quote in CRYPTO_PAIR_QUOTES:
        if alias.endswith(quote) and len(alias) > len(quote):
            base = alias[: -len(quote)]
            if re.fullmatch(r"[A-Z0-9]+", base) is not None:
                return base, quote
    return None


def validate_crypto_registry_rows(rows: Iterable[Mapping[str, Any]]) -> None:
    """Validate complete registry rows against identity and runtime invariants."""
    canonical_identities: set[str] = set()
    for row in rows:
        symbol = _row_value(row, "canonical_symbol")
        normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
        if normalized not in SUPPORTED_CRYPTO_UNIVERSE:
            canonical_pair = _CANONICAL_PAIR_PATTERN.fullmatch(normalized)
            diagnostic_code = (
                "missing_runtime_capability"
                if canonical_pair is not None
                and canonical_pair.group("quote") == CRYPTO_QUOTE_CURRENCY
                else "unsupported_base_quote"
            )
            raise CryptoUniversePolicyError(
                diagnostic_code,
                f"{normalized or '<invalid>'} is outside the Supported Crypto Universe",
            )
        if normalized in canonical_identities:
            raise CryptoUniversePolicyError(
                "duplicate_canonical_identity",
                f"{normalized} appears more than once",
            )
        canonical_identities.add(normalized)

        canonical_match = _CANONICAL_PAIR_PATTERN.fullmatch(normalized)
        if canonical_match is None:  # Defensive: the closed universe is canonical.
            raise CryptoUniversePolicyError(
                "unsupported_base_quote",
                f"{normalized} is not a canonical base/quote pair",
            )
        canonical_base = canonical_match.group("base")
        canonical_quote = canonical_match.group("quote")
        if _row_value(row, "venue") != CRYPTO_REFERENCE_MARKET:
            raise CryptoUniversePolicyError(
                "reference_market_not_ccc",
                f"{normalized} must use the CCC Reference Market",
            )
        if _row_value(row, "instrument_kind") != "crypto":
            raise CryptoUniversePolicyError(
                "missing_runtime_capability",
                f"{normalized} does not select the crypto runtime capability",
            )
        if _row_value(row, "currency") != canonical_quote:
            raise CryptoUniversePolicyError(
                "unsupported_base_quote",
                f"{normalized} currency does not match its quote currency",
            )

        aliases = _row_value(row, "aliases")
        if not isinstance(aliases, (list, tuple)):
            raise CryptoUniversePolicyError(
                "invalid_alias",
                f"{normalized} aliases must be a list",
            )
        if any(
                not isinstance(alias, str)
                or not alias
                or alias != alias.strip().upper()
                for alias in aliases
        ):
            raise CryptoUniversePolicyError(
                "invalid_alias",
                f"{normalized} has a non-canonical alias",
            )
        if len(aliases) != len(set(aliases)):
            raise CryptoUniversePolicyError(
                "invalid_alias",
                f"{normalized} aliases must be unique",
            )
        for alias in aliases:
            alias_pair = _alias_pair(alias)
            if alias_pair is None:
                raise CryptoUniversePolicyError(
                    "invalid_alias",
                    f"{alias} is not a valid Crypto Symbol Alias",
                )
            alias_base, alias_quote = alias_pair
            if alias_quote != canonical_quote:
                raise CryptoUniversePolicyError(
                    "alias_quote_substitution",
                    f"{alias} changes {normalized}'s quote currency",
                )
            if alias_base != canonical_base:
                raise CryptoUniversePolicyError(
                    "alias_base_substitution",
                    f"{alias} changes {normalized}'s base asset",
                )


__all__ = [
    "SUPPORTED_CRYPTO_UNIVERSE",
    "SUPPORTED_CRYPTO_BASES",
    "CRYPTO_REGISTRY_ID",
    "CRYPTO_PAIR_QUOTES",
    "CRYPTO_QUOTE_CURRENCY",
    "CRYPTO_REFERENCE_MARKET",
    "CryptoUniversePolicyError",
    "canonical_supported_crypto_symbol",
    "is_supported_crypto_symbol",
    "is_crypto_pair_syntax",
    "validate_crypto_registry_rows",
]
