"""Exact tool implementations admitted through the counted Requests boundary.

The registry grants no vendor-wide authority. Native Yahoo/curl and unrelated
Alpha Vantage tools remain closed. Physical admission is owned solely by
analysis_budget's HTTPAdapter.send hook: logical calls/cache hits add no counts.
"""
from __future__ import annotations

import re
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
_POLICY_NOTE_PROVIDERS = {
    "get_global_news": "alpha_vantage",
    "get_macro_indicators": "fred",
    "get_prediction_markets": "polymarket",
}


class _PolicyDisabledText(str):
    """Internal typed result of the local deny branch, never provider text."""

    def __new__(cls, text, method, provider):
        value = super().__new__(cls, text)
        value.method = method
        value.provider = provider
        return value


def policy_disabled_tool_result(result, *, method, tool_call_id):
    """Expose only a locally verified deny outcome alongside its exact ToolMessage.

    No SourceArtifact/SourceFact is created. ``attempt=1`` is the local policy
    assessment, not a physical request; there is no HTTP status or retry. Each
    tool call owns its ref so repeated identical sentinels are not ambiguous.
    Legacy/provider strings remain untrusted and retain their old behavior.
    """
    if type(result) is not _PolicyDisabledText:
        return result, None
    from datetime import datetime, timezone

    from tradingagents.analysis_budget import active_budget
    from tradingagents.evidence import (
        AcquisitionUnavailableReason,
        SourceAcquisitionUnavailable,
        ToolExecutionEvidenceEnvelope,
        stable_acquisition_source_ref,
    )

    budget = active_budget()
    expected = _POLICY_NOTE_PROVIDERS.get(method)
    if (budget is None or expected is None or result.method != method
            or result.provider != expected or (tool_call_id is not None
            and (not isinstance(tool_call_id, str) or not tool_call_id.strip()))):
        raise ValueError("unbound_policy_disabled_result")
    budget.check()
    if budget.allowed_data_vendors is None or expected in budget.allowed_data_vendors:
        raise ValueError("policy_disabled_result_conflicts_with_active_policy")
    # Re-derive the nonsecret sentinel from the same policy owner. This is
    # idempotent gap bookkeeping, not budget reservation or acquisition.
    content = budget.record_data_gap(method, "user_disabled", provider=expected)
    if str(result) != content:
        raise ValueError("policy_disabled_result_content_mismatch")
    if tool_call_id is None:
        # Legacy direct .invoke(dict) has no trusted ToolNode call identity.
        # Preserve its text result without minting provenance from caller args.
        return content, None
    source_ref = stable_acquisition_source_ref("policy-disabled", method, expected, tool_call_id)
    outcome = SourceAcquisitionUnavailable(
        provider=expected, capability=method, source_ref=source_ref, attempt=1,
        acquisition_id=stable_acquisition_source_ref("policy-call", method, tool_call_id),
        retrieved_at=datetime.now(timezone.utc).isoformat(), retryable=False,
        reason=AcquisitionUnavailableReason.NOT_CONFIGURED, error_code="budget_user_disabled",
    )
    envelope = ToolExecutionEvidenceEnvelope(tool_call_id=tool_call_id, tool_name=method,
        source_ref=source_ref, capability=method, acquisition_outcomes=(outcome,), selected_artifact=None)
    return content, envelope.model_dump(mode="json")

# Exact public upstream operations used by mainland-core's news, fundamentals
# and local sentiment implementations. This is not a vendor-wide network grant.
_CORE_GET_PATHS = {
    "search-api-web.eastmoney.com": {"/search/jsonp"},
    "quotes.sina.cn": {"/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"},
    "push2his.eastmoney.com": {"/api/qt/stock/fflow/daykline/get"},
    "datacenter-web.eastmoney.com": {"/api/data/v1/get"},
}
_CORE_POST_PATHS = {
    "emappdata.eastmoney.com": {"/stockrank/getHisList", "/stockrank/getHisProfileList"},
}


def core_data_policy():
    from tradingagents.analysis_budget import active_budget

    budget = active_budget()
    return budget is not None and budget.allowed_data_vendors == {"tushare", "akshare"}


