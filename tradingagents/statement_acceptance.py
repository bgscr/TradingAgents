"""Bounded manual CURRENT statement acceptance, using isolated runtime state.

No credential discovery occurs here. The manual CLI supplies a token explicitly;
offline callers supply synthetic transport and never read the environment.
"""

from __future__ import annotations

import copy
import csv
import io
import re
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows import current_tushare_statements
from tradingagents.dataflows.financial_contracts import (
    FinancialReportingFrequency,
    FinancialStatementType,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import InstrumentKind, SourceAcquisitionUnavailable
from tradingagents.tushare_financial_https import DESTINATIONS, _Client

TOOLS = {
    "get_balance_sheet": FinancialStatementType.BALANCE_SHEET,
    "get_income_statement": FinancialStatementType.INCOME_STATEMENT,
    "get_cashflow": FinancialStatementType.CASH_FLOW,
}
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def isolated_config(root, *, destination, account):
    if destination not in DESTINATIONS or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", account
    ):
        raise ValueError("explicit destination and non-secret account scope required")
    root = Path(root).resolve()
    authority = SOURCE_ROOT / "config"
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


def validate_request(root, *, ticker, destination, account, as_of,
                     shared_binding=None, shared_storage=None):
    """Resolve the selected security using pinned source files before credentials."""
    config = isolated_config(root, destination=destination, account=account)
    date.fromisoformat(as_of)
    identity_key = 'equity_identity' if isinstance(shared_binding,dict) and 'equity_identity' in shared_binding else 'bank_identity'
    bank_identity = shared_binding.get(identity_key) if isinstance(shared_binding, dict) else None
    if bank_identity is not None:
        from tradingagents.dataflows.bank_identity_snapshot import validate_binding
        config.update(validate_binding(SOURCE_ROOT, Path(root).absolute().parent,
                                       bank_identity, ticker, as_of, equity=identity_key=="equity_identity"))
    asset = resolve_run_asset_configuration(ticker, config=config)
    identity = asset.instrument_identity
    if (not identity.is_authoritative or identity.venue not in {"XSHG", "XSHE"}
            or identity.instrument_kind is not InstrumentKind.EQUITY or identity.currency != "CNY"):
        raise ValueError("authoritative mainland equity required")
    if shared_binding is not None:
        from tradingagents.dataflows.provider_subrequests import (
            ProviderSubrequestInstrumentIdentity,
        )
        from tradingagents.market_history.coordinator import (
            ProviderRequestCoordinator,
            upstream_service_identity_for_provider,
        )
        from tradingagents.market_history.schema import SCHEMA_VERSION

        expected = {
            "contract": "shared-statement-acceptance-v1",
            "account": account,
            "destination": destination,
            "instrument_identity_revision": ProviderSubrequestInstrumentIdentity.from_authoritative(
                identity
            ).identity_revision,
            "as_of": as_of,
            "announcement_start": date(date.fromisoformat(as_of).year - 6, 1, 1).isoformat(),
            "announcement_end": as_of,
            "normalizer_version": current_tushare_statements.NORMALIZER_VERSION,
        }
        if (not isinstance(shared_binding, dict)
                or set(shared_binding) != {*expected, "storage", *({identity_key} if bank_identity is not None else set())}
                or any(shared_binding[key] != value for key, value in expected.items())
                or shared_storage is None):
            raise ValueError("shared request identity mismatch")
        storage = shared_binding["storage"]
        if storage != {
            "market_database": str(shared_storage.market),
            "coordinator_database": str(shared_storage.coordinator),
            "cache_database": str(shared_storage.cache),
            "coordinator_file_id": shared_storage.file_id,
        }:
            raise ValueError("shared path binding mismatch; use canonical paths")
        upstream_id, service = upstream_service_identity_for_provider("tushare")
        raw_ceiling = shared_storage.validate_authority(SCHEMA_VERSION, upstream_id, service)
        _require_shared_ceiling(
            ProviderRequestCoordinator._parse_operator_ceiling(upstream_id, raw_ceiling)
        )
        config.update(
            data_cache_dir=str(shared_storage.cache.parent),
            market_history_database_path=str(shared_storage.market),
        )
    elif shared_storage is not None:
        raise ValueError("shared storage requires explicit request binding")
    return config, asset


