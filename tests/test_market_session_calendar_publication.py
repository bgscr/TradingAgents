from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from tradingagents.dataflows.baostock_calendar import (
    BAOSTOCK_CALENDAR_DATASET_NAME,
    BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
    BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
    BAOSTOCK_CALENDAR_PROVENANCE,
    MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
    BaoStockSessionCalendarAcquisition,
    BaoStockSessionCalendarAttempt,
    BaoStockSessionCalendarTerminalOutcome,
    MainlandSessionCalendarMembership,
)
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSessionCalendarIntegrityError,
    MarketSessionStatus,
    PayloadMutationTimeoutError,
    PhysicalAttemptOutcome,
    publication_from_baostock_session_calendar_acquisition,
    upstream_service_identity_for_provider,
)

_OBSERVED_AT = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


def _config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _acquisition(**overrides: object) -> BaoStockSessionCalendarAcquisition:
    upstream_service_id, upstream_service_name = upstream_service_identity_for_provider(
        "baostock"
    )
    logical_request_id = "market-session-calendar-acquisition-request-v1=sha256:" + (
        "1" * 64
    )
    workflow_sequence_id = "provider-request-sequence=sha256:" + ("2" * 64)
    attempts = tuple(
        BaoStockSessionCalendarAttempt(
            attempt_event_id="provider-physical-attempt=sha256:" + str(index) * 64,
            sequence_id=workflow_sequence_id,
            request_key=logical_request_id,
            upstream_service_id=upstream_service_id,
            upstream_service_name=upstream_service_name,
            capacity_scope="session-calendar",
            operation=operation,
            attempt_index=index,
            attempted_at=_OBSERVED_AT,
            pacing_event="permit_acquired",
            pacing_wait_seconds=0.0,
            outcome=PhysicalAttemptOutcome.AVAILABLE,
            retryable=False,
            status_code=None,
            error_code=None,
            retry_after_seconds=None,
            cooldown_changed=False,
            cooldown_until=None,
            final_physical_attempt_count=3,
        )
        for index, operation in enumerate(
            ("baostock_login", "query_trade_dates", "baostock_logout"),
            start=1,
        )
    )
    values: dict[str, object] = {
        "provider": "baostock",
        "endpoint": "query_trade_dates",
        "upstream_service_id": upstream_service_id,
        "upstream_service_name": upstream_service_name,
        "provider_dataset_id": (
            "provider-dataset=sha256:"
            "b9cbc86198c8e47afd07ce8ce8505718cca4739ff584298f056f794bd1d62d2d"
        ),
        "dataset_name": BAOSTOCK_CALENDAR_DATASET_NAME,
        "reference_market": "mainland-cn",
        "timezone_name": "Asia/Shanghai",
        "requested_start_date": date(2026, 8, 10),
        "requested_end_date": date(2026, 8, 11),
        "data_usage_mode": DataUsageMode.PERSONAL_RESEARCH,
        "normalizer_version": BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
        "payload_contract": BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
        "session_time_contract": MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
        "provenance": BAOSTOCK_CALENDAR_PROVENANCE,
        "requested_at": _OBSERVED_AT,
        "retrieved_at": _OBSERVED_AT,
        "observed_at": _OBSERVED_AT,
        "membership": (
            MainlandSessionCalendarMembership(
                calendar_date=date(2026, 8, 10),
                is_trading_day=True,
            ),
            MainlandSessionCalendarMembership(
                calendar_date=date(2026, 8, 11),
                is_trading_day=False,
            ),
        ),
        "content_identity": "calendar-acquisition-content=sha256:" + ("4" * 64),
        "logical_request_id": logical_request_id,
        "workflow_sequence_id": workflow_sequence_id,
        "physical_attempt_count": 3,
        "attempt_events": attempts,
        "terminal_outcome": BaoStockSessionCalendarTerminalOutcome.AVAILABLE,
    }
    values.update(overrides)
    return BaoStockSessionCalendarAcquisition(**values)


