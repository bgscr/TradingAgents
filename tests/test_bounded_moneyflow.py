"""Independent synthetic moneyflow cells; no provider samples or real requests."""
import copy
import http.client
import io
import json
from datetime import datetime, timedelta, timezone
from itertools import permutations
from types import SimpleNamespace

import pytest

from tradingagents import analysis_budget as budgets, moneyflow as mf
from tradingagents.evidence import EvidenceState, IdentityProvenance, InstrumentIdentityEvidence

ASOF = "2026-11-13"


def state(symbol="600895.SH"):
    identity = InstrumentIdentityEvidence(
        symbol=symbol, venue="XSHG" if symbol.endswith((".SH", ".SS")) else "XSHE",
        instrument_kind="equity", currency="CNY",
        provenance=IdentityProvenance(provider="synthetic", source_ref="synthetic:moneyflow-identity",
                                      retrieved_at="2026-11-14T00:00:00Z", artifact_sha256="a" * 64))
    return {"company_of_interest": symbol, "trade_date": ASOF,
            "evidence_state": EvidenceState(instrument_identity=identity).model_dump(mode="json")}


def response(items=None, **extra):
    return json.dumps({"code": 0, "msg": None, "data": {
        "fields": list(mf.FIELDS), "items": items if items is not None else [
            ["600895.SH", "20261113", 1.25], ["600895.SH", "20261020", -2],
            ["600895.SH", "20261102", 0]]}, **extra}).encode()


@pytest.fixture
def budget(monkeypatch):
    result = budgets.AnalysisBudget(60, 1200, allowed_data_vendors={"tushare", "akshare"})
    monkeypatch.setattr(budgets, "_ACTIVE", result)
    return result


class Response:
    def __init__(self, body, status=200, cleanup=False):
        self.body, self.status, self.cleanup = body, status, cleanup
        self.closed, self.reads = 0, 0
        self.length = None

    def isclosed(self):
        return self.closed > 0

    def read1(self, size):
        self.reads += 1
        block, self.body = self.body[:size], self.body[size:]
        return block

    def close(self):
        self.closed += 1
        if self.cleanup:
            raise OSError("synthetic-secret-cleanup")

    def getheader(self, name, default=None):
        return "900" if name == "Retry-After" else default


class Connection:
    def __init__(self, result, error=None):
        self.result, self.error, self.sock = result, error, None
        self.closed, self.calls = 0, []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error

    def getresponse(self):
        return self.result

    def close(self):
        self.closed += 1


def send(monkeypatch, connection, binding=None, **kwargs):
    monkeypatch.setattr("tradingagents.market_history.coordinator.record_active_physical_attempt_io", lambda: None)
    binding = binding or mf._binding(state(), "600895.SH", ASOF)
    return mf._send(binding, "synthetic-token-not-a-credential",
                    connection_factory=lambda host, **kw: connection, **kwargs)


def test_single_fixed_post_counted_closed_and_no_secret_retained(monkeypatch, budget):
    result = Response(response())
    conn = Connection(result)
    raw = send(monkeypatch, conn)
    assert len(conn.calls) == budget.receipt()["data_attempts"] == 1
    args, kwargs = conn.calls[0]
    assert args == ("POST", "/dataapi/moneyflow")
    sent = json.loads(kwargs["body"])
    assert sent["api_name"] == "moneyflow" and sent["fields"] == "ts_code,trade_date,net_mf_amount"
    assert sent["params"] == {"ts_code": "600895.SH", "start_date": "20261015", "end_date": "20261113",
                              "ts_type_name": "https://api.waditu.com/dataapi"}
    assert set(kwargs["headers"]) == {"Content-Type", "Connection"}
    assert conn.closed == result.closed == 1
    assert mf.decode(raw, mf._binding(state(), "600895.SH", ASOF)) == [
        ["2026-10-20", "-2"], ["2026-11-02", "0"], ["2026-11-13", "1.25"]]


@pytest.mark.parametrize('fields', list(permutations(mf.FIELDS)))
def test_returned_field_order_controls_value_mapping(fields):
    cells = {'ts_code': '600895.SH', 'trade_date': '20261113', 'net_mf_amount': 1.25}
    body = json.dumps({'code': 0, 'data': {'fields': list(fields),
        'items': [[cells[name] for name in fields]]}}).encode()
    assert mf.decode(body, mf._binding(state(), '600895.SH', ASOF)) == [['2026-11-13', '1.25']]


def envelope(fields=mf.FIELDS, items=None, **extra):
    return json.dumps({'code': 0, 'data': {'fields': fields,
        'items': [['600895.SH', '20261113', 1]] if items is None else items, **extra}}).encode()


