"""Synthetic HTTP only; real capture, coordinator, validator and publication path."""
import copy
import hashlib
import json
import ssl
from datetime import timedelta

import pytest

from tests.test_bank_identity_acquisition import TOKEN, Time, authority
from tests.test_bank_identity_snapshot import NOW, payload, source_layout
from tradingagents.dataflows import (
    bank_identity_acquisition as acquire,
    bank_identity_snapshot as bank,
)


def fake_wire(monkeypatch, *, mutate=None, fail=None, close=False):
    calls, closed = [], []

    class Response:
        status = int(fail) if fail in {'401', '429', '503'} else 200

        def __init__(self, raw):
            self.raw = raw

        def getheader(self, name):
            return None

        def read1(self, maximum):
            if fail == 'read':
                raise ConnectionResetError(TOKEN)
            result, self.raw = self.raw[:maximum], self.raw[maximum:]
            return result

        def close(self):
            closed.append('response')
            if close:
                raise OSError(TOKEN)

    class Connection:
        sock = None

        def __init__(self, host, port, *, timeout, context):
            assert host == 'api.waditu.com' and port == 443 and 0 < timeout <= 30
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            if fail == 'tls_init':
                raise ssl.SSLError(TOKEN)

        def request(self, method, path, *, body, headers):
            request = json.loads(body)
            assert (method, path) == ('POST', '/dataapi/stock_basic')
            assert request['token'] == TOKEN
            assert request['api_name'] == 'stock_basic'
            assert request['fields'] == ','.join(bank.FIELDS)
            assert request['params']['ts_type_name'] == 'https://api.waditu.com/dataapi'
            assert set(request['params']) == {'exchange', 'list_status', 'ts_type_name'}
            assert set(headers) == {'Content-Type', 'Accept'}
            self.partition = request['params']['exchange'], request['params']['list_status']
            calls.append(self.partition)
            if fail == 'send':
                raise TimeoutError(TOKEN)

        def getresponse(self):
            value = payload(*self.partition)
            if self.partition[1] != 'L':
                value['data']['items'] = []
            if mutate:
                mutate(value)
            raw = b'{"code":0,"code":0}' if fail == 'json' else bank.json_bytes(value)
            return Response(raw)

        def close(self):
            closed.append('connection')
            if close:
                raise OSError(TOKEN)

    monkeypatch.setattr(acquire.http.client, 'HTTPSConnection', Connection)
    return calls, closed


def run_case(tmp_path, monkeypatch, *, mutate=None, fail=None, close=False, diagnose=True):
    source, _ = source_layout(tmp_path)
    authority(source)
    calls, closed = fake_wire(monkeypatch, mutate=mutate, fail=fail, close=close)
    result = acquire.update(source, TOKEN, clock=Time().bound(), diagnose_schema=diagnose)
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    details = json.loads((root / 'schema_diagnostics.json').read_bytes())
    assert result['schema_diagnostics_sha256'] == hashlib.sha256(
        (root / 'schema_diagnostics.json').read_bytes()).hexdigest()
    retained = json.loads((root / 'result.json').read_bytes())
    assert retained == ({**result, 'status': 'validated'} if result['status'] == 'complete' else result)
    assert '_response_diagnostics' not in result
    assert TOKEN.encode() not in b''.join(p.read_bytes() for p in root.iterdir())
    if diagnose:
        assert not (root / 'READY.json').exists() and not (root / 'READY.pending.json').exists()
        assert not (root / 'snapshot.json').exists()
        assert result['snapshot_usable'] is False and 'snapshot_ref' not in result
        assert result['contract'] == acquire.DIAGNOSTIC_CONTRACT
        assert result['maximum_requests'] == 1 and len(calls) <= 1
    return source, root, result, details['observations'], calls, closed


def test_single_valid_partition_never_becomes_consumable(tmp_path, monkeypatch):
    source, _, result, observations, calls, closed = run_case(tmp_path, monkeypatch)
    assert result['status'] == 'diagnostic_complete' and result['completed_partitions'] == 1
    assert result['physical_attempts'] == result['http_responses'] == 1
    assert calls == [('SSE', 'L')] and closed == ['response', 'connection']
    assert observations[0]['stage'] == 'partition_validated'
    shape = observations[0]['structure']
    assert (shape['field_count'], shape['row_count'], shape['minimum_row_width']) == (10, 1, 10)
    with pytest.raises(bank.BankIdentityError):
        bank._snapshot(source, result['snapshot_id'] + ':' + '0' * 64, NOW)


