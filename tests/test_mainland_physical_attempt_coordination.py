from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests
from typer.testing import CliRunner

import tradingagents.dataflows.akshare_data as akshare_data
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.market_snapshot as market_snapshot
from cli import main as cli_main
from tradingagents.agents.utils.core_stock_tools import get_stock_data
from tradingagents.dataflows.acquisition import AcquisitionController, RetryPolicy
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    build_evidence_state,
    render_analysis_outcome,
)
from tradingagents.evidence_artifacts import (
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryStore,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.reporting import write_report_tree


class _QuietCliDisplay:
    def start(self) -> None:
        return None

    def refresh(self, _spinner_text=None) -> None:
        return None

    def publish_event(self, _event) -> None:
        return None

    def report_ready(self, _section_name, _content, _path) -> None:
        return None

    def close(self) -> None:
        return None


def _runtime_config(tmp_path) -> dict:
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
    runtime_config["data_vendors"]["core_stock_apis"] = "akshare"
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "akshare"
    return runtime_config


def _analysis_outcome_state(evidence) -> dict:
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,),
    )
    return {
        "company_of_interest": "600519.SS",
        "trade_date": "2026-07-24",
        "asset_type": "stock",
        "graph_signature": "analysts=market|evidence_schema=4",
        "evidence_gate_mode": "enforce",
        "evidence_state": evidence.model_dump(mode="json"),
        "evidence_preflight": {
            "passed": False,
            "readiness": "insufficient",
            "blockers": ["authoritative instrument identity is missing"],
            "diagnostic_codes": ["identity_unavailable"],
        },
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
    }


def _hist_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
            "Amount": [1000.0],
        }
    )


class _EastmoneyResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object | None = None) -> None:
        self._payload = (
            {
                "data": {
                    "klines": [
                        "2026-07-24,10,10.5,11,9,100,1000,0,0,0,10"
                    ]
                }
            }
            if payload is None
            else payload
        )

    def json(self):
        return self._payload


