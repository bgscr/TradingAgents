"""New bounded sessions may reuse only verified retrospective observations."""
import sqlite3

import pytest

from tests import test_bounded_margin_balance as margin_tests
from tests import test_powershell_fundflow as flow_tests
from tradingagents import analysis_budget as budgets, margin_balance as margin, powershell_fundflow as flow
from tradingagents.dataflows import config as config_module, financial_dispatch as dispatch
from tradingagents.market_history.coordinator import record_active_physical_attempt_io


def new_budget(monkeypatch):
    budget = budgets.AnalysisBudget(5, 60, allowed_data_vendors=["akshare", "tushare"],
                                   skip_optional_fundamentals=True)
    monkeypatch.setattr(budgets, "_ACTIVE", budget)
    return budget


@pytest.mark.parametrize("damage", [None, "stock_date", "source", "operation", "digest", "time",
                                    "terminal", "http500", "disabled", "strict"])
def test_new_margin_session_validates_completed_receipt(tmp_path, monkeypatch, damage):
    config = margin_tests.configured(tmp_path)
    config["bounded_observation_cache_mode"] = "retrospective"
    state = margin_tests.state()
    margin_tests.own_snapshot(monkeypatch, state)
    attempts = []

    def send(binding, token):
        with budgets.admitted_transport("data"):
            record_active_physical_attempt_io()
            attempts.append(binding)
            return margin_tests.body()

    monkeypatch.setattr(margin, "_send", send)
    def run():
        new_budget(monkeypatch)
        with margin.margin_balance_scope(config, ticker="600895.SH", as_of=margin_tests.ASOF,
                                        trade_date=margin_tests.ASOF):
            return margin.margin_balance_for_state(state)

    first = run()
    assert first["status"] == "available"
    db = str(config["market_history_database_path"]) + ".provider-requests.sqlite3"
    mutations = {
        "stock_date": "UPDATE provider_request_sequences SET request_key='wrong'",
        "source": "UPDATE provider_request_attempts SET upstream_service_name='alternate'",
        "operation": "UPDATE provider_request_attempts SET operation='other'",
        "digest": "UPDATE provider_request_sequences SET result_sha256=lower(hex(zeroblob(32)))",
        "time": "UPDATE provider_request_sequences SET completed_at='2100-01-01T00:00:00+00:00'",
        "terminal": "UPDATE provider_request_attempts SET terminal_outcome_kind='permission_denied'",
        "http500": "UPDATE provider_request_attempts SET status_code=500",
    }
    if damage in mutations:
        with sqlite3.connect(db) as conn:
            conn.execute(mutations[damage])
    elif damage in ("disabled", "strict"):
        config["bounded_observation_cache_mode"] = "disabled" if damage == "disabled" else "as_known_on_date"
    result = run()
    if damage == "strict":
        assert result["status"] == "historical_availability_unverified"
        assert result["rows"] == [] and result["attempts"] == 0 and len(attempts) == 1
        return
    assert result["status"] == "available"
    assert len(attempts) == (2 if damage else 1)
    assert result["attempts"] == (1 if damage else 0)
    if damage is None:
        assert result["received_at"] == first["received_at"]
        assert result["rows"] == [[margin_tests.ASOF, "12345.25"]]


