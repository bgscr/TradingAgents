"""Ordinary four-analyst research with the project's budget and evidence gates."""

import json
import os
import subprocess
import time
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ANALYSTS = ("market", "social", "news", "fundamentals")


@contextmanager
def local_payloads():
    from scripts.personal_full import local_path
    from tradingagents.market_history.store import MarketHistoryStore

    original = MarketHistoryStore._checked_path

    def checked(root, relative_path):
        if relative_path.anchor or ".." in relative_path.parts:
            raise ValueError("invalid_payload_path")
        local_path(root / relative_path)
        return original(root, relative_path)

    with patch.object(MarketHistoryStore, "_checked_path", staticmethod(checked)):
        yield


def require_existing_ceiling(
    coordinator, *, upstream_service_id, minimum_interval, allow_prewarming, **_
):
    ceiling = coordinator.configured_operator_ceiling(upstream_service_id)
    if (
        ceiling is None
        or ceiling.allow_prewarming
        or allow_prewarming
        or ceiling.minimum_interval < minimum_interval
    ):
        raise ValueError("personal_account_policy_required")
    return ceiling


def check_account(config):
    import sqlite3

    from tradingagents.budget_readiness import _ReadFailure, _readonly_history
    from tradingagents.market_history.config import (
        MarketHistoryConfig,
        provider_request_authority_config,
    )
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )

    authority = provider_request_authority_config(MarketHistoryConfig.from_mapping(config))
    rates = (
        config["tushare_calls_per_minute"],
        config["tushare_operator_safety_ceiling_calls_per_minute"],
    )
    if any(type(rate) is not int or not 1 <= rate <= 40 for rate in rates):
        raise ValueError("personal_account_rate_invalid")
    try:
        with _readonly_history(
            dict(
                config,
                market_history_database_path=str(authority.database_path),
                market_history_payload_root=str(authority.payload_root),
                market_history_backup_root=str(authority.backup_root),
            )
        ) as store:
            require_existing_ceiling(
                ProviderRequestCoordinator(store),
                upstream_service_id=upstream_service_identity_for_provider("tushare")[0],
                minimum_interval=timedelta(seconds=60 / min(rates)),
                allow_prewarming=False,
            )
    except (_ReadFailure, OSError, sqlite3.Error) as exc:
        raise ValueError("personal_account_policy_required") from exc


def configured(config, ticker):
    """Validate a warm local research configuration before constructing any client."""
    from tradingagents.analysis_budget import coerce_output_token_limit, validate_budget_config
    from tradingagents.budget_preflight import apply_mainland_budget_sources

    if config.get("llm_provider") != "deepseek" or config.get("backend_url") not in {
        None,
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    }:
        raise ValueError("deepseek_configuration_required")
    if not all(
        isinstance(config.get(name), str) and config[name].startswith("deepseek-")
        for name in ("quick_think_llm", "deep_think_llm")
    ):
        raise ValueError("deepseek_model_selection_required")
    if config.get("market_data_vendors", {}).get("cn_a", {}).get("core_stock_apis") != "tushare":
        raise ValueError("tushare_configuration_required")
    if any(
        config.get("tool_vendors", {}).get(tool) != "tushare"
        for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow")
    ):
        raise ValueError("statement_configuration_required")
    enabled = config.get("analysis_allowed_data_vendors")
    if enabled is not None and "tushare" not in enabled:
        raise ValueError("tushare_configuration_required")
    if (
        config.get("tushare_financial_force_refresh")
        or config.get("market_history_current_force_refresh")
        or any(config.get(name) for name in ("bounded_moneyflow_enabled", "bounded_margin_date"))
    ):
        raise ValueError("incompatible_research_settings")
    calls = config.get("analysis_max_calls", 60)
    seconds = config.get("analysis_timeout_seconds", 1200)
    if type(calls) is not int or calls <= 0 or type(seconds) is not int or seconds <= 0:
        raise ValueError("invalid_session_limits")
    config = dict(
        config,
        analysis_budget_enabled=True,
        analysis_max_calls=min(60, calls),
        analysis_timeout_seconds=min(1200, seconds),
        financial_cache_only=True,
        analysis_skip_optional_fundamentals=True,
        checkpoint_enabled=False,
        memory_log_path=None,
        output_language="Chinese",
        evidence_gate_mode="enforce",
        llm_max_retries=0,
        china_a_enhancement_preset="basic",
        llm_max_output_tokens=min(
            4096, coerce_output_token_limit(config.get("llm_max_output_tokens")) or 4096
        ),
        backend_url="https://api.deepseek.com/v1",
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
    )
    config = apply_mainland_budget_sources(config, ticker=ticker, include_macro_sources=False)
    validate_budget_config(config)
    return config


