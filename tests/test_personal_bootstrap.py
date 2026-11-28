"""Empty personal-account store. Every transport boundary is synthetic."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import personal_bootstrap as cold


class SyntheticFeed:
    def __init__(self, symbol):
        self.symbol = symbol.replace(".SS", ".SH")
        self.exchange = "SSE" if self.symbol.endswith(".SH") else "SZSE"
        self.instant = datetime.fromisoformat("2024-02-29T16:30:00+08:00")
        self.calls = []
        self.fault = None

    def wait(self, seconds):
        self.instant += timedelta(seconds=seconds + 0.001)

    def post(self, token, endpoint, params, fields, remaining, admit):

        assert token == "synthetic-example" and remaining() > 0
        admit()
        self.calls.append(endpoint)
        assert params.get("ts_code", self.symbol) == self.symbol
        if self.fault == endpoint:
            raise OSError("synthetic provider failure")
        columns = (
            fields.split(",") if fields else ["ts_code", "ann_date", "end_date", "report_type"]
        )
        if endpoint == "stock_basic":
            row = [
                self.symbol,
                self.symbol[:6],
                "样本",
                "制造",
                "主板",
                self.exchange,
                "CNY",
                "L",
                "20100101",
                "",
            ]
            items = [row]
            if self.fault == "wrong_identity":
                row[0] = "601328.SH"
        elif endpoint in cold.SUPPLEMENTAL_FINANCE:
            import json

            assert params == {"ts_code": self.symbol, "trade_date": "20240229"}
            if endpoint == "moneyflow_dc":
                assert fields == "ts_code,trade_date,net_amount_rate"
                day = "20240228" if self.fault == "stale_ratio" else "20240229"
                values = [self.symbol, day, -1.25]
            else:
                assert fields == "trade_date,ts_code,rzye"
                values = ["20240229", self.symbol, 321]
            return json.dumps({"code": 0, "data": {"fields": columns, "items": [values]}}).encode()
        elif endpoint == "trade_cal":
            first = datetime.strptime(params["start_date"], "%Y%m%d")
            last = datetime.strptime(params["end_date"], "%Y%m%d")
            assert last.date() == self.instant.date() and (last - first).days <= 1900
            items = []
            while first <= last:
                items.append(
                    [self.exchange, first.strftime("%Y%m%d"), str(int(first.weekday() < 5)), None]
                )
                first += timedelta(days=1)
            if self.fault == "calendar_gap":
                items.pop(-2)
        elif endpoint in {"daily", "adj_factor", "suspend_d"}:
            assert params["end_date"] == "20240229"
            items = {
                "daily": [[self.symbol, "20240229", "11", "13", "10", "12", "10"]],
                "adj_factor": [[self.symbol, "20240229", "1"]],
                "suspend_d": [],
            }[endpoint]
            if self.fault == "wrong_day" and endpoint == "daily":
                items[0][1] = "20240228"
        else:
            assert endpoint in cold.FINANCE and params["report_type"] == "1"
            assert (params["start_date"], params["end_date"]) == ("20180101", "20240229")
            row = {
                "ts_code": self.symbol,
                "ann_date": "20230420",
                "f_ann_date": "20230420",
                "end_date": "20221231",
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "1",
                "total_assets": 1000,
                "total_liab": 400,
                "total_hldr_eqy_inc_min_int": 600,
                "total_revenue": 900,
                "n_income_attr_p": 160,
                "n_cashflow_act": 160,
            }
            if self.fault == "wrong_statement_date":
                row["ann_date"] = "20240301"
            columns = list(row)
            items = [] if self.fault == "empty_statements" else [list(row.values())]
        return {
            "code": 0,
            "data": {
                "fields": columns,
                "items": items,
                "has_more": self.fault == "truncated" and endpoint == "income",
            },
        }


def feed_for(monkeypatch, symbol):
    import socket

    from scripts import personal_full
    from tradingagents import budget_readiness
    from tradingagents.dataflows import (
        current_statement_cache,
        market_snapshot,
        tushare_market_history,
    )
    from tradingagents.market_history import current_incremental

    feed = SyntheticFeed(symbol)

    def deny(*args, **kwargs):
        pytest.fail("real network in personal bootstrap test")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(
        market_snapshot, "_coordinator_now", lambda: feed.instant.astimezone(timezone.utc)
    )
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", feed.wait)
    monkeypatch.setattr(cold, "now", lambda: feed.instant)
    monkeypatch.setattr(cold, "post", feed.post)
    monkeypatch.setattr(cold.Session, "wait", lambda session, seconds: feed.wait(seconds))

    class DateType(type):
        def __instancecheck__(cls, instance):
            return isinstance(instance, datetime)

    class Clock(datetime, metaclass=DateType):
        @classmethod
        def now(cls, tz=None):
            return feed.instant.astimezone(tz)

    for module in (
        personal_full,
        budget_readiness,
        current_statement_cache,
        tushare_market_history,
        current_incremental,
    ):
        monkeypatch.setattr(module, "datetime", Clock)
    for k in personal_full.TRANSPORT_OVERRIDES:
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(k.lower(), raising=False)
    for k in personal_full.KEYS:
        monkeypatch.setenv(k, "synthetic-example")
    return feed


@pytest.mark.parametrize(
    "fault,last",
    [
        ("wrong_identity", "stock_basic"),
        ("wrong_day", "daily"),
        ("calendar_gap", "trade_cal"),
        ("truncated", "income"),
        ("balancesheet", "balancesheet"),
        ("wrong_statement_date", "balancesheet"),
    ],
)
def test_bad_input_stops_before_model(tmp_path, monkeypatch, capsys, fault, last):
    import json

    from scripts import personal_full

    feed = feed_for(monkeypatch, "000001.SZ")
    feed.fault = fault
    monkeypatch.setattr(personal_full, "ROOT", tmp_path)
    assert personal_full.main(["--ticker", "000001.SZ", "--run"]) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "preparation_failed" and not receipt["report_written"]
    assert feed.calls[-1] == last and list(dict.fromkeys(feed.calls)) == feed.calls
    assert sum(receipt["preparation_attempts"].values()) == len(feed.calls)
    assert sum(1 for e in feed.calls if e in cold.FINANCE) <= 3
    out = tmp_path / "reports/personal" / receipt["run_id"] / "status.json"
    assert json.loads(out.read_bytes()) == receipt


def test_quota_and_deadline_stop_without_extra_send(tmp_path, monkeypatch):
    import time

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.market_history.config import MarketHistoryConfig

    feed = feed_for(monkeypatch, "600895.SS")
    config = cold.profile(
        dict(DEFAULT_CONFIG, analysis_max_calls=1), tmp_path, {"TUSHARE_TOKEN": "synthetic-example"}
    )
    session = cold.Session(config, (time.time(), time.monotonic()))
    assert session.history == MarketHistoryConfig.from_mapping(config)
    with pytest.raises(cold.PreparationError, match="personal_attempt_limit"):
        cold.hydrate(config, "600895.SS", "synthetic-example", session)
    assert feed.calls == ["stock_basic"] and session.counts == {"stock_basic": 1}
    expired = cold.Session(config, (time.time() - 1800, time.monotonic() - 1800))
    with pytest.raises(cold.PreparationError, match="personal_deadline_reached"):
        cold.hydrate(config, "600895.SS", "synthetic-example", expired)
    assert not expired.counts and feed.calls == ["stock_basic"]


@pytest.mark.parametrize("symbol", ["600895.SS", "000001.SZ"])
def test_cold_then_cached_same_account(tmp_path, monkeypatch, symbol):
    import time

    from scripts import personal_graph
    from scripts.personal_full import local_session
    from tradingagents.default_config import DEFAULT_CONFIG

    feed = feed_for(monkeypatch, symbol)
    cfg = cold.profile(DEFAULT_CONFIG, tmp_path, {"TUSHARE_TOKEN": "synthetic-example"})
    session = cold.Session(cfg, (time.time(), time.monotonic()))
    day = cold.hydrate(cfg, symbol, "synthetic-example", session)
    assert str(day) == "2024-02-29"
    assert set(feed.calls) == {"stock_basic", *cold.FIELDS, *cold.FINANCE}
    assert len(feed.calls) == sum(session.counts.values()) == 8
    assert local_session(symbol, cfg, feed.instant) == (str(day), True, None)
    assert personal_graph.prepare(cfg, symbol, str(day))["financial_cache_only"]
    fresh = cold.Session(cfg, (time.time(), time.monotonic()))
    assert cold.hydrate(cfg, symbol, "synthetic-example", fresh) == day
    assert fresh.counts == {} and len(feed.calls) == 8


def install_graph_boundary(monkeypatch, symbol, trace):
    """Exercise real evidence acquisition, graph selection and export; replace only remote I/O."""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from tests.test_decision_graph_v2 import _FixtureBoundaryLLM, _permitted_policy_case
    from tradingagents import tushare_https
    from tradingagents.agents.schemas import SentimentReport
    from tradingagents.analysis_budget import admitted_transport
    from tradingagents.graph import trading_graph

    feed = feed_for(monkeypatch, symbol)

    def event(name):
        with Path(trace).open("a", encoding="utf8") as out:
            out.write(name + "\n")

    from scripts import personal_observations as observations

    def public_page(ticker, day, remaining, admit):
        assert ticker == symbol and day == "2024-02-29"
        remaining()
        admit()
        event("personal:public_news")
        from tests.test_personal_observations import page_bytes

        return page_bytes(ticker)

    monkeypatch.setattr(observations, "get_page", public_page)
    original = feed.post

    def capture(*args):
        event("prepare:" + args[1])
        return original(*args)

    monkeypatch.setattr(cold, "post", capture)

    def market_wire(url, payload, timeout, **kwargs):
        tushare_https.validate_request(url, payload, timeout)
        endpoint, params = payload["api_name"], payload["params"]
        assert endpoint in cold.FIELDS and params.get("ts_code", feed.symbol) == feed.symbol
        assert params.get("exchange", feed.exchange) == feed.exchange
        with admitted_transport("data"):
            event("graph:" + endpoint)
            if feed.fault == "missing_graph_history" and endpoint == "daily":
                return 200, json.dumps(
                    {"code": 0, "data": {"fields": payload["fields"].split(","), "items": []}}
                ).encode()
            fields = payload["fields"].split(",")
            end = datetime.strptime(params["end_date"], "%Y%m%d")
            first = datetime.strptime(params.get("start_date", "20180101"), "%Y%m%d")
            assert end.date() <= feed.instant.date()
            rows = []
            while first <= end:
                day = first.strftime("%Y%m%d")
                trading = first.weekday() < 5
                row = {
                    "trade_date": day,
                    "cal_date": day,
                    "ts_code": feed.symbol,
                    "exchange": feed.exchange,
                    "pretrade_date": None,
                    "is_open": str(int(trading)),
                    "open": "11",
                    "high": "13",
                    "low": "10",
                    "close": "12",
                    "vol": "10",
                    "adj_factor": "1",
                }
                if endpoint == "trade_cal" or (trading and endpoint != "suspend_d"):
                    rows.append([row.get(f) for f in fields])
                first += timedelta(days=1)
            rows = rows[params["offset"] : params["offset"] + params["limit"]]
            return 200, json.dumps({"code": 0, "data": {"fields": fields, "items": rows}}).encode()

    monkeypatch.setattr(tushare_https, "send_once", market_wire)
    _, _, selection, _ = _permitted_policy_case(
        symbol=symbol,
        venue="XSHG" if symbol.endswith(".SS") else "XSHE",
        currency="CNY",
        trade_date="2024-02-29",
    )

    class Model(_FixtureBoundaryLLM):
        def invoke(self, prompt):
            with admitted_transport("model"):
                event("model")
                return AIMessage(content="合成研究讨论，已注明数据覆盖。")

        def bind_tools(self, tools):
            def analyst(prompt):
                with admitted_transport("model"):
                    event("model")
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystEvidenceReport",
                                "id": "sample",
                                "type": "tool_call",
                                "args": {
                                    "report_markdown": "合成分析记录，缺项按证据标注。",
                                    "material_claims": [],
                                },
                            }
                        ],
                    )

            return RunnableLambda(analyst)

        def with_structured_output(self, schema, **kwargs):
            binding = super().with_structured_output(schema, **kwargs)

            def invoke(prompt):
                with admitted_transport("model"):
                    event("model")
                    if schema is SentimentReport:
                        return SentimentReport(
                            overall_band="Neutral",
                            overall_score=5,
                            confidence="low",
                            narrative="合成回归。",
                        )
                    return binding.invoke(prompt)

            return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(
        trading_graph,
        "create_llm_client",
        lambda **kwargs: SimpleNamespace(get_llm=lambda: Model(selection)),
    )
    return feed


@pytest.mark.parametrize("symbol", ["600895.SS", "000001.SZ"])
def test_cold_real_graph_entry(tmp_path, monkeypatch, capsys, symbol):
    import json

    from scripts import personal_full
    from tradingagents import default_config

    monkeypatch.setattr(personal_full, "ROOT", tmp_path)
    monkeypatch.setattr(
        default_config, "DEFAULT_CONFIG", dict(default_config.DEFAULT_CONFIG, analysis_max_calls=60)
    )
    trace = tmp_path / "synthetic-transport.txt"
    install_graph_boundary(monkeypatch, symbol, trace)
    from scripts import personal_observations

    def denied_news(ticker, day, remaining, admit):
        admit()
        with trace.open("a", encoding="utf8") as stream:
            stream.write("personal:news_denied\n")
        raise PermissionError

    monkeypatch.setattr(personal_observations, "get_page", denied_news)
    assert personal_full.main(["--ticker", symbol, "--check-only"]) == 2
    assert not trace.exists() and not (tmp_path / "reports").exists()
    capsys.readouterr()
    options = ["--basic"] if symbol.endswith(".SZ") else []
    code = personal_full.main(["--ticker", symbol, "--run", *options])
    receipt = json.loads(capsys.readouterr().out)
    if options:
        assert set(receipt["enhancements"].values()) == {"not_selected"}
        assert "personal:news_denied" not in trace.read_text()
    else:
        assert receipt["enhancements"]["news"] == "permission_denied"
        assert trace.read_text().count("personal:news_denied") == 1
    assert code in (0, 3) and receipt["report_written"], (receipt, trace.read_text())
    assert receipt["exchange_session"] == "2024-02-29" and receipt["ticker"] == symbol
    events = trace.read_text().splitlines()
    assert receipt["financial_attempts"] == 3
    assert 0 < receipt["model_attempts"] == events.count("model")
    assert len(events) == receipt["admitted_attempts"] <= 60
    reportdir = tmp_path / "reports" / "personal" / receipt["run_id"]
    assert json.loads((reportdir / "status.json").read_bytes()) == receipt
    report = (reportdir / receipt["report_relative_path"]).read_text("utf8")
    assert symbol in report and "合成" in report


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows PowerShell launcher")
@pytest.mark.parametrize("ticker", ["600895.SH", "000001.SZ"])
def test_windows_launcher_from_empty_personal_directory(tmp_path, ticker):
    import json
    import os
    import shutil
    import subprocess
    import venv
    from pathlib import Path

    pwsh = shutil.which("pwsh")
    assert pwsh, "PowerShell 7 required for the Windows launcher test"
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "empty personal project"
    (project / "scripts").mkdir(parents=True)
    for name in ("start_personal_research.ps1", "scripts/personal_full.py"):
        shutil.copyfile(source / name, project / name)
    # Only this disposable interpreter receives the test transport at startup.
    runtime = tmp_path / "test-runtime"
    venv.EnvBuilder(with_pip=False).create(runtime)
    packages = runtime / "Lib/site-packages"
    deps = Path(pytest.__file__).resolve().parent.parent
    trace = tmp_path / "test-attempts"
    hook = f"""
