# China A-Share Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add China A-share enhancement presets, Beijing-date validation, run-scoped logs, and bounded China-local data snapshots without changing the core TradingAgents graph.

**Architecture:** Keep the existing graph and LangChain tool signatures stable. Add one focused China A-share enhancement dataflow module, then append bounded source-labeled snapshots to existing stock, news, fundamentals, and sentiment paths based on the active preset in config.

**Tech Stack:** Python 3, Typer, Rich, pandas, AKShare, existing TradingAgents dataflow config, pytest, unittest.mock, standard-library `zoneinfo`, `concurrent.futures`, and `pathlib`.

## Global Constraints

- Work only inside `D:\prj\TradingAgents\TradingAgents\.worktrees\china-a-enhancements` on branch `agent/china-a-enhancements`.
- Prefix shell verification commands with `rtk`.
- Use CodeGraph before source exploration in this indexed worktree.
- Do not change the core LangGraph workflow or create a China-specific analyst node.
- Keep public LangChain tool signatures stable.
- Default `china_a_enhancement_preset` must be `"basic"`.
- Show the China A-share enhancement selector only for mainland A-share tickers.
- Use `ZoneInfo("Asia/Shanghai")` for China A-share date validation; add no dependency.
- A China A-share date equal to current Beijing date is allowed and must print an incomplete-data warning.
- A China A-share date later than current Beijing date is rejected.
- Non-China tickers preserve current local-date behavior.
- New data sources must fail open and must not abort the run.
- Enhancement prompt text must be bounded and source-labeled.
- Maintain `latest_message_tool.log` as an overwritten copy of the newest run log.
- Keep final saved reports under the existing root `reports/<ticker>_<stamp>` path.

---

## File Structure

- `cli/main.py`
  - Add China A-share helpers, preset prompt, Beijing-aware analysis-date validation, config propagation, and run-scoped log setup.
- `tradingagents/default_config.py`
  - Add default `china_a_enhancement_preset`.
- `tradingagents/dataflows/china_a_enhancements.py`
  - New module for preset/category selection, source calls, caching, source-status formatting, and bounded markdown snapshots.
- `tradingagents/dataflows/interface.py`
  - Append enhancement snapshots to successful `get_stock_data`, `get_news`, and `get_fundamentals` results.
- `tradingagents/agents/analysts/sentiment_analyst.py`
  - Inject the flow/sentiment enhancement into the existing China A-share local sentiment block.
- `tradingagents/agents/analysts/market_analyst.py`
  - Add one prompt sentence that source-labeled China enhancement snapshots in `get_stock_data` are supplemental short-term flow data.
- `tradingagents/agents/analysts/news_analyst.py`
  - Add one prompt sentence that China announcements and industry/policy snapshots are supplemental event context.
- `tradingagents/agents/analysts/fundamentals_analyst.py`
  - Add one prompt sentence that China disclosure snapshots are supplemental fundamentals context.
- `tests/test_china_a_cli_enhancements.py`
  - New tests for preset display, config propagation, and Beijing-date validation.
- `tests/test_china_a_run_logging.py`
  - New tests for run-scoped log paths and latest-log behavior.
- `tests/test_china_a_enhancements.py`
  - New tests for preset dispatch, formatting limits, source failure behavior, and cache behavior.
- `tests/test_vendor_routing.py`
  - Add focused tests that successful routed output gets the right enhancement appendix only for China A-shares.
- `tests/test_sentiment_analyst_china_a.py`
  - Add one test that sentiment blocks include the flow/sentiment enhancement for A-shares when enabled.

---

### Task 1: CLI Presets, Config, and Beijing-Date Validation

**Files:**
- Modify: `cli/main.py`
- Modify: `tradingagents/default_config.py`
- Create: `tests/test_china_a_cli_enhancements.py`

**Interfaces:**
- Produces: `is_china_a_ticker(ticker: str) -> bool`
- Produces: `select_china_a_enhancement_preset() -> str`
- Produces: `_analysis_date_limit(ticker: str | None) -> tuple[datetime.date, str]`
- Changes: `get_analysis_date(ticker: str | None = None) -> str`
- Changes: `_build_run_config(selections: dict, checkpoint: bool | None) -> dict` stores `china_a_enhancement_preset`

- [ ] **Step 1: Write failing tests for ticker-gated preset selection**

Create `tests/test_china_a_cli_enhancements.py` with this initial content:

```python
import datetime
from unittest import mock

import pytest


@pytest.mark.unit
def test_is_china_a_ticker_accepts_mainland_forms():
    import cli.main as m

    assert m.is_china_a_ticker("600895.SS") is True
    assert m.is_china_a_ticker("600895.SH") is True
    assert m.is_china_a_ticker("000333.SZ") is True
    assert m.is_china_a_ticker("600895") is True


@pytest.mark.unit
def test_is_china_a_ticker_rejects_non_mainland_forms():
    import cli.main as m

    assert m.is_china_a_ticker("AAPL") is False
    assert m.is_china_a_ticker("0700.HK") is False
    assert m.is_china_a_ticker("BTC-USD") is False


@pytest.mark.unit
def test_build_run_config_carries_china_a_preset():
    import cli.main as m

    selections = {
        "research_depth": 1,
        "shallow_thinker": "gpt-5.4-mini",
        "deep_thinker": "gpt-5.5",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
        "china_a_enhancement_preset": "flow_sentiment",
    }

    config = m._build_run_config(selections, checkpoint=None)

    assert config["china_a_enhancement_preset"] == "flow_sentiment"


@pytest.mark.unit
def test_china_a_preset_prompt_maps_numeric_choice_to_value(monkeypatch):
    import cli.main as m

    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: "2")

    assert m.select_china_a_enhancement_preset() == "flow_sentiment"
```

