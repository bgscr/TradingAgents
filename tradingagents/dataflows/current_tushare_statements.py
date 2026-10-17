"""Explicit CURRENT-only Tushare statements, separate from qualified routing."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pandas as pd

from tradingagents.dataflows._tushare_statement_artifacts import (
    decode_provider_artifact,
    encode_provider_artifact,
    financial_artifact_identity,
    financial_row_artifact_identities,
)
from tradingagents.dataflows._tushare_statement_normalization import project_candidates
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialPeriodDisposition,
    FinancialProviderCandidate,
    assess_financial_period_candidate,
    financial_revision_selection_key,
    financial_statement_field_declaration,
    order_financial_provider_revisions,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity
from tradingagents.evidence import AcquisitionUnavailableReason, InstrumentKind
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.coordinator import (
    PhysicalAttemptBudgetExhausted,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestPriority,
    record_active_physical_attempt_io,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore

TOOL_ENDPOINTS = {
    "get_balance_sheet": "balancesheet",
    "get_income_statement": "income",
    "get_cashflow": "cashflow",
}
FINANCIAL_HTTPS_ENDPOINTS = frozenset(
    {
        "https://api.tushare.pro",
        "https://api.waditu.com/dataapi",
    }
)
NORMALIZER_VERSION = "current-tushare-statements-v1"


def create_financial_statement_client(endpoint):
    # The real boundary is lazy: a fresh cache hit never discovers credentials.
    from tradingagents.tushare_financial_https import create_client

    return create_client(endpoint)


class CurrentTushareStatements:
    def __init__(self, *, instrument_identity, config, clock=None, sleeper=None):
        self._identity = instrument_identity
        self._config = {
            key: config.get(key)
            for key in (
                "market_history_mode",
                "data_usage_mode",
                "tushare_financial_https_endpoint",
                "tushare_account_scope_label",
                "data_cache_dir",
                "market_history_database_path",
                "market_history_payload_root",
                "market_history_backup_root",
                "tushare_calls_per_minute",
                "tushare_operator_safety_ceiling_calls_per_minute",
                "tushare_financial_force_refresh",
                "tushare_financial_cache_max_age_seconds",
            )
        }
        self._max_age_seconds = config.get("tushare_financial_cache_max_age_seconds", 86400)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._responses = {}
        binding = {
            "presentation": "current-statement-applicability-v2",
            "normalizer": NORMALIZER_VERSION,
            "endpoint": self._config["tushare_financial_https_endpoint"],
            "account": self._config["tushare_account_scope_label"],
            "mode": self._config["market_history_mode"],
            "usage": self._config["data_usage_mode"],
            "refresh": self._config["tushare_financial_force_refresh"] is True,
            "max_age_seconds": self._max_age_seconds,
        }
        self.variant_id = (
            "current-v1:" + sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        )

    def __call__(self, request):
        if self._config["data_usage_mode"] != "personal_research":
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.USAGE_NOT_ENTITLED, retryable=False
            )
        if (
            self._config["market_history_mode"] != "current"
            or self._identity.instrument_kind is not InstrumentKind.EQUITY
            or self._identity.venue not in {"XSHG", "XSHE"}
            or self._identity.currency != "CNY"
            or not self._identity.is_authoritative
            or type(self._max_age_seconds) is not int
            or not 1 <= self._max_age_seconds <= 31536000
            or self._config["tushare_financial_https_endpoint"] not in FINANCIAL_HTTPS_ENDPOINTS
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
                str(self._config["tushare_account_scope_label"]),
            )
        ):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.NOT_CONFIGURED, retryable=False
            )
        endpoint = TOOL_ENDPOINTS[request.tool_name]
        scope = StatementCacheScope(
            account_scope=self._config["tushare_account_scope_label"],
            instrument_identity_revision=ProviderSubrequestInstrumentIdentity.from_authoritative(
                self._identity
            ).identity_revision,
            endpoint_id=endpoint,
            transport_identity=sha256(
                self._config["tushare_financial_https_endpoint"].encode()
            ).hexdigest(),
            normalizer_version=NORMALIZER_VERSION,
        )
        start, end = date(request.as_of_date.year - 6, 1, 1), request.as_of_date
        memo_key = (scope.key, start, end)
        if memo_key not in self._responses:
            cache = CurrentStatementCache(
                Path(self._config["data_cache_dir"]) / "current-financial-statements.sqlite3",
                max_age=timedelta(seconds=self._max_age_seconds),
            )
            self._responses[memo_key] = cache.read_through(
                scope=scope,
                range_start=start,
                range_end=end,
                now=self._clock(),
                refresh=self._config["tushare_financial_force_refresh"] is True,
                fetch=lambda lo, hi: self._fetch(scope, lo, hi),
            )
        result = self._responses[memo_key]
        candidates, rejected, unsupported = [], 0, 0
        for chunk in result.chunks:
            payload = decode_provider_artifact(chunk.payload, endpoint_id=endpoint)
            # A refreshed subinterval can split an older response. Project only
            # its still-active rows; never resurrect superseded observations.
            payload = {
                **payload,
                "rows": [
                    row
                    for row in payload["rows"]
                    if _announcement_in_range(row, chunk.range_start, chunk.range_end)
                ],
            }
            key = {
                "contract": NORMALIZER_VERSION,
                "scope": scope.key,
                "announcement_start": chunk.range_start.isoformat(),
                "announcement_end": chunk.range_end.isoformat(),
            }
            artifact = financial_artifact_identity(key=key, payload=payload)
            annual, reporting, row_rejections = project_candidates(
                payload=payload,
                row_artifacts=financial_row_artifact_identities(
                    key=key, response_artifact=artifact, payload=payload
                ),
                statement_type=request.statement_type,
                instrument_identity=self._identity,
                expected_provider_symbol=self._provider_symbol,
            )
            rejected += len(row_rejections)
            for candidate in annual if request.frequency.value == "annual" else reporting:
                if candidate.period_identity.company_type not in {
                    FinancialCompanyType.BANK, FinancialCompanyType.INDUSTRIAL_NON_BANK
                }:
                    unsupported += 1
                    rejected += 1
                    continue
                if candidate.period_identity.period_end > request.as_of_date or (
                    candidate.filing_metadata.f_ann_date is not None
                    and candidate.filing_metadata.f_ann_date > request.as_of_date
                ):
                    rejected += 1
                    continue
                assessment = assess_financial_period_candidate(
                    candidate,
                    requested_capability=FinancialCapability.STATEMENT,
                    requested_statement_type=request.statement_type,
                    requested_ratio_family=None,
                    expected_instrument_identity=self._identity,
                    expected_frequency=request.frequency,
                    expected_currency="CNY",
                    expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                    pit_as_of_date=request.as_of_date,
                    eligibility_policy_version="mainland-financial-period-eligibility-v2",
                )
                if assessment.disposition is FinancialPeriodDisposition.REJECTED:
                    rejected += 1
                else:
                    candidates.append(
                        FinancialProviderCandidate(candidate=candidate, assessment=assessment)
                    )
        groups = defaultdict(list)
        for candidate in candidates:
            groups[financial_revision_selection_key(candidate)].append(candidate)
        selected = []
        for group in groups.values():
            ordered, _ = order_financial_provider_revisions(group)
            top = ordered[0].candidate
            tied = [
                item.candidate
                for item in ordered
                if item.candidate.filing_metadata.ann_date == top.filing_metadata.ann_date
                and item.candidate.artifact.observed_at == top.artifact.observed_at
            ]
            if (
                len(
                    {
                        tuple(
                            (f.normalized_field, f.normalized_value, f.normalized_unit)
                            for f in item.fields
                        )
                        for item in tied
                    }
                )
                > 1
            ):
                rejected += len(tied)
                continue
            selected.append(top)
        selected.sort(key=lambda c: c.period_identity.period_end, reverse=True)
        selected = selected[: 5 if request.frequency.value == "annual" else 8]
        if not selected:
            if result.failure is not None:
                _raise_acquisition_failure(result.failure)
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.EMPTY_FRAME,
                error_code=(
                    "current_financial_unsupported_company_type" if unsupported
                    else "current_financial_ineligible_rows" if rejected
                    else "current_financial_no_eligible_periods"
                ),
                retryable=False,
            )
        return _render(self._identity.symbol, request, selected, result, rejected)

    @property
    def _provider_symbol(self):
        return self._identity.symbol.split(".")[0] + (
            ".SH" if self._identity.venue == "XSHG" else ".SZ"
        )

    def _fetch(self, scope, start, end):
        from tradingagents.dataflows.market_snapshot import (
            _typed_mainland_physical_request,
            record_active_physical_attempt_events,
        )

        rates = (
            self._config["tushare_calls_per_minute"],
            self._config["tushare_operator_safety_ceiling_calls_per_minute"],
        )
        if any(type(rate) is not int or not 1 <= rate <= 40 for rate in rates):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.NOT_CONFIGURED, retryable=False
            )
        client = create_financial_statement_client(self._config["tushare_financial_https_endpoint"])
        history_config = MarketHistoryConfig.from_mapping(self._config)
        with MarketHistoryStore.open_provider_request_authority(history_config) as store:
            coordinator = ProviderRequestCoordinator(store)
            # Match the existing CURRENT market capacity bucket. The persistent
            # response cache remains account-scoped; this shared ceiling is conservative.
            upstream_id, service = upstream_service_identity_for_provider("tushare")
            coordinator.register_upstream_service(upstream_id, service)
            coordinator.configure_operator_ceiling(
                upstream_service_id=upstream_id,
                minimum_interval=timedelta(seconds=60 / min(rates)),
                allow_prewarming=False,
                configured_at=self._clock(),
            )

            def query():
                started_at = self._clock()
                record_active_physical_attempt_io()
                try:
                    frame = client.query(
                        scope.endpoint_id,
                        ts_code=self._provider_symbol,
                        start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"),
                        report_type="1",
                    )
                except PhysicalAttemptFailure as failure:
                    if (
                        failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED
                        and failure.retry_after_seconds is not None
                    ):
                        # HTTP delays start at receipt, whereas the shared coordinator
                        # adds them to its I/O-start timestamp. Capture our start before
                        # its recorder so elapsed time cannot shorten the server wait.
                        elapsed = max(0.0, (self._clock() - started_at).total_seconds())
                        raise PhysicalAttemptFailure(
                            outcome=failure.outcome,
                            retryable=failure.retryable,
                            status_code=failure.status_code,
                            error_code=failure.error_code,
                            retry_after_seconds=failure.retry_after_seconds + elapsed,
                            rate_limit_scope=failure.rate_limit_scope,
                        ) from None
                    raise
                if not isinstance(frame, pd.DataFrame):
                    raise ValueError("invalid statement response")
                if not {"ts_code", "ann_date", "end_date"}.issubset(frame.columns):
                    raise ValueError("invalid statement columns")
                now = self._clock()
                return encode_provider_artifact(
                    endpoint_id=scope.endpoint_id, frame=frame, retrieved_at=now, observed_at=now
                )

            try:
                response = coordinator.execute_physical_workflow(
                    request_key="current-statements:"
                    + sha256(f"{scope.key}:{start}:{end}".encode()).hexdigest(),
                    upstream_service_id=upstream_id,
                    owner_id="financial:" + uuid4().hex,
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=self._clock,
                    sleep=self._sleep,
                    lease_duration=timedelta(minutes=2),
                    max_physical_operations=1,
                    wait_for_initial_pacing=True,
                    workflow=lambda execute: execute(
                        scope.endpoint_id, lambda: _typed_mainland_physical_request(query)
                    ),
                    cooldown_scope="market-snapshot",
                )
            except PhysicalAttemptBudgetExhausted as exc:
                record_active_physical_attempt_events(exc.attempt_events)
                raise exc.failure from None
            record_active_physical_attempt_events(response.attempt_events)
            return response.value


def _announcement_in_range(row, start, end):
    try:
        value = datetime.strptime(str(row.get("ann_date")), "%Y%m%d").date()
        return start <= value <= end and str(row.get("report_type", "1")) == "1"
    except ValueError:
        return False


def _raise_acquisition_failure(failure):
    if isinstance(failure, AcquisitionFailure):
        raise failure
    if isinstance(failure, PhysicalAttemptFailure):
        try:
            reason = AcquisitionUnavailableReason(failure.outcome.value)
        except ValueError:
            reason = AcquisitionUnavailableReason.PROVIDER_ERROR
        raise AcquisitionFailure(
            reason=reason,
            status_code=failure.status_code,
            error_code=failure.error_code,
            retry_after_seconds=failure.retry_after_seconds,
            retryable=False,
        ) from None
    raise AcquisitionFailure(
        reason=AcquisitionUnavailableReason.PROVIDER_ERROR, retryable=False
    ) from None


def _render(symbol, request, selected, result, rejected):
    titles = {
        "get_balance_sheet": "Balance Sheet",
        "get_income_statement": "Income Statement",
        "get_cashflow": "Cash Flow",
    }
    buffer = io.StringIO()
    latest = max(item.artifact.retrieved_at for item in selected).isoformat()
    buffer.write(f"# {titles[request.tool_name]} data for {symbol} ({request.frequency.value})\n")
    buffer.write(f"# Data retrieved on: {latest}\n")
    buffer.write("# CURRENT-only; source=tushare; not strict PIT evidence.\n")
    company_types = {item.period_identity.company_type for item in selected}
    buffer.write("# company_types=" + ",".join(sorted(t.value for t in company_types)) + "\n")
    buffer.write("# value_status=reported includes true zero; missing has no numeric value.\n")
    if FinancialCompanyType.BANK in company_types:
        buffer.write(
            "# bank_not_applicable_metrics=gross_margin,current_ratio,quick_ratio,inventory_turnover\n"
        )
        buffer.write(
            "# bank_unavailable_metrics=net_interest_margin,nonperforming_loan_ratio,capital_adequacy_ratio,provision_coverage_ratio; require separate source, never infer zero.\n"
        )
    buffer.write(
        "# Query coverage is an announcement window, not complete report-period coverage.\n"
    )
    buffer.write(
        "# Income/cashflow quarterly rows retain cumulative report_type=1 values, not standalone-quarter deltas.\n"
    )
    degraded = bool(
        result.failure
        or result.cache_failure
        or result.missing_ranges
        or result.stale_ranges
        or rejected
    )
    buffer.write(
        f"# degraded={str(degraded).lower()}; rejected_rows={rejected}; cache_failure={result.cache_failure or 'none'}\n"
    )
    for name, ranges in (("missing", result.missing_ranges), ("stale", result.stale_ranges)):
        if ranges:
            buffer.write(
                f"# {name}_announcement_ranges="
                + ",".join(f"{lo}/{hi}" for lo, hi in ranges)
                + "\n"
            )
    if result.failure:
        if result.failure_range:
            start, end = result.failure_range
            buffer.write(f"# failure_announcement_range={start}/{end}\n")
        buffer.write(
            "# fetch_failure="
            + (
                result.failure.outcome.value
                if isinstance(result.failure, PhysicalAttemptFailure)
                else "provider_error"
            )
            + "\n"
        )
    writer = csv.DictWriter(
        buffer, lineterminator="\n", fieldnames=[
            "period_end",
            "ann_date",
            "f_ann_date",
            "retrieved_at",
            "observed_at",
            "provider_field",
            "original_value",
            "original_unit",
            "field",
            "value",
            "unit",
            "artifact_identity",
            "value_status",
        ]
    )
    writer.writeheader()
    for item in selected:
        provenance = {
            "period_end": item.period_identity.period_end,
            "ann_date": item.filing_metadata.ann_date,
            "f_ann_date": item.filing_metadata.f_ann_date,
            "retrieved_at": item.artifact.retrieved_at.isoformat(),
            "observed_at": item.artifact.observed_at.isoformat(),
            "artifact_identity": item.artifact.artifact_identity,
        }
        for field in item.fields:
            writer.writerow(
                {**provenance,
                 "provider_field": field.provider_field,
                 "original_value": field.original_value,
                 "original_unit": field.original_unit,
                 "field": field.normalized_field,
                 "value": field.normalized_value,
                 "unit": field.normalized_unit,
                 "value_status": "reported"}
            )
        declared = financial_statement_field_declaration(
            item.period_identity.company_type, request.statement_type,
            declaration_version="financial-field-declarations-tushare-v2",
        )
        missing = set(declared.core_fields) - {field.normalized_field for field in item.fields}
        for name in sorted(missing):
            writer.writerow({**provenance, "field": name, "value_status": "missing"})
    return buffer.getvalue()
