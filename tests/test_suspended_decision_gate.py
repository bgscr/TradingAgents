from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from typer.testing import CliRunner

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.market_snapshot as market_snapshot
import tradingagents.market_history.shadow as shadow_module
from cli import main as cli_main
from tests.test_market_history_shadow import _suspended_baostock_candidate
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.market_snapshot import SnapshotProvider
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    SourceArtifact,
    TradingStatusProvenanceEvidence,
    capability_profile_for,
)
from tradingagents.graph import trading_graph as trading_graph_module
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.evidence_gate import (
    create_preflight_gate_node,
    route_after_preflight,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history import (
    AdjustmentFactorObservation,
    MarketSession,
    MarketSessionStatus,
    RawMarketObservation,
    TradingStatus,
    TradingStatusObservation,
)
from tradingagents.strategy_registry import (
    DEFAULT_DECISION_HORIZON,
    create_production_decision_policy,
)


def _suspended_evidence(
    *,
    latest_traded_close: Decimal | None = Decimal("10.25"),
) -> EvidenceState:
    identity_raw = '{"symbol":"600519.SS","venue":"shanghai"}'
    identity_digest = sha256(identity_raw.encode("utf-8")).hexdigest()
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="600519.SS",
            venue="shanghai",
            instrument_kind="equity",
            currency="CNY",
            provenance=IdentityProvenance(
                provider="mainland-registry",
                source_ref="registry:mainland-v1",
                retrieved_at="2026-07-24T10:00:00+00:00",
                artifact_sha256=identity_digest,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="600519.SS",
            provider="baostock",
            retrieved_at="2026-07-24T10:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-24",
            effective_trading_date="2026-07-24",
            history_rows=21,
            frame_sha256="b" * 64,
            snapshot_id="snapshot:suspended",
            current_tradeability="suspended",
            current_status_provenance=TradingStatusProvenanceEvidence(
                provider="baostock",
                provider_dataset_id="provider:baostock-strict-v1",
                session_date="2026-07-24",
                status="suspended",
                observed_at="2026-07-24T10:00:00+00:00",
                revision_id="status:2026-07-24:suspended",
            ),
            latest_traded_close=latest_traded_close,
            latest_traded_close_diagnostic=(
                None
                if latest_traded_close is not None
                else "no_genuinely_traded_close_in_retained_history"
            ),
            carried_suspension_close=Decimal("10.50"),
        ),
        source_artifacts=(
            SourceArtifact(
                artifact_sha256=identity_digest,
                source_ref="registry:mainland-v1",
                tool_call_id="identity:600519.SS",
                tool_name="resolve_authoritative_instrument_identity",
                raw_text=identity_raw,
            ),
        ),
    )


@pytest.mark.unit
def test_compiled_suspension_gate_stops_before_models_and_renders_latest_close() -> None:
    llm = MagicMock()

    def forbidden_tool_node(_state: AgentState) -> AgentState:
        raise AssertionError("suspended preflight must not invoke analyst tools")

    policy = create_production_decision_policy()
    setup = GraphSetup(
        llm,
        llm,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            forbidden_tool_node,
        ),
        ConditionalLogic(max_debate_rounds=0, max_risk_discuss_rounds=0),
        evidence_gate_mode="enforce",
        decision_policy=policy,
        decision_horizon=DEFAULT_DECISION_HORIZON,
    )
    graph = setup.setup_graph(["market"]).compile()
    llm.reset_mock()
    initial_state = Propagator().create_initial_state(
        "600519.SS",
        "2026-07-24",
        evidence_state=_suspended_evidence(),
    )

    result = graph.invoke(initial_state)

    assert result["analysis_outcome_contract"] == {
        "contract_version": "2.0",
        "readiness": "insufficient",
        "reason": "instrument_currently_suspended",
        "diagnostic_codes": ["instrument_currently_suspended"],
        "current_suspension": {
            "current_tradeability": "suspended",
            "latest_traded_close_status": "available",
            "latest_traded_close": "10.25",
        },
    }
    assert "**Latest Genuinely Traded Close:** `10.25`" in result[
        "analysis_outcome"
    ]
    assert "No Trading Decision was issued." in result["analysis_outcome"]
    assert result.get("trading_decision") is None
    assert result.get("final_trade_decision") is None
    assert llm.mock_calls == []


