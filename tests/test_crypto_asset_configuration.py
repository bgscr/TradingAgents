from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from langgraph.graph import END, StateGraph
from pydantic import ValidationError
from typer.testing import CliRunner

from cli import main as cli_main
from tradingagents.asset_configuration import (
    ObservationCalendarKind,
    RunAssetConfiguration,
    RunAssetConfigurationError,
    resolve_run_asset_configuration,
)
from tradingagents.dataflows import instrument_identity as identity_module, market_snapshot
from tradingagents.dataflows.market_snapshot import (
    SnapshotProvider,
    authoritative_snapshot_run,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.decision_policy import DecisionHorizon, HorizonUnit
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceState,
    InstrumentKind,
    SourceAcquisitionAvailable,
    SourceArtifact,
    acquire_run_evidence,
    capability_profile_for,
    stable_acquisition_source_ref,
)
from tradingagents.graph import trading_graph as trading_graph_module
from tradingagents.graph.checkpointer import get_checkpointer, has_checkpoint, thread_id
from tradingagents.graph.evidence_gate import create_preflight_gate_node
from tradingagents.graph.propagation import Propagator
from tradingagents.strategy_registry import create_production_decision_policy


def test_sol_asset_configuration_is_authoritative_immutable_and_crypto_specific():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    assert isinstance(asset_configuration, RunAssetConfiguration)
    assert asset_configuration.instrument_identity.symbol == "SOL-USD"
    assert asset_configuration.instrument_identity.venue == "CCC"
    assert asset_configuration.instrument_kind is InstrumentKind.CRYPTO
    assert asset_configuration.registry_id == "crypto-ccc-identity-registry-v1"
    assert asset_configuration.registry_digest == (
        "d8bd9fa6491af365384015b1e34b452d0df2a8ca5c5b0d9cb0a78908361de5d1"
    )
    assert asset_configuration.reference_market == "CCC"
    assert asset_configuration.capability_profile.profile_id == "crypto.v1"
    assert asset_configuration.observation_calendar_kind is (
        ObservationCalendarKind.CONSECUTIVE_DAILY
    )
    assert asset_configuration.calculation_id == (
        "market.close_return_20d.crypto.auto_adjusted"
    )
    assert asset_configuration.horizon.count == 20
    assert asset_configuration.horizon.unit is HorizonUnit.CALENDAR_DAYS
    assert asset_configuration.asset_configuration_version == "1.0"
    assert asset_configuration.asset_configuration_signature.startswith(
        "asset-config:v1:"
    )
    assert "trading_days" not in asset_configuration.model_dump_json()

    with pytest.raises(ValidationError, match="frozen"):
        asset_configuration.reference_market = "XSHG"


def test_mainland_asset_configuration_preserves_equity_semantics():
    project_root = Path(__file__).parents[1]
    registry_path = project_root / "config" / "instrument_identity_registry.json"
    registry_digest = registry_path.with_suffix(".sha256").read_text(
        encoding="utf-8"
    ).split()[0]
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "instrument_identity_registry_path": str(registry_path),
            "instrument_identity_registry_sha256": registry_digest,
        }
    )

    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=config,
    )

    assert asset_configuration.instrument_identity.symbol == "601328.SS"
    assert asset_configuration.reference_market == "XSHG"
    assert asset_configuration.instrument_kind is InstrumentKind.EQUITY
    assert asset_configuration.capability_profile.profile_id == "equity.v1"
    assert asset_configuration.observation_calendar_kind is (
        ObservationCalendarKind.MARKET_SESSIONS
    )
    assert asset_configuration.adjustment_basis == "qfq"
    assert asset_configuration.calculation_id == "market.close_return_20d.qfq"
    assert asset_configuration.horizon == DecisionHorizon(
        count=20,
        unit=HorizonUnit.TRADING_DAYS,
    )


def test_crypto_asset_configuration_drives_graph_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
        }
    )

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market", "social", "news", "fundamentals"),
        config=config,
        asset_configuration=asset_configuration,
    )

    assert graph.asset_configuration is asset_configuration
    assert graph.instrument_kind is InstrumentKind.CRYPTO
    assert graph.capability_profile.profile_id == "crypto.v1"
    assert graph.selected_analysts == ("market", "social", "news")
    assert graph.decision_horizon == asset_configuration.horizon
    assert "Fundamentals Analyst" not in graph.workflow.nodes
    assert "tools_fundamentals" not in graph.workflow.nodes


