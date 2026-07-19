"""Immutable, deterministic audit records for terminal analysis outcomes."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from tradingagents.evidence import EvidenceState


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_decision_audit(final_state: dict[str, Any]) -> dict[str, Any]:
    """Build the audit payload without copying untrusted debate prose."""
    created_at = final_state.get("decision_audit_created_at")
    if not created_at:
        created_at = datetime.now(UTC).isoformat()
        final_state["decision_audit_created_at"] = created_at
    evidence = EvidenceState.model_validate(final_state.get("evidence_state", {}))
    risk_state = final_state.get("risk_debate_state") or {}
    terminal_output = (
        final_state.get("analysis_outcome")
        or final_state.get("final_trade_decision")
        or risk_state.get("judge_decision", "")
    )
    terminal_kind = (
        "analysis_outcome" if final_state.get("analysis_outcome") else "trading_decision"
    )
    graph_signature = final_state.get("graph_signature")
    if not isinstance(graph_signature, str):
        graph_signature = None
    portfolio_records = {
        "original_selection": final_state.get("pm_original_selection"),
        "selection_retry": final_state.get("pm_selection_retry"),
        "revision": final_state.get("pm_revision"),
        "revision_retry": final_state.get("pm_revision_retry"),
        "original_draft": final_state.get("original_draft_thesis"),
        "original_gate": final_state.get("original_decision_gate"),
        "revised_draft": final_state.get("revised_draft_thesis"),
        "revised_gate": final_state.get("revised_decision_gate"),
        "final_draft": final_state.get("draft_thesis"),
        "final_gate": final_state.get("decision_gate"),
    }
    portfolio_records["sha256"] = {
        key: sha256(_canonical_json(value)).hexdigest()
        for key, value in portfolio_records.items()
        if value is not None
    }
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "created_at": created_at,
        "run": {
            "ticker": final_state.get("company_of_interest"),
            "trade_date": final_state.get("trade_date"),
            "asset_type": final_state.get("asset_type"),
            "graph_signature": graph_signature,
            "evidence_gate_mode": final_state.get("evidence_gate_mode", "enforce"),
        },
        "terminal": {
            "kind": terminal_kind,
            "output_sha256": sha256(str(terminal_output).encode("utf-8")).hexdigest(),
        },
        "evidence_state": evidence.model_dump(mode="json"),
        "admission_gate": final_state.get("admission_gate"),
        "portfolio_manager": portfolio_records,
    }
    payload["audit_sha256"] = sha256(_canonical_json(payload)).hexdigest()
    return payload


def write_immutable_decision_audit(
    final_state: dict[str, Any],
    directory: str | Path,
    *,
    filename: str = "decision-audit.json",
) -> Path:
    """Publish a complete audit atomically and never overwrite different data."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = build_decision_audit(final_state)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    target = directory / filename
    if target.exists():
        if target.read_bytes() != encoded:
            raise FileExistsError(f"immutable decision audit already exists: {target}")
        return target

    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{filename}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != encoded:
                raise FileExistsError(
                    f"immutable decision audit already exists: {target}"
                ) from None
        except OSError as exc:
            # Some Windows filesystems disallow hard links. Exclusive creation
            # preserves immutability; the source bytes are already fully flushed.
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                target_descriptor = os.open(target, flags, 0o600)
            except FileExistsError:
                if target.read_bytes() != encoded:
                    raise FileExistsError(
                        f"immutable decision audit already exists: {target}"
                    ) from None
            else:
                with os.fdopen(target_descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)
    return target
