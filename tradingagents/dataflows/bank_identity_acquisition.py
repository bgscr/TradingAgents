"""Explicit six-part CURRENT identity capture. No analysis-time acquisition."""
from __future__ import annotations

import hashlib
import http.client
import os
import sqlite3
import ssl
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingagents.dataflows import bank_identity_snapshot as bank

SCHEMA_DIAGNOSTIC_CONTRACT = 'bank-response-structure-v1'
DIAGNOSTIC_CONTRACT = 'bank-stock-basic-schema-diagnostic-v1'
# A logging vocabulary, not an expansion of the response acceptance contract.
_DIAGNOSTIC_KEYS = frozenset({
    'code', 'msg', 'request_id', 'data', 'fields', 'items', 'has_more',
    'truncated', 'total', 'total_count', 'offset', 'limit', 'count',
    'pagination', 'page', 'page_size', 'next', 'next_page', 'metadata',
})
_SCHEMA_KEY_LIMIT = 32
_SCHEMA_ROW_LIMIT = 6000


_SAFE_BANK_REASONS = frozenset({
    'bank_exact_ticker_requires_venue', 'bank_exact_query_contract', 'bank_exact_row_count_unverified',
    'bank_exact_identity_mismatch', 'bank_exact_pagination_unverified', 'bank_exact_observation_contract',
    'bank_exact_ready_contract', 'bank_exact_mode_conflict', 'bank_listing_date_after_observation',
    'bank_as_of_invalid', 'bank_attempt_limit', 'bank_base_alias_conflict',
    'bank_base_identity_invalid', 'bank_base_pin_mismatch', 'bank_base_pin_missing',
    'bank_binding_contract', 'bank_capacity_unverified', 'bank_capture_deadline',
    'bank_capture_incomplete', 'bank_capture_receipt_mismatch', 'bank_capture_stopped',
    'bank_capture_window', 'bank_classification_unavailable', 'bank_content_encoding',
    'bank_coordinator_attempt_mismatch', 'bank_coordinator_close_failed', 'bank_count_unverified',
    'bank_credential_echo', 'bank_diagnostic_mode', 'bank_diagnostic_receipt_limit',
    'bank_diagnostic_structure_limit', 'bank_diagnostics_write_failed', 'bank_digest_mismatch',
    'bank_duplicate_json_key', 'bank_duplicate_security', 'bank_existing_ceiling_required',
    'bank_field_type', 'bank_file_changed', 'bank_file_size',
    'bank_file_unreadable', 'bank_future_snapshot', 'bank_http_rate_limited',
    'bank_http_status_rejected', 'bank_identity_unavailable_or_ambiguous', 'bank_incomplete_partitions',
    'bank_invalid_credential_input', 'bank_invalid_digest', 'bank_invalid_json',
    'bank_invalid_listing_date', 'bank_invalid_utc_time', 'bank_io_deadline',
    'bank_linked_path', 'bank_listing_interval', 'bank_missing_identity_field',
    'bank_missing_listing_date', 'bank_noncanonical_utc_time', 'bank_nonfinite_json',
    'bank_not_regular_file', 'bank_offset_unverified', 'bank_outside_listing_interval',
    'bank_overlay_changed', 'bank_overlay_for_known_identity', 'bank_partition_contract',
    'bank_partition_order', 'bank_partition_scope', 'bank_provider_rejected',
    'bank_response_schema', 'bank_response_too_large', 'bank_row_identity_conflict',
    'bank_row_schema', 'bank_session_binding_mismatch', 'bank_session_owner',
    'bank_session_path', 'bank_snapshot_contract', 'bank_snapshot_expired',
    'bank_snapshot_id', 'bank_snapshot_not_complete', 'bank_snapshot_reference',
    'bank_table_schema', 'bank_ticker_syntax', 'bank_time_requires_timezone',
    'bank_transport_close_failed', 'bank_truncated_response', 'bank_unknown_response_metadata',
    'bank_unknown_table_metadata', 'bank_windows_publication_required',
})


def _safe_reason(error):
    if (type(error) is bank.BankIdentityError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _SAFE_BANK_REASONS):
        return error.args[0]
    return 'bank_capture_stopped'


def _type_name(value):
    return {type(None): 'null', bool: 'boolean', int: 'integer', float: 'number',
            str: 'string', list: 'array', dict: 'object'}.get(type(value), 'other')


