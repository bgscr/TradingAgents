"""yfinance-based news data fetching functions."""

import contextlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import yfinance as yf
from dateutil.relativedelta import relativedelta
from yfinance.exceptions import YFRateLimitError

from tradingagents.evidence import InstrumentIdentityEvidence

from .config import get_config
from .errors import NoMarketDataError, VendorRateLimitError
from .stockstats_utils import yf_retry
from .symbol_utils import normalize_symbol, resolve_china_a_symbol


def _related_symbols(article: Mapping[str, Any]) -> frozenset[str]:
    """Extract provider-declared instrument associations across Yahoo schemas."""

    values: list[object] = []
    values.extend(article.get("relatedTickers", ()) or ())
    content = article.get("content")
    if isinstance(content, Mapping):
        values.extend(content.get("relatedTickers", ()) or ())
        finance = content.get("finance")
        if isinstance(finance, Mapping):
            values.extend(finance.get("stockTickers", ()) or ())
    symbols: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("symbol") or value.get("ticker")
        if not isinstance(value, str) or not value.strip():
            continue
        with contextlib.suppress(ValueError, TypeError):
            symbols.add(normalize_symbol(value).upper())
    return frozenset(symbols)


def _instrument_aliases(
    ticker: str,
    canonical: str,
    identity: InstrumentIdentityEvidence | None,
) -> tuple[str, ...]:
    aliases = {ticker.strip(), canonical.strip()}
    china = resolve_china_a_symbol(canonical)
    if china is not None:
        aliases.update(
            {
                china.akshare_code,
                china.yahoo_symbol,
                f"{china.akshare_code}.SH"
                if china.yahoo_symbol.endswith(".SS")
                else f"{china.akshare_code}.SZ",
            }
        )
    if identity is not None:
        aliases.add(identity.symbol.strip())
        if identity.display_name:
            aliases.add(identity.display_name.strip())
    return tuple(sorted(alias for alias in aliases if alias))


def _alias_in_text(alias: str, text: str) -> bool:
    if any("\u3400" <= character <= "\u9fff" for character in alias):
        return alias.casefold() in text.casefold()
    return re.search(
        rf"(?<![A-Z0-9]){re.escape(alias.upper())}(?![A-Z0-9])",
        text.upper(),
    ) is not None


def _article_is_relevant(
    article: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    ticker: str,
    canonical: str,
    identity: InstrumentIdentityEvidence | None,
) -> bool:
    provider_symbols = _related_symbols(article)
    accepted_symbols = {
        normalize_symbol(alias).upper()
        for alias in _instrument_aliases(ticker, canonical, identity)
        if re.fullmatch(r"[A-Za-z0-9.=+\-]+", alias)
    }
    if provider_symbols:
        return not provider_symbols.isdisjoint(accepted_symbols)
    text = f"{data.get('title', '')}\n{data.get('summary', '')}"
    return any(
        _alias_in_text(alias, text)
        for alias in _instrument_aliases(ticker, canonical, identity)
    )


def _extract_article_data(article: dict) -> dict:
    """Extract article data from yfinance news format (handles nested 'content' structure)."""
    # Handle nested content structure
    if "content" in article:
        content = article["content"]
        title = content.get("title", "No title")
        summary = content.get("summary", "")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "Unknown")

        # Get URL from canonicalUrl or clickThroughUrl
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        link = url_obj.get("url", "")

        # Get publish date
        pub_date_str = content.get("pubDate", "")
        pub_date = None
        if pub_date_str:
            with contextlib.suppress(ValueError, AttributeError):
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

        return {
            "title": title,
            "summary": summary,
            "publisher": publisher,
            "link": link,
            "pub_date": pub_date,
        }
    else:
        # Fallback for flat structure. Parse the epoch publish time so flat
        # articles are date-filterable too (otherwise they bypass the
        # historical window and leak future news, #992/#1007).
        pub_date = None
        ts = article.get("providerPublishTime")
        if ts:
            with contextlib.suppress(ValueError, OSError, TypeError):
                pub_date = datetime.fromtimestamp(ts)
        return {
            "title": article.get("title", "No title"),
            "summary": article.get("summary", ""),
            "publisher": article.get("publisher", "Unknown"),
            "link": article.get("link", ""),
            "pub_date": pub_date,
        }


