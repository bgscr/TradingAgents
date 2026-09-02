from __future__ import annotations

import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

import cli.main as cli_main
import tradingagents.capability_routing as capability_routing_module
import tradingagents.dataflows.financial_dispatch as financial_dispatch_module
import tradingagents.dataflows.financial_eligibility_metadata as metadata_module
import tradingagents.dataflows.financial_temporal_eligibility as temporal_module
import tradingagents.dataflows.financial_temporal_proof_projection as temporal_projection_module
import tradingagents.dataflows.interface as vendor_interface
import tradingagents.qualified_routing_composition as composition_module
import tradingagents.qualified_routing_eligibility_lifecycle as lifecycle_module
from tests.test_financial_capability_routing_selection import (
    _indicator_response_with_compatibility,
    _qualified_ratio_candidate,
    _with_raw_indicator_metadata,
)
from tests.test_market_session_calendar_publication import _acquisition
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.dataflows.baostock_calendar import MainlandSessionCalendarMembership
from tradingagents.dataflows.baostock_capabilities import BaoStockCapabilityAdapter
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityRoutingRequest,
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
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialDispatchCheckpointLedger,
    FinancialIndicatorRequestArguments,
    FinancialToolDispatcher,
    project_financial_dispatch_ledger,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityMetadataBinding,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    FinancialIndicatorMetadataDimension,
    FinancialIndicatorMetadataEvidenceOrigin,
    FinancialIndicatorSemanticAssessmentState,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    TemporalEligibilityRejectionReason,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import FinancialDispatchToolNode
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history import (
    publication_from_baostock_session_calendar_acquisition,
)
from tradingagents.market_history.config import (
    MarketHistoryConfig,
    provider_request_authority_config,
)
from tradingagents.market_history.store import MarketHistoryStore
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


def _single_localized_shadow_failure(entry: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": "2.1",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
        "retained_failure_count": 1,
        "dropped_failure_count": 0,
        "failures": [entry],
    }


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

    def get_info(self) -> dict[str, object]:
        self._provider._record("get_info", {})
        return dict(self._provider.info)

    def history(self, **kwargs: object) -> pd.DataFrame:
        self._provider._record("history", kwargs)
        return self._provider.history_frame.copy(deep=True)


class _YahooProvider(_OwnedFakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.info: dict[str, object] = {}
        self.history_frame = pd.DataFrame()

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
                "publication_date": ["2007-05-15" if self.symbol == "601328.SS" else "1996-04-22"],
                "first_publication_date": ["2007-05-15" if self.symbol == "601328.SS" else "1996-04-22"],
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
    checkpoint_config: dict[str, object]
    deterministic_llm: MagicMock
    primary_calendar_revision_id: str
    sidecar_database_path: Path
    calendar_resolved: threading.Event
    calendar_reader_open_count: int

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
        indicator_families: tuple[FinancialRatioFamily, ...] | None = None,
        include_indicator_arguments: bool = True,
        tool_call_prefix: str = "qcf",
        request_as_of_date: date = _AS_OF,
    ) -> dict[str, object]:
        tool_calls = []
        for index, tool_name in enumerate(tool_names):
            arguments = {
                "ticker": self.symbol,
                "curr_date": request_as_of_date.isoformat(),
            }
            if tool_name != "get_fundamentals":
                arguments["freq"] = "quarterly"
            elif include_indicator_arguments:
                families = indicator_families or tuple(FinancialRatioFamily)
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
                    "id": f"{tool_call_prefix}:{index}:{tool_name}",
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
        return self.compiled_fundamentals.invoke(
            state,
            config=self.checkpoint_config,
        )

    def checkpoint_state(self) -> dict[str, object]:
        snapshot = self.compiled_fundamentals.get_state(self.checkpoint_config)
        return copy.deepcopy(dict(snapshot.values))


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


def _publish_pinned_temporal_calendar(store: object) -> str:
    membership = tuple(
        MainlandSessionCalendarMembership(
            calendar_date=session_date,
            is_trading_day=True,
        )
        for session_date in (
            _AS_OF - timedelta(days=1),
            _AS_OF,
            _AS_OF + timedelta(days=1),
        )
    )
    published = store.publish_session_calendar(
        publication_from_baostock_session_calendar_acquisition(
            _acquisition(
                requested_start_date=membership[0].calendar_date,
                requested_end_date=membership[-1].calendar_date,
                membership=membership,
            )
        )
    )
    return published.calendar_revision_id


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
    plan = prepare_mainland_temporal_capability_routing_plan(preflight.plan)
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
    history_config = MarketHistoryConfig.from_mapping(config)
    with MarketHistoryStore.open(history_config) as primary_store:
        primary_calendar_revision_id = _publish_pinned_temporal_calendar(primary_store)
    calendar_resolved = threading.Event()
    calendar_reader_open_count = 0

    def open_eligibility_calendar_reader(blueprint: object):
        nonlocal calendar_reader_open_count
        calendar_reader_open_count += 1
        if calendar_reader_open_count > 1:
            return composition_module.ProductionQualifiedEligibilityCalendarReader(
                provider_request_authority_config(history_config)
            )
        open_reader = production_factories.open_eligibility_calendar_reader
        assert open_reader is not None
        reader = open_reader(blueprint)
        read_current = reader.read_current_session_calendar

        def tracked_read_current(reference_market: str):
            calendar = read_current(reference_market)
            calendar_resolved.set()
            return calendar

        reader.read_current_session_calendar = tracked_read_current
        return reader

    for provider in providers.values():
        record = provider._record

        def guarded_record(endpoint, kwargs, *, record=record):
            assert calendar_resolved.is_set(), (
                "provider metadata acquisition preceded calendar resolution"
            )
            record(endpoint, kwargs)

        provider._record = guarded_record

    deterministic_factories = replace(
        production_factories,
        open_eligibility_calendar_reader=open_eligibility_calendar_reader,
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
    calendar_preflight = (
        composition_module.preflight_production_qualified_calendar_reader(
            composition,
            authorized_calendar_revision_id=primary_calendar_revision_id,
            requested_session=_AS_OF,
            evaluated_at=_NOW,
        )
    )
    assert calendar_preflight.calendar_revision_id == primary_calendar_revision_id
    assert calendar_reader_open_count == 1

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
    compiled_fundamentals = workflow.compile(checkpointer=MemorySaver())
    checkpoint_config: dict[str, object] = {
        "configurable": {"thread_id": checkpoint_thread_id}
    }
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
        checkpoint_config=checkpoint_config,
        deterministic_llm=deterministic_llm,
        primary_calendar_revision_id=primary_calendar_revision_id,
        sidecar_database_path=history_config.database_path.with_name(
            f"{history_config.database_path.name}.provider-requests.sqlite3"
        ),
        calendar_resolved=calendar_resolved,
        calendar_reader_open_count=calendar_reader_open_count,
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
def test_production_runtime_reads_primary_calendar_with_empty_coordinator_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
    )

    def calendar_row_counts(database_path: Path) -> tuple[int, int]:
        with sqlite3.connect(database_path) as connection:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM market_session_calendars"
            ).fetchone()
            session_count = connection.execute(
                "SELECT COUNT(*) FROM market_sessions"
            ).fetchone()
        assert revision_count is not None
        assert session_count is not None
        return int(revision_count[0]), int(session_count[0])

    try:
        assert all(provider.calls == [] for provider in fresh.providers.values())
        assert calendar_row_counts(Path(fresh.config["market_history_database_path"])) == (
            1,
            3,
        )
        assert calendar_row_counts(fresh.sidecar_database_path) == (0, 0)

        result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))

        assert "financial_dispatch_shadow_failure" not in result
        selection_request = result["financial_dispatch_shadow_ledger"][
            "qualified_statement_selections"
        ][0]["terminal"]["selection_request"]
        assert selection_request["requested_session"] == _AS_OF.isoformat()
        assert "as_of_date" not in selection_request
        context = fresh.owner.context
        assert context is not None
        assert context.contract_version == "financial-eligibility-acquisition-context-v3"
        assert context.company_type_evidence.contract_version == (
            "financial-company-type-evidence-binding-v4"
        )
        acquisition = fresh.owner._acquisition_result
        assert acquisition is not None
        assert acquisition.contract_version == "financial-eligibility-acquisition-result-v3"
        assert acquisition.context is context
        temporal = context.temporal_eligibility_binding
        assert temporal is not None
        window = temporal.acquisition_window
        assert window is not None
        assert window.transaction_binding.canonical_run_id == context.run_id
        assert (
            window.transaction_binding.routing_plan_signature
            == context.routing_plan_signature
        )
        assert all(
            decision.evidence_acquisition_binding is not None
            and decision.evidence_acquisition_binding.transaction_identity
            == window.transaction_binding.transaction_identity
            and decision.evidence_origin == "fresh"
            for decision in (
                temporal.company_type_temporal_decision,
                temporal.listing_temporal_decision,
            )
        )
        metadata_binding = FinancialEligibilityMetadataBinding(
            contract_version="financial-eligibility-metadata-binding-v3",
            instrument_identity=context.instrument_identity,
            requested_session=temporal.requested_session,
            availability_scope=context.availability_scope,
            acquisition_purpose=context.acquisition_purpose,
            financial_eligibility_binding=context.financial_eligibility_binding,
            routing_plan_signature=context.routing_plan_signature,
            temporal_mode=temporal.temporal_mode,
            eligibility_started_at=window.eligibility_started_at,
            calendar_binding=window.calendar_binding.started,
            strict_cutoff=temporal.strict_cutoff,
        )
        company_projection = FinancialCompanyTypeEvidenceProjection(
            contract_version="financial-company-type-evidence-projection-v3",
            metadata_binding=metadata_binding,
            evidence=context.company_type_evidence,
            temporal_decision=temporal.company_type_temporal_decision,
        )
        assert FinancialCompanyTypeEvidenceProjection.model_validate(
            company_projection.model_dump(mode="json")
        ) == company_projection
        assert (
            temporal.calendar_binding.calendar_revision_id
            == fresh.primary_calendar_revision_id
        )
        assert temporal.calendar_binding.binding_identity
        assert fresh.calendar_resolved.is_set()
        assert fresh.calendar_reader_open_count == 1
        assert calendar_row_counts(fresh.sidecar_database_path) == (0, 0)
        with pytest.raises(lifecycle_module.ProductionQualifiedRoutingLifecycleError):
            fresh.owner.acquire_context(as_of_date=_AS_OF - timedelta(days=1))
    finally:
        fresh.close()