@pytest.mark.parametrize('body,rejection', [
    ('synthetic-secret', 'payload_type_or_size'),
    (b'synthetic-secret', 'json_syntax'),
    (b'{"code":0,"code":0}', 'json_duplicate_key'),
    (b'{"code":NaN}', 'json_nonfinite'),
    (b'[]', 'envelope_type'),
    (b'{"code":0,"synthetic-secret-key":"secret"}', 'data_shape'),
    (b'{"code":"synthetic-secret"}', 'business_code_type'),
    (b'{"code":0,"data":null}', 'data_shape'),
    (envelope(fields='synthetic-secret'), 'fields_type'),
    (envelope(fields=['ts_code', 'trade_date', None]), 'fields_name_type'),
    (envelope(fields=['ts_code', 'ts_code', 'trade_date']), 'fields_duplicate'),
    (envelope(fields=['ts_code', 'trade_date', 'synthetic-secret']), 'fields_set'),
    (envelope(fields=['ts_code', 'trade_date']), 'fields_set'),
    (envelope(fields=[*mf.FIELDS, 'synthetic-secret']), 'row_shape'),
    (envelope(items='synthetic-secret'), 'items_type'),
    (envelope(items=[['600895.SH', '20261113', 1]] * 31), 'row_limit'),
    (envelope(items=[['synthetic-secret']]), 'row_shape'),
    (envelope(items=[[123, '20261113', 1]]), 'stock_type'),
    (envelope(items=[['synthetic-secret', '20261113', 1]]), 'stock_mismatch'),
    (envelope(items=[['600895.SH', 'synthetic-secret', 1]]), 'date_format'),
    (envelope(items=[['600895.SH', '20260230', 1]]), 'date_invalid'),
    (envelope(items=[['600895.SH', '20261114', 1]]), 'date_window'),
    (envelope(items=[['600895.SH', '20261014', 1]]), 'date_window'),
    (envelope(items=[['600895.SH', '20261113', 1]] * 2), 'date_duplicate'),
    (envelope(items=[['600895.SH', '20261113', None]]), 'amount_missing'),
    (envelope(items=[['600895.SH', '20261113', 'synthetic-secret']]), 'amount_type'),
    (envelope(items=[['600895.SH', '20261113', True]]), 'amount_type'),
    (envelope(items=[['600895.SH', '20261113', 1e15]]), 'amount_range'),
    (envelope(items=[['600895.SH', '20261113', 0.0000001]]), 'amount_precision'),
    (envelope(fields=list(reversed(mf.FIELDS))), 'stock_type'),
])
def test_decode_rejection_is_closed_and_does_not_include_actual_values(body, rejection):
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        mf.decode(body, mf._binding(state(), '600895.SH', ASOF))
    assert caught.value.rejection == rejection
    diag = mf._diagnostic('decode', mf._reason(caught.value), caught.value, 200,
                          rejection=caught.value.rejection)
    assert mf._valid_diagnostic(diag)
    encoded = json.dumps(diag)
    assert 'synthetic-secret' not in encoded and 'synthetic_secret' not in encoded


@pytest.mark.parametrize('code,reason', [(2002, 'permission_denied'), (1, 'provider_error')])
def test_business_rejection_keeps_only_fixed_class(code, reason):
    body = json.dumps({'code': code, 'msg': 'synthetic-secret', 'data': None}).encode()
    with pytest.raises(mf.MoneyflowUnavailable, match=reason) as caught:
        mf.decode(body, mf._binding(state(), '600895.SH', ASOF))
    assert caught.value.rejection == 'business_status'


@pytest.mark.parametrize('kind', [budgets.AnalysisBudgetStopped, KeyboardInterrupt, SystemExit])
def test_decode_does_not_swallow_cancellation(monkeypatch, kind):
    binding = mf._binding(state(), '600895.SH', ASOF)
    error = kind('synthetic-secret')

    def cancelled(*args, **kwargs):
        raise error

    monkeypatch.setattr(mf.json, 'loads', cancelled)
    with pytest.raises(kind) as caught:
        mf.decode(b'{}', binding)
    assert caught.value is error


def test_nonfinite_amount_and_internal_failure_are_closed(monkeypatch):
    from decimal import Decimal
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        mf._amount(Decimal('NaN'))
    assert caught.value.rejection == 'amount_nonfinite'
    binding = mf._binding(state(), '600895.SH', ASOF)

    def broken(*args, **kwargs):
        raise RuntimeError('synthetic-secret-decoder')

    monkeypatch.setattr(mf.json, 'loads', broken)
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        mf.decode(b'{}', binding)
    assert caught.value.rejection == 'decoder_internal'


