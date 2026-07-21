"""StockTwits public symbol-stream fetcher.

StockTwits exposes a per-symbol message stream at
``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` that requires no
API key, no OAuth, and no registration. Each message includes a
user-labeled sentiment field (``Bullish``/``Bearish``/null), the message
body, timestamp, and posting user.

The function is deliberately self-contained: short timeout, graceful
degradation on any HTTP or parse failure, and a string return type so
the calling agent gets a uniform interface regardless of whether the
network call succeeded.
"""

from __future__ import annotations

import http.client
import json
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tradingagents.dataflows.acquisition import (
    AcquisitionController,
    AcquisitionFailure,
    AcquisitionRequest,
)
from tradingagents.dataflows.market_snapshot import get_active_acquisition_controller
from tradingagents.evidence import AcquisitionUnavailableReason

from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"


def _stocktwits_symbol(ticker: str) -> str:
    """Map a crypto pair to StockTwits' ``<BASE>.X`` convention.

    StockTwits lists crypto as ``BTC.X`` (Yahoo's ``BTC-USD`` form 404s), so any
    crypto symbol resolves to its base plus ``.X``; other symbols pass through
    upper-cased.
    """
    base = crypto_base(ticker)
    return f"{base}.X" if base else ticker.strip().upper()


def _fetch_stocktwits_messages_raw(
    ticker: str, limit: int = 30, timeout: float = 10.0
) -> str:
    """Fetch and format StockTwits messages, preserving transport failures."""
    url = _API.format(ticker=_stocktwits_symbol(ticker))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())

    messages = data.get("messages", []) if isinstance(data, dict) else []
    if not messages:
        return f"<no StockTwits messages found for ${ticker.upper()}>"

    lines = []
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        created = m.get("created_at", "")
        user = (m.get("user") or {}).get("username", "?")
        entities = m.get("entities") or {}
        sentiment_obj = entities.get("sentiment") or {}
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        body = (m.get("body") or "").replace("\n", " ").strip()
        if len(body) > 280:
            body = body[:280] + "…"

        if sentiment == "Bullish":
            bullish += 1
            tag = "Bullish"
        elif sentiment == "Bearish":
            bearish += 1
            tag = "Bearish"
        else:
            unlabeled += 1
            tag = "no-label"
        lines.append(f"[{created} · @{user} · {tag}] {body}")

    total = bullish + bearish + unlabeled
    bull_pct = round(100 * bullish / total) if total else 0
    bear_pct = round(100 * bearish / total) if total else 0
    summary = (
        f"Bullish: {bullish} ({bull_pct}%) · "
        f"Bearish: {bearish} ({bear_pct}%) · "
        f"Unlabeled: {unlabeled} · "
        f"Total: {total} most-recent messages"
    )
    return summary + "\n\n" + "\n".join(lines)


def fetch_stocktwits_messages(ticker: str, limit: int = 30, timeout: float = 10.0) -> str:
    """Fetch recent StockTwits messages with legacy graceful degradation."""
    try:
        return _fetch_stocktwits_messages_raw(ticker, limit=limit, timeout=timeout)
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return f"<stocktwits unavailable: {type(exc).__name__}>"


def _retry_after_seconds(error: HTTPError) -> float | None:
    raw_value = error.headers.get("Retry-After") if error.headers else None
    try:
        value = float(raw_value) if raw_value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is None or value >= 0 else None


def acquire_stocktwits_messages(
    ticker: str,
    limit: int,
    *,
    tool_call_id: str,
    source_ref: str,
    capability: str,
):
    """Acquire StockTwits through the run-owned controller when available."""

    def provider(_request: AcquisitionRequest) -> str:
        try:
            value = _fetch_stocktwits_messages_raw(ticker, limit=limit)
            if value == f"<no StockTwits messages found for ${ticker.upper()}>":
                raise AcquisitionFailure(
                    reason=AcquisitionUnavailableReason.NO_DATA
                )
            return value
        except HTTPError as error:
            if error.code == 429:
                raise AcquisitionFailure(
                    reason=AcquisitionUnavailableReason.RATE_LIMITED,
                    status_code=error.code,
                    retry_after_seconds=_retry_after_seconds(error),
                ) from None
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
                status_code=error.code,
            ) from None
        except TimeoutError:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.TIMEOUT
            ) from None
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.PROVIDER_ERROR
            ) from None

    controller = get_active_acquisition_controller() or AcquisitionController(
        providers=()
    )

    def validate(value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE)

    return controller.acquire(
        AcquisitionRequest(
            capability=capability,
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name="fetch_stocktwits_messages",
        ),
        providers=(("stocktwits", provider),),
        validator=validate,
        serializer=lambda value: value,
    )
