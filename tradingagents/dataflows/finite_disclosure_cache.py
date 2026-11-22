"""Explicit, read-only finite profile. No credentials, fallback, refresh or writer."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import finite_disclosure_admission as a

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE = "601328-2026H1-v2"
PROFILE_PATH = "configs/finite-disclosure/601328-2026H1-v2.json"
ACTIVE_PATH = "configs/finite-disclosure/active-601328-v2.json"
COMPAT_PATH = "tradingagents/dataflows/finite-disclosure-compatibility-v2.json"
ALIASES = ("601328.SH", "601328.SS")
OVERRIDES = {
    "instrument_identity_registry_path": str(
        PROJECT_ROOT / "config/instrument_identity_registry.json"
    ),
    "instrument_identity_registry_sha256": "066306201a136358fc96099141e7f92ee456fe84daab9257a00a8ca726e6b576",
    "analysis_budget_enabled": True,
    "analysis_max_calls": 30,
    "analysis_timeout_seconds": 600,
    "financial_cache_only": True,
    "tushare_financial_force_refresh": False,
    "tushare_financial_cache_max_age_seconds": 86400,
    "checkpoint_enabled": False,
    "tushare_account_scope_label": "personal-2000",
    "tushare_financial_https_endpoint": "https://api.waditu.com/dataapi",
    "llm_provider": "deepseek",
    "quick_think_llm": "deepseek-flash",
    "deep_think_llm": "deepseek-v4-pro",
    "market_history_mode": "current",
    "data_usage_mode": "personal_research",
    "mainland_capability_routing_mode": "legacy",
    "market_history_database_path": str(
        PROJECT_ROOT / "data/market_history/market_history.sqlite3"
    ),
    "market_history_payload_root": str(PROJECT_ROOT / "data/market_history/payloads"),
    "market_history_backup_root": str(PROJECT_ROOT / "data/market_history/backups"),
    "provider_request_coordinator_path": None,
}
STATEMENTS = ("get_balance_sheet", "get_income_statement", "get_cashflow")
PRODUCT_SOURCES = frozenset(
    (
        "tradingagents/dataflows/current_cache_qualification.py",
        "tests/test_current_cache_qualification_light.py",
        "cli/__init__.py",
        "cli/announcements.py",
        "cli/config.py",
        "cli/console_encoding.py",
        "cli/main.py",
        "cli/models.py",
        "cli/run_display.py",
        "cli/run_progress.py",
        "cli/runtime_artifacts.py",
        "cli/stats_handler.py",
        "cli/utils.py",
        "config/instrument_identity_registry.json",
        "config/instrument_identity_registry.sha256",
        "docs/adr/0040-finite-disclosure-cache-evidence.md",
        "docs/finite-disclosure-runtime.md",
        "scripts/accept_tushare_current.py",
        "scripts/finite_disclosure_preflight.py",
        "scripts/migrate_finite_disclosure_calendar.py",
        "scripts/publish_finite_disclosure.py",
        "scripts/run_current_cache_compatibility.py",
        "scripts/run_finite_disclosure_offline.py",
        "tests/test_finite_disclosure_admission.py",
        "tests/test_finite_disclosure_advisory.py",
        "tests/test_finite_disclosure_graph_flow.py",
        "tests/test_finite_disclosure_output.py",
        "tests/test_finite_disclosure_publication.py",
        "tests/test_finite_disclosure_release.py",
        "tests/test_finite_disclosure_routing.py",
        "tests/test_market_history_persistent_reuse.py",
        "tests/test_tushare_current_acceptance.py",
        "tests/test_tushare_current_incremental.py",
        "tests/test_tushare_pit_provider.py",
        "tradingagents/__init__.py",
        "tradingagents/agents/__init__.py",
        "tradingagents/agents/analysts/fundamentals_analyst.py",
        "tradingagents/agents/analysts/market_analyst.py",
        "tradingagents/agents/analysts/news_analyst.py",
        "tradingagents/agents/analysts/sentiment_analyst.py",
        "tradingagents/agents/analysts/social_media_analyst.py",
        "tradingagents/agents/analysts/submission.py",
        "tradingagents/agents/finite_disclosure_advisory.py",
        "tradingagents/agents/managers/direction_selector.py",
        "tradingagents/agents/managers/portfolio_manager.py",
        "tradingagents/agents/managers/research_manager.py",
        "tradingagents/agents/researchers/bear_researcher.py",
        "tradingagents/agents/researchers/bull_researcher.py",
        "tradingagents/agents/risk_mgmt/aggressive_debator.py",
        "tradingagents/agents/risk_mgmt/conservative_debator.py",
        "tradingagents/agents/risk_mgmt/neutral_debator.py",
        "tradingagents/agents/schemas.py",
        "tradingagents/agents/trader/trader.py",
        "tradingagents/agents/utils/agent_states.py",
        "tradingagents/agents/utils/agent_utils.py",
        "tradingagents/agents/utils/core_stock_tools.py",
        "tradingagents/agents/utils/fundamental_data_tools.py",
        "tradingagents/agents/utils/macro_data_tools.py",
        "tradingagents/agents/utils/market_data_validation_tools.py",
        "tradingagents/agents/utils/memory.py",
        "tradingagents/agents/utils/news_data_tools.py",
        "tradingagents/agents/utils/prediction_markets_tools.py",
        "tradingagents/agents/utils/rating.py",
        "tradingagents/agents/utils/structured.py",
        "tradingagents/agents/utils/technical_indicators_tools.py",
        "tradingagents/analysis_budget.py",
        "tradingagents/asset_configuration.py",
        "tradingagents/budget_data.py",
        "tradingagents/budget_preflight.py",
        "tradingagents/budget_readiness.py",
        "tradingagents/capability_routing.py",
        "tradingagents/cninfo_production_binding.py",
        "tradingagents/dataflows/__init__.py",
        "tradingagents/dataflows/_akshare_sina_statement_artifacts.py",
        "tradingagents/dataflows/_akshare_sina_statement_normalization.py",
        "tradingagents/dataflows/_tushare_indicator_normalization.py",
        "tradingagents/dataflows/_tushare_statement_artifacts.py",
        "tradingagents/dataflows/_tushare_statement_normalization.py",
        "tradingagents/dataflows/acquisition.py",
        "tradingagents/dataflows/akshare_data.py",
        "tradingagents/dataflows/akshare_sina_statements.py",
        "tradingagents/dataflows/alpha_vantage.py",
        "tradingagents/dataflows/alpha_vantage_common.py",
        "tradingagents/dataflows/alpha_vantage_fundamentals.py",
        "tradingagents/dataflows/alpha_vantage_indicator.py",
        "tradingagents/dataflows/alpha_vantage_news.py",
        "tradingagents/dataflows/alpha_vantage_stock.py",
        "tradingagents/dataflows/baostock_calendar.py",
        "tradingagents/dataflows/baostock_capabilities.py",
        "tradingagents/dataflows/baostock_data.py",
        "tradingagents/dataflows/baostock_output.py",
        "tradingagents/dataflows/baostock_ratios.py",
        "tradingagents/dataflows/china_a_enhancements.py",
        "tradingagents/dataflows/china_sentiment.py",
        "tradingagents/dataflows/cninfo_listing_publication.py",
        "tradingagents/dataflows/config.py",
        "tradingagents/dataflows/crypto_universe.py",
        "tradingagents/dataflows/current_statement_cache.py",
        "tradingagents/dataflows/current_tushare_statements.py",
        "tradingagents/dataflows/errors.py",
        "tradingagents/dataflows/factor_name_contracts.py",
        "tradingagents/dataflows/financial_capability_routing.py",
        "tradingagents/dataflows/financial_contracts.py",
        "tradingagents/dataflows/financial_dispatch.py",
        "tradingagents/dataflows/financial_eligibility_acquisition.py",
        "tradingagents/dataflows/financial_eligibility_metadata.py",
        "tradingagents/dataflows/financial_eligibility_policy.py",
        "tradingagents/dataflows/financial_indicator_metadata.py",
        "tradingagents/dataflows/financial_period_targets.py",
        "tradingagents/dataflows/financial_request_factory.py",
        "tradingagents/dataflows/financial_temporal_eligibility.py",
        "tradingagents/dataflows/financial_temporal_proof_projection.py",
        "tradingagents/dataflows/finite_disclosure_admission.py",
        "tradingagents/dataflows/finite_disclosure_cache.py",
        "tradingagents/dataflows/finite_disclosure_contracts.py",
        "tradingagents/dataflows/finite_disclosure_output.py",
        "tradingagents/dataflows/finite_disclosure_tools.py",
        "tradingagents/dataflows/fred.py",
        "tradingagents/dataflows/identity_registry_refresh.py",
        "tradingagents/dataflows/instrument_identity.py",
        "tradingagents/dataflows/interface.py",
        "tradingagents/dataflows/listing_publication_evidence.py",
        "tradingagents/dataflows/market_data_validator.py",
        "tradingagents/dataflows/market_snapshot.py",
        "tradingagents/dataflows/monetary_facts.py",
        "tradingagents/dataflows/polymarket.py",
        "tradingagents/dataflows/provider_subrequests.py",
        "tradingagents/dataflows/qualified_production_sources.py",
        "tradingagents/dataflows/reddit.py",
        "tradingagents/dataflows/stockstats_utils.py",
        "tradingagents/dataflows/stocktwits.py",
        "tradingagents/dataflows/symbol_utils.py",
        "tradingagents/dataflows/tushare_factors_names.py",
        "tradingagents/dataflows/tushare_indicators.py",
        "tradingagents/dataflows/tushare_market_history.py",
        "tradingagents/dataflows/tushare_statements.py",
        "tradingagents/dataflows/utils.py",
        "tradingagents/dataflows/y_finance.py",
        "tradingagents/dataflows/yfinance_news.py",
        "tradingagents/decision_audit.py",
        "tradingagents/decision_policy.py",
        "tradingagents/default_config.py",
        "tradingagents/evidence.py",
        "tradingagents/evidence_artifacts.py",
        "tradingagents/evidence_diagnostics.py",
        "tradingagents/finite_disclosure_runtime.py",
        "tradingagents/graph/__init__.py",
        "tradingagents/graph/analyst_execution.py",
        "tradingagents/graph/checkpointer.py",
        "tradingagents/graph/conditional_logic.py",
        "tradingagents/graph/evidence_gate.py",
        "tradingagents/graph/financial_tools.py",
        "tradingagents/graph/propagation.py",
        "tradingagents/graph/reflection.py",
        "tradingagents/graph/setup.py",
        "tradingagents/graph/signal_processing.py",
        "tradingagents/graph/trading_graph.py",
        "tradingagents/llm_clients/__init__.py",
        "tradingagents/llm_clients/anthropic_client.py",
        "tradingagents/llm_clients/api_key_env.py",
        "tradingagents/llm_clients/azure_client.py",
        "tradingagents/llm_clients/base_client.py",
        "tradingagents/llm_clients/bedrock_client.py",
        "tradingagents/llm_clients/capabilities.py",
        "tradingagents/llm_clients/factory.py",
        "tradingagents/llm_clients/google_client.py",
        "tradingagents/llm_clients/model_catalog.py",
        "tradingagents/llm_clients/openai_client.py",
        "tradingagents/llm_clients/validators.py",
        "tradingagents/market_history/__init__.py",
        "tradingagents/market_history/bootstrap.py",
        "tradingagents/market_history/bundle.py",
        "tradingagents/market_history/calendar.py",
        "tradingagents/market_history/calendar_discovery.py",
        "tradingagents/market_history/config.py",
        "tradingagents/market_history/coordinator.py",
        "tradingagents/market_history/current.py",
        "tradingagents/market_history/current_cache.py",
        "tradingagents/market_history/current_incremental.py",
        "tradingagents/market_history/equivalence.py",
        "tradingagents/market_history/frames.py",
        "tradingagents/market_history/maintenance.py",
        "tradingagents/market_history/models.py",
        "tradingagents/market_history/result_codec.py",
        "tradingagents/market_history/revisions.py",
        "tradingagents/market_history/schema.py",
        "tradingagents/market_history/shadow.py",
        "tradingagents/market_history/snapshot_identity.py",
        "tradingagents/market_history/store.py",
        "tradingagents/market_history/suspension.py",
        "tradingagents/news_diagnostic.py",
        "tradingagents/offline_runtime_bundle.py",
        "tradingagents/picker/__init__.py",
        "tradingagents/picker/cache.py",
        "tradingagents/picker/cli.py",
        "tradingagents/picker/errors.py",
        "tradingagents/picker/ingestion.py",
        "tradingagents/picker/normalize.py",
        "tradingagents/picker/pit_config.py",
        "tradingagents/picker/pit_dates.py",
        "tradingagents/picker/pit_models.py",
        "tradingagents/picker/rate_limit.py",
        "tradingagents/picker/snapshot.py",
        "tradingagents/picker/tushare_provider.py",
        "tradingagents/policy_coverage_contract.py",
        "tradingagents/policy_coverage_models.py",
        "tradingagents/policy_coverage_native.py",
        "tradingagents/policy_coverage_schema.py",
        "tradingagents/policy_coverage_verification.py",
        "tradingagents/policy_evidence_acquisition.py",
        "tradingagents/policy_evidence_client.py",
        "tradingagents/policy_evidence_contract.py",
        "tradingagents/policy_evidence_limits.py",
        "tradingagents/policy_owner_admission_crypto.py",
        "tradingagents/policy_owner_admission_migration.py",
        "tradingagents/policy_owner_admission_v2.py",
        "tradingagents/policy_owner_admission_windows.py",
        "tradingagents/policy_owner_contract.py",
        "tradingagents/policy_qualification_lifecycle.py",
        "tradingagents/policy_qualified_contract.py",
        "tradingagents/policy_qualified_native.py",
        "tradingagents/policy_qualified_results.py",
        "tradingagents/qualified_routing_composition.py",
        "tradingagents/qualified_routing_eligibility_lifecycle.py",
        "tradingagents/qualified_routing_lifecycle.py",
        "tradingagents/qualified_routing_provider_clients.py",
        "tradingagents/recorded_replay.py",
        "tradingagents/report_exports.py",
        "tradingagents/report_presentation.py",
        "tradingagents/report_storage.py",
        "tradingagents/reporting.py",
        "tradingagents/run_telemetry.py",
        "tradingagents/runtime_protection.py",
        "tradingagents/runtime_protection_handoff.py",
        "tradingagents/statement_acceptance.py",
        "tradingagents/strategy_registry.py",
        "tradingagents/terminal_contract.py",
        "tradingagents/tushare_acceptance.py",
        "tradingagents/tushare_financial_https.py",
        "tradingagents/tushare_https.py",
        "tradingagents/windows_policy_owner.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
    )
)


def enabled(config):
    return config.get("finite_disclosure_profile") is not None


def _read(relative):
    path = PROJECT_ROOT / relative
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise a.Rejected("finite_path_alias")
    try:
        with path.open("rb") as handle:
            raw = handle.read(a.MAX_BYTES + 1)
        return raw, a.decode(raw)
    except OSError:
        raise a.Rejected("finite_binding_missing") from None


def installed_profile():
    require_current_release_authority()
    return _read_installed_profile()


def require_current_release_authority():
    # The archived owner approval is not a transferable production grant.
    raise a.Rejected("finite_release_authority_unconfigured")


def _read_installed_profile():
    _, active = _read(ACTIVE_PATH)
    profile_raw, profile = _read(PROFILE_PATH)
    compat_raw, compat = _read(COMPAT_PATH)
    if (
        set(active) != {"contract", "enabled", "profile_sha256", "compatibility_sha256"}
        or active["contract"] != "finite-disclosure-activation-v1"
        or active["enabled"] is not True
        or active["profile_sha256"] != a.digest(profile_raw)
        or active["compatibility_sha256"] != a.digest(compat_raw)
    ):
        raise a.Rejected("finite_activation_binding")
    if (
        set(profile)
        != {
            "contract",
            "profile",
            "scope",
            "namespace",
            "cache_relative_path",
            "admission_config",
            "runtime_overrides",
            "limits",
            "user_authorization",
            "release_sha256",
        }
        or profile["contract"] != "finite-disclosure-profile-v2"
        or profile["profile"] != PROFILE
        or not a.equal(profile["scope"], a.SCOPE)
        or profile["namespace"] != a.namespace()
        or profile["cache_relative_path"]
        != "data/finite-financial-disclosures/" + a.namespace() + "/finite-view.json"
        or not a.equal(profile["runtime_overrides"], OVERRIDES)
        or not a.equal(profile["limits"], a.LIMITS)
        or profile["user_authorization"] != "historical-reference-withheld"
    ):
        raise a.Rejected("finite_profile_scope")
    a.validate_config(profile["admission_config"])
    if (
        compat.get("contract") != "finite-disclosure-compatibility-v2"
        or compat.get("profile_sha256") != a.digest(profile_raw)
        or compat.get("view_sha256") != profile["admission_config"]["view_sha256"]
        or compat.get("scope_sha256") != a.digest(a.encode(a.SCOPE))
    ):
        raise a.Rejected("finite_compatibility_binding")
    current_raw, current = _read("tradingagents/market_history/current_cache_compatibility.json")
    if (
        a.digest(current_raw) != a.FIXED_CONFIG["CURRENT_manifest_sha256"]
        or compat.get("original_CURRENT_sha256") != a.digest(current_raw)
        or len(current["sources"]) != 38
        or not a.equal(compat.get("original_CURRENT_sources"), current["sources"])
    ):
        raise a.Rejected("frozen_CURRENT_mismatch")
    # New qualification is independent; old manifest remains retained provenance only.
    release_raw, release = _read(
        "releases/finite-601328-v2/" + profile["release_sha256"] + "/manifest.json"
    )
    if (
        a.digest(release_raw) != profile["release_sha256"]
        or release.get("contract") != "finite-runtime-release-v2"
    ):
        raise a.Rejected("finite_release_binding")
    sources = release.get("sources")
    if not isinstance(sources, dict) or set(sources) != PRODUCT_SOURCES:
        raise a.Rejected("finite_source_inventory")
    for rel, sha in sources.items():
        if (
            rel.startswith(("/", "\\"))
            or ".." in Path(rel).parts
            or not rel.endswith((".py", ".md", ".json", ".sha256"))
        ):
            raise a.Rejected("finite_source_path")
        if a.digest((PROJECT_ROOT / rel).read_bytes()) != sha:
            raise a.Rejected("finite_product_source_mismatch")
    qualified_raw, qualified = _read(
        "tradingagents/market_history/current_cache_compatibility_finite_v2.json"
    )
    if release.get("CURRENT_sha256") != a.digest(qualified_raw):
        raise a.Rejected("finite_current_release_binding")
    from tradingagents.dataflows.current_cache_qualification import compatibility_diagnostic

    if compatibility_diagnostic(
        manifest_path=PROJECT_ROOT
        / "tradingagents/market_history/current_cache_compatibility_finite_v2.json",
        source_root=PROJECT_ROOT,
    ):
        raise a.Rejected("finite_current_qualification_failed")
    return profile


def select_profile(config, profile):
    if profile != PROFILE:
        raise a.Rejected("unknown_finite_profile")
    installed = installed_profile()
    result = deepcopy(config)
    result.update(installed["runtime_overrides"])
    result["finite_disclosure_profile"] = PROFILE
    result["tool_vendors"] = {
        **result.get("tool_vendors", {}),
        **dict.fromkeys(STATEMENTS, "tushare"),
    }
    return result


def validate_runtime(config, *, symbol=None, selected_analysts=None):
    if config.get("finite_disclosure_profile") != PROFILE:
        raise a.Rejected("finite_profile_not_explicit")
    if symbol is not None and symbol not in ALIASES:
        raise a.Rejected("finite_symbol_mismatch")
    if selected_analysts is not None and tuple(selected_analysts) != (
        "market",
        "social",
        "news",
        "fundamentals",
    ):
        raise a.Rejected("finite_four_analysts_required")
    if any(not a.equal(config.get(k), v) for k, v in OVERRIDES.items()):
        raise a.Rejected("finite_runtime_override_conflict")
    if any(config.get("tool_vendors", {}).get(k) != "tushare" for k in STATEMENTS):
        raise a.Rejected("finite_statement_route_conflict")
    return installed_profile()


def load_view(config, symbol, cutoff, *, now=None):
    if symbol not in ALIASES or cutoff != "2026-10-16":
        raise a.Rejected("finite_instrument_or_date")
    profile = validate_runtime(config, symbol=symbol)
    root = (PROJECT_ROOT / profile["cache_relative_path"]).parent
    result, view = a.read_candidate(
        root, profile["admission_config"], now or datetime.now(timezone.utc), approved_root=root
    )
    if view is None or not result["financial_runtime_fresh"]:
        raise a.Rejected("finite_cache_missing_stale_or_invalid")
    return profile, result, view


def financial_preflight(config, symbol, cutoff, *, now=None):
    return load_view(config, symbol, cutoff, now=now)[1]
