"""Synthetic metadata only; never an installed or live bank registry."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.dataflows import bank_identity_snapshot as bank

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def payload(exchange, status, *, symbol=None):
    symbol = symbol or ('600123.SH' if exchange == 'SSE' else '000123.SZ')
    row = [symbol, symbol[:6], 'SYNTHETIC BANK', '银行', '主板', exchange,
           'CNY', status, '20000101', '20250101' if status == 'D' else None]
    return {'code': 0, 'data': {'fields': list(bank.FIELDS), 'items': [row]}}


def snapshot():
    parts = []
    for ordinal, (exchange, status) in enumerate(bank.PARTITIONS):
        value = payload(exchange, status)
        if status != 'L':
            value['data']['items'] = []
        parts.append(bank.parse_partition(value, exchange, status, NOW + timedelta(seconds=ordinal)))
    return bank.seal_snapshot(parts, snapshot_id='a' * 32)


def source_layout(tmp_path):
    source, run = tmp_path / 'project', tmp_path / 'project/reports/bounded-runs' / ('b' * 32)
    (source / 'config').mkdir(parents=True)
    run.mkdir(parents=True)
    base = {'schema_version': '1.0', 'registry_id': 'synthetic-base', 'rows': [{
        'canonical_symbol': '600895.SS', 'aliases': ['600895', '600895.SH'],
        'venue': 'XSHG', 'instrument_kind': 'equity', 'currency': 'CNY',
        'provenance': {'provider': 'synthetic fixture', 'source_ref': 'fixture:base',
                       'retrieved_at': '2026-01-01T00:00:00Z'}}]}
    raw = bank.json_bytes(base)
    (source / 'config/instrument_identity_registry.json').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (source / 'config/instrument_identity_registry.sha256').write_text(digest)
    (source / 'config/bounded_mainland_release.json').write_text(json.dumps({'files': [{
        'root': 'project', 'relative': 'config/instrument_identity_registry.json', 'sha256': digest}]}))
    (run / 'session.json').write_text(json.dumps({'contract': 'bounded-mainland-session-v1',
        'run_id': run.name, 'root': str(run), 'resume': False}))
    return source, run


def save_snapshot(source, value):
    root = source / 'data/instrument_identity/stock_basic' / value['snapshot_id']
    root.mkdir(parents=True)
    raw = bank.json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    (root / 'snapshot.json').write_bytes(raw)
    receipt = bank.json_bytes({'contract': bank.CONTRACT, 'snapshot_id': root.name, 'status': 'validated',
        'physical_attempts': 6, 'http_responses': 6, 'completed_partitions': 6, 'cleanup_failed': False,
        'snapshot_ref': root.name + ':' + digest})
    (root / 'result.json').write_bytes(receipt)
    (root / 'READY.json').write_text(json.dumps({'contract': bank.CONTRACT, 'snapshot_id': root.name,
        'snapshot_sha256': digest, 'result_sha256': hashlib.sha256(receipt).hexdigest(),
        'physical_attempts': 6, 'status': 'complete'}))
    return root.name + ':' + digest


def test_both_exchanges_and_aliases_bound_to_one_snapshot(tmp_path):
    source, run = source_layout(tmp_path)
    ref = save_snapshot(source, snapshot())
    binding = bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))
    config = bank.validate_binding(source, run, binding, '000123.SZ', '2026-10-16', now=NOW + timedelta(seconds=10))
    from tradingagents.dataflows.instrument_identity import (
        IdentityRegistryAvailable,
        resolve_authoritative_instrument_identity,
    )
    result = resolve_authoritative_instrument_identity('000123', registry_path=config['instrument_identity_registry_path'],
        expected_sha256=config['instrument_identity_registry_sha256'])
    assert isinstance(result, IdentityRegistryAvailable)
    assert result.identity.venue == 'XSHE' and result.identity.currency == 'CNY'
    assert binding['symbol'] == '000123.SZ'
    assert binding['expires_at'] == bank.utc_text(NOW + timedelta(hours=24))
    assert 'path' not in json.dumps(binding)


def test_known_base_identity_uses_original_revision_without_reading_snapshot(tmp_path):
    source, run = source_layout(tmp_path)
    assert bank.prepare_overlay(source, run, '600895.SH', '2026-10-16', 'a' * 32 + ':' + '0' * 64,
                                now=NOW) is None
    assert not (run / 'bank-identity-registry.json').exists()


@pytest.mark.parametrize('mutate', [
    lambda s: s['partitions'].pop(),
    lambda s: s['partitions'].__setitem__(1, copy.deepcopy(s['partitions'][0])),
    lambda s: s.__setitem__('expires_at', bank.utc_text(NOW + timedelta(days=2))),
    lambda s: s.__setitem__('endpoint', 'https://other.invalid/stock_basic'),
])
def test_invalid_snapshot_contract_rejected(tmp_path, mutate):
    source, run = source_layout(tmp_path)
    value = snapshot()
    mutate(value)
    ref = save_snapshot(source, value)
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))
    assert not (run / 'bank-identity-registry.json').exists()


@pytest.mark.parametrize('ticker,as_of', [('000123.SH', '2026-10-16'), ('999999.SZ', '2026-10-16'),
                                        ('000123.SZ', '1999-12-31')])
def test_wrong_identity_or_listing_date_rejected(tmp_path, ticker, as_of):
    source, run = source_layout(tmp_path)
    ref = save_snapshot(source, snapshot())
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, ticker, as_of, ref, now=NOW + timedelta(seconds=10))


@pytest.mark.parametrize('field,value', [('industry', None), ('industry', '非银行'), ('curr_type', 'USD'),
                                      ('exchange', 'SSE'), ('list_date', None), ('list_date', 'not-date')])
def test_missing_or_wrong_bank_metadata_never_authorizes(tmp_path, field, value):
    source, run = source_layout(tmp_path)
    data = snapshot()
    data['partitions'][1]['items'][0][bank.FIELDS.index(field)] = value
    ref = save_snapshot(source, data)
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))


def test_expired_future_corrupted_or_cross_session_binding_rejected(tmp_path):
    source, run = source_layout(tmp_path)
    ref = save_snapshot(source, snapshot())
    for when in (NOW - timedelta(seconds=1), NOW + timedelta(days=1)):
        with pytest.raises(bank.BankIdentityError):
            bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=when)
    binding = bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))
    for key, value in [('symbol', '600123.SS'), ('as_of', '2026-10-15'), ('session_id', 'c' * 32),
                       ('source_path', 'arbitrary'), ('registry_sha256', '0' * 64)]:
        changed = {**binding, key: value}
        with pytest.raises(bank.BankIdentityError):
            bank.validate_binding(source, run, changed, '000123.SZ', '2026-10-16', now=NOW + timedelta(seconds=10))
    (run / 'bank-identity-registry.json').write_text('{}')
    with pytest.raises(bank.BankIdentityError):
        bank.validate_binding(source, run, binding, '000123.SZ', '2026-10-16', now=NOW + timedelta(seconds=10))


def test_json_duplicates_and_capacity_do_not_publish():
    with pytest.raises(bank.BankIdentityError):
        bank.decode_json(b'{"code":0,"code":0}')
    data = payload('SSE', 'L')
    data['data']['items'] *= 6000
    with pytest.raises(bank.BankIdentityError):
        bank.parse_partition(data, 'SSE', 'L', NOW)


@pytest.mark.parametrize('as_of,allowed', [('2024-12-31', True), ('2025-01-01', False), ('1999-12-31', False)])
def test_delisted_bank_only_before_delisting_not_historical_pit(tmp_path, as_of, allowed):
    source, run = source_layout(tmp_path)
    data = snapshot()
    data['partitions'][0]['items'] = []
    data['partitions'][2] = bank.parse_partition(payload('SSE', 'D'), 'SSE', 'D', NOW + timedelta(seconds=2))
    ref = save_snapshot(source, data)
    if allowed:
        binding = bank.prepare_overlay(source, run, '600123.SH', as_of, ref, now=NOW + timedelta(seconds=10))
        assert binding['symbol'] == '600123.SS' and data['historical_pit'] is False
    else:
        with pytest.raises(bank.BankIdentityError, match='bank_outside_listing_interval'):
            bank.prepare_overlay(source, run, '600123.SH', as_of, ref, now=NOW + timedelta(seconds=10))


def test_base_failure_cannot_fall_back_and_known_revision_is_stable(tmp_path):
    from tradingagents.asset_configuration import resolve_run_asset_configuration
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity
    source, run = source_layout(tmp_path)
    path = source / 'config/instrument_identity_registry.json'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    config = {'instrument_identity_registry_path': str(path), 'instrument_identity_registry_sha256': digest}
    before = resolve_run_asset_configuration('600895.SS', config=config)
    assert bank.prepare_overlay(source, run, '600895.SH', '2026-10-16', 'invalid-expired-ref') is None
    after = resolve_run_asset_configuration('600895.SH', config=config)
    assert ProviderSubrequestInstrumentIdentity.from_authoritative(before.instrument_identity) == ProviderSubrequestInstrumentIdentity.from_authoritative(after.instrument_identity)
    ref = save_snapshot(source, snapshot())
    path.write_text('{}')
    with pytest.raises(bank.BankIdentityError, match='bank_digest_mismatch'):
        bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))


@pytest.mark.parametrize('change', ['duplicate', 'unknown-currency', 'row-size', 'row-name', 'future-part', 'cross-batch'])
def test_conflicts_or_uncertain_records_never_produce_overlay(tmp_path, change):
    source, run = source_layout(tmp_path)
    data = snapshot()
    if change == 'duplicate':
        data['partitions'][1]['items'] *= 2
    elif change == 'unknown-currency':
        data['partitions'][1]['items'][0][6] = None
    elif change == 'row-size':
        data['partitions'][1]['items'][0].pop()
    elif change == 'row-name':
        data['partitions'][1]['items'][0][2] = None
    elif change == 'future-part':
        data['partitions'][-1]['checked_at'] = bank.utc_text(NOW + timedelta(seconds=11))
    else:
        data['partitions'][-1]['checked_at'] = bank.utc_text(NOW + timedelta(seconds=241))
    ref = save_snapshot(source, data)
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))
    assert not (run / 'bank-identity-registry.json').exists()


@pytest.mark.parametrize('key,value', [('has_more', True), ('truncated', 'false'), ('truncated', 0),
                                     ('has_more', None), ('total', 2), ('offset', 1), ('limit', 6000)])
def test_uncertain_truncation_count_or_offset_rejected(key, value):
    data = payload('SSE', 'L')
    data['data'][key] = value
    with pytest.raises(bank.BankIdentityError):
        bank.parse_partition(data, 'SSE', 'L', NOW)


@pytest.mark.parametrize('key,value', [('pagination', {'has_more': True}), ('has_more', None),
                                     ('truncated', True), ('total', 2), ('offset', 1)])
def test_top_level_unknown_or_partial_metadata_rejected(key, value):
    data = payload('SSE', 'L')
    data[key] = value
    with pytest.raises(bank.BankIdentityError):
        bank.parse_partition(data, 'SSE', 'L', NOW)


@pytest.mark.parametrize('field,value', [('industry', '银行'), ('industry', None), ('industry', '其他'), ('curr_type', 'USD')])
def test_bare_code_ambiguity_checked_before_bank_filter(tmp_path, field, value):
    source, run = source_layout(tmp_path)
    data = snapshot()
    data['partitions'][0]['items'][0][:2] = ['000123.SH', '000123']
    data['partitions'][0]['items'][0][bank.FIELDS.index(field)] = value
    ref = save_snapshot(source, data)
    with pytest.raises(bank.BankIdentityError, match='bank_identity_unavailable_or_ambiguous'):
        bank.prepare_overlay(source, run, '000123', '2026-10-16', ref, now=NOW + timedelta(seconds=10))
    # Explicit exchange disambiguates; it never borrows the other venue's row.
    assert bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref,
        now=NOW + timedelta(seconds=10))['symbol'] == '000123.SZ'


def test_new_bank_cannot_reassign_base_alias(tmp_path):
    source, run = source_layout(tmp_path)
    data = snapshot()
    data['partitions'][1]['items'][0][:2] = ['600895.SZ', '600895']
    ref = save_snapshot(source, data)
    with pytest.raises(bank.BankIdentityError, match='bank_base_alias_conflict'):
        bank.prepare_overlay(source, run, '600895.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=10))


def exact_payload(symbol='600919.SH'):
    response = payload('SSE' if symbol.endswith('.SH') else 'SZSE', 'L', symbol=symbol)
    response['data']['has_more'] = False
    response['data']['count'] = 2318  # Deliberately not a completeness fact.
    response['extension'] = 'synthetic-uninterpreted-metadata'
    return response


def test_exact_positive_is_one_security_not_six_partition_coverage():
    query = bank.exact_query('600919.SS')
    value = bank.parse_exact_response(exact_payload(), query, NOW)
    assert value['query']['params']['ts_code'] == '600919.SH'
    assert value['partition']['items'][0][0] == '600919.SH'
    assert value['coverage'] == 'query_response_only'
    assert value['universe_coverage'] == 'partial'
    assert value['historical_pit'] is False
    assert 'count' not in value and 'extension' not in value


def save_exact_snapshot(source, value):
    root = source / 'data/instrument_identity/stock_basic' / value['snapshot_id']
    root.mkdir(parents=True)
    raw = bank.json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    (root / 'snapshot.json').write_bytes(raw)
    reference = root.name + ':' + digest
    receipt = bank.json_bytes({'contract': bank.EXACT_CONTRACT, 'snapshot_id': root.name,
        'status': 'validated', 'physical_attempts': 1, 'http_responses': 1, 'completed_partitions': 1,
        'cleanup_failed': False, 'snapshot_ref': reference, 'query_sha256': value['query_sha256'],
        'query_scope': 'exact_current_security'})
    (root / 'result.json').write_bytes(receipt)
    (root / 'READY.json').write_text(json.dumps({'contract': bank.EXACT_CONTRACT,
        'snapshot_id': root.name, 'snapshot_sha256': digest,
        'result_sha256': hashlib.sha256(receipt).hexdigest(), 'physical_attempts': 1,
        'status': 'exact_positive'}))
    return reference


def exact_snapshot(symbol='600919.SH'):
    query = bank.exact_query(symbol)
    observation = bank.parse_exact_response(exact_payload(symbol), query, NOW)
    return bank.seal_exact_snapshot(observation, snapshot_id='c' * 32)


def test_exact_positive_consumer_rebuilds_single_row_session_authority(tmp_path):
    source, run = source_layout(tmp_path)
    ref = save_exact_snapshot(source, exact_snapshot())
    binding = bank.prepare_overlay(source, run, '600919.SH', '2026-10-16', ref, now=NOW)
    assert binding['contract'] == bank.EXACT_BINDING
    assert binding['query']['params']['ts_code'] == '600919.SH'
    assert binding['coverage'] == 'query_response_only' and binding['universe_coverage'] == 'partial'
    assert binding['historical_pit'] is False
    for alias in ('600919.SH', '600919.SS', '600919'):
        config = bank.validate_binding(source, run, binding, alias, '2026-10-16', now=NOW)
        from tradingagents.asset_configuration import resolve_run_asset_configuration
        asset = resolve_run_asset_configuration(alias, config=config)
        assert asset.instrument_identity.symbol == '600919.SS'
    with pytest.raises(bank.BankIdentityError):
        bank.validate_binding(source, run, binding, '600919.SZ', '2026-10-16', now=NOW)


@pytest.mark.parametrize('symbol', ['600919.SH', '601111.SH', '000123.SZ', '001234.SZ'])
def test_exact_query_is_generic_for_both_supported_venues(tmp_path, symbol):
    source, run = source_layout(tmp_path)
    ref = save_exact_snapshot(source, exact_snapshot(symbol))
    binding = bank.prepare_overlay(source, run, symbol, '2026-10-16', ref, now=NOW)
    assert binding['symbol'] == symbol.replace('.SH', '.SS')
    assert binding['query']['params']['ts_code'] == symbol
    assert binding['expires_at'] == bank.utc_text(NOW + timedelta(hours=24))


@pytest.mark.parametrize('symbol', ['600919', '600919.BJ', '600919.sh', ' 600919.SH',
                                  '６００９１９.SH', '', None])
def test_exact_first_query_requires_explicit_supported_venue(symbol):
    with pytest.raises(bank.BankIdentityError):
        bank.exact_query(symbol)


@pytest.mark.parametrize('value', [None, False, True, -1, 0, 1, 2318, 2**80, 'unknown', {'opaque': True}])
def test_exact_count_is_never_a_total_or_fact(value):
    response = exact_payload()
    expected = bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW)
    response['data']['count'] = value
    response['count'] = value
    assert bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW) == expected


@pytest.mark.parametrize('field,value', [
    ('ts_code', '600123.SH'), ('symbol', '600123'), ('exchange', 'SZSE'),
    ('curr_type', 'USD'), ('curr_type', None), ('industry', '其他'), ('industry', None),
    ('market', 'CDR'), ('market', '北交所'), ('market', 'unknown'), ('market', ''),
    ('list_status', 'D'), ('list_status', 'P'), ('list_date', None),
    ('list_date', '20270229'), ('list_date', '20270101'), ('delist_date', '20250101'),
    ('name', ''), ('name', 'bad\nname'), ('name', 'x' * 201),
])
def test_exact_wrong_identity_classification_or_dates_rejected(field, value):
    response = exact_payload()
    response['data']['items'][0][bank.FIELDS.index(field)] = value
    with pytest.raises(bank.BankIdentityError):
        bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW)


@pytest.mark.parametrize('change', ['zero', 'duplicate', 'wrong-width', 'duplicate-field',
                                   'wrong-field', 'bool-code', 'error-code', 'wrong-query'])
def test_exact_row_and_core_schema_are_strict(change):
    response = exact_payload()
    query = bank.exact_query('600919.SH')
    if change == 'zero':
        response['data']['items'] = []
    elif change == 'duplicate':
        response['data']['items'] *= 2
    elif change == 'wrong-width':
        response['data']['items'][0].pop()
    elif change == 'duplicate-field':
        response['data']['fields'][-1] = 'list_date'
    elif change == 'wrong-field':
        response['data']['fields'][-1] = 'unknown'
    elif change == 'bool-code':
        response['code'] = False
    elif change == 'error-code':
        response['code'] = 2002
    else:
        query['params']['exchange'] = 'SZSE'
    with pytest.raises(bank.BankIdentityError):
        bank.parse_exact_response(response, query, NOW)


@pytest.mark.parametrize('container,key,value', [
    ('data', 'has_more', None), ('data', 'has_more', True), ('data', 'has_more', 0),
    ('data', 'has_more', 'false'), ('data', 'has_more', 'missing'),
    ('outer', 'has_more', True), ('outer', 'has_more', 0), ('outer', 'truncated', True),
    ('data', 'truncated', 'false'), ('data', 'offset', 1), ('data', 'offset', False),
    ('data', 'total', 2), ('outer', 'total_count', 0), ('data', 'limit', 0),
    ('data', 'next_page', 'opaque'), ('data', 'next', 0),
    ('data', 'pagination', {'has_more': False}), ('outer', 'page', 1),
])
def test_exact_known_paging_conflict_stops_without_guessing(container, key, value):
    response = exact_payload()
    target = response['data'] if container == 'data' else response
    if value == 'missing':
        target.pop(key)
    else:
        target[key] = value
    with pytest.raises(bank.BankIdentityError, match='bank_exact_pagination_unverified'):
        bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW)


def test_exact_extra_metadata_not_canonical_and_field_order_is_normalized():
    response = exact_payload()
    expected = bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW)
    response['unrecognized'] = {'secret_like_value': 'SYNTHETIC-NONCANONICAL'}
    response['data']['opaque'] = ['SYNTHETIC-NONCANONICAL']
    response['data']['fields'].reverse()
    response['data']['items'][0].reverse()
    actual = bank.parse_exact_response(response, bank.exact_query('600919.SH'), NOW)
    assert actual == expected
    assert b'SYNTHETIC-NONCANONICAL' not in bank.json_bytes(actual)


@pytest.mark.parametrize('field,value', [
    ('historical_pit', 0), ('has_more', 0), ('business_code', False),
    ('coverage', 'complete'), ('universe_coverage', 'complete'),
])
def test_exact_retained_observation_cannot_upgrade_or_coerce(field, value):
    observation = bank.parse_exact_response(exact_payload(), bank.exact_query('600919.SH'), NOW)
    observation[field] = value
    with pytest.raises(bank.BankIdentityError):
        bank.seal_exact_snapshot(observation, snapshot_id='c' * 32)


@pytest.mark.parametrize('change', ['query', 'row', 'scope', 'expiry', 'session', 'symbol',
                                   'base', 'registry', 'version', 'historical', 'date'])
def test_exact_binding_is_rederived_not_parent_asserted(tmp_path, change):
    source, run = source_layout(tmp_path)
    ref = save_exact_snapshot(source, exact_snapshot())
    binding = bank.prepare_overlay(source, run, '600919.SH', '2026-10-16', ref, now=NOW)
    if change == 'query':
        binding['query']['params']['ts_code'] = '600123.SH'
    else:
        field, value = {
            'row': ('row_sha256', '0' * 64), 'scope': ('coverage', 'complete'),
            'expiry': ('expires_at', bank.utc_text(NOW + timedelta(days=2))),
            'session': ('session_id', 'e' * 32), 'symbol': ('symbol', '600123.SS'),
            'base': ('base_sha256', '0' * 64), 'registry': ('registry_sha256', '0' * 64),
            'version': ('contract', bank.BINDING), 'historical': ('historical_pit', True),
            'date': ('as_of', '2026-10-15'),
        }[change]
        binding[field] = value
    with pytest.raises(bank.BankIdentityError):
        bank.validate_binding(source, run, binding, '600919.SH', '2026-10-16', now=NOW)


@pytest.mark.parametrize('change', ['query', 'row', 'expiry', 'partial-v1', 'future'])
def test_exact_artifact_and_receipt_are_closed_and_version_bound(tmp_path, change):
    source, run = source_layout(tmp_path)
    value = exact_snapshot()
    if change == 'query':
        value['query']['params']['ts_code'] = '600123.SH'
    elif change == 'row':
        value['partition']['items'][0][0] = '600123.SH'
    elif change == 'expiry':
        value['expires_at'] = bank.utc_text(NOW + timedelta(days=2))
    elif change == 'partial-v1':
        value['contract'] = bank.CONTRACT
    else:
        observation = bank.parse_exact_response(exact_payload(), bank.exact_query('600919.SH'),
                                               NOW + timedelta(seconds=1))
        value = bank.seal_exact_snapshot(observation, snapshot_id='c' * 32)
    ref = save_exact_snapshot(source, value)
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, '600919.SH', '2026-10-16', ref, now=NOW)
    assert not (run / 'bank-identity-registry.json').exists()


def test_exact_expiry_and_listing_date_remain_required(tmp_path):
    source, run = source_layout(tmp_path)
    ref = save_exact_snapshot(source, exact_snapshot())
    for as_of, now in [('1999-12-31', NOW), ('2026-10-16', NOW + timedelta(days=1))]:
        with pytest.raises(bank.BankIdentityError):
            bank.prepare_overlay(source, run, '600919.SH', as_of, ref, now=now)
    assert not (run / 'bank-identity-registry.json').exists()
