"""Real coordinator + fixed transport construction; only the external wire is fake."""
import hashlib
import json
import sqlite3
import sys
from datetime import timedelta

import pytest

from tests.test_bank_identity_snapshot import NOW, payload, source_layout
from tradingagents.dataflows import (
    bank_identity_acquisition as acquire,
    bank_identity_snapshot as bank,
)
from tradingagents.market_history.config import (
    MarketHistoryConfig,
    provider_request_authority_config,
)
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore

TOKEN = 'synthetic-not-a-real-credential'


class Time:
    value = 0.0
    def clock(self):
        return self.value
    def wall(self):
        return NOW.timestamp() + self.value
    def sleep(self, seconds):
        self.value += seconds
    def bound(self):
        return acquire.CaptureClock(clock=self.clock, wall=self.wall, sleep=self.sleep)


def authority(source, *, interval=1.5):
    config = MarketHistoryConfig.from_mapping({'market_history_mode': 'current', 'data_usage_mode': 'personal_research',
        'market_history_database_path': str(source / 'data/market_history/market_history.sqlite3'),
        'market_history_payload_root': str(source / 'data/market_history/payloads'),
        'market_history_backup_root': str(source / 'data/market_history/backups')})
    with MarketHistoryStore.open(config):
        pass
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        coord = ProviderRequestCoordinator(store)
        uid, service = upstream_service_identity_for_provider('tushare')
        coord.register_upstream_service(uid, service)
        coord.configure_operator_ceiling(upstream_service_id=uid, minimum_interval=timedelta(seconds=interval),
                                        allow_prewarming=False, configured_at=NOW)
    (source / 'data/cache').mkdir()
    return provider_request_authority_config(config).database_path


def wire(monkeypatch, *, mode=None, timer=None, exact_symbol=None):
    calls, closed, replies = [], [], []

    class Response:
        status = 302 if mode == 'redirect' else 401 if mode == '401' else 429 if mode == '429' else 200
        def __init__(self, data):
            self.raw = data
        def getheader(self, name):
            if name == 'Retry-After' and mode == '429':
                return '3600'
            if name == 'Content-Length':
                return str(bank.MAX_RESPONSE_BYTES + 1) if mode == 'oversize' else str(len(self.raw))
            return None
        def read1(self, maximum):
            data, self.raw = self.raw[:maximum], self.raw[maximum:]
            return data
        def close(self):
            replies.append('closed')

    class Connection:
        sock = None
        def __init__(self, host, port, *, timeout, context):
            assert host == 'api.waditu.com' and port == 443 and 0 < timeout <= 30
            assert context.check_hostname and context.verify_mode == 2
        def request(self, method, path, *, body, headers):
            document = json.loads(body)
            assert method == 'POST' and path == '/dataapi/stock_basic'
            assert set(headers) == {'Content-Type', 'Accept'}
            assert document['api_name'] == 'stock_basic' and document['token'] == TOKEN
            assert document['fields'] == ','.join(bank.FIELDS)
            assert set(document['params']) == {'exchange', 'list_status', 'ts_type_name'} | ({'ts_code'} if exact_symbol else set())
            if exact_symbol:
                assert document['params']['ts_code'] == exact_symbol
            assert document['params']['ts_type_name'] == 'https://api.waditu.com/dataapi'
            self.parameters = document['params']
            calls.append((self.parameters['exchange'], self.parameters['list_status']))
            if mode == 'timeout':
                raise TimeoutError('secret=' + TOKEN)
            if mode == 'disconnect':
                raise ConnectionResetError('secret=' + TOKEN)
            if mode == 'deadline':
                timer.value += 241
        def getresponse(self):
            exchange, status = self.parameters['exchange'], self.parameters['list_status']
            value = payload(exchange, status, symbol=exact_symbol)
            if exact_symbol:
                value['data'].update(has_more=False, count=2318)
                value['provider_extension'] = 'synthetic-noncanonical'
            if status != 'L':
                value['data']['items'] = []
            if mode == 'provider':
                value['code'] = 40101
                value['msg'] = TOKEN
            if mode == 'truncated':
                value['data']['has_more'] = True
            if mode == 'unknown-echo':
                value['unreviewed_extension'] = {'secret': TOKEN}
            if mode == 'key-echo':
                value[TOKEN] = 'unknown'
            if mode == 'zero':
                value['data']['items'] = []
            if mode == 'two':
                value['data']['items'] *= 2
            if mode == 'missing-page':
                value['data'].pop('has_more', None)
            if mode == 'wrong-symbol':
                value['data']['items'][0][0] = '600123.SH'
            if mode == 'echo':
                value['data']['items'][0][2] = TOKEN
            raw = b'{"code":0,"code":0}' if mode == 'duplicate' else bank.json_bytes(value)
            return Response(raw)
        def close(self):
            closed.append('closed')
            if mode == 'close':
                raise OSError(TOKEN)
    monkeypatch.setattr(acquire.http.client, 'HTTPSConnection', Connection)
    return calls, closed, replies


