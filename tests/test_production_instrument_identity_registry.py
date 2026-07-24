from __future__ import annotations

import os
from pathlib import Path

import pytest

from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    resolve_authoritative_instrument_identity,
)

DEFAULT_PRODUCTION_REGISTRY = (
    Path(__file__).parents[1] / "config" / "instrument_identity_registry.json"
)
DEFAULT_PRODUCTION_CHECKSUM = DEFAULT_PRODUCTION_REGISTRY.with_suffix(".sha256")
PRODUCTION_REGISTRY = Path(
    os.environ.get("TRADINGAGENTS_IDENTITY_REGISTRY_PATH")
    or str(DEFAULT_PRODUCTION_REGISTRY)
)


def _configured_digest() -> str:
    configured = os.environ.get("TRADINGAGENTS_IDENTITY_REGISTRY_SHA256")
    if configured:
        return configured
    return DEFAULT_PRODUCTION_CHECKSUM.read_text(encoding="utf-8").split()[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "display_name"),
    (
        ("600895.SS", "上海张江高科技园区开发股份有限公司"),
        ("601658.SS", "中国邮政储蓄银行股份有限公司"),
    ),
)
def test_production_registry_resolves_controlled_live_validation_equities(
    symbol: str,
    display_name: str,
):
    digest = _configured_digest()
    result = resolve_authoritative_instrument_identity(
        symbol,
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_sha256 == digest
    assert result.identity.canonical_symbol == symbol
    assert result.identity.venue == "XSHG"
    assert result.identity.instrument_kind == "equity"
    assert result.identity.currency == "CNY"
    assert result.identity.display_name == display_name
    assert result.identity.provenance_provider == "Shanghai Stock Exchange"
    assert result.identity.provenance_source_ref == (
        "https://www.sse.com.cn/assortment/stock/list/info/company/"
        f"index.shtml?COMPANY_CODE={symbol.removesuffix('.SS')}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("alias", ("510500", "510500.SH", "510500.SS"))
def test_production_registry_resolves_510500_aliases_as_authoritative_fund(alias):
    digest = _configured_digest()

    result = resolve_authoritative_instrument_identity(
        alias,
        registry_path=PRODUCTION_REGISTRY,
        expected_sha256=digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_sha256 == digest
    assert result.identity.canonical_symbol == "510500.SS"
    assert result.identity.venue == "XSHG"
    assert result.identity.instrument_kind == "fund"
    assert result.identity.currency == "CNY"
    assert result.identity.display_name == "中证500ETF南方"
    assert result.identity.provenance_provider == "Shanghai Stock Exchange"
    assert result.identity.provenance_source_ref.startswith(
        "https://www.sse.com.cn/assortment/fund/"
    )
    assert "/stock/" not in result.identity.provenance_source_ref