@pytest.mark.parametrize('key,value', [
    ('limit', 6000), ('count', 1), ('opaque_extension', {'private-value': 'not retained'}),
    ('has_more', True), ('truncated', 'false'), ('total', 2), ('offset', 1),
])
def test_rejected_metadata_retains_structure_without_relaxing_validation(tmp_path, monkeypatch, key, value):
    _, _, result, observations, calls, _ = run_case(
        tmp_path, monkeypatch, mutate=lambda p: p['data'].__setitem__(key, value))
    assert result['status'] == 'stopped' and result['completed_partitions'] == 0
    assert result['physical_attempts'] == result['http_responses'] == 1 and len(calls) == 1
    assert observations[0]['stage'] == 'response_schema'
    assert observations[0]['primary_error_type'] == 'BankIdentityError'
    rendered = json.dumps(observations)
    assert 'not retained' not in rendered and 'private-value' not in rendered
    if key in {'limit', 'count', 'opaque_extension'}:
        assert result['reason'] == 'bank_unknown_table_metadata'
    if key == 'opaque_extension':
        assert key not in rendered and 'anonymous_key_3' in rendered


@pytest.mark.parametrize('change', ['empty_fields', 'wrong_fields', 'row_width', 'items_object', 'provider'])
def test_invalid_table_and_business_response_still_stop(tmp_path, monkeypatch, change):
    def mutate(value):
        if change == 'empty_fields':
            value['data']['fields'] = []
        elif change == 'wrong_fields':
            value['data']['fields'][0] = 'personal-name@example.invalid'
        elif change == 'row_width':
            value['data']['items'][0].pop()
        elif change == 'items_object':
            value['data']['items'] = {'private-name': 'secret-content'}
        else:
            value['code'], value['msg'] = 2011, 'private-message'
    _, _, result, observations, _, _ = run_case(tmp_path, monkeypatch, mutate=mutate)
    assert result['status'] == 'stopped' and result['completed_partitions'] == 0
    assert observations[0]['stage'] == 'response_schema'
    text = json.dumps(observations)
    assert all(x not in text for x in ('personal-name', 'private-name', 'secret-content', 'private-message'))


@pytest.mark.parametrize('location', ['key', 'nested_value', 'field', 'row', 'message'])
def test_credential_echo_precedes_any_structure_capture(tmp_path, monkeypatch, location):
    def mutate(value):
        if location == 'key':
            value['data'][TOKEN] = None
        elif location == 'nested_value':
            value['data']['unknown'] = {'nested': [TOKEN]}
        elif location == 'field':
            value['data']['fields'][0] = TOKEN
        elif location == 'row':
            value['data']['items'][0][2] = TOKEN
        else:
            value['msg'] = TOKEN
    _, _, result, observations, _, _ = run_case(tmp_path, monkeypatch, mutate=mutate)
    assert result['reason'] == 'bank_credential_echo'
    assert observations[0]['structure'] is None
    assert observations[0]['stage'] == 'credential_echo_check'


def test_structure_is_bounded_and_does_not_fingerprint_values():
    value = payload('SSE', 'L')
    first = acquire.response_structure(value, 999)
    changed = copy.deepcopy(value)
    changed['data']['items'][0][2] = 'ANOTHER PRIVATE NAME'
    assert acquire.response_structure(changed, 999) == first
    value['data'].update({('arbitrary-' + str(i)): 'do-not-save' for i in range(100)})
    value['data']['fields'] += ['x' * 10000] * 100
    value['data']['items'] = [[None] * 10 for _ in range(6001)] + ['private row']
    summary = acquire.response_structure(value, 12345)
    assert summary['data']['keys_omitted'] == 70
    assert summary['fields_omitted'] == 78
    assert (summary['row_count'], summary['rows_inspected'], summary['rows_omitted']) == (6002, 6000, 2)
    raw = bank.json_bytes(summary)
    assert len(raw) < 16384
    assert b'arbitrary-' not in raw and b'do-not-save' not in raw and b'private row' not in raw
    assert b'x' * 100 not in raw


@pytest.mark.parametrize('fail,stage,attempts,responses', [
    ('tls_init', 'transport_initialization', 0, 0), ('send', 'request_send', 1, 0),
    ('401', 'response_headers', 1, 1), ('429', 'response_headers', 1, 1),
    ('503', 'response_headers', 1, 1), ('read', 'response_body', 1, 1),
    ('json', 'json_decode', 1, 1),
])
def test_transport_and_decode_failures_keep_safe_stage(tmp_path, monkeypatch, fail, stage, attempts, responses):
    _, _, result, observations, _, _ = run_case(tmp_path, monkeypatch, fail=fail)
    assert result['status'] == 'stopped'
    assert (result['physical_attempts'], result['http_responses']) == (attempts, responses)
    assert observations[0]['stage'] == stage and observations[0]['primary_error_type']
    assert observations[0]['structure'] is None


