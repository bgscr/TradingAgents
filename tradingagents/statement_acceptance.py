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


def _dispatcher(config):
    asset = resolve_run_asset_configuration("600895.SS", config=config)
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


def _run(config, as_of):
    route = _dispatcher(config)
    views, completed = {}, []
    for tool, statement in TOOLS.items():
        for frequency in (
            FinancialReportingFrequency.QUARTERLY,
            FinancialReportingFrequency.ANNUAL,
        ):
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
            if view is None:
                return {
                    "status": "stopped",
                    "completed_tools": completed,
                    "views": views,
                    "failed_view": tool + ":" + frequency.value,
                }
            views[tool + ":" + frequency.value] = view
        completed.append(tool)
    return {"status": "acquired", "completed_tools": completed, "views": views}


@contextmanager
def _factory(factory):
    # This diagnostic is single-threaded and short-lived; retain the production
    # configured dispatcher/adapter/cache/coordinator/normalizer path verbatim.
    with patch.object(current_tushare_statements, "create_financial_statement_client", factory):
        yield


def acquire(root, *, destination, account, as_of, token):
    config = isolated_config(root, destination=destination, account=account)
    date.fromisoformat(as_of)
    if not isinstance(token, str) or not token:
        raise ValueError("explicit in-memory token required")
    attempted = []

    def before_request(endpoint):
        if len(attempted) >= 3 or endpoint in attempted:
            raise RuntimeError("manual physical budget exhausted")
        attempted.append(endpoint)

    with _factory(lambda _: _Client(destination, token, before_request=before_request)):
        result = _run(config, as_of)
    return {**result, "physical_requests": len(attempted), "endpoints": attempted}


def reuse(root, *, destination, account, as_of):
    attempts = []

    def forbidden(_):
        attempts.append("client_creation")
        raise RuntimeError("offline reuse forbids client or credential access")

    with _factory(forbidden):
        result = _run(isolated_config(root, destination=destination, account=account), as_of)
    if result["status"] == "acquired" and not attempts:
        result["status"] = "reused"
    return {**result, "physical_requests": 0, "client_attempts": len(attempts)}
