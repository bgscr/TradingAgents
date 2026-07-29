from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from threading import Event

import pandas as pd
import pytest
from pydantic import ValidationError

import tradingagents.dataflows.akshare_data as akshare_data
import tradingagents.dataflows.baostock_data as baostock_data
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.financial_dispatch as financial_dispatch_module
import tradingagents.dataflows.market_snapshot as market_snapshot
import tradingagents.dataflows.y_finance as y_finance
from tradingagents.dataflows.acquisition import AcquisitionFailure, RetryPolicy
from tradingagents.dataflows.errors import VendorRateLimitError
from tradingagents.dataflows.financial_dispatch import (
    FinancialProvider,
    FinancialProviderVariant,
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolMessageAuditEnvelope,
    FinancialToolRequest,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceReadiness,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    SourceAcquisitionAvailable,
    evaluate_preflight_gate,
    merge_source_acquisition_outcomes,
    merge_source_artifacts,
)
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryStore,
    PhysicalAttemptBudgetExhausted,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestPriority,
    upstream_service_identity_for_provider,
)


def _identity(
    *,
    symbol: str = "AAPL",
    venue: str = "XNAS",
    instrument_kind: InstrumentKind = InstrumentKind.EQUITY,
    currency: str = "USD",
    provenance_provider: str = "test-registry",
    provenance_source_ref: str = "registry:test:v1",
    provenance_retrieved_at: str = "2026-07-01T00:00:00Z",
    artifact_sha256: str = "a" * 64,
) -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue=venue,
        instrument_kind=instrument_kind,
        currency=currency,
        provenance=IdentityProvenance(
            provider=provenance_provider,
            source_ref=provenance_source_ref,
            retrieved_at=provenance_retrieved_at,
            artifact_sha256=artifact_sha256,
        ),
        display_name="Display metadata is not identity",
    )


def _coordinator_runtime_config(tmp_path) -> dict:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(
                history_root / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    return runtime_config


def _valid_payload(
    tool_name: str = "get_balance_sheet",
    *,
    symbol: str = "AAPL",
    frequency: FinancialReportingFrequency = FinancialReportingFrequency.QUARTERLY,
) -> str:
    if tool_name == "get_fundamentals":
        return (
            f"# Company Fundamentals for {symbol}\n"
            "# Data retrieved on: 2026-07-28 12:00:00\n\n"
            "Name: Deterministic Test Company\nMarket Cap: 100"
        )
    headings = {
        "get_balance_sheet": "Balance Sheet",
        "get_cashflow": "Cash Flow",
        "get_income_statement": "Income Statement",
    }
    return (
        f"# {headings[tool_name]} data for {symbol} ({frequency.value})\n"
        "# Data retrieved on: 2026-07-28 12:00:00\n\n"
        "metric,2026-06-30\n"
        "Total,100"
    )


def _provider(
    name: str = "yfinance",
    *variant_ids: str,
) -> FinancialProvider:
    variants = variant_ids or ("default",)
    return FinancialProvider(
        name=name,
        variants=tuple(
            FinancialProviderVariant(
                variant_id=variant_id,
                invoke=lambda _request: _valid_payload(),
            )
            for variant_id in variants
        ),
    )


def _dispatcher(
    *,
    identity: InstrumentIdentityEvidence | None = None,
    providers: tuple[FinancialProvider, ...] | None = None,
    acquisition_policy_version: str = "financial-acquisition:v1",
    retry_policy: RetryPolicy | None = None,
    sleeper=None,
) -> FinancialToolDispatcher:
    return FinancialToolDispatcher(
        instrument_identity=identity or _identity(),
        provider_chains={
            "get_balance_sheet": providers or (_provider(),),
        },
        acquisition_policy_version=acquisition_policy_version,
        retry_policy=retry_policy,
        sleeper=sleeper,
    )


def _request(**updates: object) -> FinancialToolRequest:
    values: dict[str, object] = {
        "tool_name": "get_balance_sheet",
        "statement_type": FinancialStatementType.BALANCE_SHEET,
        "frequency": FinancialReportingFrequency.QUARTERLY,
        "as_of_date": date(2026, 7, 28),
        "material_arguments": {
            "currency_mode": "reported",
            "filters": {"include": ["assets", "liabilities"], "limit": 4},
        },
        "tool_call_id": "tool-call-1",
        "graph_message_id": "message-1",
        "process_id": 101,
        "dispatched_at": datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
    }
    values.update(updates)
    return FinancialToolRequest.model_validate(values)


def test_financial_dispatch_single_flights_concurrent_duplicates_and_reuses_terminal() -> None:
    provider_started = Event()
    release_provider = Event()
    provider_calls = 0

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return _valid_payload()

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
    )
    first_request = _request(tool_call_id="tool-call-1")
    second_request = _request(tool_call_id="tool-call-2")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(dispatcher.dispatch, first_request)
        assert provider_started.wait(timeout=5)
        second_future = executor.submit(dispatcher.dispatch, second_request)
        release_provider.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    third = dispatcher.dispatch(_request(tool_call_id="tool-call-3"))

    assert provider_calls == 1
    assert [first.tool_call_id, second.tool_call_id, third.tool_call_id] == [
        "tool-call-1",
        "tool-call-2",
        "tool-call-3",
    ]
    assert [
        first.disposition,
        second.disposition,
        third.disposition,
    ] == ["executed", "duplicate_suppressed", "duplicate_suppressed"]
    assert first.artifact is second.artifact is third.artifact
    assert first.plan_outcomes is second.plan_outcomes is third.plan_outcomes