@pytest.mark.unit
def test_suspended_outcome_renders_explicit_unavailable_latest_close() -> None:
    evidence = _suspended_evidence(latest_traded_close=None)

    result = create_preflight_gate_node(
        create_production_decision_policy(),
        DEFAULT_DECISION_HORIZON,
    )({"evidence_state": evidence.model_dump(mode="json")})

    assert result["analysis_outcome_contract"]["current_suspension"] == {
        "current_tradeability": "suspended",
        "latest_traded_close_status": "unavailable",
    }
    assert "**Latest Genuinely Traded Close:** `unavailable`" in result[
        "analysis_outcome"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("current_tradeability", ["tradeable", "unknown"])
def test_non_suspended_tradeability_controls_retain_preflight_behavior(
    current_tradeability: str,
) -> None:
    evidence_payload = _suspended_evidence().model_dump(mode="json")
    snapshot = evidence_payload["market_snapshot"]
    if current_tradeability == "tradeable":
        snapshot["current_tradeability"] = "tradeable"
        snapshot["current_status_provenance"]["status"] = "traded"
        snapshot["latest_traded_close"] = "10.50"
        snapshot["carried_suspension_close"] = None
    else:
        snapshot["current_tradeability"] = "unknown"
        snapshot["current_status_provenance"] = None
        snapshot["latest_traded_close"] = None
        snapshot["latest_traded_close_diagnostic"] = None
        snapshot["carried_suspension_close"] = None
    evidence = EvidenceState.model_validate(evidence_payload)

    result = create_preflight_gate_node(
        create_production_decision_policy(),
        DEFAULT_DECISION_HORIZON,
    )({"evidence_state": evidence.model_dump(mode="json")})

    assert result["evidence_preflight"]["passed"] is True
    assert route_after_preflight(result) == "admitted"
    assert "analysis_outcome_contract" not in result


@pytest.mark.unit
@pytest.mark.parametrize("status_failure", ["loss", "contradiction"])
def test_status_loss_or_contradiction_fails_closed_before_models(
    status_failure: str,
) -> None:
    llm = MagicMock()

    def forbidden_tool_node(_state: AgentState) -> AgentState:
        raise AssertionError("invalid status must not invoke analyst tools")

    policy = create_production_decision_policy()
    setup = GraphSetup(
        llm,
        llm,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            forbidden_tool_node,
        ),
        ConditionalLogic(max_debate_rounds=0, max_risk_discuss_rounds=0),
        evidence_gate_mode="enforce",
        decision_policy=policy,
        decision_horizon=DEFAULT_DECISION_HORIZON,
    )
    graph = setup.setup_graph(["market"]).compile()
    llm.reset_mock()
    initial_state = Propagator().create_initial_state(
        "600519.SS",
        "2026-07-24",
        evidence_state=_suspended_evidence(),
    )
    status = initial_state["evidence_state"]["market_snapshot"]
    if status_failure == "loss":
        status["current_status_provenance"] = None
    else:
        status["current_status_provenance"]["status"] = "traded"

    with pytest.raises(ValueError, match="status provenance"):
        graph.invoke(initial_state)

    assert llm.mock_calls == []


class _ForbiddenSignalProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def process_signal(self, _state) -> str:
        self.calls += 1
        raise AssertionError("suspended outcome must not publish a signal")


class _SuspendedProgrammaticGraph(TradingAgentsGraph):
    def __init__(self, root: Path, evidence: EvidenceState) -> None:
        self.debug = False
        self.config = {
            "checkpoint_enabled": False,
            "data_cache_dir": str(root / "cache"),
            "results_dir": str(root / "results"),
            "evidence_gate_mode": "enforce",
            "max_debate_rounds": 0,
            "max_risk_discuss_rounds": 0,
        }
        self.callbacks = []
        self.decision_policy = create_production_decision_policy()
        self.decision_horizon = DEFAULT_DECISION_HORIZON
        self._decision_horizon_is_explicit = True
        assert evidence.instrument_identity is not None
        self.instrument_kind = evidence.instrument_identity.instrument_kind
        self.capability_profile = capability_profile_for(self.instrument_kind)
        self.asset_type = "stock"
        self._requested_analysts = ("market",)
        self.selected_analysts = ("market",)
        self.boundary_llm = MagicMock()

        def forbidden_tool_node(_state: AgentState) -> AgentState:
            raise AssertionError("suspended preflight must not invoke analyst tools")

        self.graph_setup = GraphSetup(
            self.boundary_llm,
            self.boundary_llm,
            dict.fromkeys(
                ("market", "social", "news", "fundamentals"),
                forbidden_tool_node,
            ),
            ConditionalLogic(max_debate_rounds=0, max_risk_discuss_rounds=0),
            evidence_gate_mode="enforce",
            decision_policy=self.decision_policy,
            decision_horizon=self.decision_horizon,
        )
        self.workflow = self.graph_setup.setup_graph(self.selected_analysts)
        self.graph = self.workflow.compile()
        self.boundary_llm.reset_mock()
        self.propagator = Propagator()
        memory_path = root / "trading_memory.md"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(
            "[2026-07-23 | 600519.SS | Buy | pending]\n\n"
            "DECISION:\nPrior authorized decision.\n\n"
            "<!-- ENTRY_END -->\n\n",
            encoding="utf-8",
        )
        self.memory_path = memory_path
        self.memory_log = TradingMemoryLog({"memory_log_path": str(memory_path)})
        self.signal_processor = _ForbiddenSignalProcessor()
        self.reflector = MagicMock()
        self.log_states_dict = {}
        self.curr_state = None
        self.ticker = None
        self._checkpointer_ctx = None
        self._fixture_evidence = evidence

    def resolve_evidence_state(self, ticker: str, trade_date: str) -> EvidenceState:
        assert (ticker, trade_date) == ("600519.SS", "2026-07-24")
        return self._fixture_evidence


