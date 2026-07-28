from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.agents.utils.core_stock_tools import get_stock_data
from tradingagents.agents.utils.technical_indicators_tools import get_indicators
from tradingagents.dataflows import (
    akshare_data,
    baostock_data,
    interface,
    market_data_validator,
    market_snapshot,
    stockstats_utils,
    y_finance,
)
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.market_snapshot import (
    AuthoritativeMarketSnapshot,
    SnapshotProvider,
    authoritative_snapshot_run,
    build_authoritative_indicator_window,
    get_authoritative_market_snapshot,
)
from tradingagents.evidence import AcquisitionUnavailableReason, SourceArtifact
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history import TradingStatus, TradingStatusProvenance


def _valid_history(rows: int) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-07-17", periods=rows)
    return pd.DataFrame({
        "Date": dates,
        "Open": [10.0] * rows,
        "High": [10.5] * rows,
        "Low": [9.5] * rows,
        "Close": [10.0] * rows,
        "Volume": [1_000_000] * rows,
    })


@pytest.mark.unit
def test_live_status_and_explicit_unknown_have_distinct_snapshot_v2_identities() -> None:
    frame = _valid_history(1)
    raw_text = frame.to_csv(index=False)
    artifact = SourceArtifact(
        artifact_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
        source_ref="acq.v1:market:test-live-status",
        tool_call_id="live-status",
        tool_name="fixture_market_snapshot",
        raw_text=raw_text,
    )
    common = {
        "symbol": "600519.SS",
        "frame": frame,
        "provider": "baostock",
        "retrieved_at": "2026-07-24T10:00:00+00:00",
        "adjustment_basis": "qfq",
        "requested_date": "2026-07-24",
        "effective_trading_date": frame.iloc[-1]["Date"].strftime("%Y-%m-%d"),
        "source_artifact": artifact,
    }
    unknown = AuthoritativeMarketSnapshot(**common)
    traded = AuthoritativeMarketSnapshot(
        **common,
        current_tradeability="tradeable",
        current_status_provenance=TradingStatusProvenance(
            provider="baostock",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            session_date=date.fromisoformat(common["effective_trading_date"]),
            status=TradingStatus.TRADED,
            observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
            revision_id="trading-status=sha256:" + "1" * 64,
        ),
        latest_traded_close=Decimal("10"),
    )

    assert unknown.snapshot_id.startswith("snapshot:v2:")
    assert traded.snapshot_id.startswith("snapshot:v2:")
    assert unknown.snapshot_id != traded.snapshot_id
    assert unknown.snapshot_id_version == traded.snapshot_id_version == "v2"
    assert unknown.pin_membership_digest != traded.pin_membership_digest
    unknown_manifest = json.loads(unknown.snapshot_manifest_json)
    assert unknown_manifest["adjustment_basis"] == "qfq"
    assert unknown_manifest["instrument"] == {
        "canonical_symbol": "600519.SS",
        "currency": "CNY",
        "identity_revision": "mainland-routing-v1",
        "instrument_id": unknown_manifest["instrument"]["instrument_id"],
        "instrument_kind": "equity",
        "reference_market": "shanghai",
    }
    assert "asset_configuration" not in unknown_manifest
    assert unknown_manifest["provider"]["provider_dataset_id"].startswith(
        "provider-dataset=sha256:"
    )
    assert "dataset" not in unknown_manifest["provider"]


@pytest.mark.unit
def test_snapshot_preserves_rate_limit_before_ordered_secondary_success(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "primary,secondary"},
        }
    })

    def rate_limited(*_args):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
            retry_after_seconds=3,
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "primary": SnapshotProvider(rate_limited, "qfq"),
            "secondary": SnapshotProvider(lambda *_args: _valid_history(10), "qfq"),
        },
    )

    with authoritative_snapshot_run():
        snapshot = get_authoritative_market_snapshot(
            "510500.SS", "2026-06-01", "2026-07-18"
        )

    assert snapshot.provider == "secondary"
    assert [outcome.outcome for outcome in snapshot.acquisition_outcomes] == [
        "unavailable",
        "available",
    ]
    primary = snapshot.acquisition_outcomes[0]
    assert primary.provider == "primary"
    assert primary.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert primary.retry_after_seconds == 3
    assert snapshot.acquisition_outcomes[1].provider == "secondary"
    assert snapshot.source_artifact is snapshot.acquisition_outcomes[1].artifact
    assert snapshot.source_artifact.tool_name == (
        "authoritative_market_snapshot_normalized_frame_v1"
    )
    assert snapshot.source_artifact.artifact_sha256 == snapshot.frame_sha256