def _object_shape(value):
    result = {'type': _type_name(value)}
    if type(value) is not dict:
        return result
    result.update(key_count=len(value), keys=[], keys_omitted=max(0, len(value) - _SCHEMA_KEY_LIMIT))
    for ordinal, (key, item) in enumerate(value.items(), 1):
        if ordinal > _SCHEMA_KEY_LIMIT:
            break
        # Never truncate or hash arbitrary names: even an identifier can be a secret.
        name = key if key in _DIAGNOSTIC_KEYS else 'anonymous_key_' + str(ordinal)
        result['keys'].append({'name': name, 'type': _type_name(item)})
    return result


def response_structure(value, byte_count):
    """Closed, bounded structure only. Call after the credential echo check."""
    data = value.get('data') if type(value) is dict else None
    fields = data.get('fields') if type(data) is dict else None
    items = data.get('items') if type(data) is dict else None
    summary = {'contract': SCHEMA_DIAGNOSTIC_CONTRACT, 'response_bytes': byte_count,
               'envelope': _object_shape(value), 'data': _object_shape(data),
               'field_count': len(fields) if type(fields) is list else None,
               'row_count': len(items) if type(items) is list else None,
               'fields': [], 'fields_omitted': 0,
               'rows_inspected': 0, 'rows_omitted': 0, 'invalid_row_types': 0,
               'minimum_row_width': None, 'maximum_row_width': None}
    if type(fields) is list:
        summary['fields_omitted'] = max(0, len(fields) - _SCHEMA_KEY_LIMIT)
        for ordinal, field in enumerate(fields[:_SCHEMA_KEY_LIMIT], 1):
            name = field if type(field) is str and field in bank.FIELDS else 'anonymous_field_' + str(ordinal)
            summary['fields'].append({'name': name, 'type': _type_name(field)})
    if type(items) is list:
        rows = items[:_SCHEMA_ROW_LIMIT]
        summary['rows_inspected'] = len(rows)
        summary['rows_omitted'] = len(items) - len(rows)
        widths = [len(row) for row in rows if type(row) is list]
        summary['invalid_row_types'] = len(rows) - len(widths)
        if widths:
            summary['minimum_row_width'], summary['maximum_row_width'] = min(widths), max(widths)
    # Fingerprint only this sanitized structure, never the provider body or values.
    raw = bank.json_bytes(summary)
    bank.require(len(raw) <= 16384, 'bank_diagnostic_structure_limit')
    return {**summary, 'structure_sha256': hashlib.sha256(raw).hexdigest()}


def _persist_structure(root, receipt):
    """One sidecar attempt; failure cannot publish READY or retry this write."""
    observations = receipt.pop('_response_diagnostics', [])
    if not observations:
        return
    receipt['schema_diagnostics_status'] = 'write_failed'
    raw = bank.json_bytes({'contract': SCHEMA_DIAGNOSTIC_CONTRACT, 'observations': observations})
    bank.require(len(raw) <= 131072, 'bank_diagnostic_receipt_limit')
    try:
        durable_new(root / 'schema_diagnostics.json', raw)
    except BaseException as error:
        receipt['diagnostic_write_error_type'] = safe_type(error)
        raise bank.BankIdentityError('bank_diagnostics_write_failed') from None
    receipt['schema_diagnostics_status'] = 'written'
    receipt['schema_diagnostics_sha256'] = hashlib.sha256(raw).hexdigest()


