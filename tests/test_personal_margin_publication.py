"""Synthetic personal margin publication times, with the real request ledger."""

import json
import time
from datetime import date, datetime, timedelta

import pytest

from scripts import personal_bootstrap as cold, personal_observations as full
from tests.test_personal_observations import binding, profile
from tradingagents import margin_balance
from tradingagents.market_history.config import provider_request_authority_config
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    RequestPriority,
    record_active_physical_attempt_io,
)
from tradingagents.market_history.store import MarketHistoryStore


class MarginWindow:
    def __init__(self, tmp_path, monkeypatch, symbol, day, instant):
        self.config = dict(profile(tmp_path), personal_mode="full", personal_sources=full.SOURCES)
        self.binding = dict(binding(day), symbol=symbol)
        self.now = datetime.fromisoformat(instant).replace(tzinfo=full.SHANGHAI)
        self.sends = []
        self.rows = []
        monkeypatch.setattr(cold, "now", lambda: self.now)
        monkeypatch.setattr(cold, "post", self.post)
        self.session = self.new_session()
        self.authority = provider_request_authority_config(self.session.history)
        with MarketHistoryStore.open_provider_request_authority(self.session.history) as store:
            ProviderRequestCoordinator(store).register_upstream_service(
                self.session.service, self.session.name
            )

    def new_session(self):
        session = cold.Session(self.config, (time.time(), time.monotonic()))
        session.symbol = self.binding["symbol"].replace(".SH", ".SS")
        session.day = date.fromisoformat(self.binding["trade_date"])
        return session

    def raw(self):
        return json.dumps(
            {"code": 0, "data": {"fields": list(margin_balance.FIELDS), "items": self.rows}}
        ).encode()

    def post(self, token, endpoint, params, fields, remaining, admit):
        assert endpoint == "margin_detail" and token == "synthetic"
        admit()
        self.sends.append(endpoint)
        return self.raw()

    def observe(self):
        s = self.session
        return full.margin_observation(
            s, self.binding, "synthetic", s.account_scope, self.authority
        )

    def seed_old_result(self, latency):
        # Use the original request identity and coordinator, including an actual
        # I/O admission record, as an older client could before the new window.
        s = self.session

        def response():
            record_active_physical_attempt_io()
            self.now += latency
            return full.project_margin(self.raw(), self.binding)

        with MarketHistoryStore.open_provider_request_authority(s.history) as store:
            coord = ProviderRequestCoordinator(store)
            coord.configure_operator_ceiling(
                upstream_service_id=s.service,
                minimum_interval=s.interval,
                allow_prewarming=False,
                configured_at=self.now,
            )
            return coord.execute_physical_workflow(
                request_key=full.margin_key(self.binding, s.account_scope),
                upstream_service_id=s.service,
                owner_id="previous-synthetic-session",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: self.now,
                sleep=s.wait,
                lease_duration=timedelta(minutes=2),
                max_physical_operations=1,
                cooldown_scope="market-snapshot",
                workflow=lambda execute: execute("margin_detail", response),
            )


@pytest.mark.parametrize(
    "symbol,instant,expected",
    [
        ("600895.SH", "2024-03-02T00:01", "publication_pending"),
        ("600895.SH", "2024-03-02T08:59:59", "publication_pending"),
        ("600895.SH", "2024-03-02T09:00", "valid_empty"),
        ("000001.SZ", "2024-03-02T12:00", "publication_pending"),
        ("000001.SZ", "2024-03-03T12:00", "publication_pending"),
        ("000001.SZ", "2024-03-04T08:59:59", "publication_pending"),
        ("000001.SZ", "2024-03-04T09:00", "valid_empty"),
    ],
)
def test_margin_window(tmp_path, monkeypatch, symbol, instant, expected):
    case = MarginWindow(tmp_path, monkeypatch, symbol, "2024-03-01", instant)
    assert case.observe()["status"] == expected
    assert case.session.financial_attempts() == len(case.sends) == int(expected == "valid_empty")


@pytest.mark.parametrize("latency", [0, 65])
def test_old_empty_does_not_hide_published_rows(tmp_path, monkeypatch, latency):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T08:59")
    old = case.seed_old_result(timedelta(seconds=latency))
    case.now += timedelta(minutes=3)
    case.rows = [["20240301", "600895.SH", 321]]
    first = case.observe()
    assert first["status"] == "verified" and first["acquisition"] == "account_request"
    assert first["rzye_yuan"] == "321" and case.session.financial_attempts() == 1
    case.session = case.new_session()
    assert case.observe() == dict(first, acquisition="coordinator_cache")
    assert case.session.financial_attempts() == 0 and len(case.sends) == 1
    assert old.physical_attempt_count == 1


@pytest.mark.parametrize("positive", [False, True])
def test_qualified_cache_still_reused(tmp_path, monkeypatch, positive):
    stamp = "2024-03-02T08:45" if positive else "2024-03-02T09:01"
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", stamp)
    if positive:
        case.rows = [["20240301", "600895.SH", 321]]
    case.seed_old_result(timedelta(0))
    result = case.observe()
    assert result["acquisition"] == "coordinator_cache"
    assert result["status"] == ("verified" if positive else "valid_empty")
    assert case.session.financial_attempts() == 0 and case.sends == []


@pytest.mark.parametrize("unsettled", ["sequence", "attempt", "recovered"])
def test_old_empty_refresh_defers_unsettled_work(tmp_path, monkeypatch, unsettled):
    import sqlite3
    from contextlib import closing

    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T08:45")
    old = case.seed_old_result(timedelta(0))
    case.now += timedelta(hours=1)
    # Simulate an interrupted local writer with no live lease. The coordinator
    # cannot join a missing owner; the personal session must still avoid a send.
    with closing(sqlite3.connect(case.authority.database_path)) as db, db:
        if unsettled in {"sequence", "recovered"}:
            db.execute(
                "UPDATE provider_request_sequences SET status='running', completed_at=NULL "
                "WHERE sequence_id=?",
                (old.sequence_id,),
            )
        if unsettled in {"attempt", "recovered"}:
            db.execute(
                "UPDATE provider_request_attempts SET outcome='started', final_physical_attempt_count=NULL "
                "WHERE sequence_id=?",
                (old.sequence_id,),
            )
    if unsettled == "recovered":
        # The coordinator closes a lost owner locally; this cannot resolve
        # an already-admitted request's unknown provider result.
        with MarketHistoryStore.open_provider_request_authority(case.session.history) as store:
            coordinator = ProviderRequestCoordinator(store)
            coordinator._close_running_sequence(
                sequence_id=old.sequence_id,
                completed_at=case.now,
                error_code="coordinator_lease_expired",
            )
            (event,) = coordinator.physical_attempt_events(old.sequence_id)
            assert event.outcome.value == "abandoned" and event.final_physical_attempt_count == 1
    assert case.observe()["status"] == "prior_margin_attempt_unsettled"
    assert case.sends == [] and case.session.financial_attempts() == 0


def test_early_physical_query_cannot_bypass_observation_window(tmp_path, monkeypatch):
    case = MarginWindow(tmp_path, monkeypatch, "000001.SZ", "2024-03-01", "2024-03-03T12:00")
    with pytest.raises(cold.PreparationError, match="margin_publication_window_not_open"):
        case.session.query(
            "synthetic",
            "margin_detail",
            {"ts_code": "000001.SZ", "trade_date": "20240301"},
            ",".join(margin_balance.FIELDS),
            observation_binding=case.binding,
        )
    assert case.sends == [] and case.session.financial_attempts() == 0