def test_evidence_uses_pre_resolved_asset_without_reopening_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    asset_configuration = resolve_run_asset_configuration("SOL-USD", config=config)
    dates = pd.date_range(start="2026-07-05", periods=21, freq="D")
    closes = [100.0] * 20 + [110.0]
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100.0] * 21,
        }
    )

    def forbidden_registry_resolution(*_args, **_kwargs):
        raise AssertionError("evidence must use the pre-resolved run asset")

    monkeypatch.setattr(
        identity_module,
        "resolve_authoritative_instrument_identity",
        forbidden_registry_resolution,
    )
    with (
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence(
            "SOL-USD",
            "2026-07-25",
            asset_configuration=asset_configuration,
        )

    assert evidence.instrument_identity == asset_configuration.instrument_identity
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.history_rows == 21
    assert evidence.source_facts[0].calculation_lineage is not None
    assert evidence.source_facts[0].calculation_lineage.calculation_id == (
        asset_configuration.calculation_id
    )


def test_checkpointable_initial_state_contains_asset_configuration_before_stream():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    state = Propagator().create_initial_state(
        "SOL-USD",
        "2026-07-25",
        asset_type="crypto",
        asset_configuration=asset_configuration,
    )

    assert state["asset_configuration"] == asset_configuration.model_dump(mode="json")
    assert state["asset_configuration"]["asset_configuration_signature"] == (
        asset_configuration.asset_configuration_signature
    )


def test_checkpoint_signature_commits_every_crypto_asset_semantic():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    graph = object.__new__(trading_graph_module.TradingAgentsGraph)
    graph.selected_analysts = ("market", "social", "news")
    graph.config = {
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "evidence_gate_mode": "enforce",
    }
    graph.decision_policy = type(
        "Policy",
        (),
        {"registry_digest": "registry:production-rules"},
    )()
    graph.decision_horizon = asset_configuration.horizon
    graph.asset_configuration = asset_configuration
    baseline = graph._run_signature("crypto")

    variants = (
        asset_configuration.model_copy(
            update={
                "instrument_identity": asset_configuration.instrument_identity.model_copy(
                    update={"symbol": "BTC-USD"}
                )
            }
        ),
        asset_configuration.model_copy(update={"registry_id": "crypto-registry-v2"}),
        asset_configuration.model_copy(update={"registry_digest": "0" * 64}),
        asset_configuration.model_copy(update={"reference_market": "OTHER"}),
        asset_configuration.model_copy(
            update={"observation_calendar_kind": ObservationCalendarKind.MARKET_SESSIONS}
        ),
        asset_configuration.model_copy(
            update={
                "horizon": DecisionHorizon(count=21, unit=HorizonUnit.CALENDAR_DAYS)
            }
        ),
        asset_configuration.model_copy(
            update={"capability_profile": capability_profile_for(InstrumentKind.EQUITY)}
        ),
    )

    for variant in variants:
        graph.asset_configuration = variant
        assert graph._run_signature("crypto") != baseline

    graph.asset_configuration = None
    assert graph._run_signature("crypto") != baseline
    assert "trading_days" not in baseline


def test_legacy_checkpoint_without_asset_semantics_starts_fresh_crypto_run(
    tmp_path: Path,
) -> None:
    class CheckpointState(TypedDict):
        count: int

    def increment(state: CheckpointState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    workflow = StateGraph(CheckpointState)
    workflow.add_node("increment", increment)
    workflow.set_entry_point("increment")
    workflow.add_edge("increment", END)

    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    graph = object.__new__(trading_graph_module.TradingAgentsGraph)
    graph.config = {
        "checkpoint_enabled": True,
        "data_cache_dir": str(tmp_path),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "evidence_gate_mode": "enforce",
    }
    graph.selected_analysts = ("market", "social", "news")
    graph.decision_policy = create_production_decision_policy()
    graph.decision_horizon = asset_configuration.horizon
    graph.asset_configuration = None
    graph.workflow = workflow
    graph.graph = workflow.compile()

    legacy_signature = graph._run_signature("crypto")
    legacy_config = {
        "configurable": {
            "thread_id": thread_id("SOL-USD", "2026-07-25", legacy_signature),
        }
    }
    with get_checkpointer(tmp_path, "SOL-USD") as saver:
        legacy_graph = workflow.compile(checkpointer=saver)
        legacy_graph.invoke({"count": 40}, config=legacy_config)
    assert has_checkpoint(
        tmp_path,
        "SOL-USD",
        "2026-07-25",
        legacy_signature,
    )

    graph.asset_configuration = asset_configuration
    current_signature = graph._run_signature("crypto")
    assert current_signature != legacy_signature

    with graph.checkpoint_scope("SOL-USD", "2026-07-25", "crypto") as session:
        assert session.resume_from_checkpoint is False
        assert session.graph_config["configurable"]["thread_id"] == thread_id(
            "SOL-USD",
            "2026-07-25",
            current_signature,
        )
        result = graph.graph.invoke({"count": 0}, config=session.graph_config)

    assert result["count"] == 1
    assert has_checkpoint(
        tmp_path,
        "SOL-USD",
        "2026-07-25",
        legacy_signature,
    )


def test_cli_resolves_asset_configuration_before_graph_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    resolved = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    class ConstructionObserved(RuntimeError):
        pass

    def resolve_before_graph(symbol: str, *, config):
        assert symbol == "SOL-USD"
        events.append("asset_configuration")
        return resolved

    class GraphProbe:
        def __init__(self, *_args, **kwargs):
            events.append("graph")
            assert kwargs["asset_configuration"] is resolved
            assert kwargs["asset_type"] == "crypto"
            raise ConstructionObserved

    selections = {
        "ticker": "SOL-USD",
        "analysis_date": "2026-07-25",
        "asset_type": "crypto",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "fixture-quick",
        "deep_thinker": "fixture-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(
            cli_main.DEFAULT_CONFIG,
            results_dir=str(tmp_path / "results"),
            data_cache_dir=str(tmp_path / "cache"),
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "resolve_run_asset_configuration",
        resolve_before_graph,
        raising=False,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", GraphProbe)

    result = CliRunner().invoke(cli_main.app, ["analyze", "--no-checkpoint"])

    assert isinstance(result.exception, ConstructionObserved)
    assert events == ["asset_configuration", "graph"]


def test_programmatic_constructor_resolves_asset_before_model_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_resolver = trading_graph_module.resolve_run_asset_configuration

    def recording_resolver(symbol: str, *, config):
        events.append("asset_configuration")
        return original_resolver(symbol, config=config)

    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm

    def recording_model_client(**_kwargs):
        events.append("model_client")
        return client

    monkeypatch.setattr(
        trading_graph_module,
        "resolve_run_asset_configuration",
        recording_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        recording_model_client,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
        }
    )

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market", "fundamentals"),
        config=config,
        instrument_symbol="SOL-USD",
    )

    assert events[0] == "asset_configuration"
    assert events[1:] == ["model_client", "model_client"]
    assert graph.asset_configuration is not None
    assert graph.asset_configuration.instrument_identity.symbol == "SOL-USD"
    assert graph.asset_type == "crypto"


@pytest.mark.parametrize(
    ("config_update", "diagnostic_code"),
    [
        (
            {"crypto_identity_registry_sha256": None},
            "crypto_registry_pin_partial_override",
        ),
        (
            {"crypto_identity_registry_sha256": "0" * 64},
            "registry_digest_mismatch",
        ),
        (
            {"crypto_identity_registry_id": "wrong-registry"},
            "crypto_registry_id_mismatch",
        ),
    ],
)
def test_incoherent_crypto_configuration_fails_before_model_clients(
    config_update: dict,
    diagnostic_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(config_update)

    def forbidden_model_client(**_kwargs):
        raise AssertionError("asset configuration must fail before model clients")

    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        forbidden_model_client,
    )

    with pytest.raises(RunAssetConfigurationError) as raised:
        trading_graph_module.TradingAgentsGraph(
            config=config,
            instrument_symbol="SOL-USD",
        )

    assert raised.value.diagnostic_code == diagnostic_code


def test_decision_audit_records_complete_crypto_asset_configuration(
    tmp_path: Path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    source_ref = stable_acquisition_source_ref(
        "identity-registry",
        asset_configuration.registry_digest,
    )
    registry_artifact = SourceArtifact(
        artifact_sha256=asset_configuration.registry_digest,
        source_ref=source_ref,
        tool_call_id="identity-registry",
        tool_name="instrument_identity_registry",
        raw_text=asset_configuration.registry_artifact,
    )
    evidence = EvidenceState(
        instrument_identity=asset_configuration.instrument_identity,
        source_artifacts=(registry_artifact,),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="instrument-identity-registry",
                capability="instrument_identity",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-25T00:00:00+00:00",
                artifact=registry_artifact,
            ),
        ),
    )
    state = Propagator().create_initial_state(
        "SOL-USD",
        "2026-07-25",
        asset_type="crypto",
        asset_configuration=asset_configuration,
        evidence_state=evidence,
    )
    state.update(
        create_preflight_gate_node(
            create_production_decision_policy(),
            asset_configuration.horizon,
        )(state)
    )
    state["graph_signature"] = (
        "asset_configuration=" + asset_configuration.asset_configuration_signature
    )
    state["evidence_gate_mode"] = "enforce"
    config = dict(
        DEFAULT_CONFIG,
        asset_configuration_signature=(
            asset_configuration.asset_configuration_signature
        ),
    )

    audit = prepare_decision_audit(state, tmp_path, config=config)

    projected = audit["asset_configuration"]
    assert projected["instrument_identity"]["symbol"] == "SOL-USD"
    assert projected["instrument_kind"] == "crypto"
    assert projected["registry_id"] == "crypto-ccc-identity-registry-v1"
    assert projected["registry_digest"] == asset_configuration.registry_digest
    assert projected["reference_market"] == "CCC"
    assert projected["capability_profile"]["profile_id"] == "crypto.v1"
    assert projected["observation_calendar_kind"] == "consecutive_daily"
    assert projected["calculation_id"] == asset_configuration.calculation_id
    assert projected["horizon"] == {
        "contract_version": "1.0",
        "count": 20,
        "unit": "calendar_days",
    }
    assert projected["asset_configuration_signature"] == (
        asset_configuration.asset_configuration_signature
    )
    assert "registry_source_ref" not in projected
    assert "registry_artifact" not in projected
    assert "trading_days" not in json.dumps(projected, sort_keys=True)


