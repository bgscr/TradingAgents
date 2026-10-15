import json
import ssl
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents import tushare_financial_https as transport
from tradingagents.market_history.coordinator import PhysicalAttemptFailure, PhysicalAttemptOutcome


def fake_connection(
    monkeypatch, *, status=200, payload=None, retry_after="2", on_response=None, close_error=False
):
    calls = []
    if payload is None:
        payload = {
            "code": 0,
            "data": {
                "fields": ["ts_code", "ann_date", "end_date"],
                "items": [["600895.SH", "20260420", "20251231"]],
            },
        }

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
            calls.append(("connect", host, timeout))

        def request(self, method, path, *, body, headers):
            calls.append(("request", method, path, json.loads(body), headers))

        def getresponse(self):
            if on_response is not None:
                on_response()
            return self

        def read(self, maximum):
            return json.dumps(payload).encode()[:maximum]

        def getheader(self, name, default=None):
            return retry_after if name == "Retry-After" else default

        def close(self):
            calls.append(("close",))
            if close_error:
                raise ConnectionError("synthetic-sensitive-cleanup-failure")

    Connection.status = status
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(
        transport.ssl, "create_default_context", lambda **_: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-test-token")
    return calls


@pytest.mark.parametrize(
    "base,host,path",
    [
        ("https://api.tushare.pro", "api.tushare.pro", "/"),
        ("https://api.waditu.com/dataapi", "api.waditu.com", "/dataapi/income"),
    ],
)
def test_explicit_https_protocol_one_post(monkeypatch, base, host, path):
    calls = fake_connection(monkeypatch)
    frame = transport.create_client(base).query(
        "income", ts_code="600895.SH", start_date="20200101", end_date="20261015", report_type="1"
    )
    assert frame.iloc[0]["end_date"] == "20251231"
    assert calls[0] == ("connect", host, 30)
    request = calls[1]
    assert request[1:3] == ("POST", path)
    assert request[3]["token"] == "synthetic-test-token"
    assert request[3]["api_name"] == "income"
    assert ("ts_type_name" in request[3]["params"]) == ("waditu" in base)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "status,outcome",
    [(302, PhysicalAttemptOutcome.PROVIDER_ERROR), (429, PhysicalAttemptOutcome.RATE_LIMITED)],
)
def test_no_redirect_or_retry_and_sanitized_failure(monkeypatch, status, outcome):
    calls = fake_connection(monkeypatch, status=status)
    client = transport.create_client("https://api.tushare.pro")
    with pytest.raises(PhysicalAttemptFailure) as caught:
        client.query(
            "cashflow",
            ts_code="600895.SH",
            start_date="20200101",
            end_date="20261015",
            report_type="1",
        )
    assert caught.value.outcome is outcome
    assert not caught.value.retryable
    assert "synthetic-test-token" not in str(caught.value)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://api.tushare.pro",
        "https://attacker.example",
        "https://api.tushare.pro/redirect",
    ],
)
def test_unapproved_destination_rejects_before_token_read(monkeypatch, endpoint):
    def forbidden(*_):
        raise AssertionError("credential lookup forbidden")

    monkeypatch.setattr(transport.os, "getenv", forbidden)
    with pytest.raises(ValueError):
        transport.create_client(endpoint)


def test_timeout_retains_its_type_and_cannot_retry(monkeypatch):
    calls = fake_connection(monkeypatch)
    original = transport.http.client.HTTPSConnection

    class TimeoutConnection(original):
        def getresponse(self):
            raise TimeoutError("synthetic-sensitive-message")

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", TimeoutConnection)
    with pytest.raises(PhysicalAttemptFailure) as caught:
        transport.create_client("https://api.tushare.pro").query(
            "income",
            ts_code="600895.SH",
            start_date="20200101",
            end_date="20261015",
            report_type="1",
        )
    assert caught.value.outcome is PhysicalAttemptOutcome.TIMEOUT
    assert caught.value.__context__ is None
    assert not caught.value.retryable
    assert len(calls) == 3


@pytest.mark.parametrize(
    "payload,outcome",
    [
        ({"code": 2002, "msg": "synthetic 权限不足"}, PhysicalAttemptOutcome.PERMISSION_DENIED),
        ({"code": -1, "msg": "synthetic 每分钟"}, PhysicalAttemptOutcome.RATE_LIMITED),
        (
            {"code": 0, "data": {"fields": ["same", "same"], "items": [[1, 2]]}},
            PhysicalAttemptOutcome.MALFORMED_RESPONSE,
        ),
    ],
)
def test_business_failures_are_typed_without_response_text(monkeypatch, payload, outcome):
    calls = fake_connection(monkeypatch, payload=payload)
    with pytest.raises(PhysicalAttemptFailure) as caught:
        transport.create_client("https://api.tushare.pro").query(
            "income",
            ts_code="600895.SH",
            start_date="20200101",
            end_date="20261015",
            report_type="1",
        )
    assert caught.value.outcome is outcome
    assert "synthetic" not in str(caught.value)
    assert caught.value.__context__ is None
    assert len(calls) == 3


@pytest.mark.parametrize(
    "header,deadline_seconds",
    [("Thu, 15 Oct 2026 04:30:00 GMT", 1800), ("1800", 1820)],
)
def test_retry_after_is_shared_until_deadline_despite_request_latency(
    tmp_path, monkeypatch, header, deadline_seconds
):
    from tests.test_current_tushare_statements import Clock, dispatcher, request, runtime_config

    clock = Clock()
    started_at = clock()
    calls = fake_connection(
        monkeypatch, status=429, retry_after=header, on_response=lambda: clock.sleep(20)
    )

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock().astimezone(tz or timezone.utc)

    monkeypatch.setattr(transport, "datetime", ControlledDatetime)
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    first = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert first.plan_outcomes[0].outcome.retry_after_seconds == deadline_seconds
    clock.value = started_at + timedelta(seconds=deadline_seconds - 10)
    second = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_income_statement")
    )
    assert second.artifact is None
    assert len([c for c in calls if c[0] == "request"]) == 1
    assert second.plan_outcomes[0].outcome.retry_after_seconds == 10
    clock.value += timedelta(seconds=11)
    dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(request("get_cashflow"))
    assert len([c for c in calls if c[0] == "request"]) == 2


@pytest.mark.parametrize("status,expected_wait", [(429, 2), (200, 300)])
def test_cleanup_failure_preserves_rate_limit_and_shared_cooldown(
    tmp_path, monkeypatch, status, expected_wait
):
    from tests.test_current_tushare_statements import Clock, dispatcher, request, runtime_config

    calls = fake_connection(
        monkeypatch,
        status=status,
        payload={"code": -1, "msg": "synthetic 每分钟"},
        close_error=True,
    )
    clock = Clock()
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    for tool in ("get_balance_sheet", "get_income_statement"):
        result = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(request(tool))
        assert result.artifact is None
        assert result.plan_outcomes[0].outcome.reason.value == "rate_limited"
        assert result.plan_outcomes[0].outcome.retry_after_seconds == expected_wait
    assert len([c for c in calls if c[0] == "request"]) == 1
