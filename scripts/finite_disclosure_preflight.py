"""Read-only readiness projection. Never opens a client, coordinator or graph."""

from dataclasses import asdict


def inspect():
    from tradingagents.budget_preflight import inspect_budget_routes
    from tradingagents.budget_readiness import inspect_budget_readiness
    from tradingagents.dataflows import finite_disclosure_cache as cache
    from tradingagents.dataflows.config import preview_config
    from tradingagents.default_config import DEFAULT_CONFIG

    config = cache.select_profile(DEFAULT_CONFIG, cache.PROFILE)
    config = preview_config(config)
    financial = cache.financial_preflight(config, "601328.SH", "2026-10-16")
    routes = inspect_budget_routes(
        config, selected_analysts=("market", "social", "news", "fundamentals"), symbol="601328.SH"
    )
    readiness = inspect_budget_readiness(config, ticker="601328.SH", as_of_date="2026-10-16")
    return {
        "contract": "finite-readonly-startup-v2",
        "financial": financial,
        "routes": asdict(routes),
        "readiness": asdict(readiness),
        "full_analysis_started": False,
        "model_constructions": 0,
        "physical_requests": 0,
        "coordinator_path": config["provider_request_coordinator_path"],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(inspect(), sort_keys=True))
