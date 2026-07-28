from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

import tradingagents.dataflows.config as config_module
from tradingagents.dataflows.crypto_universe import (
    SUPPORTED_CRYPTO_UNIVERSE,
    is_supported_crypto_symbol,
)
from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    RegistryFailureReason,
    resolve_authoritative_crypto_identity,
    resolve_authoritative_instrument_identity,
)

PRODUCTION_REGISTRY = (
    Path(__file__).parents[1] / "config" / "crypto_identity_registry.json"
)
PRODUCTION_CHECKSUM = PRODUCTION_REGISTRY.with_suffix(".sha256")
MAINLAND_REGISTRY = (
    Path(__file__).parents[1] / "config" / "instrument_identity_registry.json"
)
MAINLAND_CHECKSUM = MAINLAND_REGISTRY.with_suffix(".sha256")
EXPECTED_SUPPORTED_USD_SYMBOLS = {
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


def _write_registry(path: Path, rows: list[dict[str, object]]) -> str:
    artifact = {
        "schema_version": "1.0",
        "registry_id": "crypto-ccc-identity-registry-v1",
        "rows": rows,
    }
    raw = json.dumps(artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    path.write_text(raw, encoding="utf-8")
    return sha256(raw.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_shared_policy_admits_every_and_only_supported_crypto_member():
    assert frozenset(EXPECTED_SUPPORTED_USD_SYMBOLS) == SUPPORTED_CRYPTO_UNIVERSE
    assert all(
        is_supported_crypto_symbol(symbol)
        for symbol in EXPECTED_SUPPORTED_USD_SYMBOLS
    )
    assert not any(
        is_supported_crypto_symbol(symbol)
        for symbol in ("UNI-USD", "BTC-USDT", "BTC-USDC", "BTC-EUR", "AAPL")
    )


@pytest.mark.unit
def test_crypto_resolver_establishes_digest_pinned_ccc_identity(tmp_path: Path):
    registry_path = tmp_path / "crypto_identity_registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "BTC-USD",
                "aliases": ["BTCUSD"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "display_name": "Bitcoin USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "https://finance.yahoo.com/quote/BTC-USD/",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            }
        ],
    )

    result = resolve_authoritative_crypto_identity(
        "btcusd",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_sha256 == digest
    assert result.identity.canonical_symbol == "BTC-USD"
    assert result.identity.venue == "CCC"
    assert result.identity.instrument_kind == "crypto"
    assert result.identity.currency == "USD"
    assert result.identity.display_name == "Bitcoin USD"


@pytest.mark.unit
def test_crypto_resolver_does_not_promote_unsupported_registry_row(tmp_path: Path):
    registry_path = tmp_path / "crypto_identity_registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "UNI-USD",
                "aliases": ["UNIUSD"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "https://finance.yahoo.com/quote/UNI-USD/",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            }
        ],
    )

    result = resolve_authoritative_crypto_identity(
        "UNI-USD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_symbol_not_supported"


@pytest.mark.unit
def test_crypto_registry_rejects_quote_substitution_aliases(tmp_path: Path):
    registry_path = tmp_path / "crypto_identity_registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "BTC-USD",
                "aliases": ["BTCUSD", "BTC-USDT"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "https://finance.yahoo.com/quote/BTC-USD/",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            }
        ],
    )

    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.MALFORMED
    assert result.diagnostic_code == "crypto_registry_alias_quote_substitution"


@pytest.mark.unit
def test_crypto_resolver_validates_complete_registry_against_shared_policy(
    tmp_path: Path,
):
    registry_path = tmp_path / "crypto_identity_registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "BTC-USD",
                "aliases": ["BTCUSD"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "registry:btc",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            },
            {
                "canonical_symbol": "UNI-USD",
                "aliases": ["UNIUSD"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "registry:uni",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            },
        ],
    )

    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.MALFORMED
    assert result.diagnostic_code == "crypto_registry_missing_runtime_capability"


@pytest.mark.unit
def test_crypto_resolver_rejects_duplicate_identity_before_lookup(tmp_path: Path):
    registry_path = tmp_path / "crypto_identity_registry.json"
    row = {
        "canonical_symbol": "BTC-USD",
        "aliases": ["BTCUSD"],
        "venue": "CCC",
        "instrument_kind": "crypto",
        "currency": "USD",
        "provenance": {
            "provider": "Yahoo Finance",
            "source_ref": "registry:btc",
            "retrieved_at": "2026-07-25T00:00:00+00:00",
        },
    }
    digest = _write_registry(registry_path, [row, {**row, "aliases": []}])

    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.MALFORMED
    assert result.diagnostic_code == "crypto_registry_duplicate_canonical_identity"