def _dispatcher(config, asset):
    # Request validation and storage binding do not need provider dispatch.
    from tradingagents.dataflows.financial_dispatch import FinancialToolDispatcher

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


def _run(config, asset, as_of, progress, *, allow_optional_gaps=False):
    from tradingagents.dataflows.current_statement_diagnostics import (
        capture_statement_diagnostics,
        scopes_from_binding,
        unavailable_view,
        validate_prepared_views,
    )
    from tradingagents.dataflows.financial_dispatch import FinancialToolRequest
    from tradingagents.evidence import EvidenceCapability, capability_profile_for

    if type(allow_optional_gaps) is not bool:
        raise ValueError("optional financial gap mode must be explicit")
    profile = capability_profile_for(asset.instrument_identity.instrument_kind)
    optional = allow_optional_gaps and EvidenceCapability.COMPANY_FINANCIALS in profile.optional_capabilities
    from tradingagents.dataflows.current_tushare_statements import NORMALIZER_VERSION
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity
    expected_scopes = scopes_from_binding({
        "account": config["tushare_account_scope_label"],
        "instrument_identity_revision": ProviderSubrequestInstrumentIdentity.from_authoritative(asset.instrument_identity).identity_revision,
        "destination": config["tushare_financial_https_endpoint"],
        "normalizer_version": NORMALIZER_VERSION,
    })
    route = _dispatcher(config, asset)
    identity = {"canonical_symbol": asset.instrument_identity.symbol}
    views, completed = {}, []
    diagnostics = {}
    progress["statement_diagnostics"] = diagnostics
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
                with capture_statement_diagnostics() as records:
                    result = route.dispatch(
                    FinancialToolRequest(
                        tool_name=tool,
                        statement_type=statement,
                        frequency=frequency,
                        as_of_date=date.fromisoformat(as_of),
                        tool_call_id=tool + ":" + frequency.value,
                    )
                )
                if records:
                    diagnostics[tool + ":" + frequency.value] = records[-1]
                view = _view(result)
                if view is not None:
                    view = {**view, "status": "available"}
                elif (optional and records and result.artifact is None
                      and _failure(result)["error_code"] == "current_financial_ineligible_rows"):
                    try:
                        view = unavailable_view(records[-1], ticker=asset.instrument_identity.symbol,
                                                tool=tool, frequency=frequency.value, as_of=as_of,
                                                expected_scope=expected_scopes[tool])
                    except ValueError:
                        view = None
            except (Exception, KeyboardInterrupt) as exc:
                return {**identity, "status": "stopped", "completed_tools": completed,
                        "views": views, "statement_diagnostics": diagnostics, "failed_view": tool + ":" + frequency.value,
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
                    "statement_diagnostics": diagnostics,
                }
            views[tool + ":" + frequency.value] = view
        completed.append(tool)
    gaps = validate_prepared_views(views, ticker=asset.instrument_identity.symbol,
                                   as_of=as_of, allow_optional_gaps=optional, expected_scopes=expected_scopes)
    return {**identity, "status": "acquired_degraded" if gaps else "acquired",
            "completed_tools": completed, "views": views, "statement_diagnostics": diagnostics}


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


def _require_shared_ceiling(ceiling):
    if (ceiling is None or ceiling.minimum_interval < timedelta(seconds=1.5)
            or ceiling.allow_prewarming is not False):
        raise ValueError("existing shared ceiling incompatible with bounded acceptance")
    return ceiling


