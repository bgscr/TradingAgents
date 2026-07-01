# A-share Data Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the China A-share workflow so candidate selection, optional enrichment, report assembly, and run artifacts expose data quality problems instead of hiding them.

**Architecture:** Keep the existing vendor routing and graph intact. Add narrow quality metadata at the candidate picker, enrichment snapshot, shared report writer, and CLI run-artifact boundaries.

**Tech Stack:** Python, pandas, AkShare, pytest, Typer CLI, LangGraph workflow state, existing `rtk` shell wrapper.

## Global Constraints

- Work only inside `D:\prj\TradingAgents\TradingAgents\.worktrees\a-share-data-quality` on branch `agent/a-share-data-quality`.
- Prefix shell commands with `rtk`.
- Use CodeGraph before structural code exploration; CodeGraph is initialized and `rtk codegraph status` reports the index is up to date.
- Do not change the LLM provider or model selection flow.
- Do not change trading strategy or final decision semantics.
- Do not introduce paid China market data providers.
- Do not rewrite the vendor registry.
- Keep per-section report files.
- Keep optional enrichment failures fail-open; keep core market-data failures loud.

---

## File Structure

- Modify `ak_pick_a_stock.py`: add valuation-column aliases, quality status columns, degraded warnings, and candidate scoring behavior for missing valuation.
- Modify `tests/test_ak_pick_a_stock.py`: cover valid PE filtering, row-level missing PE filtering, source-wide PE degradation, and existing source fallback behavior.
- Modify `tradingagents/dataflows/china_a_enhancements.py`: add section/snapshot status calculation and avoid normal TTL caching for all-failed snapshots.
- Modify `tests/test_china_a_enhancements.py`: cover `ok`, `partial`, `failed`, cache behavior, and degraded source rendering.
- Modify `tradingagents/reporting.py`: keep detailed report files but make `complete_report.md` summary-first with appendix links.
- Modify `tests/test_reporting.py`: verify summary-first ordering and no embedded full debate histories.
- Modify `cli/main.py`: add `run_status.json` helpers and update run phases during CLI execution.
- Create `tests/test_cli_run_status.py`: test run-status helpers without invoking the full interactive graph.

---

### Task 1: Candidate Quality Gate

**Files:**
- Modify: `ak_pick_a_stock.py`
- Modify: `tests/test_ak_pick_a_stock.py`

**Interfaces:**
- Consumes: `prepare_candidates(data: pd.DataFrame, source: str) -> pd.DataFrame`
- Produces:
  - `DYNAMIC_PE_COL: str`
  - `VALUATION_STATUS_COL: str`
  - `CANDIDATE_WARNING_COL: str`
  - `_resolve_valuation_column(data: pd.DataFrame) -> str | None`
  - `prepare_candidates(data: pd.DataFrame, source: str) -> pd.DataFrame` returning `valuation_data_status` and `candidate_warning`

- [ ] **Step 1: Write failing tests for row-level valuation filtering**

Add this test to `tests/test_ak_pick_a_stock.py`:

```python
def test_prepare_candidates_filters_missing_and_extreme_valuation_when_available():
    pe = picker.DYNAMIC_PE_COL
    data = pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "ValidCo",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 900_000_000,
                pe: 25.0,
            },
            {
                "代码": "600002",
                "名称": "MissingPE",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 950_000_000,
                pe: None,
            },
            {
                "代码": "600003",
                "名称": "ExpensiveCo",
                "最新价": 12.0,
                "涨跌幅": 2.0,
                "成交额": 1_000_000_000,
                pe: 120.0,
            },
        ]
    )

    result = picker.prepare_candidates(data, "unit_source")

    assert list(result["代码"]) == ["600001"]
    assert list(result[picker.VALUATION_STATUS_COL]) == ["ok"]
    assert list(result[picker.CANDIDATE_WARNING_COL]) == [""]
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
rtk pytest tests/test_ak_pick_a_stock.py::test_prepare_candidates_filters_missing_and_extreme_valuation_when_available -q
```

Expected: FAIL because `DYNAMIC_PE_COL`, `VALUATION_STATUS_COL`, and `CANDIDATE_WARNING_COL` do not exist.

