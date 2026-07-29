"""PROTOTYPE ONLY: persist per-family BaoStock ratio coverage omitted by the aggregate."""

from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path

import baostock as bs
import pandas as pd

from probe import SYMBOLS, _bs_frame, analyze_frame, failure, merge_frames


ROOT = Path(__file__).resolve().parent
PERIODS = sorted({
    (2021, 4), (2022, 4), (2023, 4), (2024, 4), (2025, 4),
    (2024, 2), (2024, 3), (2025, 1), (2025, 2), (2025, 3), (2026, 1), (2026, 2),
})
FUNCTIONS = {
    "operation": bs.query_operation_data,
    "growth": bs.query_growth_data,
    "dupont": bs.query_dupont_data,
}


def main():
    records = []
    for run in (1, 2):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock login {login.error_code}: {login.error_msg}")
        try:
            for symbol in SYMBOLS:
                for family, func in FUNCTIONS.items():
                    frames, failures, latency = [], [], 0.0
                    for year, quarter in PERIODS:
                        started = time.perf_counter()
                        try:
                            frame = _bs_frame(func(symbol["baostock"], year=year, quarter=quarter))
                            if not frame.empty:
                                frames.append(frame)
                        except Exception as exc:
                            failures.append(failure(exc))
                        latency += (time.perf_counter() - started) * 1000
                    merged = merge_frames(frames)
                    analysis = analyze_frame(merged, max_periods=13)
                    records.append({
                        "run": run,
                        "provider": "baostock",
                        "provider_version": "0.9.3",
                        "normalized_symbol": symbol["normalized"],
                        "native_symbol": symbol["baostock"],
                        "family": family,
                        "endpoint": func.__name__,
                        "called": True,
                        "physical_requests": len(PERIODS),
                        "status": "available" if not merged.empty else "unavailable",
                        **analysis,
                        "latency_ms": round(latency, 1),
                        "typed_failure": failures[0]["typed_failure"] if failures and merged.empty else None,
                        "failure_detail": failures[0]["failure_detail"] if failures and merged.empty else None,
                        "unit": "mixed provider-defined ratios; not explicit",
                        "currency": "CNY inferred where monetary; not explicit",
                        "announcement_field": "pubDate" if "pubDate" in merged.columns else None,
                        "restatement_identifier": None,
                    })
        finally:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                bs.logout()
    (ROOT / "outputs" / "baostock_financial_api_details.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