@contextmanager
def _retain_shared_ceiling(storage):
    """Single-threaded manual worker: read the real policy, never reconfigure it.

    The ordinary adapter configures its isolated 40/min policy before dispatch.
    Shared acceptance substitutes only that configuration call, returning the
    existing typed policy (including its original timestamp). Real coordinator
    scheduling, leases, cooldowns and attempt accounting remain unchanged.
    """
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )

    expected_id, _ = upstream_service_identity_for_provider("tushare")

    def retain(coordinator, *, upstream_service_id, minimum_interval,
               allow_prewarming, configured_at):
        if (upstream_service_id != expected_id
                or coordinator._store.config.database_path != storage.coordinator
                or minimum_interval != timedelta(seconds=1.5) or allow_prewarming is not False):
            raise ValueError("unexpected shared ceiling configuration request")
        storage.check_existing()
        return _require_shared_ceiling(coordinator.configured_operator_ceiling(expected_id))

    with patch.object(ProviderRequestCoordinator, "configure_operator_ceiling", retain):
        yield


def acquire(root, *, ticker, destination, account, as_of, token, exchange_guard=None,
            progress=None, shared_binding=None, shared_storage=None, allow_optional_gaps=False):
    config, asset = validate_request(
        root, ticker=ticker, destination=destination, account=account, as_of=as_of,
        shared_binding=shared_binding, shared_storage=shared_storage
    )
    if not isinstance(token, str) or not token:
        raise ValueError("explicit in-memory token required")
    attempted = []
    failure_metadata = {}
    progress = {} if progress is None else progress
    progress.update(physical_requests=0, endpoints=attempted)

    def before_request(endpoint):
        if endpoint not in {"balancesheet", "income", "cashflow"} or len(attempted) >= 3 or endpoint in attempted:
            raise RuntimeError("manual physical budget exhausted")
        if isinstance(shared_binding, dict) and ('bank_identity' in shared_binding or 'equity_identity' in shared_binding):
            from tradingagents.dataflows.bank_identity_snapshot import verify_expiry
            verify_expiry(shared_binding.get('equity_identity') or shared_binding['bank_identity'])
        if shared_storage is not None:
            shared_storage.check_existing()
        attempted.append(endpoint)
        progress["physical_requests"] = len(attempted)

    storage_scope = shared_storage.connection_scope() if shared_storage else nullcontext()
    ceiling_scope = _retain_shared_ceiling(shared_storage) if shared_storage else nullcontext()
    with storage_scope, ceiling_scope, _factory(lambda _: _Client(destination, token, before_request=before_request,
                                   exchange_guard=exchange_guard,
                                   failure_metadata=failure_metadata)):
        result = _run(config, asset, as_of, progress, allow_optional_gaps=allow_optional_gaps)
    if "failure" in result:
        result["failure"].update(failure_metadata)
    return {**result, "physical_requests": len(attempted), "endpoints": attempted}


def reuse(root, *, ticker, destination, account, as_of, progress=None,
          shared_binding=None, shared_storage=None, allow_optional_gaps=False):
    config, asset = validate_request(
        root, ticker=ticker, destination=destination, account=account, as_of=as_of,
        shared_binding=shared_binding, shared_storage=shared_storage
    )
    attempts = []
    progress = {} if progress is None else progress
    progress.update(physical_requests=0, client_attempts=0)

    def forbidden(_):
        attempts.append("client_creation")
        progress["client_attempts"] = len(attempts)
        raise RuntimeError("offline reuse forbids client or credential access")

    storage_scope = shared_storage.connection_scope() if shared_storage else nullcontext()
    ceiling_scope = _retain_shared_ceiling(shared_storage) if shared_storage else nullcontext()
    with storage_scope, ceiling_scope, _factory(forbidden):
        result = _run(config, asset, as_of, progress, allow_optional_gaps=allow_optional_gaps)
    if result["status"] in {"acquired", "acquired_degraded"} and not attempts:
        result["status"] = "reused_degraded" if result["status"] == "acquired_degraded" else "reused"
    return {**result, "physical_requests": 0, "client_attempts": len(attempts)}
