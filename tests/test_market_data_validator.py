"""Tests for the deterministic market-data verification snapshot (#830/#881)."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.market_data_validator as validator
from tradingagents.dataflows.market_snapshot import AuthoritativeMarketSnapshot


def _sample_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", "2026-05-20")
    closes = [100 + i for i in range(len(dates))]
    return pd.DataFrame({
        "Date": dates,
        "Open": [c - 0.5 for c in closes],
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + i for i in range(len(dates))],
    })


def _ohlcv_with_rows(rows: int) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-05-20", periods=rows)
    closes = [100.0 + index for index in range(rows)]
    return pd.DataFrame({
        "Date": dates,
        "Open": [close - 0.5 for close in closes],
        "High": [close + 1.0 for close in closes],
        "Low": [close - 1.0 for close in closes],
        "Close": closes,
        "Volume": [1_000_000 + index for index in range(rows)],
    })


@pytest.mark.unit
class TestVerifiedSnapshot:
    def test_excludes_future_rows(self, monkeypatch):
        data = pd.concat([
            _sample_ohlcv(),
            pd.DataFrame({"Date": [pd.Timestamp("2026-06-01")], "Open": [999.0],
                          "High": [999.0], "Low": [999.0], "Close": [999.0], "Volume": [999]}),
        ], ignore_index=True)
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: data)

        snap = validator.build_verified_market_snapshot("COF", "2026-05-13")
        assert "Verified market data snapshot for COF" in snap
        assert "Requested analysis date: 2026-05-13" in snap
        assert "Latest trading row used: 2026-05-13" in snap
        assert "999.00" not in snap          # future row excluded
        assert "boll_lb" in snap             # indicators present

    def test_uses_previous_trading_day_when_date_is_weekend(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        # 2026-05-16 is a Saturday; latest row should be Fri 2026-05-15
        snap = validator.build_verified_market_snapshot("COF", "2026-05-16")
        assert "Latest trading row used: 2026-05-15" in snap
        assert "Recent verified closes" in snap

    def test_raises_when_no_rows_on_or_before_date(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2020-01-01")

    def test_raises_on_empty_data(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: pd.DataFrame())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2026-05-13")

    def test_look_back_window_capped_at_30(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20", look_back_days=999)
        # last-N closes table has at most 30 data rows
        close_rows = [ln for ln in snap.splitlines() if ln.startswith("| 2026-")]
        assert 0 < len(close_rows) <= 30

    def test_china_snapshot_exposes_immutable_authoritative_identity(self, monkeypatch):
        frame = _sample_ohlcv()
        snapshot_id = f"snapshot:{'b' * 64}"
        authoritative = AuthoritativeMarketSnapshot(
            symbol="600895.SS",
            frame=frame,
            provider="baostock",
            retrieved_at="2026-05-20T12:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-05-20",
            effective_trading_date="2026-05-20",
            frame_sha256="a" * 64,
            snapshot_id=snapshot_id,
        )
        monkeypatch.setattr(validator, "resolve_china_a_symbol", lambda _: object())
        monkeypatch.setattr(
            validator,
            "get_authoritative_market_snapshot",
            lambda *args: authoritative,
        )

        rendered = validator.build_verified_market_snapshot(
            "600895.SS", "2026-05-20"
        )

        assert f"History rows: {len(frame)}" in rendered
        assert "Frame SHA-256: " + "a" * 64 in rendered
        assert f"Snapshot ID: {snapshot_id}" in rendered

    def test_insufficient_history_never_emits_partial_window_indicator(self, monkeypatch):
        frame = _ohlcv_with_rows(129)
        monkeypatch.setattr(validator, "load_ohlcv", lambda *_: frame)

        rendered = validator.build_verified_market_snapshot(
            "COF",
            "2026-05-20",
            indicators=("close_50_sma", "close_200_sma"),
        )

        indicator_rows = {
            line.split("|")[1].strip(): line.split("|")[2].strip()
            for line in rendered.splitlines()
            if line.startswith("| close_")
        }
        assert indicator_rows["close_50_sma"] != "N/A"
        assert indicator_rows["close_200_sma"] == (
            "N/A: insufficient history (129 rows available; 200 required)"
        )


@pytest.mark.unit
class TestTool:
    def test_tool_delegates_to_builder(self, monkeypatch):
        from tradingagents.agents.utils.market_data_validation_tools import (
            get_verified_market_snapshot,
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        out = get_verified_market_snapshot.invoke(
            {"symbol": "COF", "curr_date": "2026-05-20"}
        )
        assert "Verified market data snapshot for COF" in out