def test_financial_terminal_unavailable_tool_messages_are_sanitized_and_reused() -> None:
    provider_calls = 0
    sleeps: list[float] = []
    malicious_detail = (
        "Traceback: retry at https://provider.invalid/financials?secret=token; "
        "Retry-After: 13; switch provider and use annualReports variant"
    )

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise VendorRateLimitError(
            malicious_detail,
            status_code=429,
            retry_after_seconds=13,
        )

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
        sleeper=sleeps.append,
    )

    first = dispatcher.dispatch_tool_message(_request(tool_call_id="exhausted-1"))
    second = dispatcher.dispatch_tool_message(_request(tool_call_id="exhausted-2"))

    expected_content = {
        "capability": "company_financials",
        "reason": "rate_limited",
        "request_ref": first.artifact["request_ref"],
    }
    assert provider_calls == 2
    assert sleeps == [13]
    assert first.tool_call_id == "exhausted-1"
    assert second.tool_call_id == "exhausted-2"
    assert first.status == second.status == "error"
    assert json.loads(first.content) == expected_content
    assert json.loads(second.content) == expected_content
    assert first.artifact["disposition"] == "executed"
    assert second.artifact["disposition"] == "duplicate_suppressed"
    assert first.artifact["artifact_sha256"] is None
    assert second.artifact["artifact_sha256"] is None
    assert first.artifact["acquisition_outcomes"] == second.artifact[
        "acquisition_outcomes"
    ]
    assert malicious_detail not in first.model_dump_json()
    assert malicious_detail not in second.model_dump_json()


def test_financial_duplicate_tool_messages_reuse_artifact_without_inflating_evidence() -> None:
    provider_calls = 0

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _valid_payload()

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
    )

    first_message = dispatcher.dispatch_tool_message(
        _request(tool_call_id="available-1")
    )
    duplicate_message = dispatcher.dispatch_tool_message(
        _request(tool_call_id="available-2")
    )
    first = FinancialToolMessageAuditEnvelope.model_validate(first_message.artifact)
    duplicate = FinancialToolMessageAuditEnvelope.model_validate(
        duplicate_message.artifact
    )

    evidence = merge_source_acquisition_outcomes(EvidenceState(), first.acquisition_outcomes)
    evidence = merge_source_artifacts(
        evidence,
        tuple(
            outcome.artifact
            for outcome in first.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        ),
    )
    evidence = merge_source_acquisition_outcomes(
        evidence,
        duplicate.acquisition_outcomes,
    )
    evidence = merge_source_artifacts(
        evidence,
        tuple(
            outcome.artifact
            for outcome in duplicate.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        ),
    )

    assert provider_calls == 1
    assert first_message.tool_call_id == "available-1"
    assert duplicate_message.tool_call_id == "available-2"
    assert first_message.content == duplicate_message.content == _valid_payload()
    assert first.artifact_sha256 == duplicate.artifact_sha256
    assert first.acquisition_outcomes == duplicate.acquisition_outcomes
    assert first.disposition == "executed"
    assert duplicate.disposition == "duplicate_suppressed"
    assert "duplicate_suppressed" not in duplicate_message.content
    assert len(evidence.acquisition_outcomes) == 1
    assert len(evidence.source_artifacts) == 1
    assert evidence.source_facts == ()
    assert evidence.physical_attempt_count == 0


def test_financial_duplicate_cache_preserves_every_request_scoped_material_difference() -> None:
    provider_calls: list[tuple[str, str, date, str]] = []

    def provider(request: FinancialToolRequest) -> object:
        provider_calls.append(
            (
                request.tool_name,
                request.frequency.value,
                request.as_of_date,
                json.dumps(request.material_arguments, sort_keys=True),
            )
        )
        return _valid_payload(
            request.tool_name,
            frequency=request.frequency,
        )

    def configured_provider() -> FinancialProvider:
        return FinancialProvider(
            name="primary",
            variants=(
                FinancialProviderVariant(variant_id="default", invoke=provider),
            ),
        )

    dispatcher = FinancialToolDispatcher(
        instrument_identity=_identity(),
        provider_chains={
            "get_fundamentals": (configured_provider(),),
            "get_balance_sheet": (configured_provider(),),
            "get_cashflow": (configured_provider(),),
            "get_income_statement": (configured_provider(),),
        },
    )
    requests = (
        _request(),
        _request(frequency=FinancialReportingFrequency.ANNUAL),
        _request(as_of_date=date(2026, 7, 29)),
        _request(material_arguments={"currency_mode": "normalized"}),
        _request(
            tool_name="get_cashflow",
            statement_type=FinancialStatementType.CASH_FLOW,
        ),
        _request(
            tool_name="get_income_statement",
            statement_type=FinancialStatementType.INCOME_STATEMENT,
        ),
        _request(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            material_arguments={},
        ),
    )

    results = tuple(dispatcher.dispatch(request) for request in requests)
    duplicate = dispatcher.dispatch(_request(tool_call_id="duplicate-baseline"))

    assert len(provider_calls) == len(requests)
    assert len({result.request_key.request_key for result in results}) == len(requests)
    assert all(result.disposition == "executed" for result in results)
    assert duplicate.disposition == "duplicate_suppressed"
    assert duplicate.request_key == results[0].request_key


def test_financial_duplicate_cache_executes_dispatcher_scoped_key_differences_independently() -> None:
    provider_calls: list[tuple[str, str, str]] = []

    def provider_chain(
        symbol: str,
        *providers: tuple[str, tuple[str, ...]],
    ) -> tuple[FinancialProvider, ...]:
        chain: list[FinancialProvider] = []
        for provider_name, variant_ids in providers:
            variants: list[FinancialProviderVariant] = []
            for variant_id in variant_ids:

                def invoke(
                    _request: FinancialToolRequest,
                    *,
                    selected_provider: str = provider_name,
                    selected_variant: str = variant_id,
                    selected_symbol: str = symbol,
                ) -> object:
                    provider_calls.append(
                        (selected_symbol, selected_provider, selected_variant)
                    )
                    return _valid_payload(symbol=selected_symbol)

                variants.append(
                    FinancialProviderVariant(
                        variant_id=variant_id,
                        invoke=invoke,
                    )
                )
            chain.append(
                FinancialProvider(
                    name=provider_name,
                    variants=tuple(variants),
                )
            )
        return tuple(chain)

    dispatchers = (
        FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL", ("primary", ("default",))
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(symbol="MSFT"),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "MSFT", ("primary", ("default",))
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(artifact_sha256="b" * 64),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL", ("primary", ("default",))
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL", ("alternate", ("default",))
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL",
                    ("alpha", ("default",)),
                    ("beta", ("default",)),
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL", ("primary", ("second", "first"))
                )
            },
        ),
        FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={
                "get_balance_sheet": provider_chain(
                    "AAPL", ("primary", ("default",))
                )
            },
            acquisition_policy_version="financial-acquisition:v2",
        ),
    )

    results = tuple(dispatcher.dispatch(_request()) for dispatcher in dispatchers)

    assert len(provider_calls) == len(dispatchers)
    assert len({result.request_key.request_key for result in results}) == len(dispatchers)
    assert all(result.disposition == "executed" for result in results)