@pytest.mark.unit
def test_snapshot_selects_largest_valid_short_candidate_without_malforming_it(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "first,largest,last"},
        }
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "first": SnapshotProvider(lambda *_args: _valid_history(8), "qfq"),
            "largest": SnapshotProvider(lambda *_args: _valid_history(12), "qfq"),
            "last": SnapshotProvider(lambda *_args: _valid_history(12), "qfq"),
        },
    )

    with authoritative_snapshot_run():
        snapshot = get_authoritative_market_snapshot(
            "510500.SS",
            "2026-06-01",
            "2026-07-18",
            minimum_history_rows=20,
        )

    assert snapshot.provider == "largest"
    assert len(snapshot.frame) == 12
    assert [outcome.outcome for outcome in snapshot.acquisition_outcomes] == [
        "available",
        "available",
        "available",
    ]
    assert "largest" not in {item.provider for item in snapshot.quarantined}


@pytest.mark.unit
def test_snapshot_run_circuit_survives_request_range_variants(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "primary,secondary"},
        }
    })
    primary_calls = []

    def rate_limited(*args):
        primary_calls.append(args)
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "primary": SnapshotProvider(rate_limited, "qfq"),
            "secondary": SnapshotProvider(lambda *_args: _valid_history(10), "qfq"),
        },
    )

    with authoritative_snapshot_run():
        get_authoritative_market_snapshot(
            "510500.SS", "2026-06-01", "2026-07-18"
        )
        second = get_authoritative_market_snapshot(
            "510500.SS", "2026-05-01", "2026-07-18"
        )

    assert len(primary_calls) == 1
    assert second.acquisition_outcomes[0].provider == "primary"
    assert second.acquisition_outcomes[0].reason is (
        AcquisitionUnavailableReason.CIRCUIT_OPEN
    )
    assert second.acquisition_outcomes[1].provider == "secondary"
    assert second.acquisition_outcomes[1].outcome == "available"


@pytest.mark.unit
def test_unknown_configured_snapshot_provider_does_not_erase_later_known_provider(
    monkeypatch,
):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "unknown,known"},
        }
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"known": SnapshotProvider(lambda *_args: _valid_history(10), "qfq")},
    )

    with authoritative_snapshot_run():
        snapshot = get_authoritative_market_snapshot(
            "510500.SS", "2026-06-01", "2026-07-18"
        )

    assert snapshot.provider == "known"
    assert snapshot.acquisition_outcomes[0].provider == "unknown"
    assert snapshot.acquisition_outcomes[0].reason is (
        AcquisitionUnavailableReason.NOT_CONFIGURED
    )
    assert snapshot.acquisition_outcomes[0].retryable is False
    assert snapshot.acquisition_outcomes[0].http_status is None
    assert snapshot.acquisition_outcomes[1].provider == "known"
    assert snapshot.acquisition_outcomes[1].outcome == "available"


@pytest.mark.unit
def test_insufficient_history_falls_through_to_next_provider(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "short,long"},
        }
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "short": SnapshotProvider(lambda *args: _valid_history(129), "qfq"),
            "long": SnapshotProvider(lambda *args: _valid_history(250), "auto_adjusted"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "510500.SS",
        "2021-07-18",
        "2026-07-18",
        minimum_history_rows=200,
    )

    assert snapshot.provider == "long"
    assert len(snapshot.frame) == 250
    assert snapshot.quarantined[0].provider == "short"
    assert "129 rows available; 200 required" in snapshot.quarantined[0].reason


@pytest.mark.unit
def test_accepted_129_row_snapshot_is_authoritative_without_a_200_period_calculation(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {"cn_a": {"core_stock_apis": "baostock"}}
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"baostock": SnapshotProvider(lambda *args: _valid_history(129), "qfq")},
    )

    snapshot = get_authoritative_market_snapshot(
        "510500.SS", "2021-07-18", "2026-07-18"
    )

    assert len(snapshot.frame) == 129
    assert snapshot.provider == "baostock"
    assert snapshot.quarantined == ()


