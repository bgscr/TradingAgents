# BaoStock Session Serialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent TradingAgents calls from corrupting BaoStock's process-global socket, while bounding lock waiters and preserving concurrency for unrelated providers.

**Architecture:** Put one non-reentrant, process-wide lock at the BaoStock adapter's session boundary. Acquire it with a 30-second timeout before login, eagerly consume all query rows while it is held, and release it after logout in every success or failure path; the existing vendor router will fall back after a logged `TimeoutError`.

**Tech Stack:** Python 3.13, `threading.Lock`, context managers, pytest, existing TradingAgents vendor routing.

## Global Constraints

- Use `_BAOSTOCK_SESSION_LOCK = threading.Lock()`; do not use `threading.RLock()`.
- Use a private 30-second lock-acquisition timeout constant; add no user-facing configuration.
- A lock timeout must log a warning and raise built-in `TimeoutError` so the existing generic vendor-fallback path remains unchanged.
- Hold the lock only for login, BaoStock query execution, eager row extraction, and logout; DataFrame conversion, indicator computation, formatting, and agent work remain outside it.
- Preserve the existing China A-share vendor order, public function signatures, adjusted-price behavior, cache behavior, and report contents.
- Do not serialize the market `ToolNode` or unrelated providers.
- Do not add a generic rate-limiter, retry framework, new vendor-error type, worker process, or network-cancellation mechanism.
- All shell commands in this worktree must run through `rtk`; PowerShell commands must use `rtk pwsh`, never legacy `powershell`.

---

## File Structure

- Modify `tradingagents/dataflows/baostock_data.py`: own the BaoStock process-global session lock, bounded acquisition, warning, and guaranteed release.
- Modify `tests/test_baostock_data.py`: add deterministic concurrency, nested-entry, waiter-timeout, and exception-release coverage using fake BaoStock calls only.

### Task 1: Serialize and Bound BaoStock Sessions

**Files:**
- Modify: `tests/test_baostock_data.py`
- Modify: `tradingagents/dataflows/baostock_data.py:1-32`

**Interfaces:**
- Consumes: existing `_session()` context manager and existing public `get_stock_data(symbol: str, start_date: str, end_date: str) -> str`.
- Produces: private `_BAOSTOCK_SESSION_LOCK: threading.Lock`, private `_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS = 30.0`, and unchanged public dataflow APIs.

- [ ] **Step 1: Add the failing concurrency and safety tests**

Add these imports to `tests/test_baostock_data.py`:

```python
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
```

Keep the existing `pytest` import only once. Add the following tests after the existing session/output tests and before the fundamental/indicator tests:

```python
@pytest.mark.unit
def test_baostock_sessions_serialize_concurrent_callers_without_cross_contamination(
    monkeypatch,
):
    symbols = {
        "600895.SH": ("sh.600895", "600895.SS", "35.17"),
        "601658.SH": ("sh.601658", "601658.SS", "4.94"),
        "600000.SH": ("sh.600000", "600000.SS", "12.31"),
        "600036.SH": ("sh.600036", "600036.SS", "41.28"),
    }
    first_code = symbols["600895.SH"][0]
    first_query_entered = threading.Event()
    release_first_query = threading.Event()
    waiter_query_entered = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def fake_login():
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        return FakeLogin()

    def fake_logout():
        with state_lock:
            state["active"] -= 1

    def fake_query(code, *args, **kwargs):
        if code == first_code:
            first_query_entered.set()
            assert release_first_query.wait(timeout=2.0)
        else:
            waiter_query_entered.set()
        price = next(value[2] for value in symbols.values() if value[0] == code)
        return FakeQuery(
            [["2026-06-29", code, price, price, price, price, "1000", "10000"]]
        )

    monkeypatch.setattr(baostock_data.bs, "login", fake_login)
    monkeypatch.setattr(baostock_data.bs, "logout", fake_logout)
    monkeypatch.setattr(baostock_data.bs, "query_history_k_data_plus", fake_query)

    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        first = pool.submit(
            baostock_data.get_stock_data,
            "600895.SH",
            "2026-06-29",
            "2026-06-29",
        )
        assert first_query_entered.wait(timeout=1.0)
        waiters = [
            (
                symbol,
                pool.submit(
                    baostock_data.get_stock_data,
                    symbol,
                    "2026-06-29",
                    "2026-06-29",
                ),
            )
            for symbol in symbols
            if symbol != "600895.SH"
        ]
        try:
            overlapped = waiter_query_entered.wait(timeout=0.25)
        finally:
            release_first_query.set()

        outputs = {"600895.SH": first.result(timeout=2.0)}
        outputs.update(
            {symbol: future.result(timeout=2.0) for symbol, future in waiters}
        )

    assert overlapped is False
    assert state == {"active": 0, "max_active": 1}
    for symbol, (_, canonical, price) in symbols.items():
        assert f"# Stock data for {canonical}" in outputs[symbol]
        assert f"2026-06-29,{price},{price},{price},{price},1000,10000" in outputs[symbol]


@pytest.mark.unit
def test_baostock_nested_session_times_out_instead_of_reentering(
    monkeypatch, caplog
):
    events = []
    monkeypatch.setattr(
        baostock_data,
        "_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "login",
        lambda: events.append("login") or FakeLogin(),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "logout",
        lambda: events.append("logout"),
    )

    with caplog.at_level("WARNING", logger=baostock_data.__name__):
        with baostock_data._session():
            with pytest.raises(TimeoutError, match="waiting for the BaoStock session lock"):
                with baostock_data._session():
                    pytest.fail("nested BaoStock session must not be entered")

    assert events == ["login", "logout"]
    assert "another session may be hung" in caplog.text


@pytest.mark.unit
def test_baostock_waiter_times_out_without_starting_a_second_login(
    monkeypatch, caplog
):
    holder_entered = threading.Event()
    release_holder = threading.Event()
    login_calls = 0

    def fake_login():
        nonlocal login_calls
        login_calls += 1
        return FakeLogin()

    def hold_session():
        with baostock_data._session():
            holder_entered.set()
            assert release_holder.wait(timeout=2.0)

    monkeypatch.setattr(
        baostock_data,
        "_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(baostock_data.bs, "login", fake_login)
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)

    holder = threading.Thread(target=hold_session)
    holder.start()
    assert holder_entered.wait(timeout=1.0)
    try:
        with caplog.at_level("WARNING", logger=baostock_data.__name__):
            with pytest.raises(TimeoutError, match="waiting for the BaoStock session lock"):
                with baostock_data._session():
                    pytest.fail("timed-out waiter must not enter the session")
    finally:
        release_holder.set()
        holder.join(timeout=2.0)

    assert holder.is_alive() is False
    assert login_calls == 1
    assert "another session may be hung" in caplog.text


@pytest.mark.unit
def test_baostock_session_lock_is_released_after_query_exception(monkeypatch):
    query_calls = 0
    events = []

    def fake_query(*args, **kwargs):
        nonlocal query_calls
        query_calls += 1
        if query_calls == 1:
            raise RuntimeError("query failed")
        return FakeQuery(
            [[
                "2026-06-29",
                "sh.600895",
                "35.17",
                "35.17",
                "35.17",
                "35.17",
                "1000",
                "10000",
            ]]
        )

    monkeypatch.setattr(
        baostock_data.bs,
        "login",
        lambda: events.append("login") or FakeLogin(),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "logout",
        lambda: events.append("logout"),
    )
    monkeypatch.setattr(baostock_data.bs, "query_history_k_data_plus", fake_query)

    with pytest.raises(RuntimeError, match="query failed"):
        baostock_data.get_stock_data(
            "600895.SH", "2026-06-29", "2026-06-29"
        )

    out = baostock_data.get_stock_data(
        "600895.SH", "2026-06-29", "2026-06-29"
    )

    assert "# Stock data for 600895.SS" in out
    assert events == ["login", "logout", "login", "logout"]


@pytest.mark.unit
def test_baostock_session_lock_is_released_after_logout_exception(monkeypatch):
    logout_calls = 0

    def fake_logout():
        nonlocal logout_calls
        logout_calls += 1
        if logout_calls == 1:
            raise RuntimeError("logout failed")

    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", fake_logout)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeQuery(
            [[
                "2026-06-29",
                "sh.600895",
                "35.17",
                "35.17",
                "35.17",
                "35.17",
                "1000",
                "10000",
            ]]
        ),
    )

    with pytest.raises(RuntimeError, match="logout failed"):
        baostock_data.get_stock_data(
            "600895.SH", "2026-06-29", "2026-06-29"
        )

    out = baostock_data.get_stock_data(
        "600895.SH", "2026-06-29", "2026-06-29"
    )

    assert "# Stock data for 600895.SS" in out
    assert logout_calls == 2
```