@pytest.mark.unit
def test_fresh_builtin_admits_truthful_provider_latency_before_context_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="1400000000000001",
    )
    tushare = fresh.providers["tushare"]
    original_record = tushare._record

    def record_with_realistic_latency(
        endpoint: str,
        kwargs: dict[str, object],
    ) -> None:
        original_record(endpoint, kwargs)
        if endpoint == "balancesheet":
            _DeterministicDateTime.advance(1)

    tushare._record = record_with_realistic_latency
    try:
        result = fresh.invoke(
            fresh.state(
                tool_names=("get_fundamentals",),
                tool_call_prefix="ticket07-latency",
            )
        )

        assert "financial_dispatch_shadow_failure" not in result
        context = fresh.owner.context
        assert context is not None
        assert context.contract_version == "financial-eligibility-acquisition-context-v3"
        temporal = context.temporal_eligibility_binding
        assert temporal is not None
        assert temporal.contract_version == "financial-eligibility-temporal-binding-v2"
        window = temporal.acquisition_window
        assert window.contract_version == "financial-eligibility-acquisition-window-v1"
        assert window.eligibility_started_at < window.eligibility_evaluated_at
        assert window.calendar_revision_id == fresh.primary_calendar_revision_id
        assert window.started_latest_completed_session == _AS_OF
        assert window.evaluated_latest_completed_session == _AS_OF
        for decision in (
            temporal.company_type_temporal_decision,
            temporal.listing_temporal_decision,
        ):
            assert (
                window.eligibility_started_at
                <= decision.observed_at
                <= decision.retrieved_at
                <= window.eligibility_evaluated_at
            )
    finally:
        fresh.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "tool_name",
        "selection_key",
        "provider_endpoint",
        "expected_provider_end_dates",
        "checkpoint_thread_id",
    ),
    (
        (
            "get_fundamentals",
            "qualified_indicator_selections",
            "fina_indicator",
            ("20260630",),
            "0700000000000001",
        ),
        (
            "get_balance_sheet",
            "qualified_statement_selections",
            "balancesheet",
            ("20260902", "20260630"),
            "0700000000000002",
        ),
    ),
)
def test_fresh_financial_dispatch_separates_run_session_from_historical_tool_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    selection_key: str,
    provider_endpoint: str,
    expected_provider_end_dates: tuple[str, ...],
    checkpoint_thread_id: str,
) -> None:
    run_session = date(2026, 9, 2)
    historical_request_date = date(2026, 6, 30)
    monkeypatch.setitem(globals(), "_AS_OF", run_session)
    monkeypatch.setitem(
        globals(),
        "_NOW",
        datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id=checkpoint_thread_id,
    )
    try:
        state = fresh.state(
            tool_names=(tool_name,),
            request_as_of_date=historical_request_date,
            tool_call_prefix="ticket07-v12-replay",
        )
        legacy_config = dict(fresh.config)
        legacy_result = FinancialDispatchToolNode(
            config=legacy_config,
            vendor_methods=_legacy_vendor_methods(),
        )(copy.deepcopy(state))

        result = fresh.invoke(state)

        context = fresh.owner.context
        assert context is not None
        assert context.contract_version == "financial-eligibility-acquisition-context-v3"
        assert context.requested_session == run_session
        shadow_failure = result.get("financial_dispatch_shadow_failure")
        assert shadow_failure is None, json.dumps(shadow_failure, sort_keys=True)
        shadow_ledger = result["financial_dispatch_shadow_ledger"]
        terminal = shadow_ledger[selection_key][0]["terminal"]
        selection_request = terminal["selection_request"]
        assert selection_request["requested_session"] == run_session.isoformat()
        assert selection_request["as_of_date"] == historical_request_date.isoformat()
        assert selection_request["financial_eligibility_context_binding"][
            "requested_session"
        ] == run_session.isoformat()
        assert json.loads(result["messages"][-1].content)["reason"] == json.loads(
            legacy_result["messages"][0].content
        )["reason"]
        assert result["financial_dispatch_ledger"]["entries"][0][
            "canonical_request_key"
        ]["as_of_date"] == historical_request_date.isoformat()
        assert legacy_result["financial_dispatch_ledger"]["entries"][0][
            "canonical_request_key"
        ]["as_of_date"] == historical_request_date.isoformat()
        provider_calls = [
            kwargs
            for endpoint, kwargs in fresh.providers["tushare"].calls
            if endpoint == provider_endpoint
        ]
        assert provider_calls
        assert tuple(call["end_date"] for call in provider_calls) == (
            expected_provider_end_dates
        )
        target_construction = terminal["routing_result"][
            "publication_target_construction"
        ]
        assert target_construction["run_as_of_date"] == (
            historical_request_date.isoformat()
        )
        assert all(
            date.fromisoformat(period) <= historical_request_date
            for key in ("annual_target_period_ends", "reporting_target_period_ends")
            for period in target_construction[key]
        )
        assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    finally:
        fresh.close()


@pytest.mark.unit
def test_future_financial_tool_date_fails_before_qualified_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0700000000000003",
    )
    future_request_date = _AS_OF + timedelta(days=1)
    try:
        result = fresh.invoke(
            fresh.state(
                tool_names=("get_fundamentals",),
                request_as_of_date=future_request_date,
                tool_call_prefix="ticket07-future-date",
            )
        )

        context = fresh.owner.context
        assert context is not None
        assert context.requested_session == _AS_OF
        shadow_failure = result["financial_dispatch_shadow_failure"]
        assert shadow_failure["failure_count"] == 2
        assert shadow_failure["failures"][0] == {
            "stage": "request_factory",
            "failure_kind": "exception",
            "exception_family": "builtins",
            "exception_class": "ValueError",
            "selection_kind": "indicator",
            "context_constructed": True,
            "dispatcher_constructed": True,
        }
        assert shadow_failure["failures"][1] == {
            "stage": "checkpoint_projection",
            "failure_kind": "exception",
            "exception_family": "financial_dispatch",
            "exception_class": "FinancialDispatchCheckpointError",
            "typed_reason": "financial_dispatch_checkpoint_qualified_selection_invalid",
            "selection_kind": "indicator",
            "context_constructed": True,
            "dispatcher_constructed": True,
        }
        assert not any(
            endpoint == "fina_indicator"
            for endpoint, _kwargs in fresh.providers["tushare"].calls
        )
        assert result["financial_dispatch_ledger"]["entries"][0][
            "canonical_request_key"
        ]["as_of_date"] == future_request_date.isoformat()
    finally:
        fresh.close()


