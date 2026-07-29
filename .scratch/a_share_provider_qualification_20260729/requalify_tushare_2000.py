"""PROTOTYPE ONLY: rerun the seven Tushare capabilities after a 2,000-point upgrade.

Question: does the upgraded account provide reproducible, sufficiently complete
Tushare financial, factor, suspension, and name-history payloads for the existing
four-symbol qualification cohort? This script writes only disposable scratch
artifacts and never persists or prints the token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from probe import END_DATE, START_DATE, SYMBOLS, failure

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
OUTPUTS = ROOT / "outputs"
RAW = OUTPUTS / "raw"
ENDPOINTS = (
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "adj_factor",
    "suspend_d",
    "namechange",
)
FINANCIAL_ENDPOINTS = {"income", "balancesheet", "cashflow", "fina_indicator"}
META_FIELDS = {
    "ts_code",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "end_type",
    "update_flag",
    "trade_date",
    "suspend_date",
    "resume_date",
    "start_date",
    "change_reason",
    "name",
}
METADATA_FIELDS = ("ann_date", "f_ann_date", "report_type", "comp_type", "update_flag")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def present(value: object) -> bool:
    try:
        if bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() not in {"", "--", "none", "null", "nan", "nat", "n/a"}


def json_rows(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))


def canonical_digest(rows: list[dict]) -> str:
    canonical_rows = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows]
    payload = "\n".join(sorted(canonical_rows)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def date_values(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame.columns:
        return []
    values = {
        str(value).replace("-", "")[:8]
        for value in frame[column]
        if present(value) and len(str(value).replace("-", "")) >= 8
    }
    return sorted(values, reverse=True)


def period_frame(frame: pd.DataFrame, metric_columns: list[str]) -> pd.DataFrame:
    if "end_date" not in frame.columns or frame.empty:
        return frame.copy()
    work = frame.copy()
    work["_period"] = work["end_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    work["_populated"] = work[metric_columns].apply(lambda row: sum(present(value) for value in row), axis=1)
    return (
        work.sort_values(["_period", "_populated"], ascending=[False, False])
        .drop_duplicates("_period")
        .drop(columns=["_populated"])
    )


def row_is_usable(row: pd.Series, columns: list[str]) -> bool:
    if not columns:
        return False
    populated = sum(present(row[column]) for column in columns)
    missing_ratio = 1.0 - populated / len(columns)
    required = min(10, max(3, math.ceil(len(columns) * 0.25)))
    return populated >= required and missing_ratio <= 0.50


def metadata_coverage(frame: pd.DataFrame, reporting_periods: list[str]) -> dict:
    output = {}
    period_series = None
    if "end_date" in frame.columns:
        period_series = frame["end_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    for field in METADATA_FIELDS:
        exists = field in frame.columns
        populated_rows = int(sum(present(value) for value in frame[field])) if exists else 0
        populated_periods = 0
        if exists and period_series is not None:
            populated_periods = sum(
                any(present(value) for value in frame.loc[period_series == period, field])
                for period in reporting_periods
            )
        values = sorted({str(value) for value in frame[field] if present(value)}) if exists else []
        output[field] = {
            "field_present": exists,
            "populated_rows": populated_rows,
            "row_coverage": round(populated_rows / len(frame), 4) if len(frame) else 0.0,
            "populated_reporting_periods": populated_periods,
            "reporting_period_coverage": round(populated_periods / len(reporting_periods), 4) if reporting_periods else 0.0,
            "values": values,
        }
    return output


def analyze_frame(endpoint: str, frame: pd.DataFrame) -> dict:
    columns = [str(column) for column in frame.columns]
    metric_columns = [column for column in columns if column not in META_FIELDS]
    active_columns = [column for column in metric_columns if any(present(value) for value in frame[column])]
    fully_missing_columns = sorted(set(metric_columns) - set(active_columns))

    def missing_ratio(selected: list[str]) -> float:
        if frame.empty or not selected:
            return 1.0
        total = len(frame) * len(selected)
        missing = sum(not present(value) for column in selected for value in frame[column])
        return round(missing / total, 4)

    field_missingness = []
    for column in metric_columns:
        ratio = round(sum(not present(value) for value in frame[column]) / len(frame), 4) if len(frame) else 1.0
        field_missingness.append({"field": column, "missing_ratio": ratio})
    field_missingness.sort(key=lambda item: (-item["missing_ratio"], item["field"]))

    reporting_periods = date_values(frame, "end_date") if endpoint in FINANCIAL_ENDPOINTS else []
    annual_periods = [period for period in reporting_periods if period[4:] == "1231"]
    interim_periods = [period for period in reporting_periods if period[4:] in {"0331", "0630", "0930"}]
    best_period_rows = (
        period_frame(frame, metric_columns)
        if endpoint in FINANCIAL_ENDPOINTS and metric_columns
        else frame.copy()
    )

    def usable_periods(columns_for_threshold: list[str]) -> list[str]:
        if endpoint not in FINANCIAL_ENDPOINTS or "_period" not in best_period_rows.columns:
            return []
        return [
            row["_period"]
            for _, row in best_period_rows.iterrows()
            if row_is_usable(row, columns_for_threshold)
        ]

    union_usable = usable_periods(metric_columns)
    active_usable = usable_periods(active_columns)
    latest_annual = annual_periods[:5]
    latest_reporting = reporting_periods[:8]
    return {
        "rows_returned": len(frame),
        "field_count": len(columns),
        "metric_field_count": len(metric_columns),
        "active_metric_field_count": len(active_columns),
        "fully_missing_metric_fields": fully_missing_columns,
        "union_schema_missing_ratio": missing_ratio(metric_columns),
        "active_schema_missing_ratio": missing_ratio(active_columns),
        "top_metric_field_missingness": field_missingness[:20],
        "reporting_periods": reporting_periods,
        "annual_periods": annual_periods,
        "interim_quarterly_periods": interim_periods,
        "latest_5_annual_periods": latest_annual,
        "latest_8_reporting_periods": latest_reporting,
        "usable_non_sparse_periods_union_schema": union_usable,
        "usable_non_sparse_periods_active_schema": active_usable,
        "usable_latest_5_annual_union_schema": [period for period in latest_annual if period in union_usable],
        "usable_latest_5_annual_active_schema": [period for period in latest_annual if period in active_usable],
        "usable_latest_8_reporting_union_schema": [period for period in latest_reporting if period in union_usable],
        "usable_latest_8_reporting_active_schema": [period for period in latest_reporting if period in active_usable],
        "event_dates": {
            "trade_date": date_values(frame, "trade_date"),
            "suspend_date": date_values(frame, "suspend_date"),
            "resume_date": date_values(frame, "resume_date"),
            "start_date": date_values(frame, "start_date"),
            "end_date": date_values(frame, "end_date") if endpoint == "namechange" else [],
        },
        "metadata": metadata_coverage(frame, reporting_periods),
    }


def endpoint_functions(pro, symbol: dict) -> dict:
    code = symbol["tushare"]
    return {
        "income": lambda: pro.income(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "balancesheet": lambda: pro.balancesheet(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "cashflow": lambda: pro.cashflow(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "fina_indicator": lambda: pro.fina_indicator(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "adj_factor": lambda: pro.adj_factor(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "suspend_d": lambda: pro.suspend_d(ts_code=code, start_date=START_DATE, end_date=END_DATE),
        "namechange": lambda: pro.namechange(ts_code=code),
    }


def run_pass(pro, run: int, min_interval_seconds: float) -> dict:
    records = []
    started = time.perf_counter()
    last_call = None
    call_index = 0
    for symbol in SYMBOLS:
        functions = endpoint_functions(pro, symbol)
        for endpoint in ENDPOINTS:
            if last_call is not None:
                remaining = min_interval_seconds - (time.perf_counter() - last_call)
                if remaining > 0:
                    time.sleep(remaining)
            call_index += 1
            call_started = time.perf_counter()
            last_call = call_started
            started_at = datetime.now(timezone.utc).isoformat()
            frame = pd.DataFrame()
            typed_failure = None
            try:
                value = functions[endpoint]()
                frame = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
            except Exception as exc:  # preserve each provider failure as a typed outcome
                typed_failure = failure(exc)
            latency_ms = round((time.perf_counter() - call_started) * 1000, 1)
            rows = json_rows(frame)
            records.append(
                {
                    "run": run,
                    "call_index": call_index,
                    "symbol_role": symbol["role"],
                    "requested_symbol": symbol["requested"],
                    "normalized_symbol": symbol["normalized"],
                    "native_symbol": symbol["tushare"],
                    "endpoint": endpoint,
                    "permission_status": "granted" if typed_failure is None else "denied_or_failed",
                    "status": "available" if rows else ("empty_success" if typed_failure is None else "unavailable"),
                    "started_at_utc": started_at,
                    "started_offset_ms": round((call_started - started) * 1000, 1),
                    "latency_ms": latency_ms,
                    "typed_failure": typed_failure["typed_failure"] if typed_failure else None,
                    "failure_detail": typed_failure["failure_detail"] if typed_failure else None,
                    "analysis": analyze_frame(endpoint, frame),
                    "schema": sorted(str(column) for column in frame.columns),
                    "schema_digest": hashlib.sha256("\n".join(sorted(str(column) for column in frame.columns)).encode("utf-8")).hexdigest(),
                    "payload_digest": canonical_digest(rows),
                    "rows": rows,
                }
            )
            print(
                json.dumps(
                    {
                        "run": run,
                        "call": call_index,
                        "symbol": symbol["normalized"],
                        "endpoint": endpoint,
                        "status": records[-1]["status"],
                        "rows": len(rows),
                        "latency_ms": latency_ms,
                        "typed_failure": records[-1]["typed_failure"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return {"run": run, "minimum_global_interval_seconds": min_interval_seconds, "records": records}


def max_attempts_in_window(records: list[dict], window_ms: float = 60000.0) -> int:
    offsets = sorted(record["started_offset_ms"] for record in records)
    best = 0
    left = 0
    for right, offset in enumerate(offsets):
        while offset - offsets[left] >= window_ms:
            left += 1
        best = max(best, right - left + 1)
    return best


def summarize(payloads: list[dict]) -> dict:
    grouped = defaultdict(list)
    for payload in payloads:
        for record in payload["records"]:
            grouped[(record["normalized_symbol"], record["endpoint"])].append(record)

    comparisons = []
    for (symbol, endpoint), records in sorted(grouped.items()):
        records.sort(key=lambda item: item["run"])
        first, second = records
        comparisons.append(
            {
                "normalized_symbol": symbol,
                "symbol_role": first["symbol_role"],
                "endpoint": endpoint,
                "permission_status_runs": [record["permission_status"] for record in records],
                "status_runs": [record["status"] for record in records],
                "typed_failure_runs": [record["typed_failure"] for record in records],
                "latency_ms_runs": [record["latency_ms"] for record in records],
                "period_sets_reproducible": first["analysis"]["reporting_periods"] == second["analysis"]["reporting_periods"],
                "schema_reproducible": first["schema_digest"] == second["schema_digest"],
                "metadata_reproducible": first["analysis"]["metadata"] == second["analysis"]["metadata"],
                "payload_reproducible": first["payload_digest"] == second["payload_digest"],
                "run_1_analysis": first["analysis"],
                "run_2_analysis": second["analysis"],
            }
        )

    frequency = []
    for payload in payloads:
        records = payload["records"]
        per_endpoint = {
            endpoint: max_attempts_in_window([record for record in records if record["endpoint"] == endpoint])
            for endpoint in ENDPOINTS
        }
        frequency.append(
            {
                "run": payload["run"],
                "minimum_global_interval_seconds": payload["minimum_global_interval_seconds"],
                "maximum_total_attempts_in_any_rolling_60_seconds": max_attempts_in_window(records),
                "maximum_attempts_per_endpoint_in_any_rolling_60_seconds": per_endpoint,
                "rate_limited_calls": sum(record["typed_failure"] == "RATE_LIMITED" for record in records),
            }
        )
    return {"comparisons": comparisons, "observed_request_frequency": frequency}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-global-interval", type=float, default=0.75)
    args = parser.parse_args()
    load_dotenv(PROJECT / ".env")
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise SystemExit("TUSHARE_TOKEN is unavailable; no calls were made.")

    import tushare as ts

    RAW.mkdir(parents=True, exist_ok=True)
    pro = ts.pro_api(token)
    payloads = []
    for run in (1, 2):
        payload = run_pass(pro, run, args.minimum_global_interval)
        payloads.append(payload)
        (RAW / f"tushare-2000-run-{run}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    summary = summarize(payloads)
    (OUTPUTS / "tushare-2000-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"runs": 2, "calls": sum(len(item["records"]) for item in payloads), **summary["observed_request_frequency"][-1]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
