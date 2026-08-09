from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

import cli.main as cli_main
import tradingagents.capability_routing as capability_routing_module
import tradingagents.dataflows.financial_dispatch as financial_dispatch_module
import tradingagents.dataflows.financial_eligibility_metadata as metadata_module
import tradingagents.dataflows.interface as vendor_interface
import tradingagents.qualified_routing_composition as composition_module
from tests.test_financial_capability_routing_selection import (
    _indicator_response_with_compatibility,
    _qualified_ratio_candidate,
    _with_raw_indicator_metadata,
)
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialIndicatorEvidenceRequirements,
    FinancialRatioFamily,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointLedger,
    FinancialIndicatorRequestArguments,
    FinancialToolDispatcher,
    project_financial_dispatch_ledger,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    FinancialIndicatorMetadataDimension,
    FinancialIndicatorMetadataEvidenceOrigin,
    FinancialIndicatorSemanticAssessmentState,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import FinancialDispatchToolNode
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.qualified_routing_composition import (
    ProductionQualifiedRoutingRuntime,
    RegisteredProductionQualifiedRoutingComposition,
)

_AS_OF = date(2026, 9, 1)
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_ENABLED_CAPABILITIES = (
    "statements",
    "financial_indicators",
    "adjustment_factors",
    "name_events",
)
_THREAD_ID = "0600000000000004"


def _patch_tushare_dependency_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class _InstalledTushareProbe:
        class util:
            @staticmethod
            def find_spec(module_name: str) -> object | None:
                assert module_name == "tushare"
                return object()

    monkeypatch.setattr(capability_routing_module, "importlib", _InstalledTushareProbe)


class _DeterministicDateTime(datetime):
    _current = _NOW
    _lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._current = _NOW

    @classmethod
    def now(cls, tz=None):
        with cls._lock:
            current = cls._current
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)

    @classmethod
    def advance(cls, seconds: float) -> None:
        with cls._lock:
            cls._current += timedelta(seconds=float(seconds))


@dataclass
class _OwnedFakeProvider:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    interrupt_calls: int = 0
    close_calls: int = 0

    def _record(self, endpoint: str, kwargs: dict[str, object]) -> None:
        self.calls.append((endpoint, dict(kwargs)))

    def interrupt(self) -> None:
        self.interrupt_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _periods() -> tuple[date, ...]:
    return (date(2021, 12, 31),) + tuple(
        date(year, month, day)
        for year in range(2022, 2027)
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
        if date(year, month, day) <= _AS_OF
    )