@pytest.mark.unit
@pytest.mark.parametrize("graph_date_state", ("missing", "malformed"))
def test_invalid_graph_trade_date_never_falls_back_to_financial_tool_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_date_state: str,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id=(
            "0700000000000004"
            if graph_date_state == "missing"
            else "0700000000000005"
        ),
    )
    historical_request_date = date(2026, 6, 30)
    try:
        state = fresh.state(
            tool_names=("get_fundamentals",),
            request_as_of_date=historical_request_date,
            tool_call_prefix=f"ticket07-{graph_date_state}-graph-date",
        )
        if graph_date_state == "missing":
            state.pop("trade_date")
        else:
            state["trade_date"] = "not-an-iso-date"

        result = fresh.invoke(state)

        assert fresh.owner.context is None
        assert result["financial_dispatch_shadow_failure"] == (
            _single_localized_shadow_failure(
                {
                    "stage": "context_exception",
                    "failure_kind": "exception",
                    "exception_family": "builtins",
                    "exception_class": "ValueError",
                    "selection_kind": "indicator",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            )
        )
        assert all(provider.calls == [] for provider in fresh.providers.values())
        assert result["financial_dispatch_ledger"]["entries"][0][
            "canonical_request_key"
        ]["as_of_date"] == historical_request_date.isoformat()
    finally:
        fresh.close()


@pytest.mark.unit
def test_fresh_builtin_rejects_when_acquisition_crosses_next_session_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="1400000000000002",
    )
    tushare = fresh.providers["tushare"]
    original_record = tushare._record

    def cross_frontier(endpoint: str, kwargs: dict[str, object]) -> None:
        original_record(endpoint, kwargs)
        if endpoint == "balancesheet":
            _DeterministicDateTime.advance(19 * 60 * 60)

    tushare._record = cross_frontier
    try:
        result = fresh.tool_node(
            fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix="ticket07-frontier-crossing",
            )
        )

        assert result["financial_dispatch_shadow_failure"] == (
            _single_localized_shadow_failure(
                {
                    "stage": "context_result",
                    "failure_kind": "typed_result",
                    "typed_acquisition_reason": "temporal_eligibility_rejected",
                    "temporal_rejection_reasons": [
                        "requested_session_not_latest_completed_frontier"
                    ],
                    "selection_kind": "statement",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            )
        )
        acquisition = fresh.owner._acquisition_result
        assert acquisition is not None
        assert acquisition.contract_version == "financial-eligibility-acquisition-result-v3"
        assert acquisition.context is None
        assert acquisition.failure is not None
        assert acquisition.failure.contract_version == (
            "financial-eligibility-acquisition-result-v3"
        )
        assert acquisition.failure.reason is (
            FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED
        )
        assert acquisition.failure.rejected_temporal_decision is not None
        assert (
            TemporalEligibilityRejectionReason.REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER
            in acquisition.failure.rejected_temporal_decision.reasons
        )
    finally:
        fresh.close()


@pytest.mark.unit
def test_fresh_builtin_rejects_mismatched_window_transaction_before_provider_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="1400000000000003",
    )
    transaction_type = (
        lifecycle_module.FinancialEligibilityTemporalEvaluationTransaction
    )

    def mismatched_transaction(**kwargs: object):
        transaction = transaction_type(**kwargs)
        transaction._calendar_binding = transaction.calendar_binding.model_copy(
            update={
                "calendar_revision_id": (
                    "market-session-calendar=sha256:" + "0" * 64
                )
            }
        )
        return transaction

    monkeypatch.setattr(
        lifecycle_module,
        "FinancialEligibilityTemporalEvaluationTransaction",
        mismatched_transaction,
    )
    try:
        result = fresh.tool_node(
            fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix="ticket07-transaction-mismatch",
            )
        )

        assert result["financial_dispatch_shadow_failure"] == (
            _single_localized_shadow_failure(
                {
                    "stage": "context_result",
                    "failure_kind": "typed_result",
                    "typed_acquisition_reason": "context_binding_mismatch",
                    "selection_kind": "statement",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            )
        )
        acquisition = fresh.owner._acquisition_result
        assert acquisition is not None
        assert acquisition.contract_version == "financial-eligibility-acquisition-result-v3"
        assert acquisition.context is None
        assert acquisition.failure is not None
        assert acquisition.failure.contract_version == (
            "financial-eligibility-acquisition-result-v3"
        )
        assert acquisition.failure.reason is (
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
        )
        assert fresh.owner.context is None
        assert fresh.runtime.financial_routing is None
        assert all(provider.calls == [] for provider in fresh.providers.values())
    finally:
        fresh.close()


@pytest.mark.unit
def test_fresh_builtin_rejects_proof_bound_to_another_active_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="1400000000000005",
    )
    transaction_type = (
        lifecycle_module.FinancialEligibilityTemporalEvaluationTransaction
    )
    original_stage = transaction_type.stage
    mismatched_once = False

    def mismatched_stage(transaction, evidence, *, evidence_origin):
        nonlocal mismatched_once
        staged = original_stage(
            transaction,
            evidence,
            evidence_origin=evidence_origin,
        )
        if mismatched_once:
            return staged
        mismatched_once = True
        return temporal_module.bind_financial_temporal_evidence_to_transaction(
            transaction_identity=(
                "financial-eligibility-temporal-transaction:v1:" + "f" * 64
            ),
            evidence_origin=staged.evidence_origin,
            evidence=evidence,
        )

    monkeypatch.setattr(transaction_type, "stage", mismatched_stage)
    try:
        result = fresh.tool_node(
            fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix="ticket07-proof-transaction-mismatch",
            )
        )

        assert result["financial_dispatch_shadow_failure"] == (
            _single_localized_shadow_failure(
                {
                    "stage": "context_result",
                    "failure_kind": "typed_result",
                    "typed_acquisition_reason": "context_binding_mismatch",
                    "selection_kind": "statement",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            )
        )
        acquisition = fresh.owner._acquisition_result
        assert acquisition is not None
        assert acquisition.contract_version == "financial-eligibility-acquisition-result-v3"
        assert acquisition.context is None
        assert acquisition.failure is not None
        assert acquisition.failure.reason is (
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
        )
        assert fresh.owner.context is None
        assert fresh.runtime.financial_routing is None
    finally:
        fresh.close()


@pytest.mark.unit
def test_fresh_builtin_rejects_terminal_clock_regression_without_partial_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="1400000000000004",
    )
    clock_values = iter((_NOW, _NOW - timedelta(seconds=1)))
    fresh.owner._eligibility_clock = lambda: next(clock_values)
    try:
        result = fresh.tool_node(
            fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix="ticket07-clock-regression",
            )
        )

        assert result["financial_dispatch_shadow_failure"] == (
            _single_localized_shadow_failure(
                {
                    "stage": "context_result",
                    "failure_kind": "typed_result",
                    "typed_acquisition_reason": "context_binding_mismatch",
                    "selection_kind": "statement",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            )
        )
        acquisition = fresh.owner._acquisition_result
        assert acquisition is not None
        assert acquisition.contract_version == "financial-eligibility-acquisition-result-v3"
        assert acquisition.context is None
        assert acquisition.failure is not None
        assert acquisition.failure.contract_version == (
            "financial-eligibility-acquisition-result-v3"
        )
        assert acquisition.failure.reason is (
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
        )
        assert acquisition.failure.rejected_temporal_decision is None
        assert fresh.owner.context is None
        assert fresh.runtime.financial_routing is None
        with pytest.raises(StopIteration):
            next(clock_values)
    finally:
        fresh.close()


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
            include_indicator_arguments=False,
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
        assert fresh.plan.plan_version == "1.4"
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
        assert routing.contract_version == "financial-indicator-routing-result-v5"
        assert indicator_result.contract_version == (
            "financial-indicator-selection-dispatch-v4"
        )
        assert all(
            manifest.contract_version == "financial-manifest-v5"
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
        assert ledger.contract_version == "1.7"
        assert ledger.financial_manifest_version == "financial-manifest-v5"
        assert ledger.financial_indicator_manifest_version == "financial-manifest-v5"
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
            "financial-indicator-selection-checkpoint-v4"
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
        assert audit.contract_version == "5.0"
        assert set(audit.metadata_assessment_identities) == assessment_identities
        indicator_audit_request = next(
            request
            for request in audit.manifest_requests
            if request.contract_version == "financial-manifest-request-audit-v5"
        )
        assert all(
            manifest.contract_version == "financial-manifest-audit-v5"
            and manifest.aggregate_completeness.contract_version
            == "financial-aggregate-completeness-v5"
            for manifest in indicator_audit_request.manifests
        )
        indicator_completeness_rows = tuple(
            row
            for row in audit.completeness_rows
            if row.capability is FinancialCapability.RATIO_FAMILY
        )
        assert indicator_completeness_rows
        assert all(
            row.contract_version == "financial-completeness-row-v6"
            for row in indicator_completeness_rows
        )
        tool_envelope = indicator_result.to_tool_message().artifact
        assert tool_envelope["contract_version"] == (
            "financial-tool-message-envelope-v5"
        )
        assert all(
            manifest["contract_version"] == "financial-tool-manifest-v5"
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


def _assert_fresh_builtin_origin(fresh: _FreshRun) -> None:
    assert isinstance(fresh.composition, RegisteredProductionQualifiedRoutingComposition)
    assert fresh.composition.factory_identity == (
        cli_main.BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY.identity
    )
    assert fresh.owner is not None
    assert fresh.owner.context is None
    assert fresh.runtime.financial_routing is None
    assert all(provider.calls == [] for provider in fresh.providers.values())


def _capability_request(
    fresh: _FreshRun,
    capability: MainlandCapability,
    *,
    range_start: date = date(2026, 7, 23),
    as_of_date: date = date(2026, 7, 24),
) -> FinancialCapabilityRoutingRequest:
    return FinancialCapabilityRoutingRequest(
        instrument_identity=fresh.asset.instrument_identity,
        capability=capability,
        range_start=range_start,
        as_of_date=as_of_date,
    )


@pytest.mark.unit
def test_qcf5_fresh_builtin_preserves_partial_tushare_work_and_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="600895.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000005",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    assert isinstance(tushare, _TushareProvider)
    original_balance = tushare.balancesheet
    original_income = tushare.income
    balance_calls = 0

    def partial_balance(**kwargs: object) -> pd.DataFrame:
        nonlocal balance_calls
        balance_calls += 1
        frame = original_balance(**kwargs)
        return frame if balance_calls == 1 else frame.iloc[-2:].reset_index(drop=True)

    def partial_income(**kwargs: object) -> pd.DataFrame:
        return original_income(**kwargs).iloc[-2:].reset_index(drop=True)

    def unavailable_cashflow(**kwargs: object) -> pd.DataFrame:
        tushare._record("cashflow", kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(tushare, "balancesheet", partial_balance)
    monkeypatch.setattr(tushare, "income", partial_income)
    monkeypatch.setattr(tushare, "cashflow", unavailable_cashflow)
    dispatches: list[tuple[object, object]] = []
    original_dispatch = FinancialToolDispatcher.dispatch_statement_selection

    def capture(dispatcher, request, selection_request):
        result = original_dispatch(dispatcher, request, selection_request)
        dispatches.append((selection_request, result))
        return result

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_statement_selection",
        capture,
    )
    try:
        result = fresh.invoke(
            fresh.state(
                tool_names=(
                    "get_balance_sheet",
                    "get_income_statement",
                    "get_cashflow",
                )
            )
        )
        assert "financial_dispatch_shadow_failure" not in result
        assert fresh.owner.context is not None
        assert len(dispatches) == 3
        partial_results = tuple(
            dispatched.routing_result
            for _request, dispatched in dispatches
            if dispatched.routing_result.selected_candidates
        )
        assert len(partial_results) == 2
        for routing in partial_results:
            assert routing.missing_annual_period_ends or (
                routing.missing_reporting_period_ends
            )
            assert tuple(call.provider_id for call in routing.provider_calls) == (
                "tushare",
                "akshare_sina",
                "yfinance",
            )
            first, second, third = routing.provider_calls
            first_targets = {
                *first.missing_annual_period_ends,
                *first.missing_reporting_period_ends,
            }
            second_targets = {
                *second.missing_annual_period_ends,
                *second.missing_reporting_period_ends,
            }
            third_targets = {
                *third.missing_annual_period_ends,
                *third.missing_reporting_period_ends,
            }
            assert second_targets < first_targets
            assert third_targets == second_targets
            assert all(
                candidate.artifact.dataset.provider_id == "tushare"
                for candidate in routing.selected_candidates
            )
        cashflow_routing = next(
            dispatched.routing_result
            for request, dispatched in dispatches
            if request.statement_type.value == "cash_flow"
        )
        assert cashflow_routing.selected_candidates == ()
        assert balance_calls == 2
        assert [endpoint for endpoint, _ in tushare.calls].count("income") == 1
        assert [endpoint for endpoint, _ in tushare.calls].count("cashflow") == 1
        attempts = _attempt_events(result["financial_dispatch_shadow_ledger"])
        assert len({event["attempt_event_id"] for event in attempts}) == len(attempts)
        assert all(event["final_physical_attempt_count"] == 1 for event in attempts)
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf6_fresh_builtin_akshare_salvages_only_unresolved_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="600895.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000006",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    akshare = fresh.providers["akshare"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(akshare, _AkshareProvider)
    original_balance = tushare.balancesheet
    balance_calls = 0

    def one_gap_balance(**kwargs: object) -> pd.DataFrame:
        nonlocal balance_calls
        balance_calls += 1
        frame = original_balance(**kwargs)
        if balance_calls == 1:
            return frame
        return frame.loc[frame["end_date"] != "20251231"].reset_index(drop=True)

    def salvaged_balance(**kwargs: object) -> pd.DataFrame:
        akshare._record("stock_financial_report_sina", kwargs)
        return pd.DataFrame(
            [
                {
                    "报告日": "2025-12-31",
                    "公告日期": "2026-04-20",
                    "币种": "CNY",
                    "类型": "合并期末",
                    "更新日期": "2026-04-22T08:30:00+08:00",
                    "资产总计": 1_000,
                    "负债合计": 400,
                    "所有者权益合计": 600,
                    "货币资金": 100,
                    "应收账款": 80,
                    "存货": 60,
                    "应付账款": 70,
                    "短期借款": 50,
                    "长期借款": 40,
                    "固定资产": 300,
                }
            ]
        )

    monkeypatch.setattr(tushare, "balancesheet", one_gap_balance)
    monkeypatch.setattr(akshare, "stock_financial_report_sina", salvaged_balance)
    dispatches: list[object] = []
    original_dispatch = FinancialToolDispatcher.dispatch_statement_selection

    def capture(dispatcher, request, selection_request):
        result = original_dispatch(dispatcher, request, selection_request)
        dispatches.append(result)
        return result

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_statement_selection",
        capture,
    )
    try:
        result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in result
        assert fresh.owner.context is not None
        assert len(dispatches) == 1
        routing = dispatches[0].routing_result
        assert routing.missing_annual_period_ends == ()
        assert routing.missing_reporting_period_ends == ()
        assert tuple(call.provider_id for call in routing.provider_calls) == (
            "tushare",
            "akshare_sina",
        )
        assert {
            candidate.artifact.dataset.provider_id
            for candidate in routing.selected_candidates
        } == {"tushare", "akshare_sina"}
        akshare_call = routing.provider_calls[1]
        assert set(akshare_call.missing_annual_period_ends) | set(
            akshare_call.missing_reporting_period_ends
        ) == {date(2025, 12, 31)}
        assert [endpoint for endpoint, _ in akshare.calls] == [
            "stock_financial_report_sina"
        ]
        assert fresh.providers["yfinance"].calls == []
        assert len(routing.physical_attempt_ids) == 2
        assert balance_calls == 2
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf7_fresh_builtin_yahoo_profile_and_derived_factor_stay_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000007",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    akshare = fresh.providers["akshare"]
    yahoo = fresh.providers["yfinance"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(akshare, _AkshareProvider)
    assert isinstance(yahoo, _YahooProvider)
    yahoo.info = {
        "industry": "Banks - Diversified",
        "sector": "Financial Services",
        "raw_token": "must-not-survive",
    }
    yahoo.history_frame = pd.DataFrame(
        {"Close": [10.0, 20.0], "Adj Close": [5.0, 15.0]},
        index=pd.to_datetime(["2026-07-29", "2026-07-30"]),
    )

    def empty_tushare_factor(**kwargs: object) -> pd.DataFrame:
        tushare._record("adj_factor", kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(tushare, "adj_factor", empty_tushare_factor)
    indicator_dispatches: list[object] = []
    original_indicator_dispatch = FinancialToolDispatcher.dispatch_indicator_selection

    def capture_indicator(dispatcher, request, selection_request):
        result = original_indicator_dispatch(dispatcher, request, selection_request)
        indicator_dispatches.append(result)
        return result

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_indicator_selection",
        capture_indicator,
    )
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_fundamentals",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        context = fresh.owner.context
        assert context is not None
        assert len(indicator_dispatches) == 1
        indicator_routing = indicator_dispatches[0].routing_result
        assert indicator_routing.provider_calls[-1].provider_id == "yfinance"
        assert not any(
            candidate.artifact.dataset.provider_id == "yfinance"
            for manifest in indicator_routing.family_manifests
            for wrapper in manifest.provider_candidates
            for candidate in (wrapper.candidate,)
        )
        assert not any(
            candidate.artifact.dataset.provider_id == "yfinance"
            for candidate in indicator_routing.selected_candidates
        )

        factor_request = _capability_request(
            fresh,
            MainlandCapability.ADJUSTMENT_FACTORS,
            range_start=date(2026, 7, 29),
            as_of_date=date(2026, 7, 30),
        )
        factors = fresh.owner.financial_routing.router.route_capability(factor_request)
        assert factors.selected_provider == "yfinance_derived"
        assert factors.derived is True
        assert factors.degraded is True
        assert factors.strict_bundle_established is False
        assert factors.provider_history_bundle_identity is None
        assert factors.authoritative_provider == "baostock"
        assert [endpoint for endpoint, _ in yahoo.calls].count("get_info") == 1
        assert [endpoint for endpoint, _ in yahoo.calls].count("history") == 1
        assert "must-not-survive" not in json.dumps(
            {
                "indicator_routing": indicator_routing.model_dump(mode="json"),
                "factors": factors.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("suspended", "expected_tradeability"),
    ((False, "tradeable"), (True, "suspended")),
)
def test_qcf8_fresh_builtin_baostock_owns_strict_bundle_and_tradeability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suspended: bool,
    expected_tradeability: str,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id=(
            "0600000000000081" if suspended else "0600000000000080"
        ),
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    baostock = fresh.providers["baostock"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(baostock, _BaoStockProvider)

    def empty_tushare_factor(**kwargs: object) -> pd.DataFrame:
        tushare._record("adj_factor", kwargs)
        return pd.DataFrame()

    def raw_daily(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_raw_daily", kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-07-23", "2026-07-24"],
                "code": ["sh.601328", "sh.601328"],
                "open": ["6.0", "6.1"],
                "high": ["6.2", "6.3"],
                "low": ["5.9", "6.0"],
                "close": ["6.1", "6.2"],
                "preclose": ["6.0", "6.1"],
                "volume": ["100", "120"],
                "amount": ["600", "744"],
            }
        )

    def factors(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_forward_adjustment_factors", kwargs)
        return pd.DataFrame(
            {
                "code": ["sh.601328"],
                "dividOperateDate": ["2026-07-01"],
                "foreAdjustFactor": ["0.5000000000"],
            }
        )

    def status(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_session_status", kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-07-23", "2026-07-24"],
                "code": ["sh.601328", "sh.601328"],
                "tradestatus": ["1", "0" if suspended else "1"],
                "isST": ["0", "0"],
                "preclose": ["6.0", "6.1"],
            }
        )

    monkeypatch.setattr(tushare, "adj_factor", empty_tushare_factor)
    monkeypatch.setattr(baostock, "query_raw_daily", raw_daily)
    monkeypatch.setattr(baostock, "query_forward_adjustment_factors", factors)
    monkeypatch.setattr(baostock, "query_session_status", status)
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        assert fresh.owner.context is not None
        indicator_result = fresh.invoke(
            fresh.state(
                tool_names=("get_fundamentals",),
                tool_call_prefix="qcf8-ratios",
            )
        )
        assert "financial_dispatch_shadow_failure" not in indicator_result
        factor_result = fresh.owner.financial_routing.router.route_capability(
            _capability_request(fresh, MainlandCapability.ADJUSTMENT_FACTORS)
        )
        status_result = fresh.owner.financial_routing.router.route_capability(
            _capability_request(fresh, MainlandCapability.SUSPENSION_STATUS)
        )
        assert factor_result.authoritative_provider == "baostock"
        assert factor_result.strict_bundle_provider == "baostock"
        if suspended:
            assert factor_result.selected_provider is None
            assert factor_result.strict_bundle_established is False
        else:
            assert factor_result.selected_provider == "baostock"
            assert factor_result.strict_bundle_established is True
            assert len(factor_result.strict_bundle_component_artifact_identities) == 3
        assert status_result.selected_provider == "baostock"
        assert status_result.authoritative_provider == "baostock"
        assert status_result.current_tradeability == expected_tradeability
        endpoints = [endpoint for endpoint, _ in baostock.calls]
        assert endpoints.count("query_lifecycle") == 1
        assert endpoints.count("query_raw_daily") == 1
        assert endpoints.count("query_forward_adjustment_factors") == 1
        assert endpoints.count("query_session_status") == 1
        for endpoint in (
            "query_profit_data",
            "query_operation_data",
            "query_growth_data",
            "query_balance_data",
            "query_cash_flow_data",
            "query_dupont_data",
        ):
            assert endpoints.count(endpoint) == 8
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf9_fresh_builtin_factor_and_name_routes_remain_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000009",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    akshare = fresh.providers["akshare"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(akshare, _AkshareProvider)

    def empty_tushare_factor(**kwargs: object) -> pd.DataFrame:
        tushare._record("adj_factor", kwargs)
        return pd.DataFrame()

    def empty_tushare_names(**kwargs: object) -> pd.DataFrame:
        tushare._record("namechange", kwargs)
        return pd.DataFrame()

    def akshare_factors(**kwargs: object) -> pd.DataFrame:
        akshare._record("stock_zh_a_daily", kwargs)
        return pd.DataFrame(
            [
                {"date": "2026-07-23", "qfq_factor": "0.5"},
                {"date": "2026-07-24", "qfq_factor": "0.75"},
            ]
        )

    def akshare_names(**kwargs: object) -> pd.DataFrame:
        akshare._record("stock_info_change_name", kwargs)
        return pd.DataFrame(
            [
                {
                    "变更日期": "2020-01-02",
                    "证券简称": "*ST交通",
                    "变更原因": "描述性简称变更",
                }
            ]
        )

    monkeypatch.setattr(tushare, "adj_factor", empty_tushare_factor)
    monkeypatch.setattr(tushare, "namechange", empty_tushare_names)
    monkeypatch.setattr(akshare, "stock_zh_a_daily", akshare_factors)
    monkeypatch.setattr(akshare, "stock_info_change_name", akshare_names)
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        assert fresh.owner.context is not None
        router = fresh.owner.financial_routing.router
        factor_result = router.route_capability(
            _capability_request(fresh, MainlandCapability.ADJUSTMENT_FACTORS)
        )
        name_result = router.route_capability(
            _capability_request(
                fresh,
                MainlandCapability.NAME_EVENTS,
                range_start=date(2019, 1, 1),
                as_of_date=date(2026, 7, 24),
            )
        )
        assert factor_result.selected_provider == "akshare"
        assert name_result.selected_provider == "akshare"
        assert factor_result.route != name_result.route
        assert set(factor_result.physical_attempt_ids).isdisjoint(
            name_result.physical_attempt_ids
        )
        assert set(factor_result.retained_artifact_identities).isdisjoint(
            name_result.retained_artifact_identities
        )
        assert factor_result.strict_bundle_established is False
        assert name_result.current_tradeability == "unknown"
        assert name_result.listing_metadata is None
        assert name_result.status_or_lifecycle_fact_created is False
        assert name_result.source_facts_created is False
        shared_cache = fresh.owner.financial_routing.provider_subrequest_cache
        assert shared_cache is not None
        cache_entries = shared_cache.checkpoint()["entries"]
        akshare_entries = tuple(
            entry
            for entry in cache_entries
            if entry["key"]["provider_id"] == "akshare_sina"
        )
        assert len(akshare_entries) == 2
        assert len(
            {
                entry["key"]["subrequest_key"] for entry in akshare_entries
            }
        ) == 2
        assert len(
            {
                entry["key"]["normalizer_version"] for entry in akshare_entries
            }
        ) == 2
        akshare_endpoints = [endpoint for endpoint, _ in akshare.calls]
        assert akshare_endpoints == [
            "stock_zh_a_daily",
            "stock_info_change_name",
        ]
        assert fresh.providers["yfinance"].calls == []
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf9_fresh_builtin_tushare_factor_and_name_successes_stay_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000091",
    )
    _assert_fresh_builtin_origin(fresh)
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        router = fresh.owner.financial_routing.router
        factor_result = router.route_capability(
            _capability_request(
                fresh,
                MainlandCapability.ADJUSTMENT_FACTORS,
                range_start=date(2026, 8, 7),
                as_of_date=date(2026, 8, 7),
            )
        )
        name_result = router.route_capability(
            _capability_request(
                fresh,
                MainlandCapability.NAME_EVENTS,
                range_start=date(2020, 1, 1),
                as_of_date=date(2026, 8, 7),
            )
        )
        assert factor_result.selected_provider == "tushare"
        assert name_result.selected_provider == "tushare"
        assert factor_result.route != name_result.route
        assert set(factor_result.physical_attempt_ids).isdisjoint(
            name_result.physical_attempt_ids
        )
        assert set(factor_result.retained_artifact_identities).isdisjoint(
            name_result.retained_artifact_identities
        )
        assert factor_result.strict_bundle_established is False
        assert name_result.current_tradeability == "unknown"
        assert name_result.status_or_lifecycle_fact_created is False
        assert [
            endpoint
            for endpoint, _ in fresh.providers["tushare"].calls
            if endpoint in {"adj_factor", "namechange"}
        ] == ["adj_factor", "namechange"]
        assert fresh.providers["akshare"].calls == []
        assert fresh.providers["yfinance"].calls == []
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf9_fresh_builtin_baostock_strict_factor_does_not_blend_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000092",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    baostock = fresh.providers["baostock"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(baostock, _BaoStockProvider)

    def empty_tushare_factor(**kwargs: object) -> pd.DataFrame:
        tushare._record("adj_factor", kwargs)
        return pd.DataFrame()

    def raw_daily(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_raw_daily", kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-07-23", "2026-07-24"],
                "code": ["sh.601328", "sh.601328"],
                "open": ["6.0", "6.1"],
                "high": ["6.2", "6.3"],
                "low": ["5.9", "6.0"],
                "close": ["6.1", "6.2"],
                "preclose": ["6.0", "6.1"],
                "volume": ["100", "120"],
                "amount": ["600", "744"],
            }
        )

    def factors(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_forward_adjustment_factors", kwargs)
        return pd.DataFrame(
            {
                "code": ["sh.601328"],
                "dividOperateDate": ["2026-07-01"],
                "foreAdjustFactor": ["0.5000000000"],
            }
        )

    def status(**kwargs: object) -> pd.DataFrame:
        baostock._record("query_session_status", kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-07-23", "2026-07-24"],
                "code": ["sh.601328", "sh.601328"],
                "tradestatus": ["1", "1"],
                "isST": ["0", "0"],
                "preclose": ["6.0", "6.1"],
            }
        )

    monkeypatch.setattr(tushare, "adj_factor", empty_tushare_factor)
    monkeypatch.setattr(baostock, "query_raw_daily", raw_daily)
    monkeypatch.setattr(baostock, "query_forward_adjustment_factors", factors)
    monkeypatch.setattr(baostock, "query_session_status", status)
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        router = fresh.owner.financial_routing.router
        factor_result = router.route_capability(
            _capability_request(fresh, MainlandCapability.ADJUSTMENT_FACTORS)
        )
        name_result = router.route_capability(
            _capability_request(
                fresh,
                MainlandCapability.NAME_EVENTS,
                range_start=date(2020, 1, 1),
                as_of_date=date(2026, 7, 24),
            )
        )
        assert factor_result.selected_provider == "baostock"
        assert factor_result.strict_bundle_established is True
        assert len(factor_result.strict_bundle_component_artifact_identities) == 3
        assert name_result.selected_provider == "tushare"
        assert name_result.current_tradeability == "unknown"
        assert name_result.listing_metadata is None
        assert set(factor_result.retained_artifact_identities).isdisjoint(
            name_result.retained_artifact_identities
        )
        assert factor_result.route != name_result.route
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf9_fresh_builtin_baostock_and_akshare_gaps_reach_yahoo_derived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000093",
    )
    _assert_fresh_builtin_origin(fresh)
    tushare = fresh.providers["tushare"]
    yahoo = fresh.providers["yfinance"]
    assert isinstance(tushare, _TushareProvider)
    assert isinstance(yahoo, _YahooProvider)

    def empty_tushare_factor(**kwargs: object) -> pd.DataFrame:
        tushare._record("adj_factor", kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(tushare, "adj_factor", empty_tushare_factor)
    yahoo.history_frame = pd.DataFrame(
        {"Close": [10.0, 20.0], "Adj Close": [5.0, 15.0]},
        index=pd.to_datetime(["2026-07-29", "2026-07-30"]),
    )
    try:
        graph_result = fresh.invoke(fresh.state(tool_names=("get_balance_sheet",)))
        assert "financial_dispatch_shadow_failure" not in graph_result
        factors = fresh.owner.financial_routing.router.route_capability(
            _capability_request(
                fresh,
                MainlandCapability.ADJUSTMENT_FACTORS,
                range_start=date(2026, 7, 29),
                as_of_date=date(2026, 7, 30),
            )
        )
        assert factors.selected_provider == "yfinance_derived"
        assert factors.derived is True
        assert factors.degraded is True
        assert factors.authoritative_provider == "baostock"
        assert factors.strict_bundle_established is False
        assert tuple(call.provider_id for call in factors.provider_calls) == (
            factors.route
        )
        assert factors.route[-1] == "yfinance_derived"
        assert {"baostock", "akshare"}.issubset(factors.route)
        assert [endpoint for endpoint, _ in yahoo.calls] == ["Ticker", "history"]
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf10_fresh_builtin_exact_terminal_attempt_and_cache_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="600895.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000010",
    )
    _assert_fresh_builtin_origin(fresh)
    akshare = fresh.providers["akshare"]
    assert isinstance(akshare, _AkshareProvider)
    original_indicator = akshare.stock_financial_analysis_indicator
    physical_invocations = 0

    class _RateLimitError(RuntimeError):
        retry_after_seconds = 60

    def retry_once(**kwargs: object) -> pd.DataFrame:
        nonlocal physical_invocations
        physical_invocations += 1
        if physical_invocations == 1:
            akshare._record("stock_financial_analysis_indicator", kwargs)
            raise _RateLimitError("rate limit")
        return original_indicator(**kwargs)

    monkeypatch.setattr(akshare, "stock_financial_analysis_indicator", retry_once)
    try:
        first = fresh.invoke(fresh.state(tool_names=("get_fundamentals",)))
        assert "financial_dispatch_shadow_failure" not in first
        first_ledger = copy.deepcopy(first["financial_dispatch_shadow_ledger"])
        akshare_entries = tuple(
            entry
            for entry in first_ledger["provider_subrequest_cache"]["entries"]
            if entry["key"]["provider_id"] == "akshare_sina"
        )
        assert len(akshare_entries) == 1
        retry_events = akshare_entries[0]["attempt_events"]
        assert physical_invocations == 1
        assert len(retry_events) == 1
        assert [event["attempt_index"] for event in retry_events] == [1]
        assert retry_events[0]["outcome"] == "rate_limited"
        assert retry_events[0]["retryable"] is True
        assert retry_events[0]["final_physical_attempt_count"] == 1
        cooldown = fresh.invoke(
            fresh.state(
                tool_names=("get_fundamentals",),
                indicator_families=(FinancialRatioFamily.PROFIT,),
                tool_call_prefix="qcf10-cooldown",
            )
        )
        assert "financial_dispatch_shadow_failure" not in cooldown
        assert physical_invocations == 1
        cooldown_entries = tuple(
            entry
            for entry in cooldown["financial_dispatch_shadow_ledger"][
                "provider_subrequest_cache"
            ]["entries"]
            if entry["key"]["provider_id"] == "akshare_sina"
        )
        assert len(cooldown_entries) == 2
        skipped = next(
            entry for entry in cooldown_entries if not entry["attempt_events"]
        )
        assert skipped["outcome"]["kind"] == "rate_limited"
        provider_calls_before_repeat = {
            provider_id: copy.deepcopy(provider.calls)
            for provider_id, provider in fresh.providers.items()
        }
        second = fresh.invoke(
            fresh.state(
                tool_names=("get_fundamentals",),
                indicator_families=(FinancialRatioFamily.PROFIT,),
                tool_call_prefix="qcf10-cache-hit",
            )
        )
        assert "financial_dispatch_shadow_failure" not in second
        assert {
            provider_id: provider.calls
            for provider_id, provider in fresh.providers.items()
        } == provider_calls_before_repeat
        second_attempts = _attempt_events(second["financial_dispatch_shadow_ledger"])
        cooldown_attempts = _attempt_events(
            cooldown["financial_dispatch_shadow_ledger"]
        )
        assert second_attempts == cooldown_attempts
        first_attempts = _attempt_events(first_ledger)
        assert len({event["attempt_event_id"] for event in first_attempts}) == len(
            first_attempts
        )
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf11_fresh_builtin_single_flight_and_bulk_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="601328.SS",
        reverse_input_order=False,
        checkpoint_thread_id="0600000000000011",
    )
    _assert_fresh_builtin_origin(fresh)
    statement_dispatches: list[object] = []
    original_dispatch = FinancialToolDispatcher.dispatch_statement_selection

    def capture(dispatcher, request, selection_request):
        result = original_dispatch(dispatcher, request, selection_request)
        statement_dispatches.append(result)
        return result

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_statement_selection",
        capture,
    )
    metadata_acquirer = fresh.owner._metadata_acquirer
    acquirer_type = type(metadata_acquirer)
    original_acquire = acquirer_type.acquire
    original_clock = fresh.owner._eligibility_clock
    original_calendar_provider = fresh.owner._calendar_provider
    original_transaction_factory = (
        lifecycle_module.FinancialEligibilityTemporalEvaluationTransaction
    )
    acquisition_entered = threading.Event()
    release_acquisition = threading.Event()
    metadata_acquisitions = 0
    clock_samples = 0
    calendar_lookups = 0
    temporal_transactions: list[object] = []

    def counted_clock():
        nonlocal clock_samples
        clock_samples += 1
        acquisition_entered.set()
        assert release_acquisition.wait(timeout=5)
        return original_clock()

    def counted_calendar_provider():
        nonlocal calendar_lookups
        calendar_lookups += 1
        return original_calendar_provider()

    def counted_transaction_factory(**kwargs: object):
        transaction = original_transaction_factory(**kwargs)
        temporal_transactions.append(transaction)
        return transaction

    def blocked_acquire(acquirer, *args, **kwargs):
        nonlocal metadata_acquisitions
        if acquirer is metadata_acquirer:
            metadata_acquisitions += 1
        return original_acquire(acquirer, *args, **kwargs)

    monkeypatch.setattr(acquirer_type, "acquire", blocked_acquire)
    fresh.owner._eligibility_clock = counted_clock
    fresh.owner._calendar_provider = counted_calendar_provider
    monkeypatch.setattr(
        lifecycle_module,
        "FinancialEligibilityTemporalEvaluationTransaction",
        counted_transaction_factory,
    )
    try:
        acquisition_start = threading.Barrier(33)

        def acquire_context():
            acquisition_start.wait(timeout=5)
            return fresh.owner.acquire_context(as_of_date=_AS_OF)

        with ThreadPoolExecutor(max_workers=32) as executor:
            acquisition_futures = tuple(
                executor.submit(acquire_context) for _ in range(32)
            )
            acquisition_start.wait(timeout=5)
            assert acquisition_entered.wait(timeout=5)
            release_acquisition.set()
            acquisition_results = tuple(
                future.result(timeout=20) for future in acquisition_futures
            )

        assert len({id(result) for result in acquisition_results}) == 1
        assert acquisition_results[0].context is fresh.owner.context
        assert acquisition_results[0].outcome == "success"
        assert metadata_acquisitions == 1
        assert clock_samples == 2
        assert calendar_lookups == 1
        assert len(temporal_transactions) == 1
        assert temporal_transactions[0].evaluation_count == 2
        context = fresh.owner.context
        assert context is not None
        temporal = context.temporal_eligibility_binding
        assert temporal is not None
        assert temporal.eligibility_evaluated_at == _NOW
        assert temporal.calendar_binding.calendar_revision_id.startswith(
            "market-session-calendar=sha256:"
        )
        assert temporal.company_type_temporal_decision.result == "accepted"
        assert temporal.listing_temporal_decision.result == "accepted"

        first_use_start = threading.Barrier(3)

        def first_financial_use(index: int):
            state = fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix=f"qcf11-first-use-{index}",
            )
            first_use_start.wait(timeout=5)
            return fresh.tool_node(state)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(first_financial_use, index) for index in range(2)
            )
            first_use_start.wait(timeout=5)
            first_results = tuple(future.result(timeout=20) for future in futures)

        assert all(
            "financial_dispatch_shadow_failure" not in result
            for result in first_results
        )
        assert fresh.owner.context is not None
        assert len(fresh.resolver_calls) == 1
        assert len(statement_dispatches) == 2
        statement_routing = statement_dispatches[0].routing_result
        assert statement_dispatches[1].routing_result == statement_routing
        assert len(statement_routing.manifest.selections) > 1
        assert tuple(
            call.provider_id for call in statement_routing.provider_calls
        ) == ("tushare",)
        assert len(statement_routing.provider_calls[0].physical_attempt_ids) == 1
        balance_calls = [
            call
            for call in fresh.providers["tushare"].calls
            if call[0] == "balancesheet"
        ]
        assert len(balance_calls) == 2
        assert [
            endpoint for endpoint, _ in fresh.providers["baostock"].calls
        ].count("query_lifecycle") == 1
        first_attempts = _attempt_events(
            first_results[0]["financial_dispatch_shadow_ledger"]
        )
        repeated = fresh.tool_node(
            fresh.state(
                tool_names=("get_balance_sheet",),
                tool_call_prefix="qcf11-bulk-repeat",
            )
        )
        assert "financial_dispatch_shadow_failure" not in repeated
        assert len(
            [
                call
                for call in fresh.providers["tushare"].calls
                if call[0] == "balancesheet"
            ]
        ) == 2
        assert _attempt_events(repeated["financial_dispatch_shadow_ledger"]) == (
            first_attempts
        )

        tushare = fresh.providers["tushare"]
        assert isinstance(tushare, _TushareProvider)
        original_factor = tushare.adj_factor
        provider_entered = threading.Event()
        release_provider = threading.Event()
        factor_invocations = 0

        def blocked_factor(**kwargs: object) -> pd.DataFrame:
            nonlocal factor_invocations
            factor_invocations += 1
            provider_entered.set()
            assert release_provider.wait(timeout=5)
            return original_factor(**kwargs)

        monkeypatch.setattr(tushare, "adj_factor", blocked_factor)
        factor_request = _capability_request(
            fresh,
            MainlandCapability.ADJUSTMENT_FACTORS,
            range_start=date(2026, 8, 7),
            as_of_date=date(2026, 8, 7),
        )
        start = threading.Barrier(3)

        def route_factor():
            start.wait(timeout=5)
            return fresh.owner.financial_routing.router.route_capability(
                factor_request
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(route_factor) for _ in range(2))
            start.wait(timeout=5)
            assert provider_entered.wait(timeout=5)
            release_provider.set()
            factor_results = tuple(future.result(timeout=10) for future in futures)

        assert factor_results[0] == factor_results[1]
        assert factor_results[0].selected_provider == "tushare"
        assert factor_invocations == 1
        shared_cache = fresh.owner.financial_routing.provider_subrequest_cache
        assert shared_cache is not None
        factor_entries = tuple(
            entry
            for entry in shared_cache.checkpoint()["entries"]
            if entry["key"]["provider_id"] == "tushare"
            and entry["key"]["capacity_scope"] == "adj_factor"
        )
        assert len(factor_entries) == 1
        assert len(factor_entries[0]["attempt_events"]) == 1
    finally:
        fresh.close()

    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())


