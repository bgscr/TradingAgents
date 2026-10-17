from __future__ import annotations

import copy
import importlib.util
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from queue import Queue
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pandas as pd
import pytest

import tradingagents.graph.financial_tools as financial_tools_module
import tradingagents.graph.trading_graph as trading_graph_module
import tradingagents.qualified_routing_composition as composition_module
import tradingagents.qualified_routing_provider_clients as provider_clients_module
from cli import main as cli_main
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingDisposition,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialConsolidationScope,
    FinancialStatementType,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.financial_tools import QualifiedFinancialRoutingComposition
from tradingagents.graph.trading_graph import CheckpointSession, TradingAgentsGraph
from tradingagents.market_history import PhysicalAttemptFailure, PhysicalAttemptOutcome
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionBaoStockRunTransport,
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
    ProductionQualifiedRoutingRunBinding,
    ProductionQualifiedRoutingRuntimeAssembly,
    ProductionQualifiedRoutingRuntimeFactories,
    build_production_qualified_routing_composition_blueprint,
    build_registered_production_qualified_routing_composition,
    production_qualified_routing_runtime_factories,
)
from tradingagents.run_telemetry import RunTelemetryLedger


@pytest.mark.parametrize("invalid_blueprint_first", [False, True])
def test_production_coordinator_requires_blueprint_before_resource_allocation(
    monkeypatch, invalid_blueprint_first
):
    allocations = []

    class SyntheticStore:
        @classmethod
        def open_provider_request_authority(cls, _config):
            allocations.append("store")
            return cls()

    def construct_coordinator(*_args, **_kwargs):
        allocations.append("coordinator")
        return object()

    monkeypatch.setattr(composition_module, "MarketHistoryStore", SyntheticStore)
    monkeypatch.setattr(composition_module, "ProviderRequestCoordinator", construct_coordinator)
    factories = production_qualified_routing_runtime_factories()
    if invalid_blueprint_first:
        with pytest.raises(ProductionQualifiedRoutingLifecycleError) as captured:
            factories.open_coordinator_store(object())
        assert captured.value.reason is ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as captured:
        factories.build_coordinator(SyntheticStore(), Event())
    assert captured.value.reason is ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
    assert allocations == []


def _blueprint_and_binding(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ["statements"],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
            "data_cache_dir": str(tmp_path / "cache"),
            "market_history_database_path": str(
                tmp_path / "market-history" / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(
                tmp_path / "market-history" / "payloads"
            ),
            "market_history_backup_root": str(
                tmp_path / "market-history" / "backups"
            ),
            "results_dir": str(tmp_path / "results"),
        }
    )
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    preflight = preflight_mainland_capability_routing(asset, config=config)
    assert preflight.plan is not None
    plan = preflight.plan
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    blueprint = build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=config,
        validation_workload=True,
        disposition=MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
        dependency_availability={
            "akshare": True,
            "baostock": True,
            "tushare": True,
            "yfinance": True,
        },
        provider_source_coverage={
            "akshare": True,
            "akshare_sina": True,
            "baostock": True,
            "baostock_qualified_families": True,
            "tushare": True,
            "yfinance": True,
            "yfinance_derived": True,
        },
        factory_contract_version=(
            PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION
        ),
    )
    binding = ProductionQualifiedRoutingRunBinding(
        run_id="run:" + "a" * 64,
        asset_configuration=asset,
        routing_plan=plan,
        checkpoint_compatibility_known=True,
    )
    return blueprint, binding


@dataclass
class _Resource:
    name: str
    events: list[tuple[object, ...]]
    stop_signal: object | None = None
    fail_close: bool = False

    def interrupt(self) -> None:
        is_stopped = self.stop_signal.is_set() if self.stop_signal is not None else None
        self.events.append(("interrupt", self.name, is_stopped))

    def close(self) -> None:
        self.events.append(("close", self.name))
        if self.fail_close:
            raise RuntimeError(
                "raw cleanup secret https://example.invalid/?token=credential C:\\private"
            )


