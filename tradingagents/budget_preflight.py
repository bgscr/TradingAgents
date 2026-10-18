"""Side-effect-free route inventory for opt-in analysis budgets.

This is a configuration gate, not cache readiness or service qualification.
Only registered identifiers enter diagnostics; arbitrary configuration values,
paths, credentials and provider exception text are never rendered.
"""
from __future__ import annotations

from dataclasses import dataclass

# Includes every executable ToolNode entry, including the news node's legacy
# insider tool. Tests compare this inventory with real nodes and analyst binds.
ANALYST_TOOLS = {
    "market": ("get_stock_data", "get_indicators", "get_verified_market_snapshot"),
    "social": ("get_news",),
    "news": ("get_news", "get_global_news", "get_insider_transactions",
             "get_macro_indicators", "get_prediction_markets"),
    "fundamentals": ("get_fundamentals", "get_balance_sheet", "get_cashflow",
                     "get_income_statement"),
}
_STATEMENTS = frozenset(("get_balance_sheet", "get_cashflow", "get_income_statement"))


@dataclass(frozen=True)
class BudgetRouteIssue:
    location: str
    position: int
    provider: str
    reason: str


@dataclass(frozen=True)
class BudgetRouteReport:
    issues: tuple[BudgetRouteIssue, ...]
    chains: tuple[tuple[str, tuple[str, ...]], ...]
    # These are prerequisites, deliberately not claims that a cache is usable.
    dependencies: tuple[str, ...]


class BudgetRouteConfigurationError(ValueError):
    def __init__(self, report: BudgetRouteReport, readiness_report=None):
        self.report = report
        self.readiness_report = readiness_report
        details = "; ".join(
            f"{issue.location}[{issue.position}]={issue.provider}:{issue.reason}"
            for issue in report.issues
        )
        if readiness_report is None:
            readiness = "; readiness_not_checked=" + ",".join(report.dependencies)
        else:
            readiness = "; contextual_issues=" + "; ".join(
                item.location + ":" + item.reason for item in readiness_report.issues
            )
        super().__init__("analysis_budget:incompatible_routes; " + details + readiness)


def inspect_budget_routes(config, *, selected_analysts=tuple(ANALYST_TOOLS), symbol=None,
                          financial_config=None, budget_config=None):
    """Inspect final merged routes without calling tools, providers or cache APIs.

    Unknown symbol defers market-specific checks until the actual run boundary.
    Every configured fallback is inspected even when an earlier source could
    succeed. The runtime budget guard remains necessary for dynamic tool args,
    cache races and transports. Disabled mode imports no provider modules.
    """
    if config.get("analysis_budget_enabled", False) is False:
        return BudgetRouteReport((), (), ())

    from tradingagents.analysis_budget import inspect_budget_config
    from tradingagents.dataflows.interface import (
        TOOLS_CATEGORIES,
        VENDOR_METHODS,
        get_akshare_placeholder,
        get_baostock_placeholder,
    )
    from tradingagents.dataflows.market_snapshot import SNAPSHOT_PROVIDERS
    from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol

    issues = [BudgetRouteIssue("config." + field, 0, "<none>", reason)
              for field, reason in inspect_budget_config(config if budget_config is None else budget_config)]
    policy = config if budget_config is None else budget_config
    disable_alpha = policy.get("analysis_disable_alpha_vantage", False) is True
    chains, dependencies = [], []
    known = set(SNAPSHOT_PROVIDERS) | {"tushare", "stocktwits", "reddit"}
    known.update(name for vendors in VENDOR_METHODS.values() for name in vendors)
    mainland = symbol is not None and resolve_china_a_symbol(symbol) is not None

    def add(location, position, vendor, reason):
        issues.append(BudgetRouteIssue(location, position,
                                      vendor if vendor in known else "<unknown>", reason))

    def mapping(value, location):
        if not isinstance(value, dict):
            add(location, 0, "", "invalid_mapping")
            return {}
        return value

    data = mapping(config.get("data_vendors", {}), "data_vendors")
    markets = mapping(config.get("market_data_vendors", {}), "market_data_vendors")
    market = mapping(markets.get("cn_a", {}), "market_data_vendors.cn_a") if mainland else {}
    tools = mapping(config.get("tool_vendors", {}), "tool_vendors")
    financial = config if financial_config is None else financial_config
    financial_tools = mapping(financial.get("tool_vendors", {}), "financial.tool_vendors")
    financial_data = mapping(financial.get("data_vendors", {}), "financial.data_vendors")
    financial_markets = mapping(financial.get("market_data_vendors", {}), "financial.market_data_vendors")
    financial_market = mapping(financial_markets.get("cn_a", {}), "financial.market_data_vendors.cn_a") if mainland else {}

    def tokens(value, location, default, *, snapshot=False):
        if not isinstance(value, str) or not value.strip():
            add(location, 0, "", "invalid_provider_chain")
            return ()
        parts = tuple(part.strip() for part in value.split(","))
        if snapshot:
            result = tuple(default) if value == "default" else parts
        else:
            result = tuple(part for part in parts if part and part != "default") or tuple(default)
        for index, part in enumerate(parts, 1):
            if not part:
                add(location, index, "", "empty_provider")
        chains.append((location, tuple(part if part in known else "<unknown>" for part in result)))
        return result

    def check_chain(location, chain, available, method=None):
        for index, vendor in enumerate(chain, 1):
            if vendor not in available:
                add(location, index, vendor, "provider_not_implemented")
            elif available[vendor] in (get_akshare_placeholder, get_baostock_placeholder):
                add(location, index, vendor, "placeholder_implementation")
            elif (disable_alpha and vendor == "alpha_vantage"
                  and method not in {"get_global_news", "get_insider_transactions"}):
                add(location, index, vendor, "disabled_provider_requires_explicit_alternative")
            elif vendor != "akshare":
                from tradingagents.budget_data import supports_budget_data

                if not supports_budget_data(method, vendor, available[vendor]):
                    add(location, index, vendor, "uncovered_data_vendor")

    if symbol is None:
        dependencies.append("instrument_context")
    else:
        value = market.get("core_stock_apis", "default")
        if value == "default":
            value = data.get("core_stock_apis", "default")
        chain = tokens(value, "market_snapshot", ("akshare", "baostock", "yfinance"), snapshot=True)
        check_chain("market_snapshot", chain, SNAPSHOT_PROVIDERS)
        dependencies.append("market_cache_integrity_freshness_and_window")
        if mainland:
            dependencies.append("verified_mainland_calendar_window_or_uncovered_baostock_acquisition")

    for analyst in selected_analysts:
        analyst = getattr(analyst, "value", analyst)
        if not isinstance(analyst, str) or analyst not in ANALYST_TOOLS:
            add("analysts", 0, "", "unknown_analyst")
            continue
        for method in ANALYST_TOOLS[analyst]:
            location = analyst + "." + method
            method_tools = financial_tools if analyst == "fundamentals" else tools
            method_data = financial_data if analyst == "fundamentals" else data
            method_market = financial_market if analyst == "fundamentals" else market
            if method == "get_verified_market_snapshot" or (mainland and method in ANALYST_TOOLS["market"]):
                continue
            # Unknown ticker: ticker-scoped choices cannot be resolved yet.
            if symbol is None and method not in ("get_global_news", "get_macro_indicators", "get_prediction_markets"):
                continue
            if method in _STATEMENTS and method_tools.get(method) == "tushare":
                chains.append((location, ("tushare",)))
                if not mainland:
                    add(location, 1, "tushare", "current_statements_require_mainland_equity")
                if not financial.get("financial_cache_only", False):
                    add(location, 1, "tushare", "financial_cache_only_required")
                dependencies.append(method + "_validated_current_cache_windows")
                continue
            category = next(name for name, details in TOOLS_CATEGORIES.items() if method in details["tools"])
            scoped_market = method_market if method not in ("get_global_news", "get_macro_indicators", "get_prediction_markets") else {}
            value = method_tools.get(method, scoped_market.get(category, method_data.get(category, "default")))
            chain = tokens(value, location, VENDOR_METHODS[method])
            check_chain(location, chain, VENDOR_METHODS[method], method)
        if analyst == "social":
            if mainland:
                chains.append(("social.local_sentiment", ("akshare",)))
            elif symbol is not None:
                # These prefetched sources bypass generic routing; they have no
                # reviewed budget counting adapter in this configuration gate.
                for provider in ("stocktwits", "reddit"):
                    add("social.prefetch", 0, provider, "non_mainland_sentiment_transport_unverified")

    return BudgetRouteReport(tuple(issues), tuple(chains), tuple(dict.fromkeys(dependencies)))