def _persisted_attempt_count(runtime_config: dict) -> int:
    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        row = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _diagnostic_only_attempt_count(runtime_config: dict) -> int:
    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        row = store._connection.execute(
            "SELECT COUNT(*) FROM history_store_diagnostics "
            "WHERE operation = 'provider_request' AND code = 'physical_attempt' "
            "AND detail NOT LIKE '%attempt_event_id%'"
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.unit
def test_mainland_eastmoney_physical_attempt_has_exact_public_cardinality(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    transport_calls: list[str] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "2026-07-24,10,10.5,11,9,100,1000,0,0,0,10"
                    ]
                }
            }

    def get(url, **_kwargs):
        transport_calls.append(url)
        return Response()

    monkeypatch.setattr(requests, "get", get)

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-01",
        "2026-07-24",
    )
    evidence = build_evidence_state(
        symbol="600519.SS",
        identity={},
        snapshot=snapshot,
    )
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    write_report_tree(
        _analysis_outcome_state(evidence),
        "600519.SS",
        tmp_path / "reports",
    )
    report = (
        tmp_path / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")

    assert len(transport_calls) == 1
    assert _persisted_attempt_count(runtime_config) == 1
    assert len(snapshot.physical_attempt_events) == 1
    assert snapshot.physical_attempt_events[0].outcome.value == "available"
    assert snapshot.adjustment_basis == "qfq"
    assert evidence.physical_attempt_count == 1
    assert audit.physical_attempt_count == 1
    assert report.count("### Physical attempt") == 1
    assert "**Total physical-attempt count:** 1" in report


@pytest.mark.unit
def test_mainland_akshare_cli_tool_snapshot_has_exact_public_cardinality(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls: list[str] = []

    def get(url, **_kwargs):
        transport_calls.append(url)
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run():
        rendered = get_stock_data.invoke(
            {
                "symbol": "600519.SS",
                "start_date": "2026-07-01",
                "end_date": "2026-07-24",
            }
        )
        snapshot = market_snapshot.get_active_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-24",
        )

    assert snapshot is not None
    evidence = build_evidence_state(
        symbol="600519.SS",
        identity={},
        snapshot=snapshot,
    )
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    write_report_tree(
        _analysis_outcome_state(evidence),
        "600519.SS",
        tmp_path / "reports",
    )
    report = (
        tmp_path / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")

    assert "# Provider: akshare" in rendered
    assert len(transport_calls) == 1
    assert _persisted_attempt_count(runtime_config) == 1
    assert len(snapshot.physical_attempt_events) == 1
    assert evidence.physical_attempt_count == 1
    assert audit.physical_attempt_count == 1
    assert report.count("### Physical attempt") == 1


@pytest.mark.unit
def test_mainland_akshare_cli_runner_preserves_exact_public_cardinality(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    runtime_config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.log"),
            "evidence_gate_mode": "enforce",
            "checkpoint_enabled": False,
        }
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls: list[str] = []

    def get(url, **_kwargs):
        transport_calls.append(url)
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)
    model = MagicMock()

    def forbidden_tool_node(_state):
        raise AssertionError("blocked preflight must stop downstream tool work")

    workflow = GraphSetup(
        model,
        model,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            forbidden_tool_node,
        ),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        evidence_gate_mode="enforce",
    ).setup_graph(["market"])
    model.reset_mock()

    class MainlandCliGraph:
        def __init__(self) -> None:
            self.graph = workflow.compile()
            self.propagator = Propagator()

        def resolve_evidence_state(self, ticker: str, trade_date: str):
            get_stock_data.invoke(
                {
                    "symbol": ticker,
                    "start_date": "2026-07-01",
                    "end_date": trade_date,
                }
            )
            snapshot = market_snapshot.get_active_authoritative_market_snapshot(
                ticker,
                trade_date,
            )
            assert snapshot is not None
            return build_evidence_state(
                symbol=ticker,
                identity={},
                snapshot=snapshot,
            )

        def create_initial_state(
            self,
            ticker: str,
            trade_date: str,
            *,
            asset_type: str,
            evidence_state,
        ):
            return self.propagator.create_initial_state(
                ticker,
                trade_date,
                asset_type=asset_type,
                evidence_state=evidence_state,
            )

    selections = {
        "ticker": "600519.SS",
        "analysis_date": "2026-07-24",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": runtime_config["quick_think_llm"],
        "deep_thinker": runtime_config["deep_think_llm"],
        "backend_url": runtime_config["backend_url"],
        "llm_provider": runtime_config["llm_provider"],
        "google_thinking_level": runtime_config["google_thinking_level"],
        "openai_reasoning_effort": runtime_config["openai_reasoning_effort"],
        "anthropic_effort": runtime_config["anthropic_effort"],
        "output_language": runtime_config["output_language"],
    }
    captured_state: dict = {}
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts):
        captured_state.update(final_state)
        original_mark_completed(final_state, artifacts)

    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", runtime_config)
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "TradingAgentsGraph",
        lambda *_args, **_kwargs: MainlandCliGraph(),
    )
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: _QuietCliDisplay(),
    )
    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    cli_result = CliRunner().invoke(
        cli_main.app,
        ["analyze", "--no-checkpoint"],
    )

    assert cli_result.exit_code == 0, cli_result.output
    audit_path = next((tmp_path / "results").rglob("decision-audit.json"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    report_path = next((tmp_path / "results").rglob("complete_report.md"))
    report = report_path.read_text(encoding="utf-8")

    assert len(transport_calls) == 1
    assert _persisted_attempt_count(runtime_config) == 1
    assert captured_state["evidence_state"]["physical_attempt_count"] == 1
    assert len(captured_state["evidence_state"]["physical_attempt_events"]) == 1
    assert audit["evidence_state"]["physical_attempt_count"] == 1
    assert len(audit["evidence_state"]["physical_attempt_events"]) == 1
    assert report.count("### Physical attempt") == 1
    assert "**Total physical-attempt count:** 1" in report
    assert model.mock_calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("transport_result", "expected"),
    (
        (TimeoutError("timeout"), PhysicalAttemptOutcome.TIMEOUT),
        (
            VendorRateLimitError(
                status_code=429,
                error_code="EASTMONEY_HTTP_429",
                retry_after_seconds=30,
            ),
            PhysicalAttemptOutcome.RATE_LIMITED,
        ),
        (
            NoMarketDataError("600519.SS", "600519.SS", "no rows"),
            PhysicalAttemptOutcome.EMPTY_FRAME,
        ),
        ({"unexpected": "payload"}, PhysicalAttemptOutcome.MALFORMED_RESPONSE),
        (ConnectionError("disconnect"), PhysicalAttemptOutcome.DISCONNECT),
        (PermissionError("unauthorized"), PhysicalAttemptOutcome.AUTHENTICATION),
        (RuntimeError("provider failed"), PhysicalAttemptOutcome.PROVIDER_ERROR),
    ),
)
def test_mainland_akshare_physical_attempt_failures_remain_distinct(
    tmp_path,
    monkeypatch,
    transport_result,
    expected: PhysicalAttemptOutcome,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)

    def get(_url, **_kwargs):
        if isinstance(transport_result, BaseException):
            raise transport_result
        return _EastmoneyResponse(transport_result)

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events = tuple(run.physical_attempt_events)

    assert len(events) == 1
    assert events[0].outcome is expected


@pytest.mark.unit
def test_mainland_eastmoney_retry_after_mapping_reaches_typed_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)

    class Response:
        status_code = 429
        headers = requests.structures.CaseInsensitiveDict({"Retry-After": "30"})

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: Response())

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events = tuple(run.physical_attempt_events)

    assert len(events) == 1
    assert events[0].outcome is PhysicalAttemptOutcome.RATE_LIMITED
    assert events[0].retry_after_seconds == 30
    assert events[0].cooldown_changed is True


