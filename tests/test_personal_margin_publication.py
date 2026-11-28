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
    PhysicalAttemptFailure,
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


def _acquire_fixture_margin(coord, session, bound, now):
    lease = coord.acquire(
        request_key=full.margin_key(bound, session.account_scope),
        upstream_service_id=session.service,
        owner_id="fixture-worker",
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=now,
        lease_duration=timedelta(minutes=2),
        cooldown_scope="market-snapshot",
    ).lease
    assert lease is not None
    return lease


def install_expired_margin_fixture(changes, feed):
    observe = full.margin_observation

    def observe_after_zero_attempt_crash(session, bound, token, account, authority):
        feed.wait(session.interval.total_seconds())
        with MarketHistoryStore.open_provider_request_authority(session.history) as store:
            coord = ProviderRequestCoordinator(store)
            old = _acquire_fixture_margin(coord, session, bound, cold.now())
            sequence = coord._sequence_id(old)
            coord._create_retry_sequence(sequence, old)
        feed.wait(121)
        result = observe(session, bound, token, account, authority)
        with MarketHistoryStore.open_provider_request_authority(session.history) as store:
            assert store._connection.execute(
                "SELECT status, final_physical_attempt_count, failure_error_code "
                "FROM provider_request_sequences WHERE sequence_id=?",
                (sequence,),
            ).fetchone() == ("failed", 0, "personal_margin_expired_before_send")
        return result

    changes.setattr(full, "margin_observation", observe_after_zero_attempt_crash)


@pytest.mark.parametrize(
    "state", ["expired_zero", "fresh", "sent_unknown", "orphan", "different_generation"]
)
def test_personal_margin_recovers_only_owned_expired_zero(tmp_path, monkeypatch, state):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T10:00")
    s = case.session
    with MarketHistoryStore.open_provider_request_authority(s.history) as store:
        coord = ProviderRequestCoordinator(store)
        lease = _acquire_fixture_margin(coord, s, case.binding, case.now)
        old = coord._sequence_id(lease)
        coord._create_retry_sequence(old, lease)
        if state == "sent_unknown":
            coord._start_attempt_event(
                sequence_id=old,
                lease=lease,
                service_name=s.name,
                operation="margin_detail",
                attempt_index=1,
                attempted_at=lease.acquired_at,
                pacing_event="permit_acquired",
                pacing_wait_seconds=0,
            )
        if state in {"orphan", "different_generation"}:
            coord.release(lease)
        if state == "different_generation":
            case.now += timedelta(seconds=1)
            _acquire_fixture_margin(coord, s, case.binding, case.now)
        if state != "fresh":
            case.now += timedelta(minutes=3)

    result = case.observe()
    success = state == "expired_zero"
    assert result["status"] == ("valid_empty" if success else "prior_margin_attempt_unsettled")
    assert len(case.sends) == s.financial_attempts() == int(success)
    with MarketHistoryStore.open_provider_request_authority(s.history) as store:
        prior = store._connection.execute(
            "SELECT status, final_physical_attempt_count FROM provider_request_sequences WHERE sequence_id=?",
            (old,),
        ).fetchone()
        if state == "expired_zero":
            assert prior == ("failed", 0)
        else:
            assert prior == ("running", None)
    case.session = case.new_session()
    again = case.observe()
    assert again["status"] == result["status"]
    assert case.session.financial_attempts() == 0 and len(case.sends) == int(success)


def test_expired_margin_permit_stops_before_mock_send(tmp_path, monkeypatch):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T10:00")

    def delayed_transport(token, endpoint, params, fields, remaining, admit):
        case.now += timedelta(minutes=3)
        admit()
        pytest.fail("expired work reached mock send")

    monkeypatch.setattr(cold, "post", delayed_transport)
    assert case.observe()["status"] == "unverified"
    assert case.session.financial_attempts() == 0
    with MarketHistoryStore.open_provider_request_authority(case.session.history) as store:
        assert store._connection.execute(
            "SELECT count(*) FROM provider_request_attempts"
        ).fetchone() == (0,)


def test_margin_rechecks_completed_cache_at_claim(tmp_path, monkeypatch):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T10:00")
    checked = full.cached_margin
    reads = 0

    def observed_cache(*args):
        nonlocal reads
        reads += 1
        if reads == 1:
            # Another completed projection becomes visible after the first lookup.
            case.seed_old_result(timedelta(0))
            return None
        return checked(*args)

    monkeypatch.setattr(full, "cached_margin", observed_cache)
    result = case.observe()
    assert result["status"] == "valid_empty" and result["acquisition"] == "coordinator_cache"
    assert reads >= 2 and case.sends == [] and case.session.financial_attempts() == 0


