"""Synthetic single-date balances. All transport/model boundaries are fake."""
import copy
import io
import json
from datetime import datetime, timezone
from itertools import permutations
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.test_bounded_moneyflow import Connection, Response
from tests.test_multiyear_trend import ASOF, synthetic_evidence
from tradingagents import analysis_budget as budgets, margin_balance as margin, moneyflow as mf


@pytest.fixture
def budget(monkeypatch):
    result = budgets.AnalysisBudget(60, 1200, allowed_data_vendors={"tushare", "akshare"})
    monkeypatch.setattr(budgets, "_ACTIVE", result)
    return result


def state():
    evidence = synthetic_evidence()
    return {"company_of_interest": evidence.instrument_identity.symbol, "trade_date": ASOF,
            "evidence_state": evidence.model_dump(mode="json")}


def own_snapshot(monkeypatch, value):
    from tradingagents.evidence import EvidenceState
    ev = EvidenceState.model_validate(value["evidence_state"])
    snap = ev.market_snapshot
    artifact = next(a for a in ev.source_artifacts if a.artifact_sha256 == snap.frame_sha256)
    frame = pd.read_csv(io.StringIO(artifact.raw_text))
    current = SimpleNamespace(**{k: getattr(snap, k) for k in (
        "symbol", "provider", "requested_date", "effective_trading_date", "snapshot_id",
        "snapshot_id_version", "snapshot_manifest_json", "frame_sha256")}, frame=frame)
    monkeypatch.setattr("tradingagents.dataflows.market_snapshot.get_active_authoritative_market_snapshot",
                        lambda symbol, end: current if (symbol, end) == (snap.symbol, snap.requested_date) else None)
    return current


def binding(value=None):
    value = value or state()
    return margin._binding(value, value["company_of_interest"], value["trade_date"],
                           value["evidence_state"]["market_snapshot"]["effective_trading_date"])


def body(rows=None, fields=margin.FIELDS, **extra):
    return json.dumps({"code": 0, "msg": "synthetic-secret-discarded", "data": {"fields": fields,
        "items": [["20261113", "600895.SH", 12345.25]] if rows is None else rows}, **extra}).encode()


@pytest.mark.parametrize("fields", list(permutations(margin.FIELDS)))
def test_exact_fields_by_name(fields):
    cells = {"trade_date": "20261113", "ts_code": "600895.SH", "rzye": 12345.25}
    assert margin.decode(body([[cells[k] for k in fields]], fields), binding()) == [[ASOF, "12345.25"]]


@pytest.mark.parametrize("row,reason", [
    (["20261112", "600895.SH", 1], "date_window"),
    (["20261114", "600895.SH", 1], "date_window"),
    (["20260230", "600895.SH", 1], "date_invalid"),
    ([20261113, "600895.SH", 1], "date_format"),
    (["20261113", "000001.SZ", 1], "stock_mismatch"),
    (["20261113", "600895.SH\r\nCookie: secret", 1], "stock_mismatch"),
    (["20261113", "600895.SH", -1], "amount_range"),
    (["20261113", "600895.SH", None], "amount_missing"),
    (["20261113", "600895.SH", "secret"], "amount_type"),
    (["20261113", "600895.SH", True], "amount_type"),
    (["20261113", "600895.SH", 1e15], "amount_range"),
    (["20261113", "600895.SH", 0.0000001], "amount_precision"),
])
def test_bad_rows_rejected_without_content(row, reason):
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        margin.decode(body([row]), binding())
    assert caught.value.rejection == reason
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("raw,reason", [
    (body(fields=["trade_date", "ts_code", "rqye"]), "fields_set"),
    (body(fields=["trade_date", "ts_code", "ts_code"]), "fields_duplicate"),
    (body(rows=[["20261113", "600895.SH", 1]] * 2), "row_limit"),
    (body(fields=[*margin.FIELDS, "synthetic_secret"]), "row_shape"),
    (body(rows=[["20261113", "600895.SH", float("inf")]]), "json_nonfinite"),
])
def test_schema_and_duplicate_rejection(raw, reason):
    with pytest.raises(mf.MoneyflowUnavailable) as caught:
        margin.decode(raw, binding())
    assert caught.value.rejection == reason


def test_zero_and_empty_are_distinct():
    assert margin.decode(body([["20261113", "600895.SH", 0]]), binding()) == [[ASOF, "0"]]
    assert margin.decode(body([]), binding()) == []


