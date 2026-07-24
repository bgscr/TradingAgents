from __future__ import annotations

import copy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.agents.utils.agent_utils import resolve_instrument_identity
from tradingagents.dataflows import market_snapshot
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.instrument_identity import (
    AuthoritativeInstrumentIdentity,
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    RegistryFailureReason,
    resolve_authoritative_instrument_identity,
)
from tradingagents.dataflows.market_snapshot import SnapshotProvider, authoritative_snapshot_run
from tradingagents.decision_policy import DecisionPolicyEngine
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    InstrumentKind,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    acquire_run_evidence,
    stable_acquisition_source_ref,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.strategy_registry import (
    DEFAULT_DECISION_HORIZON,
    MARKET_RETURN_FIELD,
    MARKET_RETURN_IMPLEMENTATION_VERSION,
    MARKET_RETURN_OBSERVATIONS,
    MARKET_RETURN_UNIT,
    create_production_decision_policy,
)

FIXTURE = Path(__file__).parent / "fixtures" / "identity_registry_510500.synthetic.json"


def _fixture_digest() -> str:
    return sha256(FIXTURE.read_bytes()).hexdigest()


def _synthetic_registry_lookup(
    *,
    canonical_symbol: str,
    instrument_kind: str,
) -> IdentityRegistryAvailable:
    raw_artifact = "{}"
    registry_digest = sha256(raw_artifact.encode()).hexdigest()
    return IdentityRegistryAvailable(
        identity=AuthoritativeInstrumentIdentity(
            canonical_symbol=canonical_symbol,
            venue="XSHG",
            instrument_kind=instrument_kind,
            currency="CNY",
            provenance_provider="synthetic-registry",
            provenance_source_ref="registry:synthetic",
            provenance_retrieved_at="2026-07-18T00:00:00+00:00",
            artifact_sha256=registry_digest,
        ),
        registry_sha256=registry_digest,
        registry_source_ref="registry:synthetic",
        raw_artifact=raw_artifact,
    )