@pytest.mark.unit
def test_snapshot_id_and_frame_digest_are_stable_and_rendered(monkeypatch):
    frame = _valid_history(129)
    snapshot = AuthoritativeMarketSnapshot(
        symbol="510500.SS", frame=frame, provider="baostock",
        retrieved_at="2026-07-18T08:00:00+00:00", adjustment_basis="qfq",
        requested_date="2026-07-18", effective_trading_date="2026-07-17",
    )
    same_snapshot = AuthoritativeMarketSnapshot(
        symbol="510500.SS", frame=frame.copy(), provider="baostock",
        retrieved_at="2026-07-18T09:00:00+00:00", adjustment_basis="qfq",
        requested_date="2026-07-18", effective_trading_date="2026-07-17",
    )
    monkeypatch.setattr(
        market_snapshot, "get_authoritative_market_snapshot", lambda *args, **kwargs: snapshot
    )

    rendered = build_authoritative_indicator_window("510500.SS", "rsi", "2026-07-18", 1)

    assert snapshot.frame_sha256 == same_snapshot.frame_sha256
    assert snapshot.snapshot_id == same_snapshot.snapshot_id
    assert f"Frame SHA-256: {snapshot.frame_sha256}" in rendered
    assert f"Snapshot ID: {snapshot.snapshot_id}" in rendered


@pytest.mark.unit
def test_indicator_requires_a_complete_warmup_window(monkeypatch):
    frame = _valid_history(129)
    snapshot = AuthoritativeMarketSnapshot(
        symbol="510500.SS",
        frame=frame,
        provider="baostock",
        retrieved_at="2026-07-18T08:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-18",
        effective_trading_date="2026-07-17",
    )
    monkeypatch.setattr(
        market_snapshot,
        "get_authoritative_market_snapshot",
        lambda *args, **kwargs: snapshot,
    )

    rendered = build_authoritative_indicator_window(
        "510500.SS",
        "close_200_sma",
        "2026-07-18",
        2,
    )

    assert "N/A: insufficient history (129 rows available; 200 required)" in rendered
    assert "2026-07-17: 10.0" not in rendered


@pytest.mark.unit
def test_default_vwma_uses_stockstats_fourteen_row_window(monkeypatch):
    frame = _valid_history(13)
    snapshot = AuthoritativeMarketSnapshot(
        symbol="510500.SS",
        frame=frame,
        provider="baostock",
        retrieved_at="2026-07-18T08:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-18",
        effective_trading_date="2026-07-17",
    )
    monkeypatch.setattr(
        market_snapshot,
        "get_authoritative_market_snapshot",
        lambda *args, **kwargs: snapshot,
    )

    rendered = build_authoritative_indicator_window(
        "510500.SS",
        "vwma",
        "2026-07-18",
        2,
    )

    assert "N/A: insufficient history (13 rows available; 14 required)" in rendered


@pytest.mark.unit
def test_indicator_history_requirement_triggers_provider_fallback(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "short,long"},
        }
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "short": SnapshotProvider(lambda *args: _valid_history(129), "qfq"),
            "long": SnapshotProvider(
                lambda *args: _valid_history(250),
                "auto_adjusted",
            ),
        },
    )

    rendered = build_authoritative_indicator_window(
        "510500.SS",
        "close_200_sma",
        "2026-07-18",
        2,
    )

    assert "Provider: long" in rendered
    assert "2026-07-17: 10.0" in rendered


