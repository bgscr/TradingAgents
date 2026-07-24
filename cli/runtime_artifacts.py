from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradingagents.run_telemetry import (
    RunTelemetryProjection,
    acquisition_summary_from_projection,
)


@dataclass(frozen=True)
class _QueuedEvent:
    kind: str
    values: tuple[Any, ...] = ()
    barrier: threading.Event | None = None


@dataclass(frozen=True)
class RuntimeArtifactGCReport:
    scanned_artifacts: int
    referenced_artifacts: int
    removable_artifacts: int
    removed_artifacts: int
    removable_bytes: int
    candidates: tuple[Path, ...]
    dry_run: bool


_ARTIFACT_REFERENCE_RE = re.compile(r"artifact=sha256:([0-9a-f]{64})")


class ActiveRuntimeArtifactRunError(RuntimeError):
    """Raised when offline collection is attempted during an active run."""


def collect_runtime_artifacts(
    results_root: Path,
    *,
    dry_run: bool = True,
) -> RuntimeArtifactGCReport:
    """Find content-addressed runtime payloads not referenced by run logs."""
    root = Path(results_root)
    for status_path in root.rglob("run_status.json"):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") == "running":
            raise ActiveRuntimeArtifactRunError(
                f"runtime artifact collection refused while run "
                f"{status_path.parent.name} is active"
            )
    artifact_root = root / "runtime_artifacts" / "sha256"
    referenced: set[str] = set()
    for log_path in root.rglob("*message_tool.log"):
        log_text = log_path.read_text(encoding="utf-8")
        referenced.update(_ARTIFACT_REFERENCE_RE.findall(log_text))

    artifacts = tuple(sorted(artifact_root.rglob("*.json.gz"))) if artifact_root.exists() else ()
    referenced_paths = tuple(
        path for path in artifacts if path.name.removesuffix(".json.gz") in referenced
    )
    candidates = tuple(path for path in artifacts if path not in referenced_paths)
    removable_bytes = sum(path.stat().st_size for path in candidates)
    removed_artifacts = 0
    if not dry_run:
        for path in candidates:
            path.unlink()
            removed_artifacts += 1
    return RuntimeArtifactGCReport(
        scanned_artifacts=len(artifacts),
        referenced_artifacts=len(referenced_paths),
        removable_artifacts=len(candidates),
        removed_artifacts=removed_artifacts,
        removable_bytes=removable_bytes,
        candidates=candidates,
        dry_run=dry_run,
    )