def test_financial_canonical_dispatch_key_excludes_runtime_correlation_metadata() -> None:
    dispatcher = _dispatcher()
    first = dispatcher.canonical_request_key(_request())
    reordered = dispatcher.canonical_request_key(
        _request(
            material_arguments={
                "filters": {"limit": 4, "include": ["assets", "liabilities"]},
                "currency_mode": "reported",
            },
            tool_call_id="different-tool-call",
            graph_message_id="different-message",
            process_id=999,
            dispatched_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert first == reordered
    assert first.instrument_identity.canonical_symbol == "AAPL"
    assert first.instrument_identity.identity_revision == "a" * 64
    assert first.tool_name == "get_balance_sheet"
    assert first.financial_capability == "company_financials"
    assert first.statement_type is FinancialStatementType.BALANCE_SHEET
    assert first.frequency is FinancialReportingFrequency.QUARTERLY
    assert first.as_of_date == date(2026, 7, 28)
    assert first.material_arguments_json == (
        '{"currency_mode":"reported","filters":'
        '{"include":["assets","liabilities"],"limit":4}}'
    )
    assert first.provider_chain_identity.startswith("financial-provider-chain:v1:")
    assert first.acquisition_policy_version == "financial-acquisition:v1"
    assert first.request_key.startswith("financial-request:v1:")


def test_financial_canonical_dispatch_key_changes_for_every_material_dimension() -> None:
    baseline_dispatcher = _dispatcher()
    baseline_request = _request()
    baseline = baseline_dispatcher.canonical_request_key(baseline_request)

    changed_keys = {
        "symbol": _dispatcher(identity=_identity(symbol="MSFT")).canonical_request_key(
            baseline_request
        ),
        "identity_revision": _dispatcher(
            identity=_identity(artifact_sha256="b" * 64)
        ).canonical_request_key(baseline_request),
        "statement_type": FinancialToolDispatcher(
            instrument_identity=_identity(),
            provider_chains={"get_cashflow": (_provider(),)},
        ).canonical_request_key(
            _request(
                tool_name="get_cashflow",
                statement_type=FinancialStatementType.CASH_FLOW,
            )
        ),
        "frequency": baseline_dispatcher.canonical_request_key(
            _request(frequency=FinancialReportingFrequency.ANNUAL)
        ),
        "as_of_date": baseline_dispatcher.canonical_request_key(
            _request(as_of_date=date(2026, 7, 27))
        ),
        "material_argument": baseline_dispatcher.canonical_request_key(
            _request(material_arguments={"currency_mode": "normalized"})
        ),
        "provider_chain": _dispatcher(
            providers=(_provider("alpha_vantage"), _provider("yfinance"))
        ).canonical_request_key(baseline_request),
        "provider_order": _dispatcher(
            providers=(_provider("yfinance"), _provider("alpha_vantage"))
        ).canonical_request_key(baseline_request),
        "variant_order": _dispatcher(
            providers=(_provider("yfinance", "second", "first"),)
        ).canonical_request_key(baseline_request),
        "policy_version": _dispatcher(
            acquisition_policy_version="financial-acquisition:v2"
        ).canonical_request_key(baseline_request),
    }

    assert all(key != baseline for key in changed_keys.values())
    assert len({key.request_key for key in changed_keys.values()}) == len(changed_keys)


@pytest.mark.parametrize(
    ("identity_update", "expected_field"),
    (
        ({"venue": "XNYS"}, "venue"),
        ({"instrument_kind": InstrumentKind.FUND}, "instrument_kind"),
        ({"currency": "CAD"}, "currency"),
        ({"provenance_provider": "other-registry"}, "provenance_provider"),
        ({"provenance_source_ref": "registry:test:v2"}, "provenance_source_ref"),
        (
            {"provenance_retrieved_at": "2026-07-02T00:00:00Z"},
            "provenance_retrieved_at",
        ),
    ),
)
def test_financial_canonical_dispatch_key_changes_for_authoritative_identity_field(
    identity_update: dict[str, object],
    expected_field: str,
) -> None:
    baseline = _dispatcher().canonical_request_key(_request())
    changed = _dispatcher(identity=_identity(**identity_update)).canonical_request_key(
        _request()
    )

    assert changed != baseline, expected_field


def test_financial_dispatch_executes_providers_and_variants_sequentially() -> None:
    calls: list[str] = []

    def unavailable(label: str, reason: AcquisitionUnavailableReason):
        def invoke(_request: FinancialToolRequest) -> object:
            calls.append(label)
            raise AcquisitionFailure(reason=reason)

        return invoke

    def available(_request: FinancialToolRequest) -> object:
        calls.append("fallback:default")
        return _valid_payload()

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(
                        variant_id="first",
                        invoke=unavailable(
                            "primary:first",
                            AcquisitionUnavailableReason.NO_DATA,
                        ),
                    ),
                    FinancialProviderVariant(
                        variant_id="second",
                        invoke=unavailable(
                            "primary:second",
                            AcquisitionUnavailableReason.AUTHENTICATION,
                        ),
                    ),
                ),
            ),
            FinancialProvider(
                name="fallback",
                variants=(
                    FinancialProviderVariant(
                        variant_id="default",
                        invoke=available,
                    ),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    )

    result = dispatcher.dispatch(_request())

    assert calls == ["primary:first", "primary:second", "fallback:default"]
    assert result.value == _valid_payload()
    assert result.provider == "fallback"
    assert result.variant_id == "default"
    assert [
        (item.provider, item.variant_id, item.outcome.outcome)
        for item in result.plan_outcomes
    ] == [
        ("primary", "first", "unavailable"),
        ("primary", "second", "unavailable"),
        ("fallback", "default", "available"),
    ]
    assert result.artifact is result.plan_outcomes[-1].outcome.artifact
    assert result.artifact.raw_text == _valid_payload()


def test_financial_dispatch_shares_one_retry_budget_across_provider_variants() -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def timeout(label: str):
        def invoke(_request: FinancialToolRequest) -> object:
            calls.append(label)
            raise TimeoutError("raw timeout")

        return invoke

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(
                        variant_id="first",
                        invoke=timeout("first"),
                    ),
                    FinancialProviderVariant(
                        variant_id="second",
                        invoke=timeout("second"),
                    ),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=3, backoff_seconds=1),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request())
    repeated = dispatcher.dispatch(_request(as_of_date=date(2026, 7, 29)))

    assert calls == ["first", "second", "second"]
    assert sleeps == [1, 1]
    assert [item.variant_id for item in result.plan_outcomes] == [
        "first",
        "second",
        "second",
    ]
    assert [item.outcome.attempt for item in result.plan_outcomes] == [1, 2, 3]
    assert (
        repeated.plan_outcomes[0].outcome.reason
        is AcquisitionUnavailableReason.CIRCUIT_OPEN
    )


