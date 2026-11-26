"""Project already authenticated CURRENT dispatch attachments into source-row facts."""
import csv
from datetime import date, datetime
from hashlib import sha256
import json

from pydantic import ValidationError
from tradingagents.dataflows.financial_dispatch import FinancialDispatchCheckpointLedger
from tradingagents.financial_observations import (
    CURRENT_STATEMENT_COLUMNS, FinancialObservation, bind_current_statement_observation,
)

FIELDS = {
    "get_balance_sheet": ("total_assets", "total_assets"),
    "get_income_statement": ("net_income", "n_income_attr_p"),
    "get_cashflow": ("net_change_in_cash", "n_incr_cash_cash_equ"),
}


def ledger_sha256(ledger):
    data = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(data.encode("utf-8")).hexdigest()


def _latest(entry, symbol, as_of, observed_by):
    key, terminal = entry.canonical_request_key, entry.terminal
    artifact = terminal.artifact
    if (artifact is None or terminal.provider != "tushare"
            or terminal.plan_outcomes[-1].provider != terminal.provider
            or terminal.terminal_outcome.outcome != "available"
            or terminal.value != artifact.raw_text or artifact.tool_name != key.tool_name
            or artifact.source_ref != key.request_key or entry.artifact_sha256 != artifact.artifact_sha256):
        return None
    lines = artifact.raw_text.splitlines()
    header = ",".join(CURRENT_STATEMENT_COLUMNS)
    if lines.count(header) != 1:
        return None
    # Retained values remain descriptive when a CURRENT announcement window is stale or missing.
    if any(line.startswith(("# stale_announcement_ranges=", "# missing_announcement_ranges=")) for line in lines[:lines.index(header)]):
        return None
    eligible = []
    for quote in lines[lines.index(header) + 1:]:
        row = dict(zip(CURRENT_STATEMENT_COLUMNS, next(csv.reader([quote], strict=True)), strict=True))
        if (max(date.fromisoformat(row[k]) for k in ("ann_date", "period_end", "f_ann_date") if row[k]) <= as_of
                and all(datetime.fromisoformat(row[k]) <= observed_by for k in ("observed_at", "retrieved_at"))):
            eligible.append((row, quote))
    if not eligible:
        return None
    period = max(row["period_end"] for row, _ in eligible)
    field, provider_field = FIELDS[key.tool_name]
    selected = [(row, quote) for row, quote in eligible if row["period_end"] == period and row["field"] == field]
    if len(selected) != 1 or selected[0][0]["provider_field"] != provider_field:
        return None
    fact = bind_current_statement_observation(
        raw_text=artifact.raw_text, quote=selected[0][1], source_ref=artifact.source_ref,
        tool_call_id=artifact.tool_call_id, tool_name=key.tool_name,
        artifact_sha256=artifact.artifact_sha256, symbol=symbol,
    )
    if (not fact or fact.frequency != key.frequency.value or not fact.filing_context
            or fact.filing_context.report_type != "1" or fact.filing_context.consolidation_scope != "consolidated"):
        return None
    return fact


def current_facts(ledger: dict, *, owner_sha256: str, symbol: str, as_of: date,
                  observed_by: datetime) -> dict[str, FinancialObservation]:
    """Caller supplies the sealed owner digest and independently trusted observation cutoff."""
    if observed_by.utcoffset() is None:
        return {}
    try:
        if ledger_sha256(ledger) != owner_sha256:
            return {}
        checkpoint = FinancialDispatchCheckpointLedger.model_validate(ledger)
    except (ValueError, TypeError, ValidationError):
        return {}
    found = {}
    seen = set()
    for entry in checkpoint.entries:
        key = entry.canonical_request_key
        if (key.tool_name not in FIELDS or key.instrument_identity.canonical_symbol != symbol
                or key.as_of_date != as_of or key.frequency.value != "quarterly"):
            continue
        if key.tool_name in seen:
            found.pop(key.tool_name, None)
            continue
        seen.add(key.tool_name)
        try:
            fact = _latest(entry, symbol, as_of, observed_by)
        except (ValueError, TypeError, csv.Error, StopIteration):
            continue
        if fact:
            found[key.tool_name] = fact
    return found

