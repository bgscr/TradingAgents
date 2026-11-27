"""Synthetic acceptance tests for the one-exchange-per-statement contract."""
from datetime import date, datetime, timezone
from hashlib import sha256

from tests.test_financial_acceptance_scope import fake_https, sample_rows
from tradingagents import statement_acceptance as acceptance
from tradingagents.dataflows import current_tushare_statements as statements
from tradingagents.dataflows.current_statement_cache import CurrentStatementCache, StatementCacheScope
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity


def test_split_window_stops_before_financial_admission(tmp_path, monkeypatch):
    request = dict(ticker="600895.SH", destination="https://api.tushare.pro",
                   account="fixture", as_of="2026-10-16")
    _, asset = acceptance.validate_request(tmp_path, **request)
    identity = ProviderSubrequestInstrumentIdentity.from_authoritative(asset.instrument_identity)
    scope = StatementCacheScope("fixture", identity.identity_revision, "balancesheet",
                                sha256(request["destination"].encode()).hexdigest(),
                                statements.NORMALIZER_VERSION)
    cache = CurrentStatementCache(tmp_path / "cache/current-financial-statements.sqlite3")
    fresh = date(2026, 10, 15)
    cache.read_through(scope, fresh, fresh, datetime.now(timezone.utc), lambda *_: b"{}")

    calls = fake_https(monkeypatch, sample_rows)
    result = acceptance.acquire(tmp_path, **request, token="synthetic-only")
    assert result["status"] == "stopped"
    assert result["failure"]["error_code"] == "financial_window_requires_multiple_exchanges"
    assert result["failure"]["retryable"] is False
    assert result["physical_requests"] == 0 and not calls
    assert not (tmp_path / "coordinator.sqlite3.provider-requests.sqlite3").exists()
    def unexpected_fetch(*args):
        raise AssertionError("fresh coverage should be reused")

    hit = cache.read_through(scope, fresh, fresh, datetime.now(timezone.utc), unexpected_fetch)
    assert hit.reused_ranges == ((fresh, fresh),) and not hit.fetched_ranges
