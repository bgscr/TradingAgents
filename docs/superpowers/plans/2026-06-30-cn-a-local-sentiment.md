# China A-Share Local Sentiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop treating StockTwits/Reddit failures as China A-share sentiment gaps and add AKShare-backed local sentiment/context blocks for A-share sentiment analysis.

**Architecture:** Keep the Sentiment Analyst prefetch design, but make source selection market-aware. China A-share tickers still receive configured ticker news, then get a local AKShare supplement from Eastmoney popularity, stock comment, and沪深港通 holding data; StockTwits and Reddit become explicit not-applicable placeholders for China A-shares and are not fetched.

**Tech Stack:** Python, pandas, AKShare, pytest, existing symbol resolver and sentiment analyst.

## Global Constraints

- No paid keys and no Tushare.
- AKShare remains the primary China A-share source.
- Baostock remains narrow fallback only; do not use it for sentiment in this task.
- `601138.SS`, `601138.SH`, and bare `601138` must be treated as the same China A-share instrument.
- Non-China tickers must keep the current StockTwits/Reddit behavior.
- Do not add broad refactors or change the LLM graph.
- Use TDD: write failing tests before production edits.

---

### Task 1: Add AKShare Local A-Share Sentiment Formatter

**Files:**
- Create: `tradingagents/dataflows/china_sentiment.py`
- Create: `tests/test_china_sentiment.py`

**Interfaces:**
- Consumes: `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
- Produces: `get_china_a_local_sentiment(ticker: str, start_date: str, end_date: str) -> str`.

- [ ] **Step 1: Write failing tests**

Create tests that monkeypatch AKShare functions and assert the formatter includes:

- `Source: AKShare/Eastmoney`
- popularity rank history from `stock_hot_rank_detail_em`
- stock comment data from `stock_comment_em`
-沪深港通 holding rows from `stock_hsgt_individual_em`
- `DATA_DEGRADED` lines when an optional endpoint raises

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_china_sentiment.py -q
```

Expected: FAIL because `tradingagents.dataflows.china_sentiment` does not exist.

- [ ] **Step 3: Implement formatter**

Create a small module with defensive AKShare calls. Each optional endpoint must degrade into text, not raise, because sentiment analysis should continue with partial local context.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_china_sentiment.py -q
```

Commit:

```bash
rtk git add tradingagents/dataflows/china_sentiment.py tests/test_china_sentiment.py
rtk git commit -m "feat: add China A-share local sentiment data"
```

### Task 2: Make Sentiment Analyst Market-Aware

**Files:**
- Modify: `tradingagents/agents/analysts/sentiment_analyst.py`
- Create: `tests/test_sentiment_analyst_china_a.py`

**Interfaces:**
- Consumes: `get_china_a_local_sentiment(ticker, start_date, end_date) -> str`.
- Produces: `_collect_sentiment_blocks(ticker, start_date, end_date) -> dict[str, str]` and an updated `_build_system_message(..., local_sentiment_block: str = "")`.

- [ ] **Step 1: Write failing tests**

Tests should assert:

- A-share tickers call configured news and local sentiment, but do not call StockTwits or Reddit.
- A-share blocks contain explicit `not applicable for China A-shares` placeholders.
- Non-China tickers still call StockTwits and Reddit and produce no local block.
- The system prompt says `configured news vendor` rather than hard-coded `Yahoo Finance`.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_sentiment_analyst_china_a.py -q
```

Expected: FAIL because helper functions and prompt text are not updated.

- [ ] **Step 3: Implement market-aware collection**

Add a helper that checks `resolve_china_a_symbol`. For China A-shares, return local AKShare block plus skipped StockTwits/Reddit placeholders. For all other tickers, preserve current behavior.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_sentiment_analyst_china_a.py tests/test_stocktwits_resilience.py tests/test_reddit_fallback.py -q
```

Commit:

```bash
rtk git add tradingagents/agents/analysts/sentiment_analyst.py tests/test_sentiment_analyst_china_a.py
rtk git commit -m "feat: use local sentiment sources for China A-shares"
```

### Task 3: Verification

**Files:**
- Test only.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: verification evidence.

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_china_sentiment.py tests/test_sentiment_analyst_china_a.py tests/test_stocktwits_resilience.py tests/test_reddit_fallback.py tests/test_symbol_utils.py tests/test_vendor_routing.py tests/test_akshare_data.py -q
```

- [ ] **Step 2: Smoke local block for `601138.SH`**

Run:

```bash
rtk .\.venv\Scripts\python.exe -c "from tradingagents.dataflows.china_sentiment import get_china_a_local_sentiment; print(get_china_a_local_sentiment('601138.SH','2026-06-22','2026-06-29')[:1200])"
```

Expected: Output contains `601138.SS`, `AKShare/Eastmoney`, and at least one local sentiment or degraded section.