def _valid_market_frame(rows: int) -> pd.DataFrame:
    closes = [100.0] * rows
    return pd.DataFrame(
        {
            "Date": pd.bdate_range(end="2026-07-17", periods=rows),
            "Open": closes,
            "High": [value + 1 for value in closes],
            "Low": [value - 1 for value in closes],
            "Close": closes,
            "Volume": [100.0] * rows,
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize("alias", ["510500", "510500.SH", "510500.SS"])
def test_aliases_resolve_to_the_same_digest_pinned_identity(alias):
    result = resolve_authoritative_instrument_identity(
        alias,
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.identity.canonical_symbol == "510500.SS"
    assert result.identity.venue == "XSHG"
    assert result.identity.instrument_kind == "fund"
    assert result.identity.currency == "CNY"
    assert result.identity.artifact_sha256 == _fixture_digest()


@pytest.mark.unit
def test_routing_normalization_does_not_establish_authority_without_registry():
    result = resolve_authoritative_instrument_identity(
        "510500.SH",
        registry_path="",
        expected_sha256="",
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED


@pytest.mark.unit
def test_wrong_digest_fails_closed_before_parsing_registry():
    result = resolve_authoritative_instrument_identity(
        "510500.SS",
        registry_path=FIXTURE,
        expected_sha256="0" * 64,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE


@pytest.mark.unit
def test_yahoo_exception_continues_mainland_enrichment_without_authority():
    resolve_instrument_identity.cache_clear()
    unavailable = IdentityRegistryUnavailable(
        reason=RegistryFailureReason.NOT_CONFIGURED,
        source_ref="identity-registry:unconfigured",
        diagnostic_code="registry_path_or_digest_missing",
    )
    with (
        patch(
            "tradingagents.agents.utils.agent_utils.resolve_authoritative_instrument_identity",
            return_value=unavailable,
        ),
        patch(
            "tradingagents.agents.utils.agent_utils.yf.Ticker",
            side_effect=RuntimeError("rate limited"),
        ),
        patch(
            "tradingagents.agents.utils.agent_utils.get_china_a_identity",
            return_value={
                "company_name": "Provider label only",
                "industry": "Provider description",
                "exchange": "shanghai",
            },
        ) as mainland,
    ):
        identity = resolve_instrument_identity("510500.SH")

    mainland.assert_called_once_with("510500.SH")
    assert identity["company_name"] == "Provider label only"
    assert "canonical_symbol" not in identity
    assert "provenance" not in identity


@pytest.mark.unit
def test_acquisition_uses_canonical_symbol_and_preserves_registry_artifact():
    lookup = resolve_authoritative_instrument_identity(
        "510500",
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )
    assert isinstance(lookup, IdentityRegistryAvailable)
    frame = pd.DataFrame(
        {
            "Date": [pd.Timestamp("2026-07-17")],
            "Open": [1.0],
            "High": [1.1],
            "Low": [0.9],
            "Close": [1.05],
            "Volume": [100.0],
        }
    )
    snapshot = SimpleNamespace(
        symbol="510500.SS",
        provider="synthetic-market-provider",
        retrieved_at="2026-07-19T01:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-19",
        effective_trading_date="2026-07-17",
        frame=frame,
        frame_sha256="a" * 64,
        snapshot_id="snapshot:synthetic",
    )

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch(
            "tradingagents.dataflows.market_snapshot.get_authoritative_market_snapshot",
            return_value=snapshot,
        ) as market,
    ):
        evidence = acquire_run_evidence("510500", "2026-07-19")

    assert market.call_args.args[0] == "510500.SS"
    assert evidence.instrument_identity is not None
    assert evidence.instrument_identity.symbol == "510500.SS"
    assert evidence.instrument_identity.is_authoritative
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.symbol == "510500.SS"
    assert len(evidence.source_artifacts) == 1
    assert isinstance(evidence.acquisition_outcomes[0], SourceAcquisitionAvailable)
    with patch(
        "tradingagents.graph.trading_graph.resolve_instrument_identity"
    ) as enrichment:
        context = TradingAgentsGraph.resolve_instrument_context(
            SimpleNamespace(),
            "510500",
            evidence_state=evidence,
        )
    enrichment.assert_not_called()
    assert "authoritative canonical symbol `510500.SS`" in context
    assert "Instrument kind: fund" in context


@pytest.mark.unit
def test_unresolved_identity_has_typed_outcome_and_skips_market_acquisition():
    unavailable = IdentityRegistryUnavailable(
        reason=RegistryFailureReason.NOT_FOUND,
        source_ref=str(FIXTURE),
        diagnostic_code="identity_row_not_found",
    )
    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=unavailable,
        ),
        patch(
            "tradingagents.agents.utils.agent_utils.resolve_instrument_identity",
            return_value={},
        ) as enrichment,
        patch(
            "tradingagents.dataflows.market_snapshot.get_authoritative_market_snapshot"
        ) as market,
    ):
        evidence = acquire_run_evidence("UNKNOWN", "2026-07-19")

    market.assert_not_called()
    enrichment.assert_not_called()
    assert evidence.instrument_identity is None
    assert evidence.market_snapshot is None
    assert evidence.source_artifacts == ()
    outcome = evidence.acquisition_outcomes[0]
    assert isinstance(outcome, SourceAcquisitionUnavailable)
    assert outcome.reason is AcquisitionUnavailableReason.IDENTITY_NOT_FOUND
    assert outcome.retryable is False
    assert outcome.source_ref == stable_acquisition_source_ref(
        "identity-registry",
        str(FIXTURE),
        RegistryFailureReason.NOT_FOUND.value,
    )


@pytest.mark.unit
def test_run_evidence_preserves_total_market_rate_limit_without_artifact_or_available():
    lookup = resolve_authoritative_instrument_identity(
        "510500",
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )
    assert isinstance(lookup, IdentityRegistryAvailable)
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "primary,secondary"},
        }
    })

    def rate_limited(*_args):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
        )

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "primary": SnapshotProvider(rate_limited, "qfq"),
                "secondary": SnapshotProvider(rate_limited, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("510500", "2026-07-19")

    market_outcomes = [
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_snapshot"
    ]
    assert [outcome.reason for outcome in market_outcomes] == [
        AcquisitionUnavailableReason.RATE_LIMITED,
        AcquisitionUnavailableReason.RATE_LIMITED,
    ]
    assert evidence.market_snapshot is None
    assert all(
        artifact.tool_name != "authoritative_market_snapshot_normalized_frame_v1"
        for artifact in evidence.source_artifacts
    )
    assert not any(
        outcome.outcome == "available" for outcome in market_outcomes
    )


@pytest.mark.unit
def test_equity_run_evidence_falls_back_until_strategy_history_is_satisfied():
    registry_digest = sha256(b"{}").hexdigest()
    lookup = IdentityRegistryAvailable(
        identity=AuthoritativeInstrumentIdentity(
            canonical_symbol="600895.SS",
            venue="XSHG",
            instrument_kind="equity",
            currency="CNY",
            provenance_provider="synthetic-registry",
            provenance_source_ref="registry:synthetic",
            provenance_retrieved_at="2026-07-18T00:00:00+00:00",
            artifact_sha256=registry_digest,
            display_name="Synthetic Equity",
        ),
        registry_sha256=registry_digest,
        registry_source_ref="registry:synthetic",
        raw_artifact="{}",
    )
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config(
        {
            "market_data_vendors": {
                "cn_a": {"core_stock_apis": "primary,secondary"},
            }
        }
    )
    provider_calls: list[str] = []

    def frame(rows: int) -> pd.DataFrame:
        closes = [100.0] * rows
        return pd.DataFrame(
            {
                "Date": pd.bdate_range(end="2026-07-17", periods=rows),
                "Open": closes,
                "High": [value + 1 for value in closes],
                "Low": [value - 1 for value in closes],
                "Close": closes,
                "Volume": [100.0] * rows,
            }
        )

    def primary(*_args):
        provider_calls.append("primary")
        return frame(1)

    def secondary(*_args):
        provider_calls.append("secondary")
        return frame(MARKET_RETURN_OBSERVATIONS)

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "primary": SnapshotProvider(primary, "qfq"),
                "secondary": SnapshotProvider(secondary, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("600895", "2026-07-19")

    assert provider_calls == ["primary", "secondary"]
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.provider == "secondary"
    assert evidence.market_snapshot.history_rows == MARKET_RETURN_OBSERVATIONS
    market_outcomes = [
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_snapshot"
    ]
    assert [outcome.provider for outcome in market_outcomes] == [
        "primary",
        "secondary",
    ]
    assert all(
        isinstance(outcome, SourceAcquisitionAvailable)
        for outcome in market_outcomes
    )


@pytest.mark.unit
def test_graph_evidence_resolution_uses_its_injected_policy_history_floor():
    registry_digest = sha256(b"{}").hexdigest()
    lookup = IdentityRegistryAvailable(
        identity=AuthoritativeInstrumentIdentity(
            canonical_symbol="600895.SS",
            venue="XSHG",
            instrument_kind="equity",
            currency="CNY",
            provenance_provider="synthetic-registry",
            provenance_source_ref="registry:synthetic",
            provenance_retrieved_at="2026-07-18T00:00:00+00:00",
            artifact_sha256=registry_digest,
        ),
        registry_sha256=registry_digest,
        registry_source_ref="registry:synthetic",
        raw_artifact="{}",
    )
    production_policy = create_production_decision_policy()
    required_rows = MARKET_RETURN_OBSERVATIONS + 1
    custom_policy = DecisionPolicyEngine(
        rules=tuple(
            rule.model_copy(update={"minimum_history_rows": required_rows})
            for rule in production_policy.rules
        )
    )
    graph = SimpleNamespace(
        decision_policy=custom_policy,
        decision_horizon=DEFAULT_DECISION_HORIZON,
    )
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config(
        {
            "market_data_vendors": {
                "cn_a": {"core_stock_apis": "primary,secondary"},
            }
        }
    )
    provider_calls: list[str] = []

    def frame(rows: int) -> pd.DataFrame:
        closes = [100.0] * rows
        return pd.DataFrame(
            {
                "Date": pd.bdate_range(end="2026-07-17", periods=rows),
                "Open": closes,
                "High": [value + 1 for value in closes],
                "Low": [value - 1 for value in closes],
                "Close": closes,
                "Volume": [100.0] * rows,
            }
        )

    def primary(*_args):
        provider_calls.append("primary")
        return frame(MARKET_RETURN_OBSERVATIONS)

    def secondary(*_args):
        provider_calls.append("secondary")
        return frame(required_rows)

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "primary": SnapshotProvider(primary, "qfq"),
                "secondary": SnapshotProvider(secondary, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = TradingAgentsGraph.resolve_evidence_state(
            graph,
            "600895",
            "2026-07-19",
        )

    assert provider_calls == ["primary", "secondary"]
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.provider == "secondary"
    assert evidence.market_snapshot.history_rows == required_rows


@pytest.mark.unit
def test_equity_primary_with_exact_required_history_stops_fallback():
    lookup = _synthetic_registry_lookup(
        canonical_symbol="600895.SS",
        instrument_kind="equity",
    )
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config(
        {
            "market_data_vendors": {
                "cn_a": {"core_stock_apis": "primary,secondary"},
            }
        }
    )
    provider_calls: list[str] = []

    def primary(*_args):
        provider_calls.append("primary")
        return _valid_market_frame(MARKET_RETURN_OBSERVATIONS)

    def secondary(*_args):
        provider_calls.append("secondary")
        return _valid_market_frame(MARKET_RETURN_OBSERVATIONS + 1)

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "primary": SnapshotProvider(primary, "qfq"),
                "secondary": SnapshotProvider(secondary, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("600895", "2026-07-19")

    assert provider_calls == ["primary"]
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.provider == "primary"
    assert evidence.market_snapshot.history_rows == MARKET_RETURN_OBSERVATIONS


@pytest.mark.unit
def test_equity_fallback_skips_short_and_malformed_before_valid_candidate():
    lookup = _synthetic_registry_lookup(
        canonical_symbol="600895.SS",
        instrument_kind="equity",
    )
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config(
        {
            "market_data_vendors": {
                "cn_a": {"core_stock_apis": "short,malformed,valid"},
            }
        }
    )
    provider_calls: list[str] = []

    def short(*_args):
        provider_calls.append("short")
        return _valid_market_frame(1)

    def malformed(*_args):
        provider_calls.append("malformed")
        return pd.DataFrame({"Date": ["2026-07-17"], "Open": [100.0]})

    def valid(*_args):
        provider_calls.append("valid")
        return _valid_market_frame(MARKET_RETURN_OBSERVATIONS)

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "short": SnapshotProvider(short, "qfq"),
                "malformed": SnapshotProvider(malformed, "qfq"),
                "valid": SnapshotProvider(valid, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("600895", "2026-07-19")

    assert provider_calls == ["short", "malformed", "valid"]
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.provider == "valid"
    market_outcomes = [
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_snapshot"
    ]
    assert [outcome.provider for outcome in market_outcomes] == [
        "short",
        "malformed",
        "valid",
    ]
    assert isinstance(market_outcomes[0], SourceAcquisitionAvailable)
    assert isinstance(market_outcomes[1], SourceAcquisitionUnavailable)
    assert (
        market_outcomes[1].reason
        is AcquisitionUnavailableReason.MALFORMED_RESPONSE
    )
    assert isinstance(market_outcomes[2], SourceAcquisitionAvailable)


@pytest.mark.unit
def test_fund_without_strategy_rule_uses_baseline_acquisition_and_fails_closed():
    lookup = _synthetic_registry_lookup(
        canonical_symbol="510500.SS",
        instrument_kind="fund",
    )
    policy = create_production_decision_policy()
    assert (
        policy.preflight_minimum_history_rows(
            InstrumentKind.FUND,
            DEFAULT_DECISION_HORIZON,
        )
        is None
    )
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config(
        {
            "market_data_vendors": {
                "cn_a": {"core_stock_apis": "primary,secondary"},
            }
        }
    )
    provider_calls: list[str] = []

    def primary(*_args):
        provider_calls.append("primary")
        return _valid_market_frame(1)

    def secondary(*_args):
        provider_calls.append("secondary")
        return _valid_market_frame(MARKET_RETURN_OBSERVATIONS)

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {
                "primary": SnapshotProvider(primary, "qfq"),
                "secondary": SnapshotProvider(secondary, "qfq"),
            },
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence(
            "510500",
            "2026-07-19",
            decision_policy=policy,
            decision_horizon=DEFAULT_DECISION_HORIZON,
        )

    assert provider_calls == ["primary"]
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.provider == "primary"
    assert evidence.market_snapshot.history_rows == 1
    calculation_outcome = next(
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_return_20d"
    )
    assert isinstance(calculation_outcome, SourceAcquisitionUnavailable)
    assert (
        calculation_outcome.reason
        is AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
    )


@pytest.mark.unit
def test_run_evidence_contains_exact_normalized_snapshot_artifact_before_preflight():
    lookup = resolve_authoritative_instrument_identity(
        "510500",
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )
    assert isinstance(lookup, IdentityRegistryAvailable)
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {"cn_a": {"core_stock_apis": "synthetic"}}
    })
    frame = pd.DataFrame({
        "Date": ["2026-07-17"],
        "Open": [1.0],
        "High": [1.1],
        "Low": [0.9],
        "Close": [1.05],
        "Volume": [100.0],
    })

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"synthetic": SnapshotProvider(lambda *_args: frame, "qfq")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("510500", "2026-07-19")

    assert evidence.market_snapshot is not None
    market_outcome = next(
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_snapshot"
    )
    assert isinstance(market_outcome, SourceAcquisitionAvailable)
    assert market_outcome.artifact.tool_name == (
        "authoritative_market_snapshot_normalized_frame_v1"
    )
    assert market_outcome.artifact.raw_text == (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-07-17,1,1.1000000000000001,0.90000000000000002,"
        "1.05,100\n"
    )
    assert market_outcome.artifact.artifact_sha256 == (
        evidence.market_snapshot.frame_sha256
    )
    assert sum(
        artifact.artifact_sha256 == evidence.market_snapshot.frame_sha256
        for artifact in evidence.source_artifacts
    ) == 1
    calculation_outcome = next(
        outcome
        for outcome in evidence.acquisition_outcomes
        if outcome.capability == "market_return_20d"
    )
    assert isinstance(calculation_outcome, SourceAcquisitionUnavailable)
    assert calculation_outcome.reason is AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
    assert calculation_outcome.calculation_readiness is not None
    assert calculation_outcome.calculation_readiness.required_observations == (
        MARKET_RETURN_OBSERVATIONS
    )
    assert calculation_outcome.calculation_readiness.available_observations == 1
    assert not any(
        fact.canonical_field == MARKET_RETURN_FIELD for fact in evidence.source_facts
    )


@pytest.mark.unit
def test_run_telemetry_includes_identity_market_and_calculation_outcomes():
    lookup = resolve_authoritative_instrument_identity(
        "510500",
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )
    assert isinstance(lookup, IdentityRegistryAvailable)
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {"cn_a": {"core_stock_apis": "synthetic"}}
    })
    frame = pd.DataFrame({
        "Date": ["2026-07-17"],
        "Open": [1.0],
        "High": [1.1],
        "Low": [0.9],
        "Close": [1.05],
        "Volume": [100.0],
    })

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"synthetic": SnapshotProvider(lambda *_args: frame, "qfq")},
        ),
        authoritative_snapshot_run() as run,
    ):
        evidence = acquire_run_evidence("510500", "2026-07-19")
        telemetry = run.telemetry_ledger.finalize()

    assert {
        outcome.capability for outcome in evidence.acquisition_outcomes
    } == {
        event.outcome.capability for event in telemetry.acquisition.events
    } == {
        "instrument_identity",
        "market_snapshot",
        "market_return_20d",
    }
    assert telemetry.acquisition.summary.attempts == len(
        evidence.acquisition_outcomes
    )


