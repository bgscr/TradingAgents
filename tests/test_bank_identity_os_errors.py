"""Synthetic exceptions and fake wire only; no real credentials or requests."""
import http.client
import json

import pytest

from scripts import update_bank_identity as entry
from tests.test_bank_identity_acquisition import TOKEN, Time, authority
from tests.test_bank_identity_diagnostic_entry import entry_context
from tests.test_bank_identity_snapshot import source_layout
from tradingagents.dataflows import bank_identity_acquisition as acquire

SECRET = 'synthetic-private-error-sentinel'
PRIVATE_URL = 'https://example.invalid/path?token=' + SECRET


def os_error():
    error = OSError(13, SECRET, PRIVATE_URL)
    error.winerror = 10013  # Synthetic value, never evidence about the real failure.
    return error


def assert_safe(value):
    rendered = json.dumps(value)
    assert all(text not in rendered for text in (TOKEN, SECRET, PRIVATE_URL, 'example.invalid'))
    assert all(key not in rendered for key in ('traceback', 'filename', 'headers', 'message'))


def test_numeric_os_codes_without_rendering_exception():
    error = os_error()
    assert acquire.safe_error_details(error) == {
        'chain': [{'error_type': 'OSError', 'errno': 13, 'winerror': 10013}],
        'truncated': False, 'extraction_failed': False}
    assert_safe(acquire.safe_error_details(error))


@pytest.mark.parametrize('code', [True, '10013', SECRET, 1.5, 2 ** 32, -(2 ** 31) - 1])
def test_non_integer_or_out_of_range_codes_are_omitted(code):
    error = os_error()
    error.errno = error.winerror = code
    details = acquire.safe_error_details(error)
    assert details['chain'] == [{'error_type': 'OSError'}]
    assert_safe(details)


@pytest.mark.parametrize('code', [-2, 0, 13, 2 ** 32 - 1])
def test_bounded_integer_codes_preserved(code):
    error = os_error()
    error.errno = error.winerror = code
    assert acquire.safe_error_details(error)['chain'][0] == {
        'error_type': 'OSError', 'errno': code, 'winerror': code}


@pytest.mark.parametrize('error', [OSError(SECRET), RuntimeError(PRIVATE_URL)])
def test_absent_codes_do_not_use_args_or_messages(error):
    details = acquire.safe_error_details(error)
    assert details['chain'] == [{'error_type': acquire.safe_type(error)}]
    assert details['extraction_failed'] is False
    assert_safe(details)


@pytest.mark.parametrize('relationship', ['cause', 'context'])
def test_common_http_wrapper_keeps_bounded_os_cause(relationship):
    wrapper = http.client.HTTPException(PRIVATE_URL)
    setattr(wrapper, '__' + relationship + '__', os_error())
    details = acquire.safe_error_details(wrapper)
    assert details['chain'] == [
        {'error_type': 'HTTPException'},
        {'error_type': 'OSError', 'errno': 13, 'winerror': 10013}]
    assert_safe(details)


def test_explicit_cause_wins_and_suppressed_context_is_not_exposed():
    wrapper = RuntimeError(SECRET)
    wrapper.__context__ = os_error()
    wrapper.__suppress_context__ = True
    assert acquire.safe_error_details(wrapper)['chain'] == [{'error_type': 'other'}]
    wrapper.__cause__ = TimeoutError(110, PRIVATE_URL)
    details = acquire.safe_error_details(wrapper)
    assert details['chain'] == [{'error_type': 'other'}, {'error_type': 'TimeoutError', 'errno': 110}]
    assert_safe(details)


@pytest.mark.parametrize('cycle', [False, True])
def test_exception_chain_is_bounded(cycle):
    errors = [RuntimeError(SECRET) for _ in range(6)]
    for first, second in zip(errors, errors[1:], strict=False):
        first.__cause__ = second
    if cycle:
        errors[1].__cause__ = errors[0]
    details = acquire.safe_error_details(errors[0])
    assert len(details['chain']) == (2 if cycle else 4)
    assert details['truncated'] is True
    assert_safe(details)


class UnreadableCode(OSError):
    @property
    def errno(self):
        raise RuntimeError(SECRET)


def test_extraction_failure_discards_partial_diagnostic():
    assert acquire.safe_error_details(UnreadableCode(SECRET)) == {
        'chain': [], 'truncated': False, 'extraction_failed': True}