@pytest.mark.unit
def test_cal01_acquisition_becomes_one_canonical_calendar_publication() -> None:
    publication = publication_from_baostock_session_calendar_acquisition(
        _acquisition()
    )

    assert publication.provider.provider_name == "baostock"
    assert publication.provider.dataset_name == "mainland-session-calendar-v1"
    assert publication.reference_market == "mainland-cn"
    assert publication.timezone_name == "Asia/Shanghai"
    assert publication.normalizer_version == "baostock-session-calendar-normalizer-v1"
    assert publication.payload_contract == "mainland-session-calendar-payload-v1"
    assert (
        publication.session_time_contract
        == "mainland-cn-regular-session-times-v1"
    )
    assert publication.membership_source == "provider_returned"
    assert publication.session_time_source == "contract_derived"

    shanghai = ZoneInfo("Asia/Shanghai")
    open_session, closed_session = publication.sessions
    assert open_session.status is MarketSessionStatus.OPEN
    assert open_session.opens_at == datetime(2026, 8, 10, 9, 30, tzinfo=shanghai)
    assert open_session.closes_at == datetime(2026, 8, 10, 15, 0, tzinfo=shanghai)
    assert closed_session.status is MarketSessionStatus.CLOSED
    assert closed_session.opens_at is None
    assert closed_session.closes_at is None

    payload = json.loads(publication.raw_payload)
    assert payload == {
        "contract_version": "mainland-session-calendar-payload-v1",
        "dataset_name": "mainland-session-calendar-v1",
        "endpoint": "query_trade_dates",
        "membership_source": "provider_returned",
        "normalizer_version": "baostock-session-calendar-normalizer-v1",
        "provenance_class": "observed_point_in_time",
        "provider": "baostock",
        "provider_dataset_id": (
            "provider-dataset=sha256:"
            "b9cbc86198c8e47afd07ce8ce8505718cca4739ff584298f056f794bd1d62d2d"
        ),
        "reference_market": "mainland-cn",
        "session_time_contract": "mainland-cn-regular-session-times-v1",
        "session_time_source": "contract_derived",
        "sessions": [
            {
                "closes_at": "2026-08-10T07:00:00+00:00",
                "opens_at": "2026-08-10T01:30:00+00:00",
                "session_date": "2026-08-10",
                "status": "open",
            },
            {
                "closes_at": None,
                "opens_at": None,
                "session_date": "2026-08-11",
                "status": "closed",
            },
        ],
        "timezone_name": "Asia/Shanghai",
        "upstream_service_id": publication.provider.upstream_service_id,
        "upstream_service_name": publication.provider.upstream_service_name,
    }
    assert publication.payload_digest == sha256(publication.raw_payload).hexdigest()
    assert "raw_payload=" not in repr(publication)


@pytest.mark.unit
def test_semantic_republish_reuses_revision_and_retains_completed_audit(tmp_path) -> None:
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    later = replace(
        publication,
        observed_at=datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        first = store.publish_session_calendar(publication)
        second = store.publish_session_calendar(later)
        revision_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone()
        session_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone()
        audits = tuple(
            store._connection.execute(
                "SELECT status, completed_at FROM ingestion_runs "
                "WHERE operation = 'calendar_refresh' ORDER BY completed_at"
            )
        )
        reopened = store.read_session_calendar_revision(first.calendar_revision_id)

    assert first.calendar_revision_id == second.calendar_revision_id
    assert first.ingestion_run_id != second.ingestion_run_id
    assert revision_count == (1,)
    assert session_count == (2,)
    assert audits == (
        ("published", "2026-08-12T08:00:00+00:00"),
        ("published", "2026-08-13T08:00:00+00:00"),
    )
    assert reopened.observed_at == _OBSERVED_AT


@pytest.mark.unit
@pytest.mark.parametrize(
    "fault_phase",
    (
        "calendar_upstream_verified",
        "calendar_provider_dataset_verified",
        "calendar_ingestion_completed",
        "calendar_revision_written",
        "calendar_sessions_written",
        "calendar_revision_verified",
        "calendar_before_commit",
    ),
)
def test_calendar_publication_faults_leave_no_partial_visible_state(
    tmp_path,
    fault_phase: str,
) -> None:
    class InjectedPublicationFailure(RuntimeError):
        pass

    def fail_at_phase(phase: str, _subject: str) -> None:
        if phase == fault_phase:
            raise InjectedPublicationFailure(fault_phase)

    with MarketHistoryStore.open(
        _config(tmp_path),
        _phase_hook=fail_at_phase,
    ) as store:
        with pytest.raises(InjectedPublicationFailure, match=fault_phase):
            store.publish_session_calendar(
                publication_from_baostock_session_calendar_acquisition(_acquisition())
            )
        visible = tuple(
            store._connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM market_session_calendars), "
                "(SELECT COUNT(*) FROM market_sessions), "
                "(SELECT COUNT(*) FROM ingestion_runs "
                " WHERE operation = 'calendar_refresh' AND status = 'published')"
            ).fetchone()
        )

    assert visible == (0, 0, 0)