@pytest.mark.parametrize("cell", [None, True, "1.2", {}, float("nan"), float("inf"), 1e20, 0.0000001])
def test_invalid_or_missing_amount_is_never_zero(cell):
    with pytest.raises(mf.MoneyflowUnavailable):
        mf.decode(response([["600895.SH", "20261113", cell]]), mf._binding(state(), "600895.SH", ASOF))


@pytest.mark.parametrize("items", [
    [["600919.SH", "20261113", 1]], [["600895.SH", "20261114", 1]],
    [["600895.SH", "20261014", 1]], [["600895.SH", "20260230", 1]],
    [["600895.SH", "20261113", 1], ["600895.SH", "20261113", 1]],
    [["600895.SH\r\nCookie: secret", "20261113", 1]],
])
def test_stock_dates_duplicates_fail_closed(items):
    with pytest.raises(mf.MoneyflowUnavailable, match="invalid_response"):
        mf.decode(response(items), mf._binding(state(), "600895.SH", ASOF))


@pytest.mark.parametrize("body", [b'{"code":0,"code":0}', b'not json synthetic-secret',
    json.dumps({"code": False, "data": {}}).encode(),
    json.dumps({"code": 0, "data": {"fields": ["ts_code", "trade_date", "token"], "items": []}}).encode()])
def test_unknown_shape_never_leaks(body):
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        mf.decode(body, mf._binding(state(), "600895.SH", ASOF))
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("code,reason", [(2002, "permission_denied"), (1, "provider_error"), (-1, "provider_error")])
def test_only_documented_permission_classification(code, reason):
    body = json.dumps({"code": code, "msg": "secret=https://secret/?token=synthetic", "data": None}).encode()
    with pytest.raises(mf.MoneyflowUnavailable, match=reason):
        mf.decode(body, mf._binding(state(), "600895.SH", ASOF))


@pytest.mark.parametrize("status,reason", [(302, "http_error"), (401, "authentication"),
                                          (403, "permission_denied"), (429, "rate_limited")])
def test_no_redirect_or_retry_cleanup_preserves_first_error(monkeypatch, budget, status, reason):
    result = Response(b"secret", status=status, cleanup=True)
    conn = Connection(result)
    with pytest.raises(mf.MoneyflowUnavailable, match=reason):
        send(monkeypatch, conn)
    assert result.reads == 0 and result.closed == conn.closed == len(conn.calls) == 1
    assert budget.receipt()["data_attempts"] == 1


def test_oversized_response_closes_and_stops(monkeypatch, budget):
    result = Response(b"x" * (mf.MAX_BYTES + 1))
    conn = Connection(result)
    with pytest.raises(mf.MoneyflowUnavailable, match="invalid_response"):
        send(monkeypatch, conn)
    assert conn.closed == result.closed == len(conn.calls) == 1


def test_network_failure_is_counted_and_redacted(monkeypatch, budget):
    conn = Connection(None, error=OSError("token=synthetic-secret"))
    with pytest.raises(mf.MoneyflowUnavailable, match="transport_error") as caught:
        send(monkeypatch, conn)
    assert caught.value.__context__ is None
    assert conn.closed == len(conn.calls) == budget.receipt()["data_attempts"] == 1


def test_budget_stop_propagates_and_closes_without_read(monkeypatch, budget):
    result = Response(response())
    conn = Connection(result)
    def getresponse():
        budget.stop("synthetic_stop")
    conn.getresponse = getresponse
    with pytest.raises(budgets.AnalysisBudgetStopped):
        send(monkeypatch, conn)
    assert conn.closed == 1 and result.reads == 0


def test_local_deadline_stops_trickle_and_closes(monkeypatch, budget):
    result = Response(b"x" * 100)
    conn = Connection(result)
    ticks = iter([0, 0, 0, 0, 31])
    with pytest.raises(mf.MoneyflowUnavailable, match="timeout"):
        send(monkeypatch, conn, clock=lambda: next(ticks))
    assert conn.closed == result.closed == 1
    assert result.reads <= 1


def test_response_owned_socket_timeout_still_shrinks(monkeypatch, budget):
    result = Response(response())
    conn = Connection(result)
    timeouts = []
    conn.sock = SimpleNamespace(settimeout=timeouts.append)
    def getresponse():
        conn.sock = None
        return result
    conn.getresponse = getresponse
    ticks = iter([0, 0, 0, 1, 10, 11, 12, 13])
    send(monkeypatch, conn, clock=lambda: next(ticks))
    assert timeouts[0] == 29
    assert timeouts[1] == 20
    assert conn.closed == result.closed == 1