@pytest.mark.unit
def test_mainland_akshare_retry_then_success_has_exact_physical_cardinality(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", lambda _seconds: None)
    transport_calls: list[int] = []

    def get(_url, **_kwargs):
        transport_calls.append(len(transport_calls) + 1)
        if len(transport_calls) == 1:
            raise TimeoutError("retry once")
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        run.acquisition_controller = AcquisitionController(
            providers=(),
            retry_policy=RetryPolicy(max_attempts_per_provider=2),
            outcome_observer=run.telemetry_ledger.record_acquisition,
        )
        snapshot = market_snapshot.get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-01",
            "2026-07-24",
        )

    evidence = build_evidence_state(
        symbol="600519.SS",
        identity={},
        snapshot=snapshot,
    )
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    write_report_tree(
        _analysis_outcome_state(evidence),
        "600519.SS",
        tmp_path / "reports",
    )
    report = (
        tmp_path / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")

    assert transport_calls == [1, 2]
    assert _persisted_attempt_count(runtime_config) == 2
    assert [event.outcome for event in snapshot.physical_attempt_events] == [
        PhysicalAttemptOutcome.TIMEOUT,
        PhysicalAttemptOutcome.AVAILABLE,
    ]
    assert evidence.physical_attempt_count == 2
    assert audit.physical_attempt_count == 2
    assert report.count("### Physical attempt") == 2


@pytest.mark.unit
def test_mainland_akshare_exact_exhaustion_has_no_hidden_extra_request(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", lambda _seconds: None)
    transport_calls: list[int] = []

    def get(_url, **_kwargs):
        transport_calls.append(len(transport_calls) + 1)
        raise TimeoutError("exhausted")

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        run.acquisition_controller = AcquisitionController(
            providers=(),
            retry_policy=RetryPolicy(max_attempts_per_provider=3),
            outcome_observer=run.telemetry_ledger.record_acquisition,
        )
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        evidence = market_snapshot.refresh_active_evidence_physical_attempts(
            EvidenceState()
        )

    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    write_report_tree(
        _analysis_outcome_state(evidence),
        "600519.SS",
        tmp_path / "reports",
    )
    report = (
        tmp_path / "reports" / "5_portfolio" / "analysis_outcome.md"
    ).read_text(encoding="utf-8")

    assert transport_calls == [1, 2, 3]
    assert _persisted_attempt_count(runtime_config) == 3
    assert [event.outcome for event in evidence.physical_attempt_events] == [
        PhysicalAttemptOutcome.TIMEOUT.value,
        PhysicalAttemptOutcome.TIMEOUT.value,
        PhysicalAttemptOutcome.TIMEOUT.value,
    ]
    assert evidence.physical_attempt_count == 3
    assert audit.physical_attempt_count == 3
    assert report.count("### Physical attempt") == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "opaque_endpoint", "expected_secid"),
    (
        ("600519.SS", "stock_zh_a_hist", "1.600519"),
        ("510500.SS", "fund_etf_hist_em", "1.510500"),
        ("166009.SZ", "fund_lof_hist_em", "0.166009"),
    ),
)
def test_mainland_akshare_unobservable_library_subrequests_are_disabled(
    tmp_path,
    monkeypatch,
    symbol: str,
    opaque_endpoint: str,
    expected_secid: str,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    transport_calls: list[tuple[str, dict]] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "2026-07-24,10,10.5,11,9,100,1000,0,0,0,10"
                    ]
                }
            }

    def get(url, **kwargs):
        transport_calls.append((url, kwargs))
        return Response()

    def opaque_library_endpoint(**_kwargs):
        raise AssertionError("opaque AKShare subrequests must be disabled")

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        akshare_data.ak,
        opaque_endpoint,
        opaque_library_endpoint,
    )

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        symbol,
        "2026-07-01",
        "2026-07-24",
    )

    assert len(transport_calls) == 1
    assert transport_calls[0][1]["params"]["secid"] == expected_secid
    assert transport_calls[0][1]["params"]["fqt"] == "1"
    assert [event.outcome for event in snapshot.physical_attempt_events] == [
        PhysicalAttemptOutcome.AVAILABLE
    ]