def _runtime_factories(
    *,
    plan,
    events: list[tuple[object, ...]],
    fail_after: str | None = None,
    fail_close: str | None = None,
    construction_failure: BaseException | None = None,
) -> ProductionQualifiedRoutingRuntimeFactories:
    def resource(name: str, stop_signal=None):
        return _Resource(
            name=name,
            events=events,
            stop_signal=stop_signal,
            fail_close=name == fail_close,
        )

    def phase_hook(phase: str, _value: object) -> None:
        events.append(("created", phase))
        if phase == fail_after:
            raise construction_failure or KeyboardInterrupt(
                "original construction interruption"
            )

    def assemble(**_kwargs) -> ProductionQualifiedRoutingRuntimeAssembly:
        routing = QualifiedFinancialRoutingComposition(
            router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            statement_request_factory=lambda _request: None,
        )
        return ProductionQualifiedRoutingRuntimeAssembly(
            financial_routing=routing,
            owned_resources=(
                ("provider_adapters", resource("provider_adapters")),
                ("request_factories", resource("request_factories")),
            ),
        )

    return ProductionQualifiedRoutingRuntimeFactories(
        open_coordinator_store=lambda _blueprint: resource("coordinator_store"),
        build_coordinator=lambda _store, stop: resource("coordinator", stop),
        build_artifact_store=lambda _blueprint: resource("artifact_store"),
        build_subrequest_cache=lambda _coordinator, _artifacts, _run_id, stop: resource(
            "subrequest_cache", stop
        ),
        build_tushare_client=lambda _blueprint, stop: resource(
            "tushare_client", stop
        ),
        build_akshare_facade=lambda _blueprint, stop: resource(
            "akshare_facade", stop
        ),
        build_yahoo_transport=lambda _blueprint, stop: resource(
            "yahoo_transport", stop
        ),
        build_baostock_transport=lambda _blueprint, stop: resource(
            "baostock_transport", stop
        ),
        assemble_runtime=assemble,
        phase_hook=phase_hook,
    )


def test_binding_same_run_is_idempotent_and_another_active_run_fails_typed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(plan=binding.routing_plan, events=events),
    )

    first = composition.bind_run(binding)
    second = composition.bind_run(binding)

    assert second is first
    assert composition.runtime is first
    different = binding.model_copy(update={"run_id": "run:" + "b" * 64})
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        composition.bind_run(different)
    assert raised.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.DIFFERENT_RUN_ACTIVE
    )
    assert [event for event in events if event[0] == "created"].count(
        ("created", "coordinator_store")
    ) == 1


def test_successful_runtime_close_and_interrupt_are_jointly_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(plan=binding.routing_plan, events=events),
    )
    runtime = composition.bind_run(binding)

    composition.interrupt()
    composition.interrupt()
    composition.close()
    composition.close()

    assert runtime.stop_signal.is_set()
    interrupts = [event for event in events if event[0] == "interrupt"]
    assert interrupts
    assert all(event[2] is True for event in interrupts if event[2] is not None)
    close_order = [event[1] for event in events if event[0] == "close"]
    assert close_order == [
        "request_factories",
        "provider_adapters",
        "baostock_transport",
        "yahoo_transport",
        "akshare_facade",
        "tushare_client",
        "subrequest_cache",
        "artifact_store",
        "coordinator",
        "coordinator_store",
    ]
    assert composition.cleanup_diagnostics == ()


def test_concurrent_interrupt_and_close_terminalize_each_resource_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(plan=binding.routing_plan, events=events),
    )
    runtime = composition.bind_run(binding)
    gate = Barrier(9)

    def terminalize(operation: str) -> None:
        gate.wait(timeout=5)
        getattr(composition, operation)()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(terminalize, "interrupt" if index % 2 else "close")
            for index in range(8)
        ]
        gate.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)

    close_events = [event for event in events if event[0] == "close"]
    assert runtime.stop_signal.is_set()
    assert len(close_events) == 10
    assert len({event[1] for event in close_events}) == 10
    assert composition.cleanup_diagnostics == ()


def test_parallel_compositions_keep_run_resources_and_stop_state_isolated(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, first_binding = _blueprint_and_binding(tmp_path, monkeypatch)
    second_binding = first_binding.model_copy(update={"run_id": "run:" + "b" * 64})
    first_events: list[tuple[object, ...]] = []
    second_events: list[tuple[object, ...]] = []
    first = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=first_binding.routing_plan,
            events=first_events,
        ),
    )
    second = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=second_binding.routing_plan,
            events=second_events,
        ),
    )
    gate = Barrier(3)

    def bind(composition, binding):
        gate.wait(timeout=5)
        return composition.bind_run(binding)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(bind, first, first_binding)
        second_future = executor.submit(bind, second, second_binding)
        gate.wait(timeout=5)
        first_runtime = first_future.result(timeout=5)
        second_runtime = second_future.result(timeout=5)

    assert first_runtime is not second_runtime
    assert first_runtime.stop_signal is not second_runtime.stop_signal
    assert first_runtime.run_id != second_runtime.run_id
    first.interrupt()
    first.close()
    assert first_runtime.stop_signal.is_set()
    assert not second_runtime.stop_signal.is_set()
    assert not [event for event in second_events if event[0] == "close"]

    second.close()
    assert len([event for event in first_events if event[0] == "close"]) == 10
    assert len([event for event in second_events if event[0] == "close"]) == 10


