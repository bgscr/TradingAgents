import pytest

from tradingagents.agents.analysts import sentiment_analyst as sa


class FakeNewsTool:
    def __init__(self, response="NEWS"):
        self.calls = []
        self.response = response

    def func(self, ticker, start_date, end_date):
        self.calls.append((ticker, start_date, end_date))
        return self.response


@pytest.mark.unit
def test_collect_sentiment_blocks_for_china_a_skips_us_social_sources(monkeypatch):
    news = FakeNewsTool("Source: AKShare stock_news_em\nA-share news")
    monkeypatch.setattr(sa, "get_news", news)
    monkeypatch.setattr(
        sa,
        "fetch_stocktwits_messages",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("StockTwits should be skipped")),
    )
    monkeypatch.setattr(
        sa,
        "fetch_reddit_posts",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Reddit should be skipped")),
    )
    monkeypatch.setattr(
        sa,
        "get_china_a_local_sentiment",
        lambda ticker, start, end: "LOCAL A-SHARE SENTIMENT",
    )

    blocks = sa._collect_sentiment_blocks("601138.SH", "2026-06-22", "2026-06-29")

    assert news.calls == [("601138.SH", "2026-06-22", "2026-06-29")]
    assert blocks["news_block"].startswith("Source: AKShare")
    assert blocks["local_sentiment_block"] == "LOCAL A-SHARE SENTIMENT"
    assert "not applicable for China A-shares" in blocks["stocktwits_block"]
    assert "not applicable for China A-shares" in blocks["reddit_block"]


@pytest.mark.unit
def test_collect_sentiment_blocks_for_non_china_keeps_existing_social_sources(monkeypatch):
    news = FakeNewsTool("Yahoo news")
    monkeypatch.setattr(sa, "get_news", news)
    monkeypatch.setattr(sa, "fetch_stocktwits_messages", lambda ticker, limit=30: "STOCKTWITS")
    monkeypatch.setattr(sa, "fetch_reddit_posts", lambda ticker: "REDDIT")
    monkeypatch.setattr(
        sa,
        "get_china_a_local_sentiment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("local source should be skipped")),
    )

    blocks = sa._collect_sentiment_blocks("NVDA", "2026-06-22", "2026-06-29")

    assert news.calls == [("NVDA", "2026-06-22", "2026-06-29")]
    assert blocks["stocktwits_block"] == "STOCKTWITS"
    assert blocks["reddit_block"] == "REDDIT"
    assert blocks["local_sentiment_block"] == ""


@pytest.mark.unit
def test_system_message_uses_configured_news_source_and_local_china_section():
    msg = sa._build_system_message(
        ticker="601138.SH",
        start_date="2026-06-22",
        end_date="2026-06-29",
        news_block="Source: AKShare stock_news_em",
        stocktwits_block="<stocktwits skipped: not applicable for China A-shares>",
        reddit_block="<reddit skipped: not applicable for China A-shares>",
        local_sentiment_block="LOCAL A-SHARE SENTIMENT",
    )

    assert "Yahoo Finance, past 7 days" not in msg
    assert "configured news vendor" in msg
    assert "China A-share local sentiment" in msg
    assert "LOCAL A-SHARE SENTIMENT" in msg
    assert "not count it as a data failure" in msg


@pytest.mark.unit
def test_collect_sentiment_blocks_adds_china_flow_enhancement(monkeypatch):
    from tradingagents.dataflows.config import set_config

    set_config({"china_a_enhancement_preset": "flow_sentiment"})
    monkeypatch.setattr(sa.get_news, "func", lambda ticker, start, end: "NEWS")
    monkeypatch.setattr(sa, "get_china_a_local_sentiment", lambda ticker, start, end: "LOCAL")
    monkeypatch.setattr(
        sa,
        "get_china_a_enhancements_for_categories",
        lambda ticker, curr_date, preset, categories: "FLOW_SENTIMENT_APPENDIX",
    )

    blocks = sa._collect_sentiment_blocks("600895.SS", "2026-06-23", "2026-06-30")

    assert "LOCAL" in blocks["local_sentiment_block"]
    assert "FLOW_SENTIMENT_APPENDIX" in blocks["local_sentiment_block"]


@pytest.mark.unit
def test_collect_sentiment_blocks_ignores_enhancement_exception(monkeypatch):
    from tradingagents.dataflows.config import set_config

    set_config({"china_a_enhancement_preset": "flow_sentiment"})
    monkeypatch.setattr(sa.get_news, "func", lambda ticker, start, end: "NEWS")
    monkeypatch.setattr(sa, "get_china_a_local_sentiment", lambda ticker, start, end: "LOCAL")
    monkeypatch.setattr(
        sa,
        "get_china_a_enhancements_for_categories",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cache offline")),
    )

    blocks = sa._collect_sentiment_blocks("600895.SS", "2026-06-23", "2026-06-30")

    assert blocks["local_sentiment_block"] == "LOCAL"
