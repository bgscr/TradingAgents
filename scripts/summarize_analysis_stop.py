"""Read an existing bounded run and print a closed, payload-free stop summary.

This stdlib-only command never imports the application, opens databases, reads
credentials, writes evidence, or retries a run. It does not qualify a decision.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import re
import stat
import zlib
from datetime import date, datetime
from pathlib import Path

PROJECT = Path(__file__).absolute().parents[1]
MAX_BYTES = 2_000_000
MAX_DIAGNOSTICS = 256
ANALYSTS = {"market", "social", "news", "fundamentals"}
NODES = {
    "Market Analyst": "market",
    "Sentiment Analyst": "social",
    "News Analyst": "news",
    "Fundamentals Analyst": "fundamentals",
}
GRAPH_STAGES = {
    **dict.fromkeys(NODES, "analysis"),
    **dict.fromkeys(("Bull Researcher", "Bear Researcher", "Research Manager"), "research_debate"),
    "Trader": "trading",
    **dict.fromkeys(("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"), "risk_debate"),
    "Portfolio Manager": "portfolio_synthesis",
}
ERROR_TYPES = {
    "RemoteProtocolError", "LocalProtocolError", "ConnectError", "ReadError",
    "WriteError", "CloseError", "ProxyError", "TransportError", "UnsupportedProtocol",
    "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "APIConnectionError", "APITimeoutError",
}
ERROR_FAMILIES = {"remote_protocol", "timeout", "connection", "transport", "client"}
DIAGNOSTIC_REF = re.compile(r"\[Diagnostic\] artifact=sha256:([0-9a-f]{64})(?:\s|$)")
DETAIL_REF = re.compile(r"artifact=sha256:([0-9a-f]{64})")


class SummaryUnavailable(ValueError):
    """A fixed reason code; never arbitrary input or exception text."""


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_bytes(project, path):
    """Bound reads to ordinary files beneath this project; reject replacements."""
    if ".." in path.parts or not path.is_relative_to(project):
        raise SummaryUnavailable("invalid_evidence_path")
    before = path.lstat()
    for item in (path, *path.parents):
        info = before if item == path else item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SummaryUnavailable("linked_evidence")
        if item == project:
            break
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SummaryUnavailable("non_regular_evidence")
    if before.st_size > MAX_BYTES:
        raise SummaryUnavailable("oversize_evidence")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise SummaryUnavailable("evidence_changed")
        data = stream.read(MAX_BYTES + 1)
        if (_identity(os.fstat(stream.fileno())) != _identity(before)
                or _identity(path.lstat()) != _identity(before)):
            raise SummaryUnavailable("evidence_changed")
    if len(data) > MAX_BYTES:
        raise SummaryUnavailable("oversize_evidence")
    return data


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SummaryUnavailable("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise SummaryUnavailable("nonfinite_json_number")


def _object(data):
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                       parse_constant=_reject_constant)
    if type(value) is not dict:
        raise SummaryUnavailable("invalid_evidence_shape")
    return value


def _mapping(value):
    return value if type(value) is dict else {}


def _known(value, allowed):
    return value if type(value) is str and value in allowed else None


def _number(value, maximum):
    return (value if type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= maximum else None)


def _artifact(project, run, digest):
    artifact = run.parents[3] / "runtime_artifacts" / "sha256" / digest[:2] / (digest + ".json.gz")
    compressed = _read_bytes(project, artifact)
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SummaryUnavailable("oversize_diagnostic")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SummaryUnavailable("diagnostic_hash_mismatch")
    return _object(raw)


_REPORT_FIELDS = frozenset({
    "contract_version", "report_markdown", "material_claims", "evidence_notes",
    "material_claims[].claim_id", "material_claims[].statement",
    "material_claims[].source_ref", "material_claims[].source_quote",
    "evidence_notes[].note_id", "evidence_notes[].kind", "evidence_notes[].statement",
    "evidence_notes[].source_ref", "evidence_notes[].source_quote",
})
_VALIDATION_KINDS = frozenset({"json_syntax", "schema_validation", "validation_unspecified",
    "blank_report", "duplicate_claim_id", "claim_namespace", "duplicate_note_id", "claim_note_collision", "note_namespace"})


def _diagnostic_details(project, run, analyst):
    log = _read_bytes(project, run / "message_tool.log").decode("utf-8")
    references = DIAGNOSTIC_REF.findall(log)
    if len(references) > MAX_DIAGNOSTICS:
        raise SummaryUnavailable("too_many_diagnostics")
    latest = None
    for digest in references:
        record = _artifact(project, run, digest)
        payload = _mapping(record.get("payload"))
        recorded_analyst = _known(payload.get("analyst"), ANALYSTS | {"sentiment"})
        if recorded_analyst == "sentiment":
            recorded_analyst = "social"
        if (record.get("version") == 1 and record.get("kind") == "analyst_output"
                and record.get("qualification_authority") is False
                and recorded_analyst == analyst):
            latest = payload
    if latest is None:
        return {}
    stage = _known(latest.get("stage"), {"initial_response", "finalizer"})
    if latest.get("outcome") == "transport_error":
        return {"failure_stage": stage}
    if latest.get("outcome") != "validation_error" or stage != "finalizer":
        return {}
    details = {"failure_stage": stage, "_validation_failure": True}
    kind = _known(latest.get("validation_kind"), _VALIDATION_KINDS)
    if analyst != "market" and kind not in {"schema_validation", "json_syntax", "validation_unspecified"}:
        kind = None
    if kind:
        details["validation_kind"] = kind
        fields = latest.get("validation_fields")
        if kind == "schema_validation" and type(fields) is list:
            details["validation_fields"] = sorted({f for f in fields[:8] if type(f) is str and f in _REPORT_FIELDS})
    return details


def _last_failure(project, run):
    metrics = _object(_read_bytes(project, run / "runtime_metrics.json"))
    transport = _mapping(_mapping(metrics.get("terminal")).get("model_transport"))
    attempts = transport.get("attempts")
    if (transport.get("contract_version") in ("1.0", "1.1")
            and type(attempts) is list and 0 < len(attempts) <= MAX_DIAGNOSTICS):
        last = _mapping(attempts[-1])
        if (last.get("contract_version") in ("1.0", "1.1")
                and last.get("disposition") == "terminal_transport_failure"):
            return last
    return {}


def _transport_fields(last):
    return {
        "transport_error_type": _known(last.get("exception_class"), ERROR_TYPES),
        "transport_error_family": _known(last.get("exception_family"), ERROR_FAMILIES),
        "elapsed_seconds": _number(last.get("elapsed_seconds"), 86400),
        "http_status": (last.get("http_status") if type(last.get("http_status")) is int
                        and 100 <= last["http_status"] <= 599 else None),
    }


def _summary(reason, analyst=None):
    return {
        "contract": "analysis-stop-summary-v2", "status": "stopped",
        "reason": reason, "analyst": analyst,
        "failure_stage": None, "failure_node": None, "transport_error_type": None,
        "transport_error_family": None, "elapsed_seconds": None, "http_status": None,
        "subsequent_analysis_sends_allowed": False, "qualification_authority": False,
        "limitations": [],
    }


def _operational_stop(project, run, status):
    """Corroborate a final graph error with its latest transport and typed cause."""
    public = _mapping(status.get("error_diagnostics"))
    if (status.get("terminal_outcome_kind") != "operational_failure"
            or status.get("failed_phase") != "graph_stream"
            or status.get("operational_error_category") != "graph_execution"
            or public.get("contract_version") != "operational-failure-public-v1"
            or public.get("outcome") != "operational_failure"
            or public.get("category") != "graph_execution" or public.get("phase") != "graph_stream"):
        raise SummaryUnavailable("unsupported_stop_record")
    last = _last_failure(project, run)
    limits = _mapping(last.get("send_limits"))
    node = _known(limits.get("graph_node"), GRAPH_STAGES)
    fields = _transport_fields(last)
    if (last.get("contract_version") != "1.1"
            or node is None or limits.get("contract_version") != "1.0"
            or limits.get("stage") != GRAPH_STAGES[node]
            or fields["transport_error_type"] is None or fields["transport_error_family"] is None):
        raise SummaryUnavailable("unsupported_transport_record")
    reference = public.get("detail_artifact")
    match = DETAIL_REF.fullmatch(reference) if type(reference) is str else None
    if match is None:
        raise SummaryUnavailable("invalid_operational_diagnostic")
    detail = _artifact(project, run, match[1])
    diagnostic = _mapping(detail.get("exception_diagnostics"))
    chain = diagnostic.get("chain")
    if (detail.get("contract_version") != "operational-failure-detail-v1"
            or detail.get("failed_phase") != "graph_stream"
            or detail.get("operational_error_category") != "graph_execution"
            or diagnostic.get("contract_version") != "1.0"
            or diagnostic.get("chain_truncated") is not False
            or diagnostic.get("cycle_detected") is not False
            or type(chain) is not list or not 1 <= len(chain) <= 16):
        raise SummaryUnavailable("unsupported_operational_diagnostic")
    types = [_known(_mapping(item).get("exception_type"), ERROR_TYPES) for item in chain]
    if (None in types or types[0] not in {"APIConnectionError", "APITimeoutError"}
            or fields["transport_error_type"] not in types):
        raise SummaryUnavailable("operational_transport_mismatch")
    summary = _summary("model_transport_failure", NODES.get(node))
    summary.update(fields, failure_node=node, failure_stage=GRAPH_STAGES[node])
    summary["limitations"].append("underlying_transport_cause_unresolved")
    return summary


def summarize_stop(run_dir, *, project_root=PROJECT):
    """Project only recognized fields from the exact requested stopped run."""
    project = Path(project_root).absolute()
    run = Path(run_dir).absolute()
    base = project / "reports" / "bounded-runs"
    if not run.is_relative_to(base) or ".." in run.parts:
        raise SummaryUnavailable("invalid_run_directory")
    parts = run.relative_to(base).parts
    if (len(parts) != 6 or not re.fullmatch(r"[0-9a-f]{32}", parts[0])
            or parts[1] != "analysis" or not re.fullmatch(r"\d{6}\.(SS|SZ)", parts[2])
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[3])
            or parts[4] != "runs" or not re.fullmatch(r"\d{8}_\d{6}", parts[5])):
        raise SummaryUnavailable("invalid_run_directory")
    date.fromisoformat(parts[3])
    datetime.strptime(parts[5], "%Y%m%d_%H%M%S")
    status = _object(_read_bytes(project, run / "run_status.json"))
    if (status.get("run_id") != parts[5] or status.get("artifact_run_id") != parts[5]
            or status.get("ticker") != parts[2] or status.get("analysis_date") != parts[3]):
        raise SummaryUnavailable("run_identity_mismatch")
    if status.get("lifecycle_status") != "failed" or status.get("status") != "failed":
        raise SummaryUnavailable("unsupported_stop_record")
    try:
        stop_bytes = _read_bytes(project, run.parents[4] / "critical-stop.json")
    except FileNotFoundError:
        return _operational_stop(project, run, status)
    stop = _object(stop_bytes)
    if (stop.get("reason") != "critical_evidence_failure"
            or stop.get("stage") != "analyst_terminal_route"
            or stop.get("subsequent_analysis_sends_allowed") is not False):
        raise SummaryUnavailable("unsupported_stop_record")
    analyst = _known(stop.get("analyst"), ANALYSTS)
    if analyst is None:
        raise SummaryUnavailable("unsupported_analyst")
    summary = _summary("critical_evidence_failure", analyst)
    summary["failure_node"] = next(node for node in NODES if NODES[node] == analyst)
    try:
        last = _last_failure(project, run)
        limits = _mapping(last.get("send_limits"))
        if NODES.get(_known(limits.get("graph_node"), NODES)) == analyst:
            summary.update(_transport_fields(last))
    except (OSError, ValueError, RecursionError, OverflowError):
        summary["limitations"].append("transport_record_unreadable")
    structured_failure = False
    try:
        details = _diagnostic_details(project, run, analyst)
        structured_failure = details.pop("_validation_failure", False)
        summary.update(details)
    except (OSError, ValueError, EOFError, RecursionError, zlib.error):
        summary["limitations"].append("diagnostic_record_unreadable")
    if summary["failure_stage"] is None:
        summary["limitations"].append("failure_stage_unavailable")
    if not structured_failure or summary["transport_error_type"] is not None:
        if summary["transport_error_type"] is None:
            summary["limitations"].append("transport_error_type_unavailable")
        summary["limitations"].append("underlying_transport_cause_unresolved")
    if structured_failure and "validation_kind" not in summary:
        summary["limitations"].append("validation_kind_unavailable")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="Exact existing bounded analysis run directory")
    args = parser.parse_args(argv)
    try:
        summary = summarize_stop(args.run_dir)
    except (OSError, ValueError, EOFError, RecursionError, OverflowError, zlib.error) as error:
        reason = str(error) if type(error) is SummaryUnavailable else "unreadable_or_invalid_evidence"
        print(json.dumps({"status": "diagnostic_unavailable", "reason": reason}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
