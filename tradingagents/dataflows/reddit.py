"""Reddit search fetcher for ticker-specific discussion posts.

Default path is Reddit's public Atom/RSS search feed
(``reddit.com/r/{sub}/search.rss``). The richer JSON search endpoint
(``/search.json``) is reliably WAF-blocked (``HTTP 403``) for public clients
(issue #862), and probing it on every call only doubled our request volume
against Reddit's per-IP rate limit — tripping ``429`` on the RSS fallback — so
it is kept (``_fetch_subreddit_json``) but not used by default. On a 429 we back
off once (honouring ``Retry-After``). RSS lacks score / comment counts, so those
posts are marked and the formatter omits the metrics rather than printing fake
zeros.

No API key required. Returns formatted plaintext blocks ready for prompt
injection and degrades gracefully — returns a placeholder string rather than
raising, so callers never special-case missing data.
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
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

_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
# A descriptive, identified User-Agent (per Reddit's API etiquette). Reddit
# blocks generic/anonymous tokens like bare "Mozilla/5.0" or "curl/…" but
# serves this one on both endpoints; the RSS feed accepts it even when the
# JSON search endpoint 403s, so no browser-spoofing is needed.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Default subreddits ordered roughly by signal density for ticker-specific
# discussion. wallstreetbets has the most volume but most noise; stocks /
# investing trend more measured. Caller can override.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")


def _search_qs(ticker: str, limit: int) -> str:
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # last 7 days
        "limit": limit,
    })


def _iso_to_timestamp(iso_str: str | None) -> float | None:
    """Parse an Atom ``published`` timestamp to a UTC epoch, or None."""
    if not iso_str:
        return None
    try:
        normalized = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _strip_html(content: str) -> str:
    """Reduce the HTML body Reddit embeds in an Atom entry to plain text."""
    if not content:
        return ""
    # Reddit wraps the real selftext between SC_OFF / SC_ON markers.
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """Return a nonnegative numeric ``Retry-After`` header when present."""
    try:
        val = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        value = float(val) if val is not None else None
        return value if value is None or value >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def _fetch_subreddit_rss_raw(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Parse one RSS feed while preserving transport and parse failures."""
    url = _RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA})
    with urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())

    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        posts.append({
            "title": (title_el.text if title_el is not None else "") or "",
            "score": None,
            "num_comments": None,
            "created_utc": _iso_to_timestamp(
                published_el.text if published_el is not None else None
            ),
            "selftext": _strip_html(content_el.text if content_el is not None else ""),
            "source": "rss",
        })
    return posts


def _fetch_subreddit_rss(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Legacy graceful RSS retrieval without local retry or sleeping."""
    try:
        return _fetch_subreddit_rss_raw(ticker, sub, limit, timeout)
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []


def _fetch_subreddit_json(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Richer JSON search path (carries score / comment counts).

    Reddit's WAF currently returns ``403 Blocked`` on this endpoint for
    non-OAuth clients (issue #862), so it is NOT used by default — calling it on
    every request only doubled our volume against the per-IP rate limit and
    triggered 429s on the RSS fallback. Kept for the day the WAF relaxes or an
    OAuth token is wired in; degrades to RSS on failure.
    """
    url = _API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        children = (payload.get("data") or {}).get("children") or []
        return [c.get("data", {}) for c in children if isinstance(c, dict)]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit JSON fetch failed for r/%s · %s: %s — falling back to RSS feed.",
            sub, ticker, exc,
        )
        return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Fetch one subreddit, RSS-first.

    The JSON search endpoint is reliably WAF-blocked (403) for public clients,
    so we go straight to the RSS feed — which serves our identified User-Agent
    reliably — halving our request volume against Reddit's per-IP rate limit.
    """
    return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _format_reddit_posts(
    ticker: str,
    subreddit_posts: Iterable[tuple[str, list[dict]]],
) -> str:
    blocks = []
    total_posts = 0
    subreddits = []
    for sub, posts in subreddit_posts:
        subreddits.append(sub)
        total_posts += len(posts)
        if not posts:
            blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
            continue

        via_rss = any(p.get("source") == "rss" for p in posts)
        header = f"r/{sub} — {len(posts)} recent posts mentioning {ticker.upper()}"
        header += " (via RSS feed; scores/comments unavailable):" if via_rss else ":"
        lines = [header]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score")
            comments = p.get("num_comments")
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            # Score / comment counts are absent on the RSS fallback path —
            # show them only when present rather than printing fake zeros.
            meta = created_str
            if score is not None and comments is not None:
                meta += f" · {score:>4}↑ · {comments:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        return (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
        )
    return "\n\n".join(blocks)


def _fetch_reddit_posts_raw(
    ticker: str,
    subreddits: tuple[str, ...],
    limit_per_sub: int,
    timeout: float,
) -> str:
    """Aggregate RSS results without converting provider failures to text."""
    ticker = crypto_base(ticker) or ticker
    return _format_reddit_posts(
        ticker,
        (
            (sub, _fetch_subreddit_rss_raw(ticker, sub, limit_per_sub, timeout))
            for sub in subreddits
        ),
    )


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,
) -> str:
    """Fetch Reddit posts with legacy graceful degradation and no local pacing."""
    del inter_request_delay
    normalized_ticker = crypto_base(ticker) or ticker
    subreddit_list = tuple(subreddits)
    return _format_reddit_posts(
        normalized_ticker,
        (
            (
                sub,
                _fetch_subreddit(normalized_ticker, sub, limit_per_sub, timeout),
            )
            for sub in subreddit_list
        ),
    )


def acquire_reddit_posts(
    ticker: str,
    subreddits: Iterable[str],
    limit_per_sub: int,
    *,
    tool_call_id: str,
    source_ref: str,
    capability: str,
):
    """Acquire aggregated Reddit RSS through the run-owned controller."""
    subreddit_list = tuple(subreddits)

    def provider(_request: AcquisitionRequest) -> str:
        try:
            value = _fetch_reddit_posts_raw(
                ticker, subreddit_list, limit_per_sub, timeout=10.0
            )
            if value.startswith("<no Reddit posts found mentioning "):
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
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.TIMEOUT) from None
        except ET.ParseError:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ) from None
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.PROVIDER_ERROR
            ) from None

    def validate(value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE)

    controller = get_active_acquisition_controller() or AcquisitionController(
        providers=()
    )
    return controller.acquire(
        AcquisitionRequest(
            capability=capability,
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name="fetch_reddit_posts",
        ),
        providers=(("reddit_rss", provider),),
        validator=validate,
        serializer=lambda value: value,
    )
