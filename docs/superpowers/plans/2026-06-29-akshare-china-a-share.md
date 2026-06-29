# AKShare China A-Share Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add free, no-key China A-share support with AKShare as the primary source, Baostock as a narrow fallback, and Yahoo/yfinance as supplemental information.

**Architecture:** Keep the existing tool surface and `route_to_vendor` router. Add a deterministic China A-share resolver, market-aware vendor chains, and focused AKShare/Baostock dataflow modules that produce the same string outputs current agents already consume.

**Tech Stack:** Python 3.10+, pandas, stockstats, yfinance, AKShare, Baostock, pytest, existing TradingAgents dataflow/router modules.

## Global Constraints

- Prefer free, no-key data sources.
- Use AKShare as the primary China A-share source.
- Use Baostock only as a narrow fallback.
- Keep Yahoo/yfinance as a supplemental source.
- Do not use Tushare in this phase because it needs a token and has points/permission tiers.
- During later manual CLI smoke testing, choose DeepSeek in the model-selection step.
- `601138.SS`, `601138.SH`, and `601138` must resolve to the same Shanghai instrument.
- `600895.SS`, `600895.SH`, and `600895` must resolve to the same Shanghai instrument.
- `000001.SZ` and `000001` must resolve to the same Shenzhen instrument.
- Non-China symbols must keep the existing behavior unless explicitly configured otherwise.
- Phase 1 keeps `get_global_news` unchanged.
- Declare `akshare` and `baostock` in `pyproject.toml` as regular dependencies for this local fork.
- Environment variable support for nested `market_data_vendors` is a follow-up, not part of phase 1.

---

## File Structure

- Modify `tradingagents/dataflows/symbol_utils.py`
  - Owns syntactic symbol normalization and the new `ChinaAInstrument` resolver.
  - Exports `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.

- Modify `tests/test_symbol_utils.py`
  - Covers `.SS`, `.SH`, `.SZ`, and bare-code China A-share normalization without network calls.

- Modify `tradingagents/default_config.py`
  - Adds `market_data_vendors.cn_a` chains while preserving existing non-China defaults.

- Modify `tradingagents/dataflows/interface.py`
  - Detects the market from the first ticker/symbol argument.
  - Uses market-specific vendor chains only for ticker-scoped methods and China A-share inputs.
  - Registers AKShare and Baostock vendor implementations after their modules exist.

- Modify `tests/test_vendor_routing.py`
  - Covers China-specific vendor-chain selection and proves non-China routing is unchanged.

- Create `tradingagents/dataflows/akshare_data.py`
  - Owns AKShare imports, China A-share OHLCV conversion, ticker news, fundamentals aggregation, and AKShare-backed indicators.

- Create `tests/test_akshare_data.py`
  - Mocks AKShare functions and verifies output shape, date filtering, degraded optional sections, and no-data behavior.

- Create `tradingagents/dataflows/baostock_data.py`
  - Owns Baostock session handling and narrow fallback functions for OHLCV and simple financial summary.

- Create `tests/test_baostock_data.py`
  - Mocks Baostock login/query objects and verifies fallback output shape plus cleanup.

- Modify `tradingagents/agents/utils/agent_utils.py`
  - Keeps Yahoo identity as the first supplemental path, and adds a China A-share fallback identity from AKShare profile fields when Yahoo returns no usable identity.

- Modify `tests/test_symbol_normalization_paths.py`
  - Adds identity tests for `601138.SH` resolving through Yahoo `.SS` and falling back to AKShare when Yahoo is empty.

- Modify `pyproject.toml`
  - Adds `akshare` and `baostock` dependencies.

- Optional modify `.env.example`
  - Add a short comment that no token is needed for AKShare/Baostock A-share data.

---

### Task 0: Local Environment And China Data Dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Create: `tests/test_china_dependencies.py`

**Interfaces:**
- Consumes: Existing project packaging metadata.
- Produces: A worktree-local `.venv` with dev tooling, plus declared `akshare` and `baostock` dependencies for later tasks.

- [ ] **Step 1: Create a worktree-local virtual environment and install current dev tooling**

Run:

```bash
rtk python -m venv .venv
rtk .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected: installation succeeds and `pytest` is available in this worktree.

- [ ] **Step 2: Add the failing dependency metadata test**

Create `tests/test_china_dependencies.py`:

```python
from pathlib import Path

import pytest


@pytest.mark.unit
def test_china_data_dependencies_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"akshare' in pyproject
    assert '"baostock' in pyproject
```

- [ ] **Step 3: Run the dependency test and verify it fails**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_china_dependencies.py -q
```

Expected: FAIL because `akshare` and `baostock` are not declared in `pyproject.toml`.

- [ ] **Step 4: Declare China data dependencies**

In `pyproject.toml`, add these entries to `[project].dependencies` near `yfinance`:

```toml
    "akshare>=1.18.64",
    "baostock>=0.8.9",
```

- [ ] **Step 5: Add a no-key note to `.env.example`**

Append this comment after the FRED section:

```dotenv
# China A-share data uses AKShare first and Baostock as a narrow fallback.
# These sources do not require API keys for the supported phase-1 endpoints.
```

- [ ] **Step 6: Reinstall the worktree package with China dependencies**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected: installation succeeds and imports for `akshare` and `baostock` work.

- [ ] **Step 7: Run the dependency test and verify it passes**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_china_dependencies.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 0**

Run:

```bash
rtk git add pyproject.toml .env.example tests/test_china_dependencies.py
rtk git commit -m "chore: declare China A-share data dependencies"
```

---

### Task 1: China A-Share Symbol Resolution

**Files:**
- Modify: `tradingagents/dataflows/symbol_utils.py`
- Modify: `tests/test_symbol_utils.py`

**Interfaces:**
- Consumes: Existing `normalize_symbol(raw: str) -> str`.
- Produces:
  - `ChinaAInstrument` dataclass with fields `raw_input`, `yahoo_symbol`, `akshare_code`, `baostock_code`, `market`, `exchange`.
  - `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
  - Updated `normalize_symbol(raw: str) -> str`, where China A-share inputs return the Yahoo symbol.