@pytest.mark.parametrize(
    "state", ["orphan_event", "missing_attempt", "inconsistent_zero", "nullable_scope"]
)
def test_ambiguous_terminal_margin_history_stays_blocked(tmp_path, monkeypatch, state):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T08:40")
    old = case.seed_old_result(timedelta(0))
    case.now += timedelta(hours=1)
    with MarketHistoryStore.open_provider_request_authority(case.session.history) as store:
        db = store._connection
        if state == "orphan_event":
            db.execute(
                "DELETE FROM provider_request_sequences WHERE sequence_id=?", (old.sequence_id,)
            )
        elif state == "missing_attempt":
            db.execute(
                "DELETE FROM provider_request_attempts WHERE sequence_id=?", (old.sequence_id,)
            )
        elif state == "nullable_scope":
            db.execute(
                "UPDATE provider_request_attempts SET capacity_scope=NULL WHERE sequence_id=?",
                (old.sequence_id,),
            )
        else:
            db.execute(
                "UPDATE provider_request_sequences SET status='failed', failure_outcome_kind='abandoned', "
                "failure_error_code=?, final_physical_attempt_count=0 WHERE sequence_id=?",
                (
                    "personal_margin_expired_before_send",
                    old.sequence_id,
                ),
            )
    assert case.observe()["status"] == "prior_margin_attempt_unsettled"
    assert case.sends == [] and case.session.financial_attempts() == 0


def test_margin_lifecycle_preserves_new_lease_generation(tmp_path, monkeypatch):
    from scripts.personal_margin_coordinator import (
        PersonalMarginCoordinator,
        PriorMarginRequestUnsettled,
    )

    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T10:00")
    s = case.session
    with MarketHistoryStore.open_provider_request_authority(s.history) as store:
        coord = PersonalMarginCoordinator(
            store,
            full.margin_key(case.binding, s.account_scope),
            s.service,
            lambda: None,
            lambda: case.now,
        )
        old = _acquire_fixture_margin(coord, s, case.binding, case.now)
        seq = coord._sequence_id(old)
        coord._create_retry_sequence(seq, old)
        case.now += timedelta(minutes=3)
        current = _acquire_fixture_margin(coord, s, case.binding, case.now)
        assert current.owner_id == old.owner_id and current.acquired_at != old.acquired_at
        assert coord.renew(old, now=case.now, lease_duration=timedelta(minutes=2)) is None
        coord.release(old)
        with pytest.raises(PriorMarginRequestUnsettled):
            coord._create_retry_sequence(seq, old)
        assert coord.renew(current, now=case.now, lease_duration=timedelta(minutes=2)) is not None
        assert not coord.physical_attempt_events(seq)
        current_seq = coord._sequence_id(current)
        coord._create_retry_sequence(current_seq, current)
        coord._close_running_sequence(
            sequence_id=current_seq, completed_at=case.now, error_code="synthetic_terminal"
        )
        with pytest.raises(PhysicalAttemptFailure):
            coord._start_attempt_event(
                sequence_id=current_seq,
                lease=current,
                service_name=s.name,
                operation="margin_detail",
                attempt_index=1,
                attempted_at=case.now,
                pacing_event="permit_acquired",
                pacing_wait_seconds=0,
            )
        assert not coord.physical_attempt_events(current_seq)
        sampled_before_lock = case.now
        case.now += timedelta(minutes=3)
        assert (
            coord.renew(current, now=sampled_before_lock, lease_duration=timedelta(minutes=2))
            is None
        )


def test_generic_recovery_without_original_lease_stays_blocked(tmp_path, monkeypatch):
    case = MarginWindow(tmp_path, monkeypatch, "600895.SH", "2024-03-01", "2024-03-02T10:00")
    session = case.session
    with MarketHistoryStore.open_provider_request_authority(session.history) as store:
        coord = ProviderRequestCoordinator(store)
        lease = _acquire_fixture_margin(coord, session, case.binding, case.now)
        sequence = coord._sequence_id(lease)
        coord._create_retry_sequence(sequence, lease)
        case.now += timedelta(minutes=3)
        # An earlier unrelated data step can run ordinary expiry recovery first.
        other = coord.acquire(
            request_key="other-synthetic-data-step",
            upstream_service_id=session.service,
            owner_id="other-fixture-worker",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=case.now,
            lease_duration=timedelta(minutes=2),
            cooldown_scope="market-snapshot",
        ).lease
        assert other is not None
        coord.release(other)
        assert store._connection.execute(
            "SELECT failure_error_code, final_physical_attempt_count "
            "FROM provider_request_sequences WHERE sequence_id=?",
            (sequence,),
        ).fetchone() == ("coordinator_lease_expired", 0)
    assert case.observe()["status"] == "prior_margin_attempt_unsettled"
    assert case.session.financial_attempts() == 0 and case.sends == []