@pytest.mark.unit
def test_impossible_primary_row_is_quarantined_and_next_provider_is_accepted(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare,baostock"}
        }
    })
    impossible = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [12.60],
        "Low": [13.01],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    accepted = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": SnapshotProvider(lambda *args: impossible, "qfq"),
            "baostock": SnapshotProvider(lambda *args: accepted, "qfq"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "baostock"
    assert snapshot.adjustment_basis == "qfq"
    assert snapshot.requested_date == "2026-07-15"
    assert snapshot.effective_trading_date == "2026-07-15"
    assert snapshot.frame["Close"].tolist() == [12.58]
    assert len(snapshot.quarantined) == 1
    assert snapshot.quarantined[0].provider == "akshare"
    assert "Low <= Open <= High" in snapshot.quarantined[0].reason
    assert snapshot.quarantined[0].row_indices == (0,)


@pytest.mark.unit
def test_missing_required_ohlcv_column_has_explicit_policy(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    missing_volume = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: missing_volume, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "missing required columns: Volume" in str(exc.value)


@pytest.mark.unit
def test_invalid_dates_are_rejected_explicitly(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    invalid_date = pd.DataFrame({
        "Date": ["not-a-date"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: invalid_date, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "invalid Date at row(s) 0" in str(exc.value)


@pytest.mark.unit
def test_negative_volume_is_rejected(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    negative_volume = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [-1],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: negative_volume, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "negative Volume at row(s) 0" in str(exc.value)


@pytest.mark.unit
def test_duplicate_trading_dates_are_rejected_as_conflicted(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    duplicate_dates = pd.DataFrame({
        "Date": ["2026-07-15", "2026-07-15"],
        "Open": [12.55, 12.60],
        "High": [13.10, 13.10],
        "Low": [12.40, 12.40],
        "Close": [12.58, 12.65],
        "Volume": [1_000_000, 900_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: duplicate_dates, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "duplicate trading date 2026-07-15 at row(s) 0, 1" in str(exc.value)


@pytest.mark.unit
def test_stale_provider_frame_is_rejected(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    stale = pd.DataFrame({
        "Date": ["2026-06-30"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: stale, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-06-01", "2026-07-15")

    assert "latest row is 2026-06-30, 15 days before requested date 2026-07-15 (stale)" in str(exc.value)


@pytest.mark.unit
def test_non_numeric_ohlcv_value_is_rejected_after_coercion(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    non_numeric = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": ["not-a-price"],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: non_numeric, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "non-numeric Open at row(s) 0" in str(exc.value)


@pytest.mark.unit
def test_non_finite_ohlcv_value_is_rejected(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "akshare"}})
    non_finite = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [float("inf")],
        "High": [float("inf")],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(lambda *args: non_finite, "qfq")},
    )

    with pytest.raises(NoMarketDataError) as exc:
        get_authoritative_market_snapshot("AAPL", "2026-07-01", "2026-07-15")

    assert "non-finite Open at row(s) 0" in str(exc.value)


@pytest.mark.unit
def test_public_stock_data_router_falls_back_when_akshare_ohlcv_is_impossible(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare,baostock"}
        },
        "china_a_enhancement_preset": "basic",
    })
    impossible = pd.DataFrame({
        "日期": ["2026-07-15"],
        "开盘": [12.55],
        "最高": [12.60],
        "最低": [13.01],
        "收盘": [12.58],
        "成交量": [1_000_000],
        "成交额": [12_580_000],
    })
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: impossible,
    )
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "akshare",
        akshare_data.get_stock_data,
    )
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "baostock",
        lambda *args: "BAOSTOCK_VALID_FRAME",
    )

    result = interface.route_to_vendor(
        "get_stock_data", "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert result == "BAOSTOCK_VALID_FRAME"


@pytest.mark.unit
def test_production_akshare_snapshot_records_full_provenance(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare"}
        }
    })
    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "2026-07-15,12.55,12.58,13.10,12.40,"
                        "1000000,12580000,0,0,0,0"
                    ]
                }
            }

    monkeypatch.setattr(akshare_data.requests, "get", lambda *args, **kwargs: Response())

    snapshot = get_authoritative_market_snapshot(
        "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "akshare"
    assert snapshot.adjustment_basis == "qfq"
    assert snapshot.requested_date == "2026-07-15"
    assert snapshot.effective_trading_date == "2026-07-15"
    assert snapshot.retrieved_at.endswith("+00:00")


@pytest.mark.unit
def test_production_baostock_snapshot_uses_same_adjustment_basis(monkeypatch):
    class Login:
        error_code = "0"
        error_msg = ""

    class Query:
        error_code = "0"
        error_msg = ""

        def __init__(self):
            self._rows = [[
                "2026-07-15", "sz.000021", "12.55", "13.10", "12.40",
                "12.58", "1000000", "12580000",
            ]]
            self._index = -1

        def next(self):
            self._index += 1
            return self._index < len(self._rows)

        def get_row_data(self):
            return self._rows[self._index]

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "baostock"}
        }
    })
    monkeypatch.setattr(baostock_data.bs, "login", lambda: Login())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: Query(),
    )

    snapshot = get_authoritative_market_snapshot(
        "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "baostock"
    assert snapshot.adjustment_basis == "qfq"
    assert snapshot.frame["Close"].tolist() == [12.58]


@pytest.mark.unit
def test_production_yahoo_snapshot_records_auto_adjustment_basis(monkeypatch):
    class Ticker:
        def __init__(self, symbol):
            assert symbol == "000021.SZ"

        def history(self, **kwargs):
            return pd.DataFrame(
                {
                    "Open": [12.55],
                    "High": [13.10],
                    "Low": [12.40],
                    "Close": [12.58],
                    "Volume": [1_000_000],
                },
                index=pd.DatetimeIndex(["2026-07-15"], name="Date"),
            )

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "yfinance"}
        }
    })
    monkeypatch.setattr(y_finance.yf, "Ticker", Ticker)

    snapshot = get_authoritative_market_snapshot(
        "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "yfinance"
    assert snapshot.adjustment_basis == "auto_adjusted"
    assert snapshot.frame["Close"].tolist() == [12.58]


@pytest.mark.unit
def test_mainland_index_never_uses_the_akshare_equity_endpoint(monkeypatch):
    equity_calls = []

    def wrong_equity_history(**kwargs):
        equity_calls.append(kwargs)
        return pd.DataFrame({
            "日期": ["2026-07-15"],
            "开盘": [10.0],
            "最高": [10.5],
            "最低": [9.5],
            "收盘": [10.1],
            "成交量": [1_000_000],
            "成交额": [10_100_000],
        })

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "000001.SS"

        def history(self, **kwargs):
            return pd.DataFrame(
                {
                    "Open": [3500.0],
                    "High": [3550.0],
                    "Low": [3480.0],
                    "Close": [3525.0],
                    "Volume": [500_000_000],
                },
                index=pd.DatetimeIndex(["2026-07-15"], name="Date"),
            )

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare,yfinance"}
        }
    })
    monkeypatch.setattr(akshare_data.ak, "stock_zh_a_hist", wrong_equity_history)
    monkeypatch.setattr(y_finance.yf, "Ticker", Ticker)

    snapshot = get_authoritative_market_snapshot(
        "000001.SH", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "yfinance"
    assert snapshot.frame["Close"].tolist() == [3525.0]
    assert equity_calls == []
    assert snapshot.quarantined[0].provider == "akshare"
    assert "does not use the equity endpoint" in snapshot.quarantined[0].reason


@pytest.mark.unit
def test_china_indicator_tool_derives_from_accepted_core_snapshot(monkeypatch):
    class Login:
        error_code = "0"
        error_msg = ""

    class Query:
        error_code = "0"
        error_msg = ""

        def __init__(self):
            self._rows = [[
                "2026-07-15", "sz.000725", "12.55", "13.10", "12.40",
                "12.58", "1000000", "12580000",
            ]]
            self._index = -1

        def next(self):
            self._index += 1
            return self._index < len(self._rows)

        def get_row_data(self):
            return self._rows[self._index]

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {
                "core_stock_apis": "akshare,baostock",
                "technical_indicators": "akshare",
            }
        }
    })
    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "2026-07-15,12.55,999,12.60,13.01,"
                        "1000000,999000000,0,0,0,0"
                    ]
                }
            }

    monkeypatch.setattr(
        akshare_data.requests,
        "get",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(baostock_data.bs, "login", lambda: Login())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: Query(),
    )

    result = get_indicators.invoke({
        "symbol": "000725.SZ",
        "indicator": "close_10_ema",
        "curr_date": "2026-07-15",
        "look_back_days": 1,
    })

    assert "Provider: baostock" in result
    assert "Adjustment basis: qfq" in result
    assert "999" not in result
    assert "N/A: insufficient history (1 rows available; 10 required)" in result


