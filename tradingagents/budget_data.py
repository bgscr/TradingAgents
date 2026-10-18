"""Exact tool implementations admitted through the counted Requests boundary.

The registry grants no vendor-wide authority. Native Yahoo/curl and unrelated
Alpha Vantage tools remain closed. Physical admission is owned solely by
analysis_budget's HTTPAdapter.send hook: logical calls/cache hits add no counts.
"""
from __future__ import annotations

from contextvars import ContextVar
from importlib import import_module
from urllib.parse import urlsplit

_ROUTES = {
    ("get_news", "alpha_vantage"): ("alpha_vantage_news", "get_news", "www.alphavantage.co"),
    ("get_global_news", "alpha_vantage"): ("alpha_vantage_news", "get_global_news", "www.alphavantage.co"),
    ("get_insider_transactions", "alpha_vantage"): ("alpha_vantage_news", "get_insider_transactions", "www.alphavantage.co"),
    ("get_macro_indicators", "fred"): ("fred", "get_macro_data", "api.stlouisfed.org"),
    ("get_prediction_markets", "polymarket"): ("polymarket", "get_prediction_markets", "gamma-api.polymarket.com"),
}
ALPHA_ENRICHMENT_TOOLS = frozenset(("get_global_news", "get_insider_transactions"))
_DESTINATION: ContextVar[str | None] = ContextVar("budget_data_destination", default=None)


def supports_budget_data(method, vendor, implementation):
    route = _ROUTES.get((method, vendor))
    if route is None:
        return False
    module, name, _ = route
    return implementation is getattr(import_module("tradingagents.dataflows." + module), name)


def check_budget_data_destination(request):
    """Check every physical send, including redirects, before admission/I/O."""
    expected = _DESTINATION.get()
    if expected is None:
        return
    from tradingagents.analysis_budget import active_budget

    target = urlsplit(request.url)
    if (target.scheme != "https" or target.hostname != expected
            or target.port not in (None, 443) or target.username or target.password
            or request.method != "GET"):
        active_budget().stop("uncovered_data_destination")


def invoke_budget_data(method, vendor, implementation, *args, **kwargs):
    from tradingagents.analysis_budget import active_budget, require_data_vendor

    budget = active_budget()
    if budget is None:
        return implementation(*args, **kwargs)
    budget.check()
    if vendor == "akshare":
        require_data_vendor(vendor)
        return implementation(*args, **kwargs)
    if not supports_budget_data(method, vendor, implementation):
        budget.stop("uncovered_data_vendor")
    enrichment = vendor == "alpha_vantage" and method in ALPHA_ENRICHMENT_TOOLS
    if vendor == "alpha_vantage" and budget.disable_alpha_vantage:
        if enrichment:
            return budget.record_data_gap(method, "user_disabled")
        # Other Alpha capabilities require an explicit compatible configuration;
        # never turn an operational sentinel into an acquired news artifact.
        budget.stop("disabled_data_vendor")
    if enrichment:
        from tradingagents.dataflows.alpha_vantage_common import (
            AlphaVantageNotConfiguredError,
            get_api_key,
        )

        try:
            get_api_key()
        except AlphaVantageNotConfiguredError:
            return budget.record_data_gap(method, "not_configured")
    token = _DESTINATION.set(_ROUTES[(method, vendor)][2])
    try:
        if enrichment:
            result = _invoke_alpha_enrichment(budget, method, implementation, *args, **kwargs)
        else:
            result = implementation(*args, **kwargs)
        budget.check()
        return result
    finally:
        _DESTINATION.reset(token)


def _invoke_alpha_enrichment(budget, method, implementation, *args, **kwargs):
    from requests import HTTPError

    from tradingagents.dataflows.alpha_vantage_common import (
        AlphaVantageEntitlementError,
        AlphaVantageNotConfiguredError,
        AlphaVantageRateLimitError,
    )

    try:
        return implementation(*args, **kwargs)
    except AlphaVantageEntitlementError:
        return budget.record_data_gap(method, "permission_denied")
    except AlphaVantageNotConfiguredError:
        # The local presence check already passed: this is a remote rejection.
        return budget.record_data_gap(method, "authentication_failed")
    except AlphaVantageRateLimitError:
        return budget.record_data_gap(method, "rate_limited")
    except HTTPError as error:
        code = error.response.status_code if error.response is not None else None
        reason = {401: "authentication_failed", 403: "permission_denied", 429: "rate_limited"}.get(code)
        if reason is None:
            raise
        return budget.record_data_gap(method, reason)