@pytest.mark.parametrize(
    "fail_after",
    [
        "coordinator_store",
        "coordinator",
        "artifact_store",
        "subrequest_cache",
        "tushare_client",
        "akshare_facade",
        "yahoo_transport",
        "baostock_transport",
        "provider_adapters",
        "request_factories",
    ],
)
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_partial_construction_failure_closes_only_created_resources_in_reverse(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fail_after: str,
    failure_type: type[BaseException],
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    original = failure_type("original construction interruption")
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=binding.routing_plan,
            events=events,
            fail_after=fail_after,
            construction_failure=original,
        ),
    )

    with pytest.raises(failure_type) as captured:
        composition.bind_run(binding)

    assert captured.value is original
    created = [event[1] for event in events if event[0] == "created"]
    closed = [event[1] for event in events if event[0] == "close"]
    assert closed == list(reversed(created))
    assert composition.runtime is None


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failure_during_financial_routing_construction_closes_request_factory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    original_factory = composition_module._ProductionQualifiedRequestFactories
    request_factories: list[object] = []
    close_events: list[str] = []
    original = KeyboardInterrupt("financial routing construction interrupted")

    class TrackingRequestFactories(original_factory):
        def __init__(self, run_binding) -> None:
            super().__init__(run_binding)
            request_factories.append(self)

        def close(self) -> None:
            close_events.append("request_factories")
            super().close()
            if cleanup_fails:
                raise SystemExit(
                    "raw cleanup token=credential https://provider.invalid/?secret=value"
                )

    def fail_financial_routing(**_kwargs):
        raise original

    monkeypatch.setattr(
        composition_module,
        "_ProductionQualifiedRequestFactories",
        TrackingRequestFactories,
    )
    monkeypatch.setattr(
        financial_tools_module,
        "QualifiedFinancialRoutingComposition",
        fail_financial_routing,
    )
    client_events: list[tuple[object, ...]] = []
    runtime_factories = replace(
        production_qualified_routing_runtime_factories(),
        build_tushare_client=lambda _blueprint, _stop: _Resource(
            "tushare_client",
            client_events,
        ),
        build_akshare_facade=lambda _blueprint, _stop: _Resource(
            "akshare_facade",
            client_events,
        ),
        build_yahoo_transport=lambda _blueprint, _stop: _Resource(
            "yahoo_transport",
            client_events,
        ),
        build_baostock_transport=lambda _blueprint, _stop: _Resource(
            "baostock_transport",
            client_events,
        ),
    )
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=runtime_factories,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        composition.bind_run(binding)

    assert captured.value is original
    assert len(request_factories) == 1
    assert close_events == ["request_factories"]
    assert composition.runtime is None
    diagnostics = composition.cleanup_diagnostics
    assert len(diagnostics) == int(cleanup_fails)
    if cleanup_fails:
        assert diagnostics[0].resource_kind == "request_factories"
        rendered = repr((composition, diagnostics))
        assert "token=credential" not in rendered
        assert "provider.invalid" not in rendered
        assert "secret=value" not in rendered


def test_cleanup_failure_does_not_replace_original_construction_exception(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=binding.routing_plan,
            events=events,
            fail_after="coordinator",
            fail_close="coordinator_store",
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="original construction interruption"):
        composition.bind_run(binding)

    assert len(composition.cleanup_diagnostics) == 1
    diagnostic = composition.cleanup_diagnostics[0]
    assert diagnostic.resource_kind == "coordinator_store"
    rendered = repr((composition, diagnostic))
    assert "token=" not in rendered
    assert "example.invalid" not in rendered
    assert "C:\\private" not in rendered