def data_scope():
    return {
        "evidence_preset": "basic",
        "full_data_acceptance": "unverified",
        "enhancements": {
            "news": "unverified",
            "margin_balance": "not_enabled",
            "eastmoney_fundflow_ratio": "not_enabled",
        },
    }


def rating_status(terminal):
    from tradingagents.terminal_contract import TerminalOutcomeKind

    return {
        TerminalOutcomeKind.TRADING_DECISION: "validated_decision",
        TerminalOutcomeKind.ANALYSIS_OUTCOME: "no_rating",
        TerminalOutcomeKind.OPERATIONAL_FAILURE: "unavailable",
    }[terminal.terminal_outcome_kind]


def prepare(config, ticker, day):
    from tradingagents.budget_preflight import validate_budget_preflight

    config = configured(config, ticker)
    with local_payloads():
        _, readiness = validate_budget_preflight(
            config, ticker=ticker, as_of_date=day, selected_analysts=ANALYSTS
        )
        config["personal_verified_inputs"] = {
            item.location: item.status
            for item in readiness.checked
            if item.location == "calendar" or item.location.startswith("financial.")
        }
        check_account(config)
    return config


@contextmanager
def personal_sources(ticker=None, day=None):
    """Run only in the dedicated CLI. Public scrapers and external helpers are unavailable."""
    from requests import RequestException, Session

    def unavailable(*args, **kwargs):
        raise RequestException("personal_optional_source_disabled")

    def no_children(*args, **kwargs):
        raise PermissionError("personal_external_helper_disabled")

    def no_scrape(*args, **kwargs):
        return "资料未采集：个人研究没有启用此来源。"

    def no_enhancement(*args, **kwargs):
        return ""

    from tradingagents import tushare_https
    from tradingagents.market_history.coordinator import ProviderRequestCoordinator

    send_market = tushare_https.send_once

    def scoped_market(url, payload, timeout, **kwargs):
        from datetime import date, datetime

        from dateutil.relativedelta import relativedelta

        from tradingagents.picker.pit_models import Dataset
        from tradingagents.picker.tushare_provider import ENDPOINTS

        tushare_https.validate_request(url, payload, timeout)
        endpoint = payload["api_name"]
        params = payload["params"]
        fields = {
            "trade_cal": ENDPOINTS[Dataset.TRADE_CAL].fields,
            "daily": ENDPOINTS[Dataset.DAILY].fields,
            "adj_factor": "ts_code,trade_date,adj_factor",
            "suspend_d": "ts_code,trade_date,suspend_timing,suspend_type",
        }
        bad = ValueError("personal_graph_request_scope_mismatch")
        if (
            not ticker
            or not day
            or kwargs
            or endpoint not in fields
            or fields[endpoint] != payload["fields"]
        ):
            raise bad
        owner_field = "exchange" if endpoint == "trade_cal" else "ts_code"
        owner = (
            ("SSE" if ticker.endswith(".SS") else "SZSE")
            if endpoint == "trade_cal"
            else ticker.replace(".SS", ".SH")
        )
        expected = {owner_field, "ts_type_name", "limit", "offset", "end_date"}
        if endpoint != "adj_factor" or "start_date" in params:
            expected.add("start_date")
        if (
            set(params) != expected
            or params[owner_field] != owner
            or type(params["limit"]) is not int
            or not 1 <= params["limit"] <= 5000
            or type(params["offset"]) is not int
            or params["offset"] < 0
        ):
            raise bad
        selected = date.fromisoformat(day)
        end = datetime.strptime(params["end_date"], "%Y%m%d").date()
        if end > selected or end.strftime("%Y%m%d") != params["end_date"]:
            raise bad
        if "start_date" in params:
            start = datetime.strptime(params["start_date"], "%Y%m%d").date()
            if (
                not selected - relativedelta(years=5) <= start <= end
                or start.strftime("%Y%m%d") != params["start_date"]
            ):
                raise bad
        return send_market(url, payload, timeout)

    with ExitStack() as stack:
        stack.enter_context(patch.object(tushare_https, "send_once", scoped_market))
        stack.enter_context(patch.object(Session, "send", unavailable))
        stack.enter_context(patch.object(subprocess, "Popen", no_children))
        stack.enter_context(
            patch.object(
                ProviderRequestCoordinator, "configure_operator_ceiling", require_existing_ceiling
            )
        )
        from tradingagents.agents.analysts import sentiment_analyst
        from tradingagents.dataflows import interface

        stack.enter_context(
            patch.object(sentiment_analyst, "get_china_a_local_sentiment", no_scrape)
        )
        stack.enter_context(
            patch.object(
                sentiment_analyst, "get_china_a_enhancements_for_categories", no_enhancement
            )
        )
        stack.enter_context(
            patch.object(interface, "get_china_a_enhancements_for_categories", no_enhancement)
        )
        yield