@pytest.mark.unit
def test_run_evidence_establishes_canonical_market_return_at_source_boundary():
    lookup = resolve_authoritative_instrument_identity(
        "510500",
        registry_path=FIXTURE,
        expected_sha256=_fixture_digest(),
    )
    assert isinstance(lookup, IdentityRegistryAvailable)
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {"cn_a": {"core_stock_apis": "synthetic"}}
    })
    closes = [100.0] * (MARKET_RETURN_OBSERVATIONS - 1) + [110.0]
    frame = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-07-17", periods=MARKET_RETURN_OBSERVATIONS),
        "Open": closes,
        "High": [value + 1 for value in closes],
        "Low": [value - 1 for value in closes],
        "Close": closes,
        "Volume": [100.0] * MARKET_RETURN_OBSERVATIONS,
    })

    with (
        patch(
            "tradingagents.dataflows.instrument_identity.resolve_authoritative_instrument_identity",
            return_value=lookup,
        ),
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"synthetic": SnapshotProvider(lambda *_args: frame, "qfq")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("510500", "2026-07-19")

    fact = next(
        fact
        for fact in evidence.source_facts
        if fact.canonical_field == MARKET_RETURN_FIELD
    )
    assert fact.fact_kind == "canonical"
    assert fact.normalized_value == Decimal("0.10000000")
    assert fact.unit == MARKET_RETURN_UNIT
    assert fact.instrument_symbol == "510500.SS"
    assert fact.effective_date == "2026-07-17"
    assert fact.calculation_lineage is not None
    assert fact.calculation_lineage.observations_used == MARKET_RETURN_OBSERVATIONS
    assert (
        fact.calculation_lineage.implementation_version
        == MARKET_RETURN_IMPLEMENTATION_VERSION
    )
    assert fact.calculation_lineage.input_artifact_sha256 == fact.artifact_sha256
