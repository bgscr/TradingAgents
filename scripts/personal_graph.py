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

# Only reviewed, fixed codes from the personal graph's budget and data guards
# may enter the receipt. New or unexpected reasons use the same safe fallback.
_PERSONAL_STOP_REASONS = frozenset(
    {
        "call_limit_exceeded",
        "concurrent_analysis_unsupported",
        "data_redirect_forbidden",
        "deadline_exceeded",
        "disabled_data_vendor",
        "financial_cache_changed",
        "financial_cache_missing",
        "financial_cache_refresh_conflict",
        "financial_cache_stale",
        "financial_cache_unreadable",
        "inactive_transport",
        "model_output_limit_unbound",
        "model_redirect_forbidden",
        "optional_sources_require_budget",
        "preparation_network_forbidden",
        "source_policy_changed",
        "uncovered_data_credentials",
        "uncovered_data_destination",
        "uncovered_data_transport_settings",
        "uncovered_data_vendor",
        "uncovered_httpx_transport",
        "uncovered_model_destination",
        "uncovered_model_transport",
        "uncovered_model_transport_settings",
        "uncovered_native_transport",
        "uncovered_requests_adapter",
        "uncovered_scheduler_transport",
        "uncovered_socket_transport",
        "uncovered_transport",
        "uncovered_tushare_endpoint",
    }
)


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
        personal_public_news_allowed=(
            config.get("personal_public_news_allowed") is not False
            and (enabled is None or "akshare" in enabled)
        ),
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