@pytest.mark.unit
def test_qcf12_fresh_create_checkpoint_restores_with_zero_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_thread_id = "0600000000000012"
    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol="600895.SS",
        reverse_input_order=False,
        checkpoint_thread_id=checkpoint_thread_id,
    )
    _assert_fresh_builtin_origin(fresh)
    indicator_dispatches: list[tuple[object, object, object]] = []
    original_dispatch = FinancialToolDispatcher.dispatch_indicator_selection

    def capture(dispatcher, request, selection_request):
        result = original_dispatch(dispatcher, request, selection_request)
        indicator_dispatches.append((request, selection_request, result))
        return result

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "dispatch_indicator_selection",
        capture,
    )
    source_result = fresh.invoke(fresh.state(tool_names=("get_fundamentals",)))
    assert "financial_dispatch_shadow_failure" not in source_result
    assert fresh.owner.context is not None
    assert len(indicator_dispatches) == 1
    original_request, selection_request, original_selection = indicator_dispatches[0]
    source_context = fresh.owner.context
    source_ledger = copy.deepcopy(source_result["financial_dispatch_shadow_ledger"])
    checkpoint_state = fresh.checkpoint_state()
    assert checkpoint_state["financial_dispatch_shadow_ledger"] == source_ledger
    assert checkpoint_state["run_id"] == fresh.owner.run_id
    assert checkpoint_state["trade_date"] == _AS_OF.isoformat()
    checkpoint_before_restore = copy.deepcopy(checkpoint_state)
    source_attempts = _attempt_events(source_ledger)
    source_attempt_ids = tuple(event["attempt_event_id"] for event in source_attempts)
    source_routing = original_selection.routing_result
    artifact_root = (
        fresh.composition.blueprint.provider_artifact_root_path
        / "qualified-provider-subrequests"
    )

    def artifact_bytes() -> dict[str, bytes]:
        return {
            path.relative_to(artifact_root).as_posix(): path.read_bytes()
            for path in artifact_root.rglob("*")
            if path.is_file()
        }

    source_artifacts = artifact_bytes()
    runtime_factories = fresh.composition.runtime_factories
    assert runtime_factories is not None
    fresh.close()
    assert all(provider.interrupt_calls == 1 for provider in fresh.providers.values())
    assert all(provider.close_calls == 1 for provider in fresh.providers.values())

    restore_providers: dict[str, _OwnedFakeProvider] = {
        "tushare": _TushareProvider("600895.SS", reverse_input_order=False),
        "akshare": _AkshareProvider(reverse_input_order=False),
        "yfinance": _YahooProvider(),
        "baostock": _BaoStockProvider("600895.SS"),
    }
    coordinator_sequences = 0
    artifact_writes = 0
    metadata_acquisitions = 0
    company_type_resolutions = 0
    temporal_evaluations = 0
    frontier_lookups = 0
    restore_clock_samples = 0
    restore_calendar_lookups = 0
    baostock_lifecycle_calls = 0
    closed_resources: list[str] = []

    def track_close(resource_name: str, resource):
        close = resource.close

        def tracked_close():
            closed_resources.append(resource_name)
            close()

        resource.close = tracked_close
        return resource

    def open_store(blueprint):
        return track_close(
            "coordinator_store",
            runtime_factories.open_coordinator_store(blueprint),
        )

    def build_coordinator(store, stop_signal):
        coordinator = runtime_factories.build_coordinator(store, stop_signal)

        def forbidden_sequence(**_kwargs):
            nonlocal coordinator_sequences
            coordinator_sequences += 1
            pytest.fail("QCF12 restore requested a coordinator permit")

        coordinator.execute_retry_sequence = forbidden_sequence

        def forbidden_calendar(_reference_market):
            nonlocal restore_calendar_lookups
            restore_calendar_lookups += 1
            pytest.fail("QCF12 restore read the current calendar frontier")

        coordinator.read_current_session_calendar = forbidden_calendar
        return track_close("coordinator", coordinator)

    def build_cache(coordinator, artifact_store, run_id, stop_signal):
        return track_close(
            "subrequest_cache",
            runtime_factories.build_subrequest_cache(
                coordinator,
                artifact_store,
                run_id,
                stop_signal,
            ),
        )

    def forbidden_install(self, *args, **kwargs):
        nonlocal artifact_writes
        artifact_writes += 1
        pytest.fail("QCF12 restore attempted an artifact write")

    monkeypatch.setattr(
        ProviderSubrequestArtifactStore,
        "install",
        forbidden_install,
    )

    def forbidden_acquire(*_args, **_kwargs):
        nonlocal metadata_acquisitions
        metadata_acquisitions += 1
        pytest.fail("QCF12 restore attempted metadata acquisition")

    def forbidden_resolution(**_kwargs):
        nonlocal company_type_resolutions
        company_type_resolutions += 1
        pytest.fail("QCF12 restore attempted company-type resolution")

    monkeypatch.setattr(
        metadata_module.FinancialEligibilityMetadataAcquirer,
        "acquire",
        forbidden_acquire,
    )
    monkeypatch.setattr(
        metadata_module,
        "resolve_financial_company_type",
        forbidden_resolution,
    )

    def forbidden_temporal_evaluation(**_kwargs):
        nonlocal temporal_evaluations
        temporal_evaluations += 1
        pytest.fail("QCF12 restore invoked the temporal evaluator")

    def forbidden_frontier(*_args, **_kwargs):
        nonlocal frontier_lookups
        frontier_lookups += 1
        pytest.fail("QCF12 restore recomputed latest_completed_session")

    def forbidden_lifecycle(*_args, **_kwargs):
        nonlocal baostock_lifecycle_calls
        baostock_lifecycle_calls += 1
        pytest.fail("QCF12 restore invoked BaoStock lifecycle acquisition")

    monkeypatch.setattr(
        temporal_projection_module,
        "evaluate_financial_metadata_temporal_eligibility",
        forbidden_temporal_evaluation,
    )
    monkeypatch.setattr(
        temporal_module,
        "latest_completed_session",
        forbidden_frontier,
    )
    monkeypatch.setattr(
        BaoStockCapabilityAdapter,
        "acquire_lifecycle",
        forbidden_lifecycle,
    )

    def assemble_restore_runtime(**kwargs):
        assembly = runtime_factories.assemble_runtime(**kwargs)

        def forbidden_clock():
            nonlocal restore_clock_samples
            restore_clock_samples += 1
            pytest.fail("QCF12 restore sampled the eligibility clock")

        def forbidden_calendar_provider():
            nonlocal restore_calendar_lookups
            restore_calendar_lookups += 1
            pytest.fail("QCF12 restore consulted the calendar provider")

        return replace(
            assembly,
            eligibility_clock=forbidden_clock,
            eligibility_calendar_provider=forbidden_calendar_provider,
        )

    restore_factories = replace(
        runtime_factories,
        open_coordinator_store=open_store,
        build_coordinator=build_coordinator,
        build_subrequest_cache=build_cache,
        build_tushare_client=lambda _blueprint, _stop: restore_providers["tushare"],
        build_akshare_facade=lambda _blueprint, _stop: restore_providers["akshare"],
        build_yahoo_transport=lambda _blueprint, _stop: restore_providers["yfinance"],
        build_baostock_transport=lambda _blueprint, _stop: restore_providers["baostock"],
        assemble_runtime=assemble_restore_runtime,
    )
    monkeypatch.setattr(
        composition_module,
        "production_qualified_routing_runtime_factories",
        lambda: restore_factories,
    )
    restore_llm = MagicMock(name="qcf12_zero_io_llm")
    restore_client = MagicMock(name="qcf12_zero_io_llm_client")
    restore_client.get_llm.return_value = restore_llm
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: restore_client,
    )
    restored_composition = cli_main.build_qualified_financial_routing_composition(
        plan=fresh.plan,
        asset_configuration=fresh.asset,
        config=fresh.config,
        validation_workload=True,
    )
    assert isinstance(
        restored_composition,
        RegisteredProductionQualifiedRoutingComposition,
    )
    restored_host = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=fresh.config,
        asset_configuration=fresh.asset,
        capability_routing_plan=fresh.plan,
        qualified_financial_routing=restored_composition,
    )
    assert restored_host.financial_eligibility_context_owner is None
    restored_runtime = restored_host.bind_qualified_routing_runtime(
        run_id=fresh.owner.run_id,
        checkpoint_compatibility_known=True,
        resume_from_checkpoint=True,
        checkpoint_thread_id=checkpoint_thread_id,
        checkpoint_state=MappingProxyType(checkpoint_state),
        analysis_as_of_date=_AS_OF,
    )
    assert restored_runtime is not None
    restored_owner = restored_host.financial_eligibility_context_owner
    assert restored_owner is not None
    assert restored_owner.context == source_context
    restored_routing = restored_owner.financial_routing
    restored_dispatcher = restored_routing.restored_shadow_dispatcher
    restored_cache = restored_routing.provider_subrequest_cache
    assert restored_dispatcher is not None
    assert restored_cache is not None
    assert restored_dispatcher.checkpoint_ledger() == source_ledger
    assert restored_cache.checkpoint() == source_ledger["provider_subrequest_cache"]
    repeated = restored_dispatcher.dispatch_indicator_selection(
        original_request.model_copy(update={"tool_call_id": "qcf12-restored-repeat"}),
        selection_request,
    )
    assert repeated.disposition == "duplicate_suppressed"
    assert repeated.routing_result == source_routing
    repeated_ledger = restored_dispatcher.checkpoint_ledger()
    repeated_attempts = _attempt_events(repeated_ledger)
    assert tuple(event["attempt_event_id"] for event in repeated_attempts) == (
        source_attempt_ids
    )
    assert restored_cache.checkpoint() == source_ledger["provider_subrequest_cache"]
    assert checkpoint_state == checkpoint_before_restore
    assert artifact_bytes() == source_artifacts
    assert coordinator_sequences == 0
    assert artifact_writes == 0
    assert metadata_acquisitions == 0
    assert company_type_resolutions == 0
    assert temporal_evaluations == 0
    assert frontier_lookups == 0
    assert restore_clock_samples == 0
    assert restore_calendar_lookups == 0
    assert baostock_lifecycle_calls == 0
    assert all(provider.calls == [] for provider in restore_providers.values())
    restore_llm.invoke.assert_not_called()
    restored_host.close_qualified_routing_runtime()
    closed_once = tuple(closed_resources)
    restored_host.close_qualified_routing_runtime()
    assert tuple(closed_resources) == closed_once
    assert set(closed_resources) == {
        "coordinator_store",
        "coordinator",
        "subrequest_cache",
    }
    assert all(
        provider.interrupt_calls == 1 for provider in restore_providers.values()
    )
    assert all(provider.close_calls == 1 for provider in restore_providers.values())

    invalid_states: dict[str, dict[str, object]] = {}
    old_state = copy.deepcopy(checkpoint_state)
    old_ledger = old_state["financial_dispatch_shadow_ledger"]
    old_ledger["contract_version"] = "1.4"
    for field_name in (
        "capability_routing_plan_version",
        "financial_indicator_manifest_version",
        "indicator_field_semantics_version",
        "indicator_metadata_compatibility_version",
        "indicator_metadata_assessments",
        "indicator_metadata_rejections",
        "indicator_provider_artifact_identities",
        "indicator_candidate_identities",
        "indicator_provider_topology",
    ):
        old_ledger.pop(field_name, None)
    invalid_states["old"] = old_state

    absent_state = copy.deepcopy(checkpoint_state)
    absent_state["financial_dispatch_shadow_ledger"].pop(
        "indicator_metadata_assessments"
    )
    invalid_states["absent"] = absent_state

    corrupt_state = copy.deepcopy(checkpoint_state)
    corrupt_state["financial_dispatch_shadow_ledger"][
        "indicator_metadata_compatibility_version"
    ] = "financial-indicator-metadata-compatibility-v0"
    invalid_states["corrupt"] = corrupt_state

    contradictory_state = copy.deepcopy(checkpoint_state)
    contradictory_state["financial_dispatch_shadow_ledger"][
        "indicator_metadata_assessments"
    ][0]["assessment_identity"] = (
        "financial-indicator-metadata-compatibility:v1:" + "f" * 64
    )
    invalid_states["contradictory"] = contradictory_state

    mismatched_state = copy.deepcopy(checkpoint_state)
    mismatched_state["financial_dispatch_shadow_ledger"][
        "indicator_candidate_identities"
    ][0] = "financial-period-candidate:v1:" + "f" * 64
    invalid_states["mismatched"] = mismatched_state

    accepted_failure_reasons = {
        FinancialDispatchCheckpointFailureReason.MALFORMED,
        FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH,
        FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
    }
    resources_after_valid_restore = tuple(closed_resources)
    for case_name, invalid_state in invalid_states.items():
        state_before_failure = copy.deepcopy(invalid_state)
        invalid_composition = cli_main.build_qualified_financial_routing_composition(
            plan=fresh.plan,
            asset_configuration=fresh.asset,
            config=fresh.config,
            validation_workload=True,
        )
        assert isinstance(
            invalid_composition,
            RegisteredProductionQualifiedRoutingComposition,
        )
        invalid_host = TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=fresh.config,
            asset_configuration=fresh.asset,
            capability_routing_plan=fresh.plan,
            qualified_financial_routing=invalid_composition,
        )
        with pytest.raises(FinancialDispatchCheckpointError) as failure:
            invalid_host.bind_qualified_routing_runtime(
                run_id=fresh.owner.run_id,
                checkpoint_compatibility_known=True,
                resume_from_checkpoint=True,
                checkpoint_thread_id=checkpoint_thread_id,
                checkpoint_state=MappingProxyType(invalid_state),
                analysis_as_of_date=_AS_OF,
            )
        assert failure.value.reason in accepted_failure_reasons, case_name
        assert invalid_state == state_before_failure
        invalid_host.close_qualified_routing_runtime()

    assert tuple(closed_resources) == resources_after_valid_restore
    assert all(provider.calls == [] for provider in restore_providers.values())
    assert all(
        provider.interrupt_calls == 1 for provider in restore_providers.values()
    )
    assert all(provider.close_calls == 1 for provider in restore_providers.values())
    assert artifact_bytes() == source_artifacts
    assert checkpoint_state == checkpoint_before_restore