@pytest.mark.unit
def test_programmatic_suspension_gate_has_no_directional_side_effects(
    tmp_path: Path,
) -> None:
    graph = _SuspendedProgrammaticGraph(tmp_path, _suspended_evidence())
    memory_before = graph.memory_path.read_bytes()

    final_state, signal = graph.propagate("600519.SS", "2026-07-24")

    assert final_state["terminal_outcome_kind"] == "analysis_outcome"
    assert final_state["analysis_outcome_contract"]["reason"] == (
        "instrument_currently_suspended"
    )
    assert signal is None
    assert graph.signal_processor.calls == 0
    assert graph.memory_path.read_bytes() == memory_before
    assert graph.boundary_llm.mock_calls == []
    assert final_state.get("trading_decision") is None
    assert final_state.get("final_trade_decision") is None
    audit = json.loads(Path(final_state["decision_audit_path"]).read_text("utf-8"))
    assert audit["terminal"]["terminal_outcome_kind"] == "analysis_outcome"
    assert audit["analysis_outcome_contract"] == final_state[
        "analysis_outcome_contract"
    ]
    assert audit["evidence_state"]["market_snapshot"][
        "current_tradeability"
    ] == "suspended"
    assert audit["evidence_state"]["market_snapshot"][
        "latest_traded_close"
    ] == "10.25"
    assert audit["trading_decision"] is None
    assert audit["direction_selection"] is None
    assert sum(
        stage["model_calls"] for stage in audit["telemetry"]["stages"].values()
    ) == 0
    assert sum(
        stage["tool_calls"] for stage in audit["telemetry"]["stages"].values()
    ) == 0


class _RecordingDisplay:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def start(self) -> None:
        return None

    def refresh(self, _spinner_text=None) -> None:
        return None

    def publish_event(self, _event) -> None:
        return None

    def report_ready(self, section_name, content, _path) -> None:
        self.reports.append((section_name, content))

    def close(self) -> None:
        return None