def test_financial_dispatch_honors_retry_after_before_next_variant() -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def first(_request: FinancialToolRequest) -> object:
        calls.append("first")
        raise VendorRateLimitError(status_code=429, retry_after_seconds=5)

    def second(_request: FinancialToolRequest) -> object:
        calls.append("second")
        return _valid_payload()

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="first", invoke=first),
                    FinancialProviderVariant(variant_id="second", invoke=second),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=1),
        sleeper=sleeps.append,
    ).dispatch(_request())

    assert result.artifact is not None
    assert calls == ["first", "second"]
    assert sleeps == [5]


def test_financial_dispatch_retries_raw_timeout_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("raw provider timeout must not escape")
        return _valid_payload()

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=3),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request())

    assert calls == 2
    assert sleeps == [3]
    assert [item.outcome.attempt for item in result.plan_outcomes] == [1, 2]
    assert result.plan_outcomes[0].outcome.reason is AcquisitionUnavailableReason.TIMEOUT
    assert result.artifact is not None


def test_financial_dispatch_uses_real_backoff_by_default(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("retry once")
        return _valid_payload()

    monkeypatch.setattr(financial_dispatch_module.time, "sleep", sleeps.append)
    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=4),
    )

    result = dispatcher.dispatch(_request())

    assert result.artifact is not None
    assert calls == 2
    assert sleeps == [4]


def test_financial_dispatch_exhausts_exact_retry_budget() -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.TIMEOUT)

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=3, backoff_seconds=2),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request())

    assert calls == 3
    assert sleeps == [2, 2]
    assert [item.outcome.attempt for item in result.plan_outcomes] == [1, 2, 3]
    assert all(
        item.outcome.reason is AcquisitionUnavailableReason.TIMEOUT
        for item in result.plan_outcomes
    )
    assert result.value is None
    assert result.artifact is None


def test_financial_dispatch_honors_valid_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VendorRateLimitError(
                "raw provider message is operational only",
                status_code=429,
                retry_after_seconds=7,
            )
        return _valid_payload()

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=1),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request())

    first = result.plan_outcomes[0].outcome
    assert calls == 2
    assert sleeps == [7]
    assert first.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert first.http_status == 429
    assert first.retry_after_seconds == 7
    assert result.artifact is not None


def test_financial_dispatch_stops_nonretryable_failure_without_artifact() -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        raise PermissionError("raw authentication detail")

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=4, backoff_seconds=1),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request())

    assert calls == 1
    assert sleeps == []
    assert len(result.plan_outcomes) == 1
    assert (
        result.plan_outcomes[0].outcome.reason
        is AcquisitionUnavailableReason.AUTHENTICATION
    )
    assert result.artifact is None


def test_financial_dispatch_nonretryable_failure_stops_provider_variants() -> None:
    calls: list[str] = []

    def denied(_request: FinancialToolRequest) -> object:
        calls.append("primary:first")
        raise PermissionError("raw authentication detail")

    def forbidden_variant(_request: FinancialToolRequest) -> object:
        calls.append("primary:second")
        return _valid_payload()

    def fallback(_request: FinancialToolRequest) -> object:
        calls.append("fallback:default")
        return _valid_payload()

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="first", invoke=denied),
                    FinancialProviderVariant(
                        variant_id="second",
                        invoke=forbidden_variant,
                    ),
                ),
            ),
            FinancialProvider(
                name="fallback",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=fallback),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    ).dispatch(_request())

    assert result.artifact is not None
    assert result.provider == "fallback"
    assert calls == ["primary:first", "fallback:default"]