import os, sys, socket
def offline(*args, **kwargs):
    raise AssertionError("unexpected network")
socket.getaddrinfo = offline
socket.socket.connect = offline
try:
    assert sys.prefix == {str(runtime)!r}
    if os.environ.get("PERSONAL_FIXTURE_STALL") == "1":
        import time
        time.sleep(10)
        os._exit(72)
    sys.path[:0] = [{str(source)!r},{str(deps)!r}]
    from pathlib import Path
    from datetime import timedelta
    import pytest
    from tests.test_personal_bootstrap import install_graph_boundary
    changes = pytest.MonkeyPatch()
    feed = install_graph_boundary(changes, {ticker.replace(".SH", ".SS")!r}, Path({str(trace)!r}))
    if Path({str(trace)!r}).exists():
        feed.instant += timedelta(minutes=5)
    if os.environ.get("PERSONAL_FIXTURE_EXPIRED_MARGIN") == "1":
        from tests.test_personal_margin_publication import install_expired_margin_fixture
        install_expired_margin_fixture(changes, feed)
    if os.environ.get("PERSONAL_FIXTURE_EARLY_MORNING") == "1":
        feed.instant += timedelta(hours=15, minutes=30)
    if os.environ.get("PERSONAL_FIXTURE_NEXT_MORNING") == "1":
        feed.instant += timedelta(hours=17)
        if feed.exchange == "SZSE":
            feed.fault = "stale_ratio"