class MemorySocket:
    """Real HTTPResponse owns this in-memory file; no socket or network exists."""
    def __init__(self, wire):
        self.released, self.timeouts = False, []
        owner = self

        class File(io.BytesIO):
            def close(self):
                owner.released = True
                super().close()

        self.file = File(wire)

    def makefile(self, mode):
        assert mode == 'rb'
        return self.file

    def settimeout(self, value):
        if self.released:
            raise OSError(9, 'synthetic-secret-url-token')
        self.timeouts.append(value)


def memory_connection(body, headers):
    sock = MemorySocket(b'HTTP/1.1 200 OK\r\nConnection: close\r\n' + headers + b'\r\n' + body)
    result = http.client.HTTPResponse(sock)
    result.begin()
    conn = Connection(result)
    conn.sock = sock

    def getresponse():
        conn.sock = None  # HTTPConnection hands the file/socket to HTTPResponse.
        return result

    conn.getresponse = getresponse
    return conn, result, sock


def test_real_httpresponse_complete_body_does_not_retouch_released_socket(monkeypatch, budget):
    body = response()
    conn, result, sock = memory_connection(body, f'Content-Length: {len(body)}\r\n'.encode())
    assert send(monkeypatch, conn) == body
    assert result.isclosed() and sock.released and len(conn.calls) == 1
    assert budget.receipt()['data_attempts'] == 1


@pytest.mark.parametrize('framing', ['chunked', 'eof', 'empty', 'multi_block'])
def test_real_httpresponse_framing_remains_bounded(monkeypatch, budget, framing):
    body = response() if framing != 'empty' else b''
    headers = b''
    wire_body = body
    if framing == 'chunked':
        headers = b'Transfer-Encoding: chunked\r\n'
        wire_body = f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n'
    elif framing in {'empty', 'multi_block'}:
        if framing == 'multi_block':
            body = wire_body = b' ' * 9000 + body
        headers = f'Content-Length: {len(body)}\r\n'.encode()
    conn, result, sock = memory_connection(wire_body, headers)
    assert send(monkeypatch, conn) == body
    assert result.isclosed() and sock.released and len(conn.calls) == 1


def test_real_httpresponse_valid_json_with_truncated_length_is_rejected(monkeypatch, budget):
    body = response()
    conn, result, _ = memory_connection(body, f'Content-Length: {len(body)+5}\r\n'.encode())
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        send(monkeypatch, conn)
    detail = caught.value.diagnostic
    assert detail['stage'] == 'body_read' and detail['http_status'] == 200
    assert detail['error']['chain'][0]['error_type'] == 'HTTPException'
    assert result.isclosed() and len(conn.calls) == budget.receipt()['data_attempts'] == 1


def test_diagnostic_preserves_os_code_first_error_and_cleanup(monkeypatch, budget):
    result = Response(response(), cleanup=True)
    conn = Connection(result)
    first = OSError(9, 'token=synthetic-secret https://private.invalid/?token=secret')

    def read1(size):
        raise first

    result.read1 = read1
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        send(monkeypatch, conn)
    diag = caught.value.diagnostic
    assert diag['stage'] == 'body_read' and diag['category'] == 'transport_error'
    assert diag['http_status'] == 200 and diag['error']['chain'][0]['errno'] == 9
    assert diag['cleanup'][0]['resource'] == 'response'
    assert len(conn.calls) == result.closed == conn.closed == 1
    assert 'synthetic-secret' not in json.dumps(diag) and 'private.invalid' not in json.dumps(diag)


def test_diagnostic_extraction_failure_does_not_replace_original(monkeypatch, budget):
    from tradingagents.dataflows import bank_identity_acquisition as bank

    def broken(error):
        raise ValueError('synthetic-secret-extractor')

    monkeypatch.setattr(bank, 'safe_error_details', broken)
    conn = Connection(None, error=OSError(9, 'synthetic-secret-root'))
    with pytest.raises(mf.MoneyflowUnavailable, match='transport_error') as caught:
        send(monkeypatch, conn)
    assert caught.value.diagnostic['error'] == {'chain': [], 'truncated': False, 'extraction_failed': True}
    assert caught.value.diagnostic['stage'] == 'connect_send'
    assert conn.closed == len(conn.calls) == 1


def test_bounded_nested_error_details_never_render_arbitrary_type_or_message():
    class SyntheticSecretNameError(RuntimeError):
        def __str__(self):
            raise AssertionError('must not stringify')

    top = SyntheticSecretNameError('synthetic-secret-body')
    top.__cause__ = OSError(10013, 'synthetic-secret-os')
    detail = mf._diagnostic('body_read', 'transport_error', top, 200)
    assert mf._valid_diagnostic(detail)
    assert detail['error']['chain'][0]['error_type'] == 'other'
    assert detail['error']['chain'][1]['errno'] == 10013
    assert 'SyntheticSecretName' not in json.dumps(detail) and 'synthetic-secret' not in json.dumps(detail)


