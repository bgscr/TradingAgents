"""Personal Full boundaries, with all external inputs simulated."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts import personal_full as personal, personal_graph
from tests import test_budget_readiness, test_tushare_current_incremental

config = test_budget_readiness.config
rig = test_tushare_current_incremental.rig


@pytest.mark.parametrize(
    "ticker,canonical", [("600895.SH", "600895.SS"), ("000001.SZ", "000001.SZ")]
)
def test_clean_cli_without_config(tmp_path, ticker, canonical):
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE"}
    }
    from shutil import copyfile

    root = tmp_path
    source = Path(__file__).resolve().parents[1]
    for name in (
        "scripts/personal_full.py",
        "scripts/personal_graph.py",
        "scripts/project_credentials.py",
        "tradingagents/__init__.py",
        "tradingagents/default_config.py",
        "tradingagents/dataflows/__init__.py",
        "tradingagents/dataflows/symbol_utils.py",
        "tradingagents/dataflows/crypto_universe.py",
        "tradingagents/dataflows/errors.py",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        copyfile(source / name, root / name)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(root / "scripts/personal_full.py"),
            "--ticker",
            ticker,
            "--check-only",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=25,
    )
    receipt = json.loads(result.stdout)
    assert result.returncode == 2
    assert receipt["ticker"] == canonical and len(receipt["analysts"]) == 4
    assert receipt["missing_credentials"] == ["TUSHARE_TOKEN", "DEEPSEEK_API_KEY"]
    assert receipt["cache_issue"] == "market_cache_missing"
    assert receipt["preflight_ready"] is False


def test_market_boundaries(tmp_path):
    from datetime import date, timedelta, timezone
    from types import SimpleNamespace

    from tradingagents.market_history.calendar import MarketSession, MarketSessionStatus

    now = datetime.fromisoformat("2026-11-27T14:00:00+08:00")
    days = [date(2026, 11, 26), date(2026, 11, 27)]
    calendar = SimpleNamespace(
        timezone_name="Asia/Shanghai",
        observed_at=now,
        sessions=tuple(MarketSession(d, MarketSessionStatus.OPEN) for d in days),
    )
    assert personal.selected_session(calendar, now) == days[0]
    assert personal.selected_session(calendar, now + timedelta(hours=2)) == days[1]
    with pytest.raises(ValueError, match="calendar_not_current"):
        personal.selected_session(calendar, now + timedelta(days=1))
    calendar.sessions = calendar.sessions[:1]
    with pytest.raises(ValueError, match="calendar_coverage_missing"):
        personal.selected_session(calendar, now)
    for value in ["NVDA", "510500.SH", "123456.SZ", "600895.SZ", "../../600895.SH"]:
        with pytest.raises(ValueError, match="unsupported_equity"):
            personal.check(value, {}, {}, datetime.now(timezone.utc))


def test_stock_close_requires_matching_current_payload(tmp_path, rig):
    from datetime import timedelta
    from types import SimpleNamespace

    from tests.test_tushare_current_incremental import DAYS, NOW, SYMBOL
    from tradingagents.budget_readiness import _readonly_history
    from tradingagents.dataflows.config import get_config
    from tradingagents.market_history.calendar import MarketSession, MarketSessionStatus

    _, load, calls, *_ = rig
    day = DAYS[-1]
    load(day, day)
    calendar = SimpleNamespace(
        timezone_name="Asia/Shanghai", sessions=[MarketSession(day, MarketSessionStatus.OPEN)]
    )
    count = len(calls)
    with _readonly_history(get_config()) as store:
        assert personal.cached_close(store, SYMBOL, calendar, day, NOW, 86400)
        assert not personal.cached_close(store, "000001.SZ", calendar, day, NOW, 86400)
        assert not personal.cached_close(
            store, SYMBOL, calendar, day, NOW + timedelta(days=2), 86400
        )
    assert len(calls) == count


def test_scrapers_and_children_disabled_at_source(monkeypatch):
    from requests import Request, RequestException, Session

    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import interface

    with personal_graph.personal_sources():
        assert "未采集" in sentiment_analyst.get_china_a_local_sentiment("600895.SH", "", "")
        assert (
            interface.get_china_a_enhancements_for_categories(
                "600895.SH", "", "basic", {"flow_sentiment"}
            )
            == ""
        )
        with pytest.raises(RequestException, match="optional_source_disabled"):
            Session().send(Request("GET", "https://example.invalid").prepare())
        with pytest.raises(PermissionError, match="external_helper_disabled"):
            subprocess.Popen(["unavailable-example"])


def test_local_payload_members_and_owner_are_checked_before_resolution(tmp_path, config):
    from tradingagents.market_history.store import MarketHistoryStore

    payload = Path(config["market_history_payload_root"])
    payload.mkdir()
    original = tmp_path / "ordinary-fixture.txt"
    original.write_text("synthetic", encoding="utf-8")
    os.link(original, payload / ".market-history-database-owner")
    with pytest.raises(ValueError, match="linked_local_path"):
        personal.check_store_paths(config)
    os.link(original, payload / "member")
    with personal_graph.local_payloads(), pytest.raises(ValueError, match="linked_local_path"):
        MarketHistoryStore._checked_path(payload, Path("member"))


def test_project_credentials_use_one_local_source(tmp_path, monkeypatch):
    from scripts.project_credentials import ProjectCredentialError

    monkeypatch.setattr(personal, "ROOT", tmp_path)
    for key in personal.KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "TUSHARE_TOKEN=example-one\nDEEPSEEK_API_KEY=example-two\n", encoding="ascii"
    )
    assert all(personal.credentials().values())
    with (tmp_path / ".env").open("a", encoding="ascii") as stream:
        stream.write("TUSHARE_TOKEN=example-three\n")
    with pytest.raises(ProjectCredentialError, match="duplicate_key"):
        personal.credentials()


def test_refuse_unconfigured_provider_before_graph():
    with pytest.raises(ValueError, match="deepseek_configuration_required"):
        personal_graph.prepare({"llm_provider": "other"}, "600895.SS", "2026-11-26")
    with pytest.raises(ValueError, match="deepseek_model_selection_required"):
        personal_graph.prepare({"llm_provider": "deepseek"}, "000001.SZ", "2026-11-26")


def identity_profile(tmp_path, config, symbol, venue):
    from hashlib import sha256

    registry = {
        "schema_version": "1.0",
        "registry_id": "synthetic",
        "rows": [
            {
                "canonical_symbol": symbol,
                "venue": venue,
                "instrument_kind": "equity",
                "currency": "CNY",
                "provenance": {
                    "provider": "synthetic",
                    "source_ref": "synthetic:registry",
                    "retrieved_at": "2024-01-01T00:00:00Z",
                },
            }
        ],
    }
    path = tmp_path / "registry.json"
    path.write_bytes(json.dumps(registry).encode())
    config.update(
        instrument_identity_registry_path=str(path),
        instrument_identity_registry_sha256=sha256(path.read_bytes()).hexdigest(),
        llm_provider="deepseek",
        deep_think_llm="deepseek-example",
        quick_think_llm="deepseek-example",
        backend_url=None,
        market_data_vendors={"cn_a": {"core_stock_apis": "tushare"}},
    )


def synthetic_account(config):
    from datetime import timedelta, timezone

    from tradingagents.market_history.config import (
        MarketHistoryConfig,
        provider_request_authority_config,
    )
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    authority = provider_request_authority_config(MarketHistoryConfig.from_mapping(config))
    with MarketHistoryStore.open(authority) as store:
        coordinator = ProviderRequestCoordinator(store)
        service, name = upstream_service_identity_for_provider("tushare")
        coordinator.register_upstream_service(service, name)
        coordinator.configure_operator_ceiling(
            upstream_service_id=service,
            minimum_interval=timedelta(seconds=3),
            allow_prewarming=False,
            configured_at=datetime.now(timezone.utc),
        )


def synthetic_market(config, symbol, venue, first, day, observed, through=None):
    from dataclasses import replace
    from datetime import timedelta

    import pandas as pd

    from tests.test_tushare_sse_prepared_calendar import publication, rows
    from tradingagents.dataflows import tushare_market_history as market
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.current_incremental import load_current_history
    from tradingagents.market_history.store import MarketHistoryStore

    exchange = {"XSHG": "SSE", "XSHE": "SZSE"}[venue]
    through = through or day
    values = rows(first, through, exchange)
    for row in values:
        instant = datetime.strptime(row["cal_date"], "%Y%m%d")
        row["pretrade_date"] = (instant - timedelta(days=1)).strftime("%Y%m%d")
    from tradingagents.market_history.current_cache import bind_prepared_publication

    calendar, _ = publication(values, start=first, end=through, symbol=symbol)
    calendar = bind_prepared_publication(calendar, exchange)
    code = symbol.replace(".SS", ".SH")
    binding, instrument = market.history_binding(symbol)
    base = {"ts_code": code, "trade_date": day.strftime("%Y%m%d")}
    candidate = market._candidate(
        pd.DataFrame([dict(base, open="11", high="13", low="10", close="12", vol="10")]),
        pd.DataFrame([dict(base, adj_factor="1")]),
        pd.DataFrame(),
        pd.DataFrame([row for row in values if row["cal_date"] == base["trade_date"]]),
        binding=binding,
        instrument=instrument,
        ts_code=code,
        exchange=exchange,
        start=day,
        end=day,
        observed_at=observed,
    )
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        store.publish_session_calendar(replace(calendar, observed_at=observed))
        load_current_history(
            store,
            symbol,
            str(day),
            str(day),
            acquire=lambda *_: candidate,
            acquire_calendar=lambda *_: pytest.fail("unexpected calendar acquisition"),
            now=observed,
        )


def test_verified_profile_requires_real_cache_coverage(tmp_path, config):
    from tests.test_budget_readiness import END, SYMBOL, publish_calendar, publish_statements
    from tradingagents.budget_readiness import BudgetReadinessError, _financial_identity

    identity_profile(tmp_path, config, SYMBOL, "XSHG")
    publish_calendar(config)
    with pytest.raises(BudgetReadinessError):
        personal_graph.prepare(config, SYMBOL, str(END))
    publish_statements(config, _financial_identity(config, SYMBOL, None))
    with pytest.raises(ValueError, match="personal_account_policy_required"):
        personal_graph.prepare(config, SYMBOL, str(END))
    synthetic_account(config)
    ready = personal_graph.prepare(config, SYMBOL, str(END))
    assert ready["financial_cache_only"] is True and ready["llm_max_retries"] == 0
    assert ready["analysis_max_calls"] <= 60 and ready["analysis_timeout_seconds"] <= 1200
    assert ready["evidence_gate_mode"] == "enforce"


@pytest.mark.parametrize("symbol,venue", [("600895.SS", "XSHG"), ("000001.SZ", "XSHE")])
def test_real_personal_graph_to_chinese_report(
    tmp_path, monkeypatch, capsys, config, symbol, venue
):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network or child-process attempt")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from types import SimpleNamespace

    from langchain_core.callbacks import BaseCallbackHandler

    from tests.test_budget_readiness import END, START, publish_statements
    from tests.test_decision_graph_v2 import HORIZON, _FixtureBoundaryLLM, _permitted_policy_case
    from tradingagents import default_config
    from tradingagents.agents.analysts import sentiment_analyst as sentiment
    from tradingagents.agents.schemas import SentimentReport
    from tradingagents.analysis_budget import active_budget
    from tradingagents.budget_readiness import _financial_identity
    from tradingagents.dataflows import config as active_config
    from tradingagents.dataflows.acquisition import AcquisitionResult
    from tradingagents.graph import trading_graph

    date = str(END)
    identity_profile(tmp_path, config, symbol, venue)
    observed = datetime.fromisoformat(date + "T16:30:00+08:00")
    synthetic_market(config, symbol, venue, START, END, observed)
    publish_statements(config, _financial_identity(config, symbol, None), empty=True)
    synthetic_account(config)
    monkeypatch.setattr(default_config, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(active_config, "_config", config)
    for key in personal.TRANSPORT_OVERRIDES:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    policy, _, selection, evidence = _permitted_policy_case(
        symbol=symbol, venue=venue, currency="CNY", trade_date=date
    )

    class Model(_FixtureBoundaryLLM):
        def invoke(self, prompt):
            from langchain_core.messages import AIMessage

            return AIMessage(content="合成研究讨论，仅用于本地回归。")

        def bind_tools(self, tools):
            from langchain_core.messages import AIMessage
            from langchain_core.runnables import RunnableLambda

            return RunnableLambda(
                lambda _: AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "AnalystEvidenceReport",
                            "id": "synthetic-analyst",
                            "type": "tool_call",
                            "args": {
                                "report_markdown": "合成分析记录：已标明资料覆盖范围。",
                                "material_claims": [],
                            },
                        }
                    ],
                )
            )

        def with_structured_output(self, schema, **kwargs):
            if schema is SentimentReport:
                return SimpleNamespace(
                    invoke=lambda prompt: SentimentReport(
                        overall_band="Neutral",
                        overall_score=5,
                        confidence="low",
                        narrative="合成情绪资料不足。",
                    )
                )
            return super().with_structured_output(schema, **kwargs)

    monkeypatch.setattr(
        sentiment, "acquire_news", lambda *a, **kw: AcquisitionResult(None, None, ())
    )
    visited = set()

    class Trace(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, metadata=None, **kwargs):
            visited.add((metadata or {}).get("langgraph_node"))

    class FixtureGraph(trading_graph.TradingAgentsGraph):
        def __init__(self, *, selected_analysts, config, instrument_symbol):
            assert selected_analysts == personal_graph.ANALYSTS and instrument_symbol == symbol
            assert active_budget() is not None
            super().__init__(
                selected_analysts=selected_analysts,
                config=config,
                instrument_symbol=symbol,
                decision_policy=policy,
                decision_horizon=HORIZON,
            )
            self.graph = self.graph.with_config(callbacks=[Trace()])

        def resolve_evidence_state(self, ticker, trade_date):
            assert (ticker, trade_date) == (symbol, date)
            return evidence

    monkeypatch.setattr(
        trading_graph,
        "create_llm_client",
        lambda **kw: SimpleNamespace(get_llm=lambda: Model(selection)),
    )
    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", FixtureGraph)
    monkeypatch.setattr(personal, "ROOT", tmp_path)

    class MarketClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed.astimezone(tz)

    monkeypatch.setattr(personal, "datetime", MarketClock)
    for key in personal.KEYS:
        monkeypatch.setenv(key, "synthetic-example")
    assert personal.main(["--ticker", symbol, "--check-only"]) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["stock_close_verified"] and preflight["exchange_session"] == date
    assert personal.main(["--ticker", symbol, "--run"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["outcome"] == "trading_decision" and receipt["report_written"] is True
    assert (
        receipt["admitted_attempts"] == receipt["model_attempts"] == receipt["data_attempts"] == 0
    )
    assert {
        "Market Analyst",
        "Sentiment Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
        "Decision Gate",
    } <= visited, visited
    published = (
        tmp_path / "reports" / "personal" / receipt["run_id"] / receipt["report_relative_path"]
    )
    report = published.read_text(encoding="utf-8")
    assert symbol in report and "决" in report and "合成分析记录" in report
    assert receipt == json.loads(
        (tmp_path / "reports" / "personal" / receipt["run_id"] / "status.json").read_text(
            encoding="utf-8"
        )
    )