def _joint_baostock_candidate(*, tradeable: bool):
    base = _suspended_baostock_candidate()
    session_dates = tuple(
        timestamp.date()
        for timestamp in pd.bdate_range(end="2026-07-24", periods=21)
    )
    prior_close = Decimal("10.00")
    final_close = Decimal("10.75") if tradeable else Decimal("10.25")
    observations = tuple(
        RawMarketObservation(
            session_date,
            prior_close,
            prior_close,
            prior_close,
            prior_close,
            Decimal("100"),
        )
        for session_date in session_dates[:-1]
    ) + (
        RawMarketObservation(
            session_dates[-1],
            final_close,
            final_close,
            final_close,
            final_close,
            Decimal("120") if tradeable else Decimal("0"),
        ),
    )
    statuses = tuple(
        TradingStatusObservation(session_date, TradingStatus.TRADED)
        for session_date in session_dates[:-1]
    ) + (
        TradingStatusObservation(
            session_dates[-1],
            TradingStatus.TRADED if tradeable else TradingStatus.SUSPENDED,
            official_carried_close=None if tradeable else final_close,
            volume=None if tradeable else Decimal("0"),
        ),
    )
    closes = [float(prior_close)] * 20 + [float(final_close)]
    volumes = [100.0] * 20 + [120.0 if tradeable else 0.0]
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(session_dates),
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": volumes,
        }
    )
    bundle = replace(
        base.history_bundle,
        instrument=replace(
            base.history_bundle.instrument,
            instrument_id="instrument:601328.SS",
            canonical_symbol="601328.SS",
            reference_market="XSHG",
        ),
        raw_payload=(
            b'{"provider":"baostock","status":"traded"}'
            if tradeable
            else b'{"provider":"baostock","status":"suspended"}'
        ),
        observations=observations,
        trading_statuses=statuses,
        adjustment_factors=(
            AdjustmentFactorObservation(session_dates[0], Decimal("1")),
        ),
    )
    return replace(
        base,
        frame=frame,
        history_bundle=bundle,
        calendar=replace(
            base.calendar,
            sessions=tuple(
                MarketSession(session_date, MarketSessionStatus.OPEN)
                for session_date in session_dates
            ),
        ),
        current_tradeability="tradeable" if tradeable else "suspended",
        current_status_provenance=replace(
            base.current_status_provenance,
            session_date=session_dates[-1],
            status=TradingStatus.TRADED if tradeable else TradingStatus.SUSPENDED,
        ),
        latest_traded_close=final_close if tradeable else prior_close,
        carried_suspension_close=None if tradeable else final_close,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tradeable", "expected_status"),
    ((False, "suspended"), (True, "tradeable")),
    ids=("suspended", "tradeable"),
)
def test_real_baostock_candidate_reaches_joint_cli_and_audit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tradeable: bool,
    expected_status: str,
) -> None:
    candidate = _joint_baostock_candidate(tradeable=tradeable)
    provider_calls: list[str] = []

    def baostock(*_args, **_kwargs):
        provider_calls.append("baostock")
        return candidate

    def yfinance(*_args, **_kwargs):
        provider_calls.append("yfinance")
        raise AssertionError("a complete BaoStock candidate must stop fallback")

    model = MagicMock()
    model.with_structured_output.return_value = model
    client = MagicMock()
    client.get_llm.return_value = model
    signal_processor = _ForbiddenSignalProcessor()
    history_root = tmp_path / "history"
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
            "checkpoint_enabled": False,
            "evidence_gate_mode": "enforce",
            "max_debate_rounds": 0,
            "max_risk_discuss_rounds": 0,
            "market_history_mode": "shadow",
            "market_history_database_path": str(
                history_root / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "baostock,yfinance"
    )
    memory_path = Path(config["memory_log_path"])
    memory_path.write_text(
        "[2026-07-23 | 601328.SS | Buy | pending]\n\n"
        "DECISION:\nPrior authorized decision.\n\n"
        "<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    memory_before = memory_path.read_bytes()
    display = _RecordingDisplay()
    captured_state: dict = {}
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts):
        captured_state.update(final_state)
        original_mark_completed(final_state, artifacts)

    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-24",
        "asset_type": "stock",
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
    monkeypatch.setattr(config_module, "_config", copy.deepcopy(config))
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: display,
    )
    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(
        trading_graph_module,
        "SignalProcessor",
        lambda *_args, **_kwargs: signal_processor,
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(baostock, "qfq"),
            "yfinance": SnapshotProvider(yfinance, "auto_adjusted"),
        },
    )
    monkeypatch.setattr(
        shadow_module,
        "persist_mainland_history_candidate",
        lambda _candidate: shadow_module.HistoryBundleWriteResult(True, True),
    )

    cli_result = CliRunner().invoke(cli_main.app, ["analyze"])

    assert cli_result.exit_code == 0, cli_result.output
    assert provider_calls == ["baostock"]
    audit_path = (
        Path(captured_state["decision_audit_path"])
        if captured_state.get("decision_audit_path")
        else next(Path(config["results_dir"]).rglob("decision-audit.json"))
    )
    audit = json.loads(
        audit_path.read_text(encoding="utf-8")
    )
    snapshot = audit["evidence_state"]["market_snapshot"]
    assert snapshot["provider"] == "baostock"
    assert snapshot["adjustment_basis"] == "qfq"
    assert snapshot["history_rows"] == 21
    assert snapshot["current_tradeability"] == expected_status
    assert snapshot["current_status_provenance"]["status"] == (
        "traded" if tradeable else "suspended"
    )
    assert snapshot["snapshot_id"].startswith("snapshot:v2:")
    assert snapshot["snapshot_id_version"] == "v2"
    assert snapshot["pin_membership_digest"] == snapshot["snapshot_id"].rsplit(
        ":", 1
    )[-1]
    identity = audit["evidence_state"]["instrument_identity"]
    assert identity["symbol"] == "601328.SS"
    assert identity["venue"] == "XSHG"
    assert identity["instrument_kind"] == "equity"
    market_return_fact = next(
        fact
        for fact in audit["evidence_state"]["source_facts"]
        if fact["canonical_field"] == "market.close_return_20d"
    )
    lineage = market_return_fact["calculation_lineage"]
    assert lineage["calculation_id"] == "market.close_return_20d.qfq"
    assert lineage["effective_range_start"] == "2026-06-26"
    assert lineage["effective_range_end"] == "2026-07-24"
    assert lineage["observations_used"] == 21
    assert lineage["adjustment_basis"] == "qfq"
    assert audit["evidence_preflight"]["passed"] is tradeable

    if tradeable:
        assert Decimal(snapshot["latest_traded_close"]) == Decimal("10.75")
        assert snapshot["carried_suspension_close"] is None
        assert model.invoke.call_count > 0
    else:
        assert captured_state["terminal_outcome_kind"] == "analysis_outcome"
        assert captured_state["analysis_outcome_contract"]["reason"] == (
            "instrument_currently_suspended"
        )
        assert Decimal(snapshot["latest_traded_close"]) == Decimal("10.00")
        assert Decimal(snapshot["carried_suspension_close"]) == Decimal("10.25")
        assert audit["trading_decision"] is None
        assert audit["direction_selection"] is None
        assert captured_state.get("trading_decision") is None
        assert captured_state.get("final_trade_decision") is None
        rendered_outcome = next(
            content
            for section_name, content in display.reports
            if section_name == "analysis_outcome"
        )
        assert "**Reason Code:** `instrument_currently_suspended`" in rendered_outcome
        assert re.search(
            r"\b(?:buy|hold|sell|position|signal)\b",
            rendered_outcome,
            re.IGNORECASE,
        ) is None
        assert sum(
            stage["model_calls"]
            for stage in audit["telemetry"]["stages"].values()
        ) == 0
        assert sum(
            stage["tool_calls"]
            for stage in audit["telemetry"]["stages"].values()
        ) == 0
        model.invoke.assert_not_called()
        model.stream.assert_not_called()
    assert signal_processor.calls == 0
    assert memory_path.read_bytes() == memory_before