def test_fixed_https_post_and_existing_clients_not_expanded(monkeypatch, budget):
    from tradingagents import tushare_financial_https, tushare_https
    monkeypatch.setattr("tradingagents.market_history.coordinator.record_active_physical_attempt_io", lambda: None)
    result = Response(body())
    connection = Connection(result)
    raw = margin._send(binding(), "synthetic-token", connection_factory=lambda host, **kw: connection)
    assert margin.decode(raw, binding()) == [[ASOF, "12345.25"]]
    assert len(connection.calls) == result.closed == connection.closed == budget.receipt()["data_attempts"] == 1
    args, kwargs = connection.calls[0]
    assert args == ("POST", margin.PATH)
    sent = json.loads(kwargs["body"])
    assert sent["api_name"] == "margin_detail" and sent["fields"] == "trade_date,ts_code,rzye"
    assert sent["params"] == {"ts_code": "600895.SH", "trade_date": "20261113",
                              "ts_type_name": "https://api.waditu.com/dataapi"}
    assert set(kwargs["headers"]) == {"Content-Type", "Connection"}
    assert "margin_detail" not in tushare_https.ENDPOINTS
    assert {"income", "balancesheet", "cashflow"} == tushare_financial_https.ENDPOINTS


@pytest.mark.parametrize("change", [
    lambda b: b.update(extra="secret"), lambda b: b.update(symbol="600895.SH\r\nHost: secret"),
    lambda b: b.update(trade_date="2026-11-14"), lambda b: b.update(snapshot_id="unbound"),
])
def test_invalid_request_never_sends(change, budget):
    b = binding()
    change(b)
    with pytest.raises(ValueError):
        margin._send(b, "synthetic-token", connection_factory=lambda *a, **k: pytest.fail("send forbidden"))
    assert budget.receipt()["data_attempts"] == 0


@pytest.mark.parametrize("status", [302, 403, 429])
def test_redirect_and_errors_never_retry(monkeypatch, budget, status):
    monkeypatch.setattr("tradingagents.market_history.coordinator.record_active_physical_attempt_io", lambda: None)
    result = Response(b"synthetic-secret", status=status)
    connection = Connection(result)
    with pytest.raises(mf.MoneyflowUnavailable):
        margin._send(binding(), "synthetic-token", connection_factory=lambda *a, **k: connection)
    assert result.reads == 0 and result.closed == connection.closed == len(connection.calls) == 1


@pytest.mark.parametrize("mode", ["cancel", "timeout", "os", "oversize"])
def test_shared_stop_limits_and_first_error(monkeypatch, budget, mode):
    monkeypatch.setattr("tradingagents.market_history.coordinator.record_active_physical_attempt_io", lambda: None)
    result = Response(b"x" * (mf.MAX_BYTES + 1), cleanup=True)
    connection = Connection(result)
    error = {"cancel": budgets.AnalysisBudgetStopped("synthetic-secret"),
             "timeout": TimeoutError("synthetic-secret"), "os": OSError(9, "synthetic-secret")}.get(mode)
    if error:
        def read(size):
            raise error
        result.read1 = read
    with pytest.raises(budgets.AnalysisBudgetStopped if mode == "cancel" else mf.MoneyflowUnavailable) as caught:
        margin._send(binding(), "synthetic-token", connection_factory=lambda *a, **k: connection)
    diag = caught.value.moneyflow_diagnostic if mode == "cancel" else caught.value.diagnostic
    assert diag["stage"] == ("response_limits" if mode == "oversize" else "body_read")
    assert diag["cleanup"][0]["resource"] == "response"
    assert "synthetic-secret" not in json.dumps(diag)
    assert result.closed == connection.closed == len(connection.calls) == 1


def configured(tmp_path):
    from tests.test_current_tushare_statements import runtime_config
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.store import MarketHistoryStore
    cfg = runtime_config(tmp_path)
    cfg.update(bounded_margin_date=ASOF, tushare_calls_per_minute=40,
               tushare_account_scope_label="personal-2000", tushare_operator_safety_ceiling_calls_per_minute=40,
               tushare_financial_https_endpoint="https://api.waditu.com/dataapi")
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(cfg)):
        pass
    return cfg


