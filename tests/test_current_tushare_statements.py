from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import tradingagents.dataflows.current_tushare_statements as current_statements
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_dispatch import (
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import AcquisitionUnavailableReason

TOOLS = {
    "get_balance_sheet": FinancialStatementType.BALANCE_SHEET,
    "get_income_statement": FinancialStatementType.INCOME_STATEMENT,
    "get_cashflow": FinancialStatementType.CASH_FLOW,
}


def runtime_config(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    authority = Path(__file__).resolve().parents[1] / "config"
    config.update(
        instrument_identity_registry_path=str(authority / "instrument_identity_registry.json"),
        instrument_identity_registry_sha256=(authority / "instrument_identity_registry.sha256")
        .read_text(encoding="ascii").split()[0],
        market_history_mode="current",
        data_usage_mode="personal_research",
        mainland_capability_routing_mode="legacy",
        tool_vendors=dict.fromkeys(TOOLS, "tushare"),
        data_cache_dir=str(tmp_path / "cache"),
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        tushare_financial_https_endpoint=None,
    )
    return config


def dispatcher(config, *, clock=None, sleeper=None, checkpoint_ledger=None):
    asset = resolve_run_asset_configuration("600895.SS", config=config)
    return FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        clock=clock,
        sleeper=sleeper,
        checkpoint_ledger=checkpoint_ledger,
    )


def request(tool, *, frequency=FinancialReportingFrequency.QUARTERLY):
    return FinancialToolRequest(
        tool_name=tool,
        statement_type=TOOLS[tool],
        frequency=frequency,
        as_of_date=date(2026, 10, 15),
        tool_call_id=tool + ":" + frequency.value,
    )


@pytest.mark.parametrize("tool", TOOLS)
def test_current_explicit_tushare_route_reports_missing_endpoint_before_io(tmp_path, tool):
    result = dispatcher(runtime_config(tmp_path)).dispatch(request(tool))
    assert result.artifact is None
    assert len(result.plan_outcomes) == 1
    assert result.plan_outcomes[0].provider == "tushare"
    assert result.plan_outcomes[0].outcome.reason is AcquisitionUnavailableReason.NOT_CONFIGURED
    assert not (tmp_path / "cache").exists()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


def statement_rows(endpoint):
    fields = {
        "balancesheet": {
            "acct_payable": 100,
            "accounts_receiv": 120,
            "money_cap": 80,
            "inventories": 50,
            "lt_borr": 40,
            "fix_assets": 400,
            "st_borr": 20,
            "total_assets": 1000,
            "total_hldr_eqy_inc_min_int": 600,
            "total_liab": 400,
        },
        "income": {
            "admin_exp": 10,
            "oper_cost": 400,
            "fin_exp": 5,
            "gross_profit": 500,
            "income_tax": 20,
            "n_income_attr_p": 160,
            "total_cogs": 700,
            "operate_profit": 200,
            "total_revenue": 900,
            "oth_income": 5,
            "total_profit": 180,
            "rd_exp": 10,
            "sell_exp": 15,
        },
        "cashflow": {
            "n_cash_flows_fnc_act": -10,
            "n_cashflow_inv_act": -30,
            "n_cashflow_act": 160,
            "c_pay_interest": 2,
            "c_paid_for_taxes": 20,
            "c_fr_sale_sg": 900,
            "c_cash_equ_end_period": 200,
            "eff_fx_flu_cash": 0,
            "n_incr_cash_cash_equ": 120,
            "c_cash_equ_beg_period": 80,
        },
    }[endpoint]
    return pd.DataFrame(
        [
            {
                "ts_code": "600895.SH",
                "ann_date": announced,
                "f_ann_date": announced,
                "end_date": period,
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "1",
                **fields,
            }
            for period, announced in (("20251231", "20260420"), ("20260630", "20260820"))
        ]
    )


def test_production_three_tables_reuse_raw_response_across_frequencies_and_analyses(
    tmp_path, monkeypatch
):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append((endpoint, params))
            return statement_rows(endpoint)

    monkeypatch.setattr(
        current_statements,
        "create_financial_statement_client",
        lambda endpoint: Client(),
        raising=False,
    )
    clock = Clock()
    first = dispatcher(config, clock=clock, sleeper=clock.sleep)
    first_values = {}
    for tool in TOOLS:
        for frequency in (
            FinancialReportingFrequency.QUARTERLY,
            FinancialReportingFrequency.ANNUAL,
        ):
            result = first.dispatch(request(tool, frequency=frequency))
            assert result.artifact is not None
            assert result.provider == "tushare"
            assert "CURRENT-only" in result.value
            assert "CNY_BASE_UNIT" in result.value
            assert "2026-04-20" in result.value
            assert "2025-12-31" in result.value
            first_values[(tool, frequency)] = result.value
    assert sorted(endpoint for endpoint, _ in calls) == ["balancesheet", "cashflow", "income"]
    assert all(params["ts_code"] == "600895.SH" for _, params in calls)
    clock.value += timedelta(hours=1)
    second = dispatcher(config, clock=clock, sleeper=clock.sleep)
    for tool in TOOLS:
        result = second.dispatch(request(tool))
        assert result.artifact is not None
        # Acquisition reuse must preserve the actual source observation time.
        assert result.value == first_values[(tool, FinancialReportingFrequency.QUARTERLY)]
    assert len(calls) == 3


def test_configurable_refresh_replaces_revisions_and_failure_retains_stale(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config.update(
        tushare_financial_https_endpoint="https://api.tushare.pro",
        tushare_financial_cache_max_age_seconds=7200,
    )
    calls = []
    revision = [1000]

    class Client:
        def query(self, endpoint, **params):
            calls.append(params)
            if revision[0] is None:
                raise TimeoutError("synthetic timeout")
            frame = statement_rows(endpoint)
            frame["total_assets"] = revision[0]
            return frame

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()

    def run():
        return dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
            request("get_balance_sheet")
        )

    first = run()
    assert first.artifact is not None
    clock.value += timedelta(hours=3)
    revision[0] = 1200
    revised = run()
    assert len(calls) == 2
    assert ",total_assets,1200," in revised.value
    assert ",total_assets,1000," not in revised.value
    clock.value += timedelta(hours=3)
    revision[0] = None
    stale = run()
    assert stale.artifact is not None
    assert len(calls) == 3
    assert "degraded=true" in stale.value
    assert "stale_announcement_ranges=" in stale.value
    assert ",total_assets,1200," in stale.value
    assert "2026-10-15T07:00:00+00:00" in stale.value


def test_only_missing_announcement_tail_is_fetched(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(params)
            frame = statement_rows(endpoint)
            return frame[
                (frame.ann_date >= params["start_date"]) & (frame.ann_date <= params["end_date"])
            ]

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    first_request = request("get_balance_sheet").model_copy(
        update={"as_of_date": date(2026, 8, 21)}
    )
    assert dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(first_request).artifact
    later = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert later.artifact
    assert [(c["start_date"], c["end_date"]) for c in calls] == [
        ("20200101", "20260821"),
        ("20260822", "20261015"),
    ]
    assert "2026-04-20" in later.value


@pytest.mark.parametrize(
    "change",
    [
        {"ts_code": "000001.SZ"},
        {"unit": "unsupported-unit"},
        {"ann_date": "20261016"},
        {"ann_date": None},
        {"end_date": "20261231"},
        {"f_ann_date": "20261016"},
        {"total_assets": None},
        {"report_type": "2"},
    ],
)
def test_invalid_or_future_rows_cannot_be_available(tmp_path, monkeypatch, change):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(params)
            frame = statement_rows(endpoint)
            for key, value in change.items():
                frame[key] = value
            return frame

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    result = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert result.artifact is None
    assert len(calls) == 1


def test_local_financial_selection_is_narrow_and_refresh_is_configurable(tmp_path, monkeypatch):
    import tradingagents.default_config as defaults

    monkeypatch.setattr(defaults, "_PROJECT_RUNTIME_ROOT", str(tmp_path))
    profile = {
        "tool_vendors": dict.fromkeys(TOOLS, "tushare"),
        "tushare_financial_https_endpoint": "https://api.tushare.pro",
        "tushare_financial_cache_max_age_seconds": 604800,
        "tushare_financial_force_refresh": False,
    }
    (tmp_path / "tradingagents.local.json").write_text(json.dumps(profile))
    config = defaults._apply_local_analysis_overrides(copy.deepcopy(DEFAULT_CONFIG))
    assert config["tool_vendors"] == profile["tool_vendors"]
    assert config["tushare_financial_cache_max_age_seconds"] == 604800
    assert (
        config["market_data_vendors"]["cn_a"]["news_data"]
        == DEFAULT_CONFIG["market_data_vendors"]["cn_a"]["news_data"]
    )
    for bad in (
        {"tool_vendors": {"get_fundamentals": "tushare"}},
        {"tushare_financial_https_endpoint": "http://api.tushare.pro"},
        {"tushare_financial_cache_max_age_seconds": 0},
        {"tushare_financial_cache_max_age_seconds": True},
        {"tushare_financial_force_refresh": "yes"},
    ):
        (tmp_path / "tradingagents.local.json").write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            defaults._apply_local_analysis_overrides(copy.deepcopy(DEFAULT_CONFIG))


def test_real_graph_tool_node_and_completed_checkpoint_reuse(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    from tradingagents.evidence import EvidenceState
    from tradingagents.graph.financial_tools import FinancialDispatchToolNode

    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    asset = resolve_run_asset_configuration("600895.SS", config=config)
    node = FinancialDispatchToolNode(config=config, clock=clock, sleeper=clock.sleep)

    def message(suffix):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool,
                    "args": {"ticker": "600895.SS", "freq": "quarterly", "curr_date": "2026-10-15"},
                    "id": tool + suffix,
                    "type": "tool_call",
                }
                for tool in TOOLS
            ],
        )

    state = {
        "messages": [message("-first")],
        "evidence_state": EvidenceState(instrument_identity=asset.instrument_identity).model_dump(
            mode="json"
        ),
        "asset_configuration": asset.model_dump(mode="json"),
        "trade_date": "2026-10-15",
    }
    result = node(state)
    assert len(calls) == 3
    assert all("CURRENT-only" in msg.content for msg in result["messages"])
    # Completed logical checkpoint reuse requires no cache/client work at all.
    monkeypatch.setattr(
        current_statements,
        "create_financial_statement_client",
        lambda _: pytest.fail("completed checkpoint must not fetch"),
    )
    clock.value += timedelta(days=2)
    state.update(result)
    state["messages"] = [message("-resume")]
    resumed = FinancialDispatchToolNode(config=config, clock=clock, sleeper=clock.sleep)(state)
    assert len(calls) == 3
    assert all(msg.artifact["disposition"] == "duplicate_suppressed" for msg in resumed["messages"])