def test_default_binding_sanitizes_raw_store_construction_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    monkeypatch.setattr(
        composition_module.MarketHistoryStore,
        "open_provider_request_authority",
        lambda _config: (_ for _ in ()).throw(
            OSError(
                "raw token=credential https://provider.invalid/?payload=private "
                "C:\\private\\market.sqlite3"
            )
        ),
    )
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
    )

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as captured:
        composition.bind_run(binding)

    assert captured.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
    )
    rendered = repr((composition, captured.value, composition.cleanup_diagnostics))
    assert "token=credential" not in rendered
    assert "provider.invalid" not in rendered
    assert "C:\\private" not in rendered
    assert composition.runtime is None


def test_run_request_factories_reject_caller_supplied_eligibility_material(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    factories = composition_module._ProductionQualifiedRequestFactories(binding)
    request = SimpleNamespace(
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 31),
        material_arguments={
            "company_type": "bank",
            "consolidation_scope": FinancialConsolidationScope.CONSOLIDATED.value,
            "eligible_annual_period_ends": ["2025-12-31"],
            "eligible_reporting_period_ends": ["2026-03-31"],
        },
    )

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as captured:
        factories.statement(request)

    assert captured.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
    )
    factories.close()


class _FakeBaoStockSdk:
    def __init__(self) -> None:
        self.login_calls = 0
        self.logout_calls = 0
        self.query_calls = 0
        self.active_queries = 0
        self.maximum_active_queries = 0
        self.query_started = Event()
        self.allow_query_return = Event()
        self.lock = Lock()

    def login(self):
        self.login_calls += 1
        return SimpleNamespace(error_code="0")

    def logout(self):
        self.logout_calls += 1
        return SimpleNamespace(error_code="0")

    def query_profit_data(self, **_kwargs: object):
        with self.lock:
            self.query_calls += 1
            self.active_queries += 1
            self.maximum_active_queries = max(
                self.maximum_active_queries,
                self.active_queries,
            )
        self.query_started.set()
        assert self.allow_query_return.wait(timeout=5)
        with self.lock:
            self.active_queries -= 1
        return pd.DataFrame([{"code": "sh.601328", "year": "2025"}])


class _PrintingBaoStockResult:
    error_code = "0"
    fields = ("code", "year")

    def __init__(self, canary: str, *, fail_drain: bool = False) -> None:
        self._canary = canary
        self._fail_drain = fail_drain
        self._rows = (("sh.601328", "2025"),)
        self._index = -1

    def next(self) -> bool:
        print(self._canary)
        print(self._canary, file=sys.stderr)
        if self._fail_drain:
            raise RuntimeError("row drain failed")
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> tuple[str, str]:
        print(self._canary)
        print(self._canary, file=sys.stderr)
        if self._fail_drain:
            raise AssertionError("failed drain must not manufacture a row")
        return self._rows[self._index]


class _PrintingBaoStockSdk:
    def __init__(self, canary: str, *, fail_drain: bool = False) -> None:
        self._canary = canary
        self._fail_drain = fail_drain
        self.login_calls = 0
        self.logout_calls = 0

    def login(self):
        self.login_calls += 1
        print(self._canary)
        print(self._canary, file=sys.stderr)
        return SimpleNamespace(error_code="0")

    def query_profit_data(self, **_kwargs: object):
        print(self._canary)
        print(self._canary, file=sys.stderr)
        return _PrintingBaoStockResult(
            self._canary,
            fail_drain=self._fail_drain,
        )

    def logout(self):
        self.logout_calls += 1
        print(self._canary)
        print(self._canary, file=sys.stderr)
        return SimpleNamespace(error_code="0")


def test_baostock_login_is_lazy_serialized_and_logged_out_exactly_once() -> None:
    sdk = _FakeBaoStockSdk()
    stop_signal = Event()
    transport = ProductionBaoStockRunTransport(
        sdk_module=sdk,
        stop_signal=stop_signal,
    )

    assert sdk.login_calls == 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            transport.query_profit_data,
            code="sh.601328",
            year=2025,
            quarter=4,
        )
        assert sdk.query_started.wait(timeout=5)
        second = executor.submit(
            transport.query_profit_data,
            code="sh.601328",
            year=2025,
            quarter=3,
        )
        sdk.allow_query_return.set()
        assert not first.result(timeout=5).empty
        assert not second.result(timeout=5).empty

    transport.close()
    transport.close()

    assert sdk.login_calls == 1
    assert sdk.query_calls == 2
    assert sdk.maximum_active_queries == 1
    assert sdk.logout_calls == 1