class RuntimeArtifactWriter:
    """Persist complete runtime payloads and bounded primary-log references."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        log_paths: Iterable[Path],
        preview_chars: int = 512,
        max_queue_size: int = 256,
        batch_size: int = 32,
        flush_interval_seconds: float = 0.25,
        metrics_path: Path | None = None,
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.log_paths = tuple(Path(path) for path in log_paths)
        self.preview_chars = preview_chars
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.metrics_path = Path(metrics_path) if metrics_path is not None else None
        self._compressed_sizes: dict[str, int] = {}
        self._queue: queue.Queue[_QueuedEvent] = queue.Queue(maxsize=max_queue_size)
        self._queue_capacity = max_queue_size
        self._metrics_lock = threading.Lock()
        self._max_queue_depth = 0
        self._dropped_by_kind: Counter[str] = Counter()
        self._coalesced_by_kind: Counter[str] = Counter()
        self._pending_coalesced: dict[str, _QueuedEvent] = {}
        self._saturation_reported = (0, 0)
        self._durations: dict[str, dict[str, dict[str, float | int]]] = defaultdict(
            dict
        )
        self._stage_activity: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {
                "model_calls": 0,
                "model_seconds": 0.0,
                "tool_calls": 0,
                "tool_seconds": 0.0,
            }
        )
        self._terminal_summary: dict[str, Any] = {}
        self._payload_uncompressed_bytes = 0
        self._payload_compressed_bytes = 0
        self._primary_log_bytes = 0
        self._artifact_compression_operations = 0
        self._artifacts_created = 0
        self._artifacts_deduplicated = 0
        self._flush_count = 0
        self._flush_total_seconds = 0.0
        self._flush_max_seconds = 0.0
        self._current_phase: str | None = None
        self._current_phase_started_at: float | None = None
        self._worker_error: BaseException | None = None
        self._closed = False
        self._worker = threading.Thread(
            target=self._run_worker,
            name="runtime-artifact-writer",
            daemon=True,
        )
        self._worker.start()

    def record_tool_call(
        self,
        timestamp: str,
        tool_name: str,
        payload: Any,
    ) -> None:
        self._enqueue(
            _QueuedEvent(
                "tool",
                (timestamp, tool_name, payload),
            )
        )

    def record_message(self, timestamp: str, message_type: str, content: str) -> None:
        if message_type == "Advisory Commentary":
            self._enqueue(
                _QueuedEvent(
                    "advisory",
                    (timestamp, message_type, content),
                )
            )
            return
        line = f"{timestamp} [{message_type}] {content.replace(chr(10), ' ')}\n"
        self._enqueue(_QueuedEvent("log", (line,)))

    def record_tool_result(self, timestamp: str, payload: Any) -> None:
        self._enqueue(_QueuedEvent("tool_result", (timestamp, payload)))

    def record_critical(
        self,
        timestamp: str,
        message_type: str,
        content: str,
    ) -> None:
        self.flush()
        line = f"{timestamp} [{message_type}] {content.replace(chr(10), ' ')}\n"
        self._append_log_line(line)

    def flush(self, timeout: float = 30.0) -> None:
        if self._closed:
            return
        started_at = time.monotonic()
        self._enqueue_saturation_detail(timeout)
        barrier = threading.Event()
        self._queue.put(_QueuedEvent("flush", barrier=barrier), timeout=timeout)
        self._observe_queue_depth()
        deadline = time.monotonic() + timeout
        while not barrier.is_set():
            self._raise_worker_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._raise_worker_error()
                raise TimeoutError("runtime artifact flush timed out")
            barrier.wait(min(0.05, remaining))
        self._raise_worker_error()
        elapsed = time.monotonic() - started_at
        with self._metrics_lock:
            self._flush_count += 1
            self._flush_total_seconds += elapsed
            self._flush_max_seconds = max(self._flush_max_seconds, elapsed)

    def record_duration(self, category: str, name: str, seconds: float) -> None:
        with self._metrics_lock:
            item = self._durations[category].setdefault(
                name,
                {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0},
            )
            item["count"] += 1
            item["total_seconds"] += seconds
            item["max_seconds"] = max(float(item["max_seconds"]), seconds)
            if category in {"model", "tool"}:
                stage = self._current_phase or "unassigned"
                stage_item = self._stage_activity[stage]
                stage_item[f"{category}_calls"] += 1
                stage_item[f"{category}_seconds"] += seconds

    def record_terminal_summary(
        self,
        *,
        terminal_route: str,
        stats: Mapping[str, Any] | None = None,
        acquisition_outcomes: Iterable[Any] = (),
        run_telemetry: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist aggregate cost/call/retry telemetry without provider payloads."""

        usage = dict(stats or {})
        attempts = 0
        available = 0
        unavailable = 0
        retryable = 0
        reasons: Counter[str] = Counter()
        maximum_attempts: dict[tuple[str, str, str], int] = {}
        circuit_breaker_events = 0
        for raw_outcome in acquisition_outcomes:
            if hasattr(raw_outcome, "model_dump"):
                outcome = raw_outcome.model_dump(mode="json")
            elif isinstance(raw_outcome, Mapping):
                outcome = dict(raw_outcome)
            else:
                continue
            attempts += 1
            outcome_kind = str(outcome.get("outcome", "unknown"))
            if outcome_kind == "available":
                available += 1
            elif outcome_kind == "unavailable":
                unavailable += 1
            if outcome.get("retryable") is True:
                retryable += 1
            reason = outcome.get("reason")
            if reason:
                reasons[str(reason)] += 1
                if str(reason) == "circuit_open":
                    circuit_breaker_events += 1
            key = (
                str(outcome.get("provider", "unknown")),
                str(outcome.get("capability", "unknown")),
                str(outcome.get("source_ref", "unknown")),
            )
            try:
                attempt = max(1, int(outcome.get("attempt", 1)))
            except (TypeError, ValueError):
                attempt = 1
            maximum_attempts[key] = max(maximum_attempts.get(key, 0), attempt)
        cost_usd = usage.get("cost_usd")
        summary = {
            "terminal_route": terminal_route,
            "model": {
                "calls": int(usage.get("llm_calls", 0) or 0),
                "tokens_in": int(usage.get("tokens_in", 0) or 0),
                "tokens_out": int(usage.get("tokens_out", 0) or 0),
            },
            "tool": {"calls": int(usage.get("tool_calls", 0) or 0)},
            "cost": {
                "available": cost_usd is not None,
                "amount_usd": float(cost_usd) if cost_usd is not None else None,
            },
            "acquisition": {
                "attempts": attempts,
                "available": available,
                "unavailable": unavailable,
                "retryable_unavailable": retryable,
                "retry_events": sum(
                    max(0, attempt - 1) for attempt in maximum_attempts.values()
                ),
                "circuit_breaker_events": circuit_breaker_events,
                "unavailable_reasons": dict(sorted(reasons.items())),
            },
        }
        if run_telemetry is not None:
            projection = RunTelemetryProjection.model_validate(run_telemetry)
            summary["acquisition"] = acquisition_summary_from_projection(projection)
            summary["stages"] = {
                name: stage.model_dump(mode="json")
                for name, stage in projection.stages.items()
            }
        with self._metrics_lock:
            self._terminal_summary = summary

    def transition_phase(self, phase: str, *, at: float | None = None) -> None:
        boundary = time.monotonic() if at is None else at
        if self._current_phase is not None:
            self.flush()
            if phase == self._current_phase:
                return
            assert self._current_phase_started_at is not None
            self.record_duration(
                "graph_phase",
                self._current_phase,
                max(0.0, boundary - self._current_phase_started_at),
            )
        self._current_phase = phase
        self._current_phase_started_at = boundary

    def finish_phases(self, *, at: float | None = None) -> None:
        if self._current_phase is None:
            self.flush()
            return
        boundary = time.monotonic() if at is None else at
        self.flush()
        assert self._current_phase_started_at is not None
        self.record_duration(
            "graph_phase",
            self._current_phase,
            max(0.0, boundary - self._current_phase_started_at),
        )
        self._current_phase = None
        self._current_phase_started_at = None

    def get_metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            durations = {
                category: {
                    name: dict(values) for name, values in names.items()
                }
                for category, names in self._durations.items()
            }
            stage_activity = {
                stage: dict(values)
                for stage, values in self._stage_activity.items()
            }
            terminal_stages = self._terminal_summary.get("stages")
            if isinstance(terminal_stages, Mapping):
                stage_activity = {
                    str(stage): dict(values)
                    for stage, values in terminal_stages.items()
                    if isinstance(values, Mapping)
                }
            saturation = {
                "dropped_events": sum(self._dropped_by_kind.values()),
                "coalesced_events": sum(self._coalesced_by_kind.values()),
                "dropped_by_kind": dict(self._dropped_by_kind),
                "coalesced_by_kind": dict(self._coalesced_by_kind),
            }
            metrics = {
                "durations": durations,
                "stage_activity": stage_activity,
                "terminal": dict(self._terminal_summary),
                "queue": {
                    "capacity": self._queue_capacity,
                    "max_depth": self._max_queue_depth,
                    "current_depth": self._queue.qsize(),
                },
                "bytes": {
                    "payload_uncompressed": self._payload_uncompressed_bytes,
                    "payload_compressed": self._payload_compressed_bytes,
                    "primary_logs": self._primary_log_bytes,
                },
                "artifacts": {
                    "compression_operations": self._artifact_compression_operations,
                    "created": self._artifacts_created,
                    "deduplicated": self._artifacts_deduplicated,
                },
                "flush": {
                    "count": self._flush_count,
                    "total_seconds": self._flush_total_seconds,
                    "max_seconds": self._flush_max_seconds,
                },
                "saturation": saturation,
                "queue_capacity": self._queue_capacity,
                "max_queue_depth": self._max_queue_depth,
                **saturation,
            }
            return metrics

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._queue.put(_QueuedEvent("stop"))
        self._worker.join(timeout=30.0)
        if self._worker.is_alive():
            raise TimeoutError("runtime artifact writer did not stop")
        self._closed = True
        self._raise_worker_error()
        if self.metrics_path is not None:
            self._write_metrics()

    def _enqueue(self, event: _QueuedEvent) -> None:
        if self._closed:
            raise RuntimeError("runtime artifact writer is closed")
        self._raise_worker_error()
        try:
            self._queue.put_nowait(event)
            self._observe_queue_depth()
        except queue.Full:
            if event.kind == "advisory":
                self._queue.put(event, timeout=30.0)
                self._observe_queue_depth()
                return
            with self._metrics_lock:
                if event.kind == "log":
                    self._pending_coalesced[event.kind] = event
                    self._coalesced_by_kind[event.kind] += 1
                else:
                    self._dropped_by_kind[event.kind] += 1

    def _observe_queue_depth(self) -> None:
        depth = self._queue.qsize()
        with self._metrics_lock:
            self._max_queue_depth = max(self._max_queue_depth, depth)

    def _enqueue_saturation_detail(self, timeout: float) -> None:
        with self._metrics_lock:
            pending = list(self._pending_coalesced.values())
            self._pending_coalesced.clear()
            dropped = sum(self._dropped_by_kind.values())
            coalesced = sum(self._coalesced_by_kind.values())
            last_dropped, last_coalesced = self._saturation_reported
            should_report = (dropped, coalesced) != (last_dropped, last_coalesced)
            if should_report:
                self._saturation_reported = (dropped, coalesced)
                detail = (
                    "[RuntimeArtifacts] queue_saturation "
                    f"dropped={dropped} coalesced={coalesced} "
                    f"dropped_by_kind={dict(self._dropped_by_kind)} "
                    f"coalesced_by_kind={dict(self._coalesced_by_kind)}\n"
                )
            else:
                detail = ""

        for event in pending:
            self._queue.put(event, timeout=timeout)
            self._observe_queue_depth()
        if detail:
            self._queue.put(_QueuedEvent("log", (detail,)), timeout=timeout)
            self._observe_queue_depth()

    def _run_worker(self) -> None:
        try:
            while True:
                first = self._queue.get()
                batch = [first]
                deadline = time.monotonic() + self.flush_interval_seconds
                while len(batch) < self.batch_size and first.kind not in {
                    "flush",
                    "stop",
                }:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        event = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    batch.append(event)
                    if event.kind in {"flush", "stop"}:
                        break

                should_stop = self._process_batch(batch)
                for _ in batch:
                    self._queue.task_done()
                if should_stop:
                    return
        except BaseException as exc:
            self._worker_error = exc

    def _process_batch(self, batch: list[_QueuedEvent]) -> bool:
        log_lines: list[str] = []
        barriers: list[threading.Event] = []
        should_stop = False
        for event in batch:
            if event.kind == "log":
                log_lines.append(event.values[0])
            elif event.kind == "advisory":
                timestamp, message_type, content = event.values
                encoded, digest = self._encode_payload(content)
                compressed_size = self._persist_payload(encoded, digest)
                preview = content[: self.preview_chars].replace("\n", " ")
                if len(content) > self.preview_chars:
                    preview += "…"
                log_lines.append(
                    f"{timestamp} [{message_type}] {preview} "
                    f"artifact=sha256:{digest} bytes={len(encoded)} "
                    f"compressed_bytes={compressed_size}\n"
                )
            elif event.kind == "tool":
                timestamp, tool_name, payload = event.values
                encoded, digest = self._encode_payload(payload)
                compressed_size = self._persist_payload(encoded, digest)
                decoded = encoded.decode("utf-8")
                preview = decoded[: self.preview_chars].replace("\n", " ")
                if len(decoded) > self.preview_chars:
                    preview += "…"
                log_lines.append(
                    f"{timestamp} [Tool Call] {tool_name} "
                    f"preview={preview} artifact=sha256:{digest} bytes={len(encoded)} "
                    f"compressed_bytes={compressed_size}\n"
                )
            elif event.kind == "tool_result":
                timestamp, payload = event.values
                encoded, digest = self._encode_payload(payload)
                compressed_size = self._persist_payload(encoded, digest)
                decoded = encoded.decode("utf-8")
                preview = decoded[: self.preview_chars].replace("\n", " ")
                if len(decoded) > self.preview_chars:
                    preview += "…"
                log_lines.append(
                    f"{timestamp} [Tool Result] preview={preview} "
                    f"artifact=sha256:{digest} bytes={len(encoded)} "
                    f"compressed_bytes={compressed_size}\n"
                )
            elif event.kind == "flush" and event.barrier is not None:
                barriers.append(event.barrier)
            elif event.kind == "stop":
                should_stop = True

        if log_lines:
            self._append_log_line("".join(log_lines))
        for barrier in barriers:
            barrier.set()
        return should_stop

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise RuntimeError("runtime artifact background writer failed") from self._worker_error

    @staticmethod
    def _encode_payload(payload: Any) -> tuple[bytes, str]:
        encoded = json.dumps(
            payload,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return encoded, hashlib.sha256(encoded).hexdigest()

    def _persist_payload(self, encoded: bytes, digest: str) -> int:
        known_size = self._compressed_sizes.get(digest)
        if known_size is not None:
            with self._metrics_lock:
                self._artifacts_deduplicated += 1
            return known_size

        target = self.artifact_root / "sha256" / digest[:2] / f"{digest}.json.gz"
        if target.exists():
            compressed_size = target.stat().st_size
            self._compressed_sizes[digest] = compressed_size
            with self._metrics_lock:
                self._artifacts_deduplicated += 1
            return compressed_size

        compressed = gzip.compress(encoded, mtime=0)
        with self._metrics_lock:
            self._artifact_compression_operations += 1
        created = self._write_artifact_once(digest, compressed)
        compressed_size = len(compressed)
        self._compressed_sizes[digest] = compressed_size
        with self._metrics_lock:
            if created:
                self._artifacts_created += 1
                self._payload_uncompressed_bytes += len(encoded)
                self._payload_compressed_bytes += compressed_size
            else:
                self._artifacts_deduplicated += 1
        return compressed_size

    def _write_artifact_once(self, digest: str, compressed: bytes) -> bool:
        target_dir = self.artifact_root / "sha256" / digest[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest}.json.gz"
        if target.exists():
            return False

        temp_path: Path | None = None
        created = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=target_dir,
                prefix=f".{digest}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(compressed)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            try:
                os.link(temp_path, target)
                created = True
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return created

    def _append_log_line(self, line: str) -> None:
        encoded_bytes = len(line.encode("utf-8"))
        for path in self.log_paths:
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
        with self._metrics_lock:
            self._primary_log_bytes += encoded_bytes * len(self.log_paths)

    def _write_metrics(self) -> None:
        assert self.metrics_path is not None
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.get_metrics(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.metrics_path.parent,
                prefix=f".{self.metrics_path.name}.",
                suffix=".tmp",
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as temp_file:
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self.metrics_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