- [ ] **Step 2: Run the new preset tests and verify they fail**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py -q
```

Expected: FAIL with missing `is_china_a_ticker` and `select_china_a_enhancement_preset`.

- [ ] **Step 3: Add config default**

In `tradingagents/default_config.py`, add this scalar near the other run-level settings after `output_language`:

```python
    # China mainland A-share enhancement preset. The CLI asks for this only
    # when the ticker resolves to a mainland A-share. "basic" preserves current
    # behavior.
    "china_a_enhancement_preset": "basic",
```

- [ ] **Step 4: Add CLI helpers and config propagation**

In `cli/main.py`, add these imports:

```python
from zoneinfo import ZoneInfo

from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol
```

Add these constants and helpers after `app = typer.Typer(...)`:

```python
CHINA_A_ENHANCEMENT_PRESETS = {
    "basic": "Basic - current behavior, no extra China A-share enhancement",
    "flow_sentiment": "Flow and sentiment - fund flow, Dragon-Tiger, margin, heat",
    "announcements": "Announcements - disclosures, dividends, buybacks, major events",
    "industry_policy": "Industry and policy - sector, concept, policy context",
    "all": "All enhancements - flow, announcements, industry, and policy context",
}

CHINA_A_ENHANCEMENT_ALIASES = {
    "1": "basic",
    "2": "flow_sentiment",
    "3": "announcements",
    "4": "industry_policy",
    "5": "all",
}


def is_china_a_ticker(ticker: str) -> bool:
    return resolve_china_a_symbol(ticker) is not None


def select_china_a_enhancement_preset() -> str:
    console.print("[bold]China A-share enhancement preset[/bold]")
    for idx, (value, label) in enumerate(CHINA_A_ENHANCEMENT_PRESETS.items(), start=1):
        console.print(f"  {idx}. {value} - {label}")

    valid = set(CHINA_A_ENHANCEMENT_PRESETS) | set(CHINA_A_ENHANCEMENT_ALIASES)
    while True:
        raw = typer.prompt(
            "Select preset",
            default="2",
        ).strip().lower()
        choice = CHINA_A_ENHANCEMENT_ALIASES.get(raw, raw)
        if choice in CHINA_A_ENHANCEMENT_PRESETS:
            return choice
        console.print(
            "[red]Invalid preset. Choose 1-5 or one of: "
            + ", ".join(CHINA_A_ENHANCEMENT_PRESETS)
            + "[/red]"
        )
```

In `get_user_selections()`, after `asset_type = detect_asset_type(selected_ticker)`, add:

```python
    china_a_enhancement_preset = "basic"
    if is_china_a_ticker(selected_ticker):
        console.print(
            create_question_box(
                "Step 1b: China A-share Enhancements",
                "Select additional mainland China data sources for this run",
                "flow_sentiment",
            )
        )
        china_a_enhancement_preset = select_china_a_enhancement_preset()
```

Change the date default and prompt call:

```python
    default_date = _analysis_date_limit(selected_ticker)[0].strftime("%Y-%m-%d")
```

```python
    analysis_date = get_analysis_date(selected_ticker)
```

Add the returned selection:

```python
        "china_a_enhancement_preset": china_a_enhancement_preset,
```

In `_build_run_config()`, add:

```python
    config["china_a_enhancement_preset"] = selections.get(
        "china_a_enhancement_preset",
        DEFAULT_CONFIG.get("china_a_enhancement_preset", "basic"),
    )
```

- [ ] **Step 5: Run preset tests and verify they pass**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py::test_is_china_a_ticker_accepts_mainland_forms tests/test_china_a_cli_enhancements.py::test_is_china_a_ticker_rejects_non_mainland_forms tests/test_china_a_cli_enhancements.py::test_build_run_config_carries_china_a_preset tests/test_china_a_cli_enhancements.py::test_china_a_preset_prompt_maps_numeric_choice_to_value -q
```

Expected: PASS.

- [ ] **Step 6: Add failing tests for Beijing-date validation**

Append this code to `tests/test_china_a_cli_enhancements.py`:

```python
class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls(2026, 6, 30, 8, 30, tzinfo=tz)
        return cls(2026, 6, 29, 17, 30)


@pytest.mark.unit
def test_china_a_same_beijing_date_allowed_when_local_date_is_behind(monkeypatch):
    import cli.main as m

    responses = iter(["2026-06-30"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("600895.SS") == "2026-06-30"
    assert any("may be incomplete" in line for line in printed)


@pytest.mark.unit
def test_china_a_after_beijing_today_is_rejected_then_accepts(monkeypatch):
    import cli.main as m

    responses = iter(["2026-07-01", "2026-06-30"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("600895.SS") == "2026-06-30"
    assert any("cannot be in the future" in line for line in printed)


@pytest.mark.unit
def test_non_china_symbol_keeps_local_date_limit(monkeypatch):
    import cli.main as m

    responses = iter(["2026-06-30", "2026-06-29"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("AAPL") == "2026-06-29"
    assert any("cannot be in the future" in line for line in printed)
```

- [ ] **Step 7: Run date tests and verify they fail**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py::test_china_a_same_beijing_date_allowed_when_local_date_is_behind tests/test_china_a_cli_enhancements.py::test_china_a_after_beijing_today_is_rejected_then_accepts tests/test_china_a_cli_enhancements.py::test_non_china_symbol_keeps_local_date_limit -q
```

Expected: FAIL because `get_analysis_date` does not accept a ticker yet.

- [ ] **Step 8: Implement Beijing-aware date validation**

Replace `get_analysis_date()` in `cli/main.py` with:

```python
def _analysis_date_limit(ticker: str | None = None) -> tuple[datetime.date, str]:
    if ticker and is_china_a_ticker(ticker):
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date(), "Beijing"
    return datetime.datetime.now().date(), "local"


