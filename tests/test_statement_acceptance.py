"""Manual acceptance must traverse the production dispatcher and HTTPS client."""

import json

import pytest

from tests.test_current_tushare_statements import statement_rows


@pytest.mark.parametrize("failure_at", [1, 2, 3])
@pytest.mark.parametrize(
    "failure", ["redirect", "rate_limit", "permission", "timeout", "malformed", "empty"]
)
def test_three_statement_acceptance_stops_at_first_http_failure(
    tmp_path, monkeypatch, failure_at, failure
):
    from tradingagents import (
        statement_acceptance as acceptance,
        tushare_financial_https as transport,
    )

    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            self.status = 200

        def request(self, method, path, *, body, headers):
            calls.append((method, path))
            self.endpoint = json.loads(body)["api_name"]
            if len(calls) == failure_at:
                self.status = {"redirect": 302, "rate_limit": 429, "permission": 403}.get(
                    failure, 200
                )
                if failure == "timeout":
                    raise TimeoutError("synthetic timeout")

        def getresponse(self):
            return self

        def getheader(self, name, default=None):
            return "1" if name == "Retry-After" else default

        def read(self, limit):
            if len(calls) == failure_at and failure == "malformed":
                return b"not json"
            frame = statement_rows(self.endpoint)
            items = [] if len(calls) == failure_at and failure == "empty" else frame.values.tolist()
            return json.dumps(
                {"code": 0, "data": {"fields": list(frame.columns), "items": items}}
            ).encode()

        def close(self):
            pass

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    result = acceptance.acquire(
        tmp_path,
        destination="https://api.tushare.pro",
        account="synthetic",
        as_of="2026-10-15",
        token="synthetic-only",
    )
    assert len(calls) == failure_at
    assert result["status"] == "stopped"
    assert result["physical_requests"] == failure_at
    assert len(result["completed_tools"]) == failure_at - 1


@pytest.mark.parametrize(
    "destination", ["https://api.tushare.pro", "https://api.waditu.com/dataapi"]
)
def test_acceptance_three_physical_posts_and_offline_views(tmp_path, monkeypatch, destination):
    from tradingagents import (
        statement_acceptance as acceptance,
        tushare_financial_https as transport,
    )

    calls = []

    class Connection:
        status = 200

        def __init__(self, host, **kwargs):
            self.host = host

        def request(self, method, path, *, body, headers):
            payload = json.loads(body)
            self.endpoint = payload["api_name"]
            calls.append((self.host, method, path, self.endpoint))

        def getresponse(self):
            return self

        def read(self, limit):
            frame = statement_rows(self.endpoint)
            return json.dumps(
                {"code": 0, "data": {"fields": list(frame.columns), "items": frame.values.tolist()}}
            ).encode()

        def close(self):
            pass

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    result = acceptance.acquire(
        tmp_path,
        destination=destination,
        account="synthetic",
        as_of="2026-10-15",
        token="synthetic-only",
    )
    assert result["status"] == "acquired"
    assert result["physical_requests"] == 3
    assert [call[3] for call in calls] == ["balancesheet", "income", "cashflow"]
    replay = acceptance.reuse(
        tmp_path, destination=destination, account="synthetic", as_of="2026-10-15"
    )
    assert replay["status"] == "reused"
    assert replay["views"] == result["views"]
    assert len(calls) == 3