@pytest.mark.parametrize('failure', [KeyboardInterrupt, budgets.AnalysisBudgetStopped])
def test_cancel_after_response_and_cleanup_failure_is_not_an_optional_gap(monkeypatch, budget, failure):
    result = Response(response(), cleanup=True)
    conn = Connection(result)
    error = failure('synthetic-secret-cancel')

    def read1(size):
        raise error

    result.read1 = read1
    with pytest.raises(failure) as caught:
        send(monkeypatch, conn)
    assert caught.value is error
    diag = caught.value.moneyflow_diagnostic
    assert diag['category'] == 'cancelled' and diag['stage'] == 'body_read'
    assert diag['cleanup'][0]['resource'] == 'response'
    assert result.closed == conn.closed == len(conn.calls) == 1


def test_legacy_rendering_preserves_exact_old_contract(monkeypatch, budget):
    _, observation = observed(monkeypatch, budget)
    current = state()
    legacy = copy.deepcopy(observation)
    legacy['contract'] = mf.LEGACY_VERSION
    legacy.pop('projected_data_sha256')
    legacy['raw_response_sha256'] = 'b' * 64  # Independent synthetic legacy receipt.
    legacy.pop('diagnostic')
    legacy['normalized_sha256'] = mf._digest({k:v for k,v in legacy.items() if k != 'normalized_sha256'})
    current['moneyflow_observation'] = legacy
    assert '合格日记录' in mf.moneyflow_markdown(current)
    legacy['diagnostic'] = None
    legacy['normalized_sha256'] = mf._digest({k:v for k,v in legacy.items() if k != 'normalized_sha256'})
    assert '绑定无效' in mf.moneyflow_markdown(current)


def test_v2_and_v3_bind_exact_diagnostic_versions(monkeypatch, budget):
    _, value = observed(monkeypatch, budget)
    value.update(contract=mf.DIAGNOSTIC_V1_VERSION, status='invalid_response', rows=[])
    value.pop('projected_data_sha256')
    value.update(raw_response_sha256=None, received_at=None)
    diagnostic = mf._diagnostic('decode', 'invalid_response', mf.MoneyflowUnavailable('invalid_response'), 200,
                                 rejection='fields_set')
    old = copy.deepcopy(diagnostic)
    old['contract'] = 'moneyflow-diagnostic-v1'
    old.pop('rejection')
    current = state()

    def render():
        value['normalized_sha256'] = mf._digest({k:v for k,v in value.items() if k != 'normalized_sha256'})
        current['moneyflow_observation'] = value
        return mf.moneyflow_markdown(current)

    value['diagnostic'] = old
    assert '绑定无效' not in render()
    value['diagnostic'] = diagnostic
    assert '绑定无效' in render()
    value['contract'] = mf.DIAGNOSTIC_V2_VERSION
    diagnostic['contract'] = 'moneyflow-diagnostic-v2'
    assert 'fields_set' in render()
    value['diagnostic'] = old
    assert '绑定无效' in render()
    value['diagnostic'] = diagnostic
    value['contract'] = mf.VERSION
    value.pop('raw_response_sha256')
    value['projected_data_sha256'] = None
    assert '绑定无效' in render()
    diagnostic['contract'] = 'moneyflow-diagnostic-v3'
    assert 'fields_set' in render()
    diagnostic['rejection'] = 'synthetic-secret'
    assert '绑定无效' in render()


def test_rejection_cannot_leak_free_text_or_attach_to_transport():
    value = mf._diagnostic('decode', 'invalid_response', ValueError('secret'), 200, rejection='fields_set')
    value['stage'] = 'body_read'
    assert not mf._valid_diagnostic(value)
    value['stage'] = 'decode'
    value['rejection'] = ['synthetic-secret']
    assert not mf._valid_diagnostic(value)
    value['contract'] = ['synthetic-secret']
    assert not mf._valid_diagnostic(value)


@pytest.mark.parametrize('change', [
    lambda x: x.update(message='synthetic-secret'),
    lambda x: x.update(stage='synthetic-secret'),
    lambda x: x.update(category='synthetic-secret'),
    lambda x: x.update(http_status=True),
    lambda x: x['error']['chain'][0].update(error_type='synthetic-secret-class'),
    lambda x: x['error']['chain'][0].update(errno=True),
    lambda x: x['error']['chain'][0].update(winerror=2**60),
])
def test_diagnostic_schema_has_no_free_text_escape(change):
    value = mf._diagnostic('body_read', 'transport_error', OSError(9, 'secret'), 200)
    change(value)
    assert not mf._valid_diagnostic(value)


