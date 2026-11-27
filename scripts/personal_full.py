"""Personal mainland-equity research. Check local evidence or explicitly run the full graph."""

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).absolute().parents[1]
KEYS = ("TUSHARE_TOKEN", "DEEPSEEK_API_KEY")
TRANSPORT_OVERRIDES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "SSLKEYLOGFILE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def local_path(value):
    """Check the literal volume before the first file operation, including on Windows."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute_local_path_required")
    if os.name == "nt":
        from ctypes import windll

        if (
            not re.fullmatch("[A-Za-z]:", path.drive)
            or windll.kernel32.GetDriveTypeW(path.anchor) != 3
        ):
            raise ValueError("fixed_local_volume_required")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or stat.S_ISREG(info.st_mode)
            and info.st_nlink != 1
        ):
            raise ValueError("linked_local_path")
    return path


def check_store_paths(config):
    for name in (
        "market_history_database_path",
        "market_history_payload_root",
        "market_history_backup_root",
        "data_cache_dir",
        "instrument_identity_registry_path",
        "crypto_identity_registry_path",
        "crypto_identity_registry_checksum_path",
    ):
        if config.get(name):
            local_path(config[name])
    base = Path(config["market_history_database_path"])
    authority = base.with_name(base.name + ".provider-requests.sqlite3")
    financial = Path(config["data_cache_dir"]) / "current-financial-statements.sqlite3"
    for db in (base, authority, financial):
        for suffix in ("", "-wal", "-shm", "-journal", ".payload-mutation.sqlite3"):
            local_path(db.with_name(db.name + suffix))
    for suffix in (".payloads", ".backups"):
        local_path(authority.with_name(authority.name + suffix))
    for root in (
        Path(config["market_history_payload_root"]),
        authority.with_name(authority.name + ".payloads"),
    ):
        local_path(root / ".market-history-database-owner")


def credentials():
    from scripts.project_credentials import read_project_credentials

    values = {key: os.environ.get(key, "") for key in KEYS}
    if not any(values.values()) and local_path(ROOT / ".env").is_file():
        return read_project_credentials(ROOT)
    if any(
        value
        and (
            len(value) > 512
            or not all(32 < ord(char) < 127 for char in value)
            or "${" in value
            or value.startswith(key + "=")
        )
        for key, value in values.items()
    ):
        raise ValueError("invalid_project_credentials")
    return values


def selected_session(calendar, as_of):
    """Select the latest completed exchange session. This alone cannot verify a stock's bar."""
    from tradingagents.market_history.maintenance import latest_completed_session

    if as_of.utcoffset() is None or calendar.timezone_name != "Asia/Shanghai":
        raise ValueError("invalid_session_clock")
    today = as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if calendar.observed_at.utcoffset() is None or not timedelta(
        0
    ) <= as_of - calendar.observed_at < timedelta(days=1):
        raise ValueError("calendar_not_current")
    dates = {row.session_date for row in calendar.sessions}
    if today not in dates:
        raise ValueError("calendar_coverage_missing")
    day = latest_completed_session(calendar, as_of=as_of)
    if day is None or any(
        day + timedelta(days=i) not in dates for i in range((today - day).days + 1)
    ):
        raise ValueError("calendar_coverage_missing")
    return day


def cached_close(store, symbol, calendar, day, now, max_age):
    """Verify the selected security's raw close in its own Tushare CURRENT family."""
    from tradingagents.dataflows.tushare_market_history import history_binding
    from tradingagents.market_history.maintenance import market_session_close

    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise ValueError("invalid_freshness")
    history = store.load_current_history(*history_binding(symbol))
    if history is None or day not in history.raw:
        return False
    session = next(row for row in calendar.sessions if row.session_date == day)
    observed = history.raw_observed_at[day]
    return (
        history.calendar.get(day) == "1"
        and history.statuses[day][0] == "traded"
        and history.raw[day][3].is_finite()
        and history.raw[day][3] > 0
        and observed.utcoffset() is not None
        and market_session_close(calendar, session=session) <= observed <= now
        and now - observed <= timedelta(seconds=max_age)
    )


