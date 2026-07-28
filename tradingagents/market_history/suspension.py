from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tradingagents.market_history.models import (
    RawMarketObservation,
    TradingStatus,
    TradingStatusObservation,
)


@dataclass(frozen=True)
class NormalizedMainlandSession:
    observation: RawMarketObservation
    status: TradingStatusObservation


def normalize_mainland_session(
    *,
    session_date: date,
    open_value: Decimal | None,
    high_value: Decimal | None,
    low_value: Decimal | None,
    close_value: Decimal | None,
    volume: Decimal | None,
    authoritative_status: TradingStatus | None,
    official_carried_close: Decimal | None,
) -> NormalizedMainlandSession:
    if authoritative_status is None:
        raise ValueError("authoritative trading status is required")
    if authoritative_status is TradingStatus.SUSPENDED:
        if (
            official_carried_close is None
            or not isinstance(official_carried_close, Decimal)
            or not official_carried_close.is_finite()
        ):
            raise ValueError("confirmed suspension requires an official carried close")
        prices = (open_value, high_value, low_value, close_value)
        if any(value is not None and value != official_carried_close for value in prices):
            raise ValueError("suspension prices conflict with the official carried close")
        if volume not in (None, Decimal("0")):
            raise ValueError("confirmed suspension volume must be blank or zero")
        observation = RawMarketObservation(
            session_date=session_date,
            open=official_carried_close,
            high=official_carried_close,
            low=official_carried_close,
            close=official_carried_close,
            volume=Decimal("0"),
        )
        status = TradingStatusObservation(
            session_date=session_date,
            status=TradingStatus.SUSPENDED,
            official_carried_close=official_carried_close,
            volume=Decimal("0"),
        )
        return NormalizedMainlandSession(observation, status)

    values = (open_value, high_value, low_value, close_value, volume)
    if any(value is None for value in values):
        raise ValueError("traded session market values must not be blank")
    assert all(value is not None for value in values)
    observation = RawMarketObservation(
        session_date=session_date,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume,
    )
    status = TradingStatusObservation(
        session_date=session_date,
        status=TradingStatus.TRADED,
        volume=volume,
    )
    return NormalizedMainlandSession(observation, status)