@pytest.mark.parametrize("mode", ["available", "zero", "empty", "permission", "invalid", "cancel"])
def test_real_coordinator_once_receipt_and_no_body_retained(tmp_path, monkeypatch, budget, mode):
    from tradingagents.market_history.coordinator import record_active_physical_attempt_io
    cfg = configured(tmp_path)
    current = state()
    own_snapshot(monkeypatch, current)
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-token")
    calls = []
    def send(b, token):
        with budgets.admitted_transport("data"):
            record_active_physical_attempt_io()
            calls.append(b)
            if mode == "cancel":
                raise budgets.AnalysisBudgetStopped("synthetic cancel")
            if mode == "permission":
                return b'{"code":2002,"msg":"synthetic-secret"}'
            if mode == "invalid":
                return body(fields=["trade_date", "ts_code", "synthetic_secret"])
            return body([] if mode == "empty" else [["20261113", "600895.SH", 0 if mode == "zero" else 12345.25]])
    monkeypatch.setattr(margin, "_send", send)
    with margin.margin_balance_scope(cfg, ticker="600895.SS", as_of=ASOF, trade_date=ASOF):
        if mode == "cancel":
            with pytest.raises(budgets.AnalysisBudgetStopped):
                margin.margin_balance_for_state(current)
            with pytest.raises(budgets.AnalysisBudgetStopped, match="already_started"):
                margin.margin_balance_for_state(current)
        else:
            result = margin.margin_balance_for_state(current)
            assert margin.margin_balance_for_state(current) == result
            expected = {"zero": "available", "permission": "permission_denied", "invalid": "invalid_response"}.get(mode, mode)
            assert result["status"] == expected
            text = margin.margin_balance_markdown({**current, "margin_balance_observation": result})
            assert "retrospective_backfill" in text and "不改变评级" in text
            assert "synthetic-secret" not in json.dumps(result) and "synthetic-token" not in json.dumps(result)
            if mode == "zero":
                assert "0 元人民币" in text
            elif mode in {"empty", "permission", "invalid"}:
                assert "未取得合格" in text and not result["rows"]
            if mode == "invalid":
                assert result["diagnostic"]["rejection"] == "fields_set"
    assert len(calls) == budget.receipt()["data_attempts"] == 1
    assert budget.receipt()["model_attempts"] == 0


@pytest.mark.parametrize("mode", ["no_active", "changed_frame", "no_source", "wrong_date", "wrong_stock"])
def test_bad_binding_does_not_read_token_or_send(monkeypatch, budget, mode):
    current = state()
    owned = own_snapshot(monkeypatch, current)
    if mode == "no_active":
        monkeypatch.setattr("tradingagents.dataflows.market_snapshot.get_active_authoritative_market_snapshot", lambda *a: None)
    elif mode == "changed_frame":
        owned.frame.loc[0, "Close"] = 999
    elif mode == "no_source":
        current["evidence_state"]["acquisition_outcomes"] = []
    elif mode == "wrong_stock":
        current["company_of_interest"] = "000001.SZ"
    def forbidden(*a, **k):
        pytest.fail("invalid binding must not reach coordinator/token")
    monkeypatch.setattr(mf, "_coordinate", forbidden)
    with margin.margin_balance_scope({}, ticker="600895.SS", as_of=ASOF,
                                     trade_date="2026-11-12" if mode == "wrong_date" else ASOF):
        result = margin.margin_balance_for_state(current)
    assert result["status"] == "binding_unavailable"
    assert "未发起查询" in margin.margin_balance_markdown({**current, "margin_balance_observation": result})
    assert budget.receipt()["data_attempts"] == 0


def test_interface_opt_ins_do_not_authorize_each_other(tmp_path, monkeypatch, budget):
    cfg = configured(tmp_path)
    original = mf.os.environ.get
    def no_token(key, default=None):
        if key == "TUSHARE_TOKEN":
            pytest.fail("disabled interface read token")
        return original(key, default)
    monkeypatch.setattr(mf.os.environ, "get", no_token)
    with pytest.raises(mf.MoneyflowUnavailable, match="not_configured"):
        mf._coordinated(cfg, mf._binding(state(), "600895.SS", ASOF))
    cfg.update(bounded_moneyflow_enabled=True, bounded_margin_date=None)
    with pytest.raises(mf.MoneyflowUnavailable, match="not_configured"):
        mf._coordinate(cfg, binding(), api="margin_detail")
    with pytest.raises(mf.MoneyflowUnavailable, match="not_configured"):
        mf._coordinate(cfg, binding(), api="other")