- [ ] **Step 1: Write the failing symbol resolver tests**

Append these tests to `tests/test_symbol_utils.py`:

```python
@pytest.mark.unit
class TestChinaASymbols(unittest.TestCase):
    def test_shanghai_suffixes_resolve_to_same_instrument(self):
        for raw in ("601138.SS", "601138.SH", "601138"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "601138.SS")
            self.assertEqual(instrument.akshare_code, "601138")
            self.assertEqual(instrument.baostock_code, "sh.601138")
            self.assertEqual(instrument.market, "cn_a")
            self.assertEqual(instrument.exchange, "shanghai")
            self.assertEqual(normalize_symbol(raw), "601138.SS")

    def test_second_shanghai_example_resolves(self):
        for raw in ("600895.SS", "600895.SH", "600895"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "600895.SS")
            self.assertEqual(instrument.akshare_code, "600895")
            self.assertEqual(instrument.baostock_code, "sh.600895")
            self.assertEqual(normalize_symbol(raw), "600895.SS")

    def test_shenzhen_suffix_and_bare_code_resolve(self):
        for raw in ("000001.SZ", "000001"):
            instrument = resolve_china_a_symbol(raw)
            self.assertIsNotNone(instrument)
            self.assertEqual(instrument.yahoo_symbol, "000001.SZ")
            self.assertEqual(instrument.akshare_code, "000001")
            self.assertEqual(instrument.baostock_code, "sz.000001")
            self.assertEqual(instrument.exchange, "shenzhen")
            self.assertEqual(normalize_symbol(raw), "000001.SZ")

    def test_unknown_bare_six_digit_code_is_not_guessed(self):
        self.assertIsNone(resolve_china_a_symbol("123456"))
        self.assertEqual(normalize_symbol("123456"), "123456")

    def test_existing_non_china_symbols_keep_current_behavior(self):
        self.assertEqual(normalize_symbol("XAUUSD"), "GC=F")
        self.assertEqual(normalize_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(normalize_symbol("0700.HK"), "0700.HK")
```

Update the import at the top of `tests/test_symbol_utils.py`:

```python
from tradingagents.dataflows.symbol_utils import (
    NoMarketDataError,
    is_yahoo_safe,
    normalize_symbol,
    resolve_china_a_symbol,
)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_symbol_utils.py -q
```

Expected: FAIL with `ImportError` or `AttributeError` for `resolve_china_a_symbol`.

- [ ] **Step 3: Implement `ChinaAInstrument` and `resolve_china_a_symbol`**

In `tradingagents/dataflows/symbol_utils.py`, add the dataclass import:

```python
from dataclasses import dataclass
```

Add these constants and dataclass after `logger = logging.getLogger(__name__)`:

```python
_CN_A_SHANGHAI_PREFIXES = ("600", "601", "603", "605", "688", "900")
_CN_A_SHENZHEN_PREFIXES = ("000", "001", "002", "003", "300", "301", "200")
_CN_A_RE = re.compile(r"^(?P<code>\d{6})(?:\.(?P<suffix>SS|SH|SZ))?$")


@dataclass(frozen=True)
class ChinaAInstrument:
    raw_input: str
    yahoo_symbol: str
    akshare_code: str
    baostock_code: str
    market: str
    exchange: str
```

Add this helper before `_normalize_crypto`:

```python
def _infer_china_a_exchange(code: str, suffix: str | None) -> str | None:
    if suffix in {"SS", "SH"}:
        return "shanghai"
    if suffix == "SZ":
        return "shenzhen"
    if code.startswith(_CN_A_SHANGHAI_PREFIXES):
        return "shanghai"
    if code.startswith(_CN_A_SHENZHEN_PREFIXES):
        return "shenzhen"
    return None


def resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None:
    """Return structured China A-share identifiers, or None for non A-shares."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    original = raw.strip()
    s = original.upper().rstrip("+")
    match = _CN_A_RE.fullmatch(s)
    if not match:
        return None

    code = match.group("code")
    suffix = match.group("suffix")
    exchange = _infer_china_a_exchange(code, suffix)
    if exchange is None:
        return None

    yahoo_suffix = ".SS" if exchange == "shanghai" else ".SZ"
    baostock_prefix = "sh" if exchange == "shanghai" else "sz"
    return ChinaAInstrument(
        raw_input=original,
        yahoo_symbol=f"{code}{yahoo_suffix}",
        akshare_code=code,
        baostock_code=f"{baostock_prefix}.{code}",
        market="cn_a",
        exchange=exchange,
    )
```

- [ ] **Step 4: Update `normalize_symbol` to use the China resolver**

In `normalize_symbol`, after `s = s.rstrip("+")` and before `crypto = _normalize_crypto(s)`, insert:

```python
    china_a = resolve_china_a_symbol(s)
    if china_a is not None:
        canonical = china_a.yahoo_symbol
        if canonical != raw.strip().upper():
            logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
        return canonical
```