@pytest.mark.parametrize(
    ("tool_name", "statement_type", "frequency", "expected_args"),
    (
        (
            "get_fundamentals",
            FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            FinancialReportingFrequency.NOT_APPLICABLE,
            ("AAPL", "2026-07-28"),
        ),
        (
            "get_balance_sheet",
            FinancialStatementType.BALANCE_SHEET,
            FinancialReportingFrequency.QUARTERLY,
            ("AAPL", "quarterly", "2026-07-28"),
        ),
        (
            "get_cashflow",
            FinancialStatementType.CASH_FLOW,
            FinancialReportingFrequency.ANNUAL,
            ("AAPL", "annual", "2026-07-28"),
        ),
        (
            "get_income_statement",
            FinancialStatementType.INCOME_STATEMENT,
            FinancialReportingFrequency.QUARTERLY,
            ("AAPL", "quarterly", "2026-07-28"),
        ),
    ),
)
def test_every_financial_tool_dispatches_through_configured_provider_chain(
    tool_name: str,
    statement_type: FinancialStatementType,
    frequency: FinancialReportingFrequency,
    expected_args: tuple[str, ...],
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def provider(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return _valid_payload(tool_name, frequency=frequency)

    vendor_methods = {
        name: {"configured": provider}
        for name in (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        )
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(),
        config={"data_vendors": {"fundamental_data": "configured"}},
        vendor_methods=vendor_methods,
    )

    result = dispatcher.dispatch(
        _request(
            tool_name=tool_name,
            statement_type=statement_type,
            frequency=frequency,
            material_arguments={"limit": 4},
        )
    )

    assert calls == [(expected_args, {"limit": 4})]
    assert result.provider == "configured"
    assert result.variant_id == "default"
    assert result.artifact is not None


def test_financial_configured_chain_preserves_explicit_vendor_and_variant_order() -> None:
    calls: list[str] = []

    def unavailable(label: str):
        def provider(*_args: object, **_kwargs: object) -> str:
            calls.append(label)
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)

        return provider

    def available(*_args: object, **_kwargs: object) -> str:
        calls.append("first:default")
        return _valid_payload()

    default_provider = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        name: {"default_only": default_provider}
        for name in (
            "get_fundamentals",
            "get_cashflow",
            "get_income_statement",
        )
    }
    vendor_methods["get_balance_sheet"] = {
        "first": available,
        "second": [unavailable("second:variant-1"), unavailable("second:variant-2")],
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(),
        config={
            "data_vendors": {"fundamental_data": "default_only"},
            "tool_vendors": {
                "get_balance_sheet": "second,not_registered,first",
            },
        },
        vendor_methods=vendor_methods,
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    )

    result = dispatcher.dispatch(_request())

    assert calls == ["second:variant-1", "second:variant-2", "first:default"]
    assert [
        (item.provider, item.variant_id)
        for item in result.plan_outcomes
    ] == [
        ("second", "variant-1"),
        ("second", "variant-2"),
        ("first", "default"),
    ]
    assert result.provider == "first"


def test_financial_default_config_preserves_registered_provider_order() -> None:
    calls: list[str] = []

    def unavailable(name: str):
        def provider(*_args: object, **_kwargs: object) -> str:
            calls.append(name)
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)

        return provider

    def available(*_args: object, **_kwargs: object) -> str:
        calls.append("second")
        return _valid_payload()

    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    vendor_methods = {
        name: {"only": unused}
        for name in ("get_fundamentals", "get_cashflow", "get_income_statement")
    }
    vendor_methods["get_balance_sheet"] = {
        "first": unavailable("first"),
        "second": available,
        "third": unavailable("third"),
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(),
        config={"data_vendors": {"fundamental_data": "default"}},
        vendor_methods=vendor_methods,
    )

    result = dispatcher.dispatch(_request())

    assert calls == ["first", "second"]
    assert result.provider == "second"


def test_authoritative_non_mainland_identity_does_not_use_mainland_vendor_config() -> None:
    def non_mainland(*_args: object, **_kwargs: object) -> str:
        return _valid_payload(symbol="600000.SS")

    def mainland(*_args: object, **_kwargs: object) -> str:
        pytest.fail("ticker syntax overrode authoritative Instrument Identity")

    vendor_methods = {
        name: {"yfinance": non_mainland, "akshare": mainland}
        for name in (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        )
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XNAS",
            currency="USD",
        ),
        config={
            "data_vendors": {"fundamental_data": "yfinance"},
            "market_data_vendors": {
                "cn_a": {"fundamental_data": "akshare"},
            },
        },
        vendor_methods=vendor_methods,
    )

    result = dispatcher.dispatch(_request())

    assert result.provider == "yfinance"
    assert result.artifact is not None


def test_configured_yahoo_financial_dispatch_uses_one_retry_owner(monkeypatch) -> None:
    direct_request_keys: list[str] = []
    sleeps: list[float] = []

    class FakeTicker:
        @property
        def quarterly_balance_sheet(self) -> pd.DataFrame:
            return pd.DataFrame(
                {pd.Timestamp("2026-06-30"): [100]},
                index=["Total Assets"],
            )

    def acquire_once(
        func,
        *,
        request_key: str,
        operation: str,
        **_kwargs: object,
    ):
        assert operation == "fundamentals"
        direct_request_keys.append(request_key)
        if len(direct_request_keys) == 1:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.TIMEOUT,
                retryable=True,
            )
        return func()

    monkeypatch.setattr(y_finance.yf, "Ticker", lambda _symbol: FakeTicker())
    monkeypatch.setattr(y_finance, "yf_acquire_once", acquire_once, raising=False)
    monkeypatch.setattr(
        y_finance,
        "yf_retry",
        lambda *_args, **_kwargs: pytest.fail("nested Yahoo retry owner was called"),
    )
    vendor_methods = {
        "get_fundamentals": {"yfinance": y_finance.get_fundamentals},
        "get_balance_sheet": {"yfinance": y_finance.get_balance_sheet},
        "get_cashflow": {"yfinance": y_finance.get_cashflow},
        "get_income_statement": {"yfinance": y_finance.get_income_statement},
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(),
        config={"data_vendors": {"fundamental_data": "yfinance"}},
        vendor_methods=vendor_methods,
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=1),
        sleeper=sleeps.append,
    )

    result = dispatcher.dispatch(_request(material_arguments={}))

    assert result.artifact is not None
    assert direct_request_keys == [
        result.request_key.request_key,
        result.request_key.request_key,
    ]
    assert sleeps == [1]