def test_baostock_terminal_failure_logs_out_and_cannot_reopen() -> None:
    class FailingSdk(_FakeBaoStockSdk):
        def query_profit_data(self, **_kwargs: object):
            raise RuntimeError(
                "raw token=credential https://provider.invalid/?payload=private"
            )

    sdk = FailingSdk()
    transport = ProductionBaoStockRunTransport(
        sdk_module=sdk,
        stop_signal=Event(),
    )

    with pytest.raises(PhysicalAttemptFailure) as captured:
        transport.query_profit_data(code="sh.601328", year=2025, quarter=4)
    with pytest.raises(PhysicalAttemptFailure) as repeated:
        transport.query_profit_data(code="sh.601328", year=2025, quarter=3)
    transport.close()

    assert captured.value.outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert repeated.value.outcome is PhysicalAttemptOutcome.ABANDONED
    assert sdk.login_calls == 1
    assert sdk.logout_calls == 1
    assert "provider.invalid" not in repr((transport, captured.value))


def test_baostock_worker_control_path_logs_out_while_query_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeBaoStockSdk()
    request_queue: Queue[object] = Queue()
    response_queue: Queue[object] = Queue()
    monkeypatch.setattr(
        provider_clients_module.importlib,
        "import_module",
        lambda _name: sdk,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(
            provider_clients_module._baostock_run_worker,
            request_queue,
            response_queue,
        )
        request_queue.put(
            (
                "query",
                "query_profit_data",
                {"code": "sh.601328", "year": 2025, "quarter": 4},
            )
        )
        assert sdk.query_started.wait(timeout=5)
        request_queue.put(("close",))
        try:
            assert response_queue.get(timeout=5) == ("closing",)
            assert sdk.logout_calls == 0
            assert not worker.done()
            sdk.allow_query_return.set()
            assert worker.result(timeout=5) is None
        finally:
            sdk.allow_query_return.set()
            worker.result(timeout=5)

    assert sdk.login_calls == 1
    assert sdk.logout_calls == 1


def test_baostock_worker_contains_sdk_output_and_preserves_success_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    raw_canary = "[WinError 10057] qualified worker socket is not connected"
    sdk = _PrintingBaoStockSdk(raw_canary)
    request_queue: Queue[object] = Queue()
    response_queue: Queue[object] = Queue()
    monkeypatch.setattr(
        provider_clients_module.importlib,
        "import_module",
        lambda _name: sdk,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(
            provider_clients_module._baostock_run_worker,
            request_queue,
            response_queue,
        )
        request_queue.put(
            (
                "query",
                "query_profit_data",
                {"code": "sh.601328", "year": 2025, "quarter": 4},
            )
        )
        assert response_queue.get(timeout=5) == (
            "ok",
            ("code", "year"),
            (("sh.601328", "2025"),),
        )
        request_queue.put(("close",))
        assert worker.result(timeout=5) is None

    captured = capsys.readouterr()
    assert sdk.login_calls == 1
    assert sdk.logout_calls == 1
    assert raw_canary not in captured.out
    assert raw_canary not in captured.err


def test_baostock_worker_contains_row_drain_output_and_retains_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    raw_canary = "[WinError 10057] qualified row drain socket is not connected"
    sdk = _PrintingBaoStockSdk(raw_canary, fail_drain=True)
    request_queue: Queue[object] = Queue()
    response_queue: Queue[object] = Queue()
    monkeypatch.setattr(
        provider_clients_module.importlib,
        "import_module",
        lambda _name: sdk,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(
            provider_clients_module._baostock_run_worker,
            request_queue,
            response_queue,
        )
        request_queue.put(
            (
                "query",
                "query_profit_data",
                {"code": "sh.601328", "year": 2025, "quarter": 4},
            )
        )
        response = response_queue.get(timeout=5)
        assert worker.result(timeout=5) is None

    captured = capsys.readouterr()
    assert response == ("failure", "baostock_provider_error")
    assert sdk.logout_calls == 1
    assert raw_canary not in repr(response)
    assert raw_canary not in captured.out
    assert raw_canary not in captured.err


def test_baostock_run_transport_contains_sdk_output_and_preserves_typed_error(
    capsys,
) -> None:
    raw_canary = "[WinError 10057] qualified transport socket is not connected"
    sdk = _PrintingBaoStockSdk(raw_canary, fail_drain=True)
    transport = ProductionBaoStockRunTransport(
        sdk_module=sdk,
        stop_signal=Event(),
    )

    with pytest.raises(PhysicalAttemptFailure) as captured_failure:
        transport.query_profit_data(code="sh.601328", year=2025, quarter=4)

    captured_output = capsys.readouterr()
    assert captured_failure.value.outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert captured_failure.value.error_code == "baostock_provider_error"
    assert sdk.login_calls == 1
    assert sdk.logout_calls == 1
    assert raw_canary not in repr(captured_failure.value)
    assert raw_canary not in captured_output.out
    assert raw_canary not in captured_output.err


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit()])
def test_baostock_process_startup_preserves_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    class FakeQueue:
        def put(self, _value: object) -> None:
            return None

        def close(self) -> None:
            return None

        def join_thread(self) -> None:
            return None

    class FakeProcess:
        pid = None

        @staticmethod
        def start() -> None:
            raise failure

        @staticmethod
        def is_alive() -> bool:
            return False

    class FakeContext:
        @staticmethod
        def Queue():
            return FakeQueue()

        @staticmethod
        def Process(**_kwargs: object):
            return FakeProcess()

    monkeypatch.setattr(multiprocessing, "get_context", lambda _mode: FakeContext())
    transport = provider_clients_module.production_baostock_process_transport(Event())

    with pytest.raises(type(failure)):
        transport.query_profit_data(code="sh.601328", year=2025, quarter=4)

    transport.close()


def test_default_runtime_binding_creates_run_resources_without_provider_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)

    class TushareModule:
        def __init__(self) -> None:
            self.client_creations = 0
            self.endpoint_calls = 0

        def pro_api(self, _credential: str):
            self.client_creations += 1
            module = self

            class Client:
                def balancesheet(self, **_kwargs: object):
                    module.endpoint_calls += 1
                    return pd.DataFrame()

            return Client()

    tushare_module = TushareModule()
    baostock_module = _FakeBaoStockSdk()
    modules = {
        "tushare": tushare_module,
        "akshare": SimpleNamespace(),
        "yfinance": SimpleNamespace(),
        "baostock": baostock_module,
    }
    monkeypatch.setattr(
        composition_module.importlib,
        "import_module",
        lambda name: modules[name],
    )
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
    )

    runtime = composition.bind_run(binding)
    dependencies = runtime.dependencies
    store = dependencies.coordinator._store
    attempt_count = store._connection.execute(
        "SELECT COUNT(*) FROM provider_request_attempts"
    ).fetchone()
    lease_count = store._connection.execute(
        "SELECT COUNT(*) FROM request_leases"
    ).fetchone()
    composition.close()
    composition.close()

    assert runtime.resource_kinds == (
        "coordinator_store",
        "coordinator",
        "artifact_store",
        "subrequest_cache",
        "tushare_client",
        "akshare_facade",
        "yahoo_transport",
        "baostock_transport",
        "provider_adapters",
        "request_factories",
    )
    assert tushare_module.client_creations == 1
    assert tushare_module.endpoint_calls == 0
    assert baostock_module.login_calls == 0
    assert baostock_module.logout_calls == 0
    assert attempt_count == (0,)
    assert lease_count == (0,)
    assert store._closed is True
    assert not tuple(
        blueprint.provider_artifact_root_path.rglob("*.bin")
    )


