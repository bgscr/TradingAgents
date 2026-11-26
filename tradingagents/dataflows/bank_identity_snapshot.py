"""Explicit, digest-pinned CURRENT bank metadata; never a network lookup."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONTRACT = 'bank-stock-basic-snapshot-v1'
BINDING = 'bank-stock-basic-session-v1'
ENDPOINT = 'https://api.waditu.com/dataapi/stock_basic'
PARTITIONS = (('SSE', 'L'), ('SZSE', 'L'), ('SSE', 'D'), ('SZSE', 'D'), ('SSE', 'P'), ('SZSE', 'P'))
FIELDS = ('ts_code', 'symbol', 'name', 'industry', 'market', 'exchange', 'curr_type',
          'list_status', 'list_date', 'delist_date')
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 6 * MAX_RESPONSE_BYTES + 16384
TTL_SECONDS = 86400
EXACT_CONTRACT = 'bank-stock-basic-exact-positive-v2'
EXACT_BINDING = 'bank-stock-basic-exact-session-v2'
EXACT_MARKETS = frozenset({'主板', '创业板', '科创板'})


class BankIdentityError(ValueError):
    """Fixed local code only; never include payload, credential or exception text."""


def require(value, code):
    if not value:
        raise BankIdentityError(code)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def decode_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'bank_duplicate_json_key')
            result[key] = value
        return result

    def invalid(_):
        raise BankIdentityError('bank_nonfinite_json')

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise BankIdentityError('bank_invalid_json') from None


def utc_text(value):
    require(isinstance(value, datetime) and value.tzinfo is not None, 'bank_time_requires_timezone')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def instant(value):
    require(type(value) is str and value.endswith('Z'), 'bank_invalid_utc_time')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise BankIdentityError('bank_invalid_utc_time') from None
    require(utc_text(result) == value, 'bank_noncanonical_utc_time')
    return result


def current_time(now=None):
    value = datetime.now(timezone.utc) if now is None else now
    require(value.tzinfo is not None, 'bank_time_requires_timezone')
    return value.astimezone(timezone.utc)


def regular_file(path, *, maximum=MAX_SNAPSHOT_BYTES):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'bank_linked_path')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'bank_not_regular_file')
    require(0 < info.st_size <= maximum, 'bank_file_size')
    return info


def read_bound(path, expected=None, *, maximum=MAX_SNAPSHOT_BYTES):
    path = Path(path)
    try:
        before = regular_file(path, maximum=maximum)
        with path.open('rb') as handle:
            import os
            opened = os.fstat(handle.fileno())
            raw = handle.read(maximum + 1)
        after = regular_file(path, maximum=maximum)
    except OSError:
        raise BankIdentityError('bank_file_unreadable') from None
    def signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    require(signature(before) == signature(opened) == signature(after), 'bank_file_changed')
    require(len(raw) <= maximum, 'bank_file_size')
    if expected is not None:
        require(type(expected) is str and re.fullmatch(r'[0-9a-f]{64}', expected) is not None,
                'bank_invalid_digest')
        require(hashlib.sha256(raw).hexdigest() == expected, 'bank_digest_mismatch')
    return raw


def _day(value):
    require(type(value) is str and re.fullmatch(r'[0-9]{8}', value) is not None, 'bank_missing_listing_date')
    try:
        return datetime.strptime(value, '%Y%m%d').date()
    except ValueError:
        raise BankIdentityError('bank_invalid_listing_date') from None


def _rows(partition):
    require(type(partition) is dict and set(partition) ==
            {'exchange', 'list_status', 'checked_at', 'fields', 'items'}, 'bank_partition_contract')
    exchange, status = partition['exchange'], partition['list_status']
    require((exchange, status) in PARTITIONS, 'bank_partition_scope')
    instant(partition['checked_at'])
    require(partition['fields'] == list(FIELDS) and type(partition['items']) is list,
            'bank_table_schema')
    require(len(partition['items']) < 6000, 'bank_capacity_unverified')
    require(len(json_bytes(partition)) <= MAX_RESPONSE_BYTES, 'bank_response_too_large')
    rows = []
    for values in partition['items']:
        require(type(values) is list and len(values) == len(FIELDS), 'bank_row_schema')
        require(all(value is None or type(value) is str and len(value) <= 200
                    and not any(ord(char) < 32 for char in value) for value in values), 'bank_field_type')
        row = dict(zip(FIELDS, values, strict=True))
        suffix = '.SH' if exchange == 'SSE' else '.SZ'
        require(type(row['symbol']) is str and re.fullmatch(r'[0-9]{6}', row['symbol']) is not None
                and row['ts_code'] == row['symbol'] + suffix and row['exchange'] == exchange
                and row['list_status'] == status, 'bank_row_identity_conflict')
        require(bool(row['name']) and bool(row['market']), 'bank_missing_identity_field')
        listed = _day(row['list_date'])
        if status == 'D':
            require(_day(row['delist_date']) > listed, 'bank_listing_interval')
        else:
            require(row['delist_date'] in (None, ''), 'bank_listing_interval')
        rows.append(row)
    return rows


def parse_partition(response, exchange, status, checked_at):
    require(type(response) is dict and type(response.get('code')) is int
            and response['code'] == 0, 'bank_provider_rejected')
    data = response.get('data')
    require(type(data) is dict, 'bank_response_schema')
    require(set(data) <= {'fields', 'items', 'has_more', 'truncated', 'total', 'total_count', 'offset'},
            'bank_unknown_table_metadata')
    require(set(response) <= {'code', 'msg', 'request_id', 'data', 'has_more', 'truncated',
                              'total', 'total_count', 'offset'}, 'bank_unknown_response_metadata')
    for container in (response, data):
        require(all(key not in container or container[key] is False for key in ('has_more', 'truncated')),
                'bank_truncated_response')
    fields, items = data.get('fields'), data.get('items')
    require(type(fields) is list and len(fields) == len(FIELDS)
            and all(type(field) is str for field in fields) and set(fields) == set(FIELDS)
            and type(items) is list, 'bank_table_schema')
    require(all(type(row) is list and len(row) == len(fields) for row in items), 'bank_row_schema')
    for container in (response, data):
        for key in ('total', 'total_count'):
            if key in container:
                require(type(container[key]) is int and container[key] == len(items), 'bank_count_unverified')
        if 'offset' in container:
            require(type(container['offset']) is int and container['offset'] == 0, 'bank_offset_unverified')
    partition = {'exchange': exchange, 'list_status': status, 'checked_at': utc_text(checked_at),
                 'fields': list(FIELDS), 'items': [[row[fields.index(field)] for field in FIELDS] for row in items]}
    _rows(partition)
    return partition


def exact_query(ticker):
    """Explicit venue is a query constraint, never inferred identity."""
    require(type(ticker) is str and re.fullmatch(r'[0-9]{6}\.(SH|SS|SZ)', ticker) is not None,
            'bank_exact_ticker_requires_venue')
    symbol = ticker.replace('.SS', '.SH')
    return {'api_name': 'stock_basic', 'params': {
        'ts_code': symbol, 'exchange': 'SSE' if symbol.endswith('.SH') else 'SZSE',
        'list_status': 'L', 'ts_type_name': 'https://api.waditu.com/dataapi'},
        'fields': ','.join(FIELDS)}


def exact_contract(equity):
    return 'equity-stock-basic-exact-positive-v1' if equity else EXACT_CONTRACT


def snapshot_directory(equity):
    return 'data/instrument_identity/stock_basic_equity' if equity else 'data/instrument_identity/stock_basic'


def _exact_observation(partition, query, *, equity=False):
    require(type(query) is dict and type(query.get('params')) is dict,
            'bank_exact_query_contract')
    require(query == exact_query(query['params'].get('ts_code')), 'bank_exact_query_contract')
    rows = _rows(partition)
    require(len(rows) == 1, 'bank_exact_row_count_unverified')
    row = rows[0]
    require(partition['exchange'] == query['params']['exchange']
            and partition['list_status'] == 'L' and row['ts_code'] == query['params']['ts_code'],
            'bank_exact_identity_mismatch')
    if equity:
        from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
        security = resolve_mainland_instrument(row['ts_code'])
        require(security and security.instrument_kind == 'equity' and not row['symbol'].startswith(('200', '900'))
                and ((row['exchange'] == 'SZSE') == (security.exchange == 'shenzhen'))
                and ((row['exchange'] == 'SSE') == (security.exchange == 'shanghai')), 'equity_identity_unverified')
    require((equity or row['industry'] == '银行') and row['curr_type'] == 'CNY'
            and row['market'] in EXACT_MARKETS, 'bank_classification_unavailable')
    require(_day(row['list_date']) <= instant(partition['checked_at']).astimezone(
        timezone(timedelta(hours=8))).date(), 'bank_listing_date_after_observation')
    return {'query_scope': 'exact_current_security', 'coverage': 'query_response_only',
            'universe_coverage': 'partial', 'historical_pit': False, 'query': query,
            'partition': partition, 'business_code': 0, 'has_more': False}


def parse_exact_response(response, query, checked_at, *, equity=False):
    """Positive identity only. Unknown extensions never enter canonical evidence."""
    require(type(response) is dict and type(response.get('code')) is int
            and response['code'] == 0, 'bank_provider_rejected')
    data = response.get('data')
    require(type(data) is dict, 'bank_response_schema')
    # A conservative local proxy condition, not a claim of universe completeness.
    require(data.get('has_more') is False, 'bank_exact_pagination_unverified')
    for container in (response, data):
        require(all(key not in container or container[key] is False for key in ('has_more', 'truncated')),
                'bank_exact_pagination_unverified')
        for key in ('total', 'total_count'):
            require(key not in container or type(container[key]) is int and container[key] == 1,
                    'bank_exact_pagination_unverified')
        require('offset' not in container or type(container['offset']) is int and container['offset'] == 0,
                'bank_exact_pagination_unverified')
        require('limit' not in container or type(container['limit']) is int and container['limit'] >= 1,
                'bank_exact_pagination_unverified')
        require(all(key not in container or container[key] is None for key in ('next', 'next_page'))
                and not any(key in container for key in ('pagination', 'page', 'page_size')),
                'bank_exact_pagination_unverified')
    fields, items = data.get('fields'), data.get('items')
    require(type(fields) is list and len(fields) == len(FIELDS)
            and all(type(field) is str for field in fields) and set(fields) == set(FIELDS)
            and type(items) is list, 'bank_table_schema')
    require(len(items) == 1, 'bank_exact_row_count_unverified')
    require(type(items[0]) is list and len(items[0]) == len(fields), 'bank_row_schema')
    require(type(query) is dict and type(query.get('params')) is dict, 'bank_exact_query_contract')
    expected = exact_query(query['params'].get('ts_code'))
    require(query == expected, 'bank_exact_query_contract')
    partition = {'exchange': expected['params']['exchange'], 'list_status': 'L',
                 'checked_at': utc_text(checked_at), 'fields': list(FIELDS),
                 'items': [[items[0][fields.index(field)] for field in FIELDS]]}
    return _exact_observation(partition, expected, equity=equity)


def seal_exact_snapshot(observation, *, snapshot_id, equity=False):
    require(type(snapshot_id) is str and re.fullmatch(r'[0-9a-f]{32}', snapshot_id) is not None,
            'bank_snapshot_id')
    require(type(observation) is dict and set(observation) == {
        'query_scope', 'coverage', 'universe_coverage', 'historical_pit', 'query', 'partition',
        'business_code', 'has_more'}, 'bank_exact_observation_contract')
    canonical = _exact_observation(observation['partition'], observation['query'], equity=equity)
    # JSON equality prevents False/0 coercion in retained evidence.
    require(json_bytes(observation) == json_bytes(canonical), 'bank_exact_observation_contract')
    moment = instant(canonical['partition']['checked_at'])
    row = _rows(canonical['partition'])[0]
    return {'contract': exact_contract(equity), 'snapshot_id': snapshot_id, 'endpoint': ENDPOINT,
            'checked_at': utc_text(moment), 'expires_at': utc_text(moment + timedelta(seconds=TTL_SECONDS)),
            'query_sha256': hashlib.sha256(json_bytes(canonical['query'])).hexdigest(),
            'row_sha256': hashlib.sha256(json_bytes(row)).hexdigest(), **canonical}


def seal_snapshot(partitions, *, snapshot_id):
    require(type(snapshot_id) is str and re.fullmatch(r'[0-9a-f]{32}', snapshot_id) is not None,
            'bank_snapshot_id')
    require(type(partitions) is list and len(partitions) == 6, 'bank_incomplete_partitions')
    require([(part.get('exchange'), part.get('list_status')) for part in partitions] == list(PARTITIONS),
            'bank_partition_order')
    moments = [instant(part['checked_at']) for part in partitions]
    require(moments == sorted(moments) and (moments[-1] - moments[0]).total_seconds() <= 240,
            'bank_capture_window')
    seen = set()
    for part in partitions:
        for row in _rows(part):
            require(row['ts_code'] not in seen, 'bank_duplicate_security')
            seen.add(row['ts_code'])
    return {'contract': CONTRACT, 'snapshot_id': snapshot_id, 'endpoint': ENDPOINT,
            'checked_at': utc_text(moments[0]), 'expires_at': utc_text(moments[0] + timedelta(seconds=TTL_SECONDS)),
            'coverage': 'validated_requested_partitions_only', 'historical_pit': False,
            'partitions': partitions}


def _snapshot(source, reference, now, *, equity=False):
    require(type(reference) is str and re.fullmatch(r'[0-9a-f]{32}:[0-9a-f]{64}', reference) is not None,
            'bank_snapshot_reference')
    snapshot_id, digest = reference.split(':')
    root = Path(source) / snapshot_directory(equity) / snapshot_id
    ready = decode_json(read_bound(root / 'READY.json', maximum=4096))
    require(type(ready) is dict and type(ready.get('result_sha256')) is str, 'bank_snapshot_not_complete')
    if equity:
        require(ready.get('contract') == exact_contract(True), 'equity_contract_required')
    if ready.get('contract') == exact_contract(equity):
        return _read_exact_snapshot(root, reference, ready, now, equity=equity), digest
    require(ready == {'contract': CONTRACT, 'snapshot_id': snapshot_id, 'snapshot_sha256': digest,
                      'result_sha256': ready['result_sha256'], 'physical_attempts': 6, 'status': 'complete'},
            'bank_snapshot_not_complete')
    receipt = decode_json(read_bound(root / 'result.json', ready['result_sha256'], maximum=16384))
    expected = {'contract': CONTRACT, 'snapshot_id': snapshot_id, 'status': 'validated',
                'physical_attempts': 6, 'http_responses': 6, 'completed_partitions': 6,
                'cleanup_failed': False, 'snapshot_ref': reference}
    require(type(receipt) is dict and all(receipt.get(key) == value for key, value in expected.items()),
            'bank_capture_receipt_mismatch')
    value = decode_json(read_bound(root / 'snapshot.json', digest))
    require(type(value) is dict, 'bank_snapshot_contract')
    try:
        rebuilt = seal_snapshot(value['partitions'], snapshot_id=snapshot_id)
    except (KeyError, TypeError, AttributeError):
        raise BankIdentityError('bank_snapshot_contract') from None
    require(value == rebuilt, 'bank_snapshot_contract')
    require(instant(value['checked_at']) <= now and all(instant(part['checked_at']) <= now
            for part in value['partitions']), 'bank_future_snapshot')
    require(now < instant(value['expires_at']), 'bank_snapshot_expired')
    return value, digest


def _read_exact_snapshot(root, reference, ready, now, *, equity=False):
    snapshot_id, digest = reference.split(':')
    expected_ready = {'contract': exact_contract(equity), 'snapshot_id': snapshot_id,
                      'snapshot_sha256': digest, 'result_sha256': ready['result_sha256'],
                      'physical_attempts': 1, 'status': 'exact_positive'}
    require(json_bytes(ready) == json_bytes(expected_ready), 'bank_exact_ready_contract')
    receipt = decode_json(read_bound(root / 'result.json', ready['result_sha256'], maximum=16384))
    value = decode_json(read_bound(root / 'snapshot.json', digest))
    require(type(value) is dict, 'bank_snapshot_contract')
    observation_keys = {'query_scope', 'coverage', 'universe_coverage', 'historical_pit',
                        'query', 'partition', 'business_code', 'has_more'}
    require(observation_keys <= set(value), 'bank_exact_observation_contract')
    rebuilt = seal_exact_snapshot({key: value[key] for key in observation_keys}, snapshot_id=snapshot_id, equity=equity)
    require(json_bytes(value) == json_bytes(rebuilt), 'bank_snapshot_contract')
    expected = {'contract': exact_contract(equity), 'snapshot_id': snapshot_id, 'status': 'validated',
                'physical_attempts': 1, 'http_responses': 1, 'completed_partitions': 1,
                'cleanup_failed': False, 'snapshot_ref': reference,
                'query_sha256': value['query_sha256'], 'query_scope': 'exact_current_security'}
    require(type(receipt) is dict and all(key in receipt for key in expected)
            and json_bytes({key: receipt[key] for key in expected}) == json_bytes(expected),
            'bank_capture_receipt_mismatch')
    require(instant(value['checked_at']) <= now, 'bank_future_snapshot')
    require(now < instant(value['expires_at']), 'bank_snapshot_expired')
    return value


def _base(source, ticker):
    from tradingagents.dataflows.instrument_identity import (
        IdentityRegistryAvailable,
        RegistryFailureReason,
        resolve_authoritative_instrument_identity,
    )
    source = Path(source)
    release = decode_json(read_bound(source / 'config/bounded_mainland_release.json', maximum=2_000_000))
    entries = [item for item in release.get('files', []) if item.get('root') == 'project'
               and item.get('relative') == 'config/instrument_identity_registry.json']
    require(len(entries) == 1, 'bank_base_pin_missing')
    digest = entries[0]['sha256']
    raw = read_bound(source / 'config/instrument_identity_registry.json', digest, maximum=2_000_000)
    declared = read_bound(source / 'config/instrument_identity_registry.sha256', maximum=1024).decode('ascii').split()[0]
    require(declared == digest, 'bank_base_pin_mismatch')
    result = resolve_authoritative_instrument_identity(ticker,
        registry_path=source / 'config/instrument_identity_registry.json', expected_sha256=digest)
    require(isinstance(result, IdentityRegistryAvailable) or result.reason is RegistryFailureReason.NOT_FOUND,
            'bank_base_identity_invalid')
    return isinstance(result, IdentityRegistryAvailable), decode_json(raw), digest


def _owned_session(source, session):
    session = Path(session).absolute()
    require(re.fullmatch(r'[0-9a-f]{32}', session.name) is not None
            and session.parent == Path(source).absolute() / 'reports/bounded-runs', 'bank_session_path')
    value = decode_json(read_bound(session / 'session.json', maximum=4096))
    require(value == {'contract': 'bounded-mainland-session-v1', 'run_id': session.name,
                      'root': str(session), 'resume': False}, 'bank_session_owner')
    return session


def _overlay_inputs(source, ticker, as_of, reference, now, *, equity=False):
    known, base, base_hash = _base(source, ticker)
    require(not known, 'bank_overlay_for_known_identity')
    value, snapshot_hash = _snapshot(source, reference, now, equity=equity)
    require(type(ticker) is str and re.fullmatch(r'[0-9]{6}(?:\.(?:SH|SS|SZ))?', ticker) is not None,
            'bank_ticker_syntax')
    candidates = []
    exact = value['contract'] == exact_contract(equity)
    for part in ([value['partition']] if exact else value['partitions']):
        for row in _rows(part):
            canonical = row['ts_code'].replace('.SH', '.SS')
            aliases = [row['symbol'], row['ts_code']]
            if ticker in {canonical, *aliases}:
                candidates.append((part, row, canonical))
    require(len(candidates) == 1, 'bank_identity_unavailable_or_ambiguous')
    part, row, canonical = candidates[0]
    require((equity or row['industry'] == '银行') and row['curr_type'] == 'CNY', 'bank_classification_unavailable')
    identifiers = {row['symbol'], row['ts_code'], canonical}
    require(not any(identifiers & {existing['canonical_symbol'], *existing['aliases']} for existing in base['rows']),
            'bank_base_alias_conflict')
    try:
        requested = date.fromisoformat(as_of)
    except (TypeError, ValueError):
        raise BankIdentityError('bank_as_of_invalid') from None
    require(_day(row['list_date']) <= requested
            and (row['list_status'] != 'D' or requested < _day(row['delist_date'])), 'bank_outside_listing_interval')
    return base, base_hash, value, snapshot_hash, part, row, canonical, exact


def _overlay(source, session, ticker, as_of, reference, now, *, equity=False):
    base, base_hash, value, snapshot_hash, part, row, canonical, exact = _overlay_inputs(
        source, ticker, as_of, reference, now, equity=equity)
    # Other existing identities retain their original registry. This overlay is
    # selected only for this one base-NOT_FOUND security, never a global update.
    overlay_row = {'canonical_symbol': canonical, 'aliases': sorted({row['symbol'], row['ts_code']} - {canonical}),
        'venue': 'XSHG' if row['exchange'] == 'SSE' else 'XSHE', 'instrument_kind': 'equity', 'currency': 'CNY',
        'display_name': row['name'], 'provenance': {'provider': 'Tushare stock_basic CURRENT metadata',
        'source_ref': 'stock-basic:' + snapshot_hash + ':' + hashlib.sha256(json_bytes(row)).hexdigest(),
        'retrieved_at': part['checked_at']}}
    artifact = {'schema_version': '1.0', 'registry_id': ('equity-overlay:' if equity else 'bank-overlay:') + snapshot_hash,
                'rows': [*base['rows'], overlay_row]}
    raw = json_bytes(artifact)
    binding = {'contract': BINDING, 'snapshot_ref': reference, 'base_sha256': base_hash,
               'registry_sha256': hashlib.sha256(raw).hexdigest(), 'session_id': session.name,
               'symbol': canonical, 'as_of': as_of, 'expires_at': value['expires_at']}
    if exact:
        binding.update(contract=("equity-stock-basic-session-v1" if equity else EXACT_BINDING), query=value['query'], query_sha256=value['query_sha256'],
                       row_sha256=value['row_sha256'], query_scope='exact_current_security',
                       coverage='query_response_only', universe_coverage='partial', historical_pit=False)
    return binding, raw


def find_current_reference(source, ticker, as_of, *, now=None, equity=False):
    """Bounded local discovery only; admission still rebuilds the session overlay.

    Only missing READY, expired, or other-stock snapshots are ineligible. Corrupt,
    linked, future or unreadable evidence stops discovery; no network fallback.
    """
    known, _, _ = _base(source, ticker)
    if known:
        return None, 'base_registry'
    now = current_time(now)
    parent = Path(source) / snapshot_directory(equity)
    for item in (parent, *parent.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'bank_linked_path')
    try:
        parent.lstat()
    except FileNotFoundError:
        return None, 'refresh_required'
    import itertools
    entries = list(itertools.islice(parent.iterdir(), 257))
    require(len(entries) <= 256, 'bank_discovery_limit')
    eligible = []
    for directory in entries:
        if re.fullmatch(r'[0-9a-f]{32}', directory.name) is None:
            continue
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not directory.is_symlink()
                and not getattr(info, 'st_file_attributes', 0) & 0x400, 'bank_linked_path')
        ready_path = directory / 'READY.json'
        try:
            ready_info = ready_path.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(ready_info.st_mode) and not getattr(ready_info, 'st_file_attributes', 0) & 0x400,
                'bank_linked_path')
        ready = decode_json(read_bound(ready_path, maximum=4096))
        require(type(ready) is dict and type(ready.get('snapshot_sha256')) is str,
                'bank_snapshot_not_complete')
        reference = directory.name + ':' + ready['snapshot_sha256']
        try:
            inputs = _overlay_inputs(source, ticker, as_of, reference, now, equity=equity)
        except BankIdentityError as error:
            if str(error) in {'bank_snapshot_expired', 'bank_identity_unavailable_or_ambiguous'}:
                continue
            raise
        eligible.append((instant(inputs[2]['checked_at']), reference))
    return (max(eligible)[1], 'retained_snapshot') if eligible else (None, 'refresh_required')


def prepare_overlay(source, session, ticker, as_of, reference, *, now=None, equity=False):
    known, _, _ = _base(source, ticker)
    if known:
        return None
    session = _owned_session(source, session)
    binding, raw = _overlay(source, session, ticker, as_of, reference, current_time(now), equity=equity)
    with (session / ('equity-identity-registry.json' if equity else 'bank-identity-registry.json')).open('xb') as output:
        output.write(raw)
    read_bound(session / ('equity-identity-registry.json' if equity else 'bank-identity-registry.json'), binding['registry_sha256'])
    return binding


def validate_binding(source, session, binding, ticker, as_of, *, now=None, equity=False):
    keys = {'contract', 'snapshot_ref', 'base_sha256', 'registry_sha256', 'session_id', 'symbol', 'as_of', 'expires_at'}
    require(type(binding) is dict, 'bank_binding_contract')
    exact_contract_id = 'equity-stock-basic-session-v1' if equity else EXACT_BINDING
    exact = binding.get('contract') == exact_contract_id
    if exact:
        keys |= {'query', 'query_sha256', 'row_sha256', 'query_scope', 'coverage', 'universe_coverage', 'historical_pit'}
    require(set(binding) == keys and binding['contract'] in ({exact_contract_id} if equity else {BINDING, EXACT_BINDING}),
            'bank_binding_contract')
    session = _owned_session(source, session)
    expected, raw = _overlay(source, session, ticker, as_of, binding['snapshot_ref'], current_time(now), equity=equity)
    require(json_bytes(binding) == json_bytes(expected), 'bank_session_binding_mismatch')
    path = session / ('equity-identity-registry.json' if equity else 'bank-identity-registry.json')
    require(read_bound(path, binding['registry_sha256']) == raw, 'bank_overlay_changed')
    return {'instrument_identity_registry_path': str(path),
            'instrument_identity_registry_sha256': binding['registry_sha256']}


def verify_expiry(binding, *, now=None):
    require(current_time(now) < instant(binding['expires_at']), 'bank_snapshot_expired')
