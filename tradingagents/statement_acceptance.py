"""Bounded manual CURRENT statement acceptance, using isolated runtime state.

No credential discovery occurs here. The manual CLI supplies a token explicitly;
offline callers supply synthetic transport and never read the environment.
"""

from __future__ import annotations

import copy
import csv
import io
import re
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows import current_tushare_statements
from tradingagents.dataflows.financial_dispatch import (
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import InstrumentKind, SourceAcquisitionUnavailable
from tradingagents.tushare_financial_https import DESTINATIONS, _Client

TOOLS = {
    "get_balance_sheet": FinancialStatementType.BALANCE_SHEET,
    "get_income_statement": FinancialStatementType.INCOME_STATEMENT,
    "get_cashflow": FinancialStatementType.CASH_FLOW,
}


def isolated_config(root, *, destination, account):
    if destination not in DESTINATIONS or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", account
    ):
        raise ValueError("explicit destination and non-secret account scope required")
    root = Path(root).resolve()
    authority = Path(__file__).resolve().parents[1] / "config"
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        instrument_identity_registry_path=str(authority / "instrument_identity_registry.json"),
        instrument_identity_registry_sha256=(authority / "instrument_identity_registry.sha256")
        .read_text(encoding="ascii")
        .split()[0],
        market_history_mode="current",
        data_usage_mode="personal_research",
        mainland_capability_routing_mode="legacy",
        tool_vendors=dict.fromkeys(TOOLS, "tushare"),
        data_cache_dir=str(root / "cache"),
        market_history_database_path=str(root / "coordinator.sqlite3"),
        market_history_payload_root=str(root / "payloads"),
        market_history_backup_root=str(root / "backups"),
        tushare_financial_https_endpoint=destination,
        tushare_account_scope_label=account,
        tushare_financial_cache_max_age_seconds=86400,
        tushare_financial_force_refresh=False,
        tushare_calls_per_minute=40,
        tushare_operator_safety_ceiling_calls_per_minute=40,
    )
    return config


def validate_request(root, *, ticker, destination, account, as_of):
    """Resolve the selected security using pinned source files before credentials."""
    config = isolated_config(root, destination=destination, account=account)
    date.fromisoformat(as_of)
    asset = resolve_run_asset_configuration(ticker, config=config)
    identity = asset.instrument_identity
    if (not identity.is_authoritative or identity.venue not in {"XSHG", "XSHE"}
            or identity.instrument_kind is not InstrumentKind.EQUITY or identity.currency != "CNY"):
        raise ValueError("authoritative mainland equity required")
    return config, asset


def _dispatcher(config, asset):
    return FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
    )


def _view(result):
    if result.artifact is None or "# degraded=true" in result.value:
        return None
    rows = list(
        csv.DictReader(
            io.StringIO(
                "\n".join(line for line in result.value.splitlines() if not line.startswith("#"))
            )
        )
    )
    if not rows:
        return None
    return {
        "sha256": sha256(result.value.encode()).hexdigest(),
        "retrieved_at": sorted({row["retrieved_at"] for row in rows}),
        "observed_at": sorted({row["observed_at"] for row in rows}),
        "periods": sorted({row["period_end"] for row in rows}),
    }


def _run(config, asset, as_of, progress):
    route = _dispatcher(config, asset)
    identity = {"canonical_symbol": asset.instrument_identity.symbol}
    views, completed = {}, []
    # The worker owns this payload-free state. Keep shared collections and update
    # the budget before I/O so even interruption before our return is truthful.
    progress.update(**identity, status="stopped", views=views, completed_tools=completed)
    for tool, statement in TOOLS.items():
        for frequency in (
            FinancialReportingFrequency.QUARTERLY,
            FinancialReportingFrequency.ANNUAL,
        ):
            progress["current_view"] = tool + ":" + frequency.value
            try:
                result = route.dispatch(
                    FinancialToolRequest(
                        tool_name=tool,
                        statement_type=statement,
                        frequency=frequency,
                        as_of_date=date.fromisoformat(as_of),
                        tool_call_id=tool + ":" + frequency.value,
                    )
                )
                view = _view(result)
            except (Exception, KeyboardInterrupt) as exc:
                return {**identity, "status": "stopped", "completed_tools": completed,
                        "views": views, "failed_view": tool + ":" + frequency.value,
                        "failure": {"error_code": "acceptance_interrupted"
                                    if isinstance(exc, KeyboardInterrupt)
                                    else "acceptance_dispatch_failed", "retryable": False}}
            if view is None:
                return {
                    **identity,
                    "status": "stopped",
                    "completed_tools": completed,
                    "views": views,
                    "failed_view": tool + ":" + frequency.value,
                    "failure": _failure(result),
                }
            views[tool + ":" + frequency.value] = view
        completed.append(tool)
    return {**identity, "status": "acquired", "completed_tools": completed, "views": views}


def _failure(result):
    for outcome in reversed(result.outcomes):
        if isinstance(outcome, SourceAcquisitionUnavailable):
            return {
                "reason": outcome.reason.value,
                "error_code": outcome.error_code or "current_financial_" + outcome.reason.value,
                "http_status": outcome.http_status,
                "retry_after_seconds": outcome.retry_after_seconds,
                "retryable": False,
            }
    return {"reason": "degraded_data", "error_code": "current_financial_degraded_view",
            "retryable": False}


@contextmanager
def _factory(factory):
    # This diagnostic is single-threaded and short-lived; retain the production
    # configured dispatcher/adapter/cache/coordinator/normalizer path verbatim.
    with patch.object(current_tushare_statements, "create_financial_statement_client", factory):
        yield


def acquire(root, *, ticker, destination, account, as_of, token, exchange_guard=None, progress=None):
    config, asset = validate_request(
        root, ticker=ticker, destination=destination, account=account, as_of=as_of
    )
    if not isinstance(token, str) or not token:
        raise ValueError("explicit in-memory token required")
    attempted = []
    failure_metadata = {}
    progress = {} if progress is None else progress
    progress.update(physical_requests=0, endpoints=attempted)

    def before_request(endpoint):
        if len(attempted) >= 3 or endpoint in attempted:
            raise RuntimeError("manual physical budget exhausted")
        attempted.append(endpoint)
        progress["physical_requests"] = len(attempted)

    with _factory(lambda _: _Client(destination, token, before_request=before_request,
                                   exchange_guard=exchange_guard,
                                   failure_metadata=failure_metadata)):
        result = _run(config, asset, as_of, progress)
    if "failure" in result:
        result["failure"].update(failure_metadata)
    return {**result, "physical_requests": len(attempted), "endpoints": attempted}


def reuse(root, *, ticker, destination, account, as_of, progress=None):
    config, asset = validate_request(
        root, ticker=ticker, destination=destination, account=account, as_of=as_of
    )
    attempts = []
    progress = {} if progress is None else progress
    progress.update(physical_requests=0, client_attempts=0)

    def forbidden(_):
        attempts.append("client_creation")
        progress["client_attempts"] = len(attempts)
        raise RuntimeError("offline reuse forbids client or credential access")

    with _factory(forbidden):
        result = _run(config, asset, as_of, progress)
    if result["status"] == "acquired" and not attempts:
        result["status"] = "reused"
    return {**result, "physical_requests": 0, "client_attempts": len(attempts)}