@pytest.mark.unit
def test_cli_and_programmatic_suspension_contracts_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _suspended_evidence()
    programmatic_graph = _SuspendedProgrammaticGraph(
        tmp_path / "programmatic_graph",
        evidence,
    )
    programmatic_state, _ = programmatic_graph.propagate(
        "600519.SS",
        "2026-07-24",
    )
    programmatic_audit = json.loads(
        Path(programmatic_state["decision_audit_path"]).read_text("utf-8")
    )

    cli_graph = _SuspendedProgrammaticGraph(tmp_path / "cli_graph", evidence)
    cli_memory_before = cli_graph.memory_path.read_bytes()
    cli_results = tmp_path / "cli_results"
    cli_display = _RecordingDisplay()
    selections = {
        "ticker": "600519.SS",
        "analysis_date": "2026-07-24",
        "asset_type": "stock",
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
            results_dir=str(cli_results),
            evidence_gate_mode="enforce",
            checkpoint_enabled=False,
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "TradingAgentsGraph",
        lambda *_args, **_kwargs: cli_graph,
    )
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: cli_display,
    )
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    cli_main.run_analysis()

    cli_audit_path = next(cli_results.rglob("decision-audit.json"))
    cli_audit = json.loads(cli_audit_path.read_text("utf-8"))
    cli_outcomes = [
        content
        for section_name, content in cli_display.reports
        if section_name == "analysis_outcome"
    ]
    assert cli_outcomes == [programmatic_state["analysis_outcome"]]
    assert "**Reason Code:** `instrument_currently_suspended`" in cli_outcomes[0]
    assert "**Latest Genuinely Traded Close:** `10.25`" in cli_outcomes[0]
    assert re.search(
        r"\b(?:buy|hold|sell|position|signal)\b",
        cli_outcomes[0],
        re.IGNORECASE,
    ) is None
    assert cli_audit["terminal"] == programmatic_audit["terminal"]
    assert cli_audit["analysis_outcome_contract"] == programmatic_audit[
        "analysis_outcome_contract"
    ]
    assert cli_audit["evidence_state"]["market_snapshot"] == programmatic_audit[
        "evidence_state"
    ]["market_snapshot"]
    assert cli_audit["evidence_preflight"] == programmatic_audit[
        "evidence_preflight"
    ]
    assert cli_audit["trading_decision"] is None
    assert cli_audit["direction_selection"] is None
    assert cli_graph.signal_processor.calls == 0
    assert cli_graph.memory_path.read_bytes() == cli_memory_before
    assert cli_graph.boundary_llm.mock_calls == []
    assert sum(
        stage["model_calls"]
        for stage in cli_audit["telemetry"]["stages"].values()
    ) == 0
    assert sum(
        stage["tool_calls"]
        for stage in cli_audit["telemetry"]["stages"].values()
    ) == 0
