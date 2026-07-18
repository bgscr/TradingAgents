from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from cli.runtime_artifacts import RuntimeArtifactWriter

EVENTS = 200
PAYLOAD = {"symbol": "000725.SZ", "raw_payload": "x" * (512 * 1024)}


def run_legacy(root: Path) -> dict[str, float | int]:
    root.mkdir(parents=True)
    paths = [root / "message_tool.log", root / "latest_message_tool.log"]
    for path in paths:
        path.write_text("run metadata\n", encoding="utf-8")

    started_at = time.perf_counter()
    for index in range(EVENTS):
        args_str = ", ".join(f"{key}={value}" for key, value in PAYLOAD.items())
        line = f"12:00:{index % 60:02d} [Tool Call] get_stock_data({args_str})\n"
        for path in paths:
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
    elapsed = time.perf_counter() - started_at
    return {
        "producer_seconds": elapsed,
        "total_seconds": elapsed,
        "physical_bytes": sum(path.stat().st_size for path in paths),
    }


def run_buffered(root: Path) -> dict[str, float | int]:
    root.mkdir(parents=True)
    paths = [root / "message_tool.log", root / "latest_message_tool.log"]
    for path in paths:
        path.write_text("run metadata\n", encoding="utf-8")
    artifact_root = root / "runtime_artifacts"
    metrics_path = root / "runtime_metrics.json"
    writer = RuntimeArtifactWriter(
        artifact_root=artifact_root,
        log_paths=paths,
        metrics_path=metrics_path,
        max_queue_size=512,
    )

    total_started_at = time.perf_counter()
    producer_started_at = time.perf_counter()
    for index in range(EVENTS):
        writer.record_tool_call(
            f"12:00:{index % 60:02d}",
            "get_stock_data",
            PAYLOAD,
        )
    producer_seconds = time.perf_counter() - producer_started_at
    writer.close()
    total_seconds = time.perf_counter() - total_started_at

    artifacts = list(artifact_root.rglob("*.json.gz"))
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    return {
        "producer_seconds": producer_seconds,
        "total_seconds": total_seconds,
        "physical_bytes": sum(path.stat().st_size for path in paths + artifacts),
        "artifact_count": len(artifacts),
        "compression_operations": metrics["artifacts"]["compression_operations"],
        "artifacts_created": metrics["artifacts"]["created"],
        "artifacts_deduplicated": metrics["artifacts"]["deduplicated"],
        "max_queue_depth": metrics["queue"]["max_depth"],
        "dropped_events": metrics["saturation"]["dropped_events"],
        "coalesced_events": metrics["saturation"]["coalesced_events"],
        "flush_max_seconds": metrics["flush"]["max_seconds"],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tradingagents-runtime-benchmark-") as temp:
        root = Path(temp)
        legacy = run_legacy(root / "legacy")
        buffered = run_buffered(root / "buffered")
        print(
            json.dumps(
                {
                    "events": EVENTS,
                    "payload_bytes": len(
                        json.dumps(
                            PAYLOAD,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    ),
                    "legacy": legacy,
                    "buffered": buffered,
                    "producer_speedup": (
                        legacy["producer_seconds"] / buffered["producer_seconds"]
                    ),
                    "physical_byte_reduction": (
                        legacy["physical_bytes"] / buffered["physical_bytes"]
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