@pytest.mark.unit
def test_crypto_registry_rejects_foreign_instrument_rows(tmp_path: Path):
    registry_path = tmp_path / "crypto_identity_registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "BTC-USD",
                "aliases": [],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "Yahoo Finance",
                    "source_ref": "https://finance.yahoo.com/quote/BTC-USD/",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            },
            {
                "canonical_symbol": "AAPL",
                "aliases": [],
                "venue": "XNAS",
                "instrument_kind": "equity",
                "currency": "USD",
                "provenance": {
                    "provider": "synthetic",
                    "source_ref": "registry:foreign-row",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            },
        ],
    )

    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.MALFORMED
    assert result.diagnostic_code == "crypto_registry_unsupported_base_quote"


@pytest.mark.unit
def test_production_crypto_registry_contains_exact_supported_usd_universe():
    digest = PRODUCTION_CHECKSUM.read_text(encoding="utf-8").split()[0]

    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    artifact = json.loads(result.raw_artifact)
    assert {row["canonical_symbol"] for row in artifact["rows"]} == (
        EXPECTED_SUPPORTED_USD_SYMBOLS
    )


@pytest.mark.unit
def test_authoritative_resolver_routes_crypto_only_to_crypto_registry(monkeypatch):
    digest = PRODUCTION_CHECKSUM.read_text(encoding="utf-8").split()[0]
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "instrument_identity_registry_path": "missing-mainland.json",
            "instrument_identity_registry_sha256": "0" * 64,
            "crypto_identity_registry_path": str(PRODUCTION_REGISTRY),
            "crypto_identity_registry_sha256": digest,
        },
    )

    result = resolve_authoritative_instrument_identity("BTCUSDT")

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_symbol_not_supported"


@pytest.mark.unit
@pytest.mark.parametrize("symbol", sorted(EXPECTED_SUPPORTED_USD_SYMBOLS))
def test_production_crypto_registry_resolves_each_supported_symbol(symbol: str):
    digest = PRODUCTION_CHECKSUM.read_text(encoding="utf-8").split()[0]

    result = resolve_authoritative_crypto_identity(
        symbol,
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.identity.canonical_symbol == symbol
    assert result.identity.venue == "CCC"
    assert result.identity.instrument_kind == "crypto"
    assert result.identity.currency == "USD"


@pytest.mark.unit
def test_crypto_registry_digest_mismatch_fails_closed():
    result = resolve_authoritative_crypto_identity(
        "BTC-USD",
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256="0" * 64,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE
    assert result.diagnostic_code == "registry_digest_mismatch"


@pytest.mark.unit
def test_crypto_registry_unknown_symbol_fails_closed():
    digest = PRODUCTION_CHECKSUM.read_text(encoding="utf-8").split()[0]

    result = resolve_authoritative_crypto_identity(
        "XMR-USD",
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_symbol_not_supported"


@pytest.mark.unit
def test_explicit_coherent_crypto_override_retains_precedence(tmp_path, monkeypatch):
    registry_path = tmp_path / "custom-crypto-registry.json"
    digest = _write_registry(
        registry_path,
        [
            {
                "canonical_symbol": "BTC-USD",
                "aliases": ["BTCUSD"],
                "venue": "CCC",
                "instrument_kind": "crypto",
                "currency": "USD",
                "provenance": {
                    "provider": "custom authority",
                    "source_ref": "registry:custom-btc",
                    "retrieved_at": "2026-07-25T00:00:00+00:00",
                },
            }
        ],
    )
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "crypto_identity_registry_path": "must-not-read.json",
            "crypto_identity_registry_sha256": "0" * 64,
        },
    )

    result = resolve_authoritative_crypto_identity(
        "BTCUSD",
        registry_path=registry_path,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_source_ref == str(registry_path.resolve())
    assert result.identity.provenance_provider == "custom authority"


@pytest.mark.unit
def test_crypto_policy_does_not_change_mainland_identity_resolution():
    digest = MAINLAND_CHECKSUM.read_text(encoding="utf-8").split()[0]

    result = resolve_authoritative_instrument_identity(
        "510500.SH",
        registry_path=MAINLAND_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.identity.canonical_symbol == "510500.SS"
    assert result.identity.venue == "XSHG"
    assert result.identity.instrument_kind == "fund"
    assert result.identity.currency == "CNY"