def check_core_request_auth(request, session=None):
    """Reject explicit/retained credentials before Requests can prepare them."""
    if not core_data_policy():
        return
    from tradingagents.analysis_budget import active_budget

    headers = {str(key).lower() for key in request.headers}
    if (headers & {"authorization", "proxy-authorization", "cookie"}
            or getattr(request, "auth", None) is not None
            or getattr(request, "cookies", None)
            or (session is not None and (session.auth is not None or session.cookies))):
        active_budget().stop("uncovered_data_credentials")


def supports_budget_data(method, vendor, implementation):
    route = _ROUTES.get((method, vendor))
    if route is None:
        return False
    module, name, _ = route
    return implementation is getattr(import_module("tradingagents.dataflows." + module), name)



def supports_budget_snapshot(vendor, implementation):
    """Only the existing counted Tushare history adapter; no vendor-wide grant."""
    if vendor != "tushare":
        return False
    from tradingagents.dataflows.market_snapshot import (
        _load_tushare,
        _tushare_history_binding,
    )

    return (
        implementation.load is _load_tushare
        and implementation.history_load is _load_tushare
        and implementation.history_binding is _tushare_history_binding
        and implementation.adjustment_basis == "qfq"
        and implementation.uses_typed_physical_requests is True
        and implementation.history_uses_typed_physical_requests is True
    )


def require_budget_snapshot(vendor, implementation):
    from tradingagents.analysis_budget import active_budget, require_data_vendor

    budget = active_budget()
    if budget is not None:
        budget.check()
        if budget.allowed_data_vendors is not None and vendor not in budget.allowed_data_vendors:
            budget.stop("disabled_data_vendor")
        if supports_budget_snapshot(vendor, implementation):
            return
    require_data_vendor(vendor)


def check_budget_data_destination(request):
    """Check every physical send, including redirects, before admission/I/O."""
    from tradingagents.analysis_budget import active_budget

    if core_data_policy():
        check_core_request_auth(request)
        try:
            target = urlsplit(request.url)
            valid = (target.scheme == "https" and target.port in (None, 443)
                     and not target.username and not target.password and not target.fragment)
            endpoint = (
                request.method == "GET" and (
                    target.path in _CORE_GET_PATHS.get(target.hostname, ())
                    or (target.hostname == "basic.10jqka.com.cn"
                        and re.fullmatch(r"/new/[0-9]{6}/operate\.html", target.path))))
            endpoint = endpoint or (request.method == "POST"
                                    and target.path in _CORE_POST_PATHS.get(target.hostname, ()))
        except (ValueError, TypeError):
            valid = endpoint = False
        from tradingagents.full_notice import matches as matches_full_notice
        from tradingagents.full_lpr import matches as matches_lpr
        if not valid or not (endpoint or matches_full_notice(request) or matches_lpr(request)):
            active_budget().stop("uncovered_data_destination")
        return
    expected = _DESTINATION.get()
    if expected is None:
        return
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
    if budget.allowed_data_vendors is not None and vendor not in budget.allowed_data_vendors:
        if (method, vendor) in _ROUTES and method in {
            "get_global_news", "get_insider_transactions", "get_macro_indicators", "get_prediction_markets"
        }:
            text = budget.record_data_gap(method, "user_disabled", provider=vendor)
            if _POLICY_NOTE_PROVIDERS.get(method) == vendor:
                return _PolicyDisabledText(text, method, vendor)
            return text
        budget.stop("disabled_data_vendor")
    if vendor == "akshare":
        require_data_vendor(vendor)
        return implementation(*args, **kwargs)
    if not supports_budget_data(method, vendor, implementation):
        budget.stop("uncovered_data_vendor")
    from tradingagents.dataflows.config import get_config

    enrichment = (
        vendor == "alpha_vantage" and method in ALPHA_ENRICHMENT_TOOLS
        and not get_config().get("budget_require_macro_sources", False)
    )
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
            try:
                result = implementation(*args, **kwargs)
            except Exception:
                if get_config().get("budget_require_macro_sources", False) and method in {
                    "get_global_news", "get_macro_indicators", "get_prediction_markets"
                }:
                    budget.stop("required_macro_source_unavailable")
                raise
        if method == "get_global_news" and get_config().get("budget_require_macro_sources", False):
            import json

            try:
                payload = json.loads(result) if isinstance(result, str) else result
                usable = (
                    isinstance(payload, dict) and isinstance(payload.get("feed"), list)
                    and not any(key in payload for key in ("Information", "Note", "Error Message"))
                )
            except (ValueError, TypeError):
                usable = False
            if not usable:
                budget.stop("required_macro_response_unverified")
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