@pytest.mark.unit
def test_existing_revision_is_revalidated_instead_of_trusted(tmp_path) -> None:
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = store.publish_session_calendar(publication)
        store._connection.execute(
            "UPDATE market_sessions SET session_status = 'open', "
            "opens_at = ?, closes_at = ? WHERE calendar_revision_id = ? "
            "AND session_date = '2026-08-11'",
            (
                "2026-08-11T01:30:00+00:00",
                "2026-08-11T07:00:00+00:00",
                published.calendar_revision_id,
            ),
        )

        with pytest.raises(
            MarketSessionCalendarIntegrityError,
            match="revision_identity_mismatch",
        ):
            store.publish_session_calendar(publication)
        row = store._connection.execute(
            "SELECT session_status, opens_at, closes_at FROM market_sessions "
            "WHERE calendar_revision_id = ? AND session_date = '2026-08-11'",
            (published.calendar_revision_id,),
        ).fetchone()
        audit_count = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()

    assert row == (
        "open",
        "2026-08-11T01:30:00+00:00",
        "2026-08-11T07:00:00+00:00",
    )
    assert audit_count == (1,)


@pytest.mark.unit
def test_audit_timestamps_and_input_order_do_not_change_semantic_revision() -> None:
    acquisition = _acquisition()
    first = publication_from_baostock_session_calendar_acquisition(acquisition)
    second = publication_from_baostock_session_calendar_acquisition(
        acquisition.model_copy(
            update={
                "requested_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
                "retrieved_at": datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc),
                "observed_at": datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc),
                "membership": tuple(reversed(acquisition.membership)),
            }
        )
    )

    assert first.raw_payload == second.raw_payload
    assert first.payload_digest == second.payload_digest
    assert first.sessions == second.sessions


@pytest.mark.unit
def test_membership_and_time_contract_semantics_change_payload_identity() -> None:
    original = publication_from_baostock_session_calendar_acquisition(_acquisition())
    changed_membership = publication_from_baostock_session_calendar_acquisition(
        _acquisition(
            membership=(
                MainlandSessionCalendarMembership(
                    calendar_date=date(2026, 8, 10),
                    is_trading_day=False,
                ),
                MainlandSessionCalendarMembership(
                    calendar_date=date(2026, 8, 11),
                    is_trading_day=False,
                ),
            )
        )
    )
    payload = json.loads(original.raw_payload)
    payload["session_time_contract"] = "mainland-cn-regular-session-times-v2"
    changed_time_contract_digest = sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert original.payload_digest != changed_membership.payload_digest
    assert original.payload_digest != changed_time_contract_digest


@pytest.mark.unit
def test_concurrent_identical_publications_share_one_revision_without_corruption(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    with MarketHistoryStore.open(config):
        pass
    barrier = Barrier(4)

    def publish(index: int):
        candidate = replace(
            publication,
            observed_at=datetime(2026, 8, 12, 8, index, tzinfo=timezone.utc),
        )
        barrier.wait(timeout=10)
        with MarketHistoryStore.open(config) as store:
            return store.publish_session_calendar(candidate)

    with ThreadPoolExecutor(max_workers=4) as executor:
        published = tuple(executor.map(publish, range(4)))

    with MarketHistoryStore.open(config) as store:
        reopened = store.read_session_calendar_revision(
            published[0].calendar_revision_id
        )
        revision_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone()
        session_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone()
        audit_count = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()

    assert len({item.calendar_revision_id for item in published}) == 1
    assert len({item.ingestion_run_id for item in published}) == 4
    assert revision_count == (1,)
    assert session_count == (2,)
    assert audit_count == (4,)
    assert reopened.sessions == publication.sessions


@pytest.mark.unit
def test_noncanonical_json_bytes_reuse_the_canonical_semantic_revision(tmp_path) -> None:
    canonical = publication_from_baostock_session_calendar_acquisition(_acquisition())
    noncanonical = replace(
        canonical,
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        raw_payload=json.dumps(
            json.loads(canonical.raw_payload),
            indent=2,
            sort_keys=False,
        ).encode(),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        first = store.publish_session_calendar(canonical)
        second = store.publish_session_calendar(noncanonical)
        reopened = store.read_session_calendar_revision(first.calendar_revision_id)

    assert first.calendar_revision_id == second.calendar_revision_id
    assert reopened.payload_digest == canonical.payload_digest


@pytest.mark.unit
def test_publication_payload_corruption_exposes_only_safe_typed_metadata(
    tmp_path,
) -> None:
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    config = _config(tmp_path)
    with MarketHistoryStore.open(config) as store:
        store.publish_session_calendar(publication)
        row = store._connection.execute(
            "SELECT relative_path FROM payload_artifacts WHERE digest = ?",
            (publication.payload_digest,),
        ).fetchone()
        assert row is not None
        (config.payload_root / str(row[0])).unlink()

        with pytest.raises(MarketSessionCalendarIntegrityError) as captured:
            store.publish_session_calendar(publication)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert rendered == (
        "revision_identity_mismatch "
        "MarketSessionCalendarIntegrityError('revision_identity_mismatch')"
    )
    assert str(tmp_path) not in rendered
    assert captured.value.__cause__ is None


@pytest.mark.unit
def test_invalid_publication_payload_does_not_leak_security_canaries(tmp_path) -> None:
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    canaries = (
        "credential=calendar-secret",
        "token=calendar-token",
        "https://calendar.invalid/query?key=calendar-key",
        str(tmp_path.resolve()),
    )
    payload = json.loads(publication.raw_payload)
    payload["unexpected"] = " ".join(canaries)
    invalid = replace(publication, raw_payload=json.dumps(payload).encode())

    with (
        MarketHistoryStore.open(_config(tmp_path)) as store,
        pytest.raises(ValueError) as captured,
    ):
        store.publish_session_calendar(invalid)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert rendered == (
        "calendar publication contract is invalid "
        "ValueError('calendar publication contract is invalid')"
    )
    assert all(canary not in rendered for canary in canaries)
    assert captured.value.__cause__ is None


@pytest.mark.unit
def test_payload_install_failure_is_safe_and_relationally_atomic(
    tmp_path,
    monkeypatch,
) -> None:
    canary = str((tmp_path / "credential=calendar-secret_token=calendar-token").resolve())
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())

    def fail_replace(_source, _destination) -> None:
        raise FileNotFoundError(canary)

    monkeypatch.setattr(os, "replace", fail_replace)
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        with pytest.raises(MarketSessionCalendarIntegrityError) as captured:
            store.publish_session_calendar(publication)
        visible = store._connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM market_session_calendars), "
            "(SELECT COUNT(*) FROM market_sessions), "
            "(SELECT COUNT(*) FROM ingestion_runs "
            " WHERE operation = 'calendar_refresh' AND status = 'published')"
        ).fetchone()

    assert captured.value.reason.value == "revision_identity_mismatch"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert canary not in f"{captured.value!s} {captured.value!r}"
    assert visible == (0, 0, 0)