- [ ] **Step 5: Run the focused tests and verify they pass**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_symbol_utils.py -q
```

Expected: all tests in `tests/test_symbol_utils.py` pass.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
rtk git add tradingagents/dataflows/symbol_utils.py tests/test_symbol_utils.py
rtk git commit -m "feat: resolve China A-share symbols"
```

---

### Task 2: Market-Aware Vendor Routing

**Files:**
- Modify: `tradingagents/default_config.py`
- Modify: `tradingagents/dataflows/interface.py`
- Modify: `tests/test_vendor_routing.py`
- Modify: `tests/test_dataflows_config.py`

**Interfaces:**
- Consumes: `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
- Produces:
  - `get_vendor(category: str, method: str | None = None, market: str | None = None) -> str`.
  - `_get_market_for_call(method: str, args: tuple, kwargs: dict) -> str | None`.
  - Existing `route_to_vendor(method, *args, **kwargs)` now selects `market_data_vendors.cn_a` for China A-share ticker-scoped calls.

- [ ] **Step 1: Write failing vendor-routing tests**

Append these tests to `VendorRoutingTests` in `tests/test_vendor_routing.py`:

```python
    def test_china_a_symbol_uses_market_specific_vendor_chain(self):
        set_config({
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            }
        })
        calls = []

        def akshare(symbol, *a, **k):
            calls.append(("akshare", symbol))
            return "AK_DATA"

        def baostock(symbol, *a, **k):
            calls.append(("baostock", symbol))
            return "BS_DATA"

        with self._route({
            "akshare": akshare,
            "baostock": baostock,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SH", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "AK_DATA")
        self.assertEqual(calls, [("akshare", "601138.SH")])

    def test_china_a_market_chain_falls_back_in_order(self):
        set_config({
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            }
        })
        with self._route({
            "akshare": _no_data,
            "baostock": _returns("BS_DATA"),
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SS", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "BS_DATA")

    def test_non_china_symbol_ignores_market_specific_vendor_chain(self):
        set_config({
            "data_vendors": {"core_stock_apis": "yfinance"},
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            },
        })
        akshare = mock.Mock(side_effect=_returns("AK_DATA"))
        with self._route({
            "akshare": akshare,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "AAPL", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "YF_DATA")
        akshare.assert_not_called()

    def test_tool_vendor_override_still_wins_for_china_a_symbol(self):
        set_config({
            "tool_vendors": {"get_stock_data": "yfinance"},
            "market_data_vendors": {
                "cn_a": {
                    "core_stock_apis": "akshare,baostock,yfinance",
                }
            },
        })
        akshare = mock.Mock(side_effect=_returns("AK_DATA"))
        with self._route({
            "akshare": akshare,
            "yfinance": _returns("YF_DATA"),
        }):
            result = interface.route_to_vendor(
                "get_stock_data", "601138.SH", "2026-06-01", "2026-06-29"
            )

        self.assertEqual(result, "YF_DATA")
        akshare.assert_not_called()
```

Append this test to `tests/test_dataflows_config.py`:

```python
    def test_market_data_vendors_default_contains_china_a_chain(self):
        fresh = get_config()
        self.assertEqual(
            fresh["market_data_vendors"]["cn_a"]["core_stock_apis"],
            "akshare,baostock,yfinance",
        )
        self.assertEqual(
            fresh["market_data_vendors"]["cn_a"]["news_data"],
            "akshare,yfinance",
        )
```

If `tests/test_dataflows_config.py` does not already import `get_config`, add:

```python
from tradingagents.dataflows.config import get_config
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
```

Expected: FAIL because `market_data_vendors` is missing and the router still uses category defaults.

- [ ] **Step 3: Add default China A-share vendor chains**

In `tradingagents/default_config.py`, after the existing `"data_vendors"` block and before `"tool_vendors"`, add:

```python
    "market_data_vendors": {
        "cn_a": {
            "core_stock_apis": "akshare,baostock,yfinance",
            "technical_indicators": "akshare,yfinance",
            "fundamental_data": "akshare,yfinance,baostock",
            "news_data": "akshare,yfinance",
        },
    },
```

- [ ] **Step 4: Add market detection to the router**

In `tradingagents/dataflows/interface.py`, add the import:

```python
from .symbol_utils import resolve_china_a_symbol
```

Add this constant after `OPTIONAL_CATEGORIES`:

```python
TICKER_SCOPED_METHODS = {
    "get_stock_data",
    "get_indicators",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_insider_transactions",
}
```

Add these helpers before `get_category_for_method`:

```python
def _first_symbol_arg(args, kwargs):
    if args:
        return args[0]
    for key in ("symbol", "ticker"):
        if key in kwargs:
            return kwargs[key]
    return None


def _get_market_for_call(method: str, args: tuple, kwargs: dict) -> str | None:
    if method not in TICKER_SCOPED_METHODS:
        return None
    symbol = _first_symbol_arg(args, kwargs)
    if not isinstance(symbol, str):
        return None
    if resolve_china_a_symbol(symbol) is not None:
        return "cn_a"
    return None
```

- [ ] **Step 5: Update `get_vendor` and `route_to_vendor`**

Replace `get_vendor` with:

```python
def get_vendor(category: str, method: str = None, market: str | None = None) -> str:
    """Get the configured vendor chain for a data category or specific tool method."""
    config = get_config()

    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    if market:
        market_vendors = config.get("market_data_vendors", {}).get(market, {})
        if category in market_vendors:
            return market_vendors[category]

    return config.get("data_vendors", {}).get(category, "default")
```

In `route_to_vendor`, replace:

```python
    vendor_config = get_vendor(category, method)
```

with:

```python
    market = _get_market_for_call(method, args, kwargs)
    vendor_config = get_vendor(category, method, market)
```

- [ ] **Step 6: Run the focused tests and verify they pass**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
rtk git add tradingagents/default_config.py tradingagents/dataflows/interface.py tests/test_vendor_routing.py tests/test_dataflows_config.py
rtk git commit -m "feat: route China A-shares to market vendors"
```

---

### Task 3: AKShare Primary Price Data And Indicators

**Files:**
- Create: `tradingagents/dataflows/akshare_data.py`
- Modify: `tradingagents/dataflows/interface.py`
- Create: `tests/test_akshare_data.py`
- Modify: `tests/test_vendor_routing.py`

**Interfaces:**
- Consumes:
  - `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
  - `_assert_ohlcv_not_stale(data: pd.DataFrame, curr_date: str, symbol: str, canonical: str | None = None)`.
- Produces:
  - `get_stock_data(symbol: str, start_date: str, end_date: str) -> str`.
  - `load_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame`.
  - `get_stock_stats_indicators_window(symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str`.

- [ ] **Step 1: Write failing AKShare OHLCV tests**

Create `tests/test_akshare_data.py`:

```python
import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows import akshare_data


def _hist_frame():
    return pd.DataFrame({
        "日期": pd.to_datetime(["2026-06-27", "2026-06-29"]).date,
        "股票代码": ["601138", "601138"],
        "开盘": [68.0, 69.3],
        "收盘": [70.0, 69.61],
        "最高": [71.0, 71.46],
        "最低": [67.5, 66.5],
        "成交量": [1000, 1964970],
        "成交额": [70000.0, 13558013854.0],
        "涨跌幅": [1.2, -0.87],
        "换手率": [0.5, 0.99],
    })


@pytest.mark.unit
def test_get_stock_data_formats_akshare_ohlcv(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: _hist_frame(),
    )

    out = akshare_data.get_stock_data("601138.SH", "2026-06-01", "2026-06-29")

    assert "# Stock data for 601138.SS" in out
    assert "# Primary source: AKShare stock_zh_a_hist" in out
    assert "Date,Open,High,Low,Close,Volume,Amount" in out
    assert "2026-06-29,69.3,71.46,66.5,69.61,1964970,13558013854.0" in out


@pytest.mark.unit
def test_get_stock_data_rejects_non_china_symbol():
    with pytest.raises(NoMarketDataError):
        akshare_data.get_stock_data("AAPL", "2026-06-01", "2026-06-29")


@pytest.mark.unit
def test_get_stock_data_empty_frame_raises_no_market_data(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: pd.DataFrame(),
    )

    with pytest.raises(NoMarketDataError) as exc:
        akshare_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    assert "AKShare returned no rows" in str(exc.value)


@pytest.mark.unit
def test_indicator_uses_akshare_ohlcv(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zh_a_hist",
        lambda **kwargs: _hist_frame(),
    )

    out = akshare_data.get_stock_stats_indicators_window(
        "601138.SH", "close_10_ema", "2026-06-29", 1
    )

    assert "## close_10_ema values" in out
    assert "2026-06-29:" in out
    assert "AKShare stock_zh_a_hist" in out
```

- [ ] **Step 2: Run the AKShare tests and verify they fail**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_akshare_data.py -q
```

Expected: FAIL because `tradingagents.dataflows.akshare_data` does not exist.

- [ ] **Step 3: Create the AKShare data module**

Create `tradingagents/dataflows/akshare_data.py`:

```python
from __future__ import annotations

from datetime import datetime

import akshare as ak
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .errors import NoMarketDataError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol


INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. Usage: Identify trend direction "
        "and serve as dynamic support/resistance. Tips: It lags price; combine "
        "with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend "
        "and identify golden/death cross setups. Tips: It reacts slowly; best for "
        "strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. Usage: Capture quick shifts in "
        "momentum and potential entry points. Tips: Prone to noise in choppy markets; "
        "use alongside longer averages for filtering false signals."
    ),
    "macd": "MACD: Computes momentum via differences of EMAs.",
    "macds": "MACD Signal: An EMA smoothing of the MACD line.",
    "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal.",
    "rsi": "RSI: Measures momentum to flag overbought/oversold conditions.",
    "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line.",
    "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line.",
    "atr": "ATR: Averages true range to measure volatility.",
    "vwma": "VWMA: A moving average weighted by volume.",
    "mfi": "MFI: Money Flow Index uses price and volume to measure buying/selling pressure.",
}


def _require_china_a(symbol: str):
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "AKShare supports China A-share symbols only")
    return instrument


def _date_for_akshare(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")


def _normalize_hist_frame(data: pd.DataFrame, symbol: str, canonical: str) -> pd.DataFrame:
    if data is None or data.empty:
        raise NoMarketDataError(symbol, canonical, "AKShare returned no rows")

    rename_map = {
        "日期": "Date",
        "开盘": "Open",
        "最高": "High",
        "最低": "Low",
        "收盘": "Close",
        "成交量": "Volume",
        "成交额": "Amount",
    }
    missing = [col for col in rename_map if col not in data.columns]
    if missing:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"AKShare history missing expected columns: {', '.join(missing)}",
        )

    frame = data.rename(columns=rename_map).copy()
    keep = ["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]
    frame = frame[keep]
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Close"])
    frame = frame.sort_values("Date")
    if frame.empty:
        raise NoMarketDataError(symbol, canonical, "AKShare returned no usable rows")
    return frame


def _fetch_hist(symbol: str, start_date: str, end_date: str) -> tuple[str, pd.DataFrame]:
    instrument = _require_china_a(symbol)
    raw = ak.stock_zh_a_hist(
        symbol=instrument.akshare_code,
        period="daily",
        start_date=_date_for_akshare(start_date),
        end_date=_date_for_akshare(end_date),
        adjust="qfq",
    )
    frame = _normalize_hist_frame(raw, symbol, instrument.yahoo_symbol)
    _assert_ohlcv_not_stale(frame, end_date, symbol, instrument.yahoo_symbol)
    return instrument.yahoo_symbol, frame


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    canonical, frame = _fetch_hist(symbol, start_date, end_date)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    header = f"# Stock data for {canonical} from {start_date} to {end_date}\n"
    header += "# Primary source: AKShare stock_zh_a_hist\n"
    header += "# Fallback source used: none\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


def load_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    curr = pd.to_datetime(curr_date)
    start = (curr - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    canonical, frame = _fetch_hist(symbol, start, curr.strftime("%Y-%m-%d"))
    filtered = frame[frame["Date"] <= curr].copy()
    _assert_ohlcv_not_stale(filtered, curr_date, symbol, canonical)
    return filtered


def _indicator_values(symbol: str, indicator: str, curr_date: str) -> dict[str, str]:
    data = load_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df[indicator]
    values = {}
    for _, row in df.iterrows():
        value = row[indicator]
        values[row["Date"]] = "N/A" if pd.isna(value) else str(value)
    return values


def get_stock_stats_indicators_window(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int,
) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)
    values = _indicator_values(symbol, indicator, curr_date)
    lines = []
    current = curr_date_dt
    while current >= before:
        date_str = current.strftime("%Y-%m-%d")
        lines.append(f"{date_str}: {values.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}")
        current = current - relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\nSource: AKShare stock_zh_a_hist\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )
```

- [ ] **Step 4: Register AKShare price and indicator functions**

In `tradingagents/dataflows/interface.py`, add imports near the top:

```python
from .akshare_data import (
    get_stock_data as get_akshare_stock,
    get_stock_stats_indicators_window as get_akshare_stock_stats_indicators_window,
)
```

Add `"akshare"` to `VENDOR_LIST`:

```python
VENDOR_LIST = [
    "yfinance",
    "fred",
    "polymarket",
    "alpha_vantage",
    "akshare",
]
```

Update `VENDOR_METHODS`:

```python
    "get_stock_data": {
        "akshare": get_akshare_stock,
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
```

and:

```python
    "get_indicators": {
        "akshare": get_akshare_stock_stats_indicators_window,
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_akshare_data.py tests/test_vendor_routing.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
rtk git add tradingagents/dataflows/akshare_data.py tradingagents/dataflows/interface.py tests/test_akshare_data.py tests/test_vendor_routing.py
rtk git commit -m "feat: add AKShare price and indicator data"
```

---

### Task 4: AKShare News, Fundamentals, And Yahoo Supplemental Profile

**Files:**
- Modify: `tradingagents/dataflows/akshare_data.py`
- Modify: `tradingagents/dataflows/interface.py`
- Modify: `tests/test_akshare_data.py`
- Modify: `tradingagents/agents/utils/agent_utils.py`
- Modify: `tests/test_symbol_normalization_paths.py`

**Interfaces:**
- Consumes:
  - `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
  - yfinance `get_fundamentals(ticker, curr_date=None) -> str` as supplemental profile text.
- Produces:
  - `get_news(ticker: str, start_date: str, end_date: str) -> str`.
  - `get_fundamentals(ticker: str, curr_date: str | None = None) -> str`.
  - `get_china_a_identity(ticker: str) -> dict[str, str]`.

- [ ] **Step 1: Add failing tests for news and fundamentals**

Append to `tests/test_akshare_data.py`:

```python
@pytest.mark.unit
def test_get_news_filters_to_requested_window(monkeypatch):
    news = pd.DataFrame({
        "关键词": ["601138", "601138"],
        "新闻标题": ["inside window", "outside window"],
        "新闻内容": ["kept body", "old body"],
        "发布时间": ["2026-06-26 16:30:06", "2026-05-01 09:00:00"],
        "文章来源": ["财联社", "证券时报"],
        "新闻链接": ["https://example.test/1", "https://example.test/2"],
    })
    monkeypatch.setattr(akshare_data.ak, "stock_news_em", lambda symbol: news)

    out = akshare_data.get_news("601138.SH", "2026-06-20", "2026-06-29")

    assert "## 601138.SS News" in out
    assert "inside window" in out
    assert "kept body" in out
    assert "outside window" not in out


@pytest.mark.unit
def test_get_fundamentals_degrades_failed_optional_endpoint(monkeypatch):
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_zyjs_ths",
        lambda symbol: pd.DataFrame({
            "股票代码": ["601138"],
            "主营业务": ["设计、研发、制造和销售电子设备产品。"],
            "产品类型": ["3C电子产品"],
            "产品名称": ["3C电子产品"],
            "经营范围": ["工业互联网技术研发。"],
        }),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_financial_abstract",
        lambda symbol: pd.DataFrame({
            "选项": ["常用指标"],
            "指标": ["净利润"],
            "20260331": ["100"],
            "20251231": ["90"],
        }),
    )
    monkeypatch.setattr(
        akshare_data.ak,
        "stock_individual_fund_flow",
        lambda stock, market: (_ for _ in ()).throw(ValueError("shape mismatch")),
    )

    out = akshare_data.get_fundamentals("601138.SS", "2026-06-29")

    assert "# Company Fundamentals for 601138.SS" in out
    assert "Primary source: AKShare" in out
    assert "设计、研发、制造和销售电子设备产品" in out
    assert "净利润" in out
    assert "DATA_DEGRADED: AKShare stock_individual_fund_flow unavailable" in out
```

Append to `tests/test_symbol_normalization_paths.py`:

```python
def test_identity_lookup_for_china_a_uses_yahoo_canonical(monkeypatch):
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        @property
        def info(self):
            return {
                "longName": "Foxconn Industrial Internet Co., Ltd.",
                "sector": "Technology",
                "industry": "Communication Equipment",
                "exchange": "SHH",
                "quoteType": "EQUITY",
            }

    monkeypatch.setattr(au.yf, "Ticker", FakeTicker)
    au.resolve_instrument_identity.cache_clear()

    identity = au.resolve_instrument_identity("601138.SH")

    assert seen["symbol"] == "601138.SS"
    assert identity["company_name"] == "Foxconn Industrial Internet Co., Ltd."
    assert identity["sector"] == "Technology"


def test_identity_lookup_for_china_a_falls_back_to_akshare(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            pass

        @property
        def info(self):
            return {}

    monkeypatch.setattr(au.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(
        au,
        "get_china_a_identity",
        lambda ticker: {"company_name": "工业富联", "exchange": "shanghai"},
    )
    au.resolve_instrument_identity.cache_clear()

    identity = au.resolve_instrument_identity("601138.SH")

    assert identity["company_name"] == "工业富联"
    assert identity["exchange"] == "shanghai"
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_akshare_data.py tests/test_symbol_normalization_paths.py -q
```

Expected: FAIL because AKShare news/fundamentals and identity fallback functions are missing.

- [ ] **Step 3: Add AKShare news and fundamentals helpers**

Append to `tradingagents/dataflows/akshare_data.py`:

```python
def _format_optional_error(endpoint: str, exc: Exception) -> str:
    return f"DATA_DEGRADED: AKShare {endpoint} unavailable ({exc})."


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    instrument = _require_china_a(ticker)
    raw = ak.stock_news_em(symbol=instrument.akshare_code)
    if raw is None or raw.empty:
        return f"No news found for {ticker} (resolved to {instrument.yahoo_symbol})"

    frame = raw.copy()
    frame["发布时间"] = pd.to_datetime(frame["发布时间"], errors="coerce")
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
    frame = frame[(frame["发布时间"] >= start) & (frame["发布时间"] <= end)]
    if frame.empty:
        return (
            f"No news found for {ticker} (resolved to {instrument.yahoo_symbol}) "
            f"between {start_date} and {end_date}"
        )

    lines = [
        f"## {instrument.yahoo_symbol} News, from {start_date} to {end_date}:",
        "",
        "Source: AKShare stock_news_em",
        "",
    ]
    for _, row in frame.iterrows():
        title = row.get("新闻标题", "No title")
        source = row.get("文章来源", "Unknown")
        lines.append(f"### {title} (source: {source})")
        content = row.get("新闻内容")
        if isinstance(content, str) and content.strip():
            lines.append(content.strip())
        link = row.get("新闻链接")
        if isinstance(link, str) and link.strip():
            lines.append(f"Link: {link.strip()}")
        lines.append("")
    return "\n".join(lines)


def _safe_frame(endpoint: str, fn):
    try:
        data = fn()
    except Exception as exc:
        return None, _format_optional_error(endpoint, exc)
    if data is None or data.empty:
        return None, f"DATA_DEGRADED: AKShare {endpoint} returned no rows."
    return data, None


def _business_section(code: str) -> tuple[list[str], list[str]]:
    data, error = _safe_frame("stock_zyjs_ths", lambda: ak.stock_zyjs_ths(symbol=code))
    if error:
        return [], [error]
    row = data.iloc[0]
    lines = ["## Business Description"]
    for col in ["主营业务", "产品类型", "产品名称", "经营范围"]:
        value = row.get(col)
        if pd.notna(value) and str(value).strip():
            lines.append(f"{col}: {value}")
    return lines, []


def _financial_abstract_section(code: str) -> tuple[list[str], list[str]]:
    data, error = _safe_frame(
        "stock_financial_abstract", lambda: ak.stock_financial_abstract(symbol=code)
    )
    if error:
        return [], [error]
    latest_cols = [c for c in data.columns if str(c).isdigit()]
    latest_cols = sorted(latest_cols, reverse=True)[:4]
    if not latest_cols:
        return [], ["DATA_DEGRADED: AKShare stock_financial_abstract returned no period columns."]
    keep = ["选项", "指标", *latest_cols]
    lines = ["## Financial Abstract", data[keep].head(20).to_csv(index=False)]
    return lines, []


def _fund_flow_section(code: str, exchange: str) -> tuple[list[str], list[str]]:
    market = "sh" if exchange == "shanghai" else "sz"
    data, error = _safe_frame(
        "stock_individual_fund_flow",
        lambda: ak.stock_individual_fund_flow(stock=code, market=market),
    )
    if error:
        return [], [error]
    keep = [col for col in ["日期", "收盘价", "涨跌幅", "主力净流入-净额", "主力净流入-净占比"] if col in data.columns]
    lines = ["## Recent Fund Flow", data[keep].tail(10).to_csv(index=False)]
    return lines, []


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    instrument = _require_china_a(ticker)
    sections = [
        f"# Company Fundamentals for {instrument.yahoo_symbol}",
        "# Primary source: AKShare",
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    degraded = []

    for builder in (
        lambda: _business_section(instrument.akshare_code),
        lambda: _financial_abstract_section(instrument.akshare_code),
        lambda: _fund_flow_section(instrument.akshare_code, instrument.exchange),
    ):
        lines, errors = builder()
        if lines:
            sections.extend(lines)
            sections.append("")
        degraded.extend(errors)

    if degraded:
        sections.append("## Degraded Fields")
        sections.extend(degraded)
        sections.append("Do not fabricate degraded or missing AKShare values.")

    return "\n".join(sections)


def get_china_a_identity(ticker: str) -> dict[str, str]:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        return {}
    data, error = _safe_frame(
        "stock_zyjs_ths", lambda: ak.stock_zyjs_ths(symbol=instrument.akshare_code)
    )
    identity = {"exchange": instrument.exchange}
    if error or data is None:
        return identity
    row = data.iloc[0]
    business = row.get("主营业务")
    if pd.notna(business) and str(business).strip():
        identity["industry"] = str(business).strip()
    return identity
```

- [ ] **Step 4: Register AKShare news and fundamentals**

In `tradingagents/dataflows/interface.py`, extend the AKShare import:

```python
from .akshare_data import (
    get_fundamentals as get_akshare_fundamentals,
    get_news as get_akshare_news,
    get_stock_data as get_akshare_stock,
    get_stock_stats_indicators_window as get_akshare_stock_stats_indicators_window,
)
```

Update `VENDOR_METHODS`:

```python
    "get_fundamentals": {
        "akshare": get_akshare_fundamentals,
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
```

and:

```python
    "get_news": {
        "akshare": get_akshare_news,
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
```

- [ ] **Step 5: Add China identity fallback to `agent_utils`**

In `tradingagents/agents/utils/agent_utils.py`, add:

```python
from tradingagents.dataflows.akshare_data import get_china_a_identity
```

inside `resolve_instrument_identity`, after the loop that fills `identity` and before `return identity`, insert:

```python
    if not identity:
        identity.update(get_china_a_identity(ticker))
```

This keeps Yahoo as the first supplemental identity source and uses AKShare only when Yahoo returns no usable fields.

- [ ] **Step 6: Run focused tests**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_akshare_data.py tests/test_symbol_normalization_paths.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 4**

Run:

```bash
rtk git add tradingagents/dataflows/akshare_data.py tradingagents/dataflows/interface.py tradingagents/agents/utils/agent_utils.py tests/test_akshare_data.py tests/test_symbol_normalization_paths.py
rtk git commit -m "feat: add AKShare A-share news and fundamentals"
```

---

### Task 5: Baostock Narrow Fallback

**Files:**
- Create: `tradingagents/dataflows/baostock_data.py`
- Create: `tests/test_baostock_data.py`
- Modify: `tradingagents/dataflows/interface.py`

**Interfaces:**
- Consumes: `resolve_china_a_symbol(raw: str) -> ChinaAInstrument | None`.
- Produces:
  - `get_stock_data(symbol: str, start_date: str, end_date: str) -> str`.
  - `get_fundamentals(ticker: str, curr_date: str | None = None) -> str`.

- [ ] **Step 1: Write failing Baostock tests**

Create `tests/test_baostock_data.py`:

```python
import pytest

from tradingagents.dataflows import baostock_data


class FakeLogin:
    error_code = "0"
    error_msg = ""


class FakeQuery:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self.rows = rows
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


@pytest.mark.unit
def test_get_stock_data_formats_baostock_rows(monkeypatch):
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery([
            ["2026-06-29", "sh.601138", "69.3", "71.46", "66.5", "69.61", "1964970", "13558013854.0"],
        ]),
    )

    out = baostock_data.get_stock_data("601138.SH", "2026-06-01", "2026-06-29")

    assert "# Stock data for 601138.SS" in out
    assert "# Primary source: Baostock query_history_k_data_plus" in out
    assert "2026-06-29,69.3,71.46,66.5,69.61,1964970,13558013854.0" in out


@pytest.mark.unit
def test_baostock_logout_runs_after_query(monkeypatch):
    events = []
    monkeypatch.setattr(baostock_data.bs, "login", lambda: events.append("login") or FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: events.append("logout"))
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery([
            ["2026-06-29", "sh.601138", "69.3", "71.46", "66.5", "69.61", "1964970", "13558013854.0"],
        ]),
    )

    baostock_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    assert events == ["login", "logout"]
```

- [ ] **Step 2: Run Baostock tests and verify they fail**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_baostock_data.py -q
```

Expected: FAIL because `tradingagents.dataflows.baostock_data` does not exist.

- [ ] **Step 3: Create Baostock fallback module**

Create `tradingagents/dataflows/baostock_data.py`:

```python
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import baostock as bs
import pandas as pd

from .errors import NoMarketDataError, VendorNotConfiguredError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol


FIELDS = "date,code,open,high,low,close,volume,amount"


@contextmanager
def _session():
    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise VendorNotConfiguredError(f"Baostock login failed: {login.error_msg}")
    try:
        yield
    finally:
        bs.logout()


def _rows_to_frame(rows: list[list[str]], symbol: str, canonical: str) -> pd.DataFrame:
    if not rows:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no rows")
    frame = pd.DataFrame(
        rows,
        columns=["Date", "Code", "Open", "High", "Low", "Close", "Volume", "Amount"],
    )
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]]
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Close"])
    if frame.empty:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no usable rows")
    return frame.sort_values("Date")


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")

    with _session():
        rs = bs.query_history_k_data_plus(
            instrument.baostock_code,
            FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise NoMarketDataError(symbol, instrument.yahoo_symbol, rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

    frame = _rows_to_frame(rows, symbol, instrument.yahoo_symbol)
    _assert_ohlcv_not_stale(frame, end_date, symbol, instrument.yahoo_symbol)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    header = f"# Stock data for {instrument.yahoo_symbol} from {start_date} to {end_date}\n"
    header += "# Primary source: Baostock query_history_k_data_plus\n"
    header += "# Fallback source used: Baostock\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        raise NoMarketDataError(ticker, ticker, "Baostock supports China A-share symbols only")
    return (
        f"# Company Fundamentals for {instrument.yahoo_symbol}\n"
        "# Primary source: Baostock\n"
        "Baostock fallback fundamentals are limited in phase 1. "
        "Use AKShare and Yahoo supplemental fundamentals when available."
    )
```

- [ ] **Step 4: Register Baostock vendor functions**

In `tradingagents/dataflows/interface.py`, add:

```python
from .baostock_data import (
    get_fundamentals as get_baostock_fundamentals,
    get_stock_data as get_baostock_stock,
)
```

Add `"baostock"` to `VENDOR_LIST` after `"akshare"`:

```python
    "akshare",
    "baostock",
```

Update `VENDOR_METHODS`:

```python
    "get_stock_data": {
        "akshare": get_akshare_stock,
        "baostock": get_baostock_stock,
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
```

and:

```python
    "get_fundamentals": {
        "akshare": get_akshare_fundamentals,
        "baostock": get_baostock_fundamentals,
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_baostock_data.py tests/test_vendor_routing.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
rtk git add tradingagents/dataflows/baostock_data.py tradingagents/dataflows/interface.py tests/test_baostock_data.py
rtk git commit -m "feat: add Baostock A-share fallback"
```

---

### Task 6: Regression Tests And Manual Smoke Checklist

**Files:**
- Modify: `docs/superpowers/specs/2026-06-29-akshare-china-a-share-design.md`
- Test only: existing test suite

**Interfaces:**
- Consumes: All functions from Tasks 1-5.
- Produces: A documented verification record for focused tests, broader regressions, real data smoke, and DeepSeek CLI smoke.

- [ ] **Step 1: Record implementation completion in the design spec**

Append this section to `docs/superpowers/specs/2026-06-29-akshare-china-a-share-design.md`:

```markdown
## Implementation Verification

Implementation must complete the focused unit tests, broader dataflow regressions,
real data smoke for `601138.SH` and `600895.SH`, and manual CLI smoke with DeepSeek.
```

- [ ] **Step 2: Run focused regression tests**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_symbol_utils.py tests/test_vendor_routing.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_symbol_normalization_paths.py tests/test_china_dependencies.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run broader dataflow regressions**

Run:

```bash
rtk .\.venv\Scripts\python.exe -m pytest tests/test_no_data_handling.py tests/test_vendor_errors.py tests/test_yfinance_stale_ohlcv_guard.py tests/test_date_boundaries.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run real data smoke without invoking the full LLM graph**

Run:

```bash
rtk .\.venv\Scripts\python.exe -c "from tradingagents.dataflows.interface import route_to_vendor; print(route_to_vendor('get_stock_data', '601138.SH', '2026-06-01', '2026-06-29').splitlines()[:6]); print(route_to_vendor('get_news', '600895.SH', '2026-06-20', '2026-06-29')[:500])"
```

Expected:

- The stock-data output contains `601138.SS` and `Primary source: AKShare`.
- The news output contains `600895.SS News` or a clear `No news found` message from AKShare, not a Yahoo-only empty result.

- [ ] **Step 5: Run manual CLI smoke with DeepSeek**

Run:

```bash
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\start_tradingagents.ps1
```

In the interactive flow:

```text
Ticker: 601138.SH
Model provider/model choice: DeepSeek
Analysts: Market, News, Fundamentals
```

Expected:

- The report keeps the instrument as `601138.SS` or clearly notes `601138.SH` resolved to `601138.SS`.
- Market data source attribution says AKShare.
- News contains AKShare ticker-specific news when available.
- Missing optional fields are reported as degraded or unavailable.
- The final report does not fabricate fund-flow, news, or financial values when a source is unavailable.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
rtk git add docs/superpowers/specs/2026-06-29-akshare-china-a-share-design.md
rtk git commit -m "docs: record China A-share verification plan"
```

---

## Final Review Checklist

- [ ] `rtk git status --short` is clean.
- [ ] `601138.SS`, `601138.SH`, and `601138` all resolve to Yahoo `601138.SS`.
- [ ] `600895.SS`, `600895.SH`, and `600895` all resolve to Yahoo `600895.SS`.
- [ ] `000001.SZ` and `000001` both resolve to Yahoo `000001.SZ`.
- [ ] Non-China symbols keep the previous yfinance behavior.
- [ ] A-share price vendor order is AKShare, Baostock, yfinance.
- [ ] A-share news vendor order is AKShare, yfinance.
- [ ] AKShare optional endpoint failures produce `DATA_DEGRADED` text and do not abort the whole tool call.
- [ ] Core price failure after all configured vendors produces `NO_DATA_AVAILABLE`.
- [ ] Manual CLI smoke uses DeepSeek in the model-selection step.