class _TushareProvider(_OwnedFakeProvider):
    def __init__(self, symbol: str, *, reverse_input_order: bool) -> None:
        super().__init__()
        self.symbol = symbol
        self.is_bank = symbol == "601328.SS"
        self.native_symbol = symbol.replace(".SS", ".SH")
        self.reverse_input_order = reverse_input_order

    def _frame(self, rows: list[dict[str, object]]) -> pd.DataFrame:
        frame = pd.DataFrame(rows)
        if self.reverse_input_order:
            frame = frame.iloc[::-1]
            frame = frame[list(reversed(frame.columns))]
        return frame.reset_index(drop=True)

    def _metadata(self, period_end: date) -> dict[str, object]:
        return {
            "ts_code": self.native_symbol,
            "ann_date": min(period_end + timedelta(days=25), _AS_OF).strftime(
                "%Y%m%d"
            ),
            "f_ann_date": min(period_end + timedelta(days=26), _AS_OF).strftime(
                "%Y%m%d"
            ),
            "end_date": period_end.strftime("%Y%m%d"),
            "report_type": "1",
            "comp_type": "2" if self.is_bank else "1",
            "update_flag": "0",
        }

    def balancesheet(self, **kwargs: object) -> pd.DataFrame:
        self._record("balancesheet", kwargs)
        values = (
            {
                "cash_reser_cb": 100,
                "depos": 800,
                "cost_fin_assets": 180,
                "fair_value_fin_assets": 120,
                "depos_in_oth_bfi": 120,
                "depos_oth_bfi": 90,
                "decr_in_disbur": 700,
                "total_assets": 1500,
                "total_hldr_eqy_inc_min_int": 200,
                "total_liab": 1300,
            }
            if self.is_bank
            else {
                "total_assets": 1000,
                "total_liab": 400,
                "total_hldr_eqy_inc_min_int": 600,
                "money_cap": 100,
                "accounts_receiv": 80,
                "acct_payable": 70,
                "inventories": 60,
                "st_borr": 50,
                "lt_borr": 40,
                "fix_assets": 300,
            }
        )
        return self._frame(
            [{**self._metadata(period_end), **values} for period_end in _periods()]
        )

    def income(self, **kwargs: object) -> pd.DataFrame:
        self._record("income", kwargs)
        values = (
            {
                "int_exp": 40,
                "int_income": 180,
                "n_income": 90,
                "comm_income": 30,
                "n_commis_income": 22,
                "income_tax": 20,
                "invest_income": 12,
                "oper_exp": 70,
                "operate_profit": 110,
                "total_profit": 105,
            }
            if self.is_bank
            else {
                "total_revenue": 900,
                "operate_profit": 180,
                "n_income": 120,
                "oper_cost": 500,
                "fin_exp": 20,
                "income_tax": 30,
                "total_cogs": 650,
                "admin_exp": 35,
                "sell_exp": 15,
                "total_profit": 150,
            }
        )
        return self._frame(
            [{**self._metadata(period_end), **values} for period_end in _periods()]
        )

    def cashflow(self, **kwargs: object) -> pd.DataFrame:
        self._record("cashflow", kwargs)
        values = {
            "n_cash_flows_fnc_act": 50,
            "n_cashflow_inv_act": -80,
            "n_cashflow_act": 160,
            "c_paid_for_taxes": 30,
            "c_cash_equ_end_period": 300,
            "eff_fx_flu_cash": 1,
            "n_incr_cash_cash_equ": 131,
            "c_cash_equ_beg_period": 169,
            **(
                {"c_inf_fr_operate_a": 930, "c_pay_dist_dpcp_int_exp": 45}
                if self.is_bank
                else {"c_pay_interest": 10, "c_fr_sale_sg": 920}
            ),
        }
        return self._frame(
            [{**self._metadata(period_end), **values} for period_end in _periods()]
        )

    def fina_indicator(self, **kwargs: object) -> pd.DataFrame:
        self._record("fina_indicator", kwargs)
        rows: list[dict[str, object]] = []
        for index, period_end in enumerate(_periods()):
            common: dict[str, object] = {
                "ts_code": self.native_symbol,
                "ann_date": min(period_end + timedelta(days=25), _AS_OF).strftime(
                    "%Y%m%d"
                ),
                "end_date": period_end.strftime("%Y%m%d"),
                "assets_turn": 1.1 + index / 100,
                "ca_turn": 1.2 + index / 100,
                "fa_turn": 1.3 + index / 100,
                "ar_turn": 1.4 + index / 100,
                "assets_yoy": 8 + index,
                "ocf_yoy": 7 + index,
                "eqt_yoy": 6 + index,
                "netprofit_yoy": 4 + index,
                "op_yoy": 3 + index,
                "tr_yoy": 2 + index,
                "roe_yoy": 1 + index,
                "ebt_yoy": 9 + index,
                "ocf_to_debt": 0.7 + index / 100,
                "q_ocf_to_sales": 18 + index,
            }
            if self.is_bank:
                common.pop("q_ocf_to_sales")
                common.update(
                    {
                        "netprofit_margin": 28 + index,
                        "profit_to_gr": 31 + index,
                        "roa": 1.1 + index / 100,
                        "roe": 12 + index,
                        "roic": 10 + index,
                        "op_of_gr": 20 + index,
                        "cash_ratio": 0.8,
                        "current_ratio": 1.5,
                        "debt_to_assets": 45 + index,
                        "debt_to_eqt": 0.9,
                        "assets_to_eqt": 1.9,
                        "longdeb_to_debt": 30 + index,
                        "quick_ratio": 1.2,
                        "tbassets_to_totalassets": 70 + index,
                        "cost_income_ratio": 30 + index,
                        "net_interest_margin": 2.1 + index / 100,
                        "net_interest_spread": 1.9 + index / 100,
                        "npl_ratio": 1.2 + index / 100,
                        "provision_coverage_ratio": 180 + index,
                        "grossprofit_yoy": 5 + index,
                        "working_capital_yoy": 10 + index,
                    }
                )
            else:
                common.update(
                    {
                        "eps": 1.2 + index / 100,
                        "ebitda_margin": 12 + index,
                        "grossprofit_margin": 40 + index,
                        "netprofit_margin": 10 + index,
                        "op_of_gr": 15 + index,
                        "profit_to_gr": 13 + index,
                        "profit_to_op": 80 + index,
                        "roa": 8 + index,
                        "roe": 16 + index,
                        "roic": 11 + index,
                        "cash_ratio": 0.8,
                        "current_ratio": 1.5,
                        "debt_to_assets": 45 + index,
                        "debt_to_eqt": 0.9,
                        "assets_to_eqt": 1.9,
                        "ebit_to_interest": 6.0,
                        "longdeb_to_debt": 30 + index,
                        "quick_ratio": 1.2,
                        "tbassets_to_totalassets": 70 + index,
                    }
                )
            rows.append(common)
        return self._frame(rows)

    def adj_factor(self, **kwargs: object) -> pd.DataFrame:
        self._record("adj_factor", kwargs)
        return pd.DataFrame(
            {
                "ts_code": [self.native_symbol],
                "trade_date": ["20260807"],
                "adj_factor": ["1.250000"],
            }
        )

    def namechange(self, **kwargs: object) -> pd.DataFrame:
        self._record("namechange", kwargs)
        return pd.DataFrame(
            {
                "ts_code": [self.native_symbol],
                "name": ["descriptive-only-name"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["deterministic fixture"],
            }
        )


class _AkshareProvider(_OwnedFakeProvider):
    def __init__(self, *, reverse_input_order: bool) -> None:
        super().__init__()
        self.reverse_input_order = reverse_input_order

    def stock_financial_report_sina(self, **kwargs: object) -> pd.DataFrame:
        self._record("stock_financial_report_sina", kwargs)
        return pd.DataFrame()

    def stock_financial_analysis_indicator(self, **kwargs: object) -> pd.DataFrame:
        self._record("stock_financial_analysis_indicator", kwargs)
        periods = list(_periods())
        if self.reverse_input_order:
            periods.reverse()
        frame = pd.DataFrame(
            [
                {
                    "日期": period_end.isoformat(),
                    "净利润增长率(%)": 5,
                    "营业收入增长率(%)": 6,
                }
                for period_end in periods
            ]
        )
        if self.reverse_input_order:
            frame = frame[list(reversed(frame.columns))]
        return frame

    def stock_zh_a_daily(self, **kwargs: object) -> pd.DataFrame:
        self._record("stock_zh_a_daily", kwargs)
        return pd.DataFrame()

    def stock_info_change_name(self, **kwargs: object) -> pd.DataFrame:
        self._record("stock_info_change_name", kwargs)
        return pd.DataFrame()


class _YahooTicker:
    def __init__(self, provider: _YahooProvider) -> None:
        self._provider = provider

    def _unused(self, endpoint: str) -> pd.DataFrame:
        self._provider._record(endpoint, {})
        return pd.DataFrame()

    @property
    def balance_sheet(self) -> pd.DataFrame:
        return self._unused("balance_sheet")

    @property
    def quarterly_balance_sheet(self) -> pd.DataFrame:
        return self._unused("quarterly_balance_sheet")

    @property
    def income_stmt(self) -> pd.DataFrame:
        return self._unused("income_stmt")

    @property
    def quarterly_income_stmt(self) -> pd.DataFrame:
        return self._unused("quarterly_income_stmt")

    @property
    def cashflow(self) -> pd.DataFrame:
        return self._unused("cashflow")

    @property
    def quarterly_cashflow(self) -> pd.DataFrame:
        return self._unused("quarterly_cashflow")

    def get_info(self) -> dict[str, str]:
        self._provider._record("get_info", {})
        return {}

    def history(self, **kwargs: object) -> pd.DataFrame:
        self._provider._record("history", kwargs)
        return pd.DataFrame()


class _YahooProvider(_OwnedFakeProvider):
    def Ticker(self, symbol: str) -> _YahooTicker:
        self._record("Ticker", {"symbol": symbol})
        return _YahooTicker(self)


class _BaoStockProvider(_OwnedFakeProvider):
    def __init__(self, symbol: str) -> None:
        super().__init__()
        self.symbol = symbol
        self.code = "sh." + symbol.split(".", maxsplit=1)[0]

    def query_lifecycle(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_lifecycle", kwargs)
        return pd.DataFrame(
            {
                "code": [self.code],
                "code_name": ["fixture issuer"],
                "ipoDate": ["2007-05-15" if self.symbol == "601328.SS" else "1996-04-22"],
                "outDate": [""],
                "type": ["1"],
                "status": ["1"],
            }
        )

    def query_raw_daily(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_raw_daily", kwargs)
        return pd.DataFrame()

    def query_forward_adjustment_factors(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_forward_adjustment_factors", kwargs)
        return pd.DataFrame()

    def query_session_status(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_session_status", kwargs)
        return pd.DataFrame()

    def query_profit_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_profit_data", kwargs)
        return pd.DataFrame()

    def query_operation_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_operation_data", kwargs)
        return pd.DataFrame()

    def query_growth_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_growth_data", kwargs)
        return pd.DataFrame()

    def query_balance_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_balance_data", kwargs)
        return pd.DataFrame()

    def query_cash_flow_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_cash_flow_data", kwargs)
        return pd.DataFrame()

    def query_dupont_data(self, **kwargs: object) -> pd.DataFrame:
        self._record("query_dupont_data", kwargs)
        return pd.DataFrame()


@dataclass
class _FreshRun:
    symbol: str
    config: dict[str, Any]
    asset: object
    plan: object
    composition: RegisteredProductionQualifiedRoutingComposition
    host: TradingAgentsGraph
    runtime: ProductionQualifiedRoutingRuntime
    providers: dict[str, _OwnedFakeProvider]
    resolver_calls: list[object]
    compiled_fundamentals: Any
    deterministic_llm: MagicMock

    @property
    def owner(self):
        return self.host._financial_eligibility_context_owner

    @property
    def tool_node(self) -> FinancialDispatchToolNode:
        return self.host.tool_nodes["fundamentals"]

    def state(
        self,
        *,
        tool_names: tuple[str, ...] = ("get_balance_sheet",),
    ) -> dict[str, object]:
        tool_calls = []
        for index, tool_name in enumerate(tool_names):
            arguments = {
                "ticker": self.symbol,
                "curr_date": _AS_OF.isoformat(),
            }
            if tool_name != "get_fundamentals":
                arguments["freq"] = "quarterly"
            else:
                families = tuple(FinancialRatioFamily)
                requirements = FinancialIndicatorEvidenceRequirements.create(
                    requested_families=families,
                    canonical_evidence_contract_id=(
                        "canonical-evidence-contract.v1"
                    ),
                    capability_profile_id=self.asset.capability_profile.profile_id,
                    strategy_rules_id="mainland-equity-strategy-rules.v1",
                )
                arguments["indicator_request_arguments"] = (
                    FinancialIndicatorRequestArguments(
                        requested_families=families,
                        family_evidence_requirements=requirements,
                    ).model_dump(mode="json")
                )
            tool_calls.append(
                {
                    "name": tool_name,
                    "args": arguments,
                    "id": f"qcf:{index}:{tool_name}",
                    "type": "tool_call",
                }
            )
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "evidence_state": EvidenceState(
                instrument_identity=self.asset.instrument_identity,
                financial_eligibility_binding=self.plan.financial_eligibility_binding,
            ).model_dump(mode="json"),
            "asset_configuration": self.asset.model_dump(mode="json"),
            "capability_routing_plan": self.plan.model_dump(mode="json"),
            "capability_routing_rollout": (
                MainlandCapabilityRoutingRunProjection.from_plan(self.plan).model_dump(
                    mode="json"
                )
            ),
            "financial_eligibility_binding": (
                self.plan.financial_eligibility_binding.model_dump(mode="json")
            ),
            "run_id": self.owner.run_id,
            "trade_date": _AS_OF.isoformat(),
        }

    def close(self) -> None:
        self.host.close_qualified_routing_runtime()

    def invoke(self, state: dict[str, object]) -> dict[str, object]:
        return self.compiled_fundamentals.invoke(state)


def _legacy_vendor_methods() -> dict[str, dict[str, object]]:
    def render_statement(symbol: str, frequency: str, current_date: str) -> str:
        return (
            f"# Deterministic legacy data for {symbol} ({frequency})\n"
            f"# Data retrieved on: {current_date} 00:00:00\n\n"
            "metric,value\nTotal,1"
        )

    def render_fundamentals(symbol: str, current_date: str) -> str:
        return (
            f"# Deterministic legacy data for {symbol}\n"
            f"# Data retrieved on: {current_date} 00:00:00\n\n"
            "metric,value\nTotal,1"
        )

    return {
        "get_fundamentals": {"scripted": render_fundamentals},
        "get_balance_sheet": {"scripted": render_statement},
        "get_cashflow": {"scripted": render_statement},
        "get_income_statement": {"scripted": render_statement},
    }


def _fresh_builtin_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str,
    reverse_input_order: bool,
    checkpoint_thread_id: str = _THREAD_ID,
) -> _FreshRun:
    _DeterministicDateTime.reset()
    monkeypatch.setenv("RUN_LIVE_DEEPSEEK_TESTS", "0")
    monkeypatch.setenv("RUN_LIVE_TUSHARE_TESTS", "0")
    monkeypatch.setenv("TUSHARE_TOKEN", "qcf-secret-canary-never-persist")

    _patch_tushare_dependency_available(monkeypatch)
    monkeypatch.setattr(
        cli_main,
        "production_qualified_routing_dependency_availability",
        lambda: dict.fromkeys(("akshare", "baostock", "tushare", "yfinance"), True),
    )
    monkeypatch.setattr(composition_module, "datetime", _DeterministicDateTime)
    monkeypatch.setattr(financial_dispatch_module, "datetime", _DeterministicDateTime)
    monkeypatch.setattr(
        composition_module.time,
        "sleep",
        _DeterministicDateTime.advance,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": list(_ENABLED_CAPABILITIES),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
            "data_cache_dir": str(tmp_path / "cache"),
            "market_history_database_path": str(tmp_path / "history.sqlite3"),
            "market_history_payload_root": str(tmp_path / "payloads"),
            "market_history_backup_root": str(tmp_path / "backups"),
            "results_dir": str(tmp_path / "results"),
        }
    )
    asset = resolve_run_asset_configuration(symbol, config=config)
    preflight = preflight_mainland_capability_routing(asset, config=config)
    assert preflight.plan is not None
    plan = preflight.plan
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    vendor_methods = _legacy_vendor_methods()
    config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)

    providers: dict[str, _OwnedFakeProvider] = {
        "tushare": _TushareProvider(
            symbol,
            reverse_input_order=reverse_input_order,
        ),
        "akshare": _AkshareProvider(
            reverse_input_order=reverse_input_order,
        ),
        "yfinance": _YahooProvider(),
        "baostock": _BaoStockProvider(symbol),
    }
    production_factories = composition_module.production_qualified_routing_runtime_factories()
    deterministic_factories = replace(
        production_factories,
        build_tushare_client=lambda _blueprint, _stop: providers["tushare"],
        build_akshare_facade=lambda _blueprint, _stop: providers["akshare"],
        build_yahoo_transport=lambda _blueprint, _stop: providers["yfinance"],
        build_baostock_transport=lambda _blueprint, _stop: providers["baostock"],
    )
    monkeypatch.setattr(
        composition_module,
        "production_qualified_routing_runtime_factories",
        lambda: deterministic_factories,
    )
    resolver_calls: list[object] = []
    resolver = metadata_module.resolve_financial_company_type

    def resolve_once(**kwargs: object):
        resolver_calls.append(dict(kwargs))
        return resolver(**kwargs)

    monkeypatch.setattr(metadata_module, "resolve_financial_company_type", resolve_once)
    composition = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(composition, RegisteredProductionQualifiedRoutingComposition)
    assert composition.factory_identity == (
        cli_main.BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY.identity
    )

    deterministic_llm = MagicMock(name="qcf4_no_io_llm")
    deterministic_client = MagicMock(name="qcf4_no_io_llm_client")
    deterministic_client.get_llm.return_value = deterministic_llm
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: deterministic_client,
    )
    host = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=config,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
    )
    assert host.financial_eligibility_context_owner is None
    initial_financial_node = host.tool_nodes["fundamentals"]
    assert isinstance(initial_financial_node, FinancialDispatchToolNode)
    assert initial_financial_node._qualified_context_owner is None
    assert initial_financial_node._qualified_statement_router is None
    run_id = "run:" + sha256(
        f"checkpoint:{checkpoint_thread_id}".encode()
    ).hexdigest()
    runtime = host.bind_qualified_routing_runtime(
        run_id=run_id,
        checkpoint_compatibility_known=True,
        checkpoint_thread_id=checkpoint_thread_id,
        analysis_as_of_date=_AS_OF,
    )
    assert runtime is not None
    financial_node = host.tool_nodes["fundamentals"]
    assert isinstance(financial_node, FinancialDispatchToolNode)
    assert financial_node._qualified_context_owner is (
        host.financial_eligibility_context_owner
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("tools_fundamentals", financial_node)
    workflow.add_edge(START, "tools_fundamentals")
    workflow.add_edge("tools_fundamentals", END)
    compiled_fundamentals = workflow.compile()
    return _FreshRun(
        symbol=symbol,
        config=config,
        asset=asset,
        plan=plan,
        composition=composition,
        host=host,
        runtime=runtime,
        providers=providers,
        resolver_calls=resolver_calls,
        compiled_fundamentals=compiled_fundamentals,
        deterministic_llm=deterministic_llm,
    )


def _attempt_events(shadow_ledger: dict[str, object]) -> tuple[dict[str, object], ...]:
    cache = shadow_ledger["provider_subrequest_cache"]
    assert isinstance(cache, dict)
    entries = cache["entries"]
    assert isinstance(entries, list)
    return tuple(
        event
        for entry in entries
        for event in entry["attempt_events"]
    )


@pytest.mark.unit
def test_qcf4_only_unresolved_indicator_family_continues_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_LIVE_DEEPSEEK_TESTS", "0")
    monkeypatch.setenv("RUN_LIVE_TUSHARE_TESTS", "0")
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-test-token")
    _patch_tushare_dependency_available(monkeypatch)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": list(_ENABLED_CAPABILITIES),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration("600895.SS", config=config)
    preflight = preflight_mainland_capability_routing(asset, config=config)
    assert preflight.plan is not None
    period = date(2026, 3, 31)
    tushare_growth = _qualified_ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.GROWTH,
        period_end=period,
        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
        currency="XXX",
    )
    tushare_balance = _with_raw_indicator_metadata(
        _qualified_ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.BALANCE,
            period_end=period,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
            currency="XXX",
        ),
        currency="XXX",
        scope=FinancialConsolidationScope.CONSOLIDATED,
    )
    akshare_growth = _with_raw_indicator_metadata(
        _qualified_ratio_candidate(
            provider_id="akshare",
            ratio_family=FinancialRatioFamily.GROWTH,
            period_end=period,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
            currency="XXX",
        ),
        currency="CNY",
        scope=FinancialConsolidationScope.CONSOLIDATED,
    )
    calls: list[tuple[str, object]] = []
    later_provider_calls: list[str] = []

    def source(provider_id: str, response: object):
        def invoke(request: object) -> object:
            calls.append((provider_id, request))
            return response

        return invoke

    def later_provider(provider_id: str):
        def unexpected(_request: object) -> object:
            later_provider_calls.append(provider_id)
            raise AssertionError(f"resolved indicator families reached {provider_id}")

        return unexpected

    router = MainlandFinancialCapabilityRouter(
        routing_plan=preflight.plan,
        statement_sources={},
        indicator_sources={
            "tushare": source(
                "tushare",
                _indicator_response_with_compatibility(
                    "tushare",
                    (tushare_growth, tushare_balance),
                    declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
                ),
            ),
            "akshare": source(
                "akshare",
                _indicator_response_with_compatibility(
                    "akshare",
                    (akshare_growth,),
                    declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
                ),
            ),
            "baostock_qualified_families": later_provider(
                "baostock_qualified_families"
            ),
            "yfinance": later_provider("yfinance"),
        },
    )

    result = router.route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=(
                tushare_growth.period_identity.instrument_identity
            ),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(
                FinancialRatioFamily.GROWTH,
                FinancialRatioFamily.BALANCE,
            ),
            eligible_reporting_period_ends=(period,),
        )
    )

    assert tuple(provider_id for provider_id, _request in calls) == (
        "tushare",
        "akshare",
    )
    assert calls[1][1].missing_periods_by_family == (
        (FinancialRatioFamily.GROWTH, (period,)),
    )
    assert later_provider_calls == []
    assert {
        (
            candidate.artifact.dataset.provider_id,
            candidate.period_identity.ratio_family,
        )
        for candidate in result.selected_candidates
    } == {
        ("tushare", FinancialRatioFamily.BALANCE),
        ("akshare", FinancialRatioFamily.GROWTH),
    }
    rejected_growth = next(
        rejection
        for rejection in result.metadata_incompatible_rejections
        if rejection.candidate_identity == tushare_growth.candidate_identity
    )
    assert rejected_growth.completeness_assessment.critical_coverage == 1
    assert rejected_growth.completeness_assessment.core_coverage == 1
    assert rejected_growth.compatibility_assessment.provider_id == "tushare"
    selected_assessments = {
        assessment.candidate_identity: assessment
        for assessment in result.metadata_compatibility_assessments
        if assessment.candidate_identity
        in {candidate.candidate_identity for candidate in result.selected_candidates}
    }
    assert selected_assessments[tushare_balance.candidate_identity].provider_id == (
        "tushare"
    )
    assert selected_assessments[akshare_growth.candidate_identity].provider_id == (
        "akshare"
    )
    assert result.physical_attempt_ids == (
        "tushare-attempt-1",
        "akshare-attempt-1",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "indicator_arguments"),
    (
        ("get_fundamentals", []),
        ("get_balance_sheet", {}),
    ),
)
def test_managed_financial_tool_rejects_incompatible_nested_indicator_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    indicator_arguments: object,
) -> None:
    monkeypatch.setenv("RUN_LIVE_DEEPSEEK_TESTS", "0")
    monkeypatch.setenv("RUN_LIVE_TUSHARE_TESTS", "0")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["tool_vendors"] = dict.fromkeys(_legacy_vendor_methods(), "scripted")
    asset = resolve_run_asset_configuration("600895.SS", config=config)
    arguments: dict[str, object] = {
        "ticker": "600895.SS",
        "curr_date": _AS_OF.isoformat(),
        "indicator_request_arguments": indicator_arguments,
    }
    if tool_name != "get_fundamentals":
        arguments["freq"] = "quarterly"
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=_legacy_vendor_methods(),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": arguments,
                        "id": f"qcf4-invalid-{tool_name}",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
        ).model_dump(mode="json"),
        "asset_configuration": asset.model_dump(mode="json"),
        "trade_date": _AS_OF.isoformat(),
    }

    with pytest.raises(
        ValueError,
        match="financial indicator request arguments are incompatible",
    ):
        node(state)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "expected_company_type", "reverse_input_order"),
    (
        ("601328.SS", FinancialCompanyType.BANK, False),
        ("601328.SS", FinancialCompanyType.BANK, True),
        ("600895.SS", FinancialCompanyType.INDUSTRIAL_NON_BANK, False),
        ("600895.SS", FinancialCompanyType.INDUSTRIAL_NON_BANK, True),
    ),
)
def test_qcf4_fresh_builtin_retains_incompatible_tushare_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    expected_company_type: FinancialCompanyType,
    reverse_input_order: bool,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol=symbol,
        reverse_input_order=reverse_input_order,
    )
    assert isinstance(fresh.host, TradingAgentsGraph)
    context_bind_calls: list[object] = []
    statement_dispatches: list[object] = []
    indicator_dispatches: list[object] = []
    dispatch_failures: list[BaseException] = []
    original_bind_context = fresh.owner.bind_context
    original_statement_dispatch = (
        FinancialToolDispatcher.dispatch_statement_selection
    )
    original_indicator_dispatch = (
        FinancialToolDispatcher.dispatch_indicator_selection
    )

    def bind_context_once(context):
        context_bind_calls.append(context)
        return original_bind_context(context)

    def capture_statement(dispatcher, request, selection_request):
        try:
            dispatched = original_statement_dispatch(
                dispatcher,
                request,
                selection_request,
            )
        except BaseException as exc:
            dispatch_failures.append(exc)
            raise
        statement_dispatches.append(dispatched)
        return dispatched

    def capture_indicator(dispatcher, request, selection_request):
        try:
            dispatched = original_indicator_dispatch(
                dispatcher,
                request,
                selection_request,
            )
        except BaseException as exc:
            dispatch_failures.append(exc)
            raise
        indicator_dispatches.append(dispatched)
        return dispatched

    monkeypatch.setattr(fresh.owner, "bind_context", bind_context_once)
    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_statement_selection",
        capture_statement,
    )
    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_indicator_selection",
        capture_indicator,
    )
    try:
        assert fresh.owner is not None
        assert fresh.owner.context is None
        assert fresh.runtime.financial_routing is None
        assert all(provider.calls == [] for provider in fresh.providers.values())
        state = fresh.state(
            tool_names=("get_balance_sheet", "get_fundamentals"),
        )
        initial_evidence_state = copy.deepcopy(state["evidence_state"])
        assert "financial_dispatch_shadow_ledger" not in state
        assert "financial_dispatch_ledger" not in state

        result = fresh.invoke(state)

        assert not dispatch_failures, repr(dispatch_failures)
        assert "financial_dispatch_shadow_failure" not in result, (
            result.get("financial_dispatch_shadow_failure"),
            len(statement_dispatches),
            len(indicator_dispatches),
            fresh.owner.context,
        )
        shadow_ledger = result["financial_dispatch_shadow_ledger"]
        assert isinstance(shadow_ledger, dict)
        assert len(statement_dispatches) == 1
        assert len(indicator_dispatches) == 1
        indicator_result = indicator_dispatches[0]
        indicator_ledger = shadow_ledger
        assert isinstance(indicator_ledger, dict)
        statement_routing = statement_dispatches[0].routing_result
        routing = indicator_result.routing_result
        context = fresh.owner.context
        assert context is not None
        assert context_bind_calls == [context]
        assert context.company_type_resolution.company_type is expected_company_type
        assert context.company_type_evidence.provider_id == "tushare"
        assert context.listing_provenance.provider_id == "baostock"
        assert context.listing_provenance.authoritative is True
        assert len(fresh.resolver_calls) == 1
        assert fresh.plan.mode.value == "qualified_v1_shadow"
        assert (
            fresh.plan.financial_eligibility_binding.rollout_authority_disposition
            == "shadow_non_authoritative"
        )
        assert result["evidence_state"] == initial_evidence_state

        assert statement_routing.route == (
            "tushare",
            "akshare_sina",
            "yfinance",
        )
        assert tuple(call.provider_id for call in statement_routing.provider_calls) == (
            "tushare",
        )
        assert statement_routing.selected_candidates
        assert all(
            candidate.artifact.dataset.provider_id == "tushare"
            for candidate in statement_routing.selected_candidates
        )
        assert statement_routing.missing_annual_period_ends == ()
        assert statement_routing.missing_reporting_period_ends == ()
        assert len(statement_routing.physical_attempt_ids) == 1

        all_families = tuple(FinancialRatioFamily)
        assert routing.route == (
            "tushare",
            "akshare",
            "baostock_qualified_families",
            "yfinance",
        )
        assert routing.complete_families == ()
        assert routing.unresolved_families == all_families
        assert routing.selected_candidates == ()
        assert tuple(call.provider_id for call in routing.provider_calls) == routing.route
        calls_by_provider = {
            call.provider_id: call for call in routing.provider_calls
        }
        assert tuple(
            family
            for family, _periods_for_family in calls_by_provider[
                "tushare"
            ].missing_periods_by_family
        ) == all_families
        assert tuple(
            family
            for family, _periods_for_family in calls_by_provider[
                "akshare"
            ].missing_periods_by_family
        ) == all_families
        for provider_id in ("baostock_qualified_families", "yfinance"):
            assert tuple(
                family
                for family, _periods_for_family in calls_by_provider[
                    provider_id
                ].missing_periods_by_family
            ) == all_families

        tushare_assessments = tuple(
            assessment
            for assessment in routing.metadata_compatibility_assessments
            if assessment.provider_id == "tushare"
        )
        akshare_assessments = tuple(
            assessment
            for assessment in routing.metadata_compatibility_assessments
            if assessment.provider_id == "akshare"
        )
        assert tushare_assessments
        assert akshare_assessments
        assert all(not assessment.compatible for assessment in tushare_assessments)
        assert all(
            not assessment.compatible
            and not assessment.current_only_metadata_eligible
            and not assessment.strict_pit_metadata_eligible
            for assessment in akshare_assessments
        )
        tushare_value_currency_dimensions = tuple(
            dimension
            for assessment in tushare_assessments
            for field_assessment in assessment.field_assessments
            for dimension in field_assessment.dimensions
            if dimension.dimension
            is FinancialIndicatorMetadataDimension.VALUE_CURRENCY
        )
        tushare_scope_dimensions = tuple(
            dimension
            for assessment in tushare_assessments
            for field_assessment in assessment.field_assessments
            for dimension in field_assessment.dimensions
            if dimension.dimension
            is FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE
        )
        assert tushare_value_currency_dimensions
        assert all(
            dimension.state
            is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
            and dimension.raw_provider_marker == "XXX"
            and dimension.raw_provider_value is None
            and dimension.evidence_value is None
            and dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.FIELD_SEMANTICS_POLICY
            for dimension in tushare_value_currency_dimensions
        )
        assert not any(
            dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT
            and dimension.evidence_value == "CNY"
            for dimension in tushare_value_currency_dimensions
        )
        assert tushare_scope_dimensions
        assert all(
            dimension.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
            and dimension.raw_provider_marker == "UNKNOWN"
            and dimension.evidence_value is None
            and dimension.expected_semantic_value == "consolidated"
            and dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT
            for dimension in tushare_scope_dimensions
        )
        tushare_instrument_currency_dimensions = tuple(
            dimension
            for assessment in tushare_assessments
            for field_assessment in assessment.field_assessments
            for dimension in field_assessment.dimensions
            if dimension.dimension
            is FinancialIndicatorMetadataDimension.INSTRUMENT_CURRENCY
        )
        assert len(tushare_instrument_currency_dimensions) == len(
            tushare_value_currency_dimensions
        ) == len(tushare_scope_dimensions)
        assert all(
            dimension.state is FinancialIndicatorSemanticAssessmentState.PROVEN
            and dimension.evidence_value == "CNY"
            and dimension.raw_provider_marker is None
            and dimension.raw_provider_value is None
            and dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.AUTHORITATIVE_INSTRUMENT_IDENTITY
            for dimension in tushare_instrument_currency_dimensions
        )
        akshare_scope_dimensions = tuple(
            dimension
            for assessment in akshare_assessments
            for field_assessment in assessment.field_assessments
            for dimension in field_assessment.dimensions
            if dimension.dimension
            is FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE
        )
        assert all(
            dimension.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
            and dimension.raw_provider_marker == "UNKNOWN"
            and dimension.evidence_value is None
            and dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT
            for dimension in akshare_scope_dimensions
        )
        akshare_value_currency_dimensions = tuple(
            dimension
            for assessment in akshare_assessments
            for field_assessment in assessment.field_assessments
            for dimension in field_assessment.dimensions
            if dimension.dimension
            is FinancialIndicatorMetadataDimension.VALUE_CURRENCY
        )
        assert akshare_value_currency_dimensions
        assert all(
            dimension.state
            is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
            and dimension.raw_provider_marker == "XXX"
            and dimension.evidence_value is None
            and dimension.safe_evidence_origin
            is FinancialIndicatorMetadataEvidenceOrigin.FIELD_SEMANTICS_POLICY
            for dimension in akshare_value_currency_dimensions
        )

        manifests = {
            manifest.requested_ratio_family: manifest
            for manifest in routing.family_manifests
        }
        growth_manifest = manifests[FinancialRatioFamily.GROWTH]
        growth_tushare = tuple(
            wrapper
            for wrapper in growth_manifest.provider_candidates
            if wrapper.candidate.artifact.dataset.provider_id == "tushare"
        )
        growth_akshare = tuple(
            wrapper
            for wrapper in growth_manifest.provider_candidates
            if wrapper.candidate.artifact.dataset.provider_id == "akshare"
        )
        assert growth_tushare and growth_akshare
        assert all(
            wrapper.candidate.artifact.dataset.schema_identity
            == "akshare-financial-indicators-v2"
            for wrapper in growth_akshare
        )
        assert all(
            wrapper.assessment.critical_coverage == 1
            and wrapper.assessment.core_coverage == 1
            and wrapper.metadata_compatibility_state == "incompatible"
            and wrapper.metadata_selection_disposition == "unselected"
            for wrapper in growth_tushare
        )
        assert all(
            wrapper.metadata_compatibility_state == "incompatible"
            and wrapper.metadata_selection_disposition == "unselected"
            for wrapper in growth_akshare
        )
        assert growth_manifest.selected_families == ()
        assert growth_manifest.unresolved_families == (
            FinancialRatioFamily.GROWTH,
        )
        if expected_company_type is FinancialCompanyType.BANK:
            bank_cash_flow_tushare = tuple(
                wrapper
                for wrapper in manifests[
                    FinancialRatioFamily.CASH_FLOW
                ].provider_candidates
                if wrapper.candidate.artifact.dataset.provider_id == "tushare"
            )
            assert bank_cash_flow_tushare
            assert all(
                wrapper.assessment.core_coverage < 1
                for wrapper in bank_cash_flow_tushare
            )
        else:
            assert all(
                wrapper.assessment.critical_coverage == 1
                and wrapper.assessment.core_coverage == 1
                for manifest in routing.family_manifests
                for wrapper in manifest.provider_candidates
                if wrapper.candidate.artifact.dataset.provider_id == "tushare"
            )
        complete_unproven_rejections = tuple(
            rejection
            for rejection in routing.metadata_incompatible_rejections
            if rejection.compatibility_assessment.provider_id == "tushare"
            and rejection.completeness_assessment.critical_coverage == 1
            and rejection.completeness_assessment.core_coverage == 1
        )
        assert complete_unproven_rejections
        assert any(
            rejection.family is FinancialRatioFamily.GROWTH
            for rejection in complete_unproven_rejections
        )
        candidate_by_identity = {
            wrapper.candidate.candidate_identity: wrapper.candidate
            for manifest in routing.family_manifests
            for wrapper in manifest.provider_candidates
        }
        tushare_candidates = tuple(
            candidate
            for candidate in candidate_by_identity.values()
            if candidate.artifact.dataset.provider_id == "tushare"
        )
        assert tushare_candidates
        assert all(
            candidate.period_identity.currency == "XXX"
            and candidate.period_identity.consolidation_scope
            is FinancialConsolidationScope.UNKNOWN
            for candidate in tushare_candidates
        )
        tushare_artifact_projection = json.dumps(
            [
                candidate.artifact.model_dump(mode="json")
                for candidate in tushare_candidates
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
        assert '"CNY"' not in tushare_artifact_projection
        assert "consolidated" not in tushare_artifact_projection.casefold()
        assert all(
            assessment.provider_artifact_identity
            == candidate_by_identity[
                assessment.candidate_identity
            ].artifact.artifact_identity
            for assessment in routing.metadata_compatibility_assessments
        )
        assert all(
            candidate.filing_metadata.f_ann_date is None
            for candidate in candidate_by_identity.values()
            if candidate.artifact.dataset.provider_id == "tushare"
        )
        assert all(
            candidate.filing_metadata.f_ann_date is None
            for candidate in candidate_by_identity.values()
            if candidate.artifact.dataset.provider_id == "akshare"
        )

        ledger = FinancialDispatchCheckpointLedger.model_validate(indicator_ledger)
        assert fresh.plan.plan_version == "1.2"
        assert fresh.plan.indicator_field_semantics_version == (
            "financial-indicator-field-semantics-v1"
        )
        assert fresh.plan.indicator_metadata_compatibility_version == (
            "financial-indicator-metadata-compatibility-v1"
        )
        assert fresh.plan.indicator_manifest_contract_version == "financial-manifest-v3"
        assert fresh.plan.indicator_selection_contract_version == (
            "financial-period-selection-v3"
        )
        assert routing.contract_version == "financial-indicator-routing-result-v3"
        assert indicator_result.contract_version == (
            "financial-indicator-selection-dispatch-v2"
        )
        assert all(
            manifest.contract_version == "financial-manifest-v3"
            for manifest in routing.family_manifests
        )
        assert all(
            wrapper.contract_version == "financial-provider-candidate-v2"
            for manifest in routing.family_manifests
            for wrapper in manifest.provider_candidates
        )
        assert all(
            selection.contract_version == "financial-period-selection-v3"
            for manifest in routing.family_manifests
            for selection in manifest.selections
        )
        assert ledger.contract_version == "1.5"
        assert ledger.financial_manifest_version == "financial-manifest-v2"
        assert ledger.financial_indicator_manifest_version == "financial-manifest-v3"
        assert ledger.indicator_field_semantics_version == (
            "financial-indicator-field-semantics-v1"
        )
        assert ledger.indicator_metadata_compatibility_version == (
            "financial-indicator-metadata-compatibility-v1"
        )
        assert len(ledger.qualified_statement_selections) == 1
        assert len(ledger.qualified_indicator_selections) == 1
        indicator_entry = ledger.qualified_indicator_selections[0]
        assert indicator_entry.contract_version == (
            "financial-indicator-selection-checkpoint-v2"
        )
        assert indicator_entry.terminal.routing_result == routing
        assessment_identities = {
            assessment.assessment_identity
            for assessment in routing.metadata_compatibility_assessments
        }
        assert {
            assessment.assessment_identity
            for assessment in ledger.indicator_metadata_assessments
        } == assessment_identities
        persisted_tushare_assessments = tuple(
            assessment
            for assessment in ledger.indicator_metadata_assessments
            if assessment.provider_id == "tushare"
        )
        assert {
            assessment.assessment_identity
            for assessment in persisted_tushare_assessments
        } == {
            assessment.assessment_identity for assessment in tushare_assessments
        }
        assert all(
            dimension.state == "not_applicable"
            and dimension.raw_provider_marker == "XXX"
            for assessment in persisted_tushare_assessments
            for dimension in assessment.dimensions
            if dimension.dimension == "value_currency"
        )
        assert all(
            dimension.state == "unproven"
            and dimension.raw_provider_marker == "UNKNOWN"
            for assessment in persisted_tushare_assessments
            for dimension in assessment.dimensions
            if dimension.dimension == "filing_consolidation_scope"
        )
        assert set(indicator_entry.terminal.metadata_assessment_identities) == (
            assessment_identities
        )
        audit = project_financial_dispatch_ledger(
            ledger,
            run_asset_configuration=fresh.asset,
            capability_routing_plan=fresh.plan,
            run_scope_id=context.run_id,
        )
        assert audit is not None
        assert audit.contract_version == "3.0"
        assert set(audit.metadata_assessment_identities) == assessment_identities
        indicator_audit_request = next(
            request
            for request in audit.manifest_requests
            if request.contract_version == "financial-manifest-request-audit-v3"
        )
        assert all(
            manifest.contract_version == "financial-manifest-audit-v3"
            and manifest.aggregate_completeness.contract_version
            == "financial-aggregate-completeness-v3"
            for manifest in indicator_audit_request.manifests
        )
        indicator_completeness_rows = tuple(
            row
            for row in audit.completeness_rows
            if row.capability is FinancialCapability.RATIO_FAMILY
        )
        assert indicator_completeness_rows
        assert all(
            row.contract_version == "financial-completeness-row-v4"
            for row in indicator_completeness_rows
        )
        tool_envelope = indicator_result.to_tool_message().artifact
        assert tool_envelope["contract_version"] == (
            "financial-tool-message-envelope-v3"
        )
        assert all(
            manifest["contract_version"] == "financial-tool-manifest-v3"
            for manifest in tool_envelope["manifests"]
        )

        assert [endpoint for endpoint, _ in fresh.providers["tushare"].calls] == [
            "balancesheet",
            "balancesheet",
            "fina_indicator",
        ]
        assert [endpoint for endpoint, _ in fresh.providers["akshare"].calls] == [
            "stock_financial_analysis_indicator"
        ]
        baostock_endpoints = [
            endpoint for endpoint, _ in fresh.providers["baostock"].calls
        ]
        assert baostock_endpoints.count("query_lifecycle") == 1
        for endpoint in (
            "query_profit_data",
            "query_operation_data",
            "query_growth_data",
            "query_balance_data",
            "query_cash_flow_data",
            "query_dupont_data",
        ):
            assert baostock_endpoints.count(endpoint) == 8
        assert len(baostock_endpoints) == 49
        assert [endpoint for endpoint, _ in fresh.providers["yfinance"].calls] == [
            "Ticker",
            "get_info",
        ]
        assert len(calls_by_provider["tushare"].physical_attempt_events) == 1
        assert len(calls_by_provider["akshare"].physical_attempt_events) == 1
        assert (
            len(
                calls_by_provider[
                    "baostock_qualified_families"
                ].physical_attempt_events
            )
            == 48
        )
        assert len(calls_by_provider["yfinance"].physical_attempt_events) == 1
        assert len(routing.physical_attempt_events) == 51
        attempts = _attempt_events(indicator_ledger)
        assert len(attempts) == 54
        assert len({event["attempt_event_id"] for event in attempts}) == 54
        assert all(event["attempt_index"] == 1 for event in attempts)
        assert all(event["final_physical_attempt_count"] == 1 for event in attempts)
        assert indicator_ledger["financial_eligibility_context"] == (
            context.safe_projection()
        )
        safe_result = json.dumps(
            {"tool_result": result, "indicator_ledger": indicator_ledger},
            default=str,
        )
        assert "qcf-secret-canary-never-persist" not in safe_result
        assert fresh.config["mainland_capability_routing_mode"] == (
            "qualified_v1_shadow"
        )
        assert DEFAULT_CONFIG["mainland_capability_routing_mode"] != "qualified_v1"
        fresh.deterministic_llm.invoke.assert_not_called()
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())
