import re
from hashlib import sha256
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot
from tradingagents.dataflows.market_snapshot import get_active_authoritative_market_snapshot
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    stable_acquisition_source_ref,
)


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    return build_verified_market_snapshot(symbol, curr_date, look_back_days)


def market_projection_envelope(
    content: str, *, symbol: str, curr_date: str, tool_name: str,
    tool_call_id: str, capability: str, segment_count: int = 1,
    metadata_prefix: str = "",
) -> dict[str, object]:
    """Bind a deterministic tool projection to the exact run-owned acquisition.

    The original acquisition remains in run evidence. This separate, call-bound
    projection attests only the rendered bytes, never historical qualification.
    Legacy non-mainland or unbound calls cannot acquire provenance from text.
    """
    snapshot = get_active_authoritative_market_snapshot(symbol, curr_date)
    # The OHLCV CSV renderer uses commented metadata; the two existing
    # projections use plain metadata. The trusted wrapper selects its format.
    if metadata_prefix not in {"", "# "}:
        raise ValueError("unsupported market projection metadata format")
    prefix = re.escape(metadata_prefix)
    ids = re.findall(rf"(?m)^{prefix}Snapshot ID: (\S+)$", content)
    digests = re.findall(rf"(?m)^{prefix}Frame SHA-256: ([0-9a-f]{{64}})$", content)
    upstream = next((outcome for outcome in snapshot.acquisition_outcomes
                     if isinstance(outcome, SourceAcquisitionAvailable)
                     and outcome.capability == "market_snapshot"
                     and outcome.artifact == snapshot.source_artifact), None) if snapshot else None
    bound = (
        snapshot is not None and upstream is not None and segment_count > 0
        and upstream.provider == snapshot.provider
        and upstream.artifact.artifact_sha256 == snapshot.frame_sha256
        and ids == [snapshot.snapshot_id] * segment_count
        and digests == [snapshot.frame_sha256] * segment_count
    )
    source_ref = snapshot.snapshot_id if bound else stable_acquisition_source_ref(
        "market-projection-unavailable", symbol, curr_date, tool_call_id,
    )
    acquisition_id = stable_acquisition_source_ref("tool-call", tool_name, tool_call_id)
    artifact = None
    if bound:
        artifact = SourceArtifact(
            artifact_sha256=sha256(content.encode("utf-8")).hexdigest(),
            source_ref=source_ref, tool_call_id=tool_call_id,
            tool_name=tool_name, raw_text=content,
        )
        outcome = SourceAcquisitionAvailable(
            provider=upstream.provider, provider_order=upstream.provider_order,
            capability=capability, source_ref=source_ref, attempt=1,
            acquisition_id=acquisition_id, retrieved_at=upstream.retrieved_at,
            artifact=artifact,
        )
    else:
        # This timestamp identifies the local failed binding, not a refreshed
        # acquisition/cache observation. No selected artifact is created.
        from datetime import datetime, timezone

        outcome = SourceAcquisitionUnavailable(
            provider="authoritative_market_snapshot", capability=capability,
            source_ref=source_ref, attempt=1, acquisition_id=acquisition_id,
            retrieved_at=datetime.now(timezone.utc).isoformat(), retryable=False,
            reason=AcquisitionUnavailableReason.INTEGRITY_FAILURE,
            error_code="snapshot_projection_binding_missing",
        )
    return ToolExecutionEvidenceEnvelope(
        tool_call_id=tool_call_id, tool_name=tool_name, source_ref=source_ref,
        capability=capability, acquisition_outcomes=(outcome,),
        selected_artifact=artifact,
    ).model_dump(mode="json")


@tool("get_verified_market_snapshot", response_format="content_and_artifact")
def get_verified_market_snapshot_with_evidence(
    symbol: str, curr_date: str,
    tool_call_id: Annotated[str, InjectedToolCallId()], look_back_days: int = 30,
) -> tuple[str, dict[str, object]]:
    """Return the deterministic verified snapshot with run-owned provenance."""
    content = build_verified_market_snapshot(symbol, curr_date, look_back_days)
    return content, market_projection_envelope(
        content, symbol=symbol, curr_date=curr_date,
        tool_name="get_verified_market_snapshot", tool_call_id=tool_call_id,
        capability="market_snapshot",
    )