@pytest.mark.parametrize('mode', ['transport', 'decode'])
def test_real_coordinator_carries_first_diagnostic_to_observation_and_report(tmp_path, monkeypatch, budget, mode):
    from tests.test_current_tushare_statements import runtime_config
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import record_active_physical_attempt_io
    from tradingagents.market_history.store import MarketHistoryStore

    cfg = runtime_config(tmp_path)
    cfg.update(bounded_moneyflow_enabled=True, tushare_calls_per_minute=40,
        tushare_account_scope_label='personal-2000', tushare_operator_safety_ceiling_calls_per_minute=40,
        tushare_financial_https_endpoint='https://api.waditu.com/dataapi')
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(cfg)):
        pass
    diag = mf._diagnostic('body_timeout', 'transport_error', OSError(9, 'synthetic-secret'), 200)

    def failed_send(binding, token):
        with budgets.admitted_transport('data'):
            record_active_physical_attempt_io()
            if mode == 'decode':
                return envelope(fields=['ts_code', 'trade_date', 'synthetic-secret'])
            raise mf.MoneyflowUnavailable('transport_error', diagnostic=diag)

    monkeypatch.setenv('TUSHARE_TOKEN', 'synthetic-token')
    monkeypatch.setattr(mf, '_send', failed_send)
    current = state()
    with mf.moneyflow_scope(cfg, ticker='600895.SH', as_of=ASOF):
        observation = mf.moneyflow_for_state(current)
        assert mf.moneyflow_for_state(current) == observation
    if mode == 'transport':
        assert observation['diagnostic'] == diag and observation['status'] == 'transport_error'
    else:
        assert observation['diagnostic']['rejection'] == 'fields_set'
        assert observation['diagnostic']['stage'] == 'decode' and observation['status'] == 'invalid_response'
    current['moneyflow_observation'] = observation
    report = mf.moneyflow_markdown(current)
    assert ('body_timeout' in report and 'errno' in report) if mode == 'transport' else 'fields_set' in report
    assert 'synthetic-secret' not in report
    assert observation['attempts'] == budget.receipt()['data_attempts'] == 1
    assert budget.receipt()['model_attempts'] == 0
    invalid = copy.deepcopy(observation)
    invalid['status'], invalid['rows'] = 'transport_error', []
    invalid['diagnostic'] = mf._diagnostic('body_read', 'transport_error', OSError(9, 'secret'), 200)
    invalid['diagnostic']['error']['chain'][0]['errno'] = 'synthetic-secret'
    invalid['normalized_sha256'] = mf._digest({k:v for k,v in invalid.items() if k != 'normalized_sha256'})
    current['moneyflow_observation'] = invalid
    assert '绑定无效' in mf.moneyflow_markdown(current)


def test_rate_limit_preserves_long_retry_after(monkeypatch, budget):
    conn = Connection(Response(b"not read", status=429))
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        send(monkeypatch, conn)
    assert caught.value.retry_after == 900
    assert len(conn.calls) == 1


def observed(monkeypatch, budget, value=None):
    def collect(config, binding):
        with budgets.admitted_transport("data"):
            rows = mf.decode(response() if value is None else value, binding)
            return rows, mf._projection_digest("moneyflow", binding, rows), datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mf, "_coordinated", collect)
    run = mf._Run({}, "600895.SH", ASOF)
    return run, run.acquire(state())


def test_once_per_run_including_empty_and_failure(monkeypatch, budget):
    run, result = observed(monkeypatch, budget)
    assert run.acquire(state()) == result
    assert budget.receipt()["data_attempts"] == 1
    value = state()
    value["moneyflow_observation"] = result
    with pytest.raises(budgets.AnalysisBudgetStopped, match="restore"):
        mf._Run({}, "600895.SH", ASOF).acquire(value)


def test_report_revalidates_values_scope_and_identity(monkeypatch, budget):
    _, observation = observed(monkeypatch, budget)
    value = {**state(), "moneyflow_observation": observation}
    text = mf.moneyflow_markdown(value)
    assert "-0.75 万元" in text and "3 条" in text and "不改变评级" in text
    assert "retrospective_backfill" in text and "未证明全部交易日覆盖" in text
    assert "synthetic-token" not in json.dumps(observation)
    assert "CNY 10,000" in mf.moneyflow_markdown(value, language="English")
    for changed in ["symbol", "as_of", "unit", "identity_sha256", "status"]:
        bad = copy.deepcopy(value)
        bad["moneyflow_observation"][changed] = "synthetic-secret"
        output = mf.moneyflow_markdown(bad)
        assert "绑定无效" in output and "synthetic-secret" not in output