def _in_news_window(pub_date, start_dt, end_dt) -> bool:
    """Whether an article belongs in the [start_dt, end_dt] window.

    Dated articles are kept only if they fall in the window. An undated article
    is kept only when the window reaches the present (live run) — in a
    historical/backtest window it's excluded, since we can't prove it isn't
    future news (look-ahead safety, #992/#1007).
    """
    if pub_date is not None:
        naive = pub_date.replace(tzinfo=None) if hasattr(pub_date, "replace") else pub_date
        return start_dt <= naive <= end_dt + relativedelta(days=1)
    return end_dt >= datetime.now() - relativedelta(days=1)


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    _acquired: bool = False,
    instrument_identity: InstrumentIdentityEvidence | None = None,
) -> str:
    """
    Retrieve news for a specific stock ticker using yfinance.

    Args:
        ticker: Stock ticker symbol (e.g., "AAPL")
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Formatted string containing news articles
    """
    article_limit = get_config()["news_article_limit"]
    # Query Yahoo with the canonical symbol, like every other yfinance path —
    # a raw broker/forex/crypto alias (XAUUSD, BTCUSD) otherwise silently
    # returns no news. Keep the user's ticker in the report header.
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    try:
        stock = yf.Ticker(canonical)
        if _acquired:
            try:
                news = stock.get_news(count=article_limit)
            except YFRateLimitError:
                raise VendorRateLimitError(status_code=429) from None
        else:
            news = yf_retry(lambda: stock.get_news(count=article_limit))

        if not news:
            if _acquired:
                raise NoMarketDataError(ticker, canonical, "Yahoo returned no news rows")
            return f"No news found for {ticker}{resolved}"

        # Parse date range for filtering
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        news_str = ""
        filtered_count = 0

        for article in news:
            data = _extract_article_data(article)

            # Keep only articles within the requested window (look-ahead safe).
            if not _in_news_window(data["pub_date"], start_dt, end_dt):
                continue
            if _acquired and not _article_is_relevant(
                article,
                data,
                ticker=ticker,
                canonical=canonical,
                identity=instrument_identity,
            ):
                continue

            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            filtered_count += 1

        if filtered_count == 0:
            if _acquired:
                raise NoMarketDataError(
                    ticker,
                    canonical,
                    "Yahoo returned no instrument-relevant news between "
                    f"{start_date} and {end_date}",
                )
            return f"No news found for {ticker}{resolved} between {start_date} and {end_date}"

        return f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n{news_str}"

    except Exception as e:
        if _acquired:
            raise
        return f"Error fetching news for {ticker}: {str(e)}"


def get_global_news_yfinance(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """
    Retrieve global/macro economic news using yfinance Search.

    Args:
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    search_queries = config["global_news_queries"]

    all_news = []
    seen_titles = set()

    try:
        for query in search_queries:
            search = yf_retry(lambda q=query: yf.Search(
                query=q,
                news_count=limit,
                enable_fuzzy_query=True,
            ))

            if search.news:
                for article in search.news:
                    # Handle both flat and nested structures
                    if "content" in article:
                        data = _extract_article_data(article)
                        title = data["title"]
                    else:
                        title = article.get("title", "")

                    # Deduplicate by title
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        all_news.append(article)

            if len(all_news) >= limit:
                break

        if not all_news:
            return f"No global news found for {curr_date}"

        # Calculate date range
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - relativedelta(days=look_back_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        news_str = ""
        kept = 0
        for article in all_news[:limit]:
            # Extract uniformly (flat + nested) and apply the same look-ahead-safe
            # window filter, so flat articles can't leak future news (#1007).
            data = _extract_article_data(article)
            if not _in_news_window(data["pub_date"], start_dt, curr_dt):
                continue
            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            kept += 1

        # All candidates fell outside the window -> say so rather than return an
        # empty-bodied report (#993).
        if kept == 0:
            return f"No global news found between {start_date} and {curr_date}"

        return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching global news: {str(e)}"
