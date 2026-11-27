"""Synthetic dated Eastmoney/Tushare responses. No provider credentials or network."""

import json
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import personal_bootstrap as cold, personal_dc_ratio as ratio
from tests.test_personal_observations import DAY, SYMBOL, binding, profile
from tradingagents.market_history.config import provider_request_authority_config
from tradingagents.market_history.coordinator import PhysicalAttemptFailure
from tradingagents.market_history.store import MarketHistoryStore


def wire(rows, *, fields=None, code=0):
    return json.dumps(
        {"code": code, "data": {"fields": fields or ratio.FIELDS.split(","), "items": rows}}
    ).encode()


@pytest.mark.parametrize(
    "case,expected",
    [
        ("outflow", "-2.5"),
        ("zero", "0"),
        ("empty", None),
        ("wrong_stock", "reject"),
        ("wrong_day", "reject"),
        ("extra_field", "reject"),
        ("other_ratio", "reject"),
        ("range", "reject"),
        ("string", "reject"),
        ("duplicates", "reject"),
        ("business_denial", "denied"),
    ],
)
def test_only_dated_main_net_percentage(case, expected):
    columns = ratio.FIELDS.split(",")
    row = ["600895.SH", "20240229", -2.5]
    if case == "zero":
        row[2] = 0
    if case == "wrong_stock":
        row[0] = "000001.SZ"
    if case == "wrong_day":
        row[1] = "20240228"
    if case == "extra_field":
        columns += ["close"]
        row.append(1)
    if case == "other_ratio":
        columns[2] = "buy_lg_amount_rate"
    if case == "range":
        row[2] = 101
    if case == "string":
        row[2] = "2%"
    rows = [] if case == "empty" else [row, row] if case == "duplicates" else [row]
    raw = wire(rows, fields=columns, code=2002 if case == "business_denial" else 0)
    if expected in ("reject", "denied"):
        with pytest.raises(PhysicalAttemptFailure) as err:
            ratio.project(raw, binding())
        assert err.value.retryable is False
        if expected == "denied":
            assert err.value.outcome.value == "permission_denied"
        return
    result = ratio.project(raw, binding())
    assert result["percent"] == expected and set(result) == {
        "symbol",
        "trade_date",
        "percent",
        "observed_at",
    }


def test_physical_result_is_scoped_fresh_and_not_self_authorized(tmp_path, monkeypatch):
    moment = datetime(2024, 3, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(cold, "now", lambda: moment)
    c = dict(profile(tmp_path), personal_fetch_dc_ratio=True)

    def session():
        s = cold.Session(c, (time.time(), time.monotonic()))
        s.symbol = SYMBOL
        s.day = date.fromisoformat(DAY)
        return s

    s = session()
    with MarketHistoryStore.open_provider_request_authority(s.history):
        pass
    authority = provider_request_authority_config(s.history)
    b = binding()
    sent = []

    def send(token, endpoint, params, fields, remaining, admit):
        assert (
            token == "synthetic"
            and endpoint == "moneyflow_dc"
            and params == {"ts_code": "600895.SH", "trade_date": "20240229"}
        )
        assert fields == "ts_code,trade_date,net_amount_rate" and remaining() > 0
        admit()
        sent.append(endpoint)
        return wire([["600895.SH", "20240229", -1.25]])

    monkeypatch.setattr(cold, "post", send)
    assert (
        ratio.observe(s, b, "synthetic", authority, fetch=False)["status"] == "cache_missing"
        and not sent
    )
    s.counts = dict.fromkeys(cold.FINANCE, 1)
    assert (
        ratio.observe(s, b, "synthetic", authority, fetch=True)["status"]
        == "financial_budget_unavailable"
        and not sent
    )
    s = session()
    first = ratio.observe(s, b, "synthetic", authority, fetch=True)
    assert (
        first["status"] == "verified"
        and first["main_net_percent"] == "-1.25"
        and first["acquisition"] == "account_request"
    )
    assert s.financial_attempts() == 1
    assert ratio.observe(session(), b, "synthetic", authority, fetch=False) == dict(
        first, acquisition="coordinator_cache"
    )
    key = ratio.request_key(b, s.account_scope)
    for other in [
        dict(b, trade_date="2024-02-28"),
        dict(b, symbol="000001.SZ"),
        dict(b, snapshot_id="snapshot:v2:" + "d" * 64),
    ]:
        assert (
            ratio.cached(authority, ratio.request_key(other, s.account_scope), other)["status"]
            == "cache_missing"
        )
    assert (
        ratio.cached(authority, ratio.request_key(b, "separate synthetic account"), b)["status"]
        == "cache_missing"
    )
    moment += timedelta(days=1)
    assert ratio.cached(authority, key, b)["status"] == "cache_stale"
    moment -= timedelta(days=1)
    with closing(sqlite3.connect(authority.database_path)) as con, con:
        con.execute(
            "UPDATE provider_request_sequences SET result_sha256=? WHERE request_key=?",
            ("a" * 64, key),
        )
    assert (
        ratio.observe(session(), b, "synthetic", authority, fetch=True)["status"]
        == "cache_unverified"
    )
    assert sent == ["moneyflow_dc"]


def test_account_ratio_wire_is_fixed_and_rejects_extra_columns_before_send(monkeypatch):
    from types import SimpleNamespace

    calls = []
    raw = wire([["600895.SH", "20240229", -1.25]])
    blocks = iter([raw, b""])
    params = {"ts_code": "600895.SH", "trade_date": "20240229"}

    class Connection:
        sock = SimpleNamespace(settimeout=lambda timeout: None)

        def __init__(self, host, *, timeout, context):
            assert host == "api.waditu.com" and timeout == 5 and context.check_hostname
            calls.append("connect")

        def request(self, method, path, *, body, headers):
            assert method == "POST" and path == "/dataapi/moneyflow_dc"
            assert json.loads(body) == {
                "api_name": "moneyflow_dc",
                "fields": "ts_code,trade_date,net_amount_rate",
                "token": "synthetic",
                "params": {**params, "ts_type_name": cold.BASE},
            }
            calls.append("post")

        def getresponse(self):
            return SimpleNamespace(status=200, read1=lambda limit: next(blocks))

        def close(self):
            calls.append("close")

    monkeypatch.setattr(cold.http.client, "HTTPSConnection", Connection)

    def query(fields, parameters):
        return cold.post(
            "synthetic",
            ratio.ENDPOINT,
            parameters,
            fields,
            lambda: 5,
            lambda: calls.append("admit"),
        )

    for fields, parameters in (
        (ratio.FIELDS + ",close", params),
        (ratio.FIELDS, {**params, "start_date": "20240201"}),
    ):
        with pytest.raises(cold.PreparationError, match="scope_mismatch"):
            query(fields, parameters)
    assert calls == []
    assert query(ratio.FIELDS, params) == raw
    assert calls == ["connect", "admit", "post", "close"]


def test_default_session_refuses_account_ratio_transport_before_admission(tmp_path):
    s = cold.Session(profile(tmp_path), (time.time(), time.monotonic()))
    s.symbol = SYMBOL
    s.day = date.fromisoformat(DAY)
    with pytest.raises(cold.PreparationError, match="scope_mismatch"):
        s.query(
            "synthetic",
            ratio.ENDPOINT,
            {"ts_code": "600895.SH", "trade_date": "20240229"},
            ratio.FIELDS,
            observation_binding=binding(),
        )
    assert s.counts == {} and not (tmp_path / "reports").exists()