def run(config, ticker, day, output_root, started, *, preparation=None):
    from tradingagents.analysis_budget import AnalysisBudgetStopped, analysis_preparation_scope
    from tradingagents.terminal_contract import TerminalContract

    run_id = uuid4().hex
    output = Path(output_root) / run_id
    config = dict(
        config,
        results_dir=str(output),
        analysis_session_deadline_epoch=started[0] + config["analysis_timeout_seconds"],
        analysis_session_monotonic_deadline=started[1] + config["analysis_timeout_seconds"],
    )
    graph = None
    receipt = {
        "mode": "personal_research",
        "run_id": run_id,
        "ticker": ticker,
        "exchange_session": day,
        "status": "failed",
        "report_written": False,
        "rating_status": "unavailable",
        "verified_inputs": dict(
            config["personal_verified_inputs"], stock_identity="verified", stock_close="verified"
        ),
        **data_scope(),
    }
    output.mkdir(parents=True, exist_ok=False)
    try:
        with (
            open(os.devnull, "w", encoding="utf-8") as quiet,
            redirect_stdout(quiet),
            redirect_stderr(quiet),
            local_payloads(),
            analysis_preparation_scope(config),
            personal_sources(ticker, day),
        ):
            if (
                time.time() >= config["analysis_session_deadline_epoch"]
                or time.monotonic() >= config["analysis_session_monotonic_deadline"]
            ):
                raise AnalysisBudgetStopped("deadline_exceeded")
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph = TradingAgentsGraph(
                selected_analysts=ANALYSTS, config=config, instrument_symbol=ticker
            )
            graph.graph = graph.graph.with_config(max_concurrency=1)
            state, _ = graph.propagate(ticker, day)
            terminal = TerminalContract.model_validate(state["terminal_contract"])
            report = graph.save_reports(state, ticker, output / "report")
            receipt.update(
                status=terminal.lifecycle_status.value,
                outcome=terminal.terminal_outcome_kind.value,
                rating_status=rating_status(terminal),
                decision_coverage=terminal.coverage.model_dump(mode="json"),
                report_written=report.is_file(),
                report_relative_path=report.relative_to(output).as_posix(),
            )
    except AnalysisBudgetStopped:
        receipt["error"] = "research_budget_or_transport_stopped"
    except Exception:
        receipt["error"] = "research_or_publication_failed"
    budget = getattr(graph, "last_analysis_budget", None)
    if budget is not None:
        receipt.update(
            {key: budget[key] for key in ("admitted_attempts", "model_attempts", "data_attempts")}
        )
    if preparation is not None:
        receipt["preparation_attempts"] = dict(preparation.counts)
        receipt["financial_attempts"] = sum(
            preparation.counts.get(e, 0) for e in ("balancesheet", "income", "cashflow")
        )
        for key in ("data_attempts", "admitted_attempts"):
            if key in receipt:
                receipt[key] += sum(preparation.counts.values())
    with (output / "status.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False)
    return receipt