def _graph_host(
    *,
    composition,
    binding: ProductionQualifiedRoutingRunBinding,
    compile_failure: BaseException | None = None,
):
    class Workflow:
        def compile(self, *, checkpointer=None):
            del checkpointer
            if compile_failure is not None:
                raise compile_failure
            return SimpleNamespace()

    class Setup:
        tool_nodes = None

        @staticmethod
        def setup_graph(_selected_analysts):
            return Workflow()

    host = SimpleNamespace(
        config={},
        _managed_qualified_routing_composition=composition,
        asset_configuration=binding.asset_configuration,
        capability_routing_plan=binding.routing_plan,
        qualified_financial_routing=composition,
        _create_tool_nodes=lambda: {"fundamentals": object()},
        graph_setup=Setup(),
        selected_analysts=("fundamentals",),
        _active_checkpointer=None,
        asset_type="stock",
        ticker=None,
    )
    host.bind_qualified_routing_runtime = lambda **kwargs: (
        TradingAgentsGraph.bind_qualified_routing_runtime(host, **kwargs)
    )
    host.interrupt_qualified_routing_runtime = lambda: (
        TradingAgentsGraph.interrupt_qualified_routing_runtime(host)
    )
    host.close_qualified_routing_runtime = lambda: (
        TradingAgentsGraph.close_qualified_routing_runtime(host)
    )
    host._configure_asset_type = lambda _asset_type: None
    host._normalize_asset_type = lambda value: value
    return host


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_graph_initialization_failure_interrupts_and_closes_bound_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=binding.routing_plan,
            events=events,
        ),
    )
    original = failure_type("graph initialization stopped")
    host = _graph_host(
        composition=composition,
        binding=binding,
        compile_failure=original,
    )

    with pytest.raises(failure_type) as captured:
        TradingAgentsGraph.bind_qualified_routing_runtime(
            host,
            run_id=binding.run_id,
            checkpoint_compatibility_known=True,
        )

    assert captured.value is original
    assert composition.runtime is not None
    assert composition.runtime.stop_signal.is_set()
    assert [event[1] for event in events if event[0] == "close"] == [
        "request_factories",
        "provider_adapters",
        "baostock_transport",
        "yahoo_transport",
        "akshare_facade",
        "tushare_client",
        "subrequest_cache",
        "artifact_store",
        "coordinator",
        "coordinator_store",
    ]