def test_empty_and_permission_are_unavailable_not_zero(monkeypatch, budget):
    run, obs = observed(monkeypatch, budget, response([]))
    assert obs["status"] == "empty"
    assert "不代表净流入为零" in mf.moneyflow_markdown({**state(), "moneyflow_observation": obs})
    assert mf.moneyflow_for_state(state()) is None


@pytest.mark.parametrize("symbol", ["600895.SH", "600919.SS", "000001.SZ"])
def test_supported_identity_and_dates_are_not_hardcoded(symbol):
    binding = mf._binding(state(symbol), symbol, ASOF)
    body = response([[binding["symbol"], "20261113", 5.12]])
    assert mf.decode(body, binding) == [[ASOF, "5.12"]]


def test_no_budget_invalid_binding_or_disabled_policy_reads_no_token(monkeypatch):
    reads = []
    original_get = mf.os.environ.get
    def guarded_get(key, default=None):
        if key == "TUSHARE_TOKEN":
            reads.append(key)
            raise AssertionError("credential read before valid authority")
        return original_get(key, default)
    monkeypatch.setattr(mf.os.environ, "get", guarded_get)
    monkeypatch.setattr(budgets, "_ACTIVE", None)
    with pytest.raises(mf.MoneyflowUnavailable, match="not_configured"):
        mf._coordinated({}, mf._binding(state(), "600895.SH", ASOF))
    monkeypatch.setattr(budgets, "_ACTIVE", budgets.AnalysisBudget(
        60, 1200, allowed_data_vendors={"tushare", "akshare"}))
    with pytest.raises(mf.MoneyflowUnavailable, match="not_configured"):
        mf._coordinated({"bounded_moneyflow_enabled": False}, mf._binding(state(), "600895.SH", ASOF))
    bad = state()
    bad["company_of_interest"] = "600895.SH\r\nCookie: synthetic"
    with pytest.raises(mf.MoneyflowUnavailable):
        mf._Run({}, "600895.SH", ASOF).acquire(bad)
    assert reads == []


def test_launcher_opt_in_and_checkpoint_binding_preserve_default(tmp_path):
    from tests.test_bounded_mainland_entry import entry
    module = entry()
    assert module.parse_arguments([]).moneyflow is False
    assert module.parse_arguments(["--moneyflow", "--check-only"]).moneyflow is True
    assert module.FIXED["max_debate_rounds"] == module.FIXED["max_risk_discuss_rounds"] == 1
    assert module.FIXED["analysis_max_calls"] == 60
    assert budgets.checkpoint_budget_binding({}, "graph") == budgets.checkpoint_budget_binding(
        {"bounded_moneyflow_enabled": False}, "graph")
    assert budgets.checkpoint_budget_binding({}, "graph") != budgets.checkpoint_budget_binding(
        {"bounded_moneyflow_enabled": True}, "graph")


def test_existing_tushare_contracts_are_not_expanded():
    from tradingagents import tushare_financial_https, tushare_https
    assert "moneyflow" not in tushare_https.ENDPOINTS
    assert {"income", "balancesheet", "cashflow"} == tushare_financial_https.ENDPOINTS


@pytest.mark.parametrize("failure,fail_audit", [
    (None, False), (KeyboardInterrupt, False), (budgets.AnalysisBudgetStopped, False),
    (KeyboardInterrupt, True),
])
def test_real_coordinator_with_fake_transport_preserves_cancel_and_single_count(
    tmp_path, monkeypatch, budget, failure, fail_audit,
):
    from tests.test_current_tushare_statements import runtime_config
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import record_active_physical_attempt_io
    from tradingagents.market_history.store import MarketHistoryStore

    cfg = runtime_config(tmp_path)
    cfg.update(bounded_moneyflow_enabled=True, tushare_calls_per_minute=40,
               tushare_account_scope_label="personal-2000",
               tushare_operator_safety_ceiling_calls_per_minute=40,
               tushare_financial_https_endpoint="https://api.waditu.com/dataapi")
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(cfg)):
        pass
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-test-token")
    calls, audit = [], []
    def fake_send(binding, token):
        with budgets.admitted_transport("data"):
            record_active_physical_attempt_io()
            calls.append(binding)
            if failure:
                raise failure("synthetic_cancel")
            return response()
    monkeypatch.setattr(mf, "_send", fake_send)
    def record(events):
        audit.extend(events)
        if fail_audit:
            raise OSError("synthetic cleanup fault after cancellation")
    monkeypatch.setattr("tradingagents.dataflows.market_snapshot.record_active_physical_attempt_events", record)
    with mf.moneyflow_scope(cfg, ticker="600895.SH", as_of=ASOF):
        if failure:
            with pytest.raises(failure):
                mf.moneyflow_for_state(state())
            with pytest.raises(budgets.AnalysisBudgetStopped, match="already_started"):
                mf.moneyflow_for_state(state())
        else:
            first = mf.moneyflow_for_state(state())
            assert first["status"] == "available" and len(first["rows"]) == 3
            assert first == mf.moneyflow_for_state(state())
    assert len(calls) == budget.receipt()["data_attempts"] == len(audit) == 1
    assert audit[0].retryable is False