@pytest.mark.unit
def test_public_yahoo_stock_data_rejects_impossible_ohlc(monkeypatch):
    class Ticker:
        def __init__(self, symbol):
            assert symbol == "000021.SZ"

        def history(self, **kwargs):
            return pd.DataFrame(
                {
                    "Open": [12.55],
                    "High": [12.60],
                    "Low": [13.01],
                    "Close": [12.58],
                    "Volume": [1_000_000],
                },
                index=pd.DatetimeIndex(["2026-07-15"], name="Date"),
            )

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "yfinance"}
        },
        "china_a_enhancement_preset": "basic",
    })
    monkeypatch.setattr(y_finance.yf, "Ticker", Ticker)
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "yfinance",
        y_finance.get_YFin_data_online,
    )

    result = interface.route_to_vendor(
        "get_stock_data", "000021.SZ", "2026-07-01", "2026-07-15"
    )

    assert result.startswith("NO_DATA_AVAILABLE:")
    assert "OHLC invariant failed" in result


@pytest.mark.unit
def test_snapshot_excludes_rows_after_requested_date(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
    includes_future = pd.DataFrame({
        "Date": ["2026-07-15", "2026-07-16"],
        "Open": [12.55, 99.0],
        "High": [13.10, 100.0],
        "Low": [12.40, 98.0],
        "Close": [12.58, 99.5],
        "Volume": [1_000_000, 1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(lambda *args: includes_future, "auto_adjusted")},
    )

    snapshot = get_authoritative_market_snapshot(
        "AAPL", "2026-07-01", "2026-07-15"
    )

    assert snapshot.effective_trading_date == "2026-07-15"
    assert snapshot.frame["Date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-15"]


@pytest.mark.unit
def test_verified_china_snapshot_cannot_mix_legacy_yahoo_frame(monkeypatch, tmp_path):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "data_cache_dir": str(tmp_path),
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "baostock"}
        }
    })
    accepted = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"baostock": SnapshotProvider(lambda *args: accepted, "qfq")},
    )
    monkeypatch.setattr(
        stockstats_utils.yf,
        "download",
        lambda *args, **kwargs: pd.DataFrame(
            {
                "Open": [999.0],
                "High": [999.0],
                "Low": [999.0],
                "Close": [999.0],
                "Volume": [999],
            },
            index=pd.DatetimeIndex(["2026-07-15"], name="Date"),
        ),
    )

    rendered = market_data_validator.build_verified_market_snapshot(
        "000725.SZ", "2026-07-15", indicators=("close_10_ema",)
    )

    assert "Provider: baostock" in rendered
    assert "Adjustment basis: qfq" in rendered
    assert "| Close | 12.58 |" in rendered
    assert "999.00" not in rendered


