"""Select exact, bounded CURRENT statement excerpts for personal research."""

from collections import Counter
from csv import Error, reader
from datetime import date

from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialStatementType,
    financial_statement_field_declaration,
)
from tradingagents.financial_observations import (
    CURRENT_STATEMENT_COLUMNS,
    STATEMENT_TITLES,
    FinancialObservation,
    bind_current_statement_observation,
)

_STATEMENT_TYPES = {
    "get_balance_sheet": FinancialStatementType.BALANCE_SHEET,
    "get_income_statement": FinancialStatementType.INCOME_STATEMENT,
    "get_cashflow": FinancialStatementType.CASH_FLOW,
}
_CORE_FIELDS = {
    tool: frozenset(
        field
        for company in (FinancialCompanyType.BANK, FinancialCompanyType.INDUSTRIAL_NON_BANK)
        for field in financial_statement_field_declaration(
            company, statement, declaration_version="financial-field-declarations-tushare-v2"
        ).core_fields
    )
    for tool, statement in _STATEMENT_TYPES.items()
}
_NOTE_PREFIXES = (
    "# CURRENT-only;",
    "# Query coverage is",
    "# Income/cashflow quarterly rows",
    "# bank_not_applicable_metrics=",
    "# bank_unavailable_metrics=",
    "# degraded=",
    "# missing_announcement_ranges=",
    "# stale_announcement_ranges=",
    "# fetch_failure=",
)
_MAX_LINE = 768
_MAX_PERIODS = {"annual": 5, "quarterly": 8}
_HEADER = ",".join(CURRENT_STATEMENT_COLUMNS)


def _cells(quote):
    return dict(zip(CURRENT_STATEMENT_COLUMNS, next(reader([quote], strict=True)), strict=True))


def verified_row_freshness(quote):
    """Format timestamps from an already source-bound row."""
    row = _cells(quote)
    return f" | retrieved_at={row['retrieved_at']} | observed_at={row['observed_at']}"


def statement_excerpts(
    raw_text: str,
    *,
    tool_name: str,
    frequency: str,
    ticker: str,
    trade_date: str,
    source_ref: str,
    tool_call_id: str,
    artifact_sha256: str,
) -> tuple[list[tuple[str, FinancialObservation]], list[str], list[str]]:
    """Return exact rows and notes, plus non-citable missing-field observations."""
    lines = raw_text.splitlines()
    title = f"# {STATEMENT_TITLES[tool_name]} data for {ticker} ({frequency})"
    if frequency not in _MAX_PERIODS or not lines or lines[0] != title or _HEADER not in lines:
        return [], [], []
    header_at = lines.index(_HEADER)
    as_of = date.fromisoformat(trade_date)
    candidates = []
    for quote in lines[header_at + 1 :]:
        if len(quote) > _MAX_LINE:
            continue
        try:
            row = _cells(quote)
            if any(
                date.fromisoformat(row[name]) > as_of
                for name in ("period_end", "ann_date", "f_ann_date")
                if row[name]
            ):
                continue
        except (Error, ValueError, StopIteration):
            continue
        if row["field"] in _CORE_FIELDS[tool_name]:
            candidates.append((row, quote))
    keys = Counter((row["period_end"], row["field"]) for row, _ in candidates)
    rows = []
    periods = sorted({row["period_end"] for row, _ in candidates}, reverse=True)[
        : _MAX_PERIODS[frequency]
    ]
    for row, quote in candidates:
        period = row["period_end"]
        if (
            keys[period, row["field"]] != 1
            or row["value_status"] != "reported"
            or period not in periods
        ):
            continue
        observation = bind_current_statement_observation(
            raw_text=raw_text,
            quote=quote,
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            artifact_sha256=artifact_sha256,
            symbol=ticker,
        )
        if observation is not None:
            rows.append((quote, observation))
    if not rows:
        return [], [], []
    notes = []
    for prefix in _NOTE_PREFIXES:
        matches = [line for line in lines[:header_at] if line.startswith(prefix)]
        if len(matches) == 1 and len(matches[0]) <= _MAX_LINE and lines.count(matches[0]) == 1:
            notes.append(matches[0])
    gaps = {
        (
            row["period_end"],
            row["field"],
            "ambiguous" if keys[row["period_end"], row["field"]] > 1 else "missing",
        )
        for row, _ in candidates
        if row["period_end"] in periods
        and (row["value_status"] == "missing" or keys[row["period_end"], row["field"]] > 1)
    }
    return rows, notes, [f"{period} {field}: {status}" for period, field, status in sorted(gaps)]