class CaptureClock:
    def __init__(self, *, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.clock, self.wall, self._sleep = clock, wall, sleep
        self.start, self.start_epoch = clock(), wall()
        self.end, self.end_epoch = self.start + 240, self.start_epoch + 240

    def remaining(self):
        value = min(self.end - self.clock(), self.end_epoch - self.wall())
        bank.require(value > 0, 'bank_capture_deadline')
        return value

    def now(self):
        return datetime.fromtimestamp(self.wall(), timezone.utc)

    def sleep(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            remaining = self.remaining()
            self._sleep(min(0.5, remaining, end - self.clock()))
        self.remaining()


def _ceiling(coordinator, upstream_id):
    value = coordinator.configured_operator_ceiling(upstream_id)
    bank.require(value is not None and value.minimum_interval.total_seconds() >= 1.5
                 and value.allow_prewarming is False, 'bank_existing_ceiling_required')


@contextmanager
def existing_coordinator(source, receipt=None):
    """Reuse existing authority. No DDL, migration, new authority or policy reset."""
    from scripts.diagnostic_runtime import SharedStatementStorage
    from tradingagents.market_history.config import (
        MarketHistoryConfig,
        provider_request_authority_config,
    )
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.schema import SCHEMA_VERSION
    from tradingagents.market_history.store import MarketHistoryStore

    source = Path(source).absolute()
    market = source / 'data/market_history/market_history.sqlite3'
    authority = market.with_name(market.name + '.provider-requests.sqlite3')
    # Path/link checks happen before stat/connect, including existing sidecars.
    SharedStatementStorage.path(authority)
    info = authority.stat()
    storage = SharedStatementStorage({'market_database': str(market), 'coordinator_database': str(authority),
        'cache_database': str(source / 'data/cache/current-financial-statements.sqlite3'),
        'coordinator_file_id': [info.st_dev, info.st_ino]})
    upstream_id, service = upstream_service_identity_for_provider('tushare')
    storage.validate_authority(SCHEMA_VERSION, upstream_id, service)
    config = provider_request_authority_config(MarketHistoryConfig.from_mapping({
        'market_history_mode': 'current', 'data_usage_mode': 'personal_research',
        'market_history_database_path': str(market),
        'market_history_payload_root': str(source / 'data/market_history/payloads'),
        'market_history_backup_root': str(source / 'data/market_history/backups')}))
    connection = sqlite3.connect(authority.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=1)
    store = MarketHistoryStore(config, connection)
    coordinator = ProviderRequestCoordinator(store)
    primary = None
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        storage.check_existing()
        _ceiling(coordinator, upstream_id)
        yield coordinator, storage
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_failed = False
        for name, resource in (('coordinator', coordinator), ('store', store)):
            try:
                resource.close()
            except BaseException as error:
                cleanup_failed = True
                if receipt is not None:
                    receipt.setdefault('cleanup_errors', []).append(
                        {'stage': 'coordinator_close', 'resource': name, 'error_type': safe_type(error)})
        if cleanup_failed:
            if receipt is not None:
                receipt['cleanup_failed'] = True
                receipt['cleanup_stage'] = 'coordinator_close'
            if primary is None:
                raise bank.BankIdentityError('bank_coordinator_close_failed')


def request_partition(token, exchange, status, clock, receipt, *, maximum_attempts=6, exact_query=None, equity=False):
    """The only wire boundary: verified TLS, fixed path, no redirect/retry/cookies."""
    bank.require((exchange, status) in bank.PARTITIONS, 'bank_partition_scope')
    bank.require(type(maximum_attempts) is int and maximum_attempts in (1, 6)
                 and receipt['physical_attempts'] < maximum_attempts, 'bank_attempt_limit')
    document = {'api_name': 'stock_basic', 'token': token,
        'params': {'exchange': exchange, 'list_status': status, 'ts_type_name': 'https://api.waditu.com/dataapi'},
        'fields': ','.join(bank.FIELDS)}
    if exact_query is not None:
        bank.require(exact_query == bank.exact_query(exact_query['params']['ts_code'])
                     and exact_query['params']['exchange'] == exchange and status == 'L'
                     and maximum_attempts == 1, 'bank_exact_query_contract')
        document = {**exact_query, 'token': token}
    body = bank.json_bytes(document)
    operation_end = clock.clock() + min(30, clock.remaining())

    def timeout():
        value = min(30, clock.remaining(), operation_end - clock.clock())
        bank.require(value > 0, 'bank_io_deadline')
        return value

    observation = {'ordinal': receipt['physical_attempts'] + 1,
        'partition': {'exchange': exchange, 'list_status': status},
        'stage': 'transport_initialization', 'http_status': None, 'response_bytes': 0,
        'structure': None, 'primary_error_type': None, 'cleanup_errors': []}
    receipt.setdefault('_response_diagnostics', []).append(observation)
    connection = None
    response = None
    primary = None
    try:
        connection = http.client.HTTPSConnection('api.waditu.com', 443, timeout=timeout(),
                                               context=ssl.create_default_context())
        observation['stage'] = 'request_send'
        connection.timeout = timeout()
        from tradingagents.full_session_ledger import owner
        if owner() is not None:
            connection.timeout=min(connection.timeout,owner().reserve("data"))
        if owner() is not None and owner().child_role=="identity" and owner().params.get("contract")=="default-full-latest-intake-v1":
            from tradingagents.market_history.coordinator import record_active_physical_attempt_io
            record_active_physical_attempt_io()
        receipt['physical_attempts'] += 1  # Before request(); delivery is not guaranteed.
        connection.request('POST', '/dataapi/stock_basic', body=body,
                           headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        if connection.sock is not None:
            connection.sock.settimeout(timeout())
        observation['stage'] = 'response_headers'
        response = connection.getresponse()
        receipt['http_responses'] += 1
        receipt['http_statuses'].append(response.status if type(response.status) is int else None)
        observation['http_status'] = receipt['http_statuses'][-1]
        if response.status == 429:
            from tradingagents.market_history.coordinator import (
                PhysicalAttemptFailure,
                PhysicalAttemptOutcome,
            )
            from tradingagents.tushare_financial_https import _retry_after_seconds
            raw_delay = response.getheader('Retry-After')
            delay = _retry_after_seconds(raw_delay) if isinstance(raw_delay, str) and len(raw_delay) <= 128 else None
            # Missing/invalid/excessive server metadata uses the existing 300s
            # conservative cooldown policy, never an immediate retry.
            delay = delay if delay is not None and 0 < delay <= 31_536_000 else 300.0
            raise PhysicalAttemptFailure(outcome=PhysicalAttemptOutcome.RATE_LIMITED, retryable=False,
                status_code=429, error_code='stock_basic_rate_limited', retry_after_seconds=delay)
        bank.require(response.status == 200, 'bank_http_status_rejected')
        length = response.getheader('Content-Length')
        if length is not None:
            bank.require(length.isdecimal() and int(length) <= bank.MAX_RESPONSE_BYTES, 'bank_response_too_large')
        bank.require(response.getheader('Content-Encoding') in (None, 'identity'), 'bank_content_encoding')
        observation['stage'] = 'response_body'
        raw = bytearray()
        while True:
            if connection.sock is not None:
                connection.sock.settimeout(timeout())
            piece = response.read1(min(65536, bank.MAX_RESPONSE_BYTES + 1 - len(raw)))
            if not piece:
                break
            raw.extend(piece)
            observation['response_bytes'] = len(raw)
            bank.require(len(raw) <= bank.MAX_RESPONSE_BYTES, 'bank_response_too_large')
        timeout()
        observation['stage'] = 'json_decode'
        value = bank.decode_json(bytes(raw))
        # The provider's free-form messages/body are never persisted. Reject a
        # credential echo even if it appears in a selected public table field.
        observation['stage'] = 'credential_echo_check'
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                bank.require(token not in item, 'bank_credential_echo')
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        observation['stage'] = 'response_schema'
        observation['structure'] = response_structure(value, len(raw))
        result = (bank.parse_exact_response(value, exact_query, clock.now(), equity=equity) if exact_query is not None
                  else bank.parse_partition(value, exchange, status, clock.now()))
        observation['stage'] = 'partition_validated'
        return result
    except BaseException as error:
        primary = error
        observation['primary_error_type'] = safe_type(error)
        observation['primary_error_details'] = safe_error_details(error)
        receipt.setdefault('primary_error_details', observation['primary_error_details'])
        raise
    finally:
        errors = []
        for name, resource in (('response', response), ('connection', connection)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    errors.append('close_failed')
                    observation['cleanup_errors'].append({'resource': name, 'error_type': safe_type(error)})
        if errors:
            receipt['cleanup_failed'] = True
            receipt['cleanup_stage'] = 'transport_close'
            if primary is None:
                raise bank.BankIdentityError('bank_transport_close_failed')


def capture(token, snapshot_id, coordinator, storage, clock, receipt, *, diagnose_schema=False, exact_query=None, equity=False):
    bank.require(type(diagnose_schema) is bool, 'bank_diagnostic_mode')
    bank.require(not diagnose_schema or exact_query is None, 'bank_exact_mode_conflict')
    from tradingagents.market_history.coordinator import (
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
        RequestPriority,
        upstream_service_identity_for_provider,
    )
    upstream_id, _ = upstream_service_identity_for_provider('tushare')
    parts = []
    maximum_attempts = 1 if diagnose_schema or exact_query is not None else 6
    partitions = ((exact_query['params']['exchange'], 'L'),) if exact_query is not None else (
        bank.PARTITIONS[:1] if diagnose_schema else bank.PARTITIONS)
    for exchange, status in partitions:
        clock.remaining()
        storage.check_existing()
        _ceiling(coordinator, upstream_id)

        def physical_request(exchange=exchange, status=status):
            storage.check_existing()
            _ceiling(coordinator, upstream_id)
            clock.remaining()
            try:
                return request_partition(token, exchange, status, clock, receipt,
                                         maximum_attempts=maximum_attempts, exact_query=exact_query, equity=equity)
            except PhysicalAttemptFailure as error:
                receipt['primary_error_type'] = 'PhysicalAttemptFailure'
                receipt['reason'] = 'bank_http_rate_limited'
                receipt['retry_after_seconds'] = error.retry_after_seconds
                raise
            except BaseException as error:
                receipt['primary_error_type'] = safe_type(error)
                receipt.setdefault('primary_error_details', safe_error_details(error))
                if isinstance(error, bank.BankIdentityError):
                    receipt['reason'] = _safe_reason(error)
                # A single typed terminal event, no automatic retry, no messages.
                outcome = PhysicalAttemptOutcome.TIMEOUT if isinstance(error, TimeoutError) else PhysicalAttemptOutcome.PROVIDER_ERROR
                raise PhysicalAttemptFailure(outcome=outcome, retryable=False,
                                             error_code='stock_basic_capture_stopped') from None

        from tradingagents.full_session_ledger import owner
        result = coordinator.execute_direct_physical_request(
            request_key='stock-basic:' + snapshot_id + ':' + exchange + ':' + status,
            upstream_service_id=upstream_id, owner_id='stock-basic:' + snapshot_id,
            priority=RequestPriority.INCREMENTAL_REFRESH, now=clock.now, sleep=clock.sleep,
            lease_duration=timedelta(seconds=35), operation='identity:stock_basic',
            physical_request=physical_request, cooldown_scope='stock_basic',
            record_at_physical_io=owner() is not None and owner().child_role=="identity" and owner().params.get('contract')=='default-full-latest-intake-v1')
        bank.require(result.physical_attempt_count == 1, 'bank_coordinator_attempt_mismatch')
        parts.append(result.value)
        receipt['completed_partitions'] += 1
        clock.remaining()
    bank.require(receipt['physical_attempts'] == receipt['http_responses'] == maximum_attempts
                 and not receipt['cleanup_failed'], 'bank_capture_incomplete')
    if diagnose_schema:
        return None  # A valid single partition never constitutes an identity snapshot.
    if exact_query is not None:
        return bank.seal_exact_snapshot(parts[0], snapshot_id=snapshot_id, equity=equity)
    return bank.seal_snapshot(parts, snapshot_id=snapshot_id)


def safe_type(error):
    for kind, name in ((bank.BankIdentityError, 'BankIdentityError'), (TimeoutError, 'TimeoutError'),
                       (ssl.SSLError, 'SSLError'), (http.client.HTTPException, 'HTTPException'),
                       (OSError, 'OSError')):
        if isinstance(error, kind):
            return name
    return 'other'


def safe_error_details(error):
    """Bounded categories and numeric OS codes; never render exception content.

    Follow an explicit cause, otherwise an unsuppressed context, at most four
    nodes. Do not inspect args/reason/response/request attributes of wrappers.
    Extraction failure discards the entire diagnostic, not the primary error.
    """
    try:
        chain, seen = [], set()
        while error is not None and len(chain) < 4 and id(error) not in seen:
            seen.add(id(error))
            item = {'error_type': safe_type(error)}
            if isinstance(error, OSError):
                for field in ('errno', 'winerror'):
                    value = getattr(error, field, None)
                    if type(value) is int and -(2 ** 31) <= value <= 2 ** 32 - 1:
                        item[field] = value
            chain.append(item)
            # Read BaseException slots, without invoking wrapper properties.
            cause = BaseException.__cause__.__get__(error)
            suppressed = BaseException.__suppress_context__.__get__(error)
            error = cause if cause is not None else (
                None if suppressed else BaseException.__context__.__get__(error))
        return {'chain': chain, 'truncated': error is not None, 'extraction_failed': False}
    except BaseException:
        return {'chain': [], 'truncated': False, 'extraction_failed': True}


def durable_new(path, raw):
    with path.open('xb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def publish_ready(root, ready, clock):
    # Windows os.rename refuses an existing destination. Every fallible write,
    # flush and close occurs before this one commit point; pending is not read
    # by consumers. No old file is overwritten or removed.
    bank.require(os.name == 'nt', 'bank_windows_publication_required')
    pending, final = root / 'READY.pending.json', root / 'READY.json'
    durable_new(pending, bank.json_bytes(ready))
    clock.remaining()
    os.rename(pending, final)


def update(source, token, *, clock=None, diagnose_schema=False, exact_symbol=None, equity=False):
    """Explicit user action only. Fresh output; partial/failed captures never READY."""
    bank.require(type(token) is str and 0 < len(token) <= 512 and token == token.strip()
                 and all(32 < ord(c) < 127 for c in token), 'bank_invalid_credential_input')
    bank.require(os.name == 'nt', 'bank_windows_publication_required')
    bank.require(type(diagnose_schema) is bool, 'bank_diagnostic_mode')
    bank.require(not diagnose_schema or exact_symbol is None, 'bank_exact_mode_conflict')
    query = bank.exact_query(exact_symbol) if exact_symbol is not None else None
    source = Path(source).absolute()
    bank.require(not equity or query is not None,'equity_exact_query_required')
    parent = source / bank.snapshot_directory(equity)
    for item in (parent, *parent.parents):
        if item.exists():
            info = item.lstat()
            bank.require(not item.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400,
                         'bank_linked_path')
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / uuid.uuid4().hex
    root.mkdir(exist_ok=False)
    clock = CaptureClock() if clock is None else clock
    receipt = {'contract': bank.CONTRACT, 'snapshot_id': root.name, 'status': 'stopped',
        'physical_attempts': 0, 'http_responses': 0, 'http_statuses': [], 'completed_partitions': 0,
        'cleanup_failed': False, 'sdk_retries': 0, 'resume': False, 'maximum_requests': 6,
        'scope': 'identity_metadata_only', 'historical_pit': False}
    if diagnose_schema:
        receipt.update(contract=DIAGNOSTIC_CONTRACT, maximum_requests=1,
                       scope='response_structure_only', snapshot_usable=False)
    if query is not None:
        receipt.update(contract=bank.exact_contract(equity), maximum_requests=1,
                       query_scope='exact_current_security', scope='positive_identity_only',
                       query_sha256=hashlib.sha256(bank.json_bytes(query)).hexdigest())
    # This exclusive marker precedes DB coordination and any physical send.
    with (root / 'started.json').open('xb') as output:
        output.write(bank.json_bytes({'snapshot_id': root.name, 'started_at': bank.utc_text(clock.now()), 'resume': False}))
    try:
        with existing_coordinator(source, receipt) as (coordinator, storage):
            value = capture(token, root.name, coordinator, storage, clock, receipt,
                             diagnose_schema=diagnose_schema, exact_query=query, equity=equity)
        clock.remaining()
        _persist_structure(root, receipt)
        clock.remaining()
        if diagnose_schema:
            receipt['status'] = 'diagnostic_complete'
            durable_new(root / 'result.json', bank.json_bytes(receipt))
            return receipt
        raw = bank.json_bytes(value)
        digest = hashlib.sha256(raw).hexdigest()
        durable_new(root / 'snapshot.json', raw)
        bank.read_bound(root / 'snapshot.json', digest)
        receipt.update(status='validated', snapshot_ref=root.name + ':' + digest)
        result_raw = bank.json_bytes(receipt)
        durable_new(root / 'result.json', result_raw)
        publish_ready(root, {'contract': bank.exact_contract(equity) if query is not None else bank.CONTRACT, 'snapshot_id': root.name,
            'snapshot_sha256': digest, 'result_sha256': hashlib.sha256(result_raw).hexdigest(),
            'physical_attempts': 1 if query is not None else 6,
            'status': 'exact_positive' if query is not None else 'complete'}, clock)
        # Commit succeeded. No further filesystem writes; printing failure is
        # separate from capture/publication and must not trigger a new update.
        return {**receipt, 'status': 'exact_positive' if query is not None else 'complete'}
    except BaseException as error:
        receipt['status'] = 'stopped'
        receipt.pop('snapshot_ref', None)
        receipt.setdefault('primary_error_type', safe_type(error))
        receipt.setdefault('primary_error_details', safe_error_details(error))
        if isinstance(error, bank.BankIdentityError):
            receipt.setdefault('reason', _safe_reason(error))
        else:
            receipt.setdefault('reason', 'bank_capture_stopped')
    name = 'failure.json' if (root / 'result.json').exists() else 'result.json'
    if '_response_diagnostics' in receipt:
        try:
            _persist_structure(root, receipt)
        except bank.BankIdentityError as error:
            receipt.setdefault('primary_error_type', safe_type(error))
            receipt.setdefault('reason', _safe_reason(error))
    durable_new(root / name, bank.json_bytes(receipt))
    return receipt