def stored(db, sql):
    with sqlite3.connect(db) as connection:
        return connection.execute(sql).fetchall()


def test_six_partition_update_uses_real_existing_coordinator_and_consumer(tmp_path, monkeypatch):
    source, run = source_layout(tmp_path)
    db = authority(source, interval=6)
    before = stored(db, 'SELECT operator_ceiling_json FROM upstream_services')
    timer = Time()
    calls, closed, replies = wire(monkeypatch)
    result = acquire.update(source, TOKEN, clock=timer.bound())
    assert result['status'] == 'complete' and result['physical_attempts'] == result['http_responses'] == 6
    assert calls == list(bank.PARTITIONS) and len(closed) == len(replies) == 6
    assert timer.value >= 30
    assert stored(db, 'SELECT operator_ceiling_json FROM upstream_services') == before
    assert len(stored(db, 'SELECT * FROM provider_request_attempts')) == 6
    assert [row[0] for row in stored(db, 'SELECT outcome FROM provider_request_attempts')] == ['available'] * 6
    binding = bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', result['snapshot_ref'], now=timer.bound().now())
    bank.validate_binding(source, run, binding, '000123.SZ', '2026-10-16', now=timer.bound().now())
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert TOKEN.encode() not in b''.join(p.read_bytes() for p in root.iterdir())
    assert hashlib.sha256((root / 'snapshot.json').read_bytes()).hexdigest() == result['snapshot_ref'].split(':')[1]


@pytest.mark.parametrize('mode', ['redirect', '401', 'timeout', 'disconnect', 'oversize', 'truncated',
                                  'provider', 'duplicate', 'echo', 'deadline', 'close'])
def test_first_failure_no_retry_no_ready_no_secret(tmp_path, monkeypatch, mode):
    source, _ = source_layout(tmp_path)
    db = authority(source)
    timer = Time()
    calls, closed, _ = wire(monkeypatch, mode=mode, timer=timer)
    result = acquire.update(source, TOKEN, clock=timer.bound())
    assert result['status'] == 'stopped' and result['physical_attempts'] == 1
    assert len(calls) == len(closed) == 1 and result['completed_partitions'] == 0
    assert len(stored(db, 'SELECT * FROM provider_request_attempts')) == 1
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists() and not (root / 'snapshot.json').exists()
    assert TOKEN.encode() not in b''.join(p.read_bytes() for p in root.iterdir())
    assert 'secret=' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['missing', 'weak', 'cooldown'])
def test_no_policy_reset_or_send_before_existing_authority_allows(tmp_path, monkeypatch, kind):
    source, _ = source_layout(tmp_path)
    db = authority(source, interval=1 if kind == 'weak' else 1.5)
    if kind == 'missing':
        with sqlite3.connect(db) as connection:
            connection.execute('UPDATE upstream_services SET operator_ceiling_json=NULL')
    if kind == 'cooldown':
        # A stricter interval beyond the capture deadline is retained, not reset.
        with sqlite3.connect(db) as connection:
            value = json.loads(connection.execute('SELECT operator_ceiling_json FROM upstream_services').fetchone()[0])
            value['minimum_interval_seconds'] = 300
            connection.execute('UPDATE upstream_services SET operator_ceiling_json=?', (json.dumps(value),))
    before = stored(db, 'SELECT operator_ceiling_json FROM upstream_services')
    calls, _, _ = wire(monkeypatch)
    result = acquire.update(source, TOKEN, clock=Time().bound())
    assert result['status'] == 'stopped'
    assert len(calls) == (1 if kind == 'cooldown' else 0)
    assert stored(db, 'SELECT operator_ceiling_json FROM upstream_services') == before
    assert result.get('snapshot_ref') is None