def validate_budget_routes(config, *, selected_analysts=tuple(ANALYST_TOOLS), symbol=None,
                           financial_config=None, budget_config=None):
    report = inspect_budget_routes(config, selected_analysts=selected_analysts, symbol=symbol,
                                   financial_config=financial_config, budget_config=budget_config)
    if report.issues:
        raise BudgetRouteConfigurationError(report)
    return report


def validate_budget_preflight(config, *, ticker, as_of_date,
                              selected_analysts=tuple(ANALYST_TOOLS),
                              financial_config=None, instrument_identity=None, budget_config=None):
    """Collect route and contextual failures together before any acquisition."""
    from dataclasses import replace

    from tradingagents.budget_readiness import (
        BudgetReadinessCheck,
        BudgetReadinessError,
        inspect_budget_readiness,
    )

    routes = inspect_budget_routes(config, selected_analysts=selected_analysts,
                                   symbol=ticker, financial_config=financial_config, budget_config=budget_config)
    readiness = inspect_budget_readiness(config, ticker=ticker, as_of_date=as_of_date,
                                         selected_analysts=selected_analysts,
                                         financial_config=financial_config,
                                         instrument_identity=instrument_identity)
    # Fixed prerequisites only: this gate does not read credentials or assert
    # authentication, entitlement, live coverage or service reachability.
    names = {"alpha_vantage": "ALPHA_VANTAGE_API_KEY", "fred": "FRED_API_KEY"}
    policy = config if budget_config is None else budget_config
    if policy.get("analysis_disable_alpha_vantage", False) is True:
        names.pop("alpha_vantage")
        readiness = replace(readiness, checked=readiness.checked + (
            BudgetReadinessCheck("provider.alpha_vantage", "user_disabled"),
        ))
    required = sorted({names[provider] for _, chain in routes.chains
                       for provider in chain if provider in names})
    readiness = replace(readiness, checked=readiness.checked + tuple(
        BudgetReadinessCheck("credential." + name, "presence_and_service_not_checked")
        for name in required
    ))
    if routes.issues:
        raise BudgetRouteConfigurationError(routes, readiness)
    if readiness.issues:
        raise BudgetReadinessError(readiness)
    return routes, readiness