@pytest.mark.parametrize(
    "original",
    [
        RuntimeError("graph execution failed"),
        KeyboardInterrupt("graph interrupted"),
        SystemExit("graph exited"),
    ],
)
def test_programmatic_execution_baseexception_interrupts_then_closes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    original: BaseException,
) -> None:
    blueprint, binding = _blueprint_and_binding(tmp_path, monkeypatch)
    events: list[tuple[object, ...]] = []
    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=_runtime_factories(
            plan=binding.routing_plan,
            events=events,
        ),
    )
    host = _graph_host(composition=composition, binding=binding)

    @contextmanager
    def checkpoint_scope(*_args, **_kwargs):
        yield CheckpointSession({}, False)

    @contextmanager
    def snapshot_scope(*_args, **_kwargs):
        yield SimpleNamespace()

    monkeypatch.setattr(TradingAgentsGraph, "checkpoint_scope", checkpoint_scope)
    monkeypatch.setattr(
        trading_graph_module,
        "authoritative_snapshot_run",
        snapshot_scope,
    )

    def fail_after_binding(*_args, **_kwargs):
        host.bind_qualified_routing_runtime(
            run_id=binding.run_id,
            checkpoint_compatibility_known=True,
        )
        raise original

    host._run_graph = fail_after_binding

    with pytest.raises(type(original)) as captured:
        TradingAgentsGraph.propagate(host, "601328.SS", "2026-08-03")

    assert captured.value is original
    assert composition.runtime is not None
    assert composition.runtime.stop_signal.is_set()
    assert len([event for event in events if event[0] == "close"]) == 10


class _RecordingQualifiedLifecycleOwner:
    def __init__(self) -> None:
        self.stop_signal = Event()
        self.events: list[str] = []
        self._lock = Lock()
        self._interrupted = False
        self._closed = False

    def interrupt(self) -> None:
        self.stop_signal.set()
        with self._lock:
            if self._interrupted:
                return
            self._interrupted = True
            self.events.append("interrupt")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.events.append("close")