- [ ] **Step 2: Run the new tests and verify the pre-fix failure**

Run:

```text
rtk pytest -q tests/test_baostock_data.py::test_baostock_sessions_serialize_concurrent_callers_without_cross_contamination tests/test_baostock_data.py::test_baostock_nested_session_times_out_instead_of_reentering tests/test_baostock_data.py::test_baostock_waiter_times_out_without_starting_a_second_login tests/test_baostock_data.py::test_baostock_session_lock_is_released_after_query_exception tests/test_baostock_data.py::test_baostock_session_lock_is_released_after_logout_exception
```

Expected: the concurrency test fails because `overlapped` is `True`; the nested and waiter tests fail because no lock timeout is enforced. The query/logout exception-release characterization tests may already pass.

- [ ] **Step 3: Implement the minimal BaoStock session guard**

Update the imports and module constants in `tradingagents/dataflows/baostock_data.py`:

```python
import contextlib
import io
import logging
import threading
from contextlib import contextmanager


logger = logging.getLogger(__name__)

FIELDS = "date,code,open,high,low,close,volume,amount"
_BAOSTOCK_SESSION_LOCK = threading.Lock()
_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS = 30.0
```

Replace `_session()` with:

```python
@contextmanager
def _session():
    acquired = _BAOSTOCK_SESSION_LOCK.acquire(
        timeout=_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS
    )
    if not acquired:
        message = (
            f"Timed out after {_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS:g}s "
            "waiting for the BaoStock session lock; another session may be hung."
        )
        logger.warning(message)
        raise TimeoutError(message)

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise VendorNotConfiguredError(f"Baostock login failed: {login.error_msg}")
        try:
            yield
        finally:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                bs.logout()
    finally:
        _BAOSTOCK_SESSION_LOCK.release()
```

Do not move `_rows_to_frame()`, `_assert_ohlcv_not_stale()`, CSV rendering, or indicator calculations inside either existing `_session()` block. The current eager `while rs.next(): rows.append(...)` loops remain inside the lock unchanged.

- [ ] **Step 4: Run the new tests and verify green**

Run the same four-test command from Step 2.

Expected: `5 passed` with no live threads left behind.

- [ ] **Step 5: Run the focused regression suite**

Run:

```text
rtk pytest -q tests/test_baostock_data.py tests/test_vendor_routing.py tests/test_market_toolnode.py
```

Expected: all focused tests pass; the baseline before this task was `33 passed`.

- [ ] **Step 6: Check formatting and scope**

Run:

```text
rtk ruff check tradingagents/dataflows/baostock_data.py tests/test_baostock_data.py
rtk ruff format --check tradingagents/dataflows/baostock_data.py tests/test_baostock_data.py
rtk pwsh -NoProfile -Command '& { git diff --check; Write-Output ("EXIT=" + $LASTEXITCODE) }'
rtk git status --short
```

Expected: Ruff reports no errors or format changes required; Git's diff check prints `EXIT=0`; only the two task files are modified before commit.

- [ ] **Step 7: Commit the implementation**

Run:

```text
rtk git add -- tests/test_baostock_data.py tradingagents/dataflows/baostock_data.py
rtk git commit -m "fix: serialize baostock sessions"
```

Expected: one implementation commit containing only the adapter and its tests.

### Final Verification

- [ ] Generate the task review package from the implementation task's recorded base commit to its head commit and obtain clean specification-compliance and code-quality verdicts.
- [ ] Run the complete test suite with `rtk pytest -q`; expect zero failures.
- [ ] Run the final whole-branch review from the branch merge base through `HEAD`; resolve every Critical or Important finding and re-review.
- [ ] Confirm `rtk git status --short` is empty and preserve the existing worktree until the user chooses how to integrate the branch.
