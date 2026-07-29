"""PROTOTYPE ONLY: one-command coordinator for the disposable qualification."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

from routing_model import score_policies


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
OUTPUTS = ROOT / "outputs"
RAW = OUTPUTS / "raw"
PROBE = ROOT / "probe.py"


def load_dotenv(path: Path):
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


def run_probes():
    load_dotenv(PROJECT / ".env")
    baseline = ROOT / "envs" / "baseline" / "Scripts" / "python.exe"
    latest = ROOT / "envs" / "latest-akshare" / "Scripts" / "python.exe"
    tasks = [
        ("akshare-current", baseline, "akshare"),
        ("akshare-latest", latest, "akshare"),
        ("baostock", baseline, "baostock"),
        ("tushare", baseline, "tushare"),
        ("yahoo", baseline, "yahoo"),
    ]
    RAW.mkdir(parents=True, exist_ok=True)
    state = {"phase": "running", "completed": [], "active": None}
    for label, python, provider in tasks:
        for run in (1, 2):
            state["active"] = f"{label} run {run}"
            print(json.dumps(state, ensure_ascii=False), flush=True)
            output = RAW / f"{label}-run-{run}.json"
            if output.exists():
                state["completed"].append(f"{state['active']} (reused scratch result)")
                continue
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(
                [
                    str(python), str(PROBE), "--provider", provider,
                    "--provider-label", label, "--run", str(run),
                    "--output", str(output), "--scratch", str(ROOT),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=1200,
            )
            if result.returncode != 0:
                stderr = result.stderr
                token = env.get("TUSHARE_TOKEN", "")
                if token:
                    stderr = stderr.replace(token, "<redacted>")
                raise RuntimeError(f"{label} run {run} failed: {stderr[-4000:]}")
            state["completed"].append(state["active"])
    state["phase"] = "complete"
    state["active"] = None
    print(json.dumps(state, ensure_ascii=False), flush=True)


def summarize(records):
    grouped = defaultdict(list)
    for item in records:
        grouped[(item["provider"], item["normalized_symbol"], item["capability"])].append(item)
    outputs = []
    for _, items in sorted(grouped.items()):
        items = sorted(items, key=lambda item: item["run"])
        statuses = [item["status"] for item in items]
        periods = [item["periods"] for item in items]
        failures = [item["typed_failure"] for item in items]
        first = items[0]
        outputs.append(
            {
                **{key: first[key] for key in (
                    "provider", "provider_version", "symbol_role", "requested_symbol", "native_symbol",
                    "normalized_symbol", "capability", "endpoint", "permission_requirement", "point_requirement",
                    "unit", "currency", "consolidation_scope", "company_type", "notes",
                )},
                "called_runs": sum(bool(item["called"]) for item in items),
                "availability_runs": sum(item["status"] in {"available", "partial"} for item in items),
                "status": first["status"] if len(set(statuses)) == 1 else "unstable",
                "periods": first["periods"],
                "periods_returned": round(mean(item["periods_returned"] for item in items), 1),
                "usable_non_sparse_periods": round(mean(item["usable_non_sparse_periods"] for item in items), 1),
                "field_count": round(mean(item["field_count"] for item in items), 1),
                "missing_field_ratio": round(mean(item["missing_field_ratio"] for item in items), 4),
                "announcement_date_available": any(item["announcement_date_available"] for item in items),
                "first_publication_date_available": any(item["first_publication_date_available"] for item in items),
                "restatement_identifier_available": any(item["restatement_identifier_available"] for item in items),
                "latency_ms_mean": round(mean(item["latency_ms"] for item in items), 1),
                "typed_failure": first["typed_failure"] if len(set(failures)) == 1 else "UNSTABLE_OUTCOME",
                "failure_detail": first["failure_detail"],
                "reproducible": statuses[0] == statuses[1] and periods[0] == periods[1],
                "call_ids": sorted({call_id for item in items for call_id in item["call_ids"]}),
            }
        )
    return outputs


def write_csv(path: Path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in row.items()})


def capability_matrix(summary):
    grouped = defaultdict(list)
    for item in summary:
        grouped[(item["provider"], item["capability"])].append(item)
    rows = []
    for (provider, capability), items in sorted(grouped.items()):
        rows.append({
            "provider": provider,
            "provider_version": items[0]["provider_version"],
            "capability": capability,
            "symbols_available_or_partial": sum(item["availability_runs"] == 2 for item in items),
            "symbols_tested": len(items),
            "reproducible_symbols": sum(item["reproducible"] for item in items),
            "mean_periods": round(mean(item["periods_returned"] for item in items), 1),
            "mean_usable_periods": round(mean(item["usable_non_sparse_periods"] for item in items), 1),
            "mean_missing_field_ratio": round(mean(item["missing_field_ratio"] for item in items), 4),
            "announcement_dates": sum(item["announcement_date_available"] for item in items),
            "first_publication_dates": sum(item["first_publication_date_available"] for item in items),
            "restatement_ids": sum(item["restatement_identifier_available"] for item in items),
            "mean_latency_ms": round(mean(item["latency_ms_mean"] for item in items), 1),
            "typed_failures": sorted({item["typed_failure"] for item in items if item["typed_failure"]}),
        })
    return rows


def main():
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    run_probes()
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(RAW.glob("*.json"))]
    records = [record for payload in payloads for record in payload["records"]]
    summary = summarize(records)
    matrix = capability_matrix(summary)
    policies = score_policies(summary)
    (OUTPUTS / "raw_records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUTS / "summary_records.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUTS / "policy_scores.json").write_text(json.dumps(policies, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUTPUTS / "provider_capability_matrix.csv", matrix)
    write_csv(OUTPUTS / "per_symbol_completeness_matrix.csv", summary)
    print(json.dumps({"records": len(records), "summary_records": len(summary), "policy_scores": policies}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
