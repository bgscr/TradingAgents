"""Read-only contextual preflight for explicitly enabled analysis budgets.

Calendar and financial cache-only failures are blocking. Market cache findings
are informational: counted live acquisition is allowed only after the separate
route gate passes. A retained frame is NOT admitted here: the complete CURRENT
decoder currently opens a migrating store, so runtime revalidation remains
mandatory. No global configuration, active budget, cache or provider is mutated.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class BudgetReadinessIssue:
    location: str
    reason: str


@dataclass(frozen=True)
class BudgetReadinessCheck:
    location: str
    status: str
    requires_provider_request: bool | None = None


@dataclass(frozen=True)
class BudgetReadinessReport:
    enabled: bool
    issues: tuple[BudgetReadinessIssue, ...]
    checked: tuple[BudgetReadinessCheck, ...]


class BudgetReadinessError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("analysis_budget:context_not_ready; " + "; ".join(
            item.location + ":" + item.reason for item in report.issues
        ))


class _ReadFailure(Exception):
    """Only fixed internal codes can reach the public report."""


def _file_stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _no_sidecars(path):
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise _ReadFailure("concurrent_database_state")


@contextmanager
def _stable_database(path):
    """Reject concurrent/uncertain SQLite state instead of ignoring a WAL.

This observes an immutable read interval, not a lock or promise about future
runtime use. Runtime readers must revalidate after this preflight returns.
"""
    path = Path(path).expanduser().resolve()
    _no_sidecars(path)
    if not path.is_file():
        raise _ReadFailure("missing")
    before = _file_stamp(path)
    try:
        yield path
    finally:
        _no_sidecars(path)
        if _file_stamp(path) != before:
            raise _ReadFailure("concurrent_database_state")


@contextmanager
def _readonly_history(config):
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.schema import SCHEMA_VERSION
    from tradingagents.market_history.store import MarketHistoryStore

    settings = MarketHistoryConfig.from_mapping(config)
    with _stable_database(settings.database_path) as path:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            # Defense in depth: even accidental calls to a store writer cannot
            # mutate this connection or attach a second database.
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
            connection.set_authorizer(lambda action, *_: (
                sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
            ))
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            if row != (SCHEMA_VERSION,):
                raise _ReadFailure("unsupported_schema")
            # __init__ only assigns members; open() migrates and is forbidden.
            store = MarketHistoryStore(settings, connection)
            try:
                yield store
            finally:
                store.close()
        finally:
            connection.close()


def _market_chain(config):
    value = config.get("market_data_vendors", {}).get("cn_a", {}).get("core_stock_apis", "default")
    if value == "default":
        value = config.get("data_vendors", {}).get("core_stock_apis", "default")
    if value == "default":
        return ("akshare", "baostock", "yfinance")
    if not isinstance(value, str):
        raise ValueError("invalid chain")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _market_cache_status(store, config, symbol, start, end, calendar, provider_name):
    from tradingagents.dataflows.market_snapshot import SNAPSHOT_PROVIDERS, validate_ohlcv_frame
    from tradingagents.market_history import current_cache
    from tradingagents.market_history.calendar import MarketSessionStatus
    from tradingagents.market_history.current import _analysis_as_of

    # Tushare CURRENT raw-family refresh and qualified-history readers need a
    # separate, reviewed seam. An adjusted-frame hit never waives those paths.
    if provider_name == "tushare":
        return "raw_current_history_not_checked", None
    descriptor = SNAPSHOT_PROVIDERS.get(provider_name)
    if descriptor is None:
        return "unsupported_provider", None
    provider, instrument = current_cache.binding(symbol, provider_name, descriptor, store=store)
    try:
        loaded = store.load_current_provider_frame(
            instrument.canonical_symbol, start.isoformat(), end.isoformat(),
            minimum_history_rows=1, provider=provider, instrument=instrument,
            adjustment_basis=descriptor.adjustment_basis,
        )
    except KeyError:
        return "missing", True
    metadata, stored = loaded.metadata, loaded.stored
    if (not isinstance(metadata, dict)
            or metadata.get("contract") != current_cache.CONTRACT
            or metadata.get("provider") != asdict(provider)
            or metadata.get("instrument") != asdict(instrument)
            or metadata.get("window") != [start.isoformat(), end.isoformat()]
            or metadata.get("data_usage_mode") != config.get("data_usage_mode")
            or stored.snapshot_id != "current-cache-record=sha256:" + current_cache.digest(metadata)):
        return "corrupt", True
    max_age = config.get("market_history_current_cache_max_age_seconds", 86400)
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        return "invalid_freshness_policy", None
    observed = datetime.fromisoformat(stored.retrieved_at)
    if observed.utcoffset() is None or not timedelta(0) <= datetime.now(timezone.utc) - observed <= timedelta(seconds=max_age):
        return "stale", True
    frame = validate_ohlcv_frame(loaded.frame, end.isoformat(), start.isoformat())
    if len(frame) != len(loaded.frame):
        return "corrupt", True
    if calendar is not None:
        as_of = _analysis_as_of(end, timezone_name=calendar.timezone_name)
        expected = {
            session.session_date for session in calendar.sessions
            if start <= session.session_date <= end and session.status is MarketSessionStatus.OPEN
            and (session.closes_at or datetime.fromisoformat(
                session.session_date.isoformat() + "T15:00:00+08:00"
            )) <= as_of
        }
        if set(frame["Date"].dt.date) != expected:
            return "gap", True
    return "evidence_checked_runtime_revalidation_required", None


def _inspect_market(config, symbol, start, end, issues, checked):
    from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
    from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window
    from tradingagents.market_history.current_cache import (
        compatibility_diagnostic,
        read_prepared_sse_calendar,
    )

    if resolve_mainland_instrument(symbol) is None:
        issues.append(BudgetReadinessIssue("market", "non_mainland_context_not_supported"))
        return
    if config.get("market_history_mode") != "current" or config.get("data_usage_mode") != "personal_research":
        issues.append(BudgetReadinessIssue("market", "current_personal_research_required"))
        return
    try:
        chain = _market_chain(config)
        with _readonly_history(config) as store:
            try:
                calendar = read_prepared_sse_calendar(store, symbol, start, end)
                if calendar is None:
                    calendar = read_mainland_calendar_window(store, start, end)
                if calendar is None:
                    issues.append(BudgetReadinessIssue("calendar", "incomplete_natural_date_window"))
                elif calendar.observed_at > datetime.now(timezone.utc):
                    issues.append(BudgetReadinessIssue("calendar", "future_observation"))
                    calendar = None
                else:
                    checked.append(BudgetReadinessCheck("calendar", "verified_full_natural_date_window", False))
            except Exception:
                calendar = None
                issues.append(BudgetReadinessIssue("calendar", "corrupt_or_unreadable"))
            if compatibility_diagnostic() is not None:
                checked.append(BudgetReadinessCheck("market_cache", "compatibility_gate_denied", True))
                # Preserve the runtime denial. Structural findings below remain
                # useful diagnostics; they neither admit this cache nor waive
                # the manifest gate or a counted live acquisition requirement.
            for provider in chain:
                label = provider if provider in {"akshare", "baostock", "yfinance", "tushare"} else "unknown"
                try:
                    status, requires_request = _market_cache_status(store, config, symbol, start, end, calendar, provider)
                except Exception:
                    status, requires_request = "corrupt_or_unreadable", True
                checked.append(BudgetReadinessCheck("market_cache." + label, status, requires_request))
    except _ReadFailure as exc:
        issues.append(BudgetReadinessIssue("calendar", str(exc)))
    except Exception:
        issues.append(BudgetReadinessIssue("calendar", "corrupt_or_unreadable"))


def _financial_identity(config, symbol, supplied):
    from tradingagents.asset_configuration import (
        RunAssetConfigurationError,
        resolve_run_asset_configuration,
    )
    from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
    from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

    if supplied is None:
        try:
            # Use the same authoritative identity and safe provenance projection
            # as the runtime dispatcher, including its exact cache scope revision.
            supplied = resolve_run_asset_configuration(symbol, config=config).instrument_identity
        except RunAssetConfigurationError as exc:
            raise _ReadFailure("authoritative_identity_unavailable") from exc
    routed = resolve_mainland_instrument(symbol)
    if (not isinstance(supplied, InstrumentIdentityEvidence) or not supplied.is_authoritative
            or supplied.instrument_kind is not InstrumentKind.EQUITY
            or supplied.venue not in {"XSHG", "XSHE"} or supplied.currency != "CNY"
            or routed is None or supplied.symbol != routed.yahoo_symbol):
        raise _ReadFailure("authoritative_mainland_equity_required")
    return supplied


def _financial_payloads(chunks, endpoint, identity, statement_type, now):
    from tradingagents.dataflows._tushare_statement_artifacts import (
        decode_provider_artifact,
        financial_artifact_identity,
        financial_row_artifact_identities,
    )
    from tradingagents.dataflows._tushare_statement_normalization import project_candidates
    from tradingagents.dataflows.current_tushare_statements import (
        NORMALIZER_VERSION,
        _announcement_in_range,
    )

    row_count = 0
    for chunk in chunks:
        payload = decode_provider_artifact(chunk.payload, endpoint_id=endpoint)
        for field in ("retrieved_at", "observed_at"):
            observed = datetime.fromisoformat(payload[field].replace("Z", "+00:00"))
            # checked_at is captured before fetch; the response's source time
            # can legitimately be later after coordinator pacing and I/O.
            if observed.utcoffset() is None or observed > now:
                raise _ReadFailure("corrupt_payload")
        payload = {**payload, "rows": [row for row in payload["rows"] if _announcement_in_range(
            row, chunk.range_start, chunk.range_end
        )]}
        key = {"contract": NORMALIZER_VERSION, "endpoint": endpoint,
               "announcement_start": chunk.range_start.isoformat(), "announcement_end": chunk.range_end.isoformat()}
        artifact = financial_artifact_identity(key=key, payload=payload)
        _, _, rejected = project_candidates(
            payload=payload, row_artifacts=financial_row_artifact_identities(key=key, response_artifact=artifact, payload=payload),
            statement_type=statement_type, instrument_identity=identity,
            expected_provider_symbol=identity.symbol.split(".")[0] + (".SH" if identity.venue == "XSHG" else ".SZ"),
        )
        # A syntactically intact cache is insufficient if its rows contradict
        # identity/period/unit metadata. This deliberately fails conservatively.
        if rejected:
            raise _ReadFailure("invalid_provider_rows")
        row_count += len(payload["rows"])
    return "validated_empty_response" if row_count == 0 else "validated_windows_runtime_period_selection_required"


def _inspect_financial(config, symbol, end, supplied_identity, issues, checked):
    from tradingagents.dataflows.current_statement_cache import (
        CurrentStatementCache,
        StatementCacheScope,
        _gaps,
    )
    from tradingagents.dataflows.current_tushare_statements import (
        FINANCIAL_HTTPS_ENDPOINTS,
        NORMALIZER_VERSION,
        TOOL_ENDPOINTS,
    )
    from tradingagents.dataflows.financial_contracts import FinancialStatementType
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity

    tools = config.get("tool_vendors", {})
    selected = [tool for tool in TOOL_ENDPOINTS if isinstance(tools, dict) and tools.get(tool) == "tushare"]
    if not selected:
        return
    try:
        if config.get("financial_cache_only") is not True:
            raise _ReadFailure("cache_only_required")
        if config.get("tushare_financial_force_refresh") is True:
            raise _ReadFailure("refresh_conflict")
        if (config.get("market_history_mode") != "current" or config.get("data_usage_mode") != "personal_research"
                or config.get("mainland_capability_routing_mode", "legacy") != "legacy"):
            raise _ReadFailure("current_legacy_personal_research_required")
        endpoint = config.get("tushare_financial_https_endpoint")
        account = config.get("tushare_account_scope_label")
        age = config.get("tushare_financial_cache_max_age_seconds", 86400)
        if (endpoint not in FINANCIAL_HTTPS_ENDPOINTS or not isinstance(account, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", account) is None
                or type(age) is not int or not 1 <= age <= 31536000):
            raise _ReadFailure("invalid_cache_scope")
        identity = _financial_identity(config, symbol, supplied_identity)
        revision = ProviderSubrequestInstrumentIdentity.from_authoritative(identity).identity_revision
        path = Path(config["data_cache_dir"]) / "current-financial-statements.sqlite3"
        start = date(end.year - 6, 1, 1)
        now = datetime.now(timezone.utc)
        with _stable_database(path):
            cache = CurrentStatementCache(path, max_age=timedelta(seconds=age), read_only=True)
            for tool in selected:
                location = "financial." + tool
                try:
                    scope = StatementCacheScope(account, revision, TOOL_ENDPOINTS[tool], sha256(endpoint.encode()).hexdigest(), NORMALIZER_VERSION)
                    chunks, failure = cache._load(scope.key, start, end)
                    if failure:
                        raise _ReadFailure("corrupt")
                    if not chunks:
                        raise _ReadFailure("missing")
                    if any(not timedelta(0) <= now - chunk.checked_at < cache.max_age for chunk in chunks):
                        raise _ReadFailure("stale")
                    if _gaps(start, end, [(chunk.range_start, chunk.range_end) for chunk in chunks]):
                        raise _ReadFailure("incomplete_announcement_window")
                    statement = {"get_balance_sheet": FinancialStatementType.BALANCE_SHEET,
                                 "get_income_statement": FinancialStatementType.INCOME_STATEMENT,
                                 "get_cashflow": FinancialStatementType.CASH_FLOW}[tool]
                    status = _financial_payloads(chunks, TOOL_ENDPOINTS[tool], identity, statement, now)
                    checked.append(BudgetReadinessCheck(location, status, False))
                except _ReadFailure as exc:
                    issues.append(BudgetReadinessIssue(location, str(exc)))
                except Exception:
                    issues.append(BudgetReadinessIssue(location, "corrupt_or_unreadable"))
    except _ReadFailure as exc:
        issues.extend(BudgetReadinessIssue("financial." + tool, str(exc)) for tool in selected)
    except Exception:
        issues.extend(BudgetReadinessIssue("financial." + tool, "corrupt_or_unreadable") for tool in selected)


def inspect_budget_readiness(config, *, ticker, as_of_date,
                             selected_analysts=("market", "social", "news", "fundamentals"),
                             financial_config=None, instrument_identity=None):
    """Inspect the baseline evidence window without provider, model or write I/O.

    This does not waive route checks, admit a snapshot, pin future filesystem
    state, or validate model-chosen date windows beyond the baseline request.
    """
    if not config.get("analysis_budget_enabled", False):
        return BudgetReadinessReport(False, (), ())
    issues, checked = [], []
    try:
        from dateutil.relativedelta import relativedelta

        end = date.fromisoformat(as_of_date) if isinstance(as_of_date, str) else as_of_date
        if type(end) is not date or not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("invalid context")
        start = end - relativedelta(years=5)
    except (TypeError, ValueError, OverflowError):
        return BudgetReadinessReport(True, (BudgetReadinessIssue("context", "invalid_instrument_or_date"),), ())
    _inspect_market(config, ticker, start, end, issues, checked)
    if "fundamentals" in tuple(getattr(item, "value", item) for item in selected_analysts):
        _inspect_financial(config if financial_config is None else financial_config,
                           ticker, end, instrument_identity, issues, checked)
    return BudgetReadinessReport(True, tuple(issues), tuple(checked))


def validate_budget_readiness(config, **context):
    report = inspect_budget_readiness(config, **context)
    if report.issues:
        raise BudgetReadinessError(report)
    return report