def get_analysis_date(ticker: str | None = None):
    """Get the analysis date from user input."""
    while True:
        limit_date, limit_label = _analysis_date_limit(ticker)
        date_str = typer.prompt("", default=limit_date.strftime("%Y-%m-%d"))
        try:
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > limit_date:
                console.print(
                    f"[red]Error: Analysis date cannot be in the future "
                    f"(max {limit_label} date: {limit_date:%Y-%m-%d})[/red]"
                )
                continue
            if ticker and is_china_a_ticker(ticker) and analysis_date.date() == limit_date:
                console.print(
                    "[yellow]China A-share same-day data may be incomplete until "
                    "mainland markets close and vendors finish publishing.[/yellow]"
                )
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )
```

- [ ] **Step 9: Run CLI/date tests**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py -q
```

Expected: PASS.

- [ ] **Step 10: Run existing CLI tests**

Run:

```powershell
rtk pytest tests/test_cli_symbol_handling.py tests/test_cli_env_skip.py tests/test_cli_config_precedence.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit Task 1**

Run:

```powershell
rtk git add cli/main.py tradingagents/default_config.py tests/test_china_a_cli_enhancements.py
rtk git commit -m "feat: add China A-share preset selection"
```

Expected: commit succeeds.

---

### Task 2: Run-Scoped Logging

**Files:**
- Modify: `cli/main.py`
- Create: `tests/test_china_a_run_logging.py`

**Interfaces:**
- Consumes: `china_a_enhancement_preset` from selections/config.
- Produces: `_prepare_run_artifacts(config: dict, selections: dict) -> dict[str, Path | str]`
- Produces: `_append_line_to_run_logs(paths: list[Path], line: str) -> None`

- [ ] **Step 1: Write failing run-log tests**

Create `tests/test_china_a_run_logging.py`:

```python
from pathlib import Path

import pytest


@pytest.mark.unit
def test_prepare_run_artifacts_creates_run_scoped_message_log(tmp_path, monkeypatch):
    import cli.main as m

    class FixedDateTime(m.datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 30, 6, 8, 16)

    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)

    artifacts = m._prepare_run_artifacts(
        {"results_dir": str(tmp_path)},
        {
            "ticker": "600895.SS",
            "analysis_date": "2026-06-30",
            "asset_type": "stock",
            "china_a_enhancement_preset": "flow_sentiment",
        },
    )

    log_file = artifacts["log_file"]
    latest_log_file = artifacts["latest_log_file"]
    report_dir = artifacts["report_dir"]

    assert log_file == tmp_path / "600895.SS" / "2026-06-30" / "runs" / "20260630_060816" / "message_tool.log"
    assert latest_log_file == tmp_path / "600895.SS" / "2026-06-30" / "latest_message_tool.log"
    assert report_dir == tmp_path / "600895.SS" / "2026-06-30" / "runs" / "20260630_060816" / "reports"
    assert log_file.exists()
    assert latest_log_file.exists()
    assert "china_a_enhancement_preset=flow_sentiment" in log_file.read_text(encoding="utf-8")
    assert latest_log_file.read_text(encoding="utf-8") == log_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_append_line_writes_run_log_and_latest_log(tmp_path):
    import cli.main as m

    run_log = tmp_path / "runs" / "20260630_060816" / "message_tool.log"
    latest_log = tmp_path / "latest_message_tool.log"
    run_log.parent.mkdir(parents=True)
    run_log.write_text("header\n", encoding="utf-8")
    latest_log.write_text("header\n", encoding="utf-8")

    m._append_line_to_run_logs([run_log, latest_log], "06:08:17 [System] Completed\n")

    assert run_log.read_text(encoding="utf-8").endswith("06:08:17 [System] Completed\n")
    assert latest_log.read_text(encoding="utf-8") == run_log.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run run-log tests and verify they fail**

Run:

```powershell
rtk pytest tests/test_china_a_run_logging.py -q
```

Expected: FAIL with missing `_prepare_run_artifacts`.

- [ ] **Step 3: Implement run artifact helpers**

Add this code in `cli/main.py` above `run_analysis`:

```python
def _prepare_run_artifacts(config: dict, selections: dict) -> dict[str, Path | str]:
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    run_dir = results_dir / "runs" / run_id
    report_dir = run_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    log_file = run_dir / "message_tool.log"
    latest_log_file = results_dir / "latest_message_tool.log"
    metadata = (
        f"run_id={run_id} "
        f"ticker={selections['ticker']} "
        f"analysis_date={selections['analysis_date']} "
        f"asset_type={selections['asset_type']} "
        f"china_a_enhancement_preset={selections.get('china_a_enhancement_preset', 'basic')}\n"
    )
    log_file.write_text(metadata, encoding="utf-8")
    latest_log_file.write_text(metadata, encoding="utf-8")
    return {
        "run_id": run_id,
        "results_dir": results_dir,
        "run_dir": run_dir,
        "report_dir": report_dir,
        "log_file": log_file,
        "latest_log_file": latest_log_file,
    }


def _append_line_to_run_logs(paths: list[Path], line: str) -> None:
    for path in paths:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
```

- [ ] **Step 4: Wire helpers into `run_analysis`**

In `run_analysis`, replace the current block that creates `results_dir`, `report_dir`, and `log_file`:

```python
    artifacts = _prepare_run_artifacts(config, selections)
    results_dir = artifacts["results_dir"]
    report_dir = artifacts["report_dir"]
    log_file = artifacts["log_file"]
    latest_log_file = artifacts["latest_log_file"]
    run_log_paths = [log_file, latest_log_file]
```

In `save_message_decorator`, replace the direct file write:

```python
            _append_line_to_run_logs(
                run_log_paths,
                f"{timestamp} [{message_type}] {content}\n",
            )
```

In `save_tool_call_decorator`, replace the direct file write:

```python
            _append_line_to_run_logs(
                run_log_paths,
                f"{timestamp} [Tool Call] {tool_name}({args_str})\n",
            )
```

Keep `save_report_section_decorator` writing to `report_dir / file_name`; `report_dir` now points to the run-scoped report directory.

- [ ] **Step 5: Run run-log tests**

Run:

```powershell
rtk pytest tests/test_china_a_run_logging.py -q
```

Expected: PASS.

- [ ] **Step 6: Run reporting tests**

Run:

```powershell
rtk pytest tests/test_reporting.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```powershell
rtk git add cli/main.py tests/test_china_a_run_logging.py
rtk git commit -m "feat: isolate TradingAgents run logs"
```

Expected: commit succeeds.

---

### Task 3: China A-Share Enhancement Dataflow Module

**Files:**
- Create: `tradingagents/dataflows/china_a_enhancements.py`
- Create: `tests/test_china_a_enhancements.py`

**Interfaces:**
- Produces: `get_china_a_enhancements(ticker: str, curr_date: str, preset: str) -> str`
- Produces: `get_china_a_enhancements_for_categories(ticker: str, curr_date: str, preset: str, categories: set[str]) -> str`
- Produces: `SourceResult`

- [ ] **Step 1: Write failing module tests**

Create `tests/test_china_a_enhancements.py`:

```python
import pandas as pd
import pytest


@pytest.mark.unit
def test_basic_preset_returns_empty_for_china_a():
    from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements

    assert get_china_a_enhancements("600895.SS", "2026-06-30", "basic") == ""


@pytest.mark.unit
def test_non_china_symbol_returns_empty():
    from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements

    assert get_china_a_enhancements("AAPL", "2026-06-30", "all") == ""


