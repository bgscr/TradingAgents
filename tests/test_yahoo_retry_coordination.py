from __future__ import annotations

import copy
from datetime import datetime, timezone

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.market_snapshot as market_snapshot
import tradingagents.dataflows.stockstats_utils as stockstats_utils
import tradingagents.dataflows.y_finance as y_finance
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import build_evidence_state
from tradingagents.evidence_artifacts import (
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryStore,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
)


def _runtime_config(tmp_path, *, max_attempts: int = 4) -> dict:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(
                history_root / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
            "yahoo_max_physical_attempts": max_attempts,
        }
    )
    runtime_config["data_vendors"]["core_stock_apis"] = "yfinance"
    return runtime_config


@pytest.mark.unit
def test_yahoo_history_retries_are_one_for_one_with_coordinator_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    calls: list[int] = []

    class FakeTicker:
        def history(self, **_kwargs):
            calls.append(len(calls) + 1)
            if len(calls) < 4:
                raise ConnectionError("deterministic disconnect")
            return pd.DataFrame(
                {
                    "Open": [10.0],
                    "High": [11.0],
                    "Low": [9.0],
                    "Close": [10.5],
                    "Volume": [100],
                },
                index=pd.DatetimeIndex(["2026-07-24"], name="Date"),
            )

    monkeypatch.setattr(y_finance.yf, "Ticker", lambda _symbol: FakeTicker())

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "BTC-USD",
        "2026-07-01",
        "2026-07-24",
    )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        rows = tuple(
            store._connection.execute(
                "SELECT sequence_id FROM provider_request_attempts "
                "ORDER BY attempt_index"
            )
        )
        assert rows
        events = ProviderRequestCoordinator(store).physical_attempt_events(
            str(rows[0][0])
        )

    assert snapshot.provider == "yfinance"
    assert calls == [1, 2, 3, 4]
    assert [event.attempt_index for event in events] == [1, 2, 3, 4]
    assert [event.outcome for event in events] == [
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.AVAILABLE,
    ]
    assert all(event.final_physical_attempt_count == 4 for event in events)
    assert len(snapshot.physical_attempt_events) == 4

    evidence = build_evidence_state(symbol="BTC-USD", identity={}, snapshot=snapshot)
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())

    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.physical_attempt_count == 4
    assert audit.market_snapshot is not None
    assert audit.market_snapshot.physical_attempt_count == 4
    assert [
        event.outcome for event in audit.market_snapshot.physical_attempt_events
    ] == ["disconnect", "disconnect", "disconnect", "available"]


@pytest.mark.unit
def test_yahoo_budget_exhausts_before_sequential_fallback_starts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=2)
    runtime_config["data_vendors"]["core_stock_apis"] = "yfinance,baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    attempted_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: attempted_at)
    call_order: list[str] = []

    class DisconnectingTicker:
        def history(self, **_kwargs):
            call_order.append("yfinance")
            raise ConnectionError("deterministic disconnect")

    def baostock_fallback(*_args):
        call_order.append("baostock")
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        y_finance.yf,
        "Ticker",
        lambda _symbol: DisconnectingTicker(),
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "yfinance": market_snapshot.SnapshotProvider(
                market_snapshot._load_yfinance,
                "auto_adjusted",
            ),
            "baostock": market_snapshot.SnapshotProvider(
                baostock_fallback,
                "qfq",
            ),
        },
    )

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "BTC-USD",
        "2026-07-01",
        "2026-07-24",
    )

    assert snapshot.provider == "baostock"
    assert call_order == ["yfinance", "yfinance", "baostock"]
    assert snapshot.acquisition_outcomes[0].reason.value == "disconnect"
    assert len(snapshot.physical_attempt_events) == 2
    assert all(
        event.final_physical_attempt_count == 2
        for event in snapshot.physical_attempt_events
    )


