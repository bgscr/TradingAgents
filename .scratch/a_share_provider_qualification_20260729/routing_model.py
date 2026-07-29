"""PROTOTYPE ONLY: pure policy scoring for mainland provider qualification."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean


FINANCIAL_CAPABILITIES = {
    "annual_balance_sheet",
    "quarterly_balance_sheet",
    "annual_income_statement",
    "quarterly_income_statement",
    "annual_cash_flow",
    "quarterly_cash_flow",
    "financial_indicators_ratios",
}

FULL_STATEMENT_CAPABILITIES = {
    "annual_balance_sheet",
    "quarterly_balance_sheet",
    "annual_income_statement",
    "quarterly_income_statement",
    "annual_cash_flow",
    "quarterly_cash_flow",
}


POLICIES = {
    "A": {
        "daily_ohlcv": ["akshare-current", "baostock", "yahoo"],
        "raw_and_adjusted_prices": ["akshare-current", "baostock", "yahoo"],
        "adjustment_factor_history": ["tushare", "baostock", "akshare-current", "yahoo"],
        "suspension_trading_status": ["tushare", "baostock", "akshare-current", "yahoo"],
        "name_st_delisting_history": ["tushare", "akshare-current", "baostock", "yahoo"],
        "annual_balance_sheet": ["tushare", "baostock", "yahoo"],
        "quarterly_balance_sheet": ["tushare", "baostock", "yahoo"],
        "annual_income_statement": ["tushare", "baostock", "yahoo"],
        "quarterly_income_statement": ["tushare", "baostock", "yahoo"],
        "annual_cash_flow": ["tushare", "baostock", "yahoo"],
        "quarterly_cash_flow": ["tushare", "baostock", "yahoo"],
        "financial_indicators_ratios": ["tushare", "baostock", "yahoo"],
        "announcement_first_publication_dates": ["tushare", "baostock", "yahoo"],
        "restatement_update_identifiers": ["tushare", "yahoo", "baostock"],
        "units_currency_scope_company_type": ["tushare", "yahoo", "baostock"],
    },
    "B": {
        "daily_ohlcv": ["akshare-current", "baostock", "yahoo"],
        "raw_and_adjusted_prices": ["akshare-current", "baostock", "yahoo"],
        "adjustment_factor_history": ["baostock", "tushare", "yahoo", "akshare-current"],
        "suspension_trading_status": ["baostock", "tushare", "akshare-current", "yahoo"],
        "name_st_delisting_history": ["tushare", "akshare-current", "baostock", "yahoo"],
        "annual_balance_sheet": ["tushare", "yahoo", "baostock"],
        "quarterly_balance_sheet": ["tushare", "yahoo", "baostock"],
        "annual_income_statement": ["tushare", "yahoo", "baostock"],
        "quarterly_income_statement": ["tushare", "yahoo", "baostock"],
        "annual_cash_flow": ["tushare", "yahoo", "baostock"],
        "quarterly_cash_flow": ["tushare", "yahoo", "baostock"],
        "financial_indicators_ratios": ["tushare", "baostock", "yahoo", "akshare-current"],
        "announcement_first_publication_dates": ["tushare", "baostock", "yahoo"],
        "restatement_update_identifiers": ["tushare", "yahoo", "baostock"],
        "units_currency_scope_company_type": ["tushare", "yahoo", "baostock"],
    },
}


def _winner(records, providers, capability):
    by_provider = {item["provider"]: item for item in records}
    attempted = []
    for provider in providers:
        item = by_provider.get(provider)
        if item is None:
            continue
        attempted.append(item)
        usable = item["availability_runs"] > 0 and item["status"] in {"available", "partial"}
        if (
            usable
            and capability in FULL_STATEMENT_CAPABILITIES
            and item["typed_failure"] == "CAPABILITY_PARTIAL_METRICS_NOT_FULL_STATEMENT"
        ):
            usable = False
        if usable:
            return item, attempted
    return None, attempted


def score_policies(summary_records):
    """Score the two requested policies using only measured qualification records."""
    grouped = defaultdict(list)
    for item in summary_records:
        grouped[(item["normalized_symbol"], item["capability"])].append(item)

    outputs = {}
    for name, routes in POLICIES.items():
        winners = []
        request_ids = set()
        entitlement_failures = 0
        complexity_penalty = 0.0
        compatibility_penalty = 0.0
        maintenance_penalty = 0.0
        for (symbol, capability), records in grouped.items():
            winner, attempted = _winner(records, routes[capability], capability)
            for item in attempted:
                request_ids.update(item.get("call_ids", []))
                entitlement_failures += item["typed_failure"] == "PERMISSION_DENIED"
            if winner is None:
                continue
            winners.append(winner)
            provider = winner["provider"]
            complexity_penalty += {
                "tushare": 0.25,
                "baostock": 0.55,
                "yahoo": 0.45,
                "akshare-current": 0.50,
            }.get(provider, 0.6)
            compatibility_penalty += {
                "tushare": 0.30,
                "baostock": 0.20,
                "yahoo": 0.45,
                "akshare-current": 0.35,
            }.get(provider, 0.5)
            maintenance_penalty += {
                "tushare": 0.25,
                "baostock": 0.25,
                "yahoo": 0.50,
                "akshare-current": 0.55,
            }.get(provider, 0.5)

        financial = [item for item in winners if item["capability"] in FINANCIAL_CAPABILITIES]
        usable = sum(item["usable_non_sparse_periods"] for item in financial)
        target = sum(5 if item["capability"].startswith("annual_") else 8 for item in financial)
        coverage = min(100.0, 100.0 * usable / max(1, target))
        pit_winners = [item for item in winners if item["capability"] == "announcement_first_publication_dates"]
        announcement = 100.0 * mean([
            float(item["announcement_date_available"] and item["first_publication_date_available"])
            for item in pit_winners
        ] or [0.0])
        reliability = 100.0 * mean(
            [item["availability_runs"] / 2 * float(item["reproducible"]) for item in winners] or [0.0]
        )
        n = max(1, len(winners))
        normalization = max(0.0, 100.0 * (1.0 - complexity_penalty / n))
        compatibility = max(0.0, 100.0 * (1.0 - compatibility_penalty / n))
        maintenance = max(0.0, 100.0 * (1.0 - maintenance_penalty / n))
        measured_request_count = math.ceil(len(request_ids) / 2)
        request_efficiency = min(100.0, 100.0 * 60.0 / max(60, measured_request_count))
        entitlement = max(0.0, 100.0 - 5.0 * entitlement_failures)
        total = (
            0.25 * coverage
            + 0.15 * announcement
            + 0.20 * reliability
            + 0.10 * request_efficiency
            + 0.10 * entitlement
            + 0.08 * normalization
            + 0.07 * compatibility
            + 0.05 * maintenance
        )
        outputs[name] = {
            "total_score": round(total, 1),
            "usable_financial_period_coverage": round(coverage, 1),
            "point_in_time_announcement_quality": round(announcement, 1),
            "provider_reliability": round(reliability, 1),
            "request_efficiency": round(request_efficiency, 1),
            "entitlement_cost_score": round(entitlement, 1),
            "normalization_complexity_score": round(normalization, 1),
            "compatibility_risk_score": round(compatibility, 1),
            "maintenance_risk_score": round(maintenance, 1),
            "measured_request_count": measured_request_count,
            "selected": [
                {
                    "symbol": item["normalized_symbol"],
                    "capability": item["capability"],
                    "provider": item["provider"],
                    "status": item["status"],
                }
                for item in winners
            ],
        }
    return outputs