def test_partial_failure_and_cache_write_failure_are_visible(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(params)
            if len(calls) == 2:
                raise ConnectionError("synthetic disconnect")
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    early = request("get_balance_sheet").model_copy(update={"as_of_date": date(2026, 8, 21)})
    initial = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(early)
    assert initial.artifact
    partial = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert partial.artifact
    assert "missing_announcement_ranges=2026-08-22/2026-10-15" in partial.value
    assert "fetch_failure=disconnect" in partial.value
    assert len(calls) == 2

    def fail_write(*_):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(current_statements.CurrentStatementCache, "_store", fail_write)
    config["tushare_financial_force_refresh"] = True
    write_failure = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert write_failure.artifact
    assert "cache_failure=cache_write_failed" in write_failure.value
    assert "degraded=true" in write_failure.value


def test_empty_response_and_malformed_response_never_fabricate_artifact(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []
    frame = [pd.DataFrame(columns=["ts_code", "ann_date", "end_date"])]

    class Client:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            return frame[0]

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    for _ in range(2):
        assert (
            dispatcher(config, clock=clock, sleeper=clock.sleep)
            .dispatch(request("get_balance_sheet"))
            .artifact
            is None
        )
    assert calls == ["balancesheet"]
    frame[0] = pd.DataFrame({"unexpected": [1]})
    clock.value += timedelta(days=1)
    malformed = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert malformed.artifact is None
    assert (
        malformed.plan_outcomes[0].outcome.reason is AcquisitionUnavailableReason.MALFORMED_RESPONSE
    )
    assert len(calls) == 2


def test_current_route_is_opt_in_and_preserves_fundamentals_chain(tmp_path):
    config = runtime_config(tmp_path)
    selected = dispatcher(config).checkpoint_ledger()
    config["tool_vendors"] = {}
    ordinary = dispatcher(config).checkpoint_ledger()
    # The dedicated CURRENT implementation cannot alter non-statement routing.
    selected_bindings = selected["provider_chain_identities"]
    ordinary_bindings = ordinary["provider_chain_identities"]
    selected_summary = next(
        item for item in selected_bindings if item["tool_name"] == "get_fundamentals"
    )
    ordinary_summary = next(
        item for item in ordinary_bindings if item["tool_name"] == "get_fundamentals"
    )
    assert selected_summary == ordinary_summary


def test_forced_failed_refresh_keeps_fresh_data_and_names_failed_range(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(params)
            if len(calls) > 1:
                raise TimeoutError("synthetic timeout")
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    first = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert first.artifact
    config["tushare_financial_force_refresh"] = True
    clock.value += timedelta(hours=1)
    failed = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(
        request("get_balance_sheet")
    )
    assert failed.artifact
    assert "failure_announcement_range=2020-01-01/2026-10-15" in failed.value
    assert "2026-10-15T04:00:00+00:00" in failed.value
    assert len(calls) == 2


def test_rate_limit_shares_coordinator_cooldown_across_three_tables(tmp_path, monkeypatch):
    from tradingagents.market_history.coordinator import (
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
    )

    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Client:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.RATE_LIMITED, retryable=False, retry_after_seconds=30
            )

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    clock = Clock()
    route = dispatcher(config, clock=clock, sleeper=clock.sleep)
    results = [route.dispatch(request(tool)) for tool in TOOLS]
    assert all(result.artifact is None for result in results)
    assert calls == ["balancesheet"]