@pytest.mark.unit
def test_mainland_adapter_exposes_one_typed_event_per_physical_subrequest(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", lambda _seconds: None)
    transport_calls: list[str] = []

    def loader(
        _symbol,
        _start_date,
        _end_date,
        *,
        physical_request,
    ):
        physical_request(
            "instrument-metadata",
            lambda: transport_calls.append("instrument-metadata") or "listed",
        )
        return physical_request(
            "current-frame",
            lambda: transport_calls.append("current-frame") or _hist_frame(),
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": market_snapshot.SnapshotProvider(
                loader,
                "qfq",
                uses_typed_physical_requests=True,
            )
        },
    )

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-01",
        "2026-07-24",
    )

    assert transport_calls == ["instrument-metadata", "current-frame"]
    assert _persisted_attempt_count(runtime_config) == 2
    assert [event.outcome for event in snapshot.physical_attempt_events] == [
        PhysicalAttemptOutcome.AVAILABLE,
        PhysicalAttemptOutcome.AVAILABLE,
    ]


@pytest.mark.unit
def test_mainland_adapter_that_bypasses_typed_physical_executor_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls: list[str] = []

    def bypassing_loader(
        _symbol,
        _start_date,
        _end_date,
        *,
        physical_request: object,
    ) -> pd.DataFrame:
        assert physical_request is not None
        transport_calls.append("bypassed")
        return _hist_frame()

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": market_snapshot.SnapshotProvider(
                bypassing_loader,
                "qfq",
                uses_typed_physical_requests=True,
            )
        },
    )

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events = tuple(run.physical_attempt_events)

    assert transport_calls == ["bypassed"]
    assert events == ()
    assert _persisted_attempt_count(runtime_config) == 0


@pytest.mark.unit
def test_mainland_snapshot_cache_hit_adds_zero_physical_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls: list[int] = []

    def get(_url, **_kwargs):
        transport_calls.append(len(transport_calls) + 1)
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        first = market_snapshot.get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-01",
            "2026-07-24",
        )
        events_after_first = tuple(run.physical_attempt_events)
        second = market_snapshot.get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-01",
            "2026-07-24",
        )
        events_after_cache_hit = tuple(run.physical_attempt_events)

    assert second.snapshot_id == first.snapshot_id
    pd.testing.assert_frame_equal(second.frame, first.frame)
    assert transport_calls == [1]
    assert len(events_after_first) == 1
    assert events_after_cache_hit == events_after_first
    assert _persisted_attempt_count(runtime_config) == 1