def data_scope(config):
    from scripts.personal_observations import scope

    return scope(config)


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
def personal_sources(ticker=None, day=None, news=None):
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

        if news is not None:
            reader = news.graph_metadata
            stack.enter_context(patch.object(interface, "get_akshare_news", reader))
            stack.enter_context(
                patch.dict(interface.VENDOR_METHODS["get_news"], {"akshare": reader})
            )

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
    from tradingagents.run_diagnostics import personal_analyst_output_scope
    from tradingagents.terminal_contract import TerminalContract

    run_id = uuid4().hex
    output = Path(output_root) / run_id
    config = dict(
        config,
        results_dir=str(output),
        analysis_session_deadline_epoch=started[0] + config["analysis_timeout_seconds"],
        analysis_session_monotonic_deadline=started[1] + config["analysis_timeout_seconds"],
    )
    from scripts.personal_observations import PersonalNews, snapshot_binding, supplement

    graph = state = binding = synthesis = None
    personal_full = config.get("personal_mode") == "full"
    news = None
    if preparation is not None and "news" in config.get("personal_sources", ()):
        news = PersonalNews(ticker, day, preparation, True, config["personal_public_news_allowed"])
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
        **data_scope(config),
    }
    output.mkdir(parents=True, exist_ok=False)
    stop_stage = "graph_setup"

    def publish():
        terminal = TerminalContract.model_validate(state["terminal_contract"])
        report_state = state
        if personal_full:
            from tradingagents.personal_research_conclusion import PersonalResearchConclusion

            report_state = dict(state, personal_report_sources=tuple(config.get("personal_sources", ())))
            if synthesis is not None:
                conclusion = synthesis if synthesis.matches(state) else PersonalResearchConclusion.for_state(
                    state, reason="invalid_reference",
                )
                report_state["personal_research_conclusion"] = conclusion
                receipt["research_synthesis_status"] = conclusion.status
                if conclusion.reason is not None:
                    receipt["research_synthesis_reason"] = conclusion.reason
        report = graph.save_reports(report_state, ticker, output / "report")
        receipt.update(
            status=terminal.lifecycle_status.value if "error" not in receipt else "failed",
            outcome=terminal.terminal_outcome_kind.value,
            rating_status=rating_status(terminal),
            decision_coverage=terminal.coverage.model_dump(mode="json"),
            report_written=report.is_file(),
            report_relative_path=report.relative_to(output).as_posix(),
        )

    try:
        with (
            open(os.devnull, "w", encoding="utf-8") as quiet,
            redirect_stdout(quiet),
            redirect_stderr(quiet),
            local_payloads(),
            analysis_preparation_scope(config),
            personal_sources(ticker, day, news),
            personal_analyst_output_scope() as analyst_output,
        ):
            receipt["analyst_output_diagnostics"] = analyst_output
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
            capture_snapshot = preparation is not None and bool({
                "margin_balance",
                "eastmoney_fundflow_ratio",
            }.intersection(config.get("personal_sources", ())))
            if capture_snapshot or personal_full:
                run_graph = graph._run_graph

                def run_with_binding(*args, **kwargs):
                    nonlocal binding, state, stop_stage, synthesis
                    value = run_graph(*args, **kwargs)
                    completed_state = value[0]
                    if capture_snapshot:
                        # Verify against the live snapshot before propagate leaves its run scope.
                        binding = snapshot_binding(completed_state, ticker, day)
                    if personal_full and not completed_state.get("portfolio_research_terminal"):
                        from tradingagents.personal_research_conclusion import (
                            synthesize_personal_research,
                        )

                        # The rule decision and its audit are already complete. This output
                        # travels separately and shares the active session's transport budget.
                        state = completed_state
                        stop_stage = "research_synthesis"
                        synthesis = synthesize_personal_research(completed_state, graph.deep_thinking_llm)
                    return value

                graph._run_graph = run_with_binding
            stop_stage = "graph_execution"
            from tradingagents.agents.analysts.submission import personal_full_prompt_scope

            with personal_full_prompt_scope(personal_full):
                state, _ = graph.propagate(ticker, day)
            stop_stage = "report_publication"
            publish()
    except AnalysisBudgetStopped as exc:
        reason = exc.reason
        receipt.update(
            error="research_budget_or_transport_stopped",
            stop_reason=reason
            if type(reason) is str and reason in _PERSONAL_STOP_REASONS
            else "unknown",
            stop_stage=stop_stage,
        )
        if stop_stage == "research_synthesis" and state is not None:
            from tradingagents.personal_research_conclusion import PersonalResearchConclusion

            synthesis = PersonalResearchConclusion.for_state(state, reason="session_stopped")
            try:
                with local_payloads():
                    publish()
            except Exception:
                # Keep the original transport/budget stop and the pre-existing audit.
                receipt["report_written"] = False
    except Exception:
        receipt["error"] = "research_or_publication_failed"
    budget = getattr(graph, "last_analysis_budget", None)
    if budget is not None:
        receipt.update(
            {key: budget[key] for key in ("admitted_attempts", "model_attempts", "data_attempts")}
        )
    status_path = output / "status.json"
    if news and news.result is not None:
        receipt["enhancements"]["news"] = news.result["status"]
        receipt["source_evidence"] = {
            "news": dict(news.result, returned_to_news_tool=news.returned_to_news_tool)
        }
    if receipt["report_written"] and "error" not in receipt and config.get("personal_sources"):
        # A supervisor timeout in optional collection must leave the sealed core result discoverable.
        preliminary = dict(receipt, supplement_status="pending", usage_final=False)
        if preparation is not None:
            preliminary["preparation_attempts"] = dict(preparation.counts)
            for key in ("admitted_attempts", "data_attempts"):
                if key in preliminary:
                    preliminary[key] += sum(preparation.counts.values())
            preliminary["financial_attempts"] = sum(
                preparation.counts.get(k, 0) for k in ("balancesheet", "income", "cashflow")
            )
        with status_path.open("x", encoding="utf-8") as stream:
            json.dump(preliminary, stream, ensure_ascii=False)
        if (
            preparation is None
            or budget is None
            or type(receipt.get("admitted_attempts")) is not int
        ):
            for key in config["personal_sources"]:
                receipt["enhancements"][key] = "isolated_session_unavailable"
        else:
            prep_counts = dict(preparation.counts)
            try:
                supplement(
                    config,
                    binding,
                    ticker,
                    day,
                    receipt,
                    preparation,
                    news,
                    os.environ.get("TUSHARE_TOKEN", ""),
                )
            except Exception:
                # The report and terminal were published before the optional phase.
                receipt["supplement_error"] = "source_evidence_unavailable"
            receipt["supplement_attempts"] = {
                k: v for k, v in preparation.counts.items() if k not in prep_counts
            }
    if preparation is not None:
        receipt["preparation_attempts"] = {
            k: v
            for k, v in preparation.counts.items()
            if k not in ("margin_detail", "moneyflow_dc", "public_news")
        }
        receipt["financial_attempts"] = preparation.financial_attempts()
        for key in ("data_attempts", "admitted_attempts"):
            if key in receipt:
                receipt[key] += sum(preparation.counts.values())
    from scripts.personal_bootstrap import write_local

    receipt["enhancement_coverage"] = (
        "all_three_verified"
        if all(v == "verified" for v in receipt["enhancements"].values())
        else "not_selected"
        if all(v == "not_selected" for v in receipt["enhancements"].values())
        else "incomplete"
    )
    receipt["usage_final"] = budget is not None
    write_local(status_path, json.dumps(receipt, ensure_ascii=False).encode("utf8"))
    return receipt