@pytest.mark.unit
def test_china_stock_data_tool_renders_same_authoritative_provider(monkeypatch):
    class Login:
        error_code = "0"
        error_msg = ""

    class Query:
        error_code = "0"
        error_msg = ""

        def __init__(self):
            self._rows = [[
                "2026-07-15", "sz.000725", "12.55", "13.10", "12.40",
                "12.58", "1000000", "12580000",
            ]]
            self._index = -1

        def next(self):
            self._index += 1
            return self._index < len(self._rows)

        def get_row_data(self):
            return self._rows[self._index]

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare,baostock"}
        }
    })
    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "2026-07-15,12.55,999,12.60,13.01,"
                        "1000000,999000000,0,0,0,0"
                    ]
                }
            }

    monkeypatch.setattr(
        akshare_data.requests,
        "get",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(baostock_data.bs, "login", lambda: Login())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: Query(),
    )

    rendered = get_stock_data.invoke({
        "symbol": "000725.SZ",
        "start_date": "2026-07-01",
        "end_date": "2026-07-15",
    })

    assert "Provider: baostock" in rendered
    assert "Adjustment basis: qfq" in rendered
    assert "Effective trading date: 2026-07-15" in rendered
    assert "999" not in rendered
    assert "2026-07-15,12.55,13.1,12.4,12.58,1000000" in rendered