def test_configured_akshare_financial_dispatch_does_not_expand_to_yahoo(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _coordinator_runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    yahoo_calls = 0

    def yahoo_supplement(*_args: object, **_kwargs: object):
        nonlocal yahoo_calls
        yahoo_calls += 1
        raise AssertionError("dispatcher-owned AKShare plan expanded to Yahoo")

    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zyjs_ths",
        lambda **_kwargs: pd.DataFrame([{"主营业务": "Semiconductors"}]),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        lambda **_kwargs: pd.DataFrame(
            [{"选项": "常用指标", "指标": "营业收入", "20261231": "100"}]
        ),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        lambda **_kwargs: pd.DataFrame(
            [{"日期": "2026-07-28", "收盘价": 10}]
        ),
    )
    monkeypatch.setattr(akshare_data, "_yahoo_supplemental_section", yahoo_supplement)
    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"akshare": akshare_data.get_fundamentals},
        "get_balance_sheet": {"akshare": unused},
        "get_cashflow": {"akshare": unused},
        "get_income_statement": {"akshare": unused},
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XSHG",
            currency="CNY",
        ),
        config={"data_vendors": {"fundamental_data": "akshare"}},
        vendor_methods=vendor_methods,
    )

    with market_snapshot.authoritative_snapshot_run() as run:
        result = dispatcher.dispatch(
            _request(
                tool_name="get_fundamentals",
                statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
                frequency=FinancialReportingFrequency.NOT_APPLICABLE,
                material_arguments={},
            )
        )
        events = tuple(run.physical_attempt_events)

    assert result.artifact is not None
    assert result.provider == "akshare"
    assert yahoo_calls == 0
    assert len(events) == 3
    assert all(
        event.outcome is PhysicalAttemptOutcome.AVAILABLE for event in events
    )


def test_dispatcher_owned_akshare_subrequests_are_typed_and_cardinality_exact(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _coordinator_runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    calls: list[str] = []
    raw_detail = "AKShare failed with secret=dispatcher-must-redact"

    def business(**_kwargs: object) -> pd.DataFrame:
        calls.append("stock_zyjs_ths")
        return pd.DataFrame([{"主营业务": "Semiconductors"}])

    def financial_abstract(**_kwargs: object) -> pd.DataFrame:
        calls.append("stock_financial_abstract")
        raise RuntimeError(raw_detail)

    def fund_flow(**_kwargs: object) -> pd.DataFrame:
        calls.append("stock_individual_fund_flow")
        return pd.DataFrame([{"日期": "2026-07-28", "收盘价": 10}])

    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", business)
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        financial_abstract,
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        fund_flow,
    )
    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"akshare": akshare_data.get_fundamentals},
        "get_balance_sheet": {"akshare": unused},
        "get_cashflow": {"akshare": unused},
        "get_income_statement": {"akshare": unused},
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XSHG",
            currency="CNY",
        ),
        config={"data_vendors": {"fundamental_data": "akshare"}},
        vendor_methods=vendor_methods,
        retry_policy=RetryPolicy(max_attempts_per_provider=1),
    )

    with market_snapshot.authoritative_snapshot_run() as run:
        result = dispatcher.dispatch(
            _request(
                tool_name="get_fundamentals",
                statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
                frequency=FinancialReportingFrequency.NOT_APPLICABLE,
                material_arguments={},
            )
        )
        events = tuple(run.physical_attempt_events)

    assert calls == ["stock_zyjs_ths", "stock_financial_abstract"]
    assert len(events) == len(calls) == 2
    assert [event.outcome for event in events] == [
        PhysicalAttemptOutcome.AVAILABLE,
        PhysicalAttemptOutcome.PROVIDER_ERROR,
    ]
    assert result.artifact is None
    assert result.plan_outcomes[-1].outcome.reason is (
        AcquisitionUnavailableReason.PROVIDER_ERROR
    )
    assert raw_detail not in result.model_dump_json()


def test_dispatcher_owned_akshare_empty_frame_is_typed_at_physical_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _coordinator_runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    calls: list[str] = []

    def empty(endpoint: str):
        def request(**_kwargs: object) -> pd.DataFrame:
            calls.append(endpoint)
            return pd.DataFrame()

        return request

    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", empty("stock_zyjs_ths"))
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        empty("stock_financial_abstract"),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        empty("stock_individual_fund_flow"),
    )
    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XSHG",
            currency="CNY",
        ),
        config={"data_vendors": {"fundamental_data": "akshare"}},
        vendor_methods={
            "get_fundamentals": {"akshare": akshare_data.get_fundamentals},
            "get_balance_sheet": {"akshare": unused},
            "get_cashflow": {"akshare": unused},
            "get_income_statement": {"akshare": unused},
        },
        retry_policy=RetryPolicy(max_attempts_per_provider=1),
    )

    with market_snapshot.authoritative_snapshot_run() as run:
        result = dispatcher.dispatch(
            _request(
                tool_name="get_fundamentals",
                statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
                frequency=FinancialReportingFrequency.NOT_APPLICABLE,
                material_arguments={},
            )
        )
        events = tuple(run.physical_attempt_events)

    assert calls == ["stock_zyjs_ths"]
    assert [event.outcome for event in events] == [
        PhysicalAttemptOutcome.EMPTY_FRAME
    ]
    assert result.artifact is None
    assert result.plan_outcomes[-1].outcome.reason is (
        AcquisitionUnavailableReason.NO_DATA
    )