@pytest.mark.parametrize("change", [None, "identity", "pin", "strict", "legacy_without_pin", "legacy_with_pin"])
def test_new_fundflow_session_reuses_only_percentage_projection(tmp_path, monkeypatch, change):
    config = flow_tests.runtime(tmp_path)
    config["bounded_observation_cache_mode"] = "retrospective"
    monkeypatch.setattr(config_module, "_config", config)
    calls = []
    def send(*args, **kwargs):
        calls.append(1)
        return flow_tests.payload(), {"status":"ok", "http_status":200, "process_closed":True}
    monkeypatch.setattr(flow, "run_child", send)
    identity = flow_tests.identity()
    def run(expect_artifact=True):
        budget = new_budget(monkeypatch)
        owner = flow_tests.owner(tmp_path)
        with flow.powershell_fundflow_scope(owner):
            dispatcher = dispatch.FinancialToolDispatcher.from_configured_vendors(
                instrument_identity=identity, config=config)
            result = dispatcher.dispatch(flow_tests.request())
            assert (result.artifact is not None) == expect_artifact
        return owner, budget
    before, _ = run()
    if change == "strict":
        config["bounded_observation_cache_mode"] = "as_known_on_date"
        _, budget = run(expect_artifact=False)
        assert len(calls) == 1 and budget.receipt()["data_attempts"] == 0
        return
    if change == "identity":
        identity = identity.model_copy(update={"display_name": "Changed identity metadata"})
    if change == "pin":
        config["bounded_fundflow_observation_sha256"] = "0" * 64
    if change in ("legacy_without_pin", "legacy_with_pin"):
        from hashlib import sha256
        from tradingagents.market_history.result_codec import decode_coordinated_result, encode_coordinated_result
        with sqlite3.connect(str(config["market_history_database_path"]) + ".provider-requests.sqlite3") as conn:
            raw, = conn.execute("SELECT result_payload FROM provider_request_sequences").fetchone()
            result = decode_coordinated_result(raw)
            assert result.pop("normalized_sha256") == before.value["normalized_sha256"]
            raw = encode_coordinated_result(result)
            conn.execute("UPDATE provider_request_sequences SET result_payload=?, result_sha256=?",
                         (raw, sha256(raw).hexdigest()))
        if change == "legacy_with_pin":
            config["bounded_fundflow_observation_sha256"] = before.value["normalized_sha256"]
    after, budget = run()
    if change in ("identity", "pin", "legacy_without_pin"):
        assert len(calls) == 2 and budget.receipt()["data_attempts"] == 1
        return
    assert len(calls) == 1
    assert after.value == before.value
    assert after.value["amounts_and_prices"] == "excluded_unit_unverified"
    assert budget.receipt()["data_attempts"] == budget.receipt()["model_attempts"] == 0


def test_bounded_session_preserves_explicit_as_known_mode(tmp_path):
    session = flow_tests.entry().Session(tmp_path, margin_date="2026-11-13")
    assert session.configure({"bounded_observation_cache_mode": "as_known_on_date"})[
        "bounded_observation_cache_mode"] == "as_known_on_date"


def test_news_receipt_is_a_miss_for_as_known_date(tmp_path, monkeypatch):
    from tradingagents import powershell_news as news

    exe, script = tmp_path / "pwsh.exe", tmp_path / "Get-PublicNews.ps1"
    exe.write_bytes(b"synthetic")
    script.write_bytes(b"synthetic")
    budget = new_budget(monkeypatch)
    owner = news.PowerShellNews(exe, script, "601328.SH", "2026-11-13", as_known_on_date=True)
    monkeypatch.setattr(news, "run_child", lambda *a, **kw: pytest.fail("unexpected child"))
    result = owner.acquire("601328.SS", "2026-11-06", "2026-11-13", tool_call_id="news",
                           source_ref="offline.news", capability="get_news")
    assert owner.summary()["cross_session_cache"]["reason"] == "raw_response_not_retained"
    assert result.artifact is None
    assert budget.receipt()["data_attempts"] == 0


@pytest.mark.parametrize("change", ["identity", "missing_digest"])
def test_inflight_follower_verifies_returned_projection(tmp_path, monkeypatch, change):
    new_budget(monkeypatch)
    owner = flow_tests.owner(tmp_path)
    identity = flow_tests.identity()
    owner.bind_identity(identity)
    monkeypatch.setattr(flow, "run_child", lambda *a, **kw: (flow_tests.payload(), {"status": "ok"}))
    captured = {}
    def publish(operation, callback):
        captured.update(callback())
        return captured
    owner.acquire("601328.SH", "2026-11-13", physical_request=publish)
    follower = flow_tests.owner(tmp_path)
    if change == "identity":
        identity = identity.model_copy(update={"display_name": "Different owner identity"})
    else:
        del captured["normalized_sha256"]
    follower.bind_identity(identity)
    with pytest.raises(flow.FundflowStop, match="fundflow_projection_binding_invalid"):
        follower.acquire("601328.SH", "2026-11-13", physical_request=lambda op, callback: captured)