def test_primary_and_two_transport_close_failures_remain_distinct(tmp_path, monkeypatch):
    _, _, result, observations, _, _ = run_case(
        tmp_path, monkeypatch, mutate=lambda p: p['data'].__setitem__('limit', 6000), close=True)
    assert result['reason'] == 'bank_unknown_table_metadata' and result['cleanup_failed']
    assert observations[0]['primary_error_type'] == 'BankIdentityError'
    assert observations[0]['cleanup_errors'] == [
        {'resource': 'response', 'error_type': 'OSError'},
        {'resource': 'connection', 'error_type': 'OSError'}]


def test_close_failure_after_valid_partition_is_not_success(tmp_path, monkeypatch):
    _, _, result, observations, _, _ = run_case(tmp_path, monkeypatch, close=True)
    assert result['reason'] == 'bank_transport_close_failed'
    assert observations[0]['stage'] == 'partition_validated'
    assert observations[0]['primary_error_type'] is None and observations[0]['cleanup_errors']


@pytest.mark.parametrize('parser_failure', [False, True])
@pytest.mark.parametrize('diagnose', [False, True])
def test_diagnostic_write_failure_no_retry_no_ready_primary_retained(tmp_path, monkeypatch, parser_failure, diagnose):
    source, _ = source_layout(tmp_path)
    authority(source)
    mutation = (lambda p: p['data'].__setitem__('limit', 6000)) if parser_failure else None
    calls, _ = fake_wire(monkeypatch, mutate=mutation)
    original, writes = acquire.durable_new, []
    def fail_sidecar(path, raw):
        writes.append(path.name)
        if path.name == 'schema_diagnostics.json':
            raise OSError(TOKEN)
        return original(path, raw)
    monkeypatch.setattr(acquire, 'durable_new', fail_sidecar)
    result = acquire.update(source, TOKEN, clock=Time().bound(), diagnose_schema=diagnose)
    assert result['status'] == 'stopped' and len(calls) == (1 if parser_failure or diagnose else 6)
    expected = 'bank_unknown_table_metadata' if parser_failure else 'bank_diagnostics_write_failed'
    assert result['reason'] == expected
    assert result['schema_diagnostics_status'] == 'write_failed'
    assert result['diagnostic_write_error_type'] == 'OSError'
    assert writes == ['schema_diagnostics.json', 'result.json']
    assert TOKEN not in json.dumps(result)
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists()


def test_arbitrary_bank_exception_text_is_not_a_safe_reason(tmp_path, monkeypatch):
    def malicious_parse(*args):
        raise bank.BankIdentityError(TOKEN)
    monkeypatch.setattr(bank, 'parse_partition', malicious_parse)
    _, _, result, observations, _, _ = run_case(tmp_path, monkeypatch)
    assert result['reason'] == 'bank_capture_stopped'
    assert observations[0]['primary_error_type'] == 'BankIdentityError'


def test_normal_update_keeps_six_partitions_and_small_consumable_result(tmp_path, monkeypatch):
    source, root, result, observations, calls, _ = run_case(tmp_path, monkeypatch, diagnose=False)
    assert result['status'] == 'complete' and calls == list(bank.PARTITIONS)
    assert len(observations) == 6 and len((root / 'result.json').read_bytes()) < 16384
    value, digest = bank._snapshot(source, result['snapshot_ref'], NOW + timedelta(seconds=100))
    assert len(value['partitions']) == 6 and digest == result['snapshot_ref'].split(':')[1]


def test_single_request_limit_checked_before_second_send(monkeypatch):
    calls, _ = fake_wire(monkeypatch)
    receipt = {'physical_attempts': 0, 'http_responses': 0, 'http_statuses': [], 'cleanup_failed': False}
    acquire.request_partition(TOKEN, 'SSE', 'L', Time().bound(), receipt, maximum_attempts=1)
    with pytest.raises(bank.BankIdentityError, match='bank_attempt_limit'):
        acquire.request_partition(TOKEN, 'SSE', 'L', Time().bound(), receipt, maximum_attempts=1)
    assert calls == [('SSE', 'L')] and receipt['physical_attempts'] == 1