@pytest.mark.unit
def test_provider_coordinator_fails_closed_when_legacy_authority_is_corrupt(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    history_config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open(history_config):
        pass
    history_config.database_path.write_bytes(b"not-a-sqlite-database")

    provider_calls = 0

    def provider_load(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return pd.DataFrame()

    provider = market_snapshot.SnapshotProvider(
        provider_load,
        "auto_adjusted",
    )

    with pytest.raises(AcquisitionFailure) as captured:
        market_snapshot._load_through_provider_coordinator(
            "yfinance",
            provider,
            "BTC-USD",
            "2026-07-01",
            "2026-07-24",
        )

    sidecar_database = history_config.database_path.with_name(
        f"{history_config.database_path.name}.provider-requests.sqlite3"
    )
    assert captured.value.reason.value == "upstream_busy"
    assert provider_calls == 0
    assert sidecar_database.exists()


@pytest.mark.unit
def test_one_yahoo_coordinator_permit_cannot_trigger_a_hidden_adapter_retry(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    runtime_config["data_vendors"]["core_stock_apis"] = "yfinance,baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    calls = 0

    class RateLimitedTicker:
        def history(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise YFRateLimitError()

    monkeypatch.setattr(y_finance.yf, "Ticker", lambda _symbol: RateLimitedTicker())
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "yfinance": market_snapshot.SnapshotProvider(
                market_snapshot._load_yfinance,
                "auto_adjusted",
            ),
            "baostock": market_snapshot.SnapshotProvider(
                lambda *_args: pd.DataFrame(
                    {
                        "Date": pd.to_datetime(["2026-07-24"]),
                        "Open": [10.0],
                        "High": [11.0],
                        "Low": [9.0],
                        "Close": [10.5],
                        "Volume": [100],
                    }
                ),
                "qfq",
            ),
        },
    )

    snapshot = market_snapshot.get_authoritative_market_snapshot(
        "BTC-USD",
        "2026-07-01",
        "2026-07-24",
    )

    assert calls == 1
    assert snapshot.provider == "baostock"
    assert [event.outcome for event in snapshot.physical_attempt_events] == [
        PhysicalAttemptOutcome.RATE_LIMITED
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("transport_result", "expected"),
    (
        (ConnectionError("disconnect"), PhysicalAttemptOutcome.DISCONNECT),
        (pd.DataFrame(), PhysicalAttemptOutcome.EMPTY_FRAME),
        (PermissionError("unauthorized"), PhysicalAttemptOutcome.AUTHENTICATION),
        ({"unexpected": "payload"}, PhysicalAttemptOutcome.MALFORMED_RESPONSE),
        (RuntimeError("other"), PhysicalAttemptOutcome.PROVIDER_ERROR),
    ),
)
def test_yahoo_history_adapter_preserves_distinct_transport_outcomes(
    monkeypatch,
    transport_result,
    expected: PhysicalAttemptOutcome,
) -> None:
    class FakeTicker:
        def history(self, **_kwargs):
            if isinstance(transport_result, BaseException):
                raise transport_result
            return transport_result

    monkeypatch.setattr(y_finance.yf, "Ticker", lambda _symbol: FakeTicker())

    with pytest.raises(PhysicalAttemptFailure) as captured:
        y_finance.load_ohlcv_range(
            "BTC-USD",
            "2026-07-01",
            "2026-07-24",
        )

    assert captured.value.outcome is expected


@pytest.mark.unit
def test_shared_yahoo_call_path_delegates_its_complete_budget_to_coordinator(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    calls: list[int] = []

    def transport() -> str:
        calls.append(len(calls) + 1)
        if len(calls) < 4:
            raise ConnectionError("deterministic disconnect")
        return "available"

    result = stockstats_utils.yf_retry(
        transport,
        request_key="news:BTC-USD:2026-07-24",
        operation="news",
        injected_transport=True,
    )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        rows = tuple(
            store._connection.execute(
                "SELECT sequence_id FROM provider_request_attempts "
                "WHERE operation = 'news'"
            )
        )
        assert len(rows) == 4
        events = ProviderRequestCoordinator(store).physical_attempt_events(
            str(rows[0][0])
        )

    assert result == "available"
    assert calls == [1, 2, 3, 4]
    assert [event.outcome for event in events] == [
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.AVAILABLE,
    ]


@pytest.mark.unit
def test_low_level_yahoo_session_blocks_hidden_second_http_call_per_permit(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    class Response:
        status_code = 200
        headers = {}

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

        def post(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    session = Session()
    guard = stockstats_utils._YahooSessionAttemptGuard(session)
    monkeypatch.setattr(stockstats_utils, "_yahoo_session_guard", lambda: guard)
    monkeypatch.setattr(
        stockstats_utils,
        "_activate_yahoo_session_guard",
        lambda _guard: None,
    )

    def high_level_history_call():
        session.get("https://query1.finance.yahoo.com/crumb")
        return session.get("https://query1.finance.yahoo.com/chart/BTC-USD")

    with pytest.raises(PhysicalAttemptFailure) as captured:
        stockstats_utils.yf_retry(
            high_level_history_call,
            request_key="low-level-hidden-retry",
            operation="market-snapshot",
        )

    assert captured.value.outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert session.calls == 4
    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        attempts = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
    assert attempts == (4,)


@pytest.mark.unit
def test_low_level_yahoo_429_retry_after_is_typed_without_cookie_retry(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    class Response:
        status_code = 429
        headers = {"Retry-After": "60"}

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

        post = get

    session = Session()
    guard = stockstats_utils._YahooSessionAttemptGuard(session)
    monkeypatch.setattr(stockstats_utils, "_yahoo_session_guard", lambda: guard)
    monkeypatch.setattr(
        stockstats_utils,
        "_activate_yahoo_session_guard",
        lambda _guard: None,
    )

    def high_level_history_call():
        session.get("https://query1.finance.yahoo.com/chart/BTC-USD")
        return session.get("https://query2.finance.yahoo.com/chart/BTC-USD")

    with pytest.raises(Exception) as captured:
        stockstats_utils.yf_retry(
            high_level_history_call,
            request_key="low-level-rate-limit",
            operation="market-snapshot",
        )

    assert captured.value.status_code == 429
    assert captured.value.retry_after_seconds == 60
    assert session.calls == 1

    second_transport_calls = 0

    def second_transport():
        nonlocal second_transport_calls
        second_transport_calls += 1
        return "must-not-probe"

    with pytest.raises(Exception) as blocked:
        stockstats_utils.yf_retry(
            second_transport,
            request_key="fundamentals-during-shared-cooldown",
            operation="fundamentals",
            injected_transport=True,
        )

    assert getattr(blocked.value, "retry_after_seconds", None) is not None
    assert second_transport_calls == 0


@pytest.mark.unit
def test_yahoo_empty_physical_response_is_a_typed_attempt_before_tool_error(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    class EmptyTicker:
        def history(self, **_kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(y_finance.yf, "Ticker", lambda _symbol: EmptyTicker())

    with pytest.raises(y_finance.NoMarketDataError):
        y_finance.get_YFin_data_online(
            "BTC-USD",
            "2026-07-01",
            "2026-07-24",
        )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        attempts = store._connection.execute(
            "SELECT outcome, final_physical_attempt_count "
            "FROM provider_request_attempts"
        ).fetchall()

    assert attempts == [("empty_frame", 1)]


@pytest.mark.unit
def test_later_yahoo_attempts_are_merged_into_the_final_run_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "yfinance": market_snapshot.SnapshotProvider(
                lambda *_args: frame,
                "auto_adjusted",
            )
        },
    )

    with market_snapshot.authoritative_snapshot_run():
        snapshot = market_snapshot.get_authoritative_market_snapshot(
            "BTC-USD",
            "2026-07-01",
            "2026-07-24",
        )
        evidence = build_evidence_state(
            symbol="BTC-USD",
            identity={},
            snapshot=snapshot,
        )
        active_snapshot_events = tuple(
            market_snapshot._ACTIVE_SNAPSHOT_RUN.get().physical_attempt_events
        )
        market_snapshot.record_active_physical_attempt_events(active_snapshot_events)
        stockstats_utils.yf_retry(
            lambda: {"quoteType": "CRYPTOCURRENCY"},
            request_key="later-fundamentals",
            operation="fundamentals",
            injected_transport=True,
        )
        refreshed = market_snapshot.refresh_active_evidence_physical_attempts(
            evidence
        )

    assert evidence.market_snapshot is not None
    assert evidence.physical_attempt_count == 1
    assert evidence.market_snapshot.physical_attempt_count == 1
    assert (
        evidence.physical_attempt_events
        == evidence.market_snapshot.physical_attempt_events
    )
    assert refreshed.market_snapshot is not None
    assert refreshed.market_snapshot.physical_attempt_count == 2
    assert refreshed.physical_attempt_count == 2
    assert [
        event.outcome for event in refreshed.market_snapshot.physical_attempt_events
    ] == ["available", "available"]


@pytest.mark.unit
def test_failed_run_audit_retains_attempts_without_a_market_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    with market_snapshot.authoritative_snapshot_run():
        with pytest.raises(PhysicalAttemptFailure):
            stockstats_utils.yf_retry(
                lambda: (_ for _ in ()).throw(ConnectionError("disconnect")),
                request_key="failed-without-snapshot",
                operation="market-snapshot",
                injected_transport=True,
            )
        refreshed = market_snapshot.refresh_active_evidence_physical_attempts(
            build_evidence_state(symbol="BTC-USD", identity={}, snapshot=None)
        )
        audit = project_evidence_for_audit(
            refreshed,
            SourceArtifactManifest(),
        )

    assert refreshed.market_snapshot is None
    assert refreshed.physical_attempt_count == 1
    assert [event.outcome for event in refreshed.physical_attempt_events] == [
        "disconnect"
    ]
    assert audit.physical_attempt_count == 1


@pytest.mark.unit
def test_yahoo_download_empty_frame_is_typed_before_coordinator_success(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=1)
    runtime_config["data_cache_dir"] = str(tmp_path / "cache")
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(stockstats_utils.yf, "download", lambda *_args, **_kwargs: pd.DataFrame())

    with pytest.raises(stockstats_utils.NoMarketDataError):
        stockstats_utils.load_ohlcv("BTC-USD", "2026-07-24")

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        attempts = store._connection.execute(
            "SELECT outcome FROM provider_request_attempts"
        ).fetchall()

    assert attempts == [("empty_frame",)]


@pytest.mark.unit
def test_yahoo_cache_hit_does_not_consume_physical_attempt_budget_or_audit(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    class Session:
        def get(self, *_args, **_kwargs):
            raise AssertionError("cache hit must not make network I/O")

        post = get

    guard = stockstats_utils._YahooSessionAttemptGuard(Session())
    monkeypatch.setattr(stockstats_utils, "_yahoo_session_guard", lambda: guard)
    monkeypatch.setattr(
        stockstats_utils,
        "_activate_yahoo_session_guard",
        lambda _guard: None,
    )

    result = stockstats_utils.yf_retry(
        lambda: {"cached": True},
        request_key="cached-metadata",
        operation="instrument-identity",
    )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        attempts = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequences = store._connection.execute(
            "SELECT status, final_physical_attempt_count "
            "FROM provider_request_sequences"
        ).fetchall()

    assert result == {"cached": True}
    assert attempts == (0,)
    assert sequences == [("succeeded", 0)]


@pytest.mark.unit
def test_yahoo_zero_io_failure_retains_type_without_physical_attempt_audit(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_config = _runtime_config(tmp_path, max_attempts=4)
    monkeypatch.setattr(config_module, "_config", runtime_config)

    class Session:
        def get(self, *_args, **_kwargs):
            raise AssertionError("zero-I/O failure must not make a network call")

        post = get

    guard = stockstats_utils._YahooSessionAttemptGuard(Session())
    monkeypatch.setattr(stockstats_utils, "_yahoo_session_guard", lambda: guard)
    monkeypatch.setattr(
        stockstats_utils,
        "_activate_yahoo_session_guard",
        lambda _guard: None,
    )

    def empty_cached_frame():
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
            retryable=False,
            error_code="YAHOO_EMPTY_CACHED_FRAME",
        )

    with pytest.raises(PhysicalAttemptFailure) as captured:
        stockstats_utils.yf_retry(
            empty_cached_frame,
            request_key="empty-cached-frame",
            operation="market-snapshot",
        )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        attempts = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequences = store._connection.execute(
            "SELECT status, final_physical_attempt_count, failure_outcome "
            "FROM provider_request_sequences"
        ).fetchall()

    assert captured.value.outcome is PhysicalAttemptOutcome.EMPTY_FRAME
    assert attempts == (0,)
    assert sequences == [("failed", 0, "empty_frame")]