@pytest.mark.unit
def test_mainland_provider_cooldown_skip_creates_zero_physical_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: observed_at)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            "akshare"
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)
        coordinator.record_rate_limit(
            upstream_service_id=upstream_service_id,
            cooldown_scope="market-snapshot",
            observed_at=observed_at,
            retry_after=timedelta(minutes=5),
            provider_code="EASTMONEY_HTTP_429",
        )
    transport_calls = 0

    def get(_url, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events = tuple(run.physical_attempt_events)

    assert transport_calls == 0
    assert events == ()
    assert _persisted_attempt_count(runtime_config) == 0


@pytest.mark.unit
def test_mainland_operator_policy_skip_creates_zero_physical_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    runtime_config["data_usage_mode"] = "production"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls = 0

    def get(_url, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        return _EastmoneyResponse()

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events = tuple(run.physical_attempt_events)

    assert transport_calls == 0
    assert events == ()
    assert _persisted_attempt_count(runtime_config) == 0


@pytest.mark.unit
def test_mainland_run_circuit_open_skip_adds_zero_physical_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_calls: list[str] = []

    def get(_url, **_kwargs):
        transport_calls.append("akshare")
        raise RuntimeError("provider failed")

    monkeypatch.setattr(requests, "get", get)

    with market_snapshot.authoritative_snapshot_run() as run:
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-01",
                "2026-07-24",
            )
        events_after_failure = tuple(run.physical_attempt_events)
        with pytest.raises(NoMarketDataError):
            market_snapshot.get_authoritative_market_snapshot(
                "600519.SS",
                "2026-07-02",
                "2026-07-25",
            )
        events_after_circuit_skip = tuple(run.physical_attempt_events)

    assert transport_calls == ["akshare"]
    assert len(events_after_failure) == 1
    assert events_after_circuit_skip == events_after_failure
    assert _persisted_attempt_count(runtime_config) == 1


@pytest.mark.unit
def test_mainland_single_flight_follower_adds_zero_physical_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    runtime_config["market_history_mode"] = "disabled"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    transport_entered = Event()
    release_transport = Event()
    follower_entered = Event()
    direct_calls_lock = Lock()
    direct_calls = 0
    transport_calls = 0

    def get(_url, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        transport_entered.set()
        assert release_transport.wait(timeout=5)
        return _EastmoneyResponse()

    original_execute = ProviderRequestCoordinator.execute_direct_physical_request

    def observed_execute(self, **kwargs):
        nonlocal direct_calls
        with direct_calls_lock:
            direct_calls += 1
            call_index = direct_calls
        if call_index == 2:
            follower_entered.set()
        return original_execute(self, **kwargs)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        ProviderRequestCoordinator,
        "execute_direct_physical_request",
        observed_execute,
    )

    def acquire_snapshot():
        return market_snapshot.get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-01",
            "2026-07-24",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(acquire_snapshot)
        assert transport_entered.wait(timeout=5)
        follower = executor.submit(acquire_snapshot)
        assert follower_entered.wait(timeout=5)
        release_transport.set()
        snapshots = (leader.result(timeout=5), follower.result(timeout=5))

    event_ids = {
        event.attempt_event_id
        for snapshot in snapshots
        for event in snapshot.physical_attempt_events
    }
    assert transport_calls == 1
    assert _persisted_attempt_count(runtime_config) == 1
    assert len(event_ids) == 1


@pytest.mark.unit
def test_mainland_sequential_fallback_records_only_requests_actually_made(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    runtime_config["market_history_mode"] = "disabled"
    runtime_config["yahoo_max_physical_attempts"] = 1
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,yfinance,baostock"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    call_order: list[str] = []

    class ProviderErrorResponse:
        status_code = 500
        headers: dict[str, str] = {}

    def eastmoney_get(_url, **_kwargs):
        call_order.append("akshare")
        return ProviderErrorResponse()

    def yahoo_loader(*_args):
        call_order.append("yfinance")
        return _hist_frame()

    def baostock_must_not_run(*_args):
        call_order.append("baostock")
        raise AssertionError("fallback continued after an available frame")

    monkeypatch.setattr(requests, "get", eastmoney_get)
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": market_snapshot.SNAPSHOT_PROVIDERS["akshare"],
            "yfinance": market_snapshot.SnapshotProvider(
                yahoo_loader,
                "auto_adjusted",
            ),
            "baostock": market_snapshot.SnapshotProvider(
                baostock_must_not_run,
                "qfq",
            ),
        },
    )

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-01",
        "2026-07-24",
    )

    assert snapshot.provider == "yfinance"
    assert call_order == ["akshare", "yfinance"]
    assert [event.outcome for event in snapshot.physical_attempt_events] == [
        PhysicalAttemptOutcome.PROVIDER_ERROR,
        PhysicalAttemptOutcome.AVAILABLE,
    ]
    assert _persisted_attempt_count(runtime_config) == 2
    assert _diagnostic_only_attempt_count(runtime_config) == 0