def test_yahoo_backed_sol_cli_and_programmatic_contracts_match_full_asset_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class QuietDisplay:
        def start(self) -> None:
            return None

        def refresh(self, _spinner_text=None) -> None:
            return None

        def publish_event(self, _event) -> None:
            return None

        def report_ready(self, _section, _content, _path) -> None:
            return None

        def close(self) -> None:
            return None

    class StreamProbe:
        def __init__(self, delegate, owner) -> None:
            self._delegate = delegate
            self._owner = owner

        def stream(self, *args, **kwargs):
            events.append("stream")
            assert self._owner.asset_configuration is not None
            return self._delegate.stream(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    class OrderedCliGraph(trading_graph_module.TradingAgentsGraph):
        def __init__(self, *args, **kwargs) -> None:
            events.append("graph")
            assert kwargs["asset_configuration"] is not None
            super().__init__(*args, **kwargs)

        @contextmanager
        def checkpoint_scope(self, *args, **kwargs):
            events.append("checkpoint")
            assert self.asset_configuration is not None
            with super().checkpoint_scope(*args, **kwargs) as session:
                self.graph = StreamProbe(self.graph, self)
                yield session

        def resolve_evidence_state(self, *args, **kwargs):
            events.append("evidence")
            assert self.asset_configuration is not None
            return super().resolve_evidence_state(*args, **kwargs)

        def create_initial_state(self, *args, **kwargs):
            events.append("checkpoint_state")
            assert self.asset_configuration is not None
            state = super().create_initial_state(*args, **kwargs)
            assert state["asset_configuration"] is not None
            return state

    model = MagicMock()
    model.with_structured_output.return_value = model
    client = MagicMock()
    client.get_llm.return_value = model
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    dates = pd.date_range(start="2026-07-05", periods=21, freq="D")
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
            "checkpoint_enabled": True,
            "evidence_gate_mode": "enforce",
        }
    )
    selections = {
        "ticker": "SOL-USD",
        "analysis_date": "2026-07-25",
        "asset_type": "crypto",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": config["quick_think_llm"],
        "deep_thinker": config["deep_think_llm"],
        "backend_url": config["backend_url"],
        "llm_provider": config["llm_provider"],
        "google_thinking_level": config["google_thinking_level"],
        "openai_reasoning_effort": config["openai_reasoning_effort"],
        "anthropic_effort": config["anthropic_effort"],
        "output_language": config["output_language"],
    }
    captured_cli: dict = {}
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts):
        captured_cli.update(final_state)
        original_mark_completed(final_state, artifacts)

    original_cli_resolver = cli_main.resolve_run_asset_configuration

    def recording_cli_resolver(symbol: str, *, config):
        events.append("asset_configuration")
        return original_cli_resolver(symbol, config=config)

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        cli_main,
        "resolve_run_asset_configuration",
        recording_cli_resolver,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", OrderedCliGraph)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: QuietDisplay(),
    )
    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    with patch.object(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
    ):
        cli_result = CliRunner().invoke(
            cli_main.app,
            ["analyze", "--checkpoint"],
        )
        assert cli_result.exit_code == 0, cli_result.output

        programmatic_graph = trading_graph_module.TradingAgentsGraph(
            selected_analysts=("market",),
            config=config,
            instrument_symbol="SOL-USD",
        )
        programmatic, signal = programmatic_graph.propagate(
            "SOL-USD",
            "2026-07-25",
        )

    assert signal is None
    assert captured_cli["terminal_contract"] == programmatic["terminal_contract"]
    assert captured_cli["configuration_digest"] == programmatic["configuration_digest"]
    assert captured_cli["asset_configuration"] == programmatic["asset_configuration"]
    assert events == [
        "asset_configuration",
        "graph",
        "checkpoint",
        "evidence",
        "checkpoint_state",
        "stream",
    ]
    cli_audit = json.loads(
        Path(captured_cli["decision_audit_path"]).read_text(encoding="utf-8")
    ) if captured_cli.get("decision_audit_path") else json.loads(
        next((tmp_path / "results").rglob("decision-audit.json")).read_text(
            encoding="utf-8"
        )
    )
    programmatic_audit = json.loads(
        Path(programmatic["decision_audit_path"]).read_text(encoding="utf-8")
    )
    assert cli_audit["asset_configuration"] == programmatic_audit["asset_configuration"]
    for audit in (cli_audit, programmatic_audit):
        asset_audit = audit["asset_configuration"]
        assert asset_audit["instrument_identity"]["symbol"] == "SOL-USD"
        assert asset_audit["instrument_kind"] == "crypto"
        assert asset_audit["registry_id"] == "crypto-ccc-identity-registry-v1"
        assert asset_audit["registry_digest"] == config[
            "crypto_identity_registry_sha256"
        ]
        assert asset_audit["reference_market"] == "CCC"
        assert asset_audit["capability_profile"]["profile_id"] == "crypto.v1"
        assert asset_audit["observation_calendar_kind"] == "consecutive_daily"
        assert asset_audit["horizon"] == {
            "contract_version": "1.0",
            "count": 20,
            "unit": "calendar_days",
        }
        assert "trading_days" not in json.dumps(asset_audit, sort_keys=True)
        market_audit = audit["evidence_state"]["market_snapshot"]
        assert market_audit["history_rows"] == 21
        market_return_fact = next(
            fact
            for fact in audit["evidence_state"]["source_facts"]
            if fact["canonical_field"] == "market.close_return_20d"
        )
        lineage = market_return_fact["calculation_lineage"]
        assert lineage["calculation_id"] == (
            "market.close_return_20d.crypto.auto_adjusted"
        )
        assert lineage["effective_range_start"] == "2026-07-05"
        assert lineage["effective_range_end"] == "2026-07-25"
        assert lineage["observations_used"] == 21
        assert market_audit["physical_attempt_count"] == 1
        assert len(market_audit["physical_attempt_events"]) == 1
        attempt = market_audit["physical_attempt_events"][0]
        assert attempt["attempt_index"] == 1
        assert attempt["outcome"] == "available"
        assert attempt["pacing_event"] == "permit_acquired"
        assert attempt["final_physical_attempt_count"] == 1
        assert attempt["upstream_service_name"] == "Yahoo Finance"
    rendered_reports = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "results").rglob("*.md")
    )
    assert "## Physical Provider Attempts" in rendered_reports
    assert "**Upstream Service Identity:**" in rendered_reports
    assert "**Final physical-attempt count:** 1" in rendered_reports
    assert model.invoke.call_count > 0
    model.stream.assert_not_called()