def test_dispatcher_owned_akshare_respects_shared_mainland_cooldown(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _coordinator_runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    history_config = MarketHistoryConfig.from_mapping(runtime_config)
    seeded_at = datetime.now(timezone.utc)

    def rate_limited() -> object:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=300,
        )

    with MarketHistoryStore.open_provider_request_authority(history_config) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            "akshare"
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)
        with pytest.raises(PhysicalAttemptBudgetExhausted):
            coordinator.execute_direct_physical_request(
                request_key="market-snapshot:seeded-rate-limit",
                upstream_service_id=upstream_service_id,
                owner_id="market-snapshot:seed",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: seeded_at,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(minutes=2),
                operation="market-snapshot:seed",
                physical_request=rate_limited,
                cooldown_scope="market-snapshot",
            )

    physical_calls: list[str] = []

    def unexpected_request(**_kwargs: object) -> pd.DataFrame:
        physical_calls.append("called")
        return pd.DataFrame([{"主营业务": "must not be requested"}])

    monkeypatch.setattr(akshare_data.ak, "stock_zyjs_ths", unexpected_request)
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        unexpected_request,
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        unexpected_request,
    )
    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XSHG",
            currency="CNY",
        ),
        config={"data_vendors": {"fundamental_data": "akshare"}},
        vendor_methods={
            "get_fundamentals": {"akshare": akshare_data.get_fundamentals},
            "get_balance_sheet": {"akshare": unused},
            "get_cashflow": {"akshare": unused},
            "get_income_statement": {"akshare": unused},
        },
        retry_policy=RetryPolicy(max_attempts_per_provider=1),
    )

    with market_snapshot.authoritative_snapshot_run() as run:
        result = dispatcher.dispatch(
            _request(
                tool_name="get_fundamentals",
                statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
                frequency=FinancialReportingFrequency.NOT_APPLICABLE,
                material_arguments={},
            )
        )
        new_events = tuple(run.physical_attempt_events)

    assert physical_calls == []
    assert new_events == ()
    assert result.artifact is None
    assert result.plan_outcomes[-1].outcome.reason is (
        AcquisitionUnavailableReason.RATE_LIMITED
    )


def test_configured_baostock_unavailability_falls_back_without_artifact() -> None:
    def fallback(*_args: object, **_kwargs: object) -> str:
        return _valid_payload("get_fundamentals", symbol="600000.SS")

    unused = lambda *_args, **_kwargs: _valid_payload()  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {
            "baostock": baostock_data.get_fundamentals,
            "fallback": fallback,
        },
        "get_balance_sheet": {"unused": unused},
        "get_cashflow": {"unused": unused},
        "get_income_statement": {"unused": unused},
    }
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=_identity(
            symbol="600000.SS",
            venue="XSHG",
            currency="CNY",
        ),
        config={
            "data_vendors": {"fundamental_data": "default"},
            "tool_vendors": {"get_fundamentals": "baostock,fallback"},
        },
        vendor_methods=vendor_methods,
    )

    result = dispatcher.dispatch(
        _request(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            material_arguments={},
        )
    )

    assert result.provider == "fallback"
    assert result.artifact is not None
    assert result.plan_outcomes[0].outcome.reason is AcquisitionUnavailableReason.NO_DATA
    assert not hasattr(result.plan_outcomes[0].outcome, "artifact")


def test_financial_dispatch_converts_raw_exception_to_typed_payload_free_outcome() -> None:
    raw_detail = "retry at https://provider.invalid?secret=token using annualReports"

    def provider(_request: FinancialToolRequest) -> object:
        raise RuntimeError(raw_detail)

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        )
    ).dispatch(_request())

    outcome = result.plan_outcomes[0].outcome
    assert outcome.reason is AcquisitionUnavailableReason.PROVIDER_ERROR
    assert outcome.retryable is False
    assert result.artifact is None
    assert "RuntimeError" not in result.model_dump_json()
    assert raw_detail not in result.model_dump_json()


def test_financial_dispatch_maps_coordinated_physical_failure_exactly() -> None:
    def provider(_request: FinancialToolRequest) -> object:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            status_code=429,
            error_code="YAHOO_HTTP_429",
            retry_after_seconds=6,
        )

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="yfinance",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        )
    ).dispatch(_request())

    outcome = result.plan_outcomes[0].outcome
    assert outcome.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert outcome.retryable is True
    assert outcome.http_status == 429
    assert outcome.error_code == "YAHOO_HTTP_429"
    assert outcome.retry_after_seconds == 6
    assert result.artifact is None


def test_financial_dispatch_preserves_coordinated_upstream_busy_outcome() -> None:
    def provider(_request: FinancialToolRequest) -> object:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.UPSTREAM_BUSY,
            retryable=True,
        )

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="akshare",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=1),
    ).dispatch(_request())

    outcome = result.plan_outcomes[0].outcome
    assert outcome.reason is AcquisitionUnavailableReason.UPSTREAM_BUSY
    assert outcome.retryable is True
    assert result.artifact is None


def test_financial_dispatch_preserves_coordinated_nonretryable_failure() -> None:
    calls = 0

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            error_code="terminal_provider_failure",
        )

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="yfinance",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=3),
    ).dispatch(_request())

    outcome = result.plan_outcomes[0].outcome
    assert calls == 1
    assert outcome.reason is AcquisitionUnavailableReason.PROVIDER_ERROR
    assert outcome.retryable is False
    assert outcome.error_code == "terminal_provider_failure"
    assert result.artifact is None


@pytest.mark.parametrize(
    "payload",
    (
        "arbitrary nonblank text",
        '{"Error Message":"Invalid API call"}',
        '{"quarterlyReports":"not-a-list"}',
        '{"quarterlyReports":[{"fiscalDateEnding":"2026-06-30"}]}',
        '{"quarterlyReports":[{"fiscalDateEnding":"2030-01-01"}]}',
        (
            '{"symbol":"MSFT","quarterlyReports":'
            '[{"fiscalDateEnding":"2026-06-30","totalAssets":"100"}]}'
        ),
    ),
)
def test_financial_terminal_validation_rejects_untrusted_payloads(payload: str) -> None:
    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(
                        variant_id="default",
                        invoke=lambda _request: payload,
                    ),
                ),
            ),
        )
    ).dispatch(_request())

    assert result.artifact is None
    assert result.value is None
    assert (
        result.plan_outcomes[0].outcome.reason
        in {
            AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            AcquisitionUnavailableReason.PROVIDER_ERROR,
        }
    )