def test_explicit_cli_and_checkpoint_bind_date_without_changing_default():
    from tests.test_bounded_mainland_entry import entry
    module = entry()
    args = ["--ticker", "600895.SH", "--analysis-date", ASOF]
    assert module.parse_arguments(args).margin_date is None
    assert module.parse_arguments([*args, "--margin-date", ASOF]).margin_date == ASOF
    for value in ["20261113", "2026-02-30", "2026-11-14", "", "synthetic-secret"]:
        with pytest.raises(module.Stop):
            module.parse_arguments([*args, "--margin-date", value])
    with pytest.raises(module.Stop):
        module.parse_arguments(["--margin-date", ASOF])
    assert budgets.checkpoint_budget_binding({}, "graph") == budgets.checkpoint_budget_binding({"bounded_margin_date": None}, "graph")
    assert budgets.checkpoint_budget_binding({"bounded_margin_date": ASOF}, "graph") != budgets.checkpoint_budget_binding({"bounded_margin_date": "2026-11-12"}, "graph")
    assert module.FIXED["analysis_max_calls"] == 60


def test_formal_session_opens_and_restores_exact_optional_scope(tmp_path):
    from tests.test_bounded_mainland_entry import entry
    module = entry()
    session = module.Session(tmp_path, margin_date=ASOF)
    session.selection = {"ticker": "600895.SS", "analysis_date": ASOF}
    cfg = session.configure({})
    assert cfg["bounded_margin_date"] == ASOF and cfg["bounded_moneyflow_enabled"] is False
    cli = SimpleNamespace(DEFAULT_CONFIG={}, get_user_selections=lambda: None,
                          _build_run_config=lambda: {}, _prepare_run_artifacts=lambda: {})
    original = cli.get_user_selections
    assert margin._SCOPE.get() is None
    with session.analysis_hooks(cli, cfg, {}):
        selected = margin._SCOPE.get()
        assert (selected.ticker, selected.as_of, selected.trade_date) == ("600895.SS", ASOF, ASOF)
        assert cli.get_user_selections() == session.selection
    assert margin._SCOPE.get() is None and cli.get_user_selections is original


def test_exhausted_shared_budget_stops_before_connection(monkeypatch, budget):
    budget.max_calls = 1
    with budgets.admitted_transport("data"):
        pass
    connection = Connection(Response(body()))
    monkeypatch.setattr("tradingagents.market_history.coordinator.record_active_physical_attempt_io", lambda: None)
    with pytest.raises(budgets.AnalysisBudgetStopped):
        margin._send(binding(), "synthetic-token", connection_factory=lambda *a, **k: connection)
    assert not connection.calls and budget.receipt()["data_attempts"] == 1


@pytest.mark.parametrize("symbol", ["600919.SH", "600895.SS", "000001.SZ"])
def test_supported_stock_identity_without_sample_specific_constants(symbol):
    from hashlib import sha256
    current = state()
    ev = current["evidence_state"]
    ev["instrument_identity"]["symbol"] = symbol
    ev["instrument_identity"]["venue"] = "XSHE" if symbol.endswith(".SZ") else "XSHG"
    snap = ev["market_snapshot"]
    snap["symbol"] = symbol
    manifest = json.loads(snap["snapshot_manifest_json"])
    manifest["instrument"].update(canonical_symbol=symbol, reference_market=ev["instrument_identity"]["venue"])
    text = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = sha256(text.encode()).hexdigest()
    snap.update(snapshot_manifest_json=text, snapshot_id="snapshot:v2:" + digest, pin_membership_digest=digest)
    current["company_of_interest"] = symbol
    selected = binding(current)
    assert selected["symbol"] == (symbol[:-3] + ".SH" if symbol.endswith(".SS") else symbol)
    assert margin.decode(body([["20261113", selected["symbol"], 0]]), selected) == [[ASOF, "0"]]