def local_session(symbol, config, now):
    if not local_path(config["market_history_database_path"]).is_file():
        return None, False, "market_cache_missing"
    from scripts.personal_graph import local_payloads
    from tradingagents.budget_readiness import _ReadFailure, _readonly_history
    from tradingagents.market_history.current_cache import (
        compatibility_diagnostic,
        read_prepared_exchange_calendar,
    )
    from tradingagents.market_history.store import MarketHistoryCorruptionError

    today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        if compatibility_diagnostic() is not None:
            return None, False, "market_cache_sources_unverified"
        with local_payloads(), _readonly_history(config) as store:
            calendar = read_prepared_exchange_calendar(store, symbol, today, today, config=config)
            if calendar is None:
                return None, False, "exchange_calendar_missing"
            day = selected_session(calendar, now)
            verified = cached_close(
                store,
                symbol,
                calendar,
                day,
                now,
                config.get("market_history_current_cache_max_age_seconds", 86400),
            )
            return day.isoformat(), verified, None if verified else "stock_close_unavailable"
    except (
        KeyError,
        ValueError,
        TypeError,
        _ReadFailure,
        OSError,
        sqlite3.Error,
        MarketHistoryCorruptionError,
    ):
        return None, False, "market_cache_or_identity_unverified"


def check(ticker, config, credentials, now):
    from scripts.personal_graph import ANALYSTS
    from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument

    if not re.fullmatch(r"[0-9]{6}(?:\.(?:SH|SS|SZ))?", ticker, re.ASCII):
        raise ValueError("unsupported_equity")
    instrument = resolve_mainland_instrument(ticker)
    if (
        not instrument
        or instrument.instrument_kind != "equity"
        or instrument.akshare_code.startswith(("200", "900"))
    ):
        raise ValueError("unsupported_equity")
    day, verified, issue = local_session(instrument.yahoo_symbol, config, now)
    missing = [name for name in KEYS if not credentials.get(name)]
    return {
        "mode": "personal_research",
        "ticker": instrument.yahoo_symbol,
        "analysts": ANALYSTS,
        "exchange_session": day,
        "stock_close_verified": verified,
        "cache_issue": issue,
        "missing_credentials": missing,
    }


def main(argv=None):
    started = (time.time(), time.monotonic())
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--ticker", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument(
        "--run",
        action="store_true",
        help="Use the configured accounts for one personal research run",
    )
    args = parser.parse_args(argv)
    from scripts import personal_graph
    from scripts.project_credentials import ProjectCredentialError, credential_failure_code

    try:
        local_path(ROOT / "tradingagents.local.json")
        local_path(ROOT / "reports" / "personal")
        keys = credentials()
        from tradingagents.default_config import DEFAULT_CONFIG

        config = dict(
            DEFAULT_CONFIG, market_history_mode="current", data_usage_mode="personal_research"
        )
        check_store_paths(config)
        result = check(args.ticker.upper(), config, keys, datetime.now(timezone.utc))
    except ProjectCredentialError as exc:
        print(json.dumps({"error": credential_failure_code(exc)}))
        return 2
    except (ValueError, OSError):
        print(json.dumps({"error": "invalid_personal_equity_or_configuration"}))
        return 2
    result["preflight_ready"] = False
    if any(os.environ.get(key) or os.environ.get(key.lower()) for key in TRANSPORT_OVERRIDES):
        result["configuration_issue"] = "custom_proxy_or_tls_settings_require_review"
    elif not result["cache_issue"] and not result["missing_credentials"]:
        try:
            config = personal_graph.prepare(config, result["ticker"], result["exchange_session"])
            result["preflight_ready"] = True
        except ValueError as exc:
            from tradingagents.budget_preflight import BudgetRouteConfigurationError
            from tradingagents.budget_readiness import BudgetReadinessError

            if isinstance(exc, BudgetReadinessError):
                result["missing_evidence"] = sorted({issue.location for issue in exc.report.issues})
            elif isinstance(exc, BudgetRouteConfigurationError):
                result["configuration_issue"] = "provider_routes_unavailable"
            elif str(exc) in {
                "deepseek_configuration_required",
                "deepseek_model_selection_required",
                "tushare_configuration_required",
                "statement_configuration_required",
                "personal_account_policy_required",
                "personal_account_rate_invalid",
                "incompatible_research_settings",
                "invalid_session_limits",
            }:
                result["configuration_issue"] = str(exc)
            else:
                result["configuration_issue"] = "invalid_research_configuration"
    if not args.run or not result["preflight_ready"]:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["preflight_ready"] else 2
    from unittest.mock import patch

    with patch.dict(os.environ, keys):
        receipt = personal_graph.run(
            config,
            result["ticker"],
            result["exchange_session"],
            ROOT / "reports" / "personal",
            started,
        )
    print(json.dumps(receipt, ensure_ascii=False))
    if receipt["status"] != "completed" or not receipt["report_written"]:
        return 1
    return 0 if receipt["outcome"] == "trading_decision" else 3


if __name__ == "__main__":
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["TRADINGAGENTS_PROJECT_ROOT"] = str(ROOT)
    os.environ.update(
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        OTEL_SDK_DISABLED="true",
    )
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