def failing_wire(monkeypatch, error, *, stage='request_send', close_failure=False):
    calls, closed = [], []

    class Response:
        status = 200

        def getheader(self, name):
            return None

        def read1(self, size):
            raise error

        def close(self):
            closed.append('response')
            if close_failure:
                raise OSError(28, SECRET)

    class Connection:
        sock = None

        def __init__(self, host, port, *, timeout, context):
            assert (host, port) == ('api.waditu.com', 443)
            assert 0 < timeout <= 30 and context.check_hostname
            if stage == 'transport_initialization':
                raise error

        def request(self, method, path, *, body, headers):
            assert (method, path) == ('POST', '/dataapi/stock_basic')
            value = json.loads(body)
            assert value['token'] == TOKEN and value['params']['ts_code'] == '600919.SH'
            calls.append('send')
            if stage == 'request_send':
                raise error

        def getresponse(self):
            return Response()

        def close(self):
            closed.append('connection')
            if close_failure:
                raise OSError(28, SECRET)

    monkeypatch.setattr(acquire.http.client, 'HTTPSConnection', Connection)
    return calls, closed


@pytest.mark.parametrize('stage', ['transport_initialization', 'request_send', 'response_body'])
@pytest.mark.parametrize('kind', ['os', 'wrapped', 'unreadable', 'no_code'])
def test_exact_capture_stops_once_and_retains_safe_primary(tmp_path, monkeypatch, stage, kind):
    source, _ = source_layout(tmp_path)
    authority(source)
    error = os_error()
    if kind == 'wrapped':
        wrapper = http.client.HTTPException(PRIVATE_URL)
        wrapper.__cause__ = error
        error = wrapper
    elif kind == 'unreadable':
        error = UnreadableCode(SECRET)
    elif kind == 'no_code':
        error = OSError(PRIVATE_URL)
    calls, closed = failing_wire(monkeypatch, error, stage=stage, close_failure=True)
    result = acquire.update(source, TOKEN, clock=Time().bound(), exact_symbol='600919.SH')
    assert result['status'] == 'stopped' and result['reason'] == 'bank_capture_stopped'
    assert result['maximum_requests'] == 1 and result['sdk_retries'] == 0 and result['resume'] is False
    assert result['physical_attempts'] == len(calls) == (0 if stage == 'transport_initialization' else 1)
    assert result['http_responses'] == (1 if stage == 'response_body' else 0)
    assert result['completed_partitions'] == 0
    expected_closed = [] if stage == 'transport_initialization' else (
        ['response', 'connection'] if stage == 'response_body' else ['connection'])
    assert closed == expected_closed
    assert result['cleanup_failed'] is bool(expected_closed)
    assert result['primary_error_type'] == acquire.safe_type(error)
    assert result['primary_error_details'] == acquire.safe_error_details(error)
    root = source / 'data/instrument_identity/stock_basic' / result['snapshot_id']
    assert not (root / 'READY.json').exists() and not (root / 'snapshot.json').exists()
    retained = json.loads((root / 'result.json').read_bytes())
    assert retained == result
    observation = json.loads((root / 'schema_diagnostics.json').read_bytes())['observations'][0]
    assert observation['stage'] == stage
    assert observation['primary_error_details'] == result['primary_error_details']
    assert_safe(result)
    assert_safe(observation)
    assert_safe([json.loads(path.read_bytes()) for path in root.iterdir()])


def test_exact_cli_prints_safe_error_codes_and_returns_failure(tmp_path, monkeypatch, capsys):
    source, _, _ = entry_context(tmp_path, monkeypatch,
        ['--refresh-security', '600919.SH', '--use-project-env'],
        confirmation='UPDATE_BANK_IDENTITY:600919.SH')
    calls, closed = failing_wire(monkeypatch, os_error())
    assert entry.main() == 1
    output = capsys.readouterr().out
    assert_safe(output)
    result = json.loads(output.splitlines()[-1])
    assert result['physical_attempts'] == len(calls) == len(closed) == 1
    assert result['primary_error_details']['chain'][0]['winerror'] == 10013
    assert not (source / 'data/instrument_identity/stock_basic' / result['snapshot_id'] / 'READY.json').exists()