def test_both_enhancements_share_one_total_budget_and_cannot_resume(monkeypatch, budget):
    current = state()
    own_snapshot(monkeypatch, current)
    calls = []
    def collect(cfg, b, *, api):
        with budgets.admitted_transport("data"):
            calls.append(api)
            rows = [[b.get("trade_date", b["as_of"]), "5"]]
            return rows, mf._projection_digest(api, b, rows), datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mf, "_coordinate", collect)
    with mf.moneyflow_scope({}, ticker="600895.SS", as_of=ASOF), margin.margin_balance_scope(
            {}, ticker="600895.SS", as_of=ASOF, trade_date=ASOF):
        flow = mf.moneyflow_for_state(current)
        balance = margin.margin_balance_for_state(current)
        assert mf.moneyflow_for_state(current) == flow and margin.margin_balance_for_state(current) == balance
    assert calls == ["moneyflow", "margin_detail"] and budget.receipt()["data_attempts"] == 2
    assert margin.margin_balance_for_state(current) is None
    current["margin_balance_observation"] = balance
    with pytest.raises(budgets.AnalysisBudgetStopped, match="restore"):
        margin._Run({}, "600895.SS", ASOF, ASOF).acquire(current)
    for key, bad in [("unit", "CNY_10000"), ("trade_date", "2026-11-12"), ("symbol", "000001.SZ"),
                     ("diagnostic", {"message": "synthetic-secret"})]:
        edited = copy.deepcopy(balance)
        edited[key] = bad
        edited["normalized_sha256"] = mf._digest({k: v for k, v in edited.items() if k != "normalized_sha256"})
        rendered = margin.margin_balance_markdown({**current, "margin_balance_observation": edited})
        assert "绑定无效" in rendered and "synthetic-secret" not in rendered


@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_actual_analyst_graph_and_report_keep_rating_evidence(monkeypatch, budget, language):
    from unittest.mock import MagicMock

    from langgraph.graph import END, START, StateGraph

    from tests.test_multiyear_integration import combined_state
    from tests.test_supplemental_presentation import MemoryStore, export
    from tradingagents import reporting
    from tradingagents.agents.analysts import sentiment_analyst as analyst
    from tradingagents.agents.schemas import SentimentReport
    from tradingagents.agents.utils.agent_states import AgentState
    from tradingagents.dataflows import config
    from tradingagents.terminal_contract import apply_terminal_contract
    current, ev = combined_state()
    current["messages"] = []
    own_snapshot(monkeypatch, current)
    monkeypatch.setitem(config._config, "output_language", language)
    monkeypatch.setattr(analyst, "acquire_news", lambda *a, **k: SimpleNamespace(value=None, artifact=None, outcomes=()))
    monkeypatch.setattr(analyst, "get_china_a_local_sentiment", lambda *a, **k: "synthetic unavailable")
    monkeypatch.setattr(analyst, "get_china_a_enhancements_for_categories", lambda *a, **k: "")
    llm = MagicMock()
    structured = llm.with_structured_output.return_value
    structured.invoke.return_value = SentimentReport(overall_band="Neutral", overall_score=5.0, confidence="low", narrative="合成正文")
    node = analyst.create_sentiment_analyst(llm)
    baseline = node(copy.deepcopy(current))
    def collect(cfg, b, *, api):
        assert api == "margin_detail"
        with budgets.admitted_transport("data"):
            rows = [[b["trade_date"], "12345.25"]]
            return rows, mf._projection_digest(api, b, rows), datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mf, "_coordinate", collect)
    graph = StateGraph(AgentState)
    graph.add_node("sentiment", node)
    graph.add_edge(START, "sentiment")
    graph.add_edge("sentiment", END)
    with margin.margin_balance_scope({}, ticker=current["company_of_interest"], as_of=current["trade_date"],
                                     trade_date=ev.market_snapshot.effective_trading_date):
        result = graph.compile().invoke(copy.deepcopy(current))
    text = "\n".join(m.content for m in structured.invoke.call_args.args[0])
    assert "12345.25" in text and "rzye" in text and "retrospective_backfill" in text
    assert result["evidence_state"] == baseline["evidence_state"] and result["sentiment_report"] == baseline["sentiment_report"]
    current.update(result)
    def audit(s, *a, **k):
        apply_terminal_contract(s, audit_digest="b" * 64)
        s["decision_audit_created_at"] = "2026-07-18T00:00:00Z"
    audit(current)
    monkeypatch.setattr(reporting, "ReportStore", MemoryStore)
    monkeypatch.setattr(reporting, "write_immutable_decision_audit", audit)
    enhanced = export(copy.deepcopy(current), None, language)
    plain = copy.deepcopy(current)
    plain.pop("margin_balance_observation")
    normal = export(plain, None, language)
    assert enhanced["1_analysts/margin_balance.md"] in enhanced["complete_report.md"]
    assert b"12345.25" in enhanced["complete_report.md"]
    assert enhanced["5_portfolio/decision.md"] == normal["5_portfolio/decision.md"]