@pytest.mark.unit
def test_analysis_run_reuses_one_snapshot_for_prices_and_indicators(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare"}
        }
    })
    provider_calls = []

    def changing_provider(*args):
        provider_calls.append(args)
        close = 12.58 if len(provider_calls) == 1 else 99.0
        dates = pd.bdate_range(end="2026-07-15", periods=10)
        return pd.DataFrame({
            "Date": dates,
            "Open": [12.55] * len(dates),
            "High": [100.0] * len(dates),
            "Low": [12.40] * len(dates),
            "Close": [close] * len(dates),
            "Volume": [1_000_000] * len(dates),
        })

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(changing_provider, "qfq")},
    )

    with authoritative_snapshot_run():
        prices = get_stock_data.invoke({
            "symbol": "000725.SZ",
            "start_date": "2026-07-01",
            "end_date": "2026-07-15",
        })
        indicator = get_indicators.invoke({
            "symbol": "000725.SZ",
            "indicator": "close_10_ema",
            "curr_date": "2026-07-15",
            "look_back_days": 1,
        })

    assert len(provider_calls) == 1
    assert "2026-07-15,12.55,100.0,12.4,12.58,1000000" in prices
    assert "2026-07-15: 12.58" in indicator
    assert "99" not in indicator


@pytest.mark.unit
def test_programmatic_analysis_opens_an_authoritative_snapshot_run(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare"}
        }
    })
    provider_calls = []

    def changing_provider(*args):
        provider_calls.append(args)
        dates = pd.bdate_range(end="2026-07-15", periods=10)
        return pd.DataFrame({
            "Date": dates,
            "Open": [12.55] * len(dates),
            "High": [13.10] * len(dates),
            "Low": [12.40] * len(dates),
            "Close": [12.58] * len(dates),
            "Volume": [1_000_000] * len(dates),
        })

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"akshare": SnapshotProvider(changing_provider, "qfq")},
    )

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {}
    graph._checkpointer_ctx = None
    graph._resolve_pending_entries = lambda ticker: None

    def run_graph(*args, **kwargs):
        get_stock_data.invoke({
            "symbol": "000725.SZ",
            "start_date": "2026-07-01",
            "end_date": "2026-07-15",
        })
        get_indicators.invoke({
            "symbol": "000725.SZ",
            "indicator": "close_10_ema",
            "curr_date": "2026-07-15",
            "look_back_days": 1,
        })
        return "complete"

    graph._run_graph = run_graph

    assert graph.propagate("000725.SZ", "2026-07-15") == "complete"
    assert len(provider_calls) == 1


@pytest.mark.unit
def test_production_akshare_cannot_silently_drop_invalid_date_row(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "akshare,baostock"}
        }
    })
    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {
                "data": {
                    "klines": [
                        "not-a-date,12.50,12.60,13.00,12.40,"
                        "900000,11000000,0,0,0,0",
                        "2026-07-15,12.55,12.58,13.10,12.40,"
                        "1000000,12580000,0,0,0,0",
                    ]
                }
            }

    accepted = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(akshare_data.requests, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": market_snapshot.SNAPSHOT_PROVIDERS["akshare"],
            "baostock": SnapshotProvider(lambda *args: accepted, "qfq"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "000725.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "baostock"
    assert snapshot.quarantined[0].reason == "malformed_response"
    assert snapshot.physical_attempt_events[0].outcome.value == "malformed_response"


@pytest.mark.unit
def test_production_baostock_cannot_silently_drop_invalid_date_row(monkeypatch):
    class Login:
        error_code = "0"
        error_msg = ""

    class Query:
        error_code = "0"
        error_msg = ""

        def __init__(self):
            self._rows = [
                ["not-a-date", "sz.000725", "12.5", "13", "12.4", "12.6", "900000", "11000000"],
                ["2026-07-15", "sz.000725", "12.55", "13.1", "12.4", "12.58", "1000000", "12580000"],
            ]
            self._index = -1

        def next(self):
            self._index += 1
            return self._index < len(self._rows)

        def get_row_data(self):
            return self._rows[self._index]

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({
        "market_data_vendors": {
            "cn_a": {"core_stock_apis": "baostock,yfinance"}
        }
    })
    monkeypatch.setattr(baostock_data.bs, "login", lambda: Login())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: Query(),
    )
    accepted = pd.DataFrame({
        "Date": ["2026-07-15"],
        "Open": [12.55],
        "High": [13.10],
        "Low": [12.40],
        "Close": [12.58],
        "Volume": [1_000_000],
    })
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": market_snapshot.SNAPSHOT_PROVIDERS["baostock"],
            "yfinance": SnapshotProvider(lambda *args: accepted, "auto_adjusted"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "000725.SZ", "2026-07-01", "2026-07-15"
    )

    assert snapshot.provider == "yfinance"
    assert "invalid Date at row(s) 0" in snapshot.quarantined[0].reason