@pytest.mark.parametrize(
    ("tool_request", "payload"),
    (
        (
            _request(
                tool_name="get_fundamentals",
                statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
                frequency=FinancialReportingFrequency.NOT_APPLICABLE,
                material_arguments={},
            ),
            '{"Symbol":"AAPL","Name":"Deterministic Test Company"}',
        ),
        (
            _request(material_arguments={}),
            (
                '{"symbol":"AAPL","quarterlyReports":'
                '[{"fiscalDateEnding":"2026-06-30","totalAssets":"100"}]}'
            ),
        ),
    ),
)
def test_financial_terminal_validation_accepts_provider_json(
    tool_request: FinancialToolRequest,
    payload: str,
) -> None:
    provider = FinancialProvider(
        name="alpha_vantage",
        variants=(
            FinancialProviderVariant(
                variant_id="default",
                invoke=lambda _request: payload,
            ),
        ),
    )
    result = FinancialToolDispatcher(
        instrument_identity=_identity(),
        provider_chains={tool_request.tool_name: (provider,)},
    ).dispatch(tool_request)

    assert result.artifact is not None
    assert result.value == payload


def test_financial_dispatch_sanitizes_invalid_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VendorRateLimitError(
                status_code=429,
                retry_after_seconds=float("nan"),
            )
        return _valid_payload()

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=2),
        sleeper=sleeps.append,
    ).dispatch(_request())

    assert result.artifact is not None
    assert result.plan_outcomes[0].outcome.retry_after_seconds is None
    assert sleeps == [2]


def test_financial_dispatch_discards_invalid_vendor_error_code_without_losing_retry() -> None:
    calls = 0
    sleeps: list[float] = []
    raw_error_code = "rate limit secret=must-not-cross-boundary"

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VendorRateLimitError(
                status_code=429,
                error_code=raw_error_code,
                retry_after_seconds=7,
            )
        return _valid_payload()

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(
                    FinancialProviderVariant(variant_id="default", invoke=provider),
                ),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
        sleeper=sleeps.append,
    ).dispatch(_request())

    first = result.plan_outcomes[0].outcome
    assert calls == 2
    assert first.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert first.retryable is True
    assert first.http_status == 429
    assert first.error_code is None
    assert first.retry_after_seconds == 7
    assert sleeps == [7]
    assert result.artifact is not None
    assert raw_error_code not in result.model_dump_json()


def test_financial_unavailable_result_creates_no_artifact_or_source_fact() -> None:
    def provider(_request: FinancialToolRequest) -> object:
        return "Error retrieving balance sheet for AAPL: raw provider text"

    result = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        )
    ).dispatch(_request())
    evidence = EvidenceState(
        instrument_identity=_identity(),
        acquisition_outcomes=result.outcomes,
    )
    preflight = evaluate_preflight_gate(evidence)

    assert result.artifact is None
    assert not any(
        isinstance(outcome, SourceAcquisitionAvailable)
        for outcome in result.outcomes
    )
    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert preflight.readiness is EvidenceReadiness.INSUFFICIENT


def test_financial_available_result_creates_one_validated_immutable_artifact() -> None:
    result = _dispatcher().dispatch(_request())

    available = tuple(
        outcome
        for outcome in result.outcomes
        if isinstance(outcome, SourceAcquisitionAvailable)
    )
    assert len(available) == 1
    assert available[0].artifact is result.artifact
    assert result.artifact is not None
    assert result.artifact.artifact_sha256 == sha256(
        _valid_payload().encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValidationError):
        result.artifact.raw_text = "mutated"  # type: ignore[misc]


def test_financial_dispatch_respects_run_scoped_circuit_state() -> None:
    calls = 0

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.TIMEOUT)

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        )
    )
    first = dispatcher.dispatch(_request())
    second = dispatcher.dispatch(_request(as_of_date=date(2026, 7, 29)))

    assert calls == 1
    assert first.plan_outcomes[0].outcome.reason is AcquisitionUnavailableReason.TIMEOUT
    assert (
        second.plan_outcomes[0].outcome.reason
        is AcquisitionUnavailableReason.CIRCUIT_OPEN
    )
    assert first.request_key != second.request_key
    assert second.artifact is None


@pytest.mark.parametrize(
    "reason",
    (
        AcquisitionUnavailableReason.NO_DATA,
        AcquisitionUnavailableReason.MALFORMED_RESPONSE,
    ),
)
@pytest.mark.parametrize(
    "second_request",
    (
        _request(frequency=FinancialReportingFrequency.ANNUAL),
        _request(as_of_date=date(2026, 7, 29)),
    ),
    ids=("different-frequency", "different-as-of-date"),
)
def test_request_specific_failure_does_not_poison_distinct_financial_request(
    reason: AcquisitionUnavailableReason,
    second_request: FinancialToolRequest,
) -> None:
    calls = 0

    def provider(_request: FinancialToolRequest) -> object:
        nonlocal calls
        calls += 1
        raise AcquisitionFailure(reason=reason)

    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=1),
    )

    first = dispatcher.dispatch(_request())
    second = dispatcher.dispatch(second_request)

    assert calls == 2
    assert first.plan_outcomes[-1].outcome.reason is reason
    assert second.plan_outcomes[-1].outcome.reason is reason
    assert first.request_key != second.request_key


def test_financial_dispatch_retries_use_immutable_canonical_arguments() -> None:
    seen_arguments: list[dict[str, object]] = []

    def provider(request: FinancialToolRequest) -> object:
        seen_arguments.append(dict(request.material_arguments))
        request.material_arguments["limit"] = 999
        if len(seen_arguments) == 1:
            raise TimeoutError("retry with the canonical plan")
        return _valid_payload()

    request = _request(material_arguments={"limit": 4})
    dispatcher = _dispatcher(
        providers=(
            FinancialProvider(
                name="primary",
                variants=(FinancialProviderVariant(variant_id="default", invoke=provider),),
            ),
        ),
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    )

    result = dispatcher.dispatch(request)

    assert seen_arguments == [{"limit": 4}, {"limit": 4}]
    assert request.material_arguments == {"limit": 4}
    assert result.request_key.material_arguments_json == '{"limit":4}'
    assert result.artifact is not None


@pytest.mark.parametrize("routing_argument", ("provider", "vendor", "variant"))
def test_financial_request_cannot_select_or_expand_acquisition_plan(
    routing_argument: str,
) -> None:
    with pytest.raises(ValidationError, match="reserved routing fields"):
        _request(material_arguments={routing_argument: "model-selected"})