@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_real_analyst_prompt_and_report_keep_decision_unchanged(monkeypatch, budget, language):
    from unittest.mock import MagicMock

    from tests.test_multiyear_integration import combined_state
    from tests.test_supplemental_presentation import MemoryStore, export
    from tradingagents import reporting
    from tradingagents.agents.analysts import sentiment_analyst as analyst
    from tradingagents.agents.schemas import SentimentReport
    from tradingagents.dataflows import config
    from tradingagents.terminal_contract import apply_terminal_contract

    value, evidence = combined_state()
    value["messages"] = []
    monkeypatch.setitem(config._config, "output_language", language)
    monkeypatch.setattr(analyst, "acquire_news", lambda *a, **k: SimpleNamespace(value=None, artifact=None, outcomes=()))
    monkeypatch.setattr(analyst, "get_china_a_local_sentiment", lambda *a, **k: "unavailable synthetic local sentiment")
    monkeypatch.setattr(analyst, "get_china_a_enhancements_for_categories", lambda *a, **k: "")
    llm = MagicMock()
    structured = llm.with_structured_output.return_value
    structured.invoke.return_value = SentimentReport(overall_band="Neutral", overall_score=5.0,
                                                     confidence="low", narrative="合成辅助正文")
    node = analyst.create_sentiment_analyst(llm)
    baseline = node(copy.deepcopy(value))
    calls = []
    def collect(cfg, binding):
        with budgets.admitted_transport("data"):
            calls.append(binding)
            rows = [[binding["as_of"], "12.50"]]
            return rows, mf._projection_digest("moneyflow", binding, rows), datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mf, "_coordinated", collect)
    from langgraph.graph import END, START, StateGraph

    from tradingagents.agents.utils.agent_states import AgentState

    graph = StateGraph(AgentState)
    graph.add_node("sentiment", node)
    graph.add_edge(START, "sentiment")
    graph.add_edge("sentiment", END)
    compiled = graph.compile()
    with mf.moneyflow_scope({}, ticker=value["company_of_interest"], as_of=value["trade_date"]):
        enhanced = compiled.invoke(copy.deepcopy(value))
    assert len(calls) == 1
    messages = structured.invoke.call_args.args[0]
    text = "\n".join(message.content for message in messages)
    assert "12.50" in text and "net_mf_amount" in text and "NOT news or social sentiment" in text
    assert enhanced["evidence_state"] == baseline["evidence_state"]
    assert enhanced["sentiment_report"] == baseline["sentiment_report"]
    value.update(enhanced)
    apply_terminal_contract(value, audit_digest="b" * 64)
    value["decision_audit_created_at"] = "2026-07-18T00:00:00Z"
    before = copy.deepcopy(value)
    def audit(state, *args, **kwargs):
        apply_terminal_contract(state, audit_digest="b" * 64)
        state["decision_audit_created_at"] = "2026-07-18T00:00:00Z"
    monkeypatch.setattr(reporting, "ReportStore", MemoryStore)
    monkeypatch.setattr(reporting, "write_immutable_decision_audit", audit)
    enhanced_files = export(copy.deepcopy(value), None, language)
    plain = copy.deepcopy(value)
    plain.pop("moneyflow_observation")
    plain_files = export(plain, None, language)
    assert b"12.50" in enhanced_files["1_analysts/moneyflow.md"]
    assert enhanced_files["1_analysts/moneyflow.md"] in enhanced_files["complete_report.md"]
    assert enhanced_files["5_portfolio/decision.md"] == plain_files["5_portfolio/decision.md"]
    assert value == before


def test_coalesced_result_keeps_original_received_time(monkeypatch, budget):
    received = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(mf, "_coordinated", lambda c, b: (
        [[ASOF, "1.0"]], mf._projection_digest("moneyflow", b, [[ASOF, "1.0"]]), received))
    value = mf._Run({}, "600895.SH", ASOF).acquire(state())
    assert value["received_at"] == received
    assert value["attempts"] == budget.receipt()["data_attempts"] == 0
    assert "复用并发请求结果" in mf.moneyflow_markdown({**state(), "moneyflow_observation": value})