except BaseException:
    os._exit(71)
"""
    (packages / "personal_test_boundary.py").write_text(hook, encoding="utf8")
    (packages / "personal-test-only.pth").write_text(
        "import personal_test_boundary\n", encoding="ascii"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
            "PATH",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
        }
    }
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        USERPROFILE=str(home),
        HOME=str(home),
        VIRTUAL_ENV=str(runtime),
        PYTHON_DOTENV_DISABLED="1",
        TRADINGAGENTS_PROJECT_ROOT=str(project),
        TUSHARE_TOKEN="synthetic-example",
        DEEPSEEK_API_KEY="synthetic-example",
    )

    def launch(flag, *options):
        proc = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(project / "start_personal_research.ps1"),
                "-Ticker",
                ticker,
                flag,
                *options,
            ],
            cwd=project,
            env=env,
            capture_output=True,
            timeout=120,
        )
        assert proc.stdout, (proc.returncode, proc.stderr[-300:])
        return proc.returncode, json.loads(proc.stdout)

    for flag, options, expected in [
        ("-Run", ["-TusharePoints", "2000"], "below_minimum"),
        ("-CheckOnly", [], "undeclared"),
    ]:
        code, blocked = launch(flag, "-FetchEastmoneyRatio", *options)
        assert code == 2 and blocked["error"] == "moneyflow_dc_points_requirement"
        assert blocked["moneyflow_dc_access"] == {
            "endpoint": "moneyflow_dc",
            "minimum_points": 5000,
            "points_status": expected,
            "entitlement_checked": False,
        }
        assert (
            blocked["admitted_attempts"]
            == blocked["financial_attempts"]
            == blocked["model_attempts"]
            == 0
        )
        assert (
            not blocked["report_written"]
            and not (project / "reports").exists()
            and not trace.exists()
        )
    code, check = launch("-CheckOnly", "-TusharePoints", "2000")
    assert (
        check["moneyflow_dc_access"]["points_status"] == "below_minimum"
        and check["ratio_policy"] == "cache_only"
    )
    assert code == 2 and check["cache_issue"] == "market_cache_missing"
    assert not (project / "reports").exists() and not trace.exists()
    # Low-points research still exercises the normal graph. Keep the no-option path on Shenzhen.
    options = ("-TusharePoints", "2000") if ticker.endswith(".SH") else ()
    # Shanghai starts the following morning with an empty account cache.
    retrospective = ticker.endswith(".SH")
    if retrospective:
        env["PERSONAL_FIXTURE_NEXT_MORNING"] = "1"
        env["PERSONAL_FIXTURE_EXPIRED_MARGIN"] = "1"
    code, receipt = launch("-Run", *options)
    env.pop("PERSONAL_FIXTURE_EXPIRED_MARGIN", None)
    assert receipt["moneyflow_dc_access"]["points_status"] == (
        "below_minimum" if options else "undeclared"
    )
    assert code in {0, 3} and receipt["status"] == "completed" and receipt["report_written"], (
        receipt
    )
    assert receipt["exchange_session"] == "2024-02-29" and receipt["ticker"] == ticker.replace(
        ".SH", ".SS"
    )
    assert code == 0 and receipt["rating_status"] == "validated_decision"
    assert receipt["full_data_acceptance"] == "unverified" and receipt["evidence_preset"] == "full"
    assert receipt["enhancements"] == {
        "news": "verified",
        "margin_balance": "verified" if retrospective else "publication_pending",
        "eastmoney_fundflow_ratio": "cache_missing_or_unverified" if retrospective else "publication_pending",
    }
    assert len(receipt["source_evidence"]["news"]["articles"]) == 1
    assert (
        receipt["verified_inputs"]["stock_identity"]
        == receipt["verified_inputs"]["stock_close"]
        == "verified"
    )
    assert all(
        receipt["verified_inputs"]["financial." + tool]
        == "validated_windows_runtime_period_selection_required"
        for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow")
    )
    counts = trace.read_text().splitlines()
    assert len(counts) == receipt["admitted_attempts"] <= 60
    assert counts.count("model") == receipt["model_attempts"] > 0
    assert receipt["financial_attempt_limit"] == 4
    assert receipt["financial_reserved_endpoint"] == "margin_detail"
    assert receipt["financial_attempts"] == (4 if retrospective else 3)
    assert all(counts.count("prepare:" + endpoint) == 1 for endpoint in cold.FINANCE)
    assert counts.count("prepare:margin_detail") == int(retrospective)
    assert "prepare:moneyflow_dc" not in counts
    out = project / "reports/personal" / receipt["run_id"]
    assert json.loads((out / "status.json").read_bytes()) == receipt
    markdown = (out / receipt["report_relative_path"]).read_text("utf8")
    assert all(
        word in markdown
        for word in (
            "覆盖",
            "证据",
            "合成",
            "市场分析师",
            "新闻分析师",
            "情绪分析师",
            "基本面分析师",
            "多头研究员",
            "空头研究员",
            "交易员",
            "风险分析师",
        )
    )
    code, warm = launch("-CheckOnly")
    assert (
        code == 0
        and warm["stock_close_verified"]
        and warm["exchange_session"] == receipt["exchange_session"]
    ), warm
    assert trace.read_text().splitlines() == counts
    code, basic = launch("-CheckOnly", "-Basic")
    assert code == 0 and basic["evidence_preset"] == "basic"
    assert set(basic["enhancements"].values()) == {"not_selected"}
    assert trace.read_text().splitlines() == counts
    if not retrospective:
        env["PERSONAL_FIXTURE_EARLY_MORNING"] = "1"
        code, early = launch("-Run", "-TusharePoints", "2000")
        del env["PERSONAL_FIXTURE_EARLY_MORNING"]
        assert code in (0, 3) and early["exchange_session"] == receipt["exchange_session"]
        assert early["enhancements"]["margin_balance"] == "publication_pending"
        assert early["financial_attempts"] == 0
        after_early = trace.read_text().splitlines()
        assert "prepare:margin_detail" not in after_early[len(counts):]
        assert early["admitted_attempts"] == len(after_early) - len(counts) <= 60
        counts = after_early
    env["PERSONAL_FIXTURE_NEXT_MORNING"] = "1"
    code, next_day = launch("-Run", "-FetchEastmoneyRatio", "-TusharePoints", "5000")
    assert next_day["moneyflow_dc_access"]["entitlement_checked"] is False
    assert code in (0, 3) and next_day["report_written"], next_day
    assert next_day["exchange_session"] == "2024-02-29"
    assert next_day["financial_attempts"] == (1 if retrospective else 2)
    assert next_day["supplement_attempts"].get("margin_detail", 0) == int(not retrospective)
    assert next_day["supplement_attempts"].get("moneyflow_dc") == 1
    assert all(name not in next_day["preparation_attempts"] for name in cold.FINANCE)
    assert (
        next_day["enhancements"]["news"] == next_day["enhancements"]["margin_balance"] == "verified"
    )
    assert not next_day["news_content_verified"]
    ratio = next_day["source_evidence"]["eastmoney_fundflow_ratio"]
    if ticker.endswith(".SH"):
        assert (
            next_day["enhancement_coverage"] == "all_three_verified"
            and ratio["status"] == "verified"
        )
        assert ratio["main_net_percent"] == "-1.25" and ratio["trade_date"] == "2024-02-29"
        assert ratio["source"] == "eastmoney.via_tushare.moneyflow_dc"
    else:
        assert ratio["status"] == "date_mismatch" and "main_net_percent" not in ratio
        assert next_day["enhancement_coverage"] == "incomplete"
    later = trace.read_text().splitlines()
    assert next_day["admitted_attempts"] == len(later) - len(counts) <= 60
    counts = later
    if retrospective:
        code, cached = launch("-Run", "-TusharePoints", "2000")
        assert code in (0, 3) and cached["financial_attempts"] == 0
        assert cached["source_evidence"]["margin_balance"]["acquisition"] == "coordinator_cache"
        after_cache = trace.read_text().splitlines()
        added = after_cache[len(counts):]
        assert cached["admitted_attempts"] == len(added) <= 60
        assert set(added).isdisjoint("prepare:" + e for e in (*cold.FINANCE, *cold.SUPPLEMENTAL_FINANCE))
        counts = after_cache
    env.update(TRADINGAGENTS_ANALYSIS_TIMEOUT_SECONDS="1", PERSONAL_FIXTURE_STALL="1")
    stopped = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(project / "start_personal_research.ps1"),
            "-Ticker",
            ticker,
            "-Run",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        timeout=9,
    )
    assert stopped.returncode == 124 and b"personal_deadline_exceeded" in stopped.stderr
    assert trace.read_text().splitlines() == counts


@pytest.mark.parametrize("status", [200, 302, 403, 429])
def test_real_transport_fixed_origin_without_retry(monkeypatch, status):
    import io
    import json
    import ssl
    from types import SimpleNamespace

    from tradingagents.market_history import coordinator

    calls = []
    body = io.BytesIO(b'{"code":0,"data":{"fields":["symbol"],"items":[]}}')

    class Connection:
        def __init__(self, host, timeout, context):
            assert host == "api.waditu.com" and 0 < timeout <= 2
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
            self.sock = SimpleNamespace(settimeout=lambda t: None)

        def request(self, method, path, *, body, headers):
            assert method == "POST" and path == "/dataapi/trade_cal"
            data = json.loads(body)
            assert (
                data["params"]["ts_type_name"] == cold.BASE and data["token"] == "synthetic-example"
            )
            calls.append("post")

        def getresponse(self):
            return SimpleNamespace(status=status, read1=body.read, getheader=lambda *args: "2")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(cold.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(
        coordinator, "record_active_physical_attempt_io", lambda: calls.append("record")
    )

    def send():
        return cold.post(
            "synthetic-example",
            "trade_cal",
            {"exchange": "SSE"},
            cold.FIELDS["trade_cal"],
            lambda: 2,
            coordinator.record_active_physical_attempt_io,
        )

    if status == 200:
        assert send()["code"] == 0
    else:
        with pytest.raises((cold.PreparationError, coordinator.PhysicalAttemptFailure)):
            send()
    assert calls == ["record", "post", "closed"]


def test_request_scope_before_reservation(tmp_path):
    import time
    from datetime import date

    from tradingagents.default_config import DEFAULT_CONFIG

    config = cold.profile(DEFAULT_CONFIG, tmp_path, {"TUSHARE_TOKEN": "synthetic-example"})
    s = cold.Session(config, (time.time(), time.monotonic()))
    s.symbol = "600895.SS"
    s.today = s.day = date(2024, 2, 29)
    original = {
        "ts_code": "600895.SH",
        "start_date": "20240201",
        "end_date": "20240229",
        "report_type": "1",
    }
    for params, fields in [
        (dict(original, ts_code="000001.SZ"), ""),
        (dict(original, end_date="20240301"), ""),
        (dict(original, start_date="20100101"), ""),
        (original, "all"),
    ]:
        with pytest.raises(cold.PreparationError, match="scope_mismatch"):
            s.query("synthetic-example", "cashflow", params, fields)
    assert s.counts == {} and not (tmp_path / "reports").exists()


def test_next_morning_refreshes_calendar_and_keeps_previous_close(tmp_path, monkeypatch):
    import time

    from tradingagents.default_config import DEFAULT_CONFIG

    feed = feed_for(monkeypatch, "600895.SS")
    cfg = cold.profile(DEFAULT_CONFIG, tmp_path, {"TUSHARE_TOKEN": "synthetic-example"})

    def acquire():
        session = cold.Session(cfg, (time.time(), time.monotonic()))
        return cold.hydrate(cfg, "600895.SS", "synthetic-example", session), session.counts

    first, _ = acquire()
    feed.instant = datetime.fromisoformat("2024-03-01T10:00:00+08:00")
    day, counts = acquire()
    assert day == first and counts == {"trade_cal": 1}


def test_graph_insufficient_history_has_no_rating(tmp_path, monkeypatch, capsys):
    import json

    from scripts import personal_full

    monkeypatch.setattr(personal_full, "ROOT", tmp_path)
    feed = install_graph_boundary(monkeypatch, "600895.SS", tmp_path / "simulated-attempts")
    feed.fault = "missing_graph_history"
    code = personal_full.main(["--ticker", "600895.SH", "--run"])
    receipt = json.loads(capsys.readouterr().out)
    assert code == 3 and receipt["outcome"] == "analysis_outcome", receipt
    assert receipt["rating_status"] == "no_rating" and receipt["report_written"]
    assert receipt["full_data_acceptance"] == "unverified" and receipt["financial_attempts"] == 3


def test_graph_transport_rejects_other_stock_and_future_dates(monkeypatch):
    from scripts import personal_graph
    from tradingagents import tushare_https
    from tradingagents.picker.pit_models import Dataset
    from tradingagents.picker.tushare_provider import ENDPOINTS

    sent = []
    monkeypatch.setattr(tushare_https, "send_once", lambda *a: sent.append("admitted"))
    base = {
        "ts_code": "600895.SH",
        "ts_type_name": cold.BASE,
        "start_date": "20240201",
        "end_date": "20240229",
        "limit": 5000,
        "offset": 0,
    }

    def invoke(params):
        return tushare_https.send_once(
            cold.BASE + "/daily",
            {
                "api_name": "daily",
                "token": "synthetic-example",
                "params": params,
                "fields": ENDPOINTS[Dataset.DAILY].fields,
            },
            20,
        )

    with personal_graph.personal_sources("600895.SS", "2024-02-29"):
        for change in (
            {"ts_code": "000001.SZ"},
            {"end_date": "20240301"},
            {"start_date": "20100101"},
            {"exchange": "SZSE"},
        ):
            with pytest.raises(ValueError, match="scope_mismatch"):
                invoke(dict(base, **change))
        assert not sent
        invoke(base)
        assert sent == ["admitted"]


def test_empty_financial_response_remains_empty(tmp_path, monkeypatch):
    import time

    from scripts import personal_graph
    from tradingagents.default_config import DEFAULT_CONFIG

    feed = feed_for(monkeypatch, "000001.SZ")
    feed.fault = "empty_statements"
    cfg = cold.profile(DEFAULT_CONFIG, tmp_path, {"TUSHARE_TOKEN": "synthetic-example"})
    session = cold.Session(cfg, (time.time(), time.monotonic()))
    day = cold.hydrate(cfg, "000001.SZ", "synthetic-example", session)
    checked = personal_graph.prepare(cfg, "000001.SZ", str(day))["personal_verified_inputs"]
    assert [v for k, v in checked.items() if k.startswith("financial.")] == [
        "validated_empty_response"
    ] * 3
    assert sum(session.counts[e] for e in cold.FINANCE) == 3


def test_setup_failure_before_physical_send_is_zero(tmp_path, monkeypatch, capsys):
    import json

    from scripts import personal_full

    feed = feed_for(monkeypatch, "600895.SS")

    def setup_failed(*args):
        raise OSError("synthetic local setup failure")

    monkeypatch.setattr(cold, "post", setup_failed)
    monkeypatch.setattr(personal_full, "ROOT", tmp_path)
    assert personal_full.main(["--ticker", "600895.SH", "--run"]) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert (
        receipt["stage"] == "stock_basic" and receipt["admitted_attempts"] == 0 and not feed.calls
    )
    assert (
        receipt["rating_status"] == "unavailable"
        and receipt["full_data_acceptance"] == "unverified"
    )