@pytest.mark.unit
def test_flow_sentiment_snapshot_limits_records(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows.config import set_config
    from tradingagents.dataflows import china_a_enhancements as enh

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    fund_flow = pd.DataFrame(
        {
            "日期": [f"2026-06-{day:02d}" for day in range(20, 31)],
            "收盘价": [30 + day for day in range(11)],
            "涨跌幅": [1.0] * 11,
            "主力净流入-净额": [1000 * day for day in range(11)],
            "主力净流入-净占比": [2.5] * 11,
        }
    )
    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", lambda stock, market: fund_flow)
    monkeypatch.setattr(enh.ak, "stock_lhb_detail_em", lambda start_date, end_date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_rank_latest_em", lambda symbol: pd.DataFrame({"排名": [287], "证券代码": ["600895"]}))
    monkeypatch.setattr(enh.ak, "stock_hot_keyword_em", lambda symbol: pd.DataFrame({"关键词": ["光刻机", "张江"]}))

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "## China A-share Enhancement Snapshot" in out
    assert "Preset: flow_sentiment" in out
    assert "stock_individual_fund_flow" in out
    assert out.count("Source:") <= 8
    assert "光刻机" in out


@pytest.mark.unit
def test_source_failure_degrades_without_raising(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows.config import set_config
    from tradingagents.dataflows import china_a_enhancements as enh

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(enh.ak, "stock_individual_fund_flow", lambda stock, market: (_ for _ in ()).throw(ValueError("shape mismatch")))
    monkeypatch.setattr(enh.ak, "stock_lhb_detail_em", lambda start_date, end_date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_margin_detail_sse", lambda date: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_rank_latest_em", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(enh.ak, "stock_hot_keyword_em", lambda symbol: pd.DataFrame())

    out = enh.get_china_a_enhancements("600895.SS", "2026-06-30", "flow_sentiment")

    assert "Source unavailable: stock_individual_fund_flow" in out
    assert "shape mismatch" in out


@pytest.mark.unit
def test_category_filter_omits_unrequested_sections(monkeypatch, tmp_path):
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows.config import set_config
    from tradingagents.dataflows import china_a_enhancements as enh

    config_module._config = None
    set_config({"data_cache_dir": str(tmp_path)})

    monkeypatch.setattr(enh.ak, "stock_individual_notice_report", lambda security, symbol, begin_date, end_date: pd.DataFrame({"公告标题": ["分红公告"], "公告时间": ["2026-06-20"]}))

    out = enh.get_china_a_enhancements_for_categories(
        "600895.SS",
        "2026-06-30",
        "announcements",
        {"announcements"},
    )

    assert "Announcements and disclosures" in out
    assert "分红公告" in out
    assert "Fund flow and trading activity" not in out
```

- [ ] **Step 2: Run module tests and verify they fail**

Run:

```powershell
rtk pytest tests/test_china_a_enhancements.py -q
```

Expected: FAIL with missing module.

- [ ] **Step 3: Create module header, types, preset selection, cache helpers**

Create `tradingagents/dataflows/china_a_enhancements.py` with:

```python
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd

from .config import get_config
from .symbol_utils import resolve_china_a_symbol

CATEGORY_FLOW_SENTIMENT = "flow_sentiment"
CATEGORY_ANNOUNCEMENTS = "announcements"
CATEGORY_INDUSTRY_POLICY = "industry_policy"

PRESET_CATEGORIES = {
    "basic": set(),
    "flow_sentiment": {CATEGORY_FLOW_SENTIMENT},
    "announcements": {CATEGORY_ANNOUNCEMENTS},
    "industry_policy": {CATEGORY_INDUSTRY_POLICY},
    "all": {CATEGORY_FLOW_SENTIMENT, CATEGORY_ANNOUNCEMENTS, CATEGORY_INDUSTRY_POLICY},
}

SOURCE_TIMEOUT_SECONDS = 8
CACHE_TTL_SECONDS = 6 * 60 * 60
_EXECUTOR = ThreadPoolExecutor(max_workers=4)


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    as_of: str | None
    records: tuple[str, ...] = ()
    error: str | None = None


def _cache_root() -> Path:
    return Path(get_config()["data_cache_dir"]) / "china_a_enhancements"


def _cache_path(ticker: str, curr_date: str, preset: str, categories: set[str]) -> Path:
    key = json.dumps(
        {
            "ticker": ticker,
            "curr_date": curr_date,
            "preset": preset,
            "categories": sorted(categories),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _cache_root() / f"{digest}.md"


def _read_cache(path: Path) -> str | None:
    if not path.exists():
        return None
    age = datetime.now().timestamp() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return None
    return path.read_text(encoding="utf-8")


def _write_cache(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _call_source(source: str, fn: Callable[[], pd.DataFrame], timeout_seconds: int = SOURCE_TIMEOUT_SECONDS) -> tuple[pd.DataFrame | None, SourceResult | None]:
    future = _EXECUTOR.submit(fn)
    try:
        frame = future.result(timeout=timeout_seconds)
    except TimeoutError:
        return None, SourceResult(source=source, status="unavailable", as_of=None, error=f"timed out after {timeout_seconds}s")
    except Exception as exc:
        return None, SourceResult(source=source, status="unavailable", as_of=None, error=str(exc))
    if frame is None or frame.empty:
        return None, SourceResult(source=source, status="unavailable", as_of=None, error="returned no rows")
    return frame, None
```

- [ ] **Step 4: Add formatting helpers**

Append:

```python
def _string_value(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _first_present(row: pd.Series, names: tuple[str, ...]) -> str:
    for name in names:
        if name in row:
            value = _string_value(row.get(name))
            if value:
                return value
    return ""


def _format_result(result: SourceResult) -> list[str]:
    if result.status == "ok":
        lines = []
        for record in result.records:
            lines.append(f"- Source: {result.source}; as_of: {result.as_of or 'unknown'}; {record}")
        return lines
    return [f"- Source unavailable: {result.source} ({result.error or 'unknown error'})."]


def _format_snapshot(ticker: str, curr_date: str, preset: str, results: list[tuple[str, list[SourceResult]]]) -> str:
    if not results:
        return ""

    lines = [
        "## China A-share Enhancement Snapshot",
        f"Ticker: {ticker}",
        f"Analysis date: {curr_date}",
        f"Preset: {preset}",
        "",
        "Use this source-labeled snapshot as supplemental China-local context. "
        "Separate short-term flow signals from medium-term fundamentals.",
        "",
    ]
    for title, source_results in results:
        lines.append(f"### {title}")
        for result in source_results:
            lines.extend(_format_result(result))
        lines.append("")
    return "\n".join(lines).strip()
```

- [ ] **Step 5: Add flow/sentiment collectors**

Append:

```python
def _eastmoney_symbol(instrument) -> str:
    prefix = "SH" if instrument.exchange == "shanghai" else "SZ"
    return f"{prefix}{instrument.akshare_code}"


def _market_arg(instrument) -> str:
    return "sh" if instrument.exchange == "shanghai" else "sz"


def _collect_flow_sentiment(instrument, curr_date: str) -> list[SourceResult]:
    results: list[SourceResult] = []

    frame, error = _call_source(
        "stock_individual_fund_flow",
        lambda: ak.stock_individual_fund_flow(stock=instrument.akshare_code, market=_market_arg(instrument)),
    )
    if error:
        results.append(error)
    else:
        keep = []
        for _, row in frame.tail(5).iterrows():
            keep.append(
                "date={date}; close={close}; pct_change={pct}; main_net_inflow={net}; main_net_ratio={ratio}".format(
                    date=_first_present(row, ("日期", "date")),
                    close=_first_present(row, ("收盘价", "close")),
                    pct=_first_present(row, ("涨跌幅", "pct_change")),
                    net=_first_present(row, ("主力净流入-净额", "main_net_inflow")),
                    ratio=_first_present(row, ("主力净流入-净占比", "main_net_ratio")),
                )
            )
        results.append(SourceResult("stock_individual_fund_flow", "ok", curr_date, tuple(keep)))

    start = (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y%m%d")
    end = datetime.strptime(curr_date, "%Y-%m-%d").strftime("%Y%m%d")
    frame, error = _call_source("stock_lhb_detail_em", lambda: ak.stock_lhb_detail_em(start_date=start, end_date=end))
    if error:
        results.append(error)
    else:
        code_col = "代码" if "代码" in frame.columns else "证券代码"
        stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code] if code_col in frame.columns else pd.DataFrame()
        records = []
        for _, row in stock_rows.head(3).iterrows():
            records.append(
                "date={date}; reason={reason}; net_buy={net_buy}".format(
                    date=_first_present(row, ("上榜日", "日期")),
                    reason=_first_present(row, ("解读", "上榜原因", "原因")),
                    net_buy=_first_present(row, ("龙虎榜净买额", "净买额")),
                )
            )
        if not records:
            records = ["no Dragon-Tiger listing found in the 14-day lookback window"]
        results.append(SourceResult("stock_lhb_detail_em", "ok", curr_date, tuple(records)))

    margin_fn = ak.stock_margin_detail_sse if instrument.exchange == "shanghai" else ak.stock_margin_detail_szse
    frame, error = _call_source("stock_margin_detail_sse" if instrument.exchange == "shanghai" else "stock_margin_detail_szse", lambda: margin_fn(date=end))
    if error:
        results.append(error)
    else:
        code_col = "证券代码" if "证券代码" in frame.columns else "标的证券代码"
        stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code] if code_col in frame.columns else pd.DataFrame()
        records = []
        for _, row in stock_rows.head(2).iterrows():
            records.append(
                "financing_balance={fin}; securities_lending_balance={lend}".format(
                    fin=_first_present(row, ("融资余额", "融资余额(元)")),
                    lend=_first_present(row, ("融券余额", "融券余额(元)")),
                )
            )
        if not records:
            records = ["no margin detail row found for this stock on the requested date"]
        results.append(SourceResult("margin_detail", "ok", curr_date, tuple(records)))

    em_symbol = _eastmoney_symbol(instrument)
    frame, error = _call_source("stock_hot_rank_latest_em", lambda: ak.stock_hot_rank_latest_em(symbol=em_symbol))
    if error:
        results.append(error)
    else:
        row = frame.iloc[0]
        results.append(SourceResult("stock_hot_rank_latest_em", "ok", curr_date, (f"rank={_first_present(row, ('排名', '当前排名'))}",)))

    frame, error = _call_source("stock_hot_keyword_em", lambda: ak.stock_hot_keyword_em(symbol=em_symbol))
    if error:
        results.append(error)
    else:
        keyword_col = "关键词" if "关键词" in frame.columns else frame.columns[0]
        keywords = [str(v).strip() for v in frame[keyword_col].head(5).tolist() if str(v).strip()]
        results.append(SourceResult("stock_hot_keyword_em", "ok", curr_date, (f"top_keywords={', '.join(keywords)}",)))

    return results
```

- [ ] **Step 6: Add announcements and industry/policy collectors**

Append:

```python
def _collect_announcements(instrument, curr_date: str) -> list[SourceResult]:
    end = datetime.strptime(curr_date, "%Y-%m-%d")
    begin = (end - timedelta(days=90)).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    results: list[SourceResult] = []

    frame, error = _call_source(
        "stock_individual_notice_report",
        lambda: ak.stock_individual_notice_report(
            security=instrument.akshare_code,
            symbol="全部",
            begin_date=begin,
            end_date=end_s,
        ),
    )
    if error:
        results.append(error)
    else:
        records = []
        important = ("业绩", "分红", "回购", "减持", "质押", "重组", "诉讼", "关联交易", "合同", "公告")
        for _, row in frame.head(20).iterrows():
            title = _first_present(row, ("公告标题", "标题", "title"))
            date = _first_present(row, ("公告时间", "公告日期", "date"))
            if title and any(token in title for token in important):
                records.append(f"date={date}; title={title}")
            if len(records) >= 8:
                break
        if not records:
            records = ["no high-priority company announcements found in the lookback window"]
        results.append(SourceResult("stock_individual_notice_report", "ok", curr_date, tuple(records)))

    frame, error = _call_source(
        "stock_zh_a_disclosure_report_cninfo",
        lambda: ak.stock_zh_a_disclosure_report_cninfo(
            symbol=instrument.akshare_code,
            market="沪深京",
            keyword="",
            category="",
            start_date=(end - timedelta(days=90)).strftime("%Y-%m-%d"),
            end_date=curr_date,
        ),
    )
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "date={date}; title={title}".format(
                    date=_first_present(row, ("公告日期", "披露日期", "date")),
                    title=_first_present(row, ("公告标题", "标题", "title")),
                )
            )
        results.append(SourceResult("stock_zh_a_disclosure_report_cninfo", "ok", curr_date, tuple(records)))

    return results


def _collect_industry_policy(instrument, curr_date: str) -> list[SourceResult]:
    results: list[SourceResult] = []

    frame, error = _call_source("stock_sector_fund_flow_rank", lambda: ak.stock_sector_fund_flow_rank(indicator="今日"))
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "sector={sector}; pct_change={pct}; net_inflow={net}".format(
                    sector=_first_present(row, ("名称", "板块名称")),
                    pct=_first_present(row, ("涨跌幅", "涨跌幅%")),
                    net=_first_present(row, ("主力净流入-净额", "净流入")),
                )
            )
        results.append(SourceResult("stock_sector_fund_flow_rank", "ok", curr_date, tuple(records)))

    frame, error = _call_source("stock_info_global_em", lambda: ak.stock_info_global_em())
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "title={title}; source={source}".format(
                    title=_first_present(row, ("标题", "新闻标题")),
                    source=_first_present(row, ("来源", "文章来源")),
                )
            )
        results.append(SourceResult("stock_info_global_em", "ok", curr_date, tuple(records)))

    return results
```

- [ ] **Step 7: Add public dispatch functions**

Append:

```python
def _categories_for_preset(preset: str) -> set[str]:
    return set(PRESET_CATEGORIES.get(preset, set()))


def get_china_a_enhancements_for_categories(
    ticker: str,
    curr_date: str,
    preset: str,
    categories: set[str],
) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        return ""
    allowed = _categories_for_preset(preset) & set(categories)
    if not allowed:
        return ""

    cache_path = _cache_path(ticker, curr_date, preset, allowed)
    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    sections: list[tuple[str, list[SourceResult]]] = []
    if CATEGORY_FLOW_SENTIMENT in allowed:
        sections.append(("Fund flow and trading activity", _collect_flow_sentiment(instrument, curr_date)))
    if CATEGORY_ANNOUNCEMENTS in allowed:
        sections.append(("Announcements and disclosures", _collect_announcements(instrument, curr_date)))
    if CATEGORY_INDUSTRY_POLICY in allowed:
        sections.append(("Industry, sector, and policy context", _collect_industry_policy(instrument, curr_date)))

    text = _format_snapshot(instrument.yahoo_symbol, curr_date, preset, sections)
    if text:
        _write_cache(cache_path, text)
    return text


def get_china_a_enhancements(ticker: str, curr_date: str, preset: str) -> str:
    return get_china_a_enhancements_for_categories(
        ticker,
        curr_date,
        preset,
        _categories_for_preset(preset),
    )
```

- [ ] **Step 8: Run module tests**

Run:

```powershell
rtk pytest tests/test_china_a_enhancements.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 3**

Run:

```powershell
rtk git add tradingagents/dataflows/china_a_enhancements.py tests/test_china_a_enhancements.py
rtk git commit -m "feat: add China A-share enhancement dataflow"
```

Expected: commit succeeds.

---

### Task 4: Wire Enhancements Into Existing Tools and Analysts

**Files:**
- Modify: `tradingagents/dataflows/interface.py`
- Modify: `tradingagents/agents/analysts/sentiment_analyst.py`
- Modify: `tradingagents/agents/analysts/market_analyst.py`
- Modify: `tradingagents/agents/analysts/news_analyst.py`
- Modify: `tradingagents/agents/analysts/fundamentals_analyst.py`
- Modify: `tests/test_vendor_routing.py`
- Modify: `tests/test_sentiment_analyst_china_a.py`

**Interfaces:**
- Consumes: `get_china_a_enhancements_for_categories(...)`
- Produces: `append_china_a_enhancement(method: str, result: str, args: tuple, kwargs: dict) -> str`

- [ ] **Step 1: Add failing routing tests**

Append to `tests/test_vendor_routing.py`:

```python
    def test_china_a_get_stock_data_appends_flow_enhancement_when_enabled(self):
        set_config({"china_a_enhancement_preset": "flow_sentiment"})
        with self._route({"akshare": _returns("PRICE_DATA")}):
            with mock.patch(
                "tradingagents.dataflows.interface.get_china_a_enhancements_for_categories",
                return_value="FLOW_APPENDIX",
            ) as enh:
                result = interface.route_to_vendor(
                    "get_stock_data", "600895.SS", "2026-06-01", "2026-06-30"
                )

        self.assertEqual(result, "PRICE_DATA\n\nFLOW_APPENDIX")
        enh.assert_called_once_with(
            "600895.SS",
            "2026-06-30",
            "flow_sentiment",
            {"flow_sentiment"},
        )

    def test_non_china_get_stock_data_does_not_append_china_enhancement(self):
        set_config({"china_a_enhancement_preset": "flow_sentiment"})
        with self._route({"yfinance": _returns("PRICE_DATA")}):
            with mock.patch(
                "tradingagents.dataflows.interface.get_china_a_enhancements_for_categories",
                return_value="FLOW_APPENDIX",
            ) as enh:
                result = interface.route_to_vendor(
                    "get_stock_data", "AAPL", "2026-06-01", "2026-06-30"
                )

        self.assertEqual(result, "PRICE_DATA")
        enh.assert_not_called()

    def test_china_a_get_news_appends_announcement_and_policy_enhancements(self):
        set_config({"china_a_enhancement_preset": "all"})
        with self._route_method("get_news", {"akshare": _returns("NEWS_DATA")}):
            with mock.patch(
                "tradingagents.dataflows.interface.get_china_a_enhancements_for_categories",
                return_value="NEWS_APPENDIX",
            ) as enh:
                result = interface.route_to_vendor(
                    "get_news", "600895.SS", "2026-06-23", "2026-06-30"
                )

        self.assertEqual(result, "NEWS_DATA\n\nNEWS_APPENDIX")
        enh.assert_called_once_with(
            "600895.SS",
            "2026-06-30",
            "all",
            {"announcements", "industry_policy"},
        )

    def test_china_a_get_fundamentals_appends_announcement_enhancement(self):
        set_config({"china_a_enhancement_preset": "announcements"})
        with self._route_method("get_fundamentals", {"akshare": _returns("FUND_DATA")}):
            with mock.patch(
                "tradingagents.dataflows.interface.get_china_a_enhancements_for_categories",
                return_value="FUND_APPENDIX",
            ) as enh:
                result = interface.route_to_vendor(
                    "get_fundamentals", "600895.SS", "2026-06-30"
                )

        self.assertEqual(result, "FUND_DATA\n\nFUND_APPENDIX")
        enh.assert_called_once_with(
            "600895.SS",
            "2026-06-30",
            "announcements",
            {"announcements"},
        )
```

- [ ] **Step 2: Run new routing tests and verify they fail**

Run:

```powershell
rtk pytest tests/test_vendor_routing.py::VendorRoutingTests::test_china_a_get_stock_data_appends_flow_enhancement_when_enabled tests/test_vendor_routing.py::VendorRoutingTests::test_non_china_get_stock_data_does_not_append_china_enhancement tests/test_vendor_routing.py::VendorRoutingTests::test_china_a_get_news_appends_announcement_and_policy_enhancements tests/test_vendor_routing.py::VendorRoutingTests::test_china_a_get_fundamentals_appends_announcement_enhancement -q
```

Expected: FAIL because interface does not append enhancements yet.

- [ ] **Step 3: Implement interface append helper**

In `tradingagents/dataflows/interface.py`, add import:

```python
from .china_a_enhancements import get_china_a_enhancements_for_categories
```

Add helpers before `route_to_vendor`:

```python
ENHANCEMENT_CATEGORIES_BY_METHOD = {
    "get_stock_data": {"flow_sentiment"},
    "get_news": {"announcements", "industry_policy"},
    "get_fundamentals": {"announcements"},
}


def _date_for_enhancement(method: str, args: tuple, kwargs: dict) -> str | None:
    if method == "get_stock_data":
        return kwargs.get("end_date") or (args[2] if len(args) > 2 else None)
    if method == "get_news":
        return kwargs.get("end_date") or (args[2] if len(args) > 2 else None)
    if method == "get_fundamentals":
        return kwargs.get("curr_date") or (args[1] if len(args) > 1 else None)
    return None


def append_china_a_enhancement(method: str, result: str, args: tuple, kwargs: dict) -> str:
    if not isinstance(result, str):
        return result
    categories = ENHANCEMENT_CATEGORIES_BY_METHOD.get(method)
    if not categories:
        return result
    symbol = _first_symbol_arg(args, kwargs)
    if not isinstance(symbol, str) or resolve_china_a_symbol(symbol) is None:
        return result
    curr_date = _date_for_enhancement(method, args, kwargs)
    if not curr_date:
        return result
    preset = get_config().get("china_a_enhancement_preset", "basic")
    appendix = get_china_a_enhancements_for_categories(symbol, curr_date, preset, categories)
    if not appendix.strip():
        return result
    return f"{result}\n\n{appendix}"
```

In `route_to_vendor`, change the successful return:

```python
            result = impl_func(*args, **kwargs)
            return append_china_a_enhancement(method, result, args, kwargs)
```

- [ ] **Step 4: Run routing tests**

Run:

```powershell
rtk pytest tests/test_vendor_routing.py -q
```

Expected: PASS.

- [ ] **Step 5: Add failing sentiment enhancement test**

Open `tests/test_sentiment_analyst_china_a.py` and append:

```python
@pytest.mark.unit
def test_collect_sentiment_blocks_adds_china_flow_enhancement(monkeypatch):
    from tradingagents.agents.analysts import sentiment_analyst as sa
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
```

- [ ] **Step 6: Run sentiment test and verify it fails**

Run:

```powershell
rtk pytest tests/test_sentiment_analyst_china_a.py::test_collect_sentiment_blocks_adds_china_flow_enhancement -q
```

Expected: FAIL because sentiment analyst does not import or append the new enhancement.

- [ ] **Step 7: Wire sentiment analyst**

In `tradingagents/agents/analysts/sentiment_analyst.py`, add imports:

```python
from tradingagents.dataflows.china_a_enhancements import get_china_a_enhancements_for_categories
from tradingagents.dataflows.config import get_config
```

Change the China A-share branch in `_collect_sentiment_blocks`:

```python
    if resolve_china_a_symbol(ticker) is not None:
        local_sentiment = get_china_a_local_sentiment(ticker, start_date, end_date)
        preset = get_config().get("china_a_enhancement_preset", "basic")
        flow_enhancement = get_china_a_enhancements_for_categories(
            ticker,
            end_date,
            preset,
            {"flow_sentiment"},
        )
        local_block = "\n\n".join(
            part for part in (local_sentiment, flow_enhancement) if part.strip()
        )
        return {
            "news_block": news_block,
            "stocktwits_block": (
                "<stocktwits skipped: not applicable for China A-shares; "
                "StockTwits does not reliably cover mainland China tickers. "
                "Do not treat this as a missing retail-sentiment failure.>"
            ),
            "reddit_block": (
                "<reddit skipped: not applicable for China A-shares; English finance "
                "subreddits are not a reliable mainland China ticker sentiment source. "
                "Do not infer absence of discussion from this skipped source.>"
            ),
            "local_sentiment_block": local_block,
        }
```

- [ ] **Step 8: Update analyst prompts with one sentence each**

In `market_analyst.py`, append this sentence to `system_message` after the verified snapshot paragraph:

```python
            + " For mainland China A-shares, get_stock_data may include a source-labeled China A-share enhancement snapshot; treat it as supplemental short-term flow and attention context, not as a replacement for OHLCV or verified indicator values."
```

In `news_analyst.py`, append this sentence to `system_message`:

```python
            + " For mainland China A-shares, ticker news may include source-labeled announcements, industry, sector, and policy snapshots; separate company-specific events from broad sector or policy context."
```

In `fundamentals_analyst.py`, append this sentence to `system_message`:

```python
            + " For mainland China A-shares, fundamentals may include source-labeled disclosure snapshots; treat them as supplemental company-event context and do not invent missing filing details."
```

- [ ] **Step 9: Run sentiment and analyst-adjacent tests**

Run:

```powershell
rtk pytest tests/test_sentiment_analyst_china_a.py tests/test_vendor_routing.py -q
```

Expected: PASS.

- [ ] **Step 10: Run focused dataflow and CLI tests**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py tests/test_china_a_run_logging.py tests/test_china_a_enhancements.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_dataflows_config.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit Task 4**

Run:

```powershell
rtk git add tradingagents/dataflows/interface.py tradingagents/agents/analysts/sentiment_analyst.py tradingagents/agents/analysts/market_analyst.py tradingagents/agents/analysts/news_analyst.py tradingagents/agents/analysts/fundamentals_analyst.py tests/test_vendor_routing.py tests/test_sentiment_analyst_china_a.py
rtk git commit -m "feat: wire China A-share enhancements into analysis"
```

Expected: commit succeeds.

---

### Task 5: Final Verification and Cleanup

**Files:**
- Review only unless verification exposes a defect.

**Interfaces:**
- Consumes all interfaces from Tasks 1 through 4.
- Produces a verified implementation branch ready for final review.

- [ ] **Step 1: Run formatter/linter if configured**

Run:

```powershell
rtk python -m ruff check cli tradingagents tests
```

Expected: PASS. If `ruff` is unavailable in the environment, record that exact command failure in the final handoff and continue with pytest verification.

- [ ] **Step 2: Run focused test suite**

Run:

```powershell
rtk pytest tests/test_china_a_cli_enhancements.py tests/test_china_a_run_logging.py tests/test_china_a_enhancements.py tests/test_vendor_routing.py tests/test_sentiment_analyst_china_a.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_reporting.py tests/test_dataflows_config.py tests/test_cli_symbol_handling.py tests/test_cli_env_skip.py tests/test_cli_config_precedence.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```powershell
rtk pytest -q
```

Expected: PASS.

- [ ] **Step 4: Inspect final diff**

Run:

```powershell
rtk git diff --stat main...HEAD
rtk git diff --check
```

Expected: diff contains only scoped files from this plan; `git diff --check` reports no whitespace errors.

- [ ] **Step 5: Commit verification fixes only when needed**

If Step 1 through Step 4 exposed a defect and code was changed to fix it, run:

```powershell
rtk git add cli tradingagents tests
rtk git commit -m "test: verify China A-share enhancements"
```

Expected: commit succeeds. If no files changed, skip this commit.

