# A-Share Local Sentiment Cutoff Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent China A-share local sentiment from including rows outside the requested Beijing-date window.

**Architecture:** Keep the public local-sentiment interface unchanged and enforce the cutoff inside the existing shared date helper. Pass the requested window into the private stock-comment builder so all three dated local sources fail closed through the same inclusive calendar-date boundary.

**Tech Stack:** Python 3.10+, pandas, pytest, Ruff

## Global Constraints

- Treat requested start and end values as inclusive `Asia/Shanghai` (UTC+8) calendar dates.
- Compare AKShare date-only values as Beijing calendar dates without converting through the host timezone or UTC.
- Missing expected date columns and unparseable dates must fail closed; they must not return unfiltered rows.
- Preserve the public `get_china_a_local_sentiment(ticker, start_date, end_date)` signature.
- Preserve valid in-window output and allow unaffected optional sentiment sections to continue when one source degrades.
- Do not add dependencies, configuration, fallback sources, caching changes, or new exception types.
- Do not change other markets, CLI date selection, the candidate picker, or `china_a_enhancements.py`.
- Keep implementation changes limited to `tradingagents/dataflows/china_sentiment.py` and `tests/test_china_sentiment.py`.
- Complete Stage 1 spec-compliance review before Stage 2 code-quality review.

## File Structure

- `tradingagents/dataflows/china_sentiment.py`: owns optional AKShare/Eastmoney local sentiment collection, inclusive date filtering, degradation messages, and output formatting; modify the shared helper and stock-comment wiring only.
- `tests/test_china_sentiment.py`: owns deterministic local-sentiment behavior tests; add regressions for future stock-comment exclusion and missing-date fail-closed behavior.

---

### Task 1: Enforce the Beijing-date window for A-share local sentiment

**Files:**
- Modify: `tradingagents/dataflows/china_sentiment.py:32-44,76-100,149-153`
- Test: `tests/test_china_sentiment.py`

**Interfaces:**
- Consumes: `_filter_date_range(data: pd.DataFrame, date_col: str, start_date: str, end_date: str) -> pd.DataFrame`
- Produces: `_stock_comment_section(code: str, start_date: str, end_date: str) -> tuple[list[str], list[str]]`
- Preserves: `get_china_a_local_sentiment(ticker: str, start_date: str, end_date: str) -> str`

- [ ] **Step 1: Write the failing regressions**

Add these tests to `tests/test_china_sentiment.py` after
`test_get_china_a_local_sentiment_formats_local_sources`:

```python
@pytest.mark.unit
def test_stock_comment_after_beijing_cutoff_is_degraded(monkeypatch):
    monkeypatch.setattr(
        china_sentiment.ak,
        "stock_comment_em",
        lambda: pd.DataFrame({
            "代码": ["601138"],
            "名称": ["工业富联"],
            "最新价": [71.75],
            "交易日": ["2026-06-30"],
        }),
    )

    lines, errors = china_sentiment._stock_comment_section(
        "601138", "2026-06-22", "2026-06-29"
    )

    assert lines == []
    assert errors == [
        "DATA_DEGRADED: AKShare stock_comment_em returned no row for this "
        "symbol in requested window."
    ]


@pytest.mark.unit
def test_filter_date_range_fails_closed_without_expected_date_column():
    data = pd.DataFrame({"排名": [1], "证券代码": ["SH601138"]})

    result = china_sentiment._filter_date_range(
        data, "时间", "2026-06-22", "2026-06-29"
    )

    assert result.empty
    assert list(result.columns) == ["排名", "证券代码"]
```

- [ ] **Step 2: Run the regressions and confirm RED**

Run:

```powershell
rtk pytest -q tests/test_china_sentiment.py::test_stock_comment_after_beijing_cutoff_is_degraded tests/test_china_sentiment.py::test_filter_date_range_fails_closed_without_expected_date_column
```

Expected: two failures. The stock-comment test raises `TypeError` because the
private helper accepts only `code`; the missing-date test fails because the
current helper returns the unfiltered row.

- [ ] **Step 3: Implement the minimal fail-closed cutoff**

Replace `_filter_date_range()` with:

```python
def _filter_date_range(
    data: pd.DataFrame,
    date_col: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frame = data.copy()
    if date_col not in frame.columns:
        return frame.iloc[0:0]
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
    return frame[(dates >= start) & (dates < end)]
```

Change the stock-comment builder to accept and apply the requested window:

```python
def _stock_comment_section(
    code: str,
    start_date: str,
    end_date: str,
) -> tuple[list[str], list[str]]:
    data, error = _safe_frame("stock_comment_em", ak.stock_comment_em)
    if error:
        return [], [error]

    frame = data.copy()
    if "代码" not in frame.columns:
        return [], ["DATA_DEGRADED: AKShare stock_comment_em missing code column."]
    frame = frame[frame["代码"].astype(str).str.zfill(6) == code]
    frame = _filter_date_range(frame, "交易日", start_date, end_date)
    if frame.empty:
        return [], [
            "DATA_DEGRADED: AKShare stock_comment_em returned no row for this "
            "symbol in requested window."
        ]
    return _csv_section(
        "Eastmoney stock comment",
        frame,
        [
            "代码",
            "名称",
            "最新价",
            "机构参与度",
            "综合得分",
            "关注指数",
            "交易日",
        ],
        limit=1,
    ), []
```

Pass the window through the existing builder tuple:

```python
    for builder in (
        lambda: _popularity_section(eastmoney_symbol, start_date, end_date),
        lambda: _stock_comment_section(instrument.akshare_code, start_date, end_date),
        lambda: _northbound_section(instrument.akshare_code, start_date, end_date),
    ):
```

The helper compares source values and request boundaries as naive calendar
dates. It does not call `datetime.now()`, inspect the host timezone, localize to
UTC, or shift the date, so an A-share date remains the same Beijing calendar
date even when the process runs on a UTC-8 machine.

- [ ] **Step 4: Run the regressions and confirm GREEN**

Run:

```powershell
rtk pytest -q tests/test_china_sentiment.py::test_stock_comment_after_beijing_cutoff_is_degraded tests/test_china_sentiment.py::test_filter_date_range_fails_closed_without_expected_date_column
```

Expected: 2 passed, 0 failed.

- [ ] **Step 5: Run the complete local-sentiment test file**

Run:

```powershell
rtk pytest -q tests/test_china_sentiment.py
```

Expected: 6 passed, 0 failed. The baseline contains 4 tests and this task adds
2 regressions.

- [ ] **Step 6: Run scoped lint verification**

Run:

```powershell
rtk ruff check tradingagents/dataflows/china_sentiment.py tests/test_china_sentiment.py
```

Expected: no Ruff findings.

- [ ] **Step 7: Perform Stage 1 spec-compliance review**

Run:

```powershell
rtk git diff -- tradingagents/dataflows/china_sentiment.py tests/test_china_sentiment.py
```

Verify all of the following before proceeding:

- `_stock_comment_section()` accepts `code`, `start_date`, and `end_date`.
- The stock-comment `交易日` column uses the same inclusive helper as the other
  dated sources.
- A row dated after `end_date` cannot appear in the formatted section.
- A missing expected date column returns an empty frame with its columns
  preserved.
- In-window data still appears through the existing formatting test.
- `get_china_a_local_sentiment()` keeps its public signature.
- No non-A-share path or `china_a_enhancements.py` code changed.

If any item fails, correct it and rerun Steps 4-6 before Stage 2.

- [ ] **Step 8: Perform Stage 2 code-quality review**

Inspect the same diff and verify:

- no dependency, configuration, fallback, caching change, or new abstraction
  was introduced;
- the tests use deterministic DataFrames and no network access;
- degradation remains isolated to the affected optional section;
- code follows the existing pandas and error-message style;
- `rtk git diff --check` reports no whitespace errors.

Run:

```powershell
rtk git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 9: Commit the completed repair**

Run:

```powershell
rtk git add tradingagents/dataflows/china_sentiment.py tests/test_china_sentiment.py
rtk git commit -m "fix: enforce A-share sentiment cutoff"
```

Expected: one implementation commit containing only the production correction
and its two regressions.