@pytest.mark.parametrize('token', ['abc\\def', 'ab"cdef'])
def test_json_escaped_credential_echo_not_saved(tmp_path, monkeypatch, token):
    monkeypatch.setattr(sys.modules[__name__], 'TOKEN', token)
    source, _ = source_layout(tmp_path)
    authority(source)
    calls, _, _ = wire(monkeypatch, mode='echo')
    result = acquire.update(source, token, clock=Time().bound())
    assert result['status'] == 'stopped' and result['reason'] == 'bank_credential_echo'
    assert len(calls) == 1
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists() and not (root / 'snapshot.json').exists()
    assert token not in str([json.loads(p.read_text()) for p in root.iterdir()])


def test_429_records_real_persistent_cooldown_and_next_update_sends_zero(tmp_path, monkeypatch):
    source, _ = source_layout(tmp_path)
    db = authority(source)
    timer = Time()
    calls, _, _ = wire(monkeypatch, mode='429')
    first = acquire.update(source, TOKEN, clock=timer.bound())
    assert first['status'] == 'stopped' and first['physical_attempts'] == 1
    assert first['retry_after_seconds'] == 3600
    cooldown = stored(db, 'SELECT cooldown_until, retry_after_seconds FROM request_cooldowns')
    assert cooldown and cooldown[0][1] == 3600
    second = acquire.update(source, TOKEN, clock=timer.bound())
    assert second['status'] == 'stopped' and second['physical_attempts'] == 0
    assert len(calls) == 1 and len(stored(db, 'SELECT * FROM provider_request_attempts')) == 1
    assert stored(db, 'SELECT cooldown_until, retry_after_seconds FROM request_cooldowns') == cooldown


@pytest.mark.parametrize('primary_mode', [None, 'timeout'])
def test_coordinator_cleanup_failure_does_not_publish_or_mask_primary(tmp_path, monkeypatch, primary_mode):
    source, _ = source_layout(tmp_path)
    authority(source)
    wire(monkeypatch, mode=primary_mode)
    original = ProviderRequestCoordinator.close
    def fail_close(self):
        original(self)
        raise OSError(TOKEN)
    monkeypatch.setattr(ProviderRequestCoordinator, 'close', fail_close)
    result = acquire.update(source, TOKEN, clock=Time().bound())
    assert result['status'] == 'stopped' and result['cleanup_failed'] is True
    assert result['cleanup_stage'] == 'coordinator_close'
    assert result['primary_error_type'] == ('TimeoutError' if primary_mode else 'BankIdentityError')
    assert TOKEN not in str(result)
    assert not (source / 'data/instrument_identity/stock_basic' / result['snapshot_id'] / 'READY.json').exists()


@pytest.mark.parametrize('stage', ['result.json', 'READY.pending.json', 'rename'])
def test_publish_failure_preserves_evidence_but_cannot_be_consumed(tmp_path, monkeypatch, stage):
    source, run = source_layout(tmp_path)
    authority(source)
    wire(monkeypatch)
    original = acquire.durable_new
    def close_fails_after_complete_bytes(path, raw):
        original(path, raw)
        if path.name == stage:
            raise OSError('synthetic close failure')
    monkeypatch.setattr(acquire, 'durable_new', close_fails_after_complete_bytes)
    if stage == 'rename':
        def fail_rename(*args):
            raise OSError('synthetic publish failure')
        monkeypatch.setattr(acquire.os, 'rename', fail_rename)
    result = acquire.update(source, TOKEN, clock=Time().bound())
    assert result['status'] == 'stopped' and result['physical_attempts'] == 6
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists() and (root / 'snapshot.json').exists()
    ref = root.name + ':' + hashlib.sha256((root / 'snapshot.json').read_bytes()).hexdigest()
    with pytest.raises(bank.BankIdentityError):
        bank.prepare_overlay(source, run, '000123.SZ', '2026-10-16', ref, now=NOW + timedelta(seconds=100))