def _run_cli_lifecycle_phase(
    *,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    owner: _RecordingQualifiedLifecycleOwner,
    phase: str,
    failure: BaseException,
    checkpoint_cleanup_failure: BaseException | None = None,
    failure_publication_failure: BaseException | None = None,
) -> None:
    telemetry_ledger = RunTelemetryLedger()

    @contextmanager
    def snapshot_scope(*_args, **_kwargs):
        yield SimpleNamespace(telemetry_ledger=telemetry_ledger)

    class FakePropagator:
        @staticmethod
        def get_graph_args(*_args, **_kwargs):
            return {}

    class FakeStream:
        @staticmethod
        def stream(*_args, **_kwargs):
            if phase == "provider_dispatch":
                raise failure
            yield {"messages": []}

    class FakeTradingAgentsGraph:
        def __init__(self, *_args, **_kwargs) -> None:
            if phase == "graph_initialization":
                raise failure
            self.asset_configuration = None
            self.propagator = FakePropagator()
            self.graph = FakeStream()

        @contextmanager
        def checkpoint_scope(self, *_args, **_kwargs):
            try:
                yield CheckpointSession({}, False)
            finally:
                terminal_failure = (
                    failure
                    if phase == "checkpoint_publication"
                    else checkpoint_cleanup_failure
                )
                if terminal_failure is not None:
                    raise terminal_failure

        @staticmethod
        def create_initial_state(ticker, trade_date, **kwargs):
            return {
                "run_id": "run:" + "c" * 64,
                "company_of_interest": ticker,
                "trade_date": str(trade_date),
                "asset_type": kwargs["asset_type"],
                "messages": [],
            }

        @staticmethod
        def resolve_evidence_state(*_args, **_kwargs):
            return None

        @staticmethod
        def bind_qualified_routing_runtime(**_kwargs) -> None:
            return None

        @staticmethod
        def clear_run_checkpoint(*_args, **_kwargs) -> None:
            return None

        @staticmethod
        def interrupt_qualified_routing_runtime() -> None:
            owner.interrupt()

        @staticmethod
        def close_qualified_routing_runtime() -> None:
            owner.close()

    class NoOpDisplay:
        def start(self) -> None:
            return None

        def refresh(self, _spinner_text=None) -> None:
            return None

        def publish_event(self, _event) -> None:
            return None

        def report_ready(self, *_args, **_kwargs) -> None:
            return None

        def close(self) -> None:
            return None

    def write_reports(*_args, **_kwargs) -> None:
        if phase == "report_publication":
            raise failure

    def mark_completed(*_args, **_kwargs) -> None:
        if phase == "report_completion":
            raise failure

    selections = {
        "ticker": "AAPL",
        "analysis_date": "2026-08-03",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "gpt-5-mini",
        "deep_thinker": "gpt-5",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    monkeypatch.setattr(cli_main, "message_buffer", cli_main.MessageBuffer())
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path)),
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeTradingAgentsGraph)
    monkeypatch.setattr(cli_main, "authoritative_snapshot_run", snapshot_scope)
    def create_display(*_args, **_kwargs):
        if phase == "display_initialization":
            raise failure
        return NoOpDisplay()

    monkeypatch.setattr(cli_main, "create_run_display", create_display)
    monkeypatch.setattr(cli_main, "_write_run_reports", write_reports)
    monkeypatch.setattr(cli_main, "_mark_run_completed", mark_completed)
    if failure_publication_failure is not None:
        def fail_publication(*_args, **_kwargs) -> None:
            raise failure_publication_failure

        monkeypatch.setattr(cli_main, "_mark_run_failed", fail_publication)

    cli_main.run_analysis(qualified_financial_routing=owner)


@pytest.mark.parametrize(
    "phase",
    [
        "display_initialization",
        "graph_initialization",
        "provider_dispatch",
        "checkpoint_publication",
        "report_publication",
        "report_completion",
    ],
)
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_cli_phase_baseexception_interrupts_then_closes_managed_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure_type: type[BaseException],
) -> None:
    owner = _RecordingQualifiedLifecycleOwner()
    original = failure_type(f"{phase} failed")

    with pytest.raises(failure_type) as captured:
        _run_cli_lifecycle_phase(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            owner=owner,
            phase=phase,
            failure=original,
        )

    assert captured.value is original
    assert owner.stop_signal.is_set()
    assert owner.events == ["interrupt", "close"]


@pytest.mark.parametrize(
    "phase",
    ["display_initialization", "graph_initialization"],
)
def test_failure_publication_baseexception_does_not_mask_primary_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    owner = _RecordingQualifiedLifecycleOwner()
    original = RuntimeError(f"{phase} failed")

    with pytest.raises(RuntimeError) as captured:
        _run_cli_lifecycle_phase(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            owner=owner,
            phase=phase,
            failure=original,
            failure_publication_failure=SystemExit("failure publication exited"),
        )

    assert captured.value is original
    assert owner.events == ["interrupt", "close"]


def test_cli_checkpoint_cleanup_failure_does_not_mask_dispatch_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _RecordingQualifiedLifecycleOwner()
    original = KeyboardInterrupt("provider dispatch interrupted")
    cleanup_failure = SystemExit("checkpoint cleanup failed")

    with pytest.raises(KeyboardInterrupt) as captured:
        _run_cli_lifecycle_phase(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            owner=owner,
            phase="provider_dispatch",
            failure=original,
            checkpoint_cleanup_failure=cleanup_failure,
        )

    assert captured.value is original
    assert owner.events == ["interrupt", "close"]
