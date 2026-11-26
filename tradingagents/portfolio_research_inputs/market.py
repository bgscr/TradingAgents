"""Revalidate a registered mainland-equity market return against its normalized frame."""
import csv
from datetime import date, datetime
from decimal import DecimalException
from hashlib import sha256
from io import StringIO
import json
import re

from tradingagents.evidence import EvidenceState, SourceAcquisitionAvailable, SourceFact
from tradingagents.strategy_registry import build_market_return_fact
from .financial import ledger_sha256


def market_fact(evidence: dict, *, owner_sha256: str, symbol: str, as_of: date,
                observed_by: datetime) -> SourceFact | None:
    """Use same-run sealed state and selected-day timestamps; never fetch or synthesize rows."""
    if (not re.fullmatch(r"[0-9]{6}\.(SS|SZ)", symbol)
            or observed_by.utcoffset() is None):
        return None
    try:
        if ledger_sha256(evidence) != owner_sha256:
            return None
        state = EvidenceState.model_validate(evidence)
        identity, snap = state.instrument_identity, state.market_snapshot
        if (not identity or not identity.is_authoritative or identity.symbol != symbol
                or identity.currency != "CNY" or identity.instrument_kind.value != "equity"
                or identity.venue != ("XSHG" if symbol.endswith(".SS") else "XSHE")
                or not snap or snap.symbol != symbol or snap.snapshot_id_version != "v2"
                or not snap.snapshot_manifest_json or snap.requested_date != as_of.isoformat()
                or snap.effective_trading_date != as_of.isoformat()
                or datetime.fromisoformat(snap.retrieved_at) > observed_by):
            return None
        manifest = json.loads(snap.snapshot_manifest_json)
        instrument = manifest["instrument"]
        if (manifest["frame"] != {"rows":snap.history_rows, "sha256":snap.frame_sha256}
                or manifest["provider"]["provider_name"] != snap.provider
                or manifest["adjustment_basis"] != snap.adjustment_basis
                or manifest["requested_date"] != snap.requested_date
                or manifest["effective_trading_date"] != snap.effective_trading_date
                or instrument["canonical_symbol"] != symbol or instrument["currency"] != "CNY"
                or instrument["instrument_kind"] != "equity" or instrument["reference_market"] not in
                {identity.venue, "shanghai" if symbol.endswith(".SS") else "shenzhen"}):
            return None
        artifacts = [a for a in state.source_artifacts if a.artifact_sha256 == snap.frame_sha256
                     and a.tool_name in {"authoritative_market_snapshot_normalized_frame_v1",
                                         "authoritative_market_snapshot_history_bundle_v1"}
                     and any(isinstance(o, SourceAcquisitionAvailable) and o.artifact == a
                             and o.provider == snap.provider and o.capability == "market_snapshot"
                             for o in state.acquisition_outcomes)]
        if len(artifacts) != 1:
            return None
        artifact = artifacts[0]
        if sha256(artifact.raw_text.encode()).hexdigest() != snap.frame_sha256 or not any(
            isinstance(o, SourceAcquisitionAvailable) and o.provider == snap.provider
            and o.capability == "market_snapshot" and o.artifact == artifact and o.source_ref == artifact.source_ref
            and datetime.fromisoformat(o.retrieved_at) <= observed_by for o in state.acquisition_outcomes
        ):
            return None
        reader = csv.DictReader(StringIO(artifact.raw_text), strict=True)
        if len(set(reader.fieldnames or ())) != len(reader.fieldnames or ()):
            return None
        dates = [date.fromisoformat(row["Date"]) for row in reader]
        if len(dates) != snap.history_rows or not dates or dates[-1] != as_of or dates != sorted(set(dates)):
            return None
        fact = build_market_return_fact(snap, artifact)
        prior = [item for item in state.source_facts if item.fact_id == fact.fact_id]
        if len(prior) != 1 or prior[0].model_dump(mode="json", exclude={"availability_lineage"}) != fact.model_dump(mode="json", exclude={"availability_lineage"}):
            return None
        return fact
    except (ValueError, KeyError, TypeError, AttributeError, DecimalException, csv.Error):
        return None