def test_slow_publication_cannot_commit_after_capture_deadline(tmp_path, monkeypatch):
    source, _ = source_layout(tmp_path)
    authority(source)
    timer = Time()
    wire(monkeypatch)
    original = acquire.durable_new
    def slow_write(path, raw):
        original(path, raw)
        if path.name == 'READY.pending.json':
            timer.value += 241
    monkeypatch.setattr(acquire, 'durable_new', slow_write)
    result = acquire.update(source, TOKEN, clock=timer.bound())
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert result['status'] == 'stopped' and result['reason'] == 'bank_capture_deadline'
    assert (root / 'READY.pending.json').exists() and not (root / 'READY.json').exists()


def test_exact_update_one_request_real_coordinator_and_session_consumer(tmp_path, monkeypatch):
    source, run = source_layout(tmp_path)
    db = authority(source)
    calls, closed, replies = wire(monkeypatch, exact_symbol='600919.SH')
    result = acquire.update(source, TOKEN, clock=Time().bound(), exact_symbol='600919.SS')
    assert result['status'] == 'exact_positive'
    assert result['maximum_requests'] == result['physical_attempts'] == result['http_responses'] == 1
    assert len(calls) == len(closed) == len(replies) == 1
    assert len(stored(db, 'SELECT * FROM provider_request_attempts')) == 1
    binding = bank.prepare_overlay(source, run, '600919.SH', '2026-10-16',
                                   result['snapshot_ref'], now=NOW)
    assert binding['contract'] == bank.EXACT_BINDING and binding['universe_coverage'] == 'partial'
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert TOKEN.encode() not in b''.join(p.read_bytes() for p in root.iterdir())
    assert 'provider_extension' not in (root / 'snapshot.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('mode', ['redirect', '401', 'timeout', 'disconnect', 'oversize', 'truncated',
                                  'provider', 'duplicate', 'echo', 'deadline', 'close',
                                  'unknown-echo', 'key-echo', 'zero', 'two', 'missing-page', 'wrong-symbol'])
def test_exact_failure_never_retries_or_publishes_and_preserves_safe_reason(tmp_path, monkeypatch, mode):
    source, _ = source_layout(tmp_path)
    db = authority(source)
    timer = Time()
    calls, closed, _ = wire(monkeypatch, mode=mode, timer=timer, exact_symbol='600919.SH')
    result = acquire.update(source, TOKEN, clock=timer.bound(), exact_symbol='600919.SH')
    assert result['status'] == 'stopped' and result['physical_attempts'] == 1
    assert result['maximum_requests'] == 1 and len(calls) == len(closed) == 1
    assert len(stored(db, 'SELECT * FROM provider_request_attempts')) == 1
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists()
    assert not (root / 'snapshot.json').exists()
    retained = b''.join(p.read_bytes() for p in root.iterdir())
    assert TOKEN.encode() not in retained and b'secret=' not in retained
    if mode in {'zero', 'two'}:
        assert result['reason'] == 'bank_exact_row_count_unverified'
    if mode == 'missing-page':
        assert result['reason'] == 'bank_exact_pagination_unverified'


def test_exact_invalid_or_conflicting_mode_has_no_state_or_send(tmp_path, monkeypatch):
    source, _ = source_layout(tmp_path)
    calls, _, _ = wire(monkeypatch, exact_symbol='600919.SH')
    for options in ({'exact_symbol': '600919'}, {'exact_symbol': '600919.SH', 'diagnose_schema': True}):
        with pytest.raises(bank.BankIdentityError):
            acquire.update(source, TOKEN, clock=Time().bound(), **options)
    assert calls == [] and not (source / 'data').exists()


def test_exact_publication_failure_keeps_unconsumable_evidence(tmp_path, monkeypatch):
    source, _ = source_layout(tmp_path)
    authority(source)
    calls, _, _ = wire(monkeypatch, exact_symbol='600919.SH')
    def fail_publish(*args):
        raise OSError('synthetic-publication-failure')
    monkeypatch.setattr(acquire, 'publish_ready', fail_publish)
    result = acquire.update(source, TOKEN, clock=Time().bound(), exact_symbol='600919.SH')
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert result['status'] == 'stopped' and len(calls) == 1
    assert (root / 'snapshot.json').exists() and (root / 'result.json').exists()
    assert (root / 'failure.json').exists() and not (root / 'READY.json').exists()