@pytest.mark.unit
def test_payload_install_cleanup_cannot_replace_primary_failure(
    tmp_path,
    monkeypatch,
) -> None:
    primary_canary = str((tmp_path / "credential=primary_token=primary").resolve())
    cleanup_canary = str((tmp_path / "credential=cleanup_token=cleanup").resolve())
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())

    def fail_replace(_source, _destination) -> None:
        raise FileNotFoundError(primary_canary)

    original_unlink = Path.unlink

    def fail_temporary_unlink(path: Path, *args, **kwargs) -> None:
        if path.suffix == ".tmp":
            raise PermissionError(cleanup_canary)
        original_unlink(path, *args, **kwargs)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        monkeypatch.setattr(os, "replace", fail_replace)
        monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
        with pytest.raises(MarketSessionCalendarIntegrityError) as captured:
            store.publish_session_calendar(publication)
        visible = store._connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM market_session_calendars), "
            "(SELECT COUNT(*) FROM market_sessions), "
            "(SELECT COUNT(*) FROM ingestion_runs "
            " WHERE operation = 'calendar_refresh' AND status = 'published')"
        ).fetchone()

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.reason.value == "revision_identity_mismatch"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert primary_canary not in rendered
    assert cleanup_canary not in rendered
    assert visible == (0, 0, 0)


@pytest.mark.unit
def test_calendar_publication_mutex_timeout_is_bounded_and_atomic(
    tmp_path,
    monkeypatch,
) -> None:
    canary = str((tmp_path / "credential=mutex-secret_token=mutex-token").resolve())
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())

    @contextmanager
    def fail_mutex(_operation, _subject):
        try:
            raise sqlite3.OperationalError(canary)
        except sqlite3.OperationalError as exc:
            raise PayloadMutationTimeoutError(canary) from exc
        yield

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        monkeypatch.setattr(store, "_payload_mutation_scope", fail_mutex)
        with pytest.raises(PayloadMutationTimeoutError) as captured:
            store.publish_session_calendar(publication)
        visible = store._connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM market_session_calendars), "
            "(SELECT COUNT(*) FROM market_sessions), "
            "(SELECT COUNT(*) FROM ingestion_runs "
            " WHERE operation = 'calendar_refresh' AND status = 'published')"
        ).fetchone()

    assert str(captured.value) == "payload-mutation mutex unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert canary not in f"{captured.value!s} {captured.value!r}"
    assert visible == (0, 0, 0)
