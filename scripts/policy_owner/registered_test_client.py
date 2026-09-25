"""Resource-free real application producer for labelled TEST native fixtures."""

from __future__ import annotations

import copy
import os
from hashlib import sha256
from pathlib import Path


def build_test_blueprint(root: Path, instrument_registry: Path):
    # Application dependencies are supplied by a separate, pinned client
    # environment. This module is not part of the registered owner worker.
    import cli.main as cli_main
    from tradingagents.asset_configuration import resolve_run_asset_configuration
    from tradingagents.capability_routing import preflight_mainland_capability_routing
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.qualified_routing_composition import (
        RegisteredProductionQualifiedRoutingComposition,
    )

    if os.environ.get("TUSHARE_TOKEN") != "test-token":
        raise ValueError("client builder requires the explicit non-secret TEST token")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        mainland_capability_routing_mode="qualified_v1_shadow",
        mainland_capability_routing_plan_version="1.5",
        tushare_enabled_capabilities=(
            "statements",
            "financial_indicators",
            "adjustment_factors",
            "name_events",
        ),
        tushare_qualification_profile="cn-a-2000-20260729-v1",
        tushare_account_scope_label="personal-research-primary",
        tushare_calls_per_minute=40,
        tushare_operator_safety_ceiling_calls_per_minute=40,
        data_usage_mode="personal_research",
        data_cache_dir=str(root / "cache"),
        market_history_database_path=str(root / "history.sqlite3"),
        market_history_payload_root=str(root / "payloads"),
        market_history_backup_root=str(root / "backups"),
        results_dir=str(root / "results"),
        cninfo_listing_publication_profile="cninfo-listing-publication-candidate-v1",
        cninfo_source_policy_version="cninfo-source-policy-v1",
        cninfo_operator_minimum_interval_seconds=1,
        output_language="English",
        instrument_identity_registry_path=str(instrument_registry),
        instrument_identity_registry_sha256=sha256(instrument_registry.read_bytes()).hexdigest(),
    )
    # This existing static instrument key only selects the capability contract;
    # no symbol analysis, acquisition, database or provider is invoked.
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    result = preflight_mainland_capability_routing(asset, config=config)
    if not result.passed or result.plan is None:
        raise ValueError("TEST client configuration preflight failed: " + str(result.failure))
    config.update(
        asset_configuration_signature=asset.asset_configuration_signature,
        mainland_capability_routing_plan_signature=result.plan.plan_signature,
    )
    managed = cli_main.build_qualified_financial_routing_composition(
        plan=result.plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    if not isinstance(managed, RegisteredProductionQualifiedRoutingComposition):
        raise ValueError("CLI did not return the registered production blueprint")
    return managed.blueprint, config
