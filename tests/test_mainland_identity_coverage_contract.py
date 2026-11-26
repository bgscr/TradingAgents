"""Synthetic authority-contract examples, not registration of real securities."""
import hashlib
import json

import pytest

from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    resolve_authoritative_instrument_identity,
)


def registry(tmp_path, rows):
    path = tmp_path / 'synthetic-registry.json'
    path.write_text(json.dumps({'schema_version': '1.0', 'registry_id': 'synthetic-only', 'rows': rows}))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def row(code, suffix, venue):
    return {'canonical_symbol': f'{code}.{suffix}',
            'aliases': [code, f'{code}.SH'] if suffix == 'SS' else [code],
            'venue': venue, 'instrument_kind': 'equity', 'currency': 'CNY',
            'display_name': 'SYNTHETIC — not an observed bank classification',
            'provenance': {'provider': 'synthetic-only', 'source_ref': 'fixture:identity',
                           'retrieved_at': '2026-11-01T00:00:00Z'}}


@pytest.mark.parametrize('code,suffix,venue', [('600000', 'SS', 'XSHG'), ('000001', 'SZ', 'XSHE')])
def test_existing_resolver_is_not_limited_to_three_installed_equities(tmp_path, code, suffix, venue):
    path, digest = registry(tmp_path, [row(code, suffix, venue)])
    for alias in [code, f'{code}.{suffix}'] + ([f'{code}.SH'] if suffix == 'SS' else []):
        result = resolve_authoritative_instrument_identity(alias, registry_path=path, expected_sha256=digest)
        assert isinstance(result, IdentityRegistryAvailable)
        assert result.identity.canonical_symbol == f'{code}.{suffix}'
        assert result.identity.venue == venue
        assert result.identity.instrument_kind == 'equity'
        # An equity identity does not assert a bank industry or establish financial coverage.
        assert not hasattr(result.identity, 'bank_classification')


@pytest.mark.parametrize('symbol', ['999999.SH', '000001.SH', 'arbitrary-bank-name'])
def test_unknown_or_wrong_exchange_does_not_become_identity(tmp_path, symbol):
    path, digest = registry(tmp_path, [row('000001', 'SZ', 'XSHE')])
    result = resolve_authoritative_instrument_identity(symbol, registry_path=path, expected_sha256=digest)
    assert isinstance(result, IdentityRegistryUnavailable)


def test_unpinned_or_ambiguous_metadata_stays_unavailable(tmp_path):
    original = row('600000', 'SS', 'XSHG')
    duplicate = row('600001', 'SS', 'XSHG')
    duplicate['aliases'].append('600000')
    path, digest = registry(tmp_path, [original, duplicate])
    wrong = resolve_authoritative_instrument_identity('600000', registry_path=path, expected_sha256='0' * 64)
    assert isinstance(wrong, IdentityRegistryUnavailable) and wrong.diagnostic_code == 'registry_digest_mismatch'
    ambiguous = resolve_authoritative_instrument_identity('600000', registry_path=path, expected_sha256=digest)
    assert isinstance(ambiguous, IdentityRegistryUnavailable) and ambiguous.diagnostic_code == 'identity_alias_ambiguous'