- [ ] **Step 3: Write failing test for source-wide missing valuation**

Add this test to `tests/test_ak_pick_a_stock.py`:

```python
def test_main_marks_degraded_candidates_when_spot_source_lacks_valuation(
    monkeypatch, tmp_path, capsys
):
    def spot_without_pe():
        return pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1700,
                    "涨跌幅": 2.0,
                    "成交额": 1_200_000_000,
                },
                {
                    "代码": "sz000001",
                    "名称": "平安银行",
                    "最新价": 10,
                    "涨跌幅": -1.0,
                    "成交额": 500_000_000,
                },
            ]
        )

    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", spot_without_pe)
    monkeypatch.chdir(tmp_path)

    assert picker.main() == 0

    captured = capsys.readouterr()
    assert "valuation data unavailable from stock_zh_a_spot_em" in captured.out

    result = pd.read_csv(tmp_path / "ak_candidates.csv")
    assert set(result[picker.VALUATION_STATUS_COL]) == {"missing_source"}
    assert set(result[picker.CANDIDATE_WARNING_COL]) == {
        "valuation data unavailable from stock_zh_a_spot_em"
    }
```

- [ ] **Step 4: Run the source-wide missing valuation test and verify it fails**

Run:

```bash
rtk pytest tests/test_ak_pick_a_stock.py::test_main_marks_degraded_candidates_when_spot_source_lacks_valuation -q
```

Expected: FAIL because `main()` does not print a valuation warning and the CSV lacks the new metadata columns.

- [ ] **Step 5: Implement valuation constants and helper functions**

In `ak_pick_a_stock.py`, add these constants near the current column constants:

```python
CODE_COL = "\u4ee3\u7801"
NAME_COL = "\u540d\u79f0"
PRICE_COL = "\u6700\u65b0\u4ef7"
CHANGE_COL = "\u6da8\u8dcc\u5e45"
AMOUNT_COL = "\u6210\u4ea4\u989d"
TURNOVER_COL = "\u6362\u624b\u7387"
DYNAMIC_PE_COL = "\u5e02\u76c8\u7387-\u52a8\u6001"

VALUATION_COLUMNS = (
    DYNAMIC_PE_COL,
    "\u5e02\u76c8\u7387-TTM",
    "\u5e02\u76c8\u7387-\u9759\u6001",
    "PE(TTM)",
    "pe_ttm",
)
VALUATION_STATUS_COL = "valuation_data_status"
CANDIDATE_WARNING_COL = "candidate_warning"
```

Replace the existing column lists with:

```python
REQUIRED_COLS = [CODE_COL, NAME_COL, PRICE_COL, AMOUNT_COL]
NUMERIC_COLS = [PRICE_COL, CHANGE_COL, AMOUNT_COL, TURNOVER_COL, *VALUATION_COLUMNS]
DISPLAY_COLS = [
    CODE_COL,
    NAME_COL,
    PRICE_COL,
    CHANGE_COL,
    AMOUNT_COL,
    TURNOVER_COL,
    DYNAMIC_PE_COL,
    VALUATION_STATUS_COL,
    CANDIDATE_WARNING_COL,
    "score",
    "tradingagents_ticker",
]
```

Add these helpers below `load_spot_data()`:

```python
def _resolve_valuation_column(data: pd.DataFrame) -> str | None:
    for col in VALUATION_COLUMNS:
        if col in data.columns:
            return col
    return None


def _valuation_missing_warning(source: str) -> str:
    return f"valuation data unavailable from {source}"
```

- [ ] **Step 6: Implement valuation-aware candidate preparation**

Replace `prepare_candidates()` with:

```python
def prepare_candidates(data: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [col for col in REQUIRED_COLS if col not in data.columns]
    if missing:
        raise ValueError(f"{source} missing required columns: {', '.join(missing)}")

    valuation_col = _resolve_valuation_column(data)
    optional_cols = [CHANGE_COL, TURNOVER_COL]
    if valuation_col:
        optional_cols.append(valuation_col)
    keep_cols = [col for col in [*REQUIRED_COLS, *optional_cols] if col in data.columns]
    df = data[keep_cols].copy()
    df[CODE_COL] = df[CODE_COL].apply(normalize_stock_code)

    if valuation_col and valuation_col != DYNAMIC_PE_COL:
        df[DYNAMIC_PE_COL] = df[valuation_col]

    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[~df[NAME_COL].astype(str).str.contains("ST", case=False, na=False)]
    df = df[df[AMOUNT_COL] > 300_000_000]
    df = df[df[PRICE_COL] > 3]

    if valuation_col:
        df[VALUATION_STATUS_COL] = "ok"
        df[CANDIDATE_WARNING_COL] = ""
        missing_mask = df[DYNAMIC_PE_COL].isna()
        df.loc[missing_mask, VALUATION_STATUS_COL] = "missing_row"
        df.loc[missing_mask, CANDIDATE_WARNING_COL] = "valuation missing for row"
        df = df[(df[DYNAMIC_PE_COL] > 0) & (df[DYNAMIC_PE_COL] < 80)]
    else:
        df[DYNAMIC_PE_COL] = pd.NA
        df[VALUATION_STATUS_COL] = "missing_source"
        df[CANDIDATE_WARNING_COL] = _valuation_missing_warning(source)

    df["score"] = 0.0
    df["score"] += df[AMOUNT_COL].rank(pct=True) * 40

    if CHANGE_COL in df.columns:
        df["score"] += df[CHANGE_COL].between(0, 5).astype(int) * 30
        df["score"] -= df[CHANGE_COL].abs().rank(pct=True) * 10

    if TURNOVER_COL in df.columns:
        df["score"] += df[TURNOVER_COL].between(1, 8).astype(int) * 20

    if not valuation_col:
        df["score"] -= 25

    df["tradingagents_ticker"] = df[CODE_COL].apply(to_ta_ticker)
    return df.sort_values("score", ascending=False).head(10)
```

- [ ] **Step 7: Print degraded valuation warning in `main()`**

In `main()`, after `result = prepare_candidates(data, source)`, add:

```python
        if (
            VALUATION_STATUS_COL in result.columns
            and (result[VALUATION_STATUS_COL] == "missing_source").any()
        ):
            print(_valuation_missing_warning(source))
```

- [ ] **Step 8: Run candidate tests**

Run:

```bash
rtk pytest tests/test_ak_pick_a_stock.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit candidate gate**

Run:

```bash
rtk git add ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
rtk git commit -m "fix: surface A-share candidate valuation gaps"
```

Expected: commit succeeds.

---

### Task 2: A-share Enrichment Status and Cache Policy

**Files:**
- Modify: `tradingagents/dataflows/china_a_enhancements.py`
- Modify: `tests/test_china_a_enhancements.py`

**Interfaces:**
- Consumes: `SourceResult`, `_format_snapshot()`, `_read_cache()`, `_write_cache()`, `get_china_a_enhancements_for_categories()`
- Produces:
  - `STATUS_OK = "ok"`
  - `STATUS_PARTIAL = "partial"`
  - `STATUS_FAILED = "failed"`
  - `@dataclass(frozen=True) class SnapshotSection`
- `_section_status(results: Sequence[SourceResult]) -> str`
- `_snapshot_status(sections: Sequence[SnapshotSection]) -> str`
- `_should_cache_snapshot(sections: Sequence[SnapshotSection]) -> bool`

- [ ] **Step 1: Write failing test for all-failed snapshots not using normal cache**

Add this test to `tests/test_china_a_enhancements.py`:

```python
@pytest.mark.unit
def test_all_failed_enrichment_snapshot_is_not_cached(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    calls = {"fund_flow": 0}

    def fail_fund_flow(stock, market):
        calls["fund_flow"] += 1
        raise ValueError("shape mismatch")

    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", fail_fund_flow)
    monkeypatch.setattr(
        enh.ak,
        "stock_lhb_detail_em",
        lambda start_date, end_date: (_ for _ in ()).throw(ValueError("lhb offline")),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_margin_detail_sse",
        lambda date: (_ for _ in ()).throw(ValueError("margin offline")),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_rank_latest_em",
        lambda symbol: (_ for _ in ()).throw(ValueError("rank offline")),
    )
    monkeypatch.setattr(
        enh.ak,
        "stock_hot_keyword_em",
        lambda symbol: (_ for _ in ()).throw(ValueError("keyword offline")),
    )

    first = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")
    second = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "Overall status: failed" in first
    assert "Overall status: failed" in second
    assert calls["fund_flow"] == 2
```

- [ ] **Step 2: Run all-failed cache test and verify it fails**

Run:

```bash
rtk pytest tests/test_china_a_enhancements.py::test_all_failed_enrichment_snapshot_is_not_cached -q
```

Expected: FAIL because snapshots do not include `Overall status` and all-failed snapshots are cached.

- [ ] **Step 3: Write failing test for partial snapshots using cache with degraded details**

Add this test to `tests/test_china_a_enhancements.py`:

```python
@pytest.mark.unit
def test_partial_enrichment_snapshot_is_cached_with_status(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import china_a_enhancements as enh
    from tradingagents.dataflows.config import set_config

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    calls = {"fund_flow": 0}

    def fund_flow(stock, market):
        calls["fund_flow"] += 1
        return pd.DataFrame(
            {
                "date": ["2026-06-30"],
                "close": [41.2],
                "pct_change": [1.3],
                "main_net_inflow": [1200000],
                "main_net_ratio": [4.2],
            }
        )

    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", fund_flow)
    monkeypatch.setattr(
        enh.ak,
        "stock_lhb_detail_em",
        lambda start_date, end_date: (_ for _ in ()).throw(ValueError("lhb offline")),
    )
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_rank_latest_em", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_keyword_em", lambda symbol: pd.DataFrame())

    first = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")
    second = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert first == second
    assert "Overall status: partial" in first
    assert "Section status: partial" in first
    assert "Source unavailable: stock_lhb_detail_em" in first
    assert calls["fund_flow"] == 1
```

- [ ] **Step 4: Run partial cache test and verify it fails**

Run:

```bash
rtk pytest tests/test_china_a_enhancements.py::test_partial_enrichment_snapshot_is_cached_with_status -q
```

Expected: FAIL because `Overall status` and `Section status` are not rendered.

- [ ] **Step 5: Add status constants and `SnapshotSection`**

In `tradingagents/dataflows/china_a_enhancements.py`, add this import near the existing imports:

```python
from collections.abc import Sequence
```

Add after existing constants:

```python
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
```

Add below `SourceResult`:

```python
@dataclass(frozen=True)
class SnapshotSection:
    title: str
    results: Sequence[SourceResult]
    status: str
```

- [ ] **Step 6: Add status helper functions**

Add below `_format_result()`:

```python
def _source_result_is_ok(result: SourceResult) -> bool:
    return result.status == STATUS_OK and bool(result.records)


def _section_status(results: Sequence[SourceResult]) -> str:
    ok_count = sum(1 for result in results if _source_result_is_ok(result))
    if ok_count == 0:
        return STATUS_FAILED
    if ok_count < len(results):
        return STATUS_PARTIAL
    return STATUS_OK


def _snapshot_status(sections: Sequence[SnapshotSection]) -> str:
    if not sections:
        return STATUS_FAILED
    statuses = {section.status for section in sections}
    if statuses == {STATUS_OK}:
        return STATUS_OK
    if statuses == {STATUS_FAILED}:
        return STATUS_FAILED
    return STATUS_PARTIAL


def _should_cache_snapshot(sections: Sequence[SnapshotSection]) -> bool:
    return _snapshot_status(sections) != STATUS_FAILED
```

- [ ] **Step 7: Update `_format_snapshot()` to render status**

Change its signature to:

```python
def _format_snapshot(
    ticker: str,
    curr_date: str,
    preset: str,
    sections: Sequence[SnapshotSection],
) -> str:
```

In the header lines, add:

```python
        f"Overall status: {_snapshot_status(sections)}",
```

Replace the section loop with:

```python
    for section in sections:
        lines.append(f"### {section.title}")
        lines.append(f"Section status: {section.status}")
        source_line_count = 0
        for result in section.results:
            for line in _format_result(result):
                if line.startswith(SOURCE_LINE_PREFIXES):
                    if source_line_count >= section_source_limit:
                        continue
                    source_line_count += 1
                lines.append(line)
        lines.append("")
```

- [ ] **Step 8: Build `SnapshotSection` objects and skip cache for failed snapshots**

In `get_china_a_enhancements_for_categories()`, replace `sections` construction with this pattern:

```python
    sections: list[SnapshotSection] = []

    def add_section(title: str, results: list[SourceResult]) -> None:
        sections.append(
            SnapshotSection(
                title=title,
                results=tuple(results),
                status=_section_status(results),
            )
        )

    if CATEGORY_FLOW_SENTIMENT in allowed:
        add_section("Fund flow and trading activity", _collect_flow_sentiment(instrument, curr_date))
    if CATEGORY_ANNOUNCEMENTS in allowed:
        add_section("Announcements and disclosures", _collect_announcements(instrument, curr_date))
    if CATEGORY_INDUSTRY_POLICY in allowed:
        add_section(
            "Industry, sector, and policy context",
            _collect_industry_policy(instrument, curr_date),
        )

    text = _format_snapshot(instrument.yahoo_symbol, curr_date, preset, sections)
    if text and _should_cache_snapshot(sections):
        _write_cache(cache_path, text)
    return text
```

- [ ] **Step 9: Run enrichment tests**

Run:

```bash
rtk pytest tests/test_china_a_enhancements.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit enrichment status and cache policy**

Run:

```bash
rtk git add tradingagents/dataflows/china_a_enhancements.py tests/test_china_a_enhancements.py
rtk git commit -m "fix: avoid caching failed A-share enrichment"
```

Expected: commit succeeds.

---

### Task 3: Summary-first Consolidated Reports

**Files:**
- Modify: `tradingagents/reporting.py`
- Modify: `tests/test_reporting.py`

**Interfaces:**
- Consumes: `write_report_tree(final_state: dict, ticker: str, save_path) -> Path`
- Produces:
  - Detail files unchanged.
  - `complete_report.md` ordered as portfolio, trader, research manager, analysts, appendix.
  - Appendix links to detail files when those files exist.

- [ ] **Step 1: Write failing summary-first report test**

Replace `_state()` in `tests/test_reporting.py` with:

```python
def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {
            "bull_history": "BULL FULL HISTORY",
            "bear_history": "BEAR FULL HISTORY",
            "judge_decision": "RM PLAN",
        },
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {
            "aggressive_history": "AGGRESSIVE FULL HISTORY",
            "conservative_history": "CONSERVATIVE FULL HISTORY",
            "neutral_history": "NEUTRAL FULL HISTORY",
            "judge_decision": "PM DECISION",
        },
    }
```

Add this test:

```python
@pytest.mark.unit
def test_complete_report_is_summary_first_and_links_full_histories(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    complete = out.read_text()

    assert complete.index("## I. Portfolio Manager Decision") < complete.index(
        "## II. Trading Team Plan"
    )
    assert complete.index("## II. Trading Team Plan") < complete.index(
        "## III. Research Manager Decision"
    )
    assert complete.index("## III. Research Manager Decision") < complete.index(
        "## IV. Analyst Team Reports"
    )

    assert "PM DECISION" in complete
    assert "TRADE" in complete
    assert "RM PLAN" in complete
    assert "MKT" in complete
    assert "BULL FULL HISTORY" not in complete
    assert "BEAR FULL HISTORY" not in complete
    assert "AGGRESSIVE FULL HISTORY" not in complete
    assert "CONSERVATIVE FULL HISTORY" not in complete
    assert "NEUTRAL FULL HISTORY" not in complete

    assert "2_research/bull.md" in complete
    assert "2_research/bear.md" in complete
    assert "4_risk/aggressive.md" in complete
    assert "4_risk/conservative.md" in complete
    assert "4_risk/neutral.md" in complete
```

- [ ] **Step 2: Run summary-first report test and verify it fails**

Run:

```bash
rtk pytest tests/test_reporting.py::test_complete_report_is_summary_first_and_links_full_histories -q
```

Expected: FAIL because the complete report embeds full histories and portfolio appears after risk sections.

- [ ] **Step 3: Add small report helpers**

In `tradingagents/reporting.py`, add below imports:

```python
def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _appendix_entry(label: str, path: Path, root: Path) -> str:
    return f"- {label}: `{path.relative_to(root).as_posix()}`"
```

- [ ] **Step 4: Rewrite `write_report_tree()` assembly while preserving detail files**

Replace the internal `sections` assembly with this structure:

```python
    complete_sections = []
    appendix_entries = []

    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        path = analysts_dir / "market.md"
        _write_markdown(path, final_state["market_report"])
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        path = analysts_dir / "sentiment.md"
        _write_markdown(path, final_state["sentiment_report"])
        analyst_parts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        path = analysts_dir / "news.md"
        _write_markdown(path, final_state["news_report"])
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        path = analysts_dir / "fundamentals.md"
        _write_markdown(path, final_state["fundamentals_report"])
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))

    research_manager = None
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        if debate.get("bull_history"):
            path = research_dir / "bull.md"
            _write_markdown(path, debate["bull_history"])
            appendix_entries.append(_appendix_entry("Bull researcher full history", path, save_path))
        if debate.get("bear_history"):
            path = research_dir / "bear.md"
            _write_markdown(path, debate["bear_history"])
            appendix_entries.append(_appendix_entry("Bear researcher full history", path, save_path))
        if debate.get("judge_decision"):
            path = research_dir / "manager.md"
            _write_markdown(path, debate["judge_decision"])
            research_manager = debate["judge_decision"]

    trader_plan = final_state.get("trader_investment_plan")
    if trader_plan:
        _write_markdown(save_path / "3_trading" / "trader.md", trader_plan)

    portfolio_decision = None
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        if risk.get("aggressive_history"):
            path = risk_dir / "aggressive.md"
            _write_markdown(path, risk["aggressive_history"])
            appendix_entries.append(_appendix_entry("Aggressive analyst full history", path, save_path))
        if risk.get("conservative_history"):
            path = risk_dir / "conservative.md"
            _write_markdown(path, risk["conservative_history"])
            appendix_entries.append(_appendix_entry("Conservative analyst full history", path, save_path))
        if risk.get("neutral_history"):
            path = risk_dir / "neutral.md"
            _write_markdown(path, risk["neutral_history"])
            appendix_entries.append(_appendix_entry("Neutral analyst full history", path, save_path))
        if risk.get("judge_decision"):
            portfolio_decision = risk["judge_decision"]
            _write_markdown(save_path / "5_portfolio" / "decision.md", portfolio_decision)

    if portfolio_decision:
        complete_sections.append(
            f"## I. Portfolio Manager Decision\n\n### Portfolio Manager\n{portfolio_decision}"
        )
    if trader_plan:
        complete_sections.append(f"## II. Trading Team Plan\n\n### Trader\n{trader_plan}")
    if research_manager:
        complete_sections.append(
            f"## III. Research Manager Decision\n\n### Research Manager\n{research_manager}"
        )
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        complete_sections.append(f"## IV. Analyst Team Reports\n\n{content}")
    if appendix_entries:
        complete_sections.append("## Appendix: Full Debate Files\n\n" + "\n".join(appendix_entries))
```

Keep the header write at the end, but write `complete_sections`:

```python
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(
        header + "\n\n".join(complete_sections),
        encoding="utf-8",
    )
```

- [ ] **Step 5: Run reporting tests**

Run:

```bash
rtk pytest tests/test_reporting.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit report writer change**

Run:

```bash
rtk git add tradingagents/reporting.py tests/test_reporting.py
rtk git commit -m "fix: make complete reports summary first"
```

Expected: commit succeeds.

---

### Task 4: CLI Run Status Artifact

**Files:**
- Modify: `cli/main.py`
- Create: `tests/test_cli_run_status.py`

**Interfaces:**
- Consumes: `_prepare_run_artifacts(config: dict, selections: dict) -> dict[str, Path | str]`
- Produces:
  - `run_status.json` under each run directory.
  - `_write_run_status(status_file: Path, payload: dict) -> None`
  - `_update_run_status(artifacts: dict, **updates) -> None`
  - `_mark_run_failed(artifacts: dict | None, exc: Exception, current_phase: str) -> None`
  - `_write_run_reports(final_state: dict, ticker: str, artifacts: dict) -> Path`

- [ ] **Step 1: Write run-status helper tests**

Create `tests/test_cli_run_status.py`:

```python
import json

import pytest

from cli import main as cli_main


def _selections():
    return {
        "ticker": "688519.SS",
        "analysis_date": "2026-07-01",
        "asset_type": "stock",
        "analysts": ["market", "social"],
        "china_a_enhancement_preset": "all",
    }


@pytest.mark.unit
def test_prepare_run_artifacts_writes_running_status(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    status_path = artifacts["status_file"]
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["ticker"] == "688519.SS"
    assert payload["analysis_date"] == "2026-07-01"
    assert payload["selected_analysts"] == ["market", "social"]
    assert payload["status"] == "running"
    assert payload["current_phase"] == "artifacts_prepared"
    assert payload["completed_at"] is None
    assert payload["error_summary"] is None
    assert payload["reports_written"] == []


@pytest.mark.unit
def test_update_run_status_marks_completed(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._update_run_status(
        artifacts,
        status="completed",
        current_phase="report_writing",
        reports_written=["reports/complete_report.md"],
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["current_phase"] == "report_writing"
    assert payload["completed_at"] is not None
    assert payload["reports_written"] == ["reports/complete_report.md"]


@pytest.mark.unit
def test_mark_run_failed_records_error_summary(tmp_path):
    artifacts = cli_main._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        _selections(),
    )

    cli_main._mark_run_failed(
        artifacts,
        RuntimeError("stream stopped"),
        current_phase="graph_stream",
    )

    payload = json.loads(artifacts["status_file"].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["current_phase"] == "graph_stream"
    assert payload["completed_at"] is None
    assert payload["error_summary"] == "RuntimeError: stream stopped"
```

- [ ] **Step 2: Run run-status tests and verify they fail**

Run:

```bash
rtk pytest tests/test_cli_run_status.py -q
```

Expected: FAIL because the helpers and `status_file` key do not exist.

- [ ] **Step 3: Add JSON import and timestamp/status helpers**

In `cli/main.py`, add `import json` near the top-level imports.

Add these helpers above `_prepare_run_artifacts()`:

```python
def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _analyst_values(analysts) -> list[str]:
    return [analyst.value if hasattr(analyst, "value") else str(analyst) for analyst in analysts]


def _write_run_status(status_file: Path, payload: dict) -> None:
    status_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _read_run_status(status_file: Path) -> dict:
    return json.loads(status_file.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Update `_prepare_run_artifacts()` to create `run_status.json`**

Inside `_prepare_run_artifacts()`, after log files are written, add:

```python
    status_file = run_dir / "run_status.json"
    now = _now_iso()
    _write_run_status(
        status_file,
        {
            "run_id": run_id,
            "ticker": selections["ticker"],
            "analysis_date": selections["analysis_date"],
            "asset_type": selections["asset_type"],
            "selected_analysts": _analyst_values(selections["analysts"]),
            "china_a_enhancement_preset": selections.get(
                "china_a_enhancement_preset", "basic"
            ),
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "status": "running",
            "current_phase": "artifacts_prepared",
            "error_summary": None,
            "reports_written": [],
        },
    )
```

Add `"status_file": status_file` to the returned artifacts dict.

- [ ] **Step 5: Add update, failure, and run-report helpers**

Add below `_prepare_run_artifacts()`:

```python
def _update_run_status(artifacts: dict, **updates) -> None:
    status_file = artifacts["status_file"]
    payload = _read_run_status(status_file)
    payload.update(updates)
    payload["updated_at"] = _now_iso()
    if updates.get("status") == "completed":
        payload["completed_at"] = payload["updated_at"]
    _write_run_status(status_file, payload)


def _mark_run_failed(artifacts: dict | None, exc: Exception, current_phase: str) -> None:
    if artifacts is None:
        return
    try:
        _update_run_status(
            artifacts,
            status="failed",
            current_phase=current_phase,
            error_summary=f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        return


def _write_run_reports(final_state: dict, ticker: str, artifacts: dict) -> Path:
    report_file = save_report_to_disk(final_state, ticker, artifacts["report_dir"])
    _update_run_status(
        artifacts,
        status="completed",
        current_phase="report_writing",
        reports_written=[str(report_file)],
    )
    return report_file
```

- [ ] **Step 6: Integrate run-status updates into `run_analysis()`**

In `run_analysis()`, initialize `artifacts` and `current_phase` before creating artifacts:

```python
    artifacts = None
    current_phase = "setup"
```

After `_prepare_run_artifacts(config, selections)`, add:

```python
    current_phase = "graph_initializing"
    _update_run_status(artifacts, current_phase="graph_initializing")
```

Before streaming the graph, add:

```python
        current_phase = "graph_stream"
        _update_run_status(artifacts, current_phase="graph_stream")
```

After final report sections are updated and before leaving the successful analysis path, add:

```python
        current_phase = "report_writing"
        _update_run_status(artifacts, current_phase=current_phase)
        _write_run_reports(final_state, selections["ticker"], artifacts)
```

Wrap the `with Live(layout, refresh_per_second=4):` block and the in-run report write in a `try` block. Replace the current block opener:

```python
    with Live(layout, refresh_per_second=4):
        # Initial display
```

with:

```python
    try:
        with Live(layout, refresh_per_second=4):
            # Initial display
```

Indent the current `with Live` body one additional level. Immediately after the successful `_write_run_reports(final_state, selections["ticker"], artifacts)` line, add the exception handler at the same indentation level as `try`:

```python
    except Exception as exc:
        _mark_run_failed(artifacts, exc, current_phase=current_phase)
        raise
```

Keep the post-analysis optional "Save report?" prompt unchanged; that prompt writes an extra user-selected copy after the run directory report has already been written.

- [ ] **Step 7: Run run-status tests**

Run:

```bash
rtk pytest tests/test_cli_run_status.py -q
```

Expected: PASS.

- [ ] **Step 8: Run CLI-related tests**

Run:

```bash
rtk pytest tests/test_cli_run_status.py tests/test_cli_config_precedence.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit run-status artifact**

Run:

```bash
rtk git add cli/main.py tests/test_cli_run_status.py
rtk git commit -m "feat: write CLI run status artifacts"
```

Expected: commit succeeds.

---

### Task 5: Integration Verification and Review

**Files:**
- Verify: `ak_pick_a_stock.py`
- Verify: `tradingagents/dataflows/china_a_enhancements.py`
- Verify: `tradingagents/reporting.py`
- Verify: `cli/main.py`
- Verify: affected tests

**Interfaces:**
- Consumes: all task outputs.
- Produces: a clean branch with committed, tested changes.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
rtk pytest tests/test_ak_pick_a_stock.py tests/test_china_a_enhancements.py tests/test_reporting.py tests/test_cli_run_status.py tests/test_cli_config_precedence.py -q
```

Expected: PASS.

- [ ] **Step 2: Run wider relevant dataflow and reporting tests**

Run:

```bash
rtk pytest tests/test_vendor_routing.py tests/test_china_a_cli_enhancements.py tests/test_china_a_run_logging.py tests/test_dataflows_config.py -q
```

Expected: PASS.

- [ ] **Step 3: Check working tree**

Run:

```bash
rtk git status --short
```

Expected: no unstaged or staged changes.

- [ ] **Step 4: Review recent commits**

Run:

```bash
rtk git log --oneline -5
```

Expected: commits for candidate valuation, enrichment cache/status, summary-first reports, and run status are present above the design and plan commits.

- [ ] **Step 5: Perform two-stage review**

Spec compliance review:

- Candidate picker exposes valuation degradation and does not silently score missing row-level valuation as normal.
- A-share enrichment renders `ok`, `partial`, or `failed`.
- All-failed enrichment snapshots do not use normal cache.
- Consolidated reports are summary-first.
- CLI run directories contain `run_status.json`.

Code quality review:

- New helpers have narrow responsibilities.
- Optional enrichment remains fail-open.
- Core data no-data behavior is unchanged.
- Tests are focused and deterministic.
- No unrelated formatting or refactors are present.

If a review finding requires a code change, make that change with a failing test first, rerun the focused tests, and commit with a message that names the fixed issue.
