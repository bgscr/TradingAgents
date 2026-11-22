"""Typed evidence shared by analysts and deterministic decision gates."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple

from dateutil.relativedelta import relativedelta
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from tradingagents.comparison_evidence import ComparisonRecorder, NumericComparisonEvidence
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityProjectionBinding,
)
from tradingagents.financial_observations import (
    FINANCIAL_TOOLS,
    FinancialObservation,
    bind_current_statement_observation,
)

if TYPE_CHECKING:
    from tradingagents.asset_configuration import RunAssetConfiguration
    from tradingagents.decision_policy import DecisionHorizon, DecisionPolicyEngine

EVIDENCE_CONTRACT_VERSION = "1.0"
ANALYSIS_OUTCOME_CONTRACT_VERSION = "2.0"
ACQUISITION_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_ACQUISITION_NAMESPACE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
)


def stable_acquisition_source_ref(namespace: str, *components: object) -> str:
    """Return a versioned opaque locator without retaining raw source metadata."""

    if _ACQUISITION_NAMESPACE_PATTERN.fullmatch(namespace) is None:
        raise ValueError("acquisition source-ref namespace is invalid")
    digest = sha256()
    for component in components:
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"acq.v1:{namespace}:{digest.hexdigest()}"


def checkpoint_safe_identity_source_ref(
    *,
    provider: str,
    source_ref: str,
    artifact_sha256: str,
) -> str:
    """Opaque query-bearing identity provenance at canonical state boundaries."""

    if "?" not in source_ref:
        return source_ref
    return stable_acquisition_source_ref(
        "identity.provenance",
        provider,
        source_ref,
        artifact_sha256,
    )


def stable_market_snapshot_id(
    *,
    symbol: str,
    provider: str,
    adjustment_basis: str,
    requested_date: str,
    effective_trading_date: str,
    frame_sha256: str,
    history_rows: int,
) -> str:
    """Return the canonical identity of an authoritative market snapshot."""

    identity = "\0".join(
        (
            symbol.strip().upper(),
            provider,
            adjustment_basis,
            requested_date,
            effective_trading_date,
            frame_sha256,
            str(history_rows),
        )
    )
    return f"snapshot:{sha256(identity.encode('utf-8')).hexdigest()}"

_NUMERIC_CLAIM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
    r"(?![A-Za-z0-9_])"
)
_CLAIM_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"

_NUMERIC_TRANSLATION = str.maketrans(
    {
        "\u2212": "-",
        "\ufe63": "-",
        "\uff0d": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\uff0b": "+",
    }
)


def _normalize_numeric_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_NUMERIC_TRANSLATION)


def _require_concrete_utc_timestamp(value: str) -> str:
    parsed_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        instant = datetime.fromisoformat(parsed_value)
    except ValueError as exc:
        raise ValueError(
            "retrieved_at must be a concrete ISO-8601 UTC timestamp"
        ) from exc
    if (
        instant.tzinfo is None
        or instant.utcoffset() != timezone.utc.utcoffset(instant)
    ):
        raise ValueError("retrieved_at must be a concrete ISO-8601 UTC timestamp")
    return value


def _numeric_claim_tokens(text: str) -> tuple[str, ...]:
    # A sign before an explicit CNY label belongs to the amount. This changes
    # tokenization only, never currency/field qualification or tolerance.
    normalized = re.sub(r"(?<![\w.+-])([+-])CNY[ \t]+(?=\d)", r"\1",
                        _normalize_numeric_text(text))
    return tuple(
        match.group(0).replace(",", "").removeprefix("+")
        for match in _NUMERIC_CLAIM_PATTERN.finditer(normalized)
    )


def _bound_instrument_symbol(message, call, identity):
    """Recognize an exact registry identity only in its paired source request."""
    if not isinstance(identity, InstrumentIdentityEvidence) or not identity.is_authoritative:
        return None
    symbol = identity.symbol
    if re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", symbol) is None:
        return None
    if identity.venue != {"SS": "XSHG", "SZ": "XSHE"}[symbol[-2:]]:
        return None
    if (not isinstance(call, Mapping) or call.get("name") != getattr(message, "name", None)
            or str(call.get("id", "")) != str(getattr(message, "tool_call_id", ""))
            or not isinstance(call.get("args"), Mapping)):
        return None
    values = [call["args"][key] for key in ("ticker", "symbol") if key in call["args"]]
    if not values or any(value != symbol for value in values):
        return None
    return symbol


def _instrument_numeric_statement(statement: str, symbol: str | None) -> str:
    """Exclude identifier spans, never other occurrences of their numeric digits."""
    if symbol is None:
        return statement
    normalized = _normalize_numeric_text(statement)
    return re.sub(r"(?<![A-Za-z0-9_.+-])" + re.escape(symbol) + r"(?![A-Za-z0-9_.])", "", normalized)


def _has_unbound_source_projection(source_ref, available_messages, all_messages):
    for other in all_messages:
        raw = getattr(other, "artifact", None)
        declared_ref = raw.get("source_ref") if isinstance(raw, Mapping) else getattr(raw, "source_ref", None)
        if declared_ref == source_ref and all(other is not m for m in available_messages):
            return True
    return False


def _bound_stock_projection_symbol(message, available_messages, all_messages, calls, identity,
                                   source_ref, quote, statement):
    """Bind an exact OHLCV row despite distinct projections of one snapshot.

    This narrow extension supplies only an identifier span, never a financial
    fact or a date/amount exemption. Acquisition envelopes and bytes have already
    been validated. Duplicate tools, conflicting headers and non-unique quotes
    cannot choose a convenient projection.
    """
    if getattr(message, "name", None) != "get_stock_data":
        return None
    # A same-ref result excluded from the trusted call catalog is unresolved,
    # not evidence that can silently disappear from this identity check.
    if _has_unbound_source_projection(source_ref, available_messages, all_messages):
        return None
    names = [getattr(m, "name", None) for m in available_messages]
    if (len(names) != len(set(names))
            or not set(names) <= {"get_stock_data", "get_verified_market_snapshot", "get_indicators"}
            or sum(str(m.content).count(quote) for m in available_messages) != 1):
        return None
    call = calls.get(str(message.tool_call_id))
    symbol = _bound_instrument_symbol(message, call, identity)
    if symbol is None or any(_bound_instrument_symbol(m, calls.get(str(m.tool_call_id)), identity)
                             != symbol for m in available_messages):
        return None
    # The immutable snapshot must agree across its differently rendered views.
    for label in ("Provider", "Adjustment basis", "Frame SHA-256", "Snapshot ID",
                  "(?:Effective trading date|Latest trading row used)"):
        values = [re.findall(r"(?m)^(?:# |- )?" + label + r": ([^\r\n]+)\r?$",
                             str(m.content)) for m in available_messages]
        flattened = {value for group in values for value in group}
        if any(not group for group in values) or len(flattened) != 1:
            return None
        if label == "Snapshot ID" and flattened != {source_ref}:
            return None
    args = call["args"]
    content = str(message.content)
    lines = content.splitlines()
    expected_title = (f"# Authoritative Market Snapshot for {symbol} "
                      f"from {args.get('start_date')} to {args.get('end_date')}")
    header = "Date,Open,High,Low,Close,Volume"
    if (not lines or lines[0] != expected_title or lines.count(header) != 1
            or lines.count(quote) != 1
            or [line for line in lines if line.startswith("# Requested date:")]
            != [f"# Requested date: {args.get('end_date')}"]):
        return None
    header_index = lines.index(header)
    if lines.index(quote) <= header_index:
        return None
    numeric = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    row_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})(?:," + numeric + r"){5}")
    rows = [row_pattern.fullmatch(line) for line in lines[header_index + 1:] if line]
    if not rows or any(row is None for row in rows):
        return None
    dates = [row[1] for row in rows]
    if len(dates) != len(set(dates)):
        return None
    selected = row_pattern.fullmatch(quote)
    if selected is None:
        return None
    try:
        start, end = date.fromisoformat(args['start_date']), date.fromisoformat(args['end_date'])
        if any(not start <= date.fromisoformat(value) <= end for value in dates):
            return None
        if any(calls[str(m.tool_call_id)]["args"].get("curr_date", end.isoformat()) != end.isoformat()
               for m in available_messages):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    snapshot = next((m for m in available_messages
                     if getattr(m, "name", None) == "get_verified_market_snapshot"), None)
    if snapshot is not None:
        # Shared IDs/headers cannot hide contradictory latest-row projections.
        # Reuse the strict renderer parser, and compare exact decimals rather
        # than adding a new tolerance or certifying the model's field semantics.
        effective = [line.removeprefix("# Effective trading date: ") for line in lines
                     if line.startswith("# Effective trading date: ")]
        if len(effective) != 1 or effective[0] not in dates:
            return None
        stock_latest = next(row[0].split(",")[1:] for row in rows if row[1] == effective[0])
        snapshot_content = str(snapshot.content)
        snapshot_call = calls[str(snapshot.tool_call_id)]
        for field, stock_value in zip(("Open", "High", "Low", "Close", "Volume"), stock_latest, strict=True):
            rendered = re.findall(r"(?m)^\| " + field + r" \| [^|\r\n]+ \|\r?$", snapshot_content)
            if len(rendered) != 1:
                return None
            row_quote = rendered[0].rstrip("\r")
            snapshot_row = _bound_snapshot_row(snapshot, snapshot_content, row_quote,
                snapshot_content.find(row_quote), snapshot_call, source_ref)
            if snapshot_row is None or snapshot_row.effective_date != effective[0]:
                return None
            try:
                if Decimal(snapshot_row.value) != Decimal(stock_value):
                    return None
            except InvalidOperation:
                return None
    stated_dates = re.findall(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", _normalize_numeric_text(statement))
    if any(value != selected[1] for value in stated_dates):
        return None
    return symbol


def _numeric_token_parts(token: str) -> tuple[Decimal, int, bool] | None:
    normalized = token.replace(",", "").removeprefix("+")
    is_percent = normalized.endswith("%")
    number = normalized.removesuffix("%")
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None
    decimals = len(number.partition(".")[2]) if "." in number else 0
    return value, decimals, is_percent


def _numeric_token_supported(claim_token: str, source_tokens: Iterable[str], *, comparison=None) -> bool:
    claim_parts = _numeric_token_parts(claim_token)
    if claim_parts is None:
        return False
    claim_value, claim_decimals, claim_percent = claim_parts
    compared = False
    for source_token in source_tokens:
        source_parts = _numeric_token_parts(source_token)
        if source_parts is None:
            continue
        source_value, _, source_percent = source_parts
        compared = True
        if claim_percent != source_percent:
            if comparison is not None:
                comparison(claim_token, source_token, "percent_mismatch", None, False)
            continue
        if claim_value == source_value:
            if comparison is not None:
                comparison(claim_token, source_token, "exact", None, True)
            return True
        if claim_decimals > 0:
            tolerance = Decimal(5).scaleb(-(claim_decimals + 1))
            matched = abs(claim_value - source_value) <= tolerance
            if comparison is not None:
                comparison(claim_token, source_token, "absolute_tolerance", tolerance, matched)
            if matched:
                return True
        elif comparison is not None:
            comparison(claim_token, source_token, "exact", None, False)
    if not compared and comparison is not None:
        comparison(claim_token, None, "no_reference", None, False)
    return False


_MOVING_AVERAGE_NAMES = {
    "close_50_sma": ("50", {"均线", "简单均线", "简单移动均线", "简单移动平均线", "SMA"}),
    "close_200_sma": ("200", {"均线", "简单均线", "简单移动均线", "简单移动平均线", "SMA"}),
    "close_10_ema": ("10", {"均线", "指数均线", "指数移动均线", "指数移动平均线", "EMA"}),
}
# Acronyms use the same whole-observation, bound-period/type checks as Chinese
# names. Only their name span is metadata; measured values remain evidence-bound.
_MOVING_AVERAGE_LABEL = (
    r"(?P<period>\d+)[ \t]*日[ \t]*"
    r"(?P<kind>简单移动平均线|指数移动平均线|简单移动均线|指数移动均线|简单均线|指数均线|均线|SMA|EMA)"
)
_MOVING_AVERAGE_LABEL_RE = re.compile(_MOVING_AVERAGE_LABEL)
_MOVING_AVERAGE_OBSERVATION_RE = re.compile(
    r"(?:(?:截至|在|于)?\s*(?P<date>\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日)\s*(?:[，,]|的)?\s*)?"
    r"(?P<latest>最新[ \t]*)?"
    + _MOVING_AVERAGE_LABEL
    + r"[ \t]*(?:值)?\s*(?:为|是|[:=])\s*(?:约)?(?P<value>[+-]?\d+(?:\.\d+)?)\s*[。. ]?"
)
_INDICATOR_CSV_ROW_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}),(?P<indicator>[a-z][a-z0-9_]*),(?P<value>[+-]?\d+(?:\.\d+)?)"
)
_INDICATOR_TEXT_ROW_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2}): (?P<value>[+-]?\d+(?:\.\d+)?)")
_INDICATOR_DATED_LINE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2}):.*")


class _BoundIndicatorObservation(NamedTuple):
    indicator: str
    effective_date: str
    value: str


class _BoundSnapshotRow(NamedTuple):
    field: str
    effective_date: str
    value: str


def _bound_snapshot_row(message, content, quote, span_start, call, source_ref):
    """Bind one whole row of the trusted snapshot renderer, not arbitrary prose.

    The caller has already validated the call-bound acquisition envelope and
    selected artifact bytes. This only supplies row/label context to excerpt
    checking; it creates neither a canonical fact nor a strategy rule.
    """
    if (getattr(message, "name", None) != "get_verified_market_snapshot"
            or not isinstance(call, Mapping) or call.get("name") != message.name
            or call.get("id") != message.tool_call_id or not isinstance(call.get("args"), Mapping)):
        return None
    args = call["args"]
    titles = re.findall(r"(?m)^## Verified market data snapshot for (\S+)\r?$", content)
    requested = re.findall(r"(?m)^- Requested analysis date: (\d{4}-\d{2}-\d{2})\r?$", content)
    latest = re.findall(r"(?m)^- Latest trading row used: (\d{4}-\d{2}-\d{2})\r?$", content)
    ids = re.findall(r"(?m)^Snapshot ID: (\S+)\r?$", content)
    frames = re.findall(r"(?m)^Frame SHA-256: ([0-9a-f]{64})\r?$", content)
    prefixes = ("## Verified market data snapshot for ", "- Requested analysis date: ",
                "- Latest trading row used: ", "Snapshot ID: ", "Frame SHA-256: ")
    if any(sum(line.startswith(prefix) for line in content.splitlines()) != 1 for prefix in prefixes):
        return None
    if (titles != [args.get("symbol")] or requested != [args.get("curr_date")]
            or len(latest) != 1 or ids != [source_ref] or len(frames) != 1):
        return None
    try:
        if date.fromisoformat(latest[0]) > date.fromisoformat(requested[0]):
            return None
    except (ValueError, TypeError):
        return None
    if span_start != content.rfind("\n", 0, span_start) + 1:
        return None
    end = content.find("\n", span_start)
    if quote != content[span_start:end if end >= 0 else len(content)].rstrip("\r"):
        return None
    row = re.fullmatch(r"\| ([A-Za-z][A-Za-z0-9_]*) \| ([+-]?\d+(?:\.\d+)?) \|", quote)
    if row is None:
        return None
    field, value = row.groups()
    if field in {"Open", "High", "Low", "Close", "Volume"}:
        title, header = "### Latest verified OHLCV row", "| Field | Value |"
    elif field in _MOVING_AVERAGE_NAMES or field == "rsi":
        title, header = "### Verified technical indicators (latest row)", "| Indicator | Value |"
    else:
        return None
    headings = tuple(re.finditer(r"(?m)^#{1,6} [^\r\n]+", content))
    first_table = next((h.start() for h in headings if h[0].startswith("### ")), -1)
    if any(content.find(prefix) >= first_table for prefix in prefixes):
        return None
    section = [h for h in headings if h[0] == title]
    if len(section) != 1:
        return None
    start = section[0].end()
    end = next((h.start() for h in headings if h.start() > section[0].start()), len(content))
    if not start < span_start < end:
        return None
    lines = [line for line in content[start:end].splitlines() if line]
    if lines[:2] != [header, "|---|---:|"]:
        return None
    rows = [re.fullmatch(r"\| ([A-Za-z][A-Za-z0-9_]*) \| ([^|]+) \|", line) for line in lines[2:]]
    if (not rows or any(r is None for r in rows)
            or len({r[1] for r in rows}) != len(rows)
            or sum(r[1] == field and r[2] == value for r in rows) != 1):
        return None
    return _BoundSnapshotRow(field, latest[0], value)


def _snapshot_dated_statement(statement, row):
    """Recognize one explicit observation date from this exact row projection."""
    if row is None:
        return statement, None
    normalized = _normalize_numeric_text(statement)
    matches = tuple(re.finditer(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", normalized))
    if not matches:
        return statement, None
    if len(matches) != 1 or matches[0][0] != row.effective_date:
        return statement, "snapshot observation date conflicts with the cited row"
    # The closed Chinese moving-average grammar validates and removes its own
    # date span together with the bound period below. Other snapshot prose
    # retains the explicit English observation-date rule.
    chinese_observation = _MOVING_AVERAGE_OBSERVATION_RE.fullmatch(normalized.strip())
    if chinese_observation is not None and chinese_observation["date"]:
        return statement, None
    match = matches[0]
    if re.search(r"\b(?:on|as of)\s+$", normalized[:match.start()], re.IGNORECASE) is None:
        return statement, "snapshot date is not an explicit observation-date span"
    normalized = normalized[:match.start()] + normalized[match.end():]
    return normalized, None


def _bound_indicator_observation(message, content, quote, span_start, call):
    """Bind one complete data row to its requested, known indicator schema."""
    if (getattr(message, "name", None) != "get_indicators" or not isinstance(call, Mapping)
            or call.get("name") != "get_indicators" or not isinstance(call.get("args"), Mapping)):
        return None
    requested = call["args"].get("indicator")
    if not isinstance(requested, str):
        return None
    requested_ids = tuple(item.strip().casefold() for item in requested.split(","))
    if span_start != content.rfind("\n", 0, span_start) + 1:
        return None
    line_end = content.find("\n", span_start)
    if quote != content[span_start:line_end if line_end >= 0 else len(content)].rstrip("\r"):
        return None
    csv_row = _INDICATOR_CSV_ROW_RE.fullmatch(quote)
    if csv_row:
        headers = tuple(re.finditer(r"(?m)^date,indicator,value\r?$", content))
        if len(headers) != 1 or headers[0].end() >= span_start:
            return None
        table_rows = [_INDICATOR_CSV_ROW_RE.fullmatch(line)
                      for line in content[headers[0].end():].splitlines() if line]
        if any(item is None for item in table_rows):
            return None
        indicator = csv_row["indicator"]
        row = csv_row
        if sum(item["date"] == row["date"] and item["indicator"] == indicator for item in table_rows) != 1:
            return None
    else:
        row = _INDICATOR_TEXT_ROW_RE.fullmatch(quote)
        if row is None:
            return None
        # Every heading ends the previous section, including unrequested/unknown ones.
        headings = tuple(re.finditer(r"(?m)^#{1,6}\s+[^\r\n]+", content))
        previous = [heading for heading in headings if heading.end() < span_start]
        if not previous:
            return None
        heading = re.fullmatch(
            r"## ([a-z][a-z0-9_]*) values from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}):", previous[-1][0])
        if heading is None:
            return None
        indicator = heading[1]
        same_indicator_headings = [item for item in headings
            if re.match(rf"## {re.escape(indicator)} values\b", item[0])]
        if len(same_indicator_headings) != 1:
            return None
        section_end = next((item.start() for item in headings if item.start() > previous[-1].start()), len(content))
        # The production renderer has a single consecutive date-row block after
        # the heading. Its footer/description is not observation data. Count N/A
        # or malformed values too, so they cannot conceal a same-date conflict.
        dated_rows = []
        position = previous[-1].end()
        for line in content[position:section_end].splitlines(keepends=True):
            start = position
            position += len(line)
            stripped = line.rstrip("\r\n")
            if not dated_rows and not stripped.strip():
                continue
            dated = _INDICATOR_DATED_LINE_RE.fullmatch(stripped)
            if dated is None:
                break
            dated_rows.append((start, dated["date"]))
        if (not any(start == span_start for start, _ in dated_rows)
                or sum(day == row["date"] for _, day in dated_rows) != 1):
            return None
        try:
            if not date.fromisoformat(heading[2]) <= date.fromisoformat(row["date"]) <= date.fromisoformat(heading[3]):
                return None
        except ValueError:
            return None
    if indicator not in _MOVING_AVERAGE_NAMES or indicator not in requested_ids:
        return None
    try:
        date.fromisoformat(row["date"])
    except ValueError:
        return None
    return _BoundIndicatorObservation(indicator, row["date"], row["value"])


def _bound_indicator_projection(message, available_messages, all_messages, calls,
                                identity, source_ref, quote):
    """One exact row among compatible trusted views, never select by value."""
    if (getattr(message, "name", None) != "get_indicators"
            or _has_unbound_source_projection(source_ref, available_messages, all_messages)):
        return None
    names = [getattr(m, "name", None) for m in available_messages]
    if (len(names) != len(set(names))
            or not set(names) <= {"get_stock_data", "get_verified_market_snapshot", "get_indicators"}
            or sum(str(m.content).count(quote) for m in available_messages) != 1):
        return None
    call = calls.get(str(message.tool_call_id))
    symbol = _bound_instrument_symbol(message, call, identity)
    if symbol is None or any(_bound_instrument_symbol(m, calls.get(str(m.tool_call_id)), identity)
                             != symbol for m in available_messages):
        return None
    for companion in available_messages:
        args = calls[str(companion.tool_call_id)]['args']
        if companion.name == 'get_stock_data':
            title = (f"# Authoritative Market Snapshot for {symbol} "
                     f"from {args.get('start_date')} to {args.get('end_date')}")
        elif companion.name == 'get_verified_market_snapshot':
            title = f"## Verified market data snapshot for {symbol}"
        else:
            continue
        if not str(companion.content).startswith(title + '\n'):
            return None
    metadata = {}
    for label in ("Provider", "Adjustment basis", "Frame SHA-256", "Snapshot ID",
                  "(?:Effective trading date|Latest trading row used)"):
        groups = [re.findall(r"(?m)^(?:# |- )?" + label + r": ([^\r\n]+)\r?$", str(m.content))
                  for m in available_messages]
        values = {value for group in groups for value in group}
        if any(not group for group in groups) or len(values) != 1:
            return None
        metadata[label] = next(iter(values))
    if (metadata['Snapshot ID'] != source_ref
            or re.fullmatch(r"[0-9a-f]{64}", metadata['Frame SHA-256']) is None):
        return None
    effective = metadata['(?:Effective trading date|Latest trading row used)']
    requested = call['args'].get('curr_date')
    try:
        if date.fromisoformat(effective) > date.fromisoformat(requested):
            return None
        if any(calls[str(m.tool_call_id)]['args'].get('curr_date',
               calls[str(m.tool_call_id)]['args'].get('end_date')) != requested
               for m in available_messages):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    content = str(message.content)
    observation = _bound_indicator_observation(message, content, quote, content.find(quote), call)
    if observation is None or observation.effective_date > effective:
        return None
    # A latest-value companion cannot contradict the exact indicator row.
    snapshot = next((m for m in available_messages if m.name == 'get_verified_market_snapshot'), None)
    if snapshot is not None and observation.effective_date == effective:
        snapshot_content = str(snapshot.content)
        rows = re.findall(r"(?m)^\| " + re.escape(observation.indicator) + r" \| [^|\r\n]+ \|\r?$", snapshot_content)
        if len(rows) != 1:
            return None
        row_quote = rows[0].rstrip('\r')
        row = _bound_snapshot_row(snapshot, snapshot_content, row_quote, snapshot_content.find(row_quote),
                                  calls.get(str(snapshot.tool_call_id)), source_ref)
        if (row is None or row.effective_date != effective
                or not _numeric_token_supported(row.value, (observation.value,))):
            return None
    return observation


def _moving_average_numeric_statement(statement, observation, *, snapshot_context=False,
                                     indicator_context=False, comparison=None):
    """Normalize only a bound name span; keep values and evidence verdict strict."""
    normalized = _normalize_numeric_text(statement).strip()
    english = tuple(re.finditer(r"(?<![\w.-])(?P<period>\d+)-day (?P<kind>SMA|EMA)(?!\w)", normalized, re.IGNORECASE))
    if english:
        # One exact identity span, never all occurrences of its digits. Other
        # numeric observations retain the existing quote comparator/tolerance.
        labels = re.findall(r"\b(?:SMA|EMA|RSI|MACD|ATR)\b|close_\d+_(?:sma|ema)", normalized, re.IGNORECASE)
        if (not (snapshot_context or indicator_context) or len(english) != 1 or len(labels) != 1 or observation is None
                or _MOVING_AVERAGE_LABEL_RE.search(normalized)):
            return statement, (), "indicator name metadata is not bound to one unambiguous observation"
        match = english[0]
        period, _ = _MOVING_AVERAGE_NAMES[observation.indicator]
        if match["period"] != period or match["kind"].lower() != observation.indicator.rsplit("_", 1)[1]:
            return statement, (), "indicator name metadata conflicts with the cited indicator"
        dates = re.findall(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", normalized)
        if any(value != observation.effective_date for value in dates):
            return statement, (), "indicator observation date conflicts with the cited row"
        if re.search(r"(?i)\b(?:USD|CNY|RMB|dollars?|yuan|shares?|lots?|percent|pct|million|billion|thousand)\b|[%$€¥元股手万亿]", normalized):
            return statement, (), "indicator observation units are not established by the cited row"
        start, end = match.span("period")
        without_name_period = normalized[:start] + normalized[end:]
        if indicator_context:
            # Dates are verified against this exact row above; their component
            # digits must never supply the separate observation value.
            amounts = _numeric_claim_tokens(re.sub(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", "", without_name_period))
            if not amounts:
                return statement, (), "indicator observation value is absent from the dated claim"
            mismatches = tuple(value for value in amounts if not _numeric_token_supported(
                value, (observation.value,), comparison=comparison))
            return without_name_period, mismatches, None
        return without_name_period, (), None
    if _MOVING_AVERAGE_LABEL_RE.search(normalized) is None:
        return statement, (), None
    matched = _MOVING_AVERAGE_OBSERVATION_RE.fullmatch(normalized)
    if matched is None or observation is None:
        return statement, (), "indicator name metadata is not bound to one unambiguous unit-free observation"
    # A dated indicator series can quote an older row. Only the separately
    # validated latest-snapshot projection establishes the word "latest".
    if matched["latest"] and not snapshot_context:
        return statement, (), "latest indicator observation requires a bound latest snapshot row"
    period, names = _MOVING_AVERAGE_NAMES[observation.indicator]
    if matched["period"] != period or matched["kind"] not in names:
        return statement, (), "indicator name metadata conflicts with the cited indicator"
    if matched["date"]:
        stated_date = matched["date"].replace("年", "-").replace("月", "-").removesuffix("日")
        try:
            stated_date = date(*map(int, stated_date.split("-"))).isoformat()
        except ValueError:
            return statement, (), "indicator observation date is invalid"
        if stated_date != observation.effective_date:
            return statement, (), "indicator observation date conflicts with the cited row"
    # The observation value may not borrow a matching year/month/day or period.
    unsupported_value = () if _numeric_token_supported(
        matched["value"], (observation.value,), comparison=comparison) else (matched["value"],)
    spans = [matched.span("period")]
    if snapshot_context and matched["date"]:
        spans.append(matched.span("date"))
    for start, end in sorted(spans, reverse=True):
        normalized = normalized[:start] + normalized[end:]
    return normalized, unsupported_value, None


def _source_quote_is_unavailable(quote: str) -> bool:
    normalized = quote.strip().upper()
    return normalized == "N/A" or any(
        marker in normalized
        for marker in (
            "DATA_UNAVAILABLE",
            "NOT_AVAILABLE",
            "UNAVAILABLE",
            "N/A: INSUFFICIENT HISTORY",
            "N/A: NOT A TRADING DAY",
        )
    )


def _source_quote_is_metadata(quote: str) -> bool:
    normalized = quote.strip().casefold()
    normalized = re.sub(r"^(?:#{1,6}|[-*+>])\s*", "", normalized).strip()
    return any(
        normalized.startswith(prefix)
        for prefix in (
            "source:",
            "provider:",
            "adjustment basis:",
            "effective trading date:",
            "retrieved at:",
            "frame sha-256:",
            "snapshot id:",
            "total records:",
            "history rows:",
        )
    )


class EvidenceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CONFLICTED = "conflicted"
    NOT_APPLICABLE = "not_applicable"


class EvidenceReadiness(str, Enum):
    DECISION_READY = "decision_ready"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"


class AnalysisDiagnosticCode(str, Enum):
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    SNAPSHOT_INVALID = "snapshot_invalid"
    HISTORY_INSUFFICIENT = "history_insufficient"
    REQUIRED_EVIDENCE_UNAVAILABLE = "required_evidence_unavailable"
    REQUIRED_EVIDENCE_CONFLICTED = "required_evidence_conflicted"
    DECISION_CONFIGURATION_INVALID = "decision_configuration_invalid"
    STRATEGY_RULE_INVALID = "strategy_rule_invalid"
    DIRECTION_CONTEXT_INVALID = "direction_context_invalid"
    DIRECTION_ASSERTIONS_CONFLICTED = "direction_assertions_conflicted"
    DIRECTION_SELECTION_INVALID = "direction_selection_invalid"
    DECISION_ASSERTION_INVALID = "decision_assertion_invalid"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    SHADOW_MODE = "shadow_mode"
    DETERMINISTIC_GATE_REJECTED = "deterministic_gate_rejected"
    OPTIONAL_EVIDENCE_UNAVAILABLE = "optional_evidence_unavailable"
    INSTRUMENT_CURRENTLY_SUSPENDED = "instrument_currently_suspended"


class ClaimValidationStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class DecisionConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InstrumentKind(str, Enum):
    UNKNOWN = "unknown"
    EQUITY = "equity"
    FUND = "fund"
    INDEX = "index"
    BOND = "bond"
    CRYPTO = "crypto"


class EvidenceCapability(str, Enum):
    MARKET_SNAPSHOT = "market_snapshot"
    INSTRUMENT_NEWS = "instrument_news"
    SOCIAL_SENTIMENT = "social_sentiment"
    COMPANY_FINANCIALS = "company_financials"
    VALUATION = "valuation"
    CORPORATE_ACTIONS = "corporate_actions"
    BENCHMARK = "benchmark"
    NAV_PREMIUM = "nav_premium"
    HOLDINGS_EXPOSURE = "holdings_exposure"
    TRACKING_ERROR = "tracking_error"
    LIQUIDITY = "liquidity"
    LISTING_PUBLICATION_EVIDENCE = "listing_publication_evidence"


class IdentityProvenance(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    provider: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_ref")
    @classmethod
    def _source_ref_is_checkpoint_safe(cls, value: str) -> str:
        if "?" in value:
            raise ValueError("identity provenance cannot contain a query")
        return value


class TradingStatusProvenanceEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    provider: str = Field(min_length=1)
    provider_dataset_id: str = Field(min_length=1)
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["traded", "suspended"]
    observed_at: str = Field(min_length=1)
    revision_id: str | None = None


class CapabilityProfile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    profile_id: str = Field(min_length=1)
    instrument_kind: InstrumentKind
    required_capabilities: tuple[EvidenceCapability, ...]
    optional_capabilities: tuple[EvidenceCapability, ...] = ()
    applicable_analysts: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_unique_disjoint_members(self) -> CapabilityProfile:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if len(set(self.optional_capabilities)) != len(self.optional_capabilities):
            raise ValueError("optional capabilities must be unique")
        if set(self.required_capabilities) & set(self.optional_capabilities):
            raise ValueError("required and optional capabilities must be disjoint")
        if len(set(self.applicable_analysts)) != len(self.applicable_analysts):
            raise ValueError("applicable analysts must be unique")
        if any(not analyst.strip() for analyst in self.applicable_analysts):
            raise ValueError("applicable analysts must be nonblank")
        return self

    @property
    def all_capabilities(self) -> frozenset[EvidenceCapability]:
        return frozenset((*self.required_capabilities, *self.optional_capabilities))

    def requires(self, capability: EvidenceCapability | str) -> bool:
        """Return whether this profile makes a capability admission-critical."""

        return EvidenceCapability(capability) in self.required_capabilities


_CAPABILITY_PROFILES = {
    InstrumentKind.EQUITY: CapabilityProfile(
        profile_id="equity.v1",
        instrument_kind=InstrumentKind.EQUITY,
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
        optional_capabilities=(
            EvidenceCapability.INSTRUMENT_NEWS,
            EvidenceCapability.SOCIAL_SENTIMENT,
            EvidenceCapability.COMPANY_FINANCIALS,
            EvidenceCapability.VALUATION,
            EvidenceCapability.CORPORATE_ACTIONS,
        ),
        applicable_analysts=("market", "social", "news", "fundamentals"),
    ),
    InstrumentKind.FUND: CapabilityProfile(
        profile_id="fund.v1",
        instrument_kind=InstrumentKind.FUND,
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
        optional_capabilities=(
            EvidenceCapability.INSTRUMENT_NEWS,
            EvidenceCapability.SOCIAL_SENTIMENT,
            EvidenceCapability.BENCHMARK,
            EvidenceCapability.NAV_PREMIUM,
            EvidenceCapability.HOLDINGS_EXPOSURE,
            EvidenceCapability.TRACKING_ERROR,
            EvidenceCapability.LIQUIDITY,
        ),
        applicable_analysts=("market", "social", "news"),
    ),
    InstrumentKind.CRYPTO: CapabilityProfile(
        profile_id="crypto.v1",
        instrument_kind=InstrumentKind.CRYPTO,
        required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
        optional_capabilities=(
            EvidenceCapability.INSTRUMENT_NEWS,
            EvidenceCapability.SOCIAL_SENTIMENT,
        ),
        applicable_analysts=("market", "social", "news"),
    ),
}


def capability_profile_for(instrument_kind: InstrumentKind | str) -> CapabilityProfile:
    """Return the registered evidence capability profile for an instrument kind."""
    normalized = InstrumentKind(instrument_kind)
    try:
        return _CAPABILITY_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"no capability profile is registered for instrument kind {normalized.value!r}"
        ) from exc


class InstrumentIdentityEvidence(BaseModel):
    """Versioned identity with a compatibility path for legacy checkpoints.

    Legacy ``symbol + name`` payloads remain parseable, but ``is_authoritative``
    stays false until venue, kind, currency, and provenance are supplied by an
    authoritative registry. No ticker-shape inference is performed here.
    """

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    symbol: str
    venue: str = ""
    instrument_kind: InstrumentKind = InstrumentKind.UNKNOWN
    currency: str = ""
    provenance: IdentityProvenance | None = None
    display_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_name(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "name" not in value:
            return value
        migrated = dict(value)
        legacy_name = migrated.pop("name")
        migrated.setdefault("display_name", legacy_name)
        return migrated

    @property
    def name(self) -> str | None:
        """Deprecated compatibility alias for ``display_name``."""
        return self.display_name

    @property
    def is_authoritative(self) -> bool:
        return bool(
            self.symbol.strip()
            and self.venue.strip()
            and self.instrument_kind is not InstrumentKind.UNKNOWN
            and self.currency.strip()
            and self.provenance is not None
            and bool(self.provenance.artifact_sha256)
        )


class ProviderPhysicalAttemptEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    attempt_event_id: str | None = Field(
        default=None,
        pattern=r"^provider-physical-attempt=sha256:[0-9a-f]{64}$",
    )
    sequence_id: str = Field(min_length=1)
    request_key: str = Field(min_length=1)
    upstream_service_id: str = Field(min_length=1)
    upstream_service_name: str = Field(min_length=1)
    capacity_scope: str = Field(default="all", pattern=ACQUISITION_TOKEN_PATTERN)
    attempt_index: int = Field(ge=1)
    attempted_at: str = Field(min_length=1)
    pacing_event: Literal[
        "permit_acquired", "paced_then_permit_acquired"
    ]
    pacing_wait_seconds: float = Field(ge=0, allow_inf_nan=False)
    outcome: Literal[
        "available",
        "rate_limited",
        "timeout",
        "disconnect",
        "empty_frame",
        "permission_denied",
        "authentication",
        "malformed_response",
        "provider_error",
        "upstream_busy",
        "abandoned",
    ]
    retryable: bool
    status_code: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = None
    retry_after_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    cooldown_changed: bool
    cooldown_until: str | None = None
    final_physical_attempt_count: int = Field(ge=1)

    @field_validator("attempted_at")
    @classmethod
    def _validate_attempted_at(cls, value: str) -> str:
        return _require_concrete_utc_timestamp(value)

    @field_validator("cooldown_until")
    @classmethod
    def _validate_cooldown_until(cls, value: str | None) -> str | None:
        return _require_concrete_utc_timestamp(value) if value is not None else None


def _validate_physical_attempt_accounting(
    events: tuple[ProviderPhysicalAttemptEvidence, ...],
    count: int,
) -> None:
    if count != len(events):
        raise ValueError("physical-attempt count must match the event sequence")
    event_ids = tuple(
        event.attempt_event_id
        for event in events
        if event.attempt_event_id is not None
    )
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("physical-attempt event identities must be unique")
    by_sequence: dict[str, list[ProviderPhysicalAttemptEvidence]] = {}
    for event in events:
        by_sequence.setdefault(event.sequence_id, []).append(event)
    for sequence_events in by_sequence.values():
        expected_count = len(sequence_events)
        if [event.attempt_index for event in sequence_events] != list(
            range(1, expected_count + 1)
        ):
            raise ValueError("physical-attempt indices must be contiguous")
        if any(
            event.final_physical_attempt_count != expected_count
            for event in sequence_events
        ):
            raise ValueError("physical-attempt final count is inconsistent")


class MarketSnapshotEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    symbol: str
    provider: str
    retrieved_at: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    history_rows: int = Field(ge=0)
    frame_sha256: str = ""
    snapshot_id: str = ""
    snapshot_id_version: Literal["v1", "v2"] = "v1"
    pin_membership_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    snapshot_manifest_json: str | None = None
    current_tradeability: Literal["unknown", "tradeable", "suspended"] = "unknown"
    current_status_provenance: TradingStatusProvenanceEvidence | None = None
    latest_traded_close: Decimal | None = None
    latest_traded_close_diagnostic: str | None = None
    carried_suspension_close: Decimal | None = None
    history_gap_dates: tuple[str, ...] = ()
    history_store_status: Literal["live", "stored", "degraded"] = "live"
    history_store_diagnostic: str | None = None
    physical_attempt_events: tuple[ProviderPhysicalAttemptEvidence, ...] = ()
    physical_attempt_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_authoritative_status(self) -> MarketSnapshotEvidence:
        provenance = self.current_status_provenance
        if self.current_tradeability == "unknown":
            if (
                provenance is not None
                or self.latest_traded_close is not None
                or self.carried_suspension_close is not None
            ):
                raise ValueError(
                    "unknown tradeability cannot carry authoritative status values"
                )
            return self
        if provenance is None:
            raise ValueError(
                "authoritative current tradeability requires status provenance"
            )
        expected_status = (
            "suspended" if self.current_tradeability == "suspended" else "traded"
        )
        if (
            provenance.provider != self.provider
            or provenance.session_date != self.effective_trading_date
            or provenance.status != expected_status
        ):
            raise ValueError(
                "authoritative status provenance contradicts the market snapshot"
            )
        if self.latest_traded_close is None:
            if not self.latest_traded_close_diagnostic:
                raise ValueError("unavailable latest traded close requires a diagnostic")
        elif self.latest_traded_close_diagnostic is not None:
            raise ValueError(
                "available latest traded close cannot carry an unavailable diagnostic"
            )
        if self.current_tradeability == "suspended":
            if self.carried_suspension_close is None:
                raise ValueError("suspended tradeability requires a carried close")
        else:
            if self.carried_suspension_close is not None:
                raise ValueError("tradeable status cannot carry a suspension close")
            if self.latest_traded_close is None:
                raise ValueError(
                    "tradeable status requires the latest genuinely traded close"
                )
        return self

    @model_validator(mode="after")
    def validate_physical_attempt_accounting(self) -> MarketSnapshotEvidence:
        _validate_physical_attempt_accounting(
            self.physical_attempt_events,
            self.physical_attempt_count,
        )
        return self

    @model_validator(mode="after")
    def validate_snapshot_manifest_binding(self) -> MarketSnapshotEvidence:
        manifest_json = self.snapshot_manifest_json
        if manifest_json is None:
            return self
        if self.snapshot_id_version != "v2":
            raise ValueError("only snapshot v2 may carry a canonical manifest")
        try:
            manifest = json.loads(manifest_json)
            canonical = json.dumps(
                manifest,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("snapshot manifest is not canonical JSON") from exc
        if canonical != manifest_json:
            raise ValueError("snapshot manifest JSON is not canonical")
        digest = sha256(manifest_json.encode("utf-8")).hexdigest()
        if (
            self.snapshot_id != f"snapshot:v2:{digest}"
            or self.pin_membership_digest != digest
        ):
            raise ValueError("snapshot manifest contradicts its v2 identity")
        return self


class MaterialClaim(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    claim_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    analyst: str
    statement: str
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=1)
    source_quote: str = Field(
        min_length=1,
        description=(
            "Exact source-language quotation copied from one cited source. "
            "The localized statement may paraphrase this quote."
        ),
    )
    fact_ids: tuple[str, ...] = ()
    minimum_history_rows: int = Field(
        default=1,
        ge=1,
        description=(
            "Minimum accepted snapshot history needed to support this claim's "
            "calculation; use the exact indicator warmup, or 1 for observations."
        ),
    )


class SubmittedMaterialClaim(BaseModel):
    """Minimal claim DTO exposed to an LLM structured-output schema.

    Provenance-derived fields on :class:`MaterialClaim` are deliberately absent:
    the application, not the model, assigns the analyst, materializes fact IDs,
    and computes indicator history requirements after exact quote validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(
        pattern=_CLAIM_ID_PATTERN,
        description=(
            "Stable claim ID using only letters, digits, underscore, dot, colon, "
            "or hyphen; it must start with a letter."
        ),
    )
    statement: str
    source_ref: str = Field(min_length=1)
    source_quote: str = Field(
        min_length=1,
        description="Exact contiguous source-language quotation from source_ref.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_internal_claim_for_programmatic_compatibility(cls, value: Any) -> Any:
        # This compatibility path does not alter the JSON schema shown to the
        # model. It only avoids breaking programmatic callers that already hold
        # a fully validated internal MaterialClaim.
        if isinstance(value, MaterialClaim):
            return {
                "claim_id": value.claim_id,
                "statement": value.statement,
                "source_ref": value.source_refs[0],
                "source_quote": value.source_quote,
            }
        if (
            isinstance(value, Mapping)
            and "source_ref" not in value
            and "source_refs" in value
        ):
            source_refs = value.get("source_refs")
            if isinstance(source_refs, (list, tuple)) and len(source_refs) == 1:
                # Legacy direct-tool payloads may still include internal fields.
                # Copy only the four submission fields; analyst/fact/history data
                # is never trusted and is deterministically rebuilt downstream.
                return {
                    "claim_id": value.get("claim_id"),
                    "statement": value.get("statement"),
                    "source_ref": source_refs[0],
                    "source_quote": value.get("source_quote"),
                }
        return value


class MissingValuePolicy(str, Enum):
    FAIL = "fail"
    DROP = "drop"
    FORWARD_FILL = "forward_fill"


class CalculationDefinition(BaseModel):
    """Registered deterministic semantics for one derived canonical field."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    calculation_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    version: str = Field(min_length=1)
    input_fields: tuple[str, ...] = Field(min_length=1)
    input_frequency: str = Field(min_length=1)
    applicable_instrument_kinds: tuple[InstrumentKind, ...] | None = None
    minimum_history_rows: int = Field(ge=1)
    warmup_rows: int = Field(ge=0)
    adjustment_basis: str = Field(min_length=1)
    missing_value_policy: MissingValuePolicy
    formula: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    output_field: str = Field(min_length=1)
    output_unit: str = Field(min_length=1)
    precision: int = Field(ge=0)

    @property
    def required_observations(self) -> int:
        return max(self.minimum_history_rows, self.warmup_rows + 1)


class CalculationLineage(BaseModel):
    """Exact authoritative inputs and implementation used for a derived fact."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    calculation_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    calculation_version: str = Field(min_length=1)
    input_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_snapshot_id: str = Field(min_length=1)
    effective_range_start: str = Field(min_length=1)
    effective_range_end: str = Field(min_length=1)
    observations_used: int = Field(ge=1)
    adjustment_basis: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_calculation_lineage(
    definition: CalculationDefinition,
    lineage: CalculationLineage,
) -> None:
    """Fail closed when recorded lineage cannot represent the definition."""
    if lineage.calculation_id != definition.calculation_id:
        raise ValueError("calculation lineage references a different definition")
    if lineage.calculation_version != definition.version:
        raise ValueError("calculation lineage references a different definition version")
    if lineage.implementation_version != definition.implementation_version:
        raise ValueError("calculation lineage implementation version does not match")
    if lineage.adjustment_basis != definition.adjustment_basis:
        raise ValueError("calculation lineage adjustment basis does not match")
    if lineage.observations_used < definition.required_observations:
        raise ValueError(
            "calculation lineage has insufficient history: "
            f"{lineage.observations_used} < {definition.required_observations}"
        )
    try:
        range_start = datetime.fromisoformat(lineage.effective_range_start)
        range_end = datetime.fromisoformat(lineage.effective_range_end)
    except ValueError as exc:
        raise ValueError("calculation lineage effective range is not ISO-8601") from exc
    if range_start > range_end:
        raise ValueError("calculation lineage effective range is reversed")


def stable_source_fact_id(
    *,
    source_ref: str,
    artifact_sha256: str,
    source_span_start: int,
    source_span_end: int,
    canonical_field: str = "",
    instrument_symbol: str = "",
    effective_date: str = "",
) -> str:
    """Derive a fact ID from source-bound semantics, never runtime/model IDs."""
    canonical_identity = "\0".join(
        (
            source_ref,
            artifact_sha256,
            str(source_span_start),
            str(source_span_end),
            canonical_field,
            instrument_symbol,
            effective_date,
        )
    )
    return f"fact:{sha256(canonical_identity.encode()).hexdigest()}"


class SourceFactAvailabilityLineage(BaseModel):
    """Closed financial availability proof carried by a v2 Source Fact."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-source-fact-availability-v2"] = (
        "financial-source-fact-availability-v2"
    )
    requested_analysis_scope: Literal["current_only", "strict_pit_eligible"]
    availability_scope: Literal["current_only", "strict_pit_eligible"]
    availability_basis: Literal[
        "ann_date_without_first_publication",
        "ann_date_with_strict_pit_cutoff_failure",
        "ann_date_first_publication_and_observed_revision",
    ]
    analysis_as_of_date: date
    ann_date: date
    f_ann_date: date | None = None
    provider_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    observed_at: datetime
    retrieved_at: datetime
    family: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    period: date
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]
    availability_reason: Literal[
        "missing_first_publication_for_strict_pit",
        "pit_cutoff_not_satisfied",
    ] | None = None

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _canonicalize_availability_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("financial Source Fact availability time must be concrete")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_availability(self) -> SourceFactAvailabilityLineage:
        if self.local_provider_revision_identity != self.provider_artifact_identity:
            raise ValueError("financial Source Fact revision lineage is contradictory")
        if self.ann_date > self.analysis_as_of_date:
            raise ValueError("financial Source Fact announcement is after the cutoff")
        if self.availability_scope == "current_only":
            if self.f_ann_date is None:
                expected_basis = "ann_date_without_first_publication"
                expected_reason = "missing_first_publication_for_strict_pit"
            else:
                expected_basis = "ann_date_with_strict_pit_cutoff_failure"
                expected_reason = "pit_cutoff_not_satisfied"
            if (
                self.availability_basis != expected_basis
                or self.availability_reason != expected_reason
                or self.requested_analysis_scope != "current_only"
            ):
                raise ValueError("current-only Source Fact availability is inconsistent")
        elif (
            self.f_ann_date is None
            or self.f_ann_date > self.analysis_as_of_date
            or self.observed_at.date() > self.analysis_as_of_date
            or self.availability_basis
            != "ann_date_first_publication_and_observed_revision"
            or self.availability_reason is not None
        ):
            raise ValueError("strict-PIT Source Fact availability is incomplete")
        return self

    def supports_analysis_scope(
        self,
        requested_scope: Literal["current_only", "strict_pit_eligible"],
    ) -> bool:
        if requested_scope == "current_only":
            return True
        return self.availability_scope == "strict_pit_eligible"


class SourceFact(BaseModel):
    """Canonical observation bound to, but separate from, a raw source artifact.

    ``raw_text`` is retained as the legacy name for the exact cited excerpt; the
    complete provider payload exists only on :class:`SourceArtifact`.
    """

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    fact_kind: Literal["excerpt", "canonical"] = "excerpt"
    fact_id: str = Field(pattern=r"^fact:[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, pattern=r".*\S.*")
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(min_length=1, pattern=r".*\S.*")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_text: str
    source_span_start: int = Field(ge=0)
    source_span_end: int = Field(ge=0)
    normalized_numeric_tokens: tuple[str, ...] = ()
    calculation_ids: tuple[str, ...] = ()
    canonical_field: str = ""
    normalized_value: Decimal | str | int | bool | None = None
    unit: str = ""
    instrument_symbol: str = ""
    effective_date: str = ""
    calculation_lineage: CalculationLineage | None = None
    availability_lineage: SourceFactAvailabilityLineage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    financial_observation: FinancialObservation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_semantics(self) -> SourceFact:
        if self.financial_observation is not None and self.fact_kind != "excerpt":
            raise ValueError("CURRENT row observation cannot promote a canonical Source Fact")
        if not self.raw_text.strip():
            raise ValueError("Source Fact raw_text must be a nonblank exact excerpt")
        if (
            self.source_span_end <= self.source_span_start
            or self.source_span_end - self.source_span_start != len(self.raw_text)
        ):
            raise ValueError("Source Fact source span must match its exact excerpt")
        if self.fact_kind == "canonical":
            required_text = {
                "canonical_field": self.canonical_field,
                "unit": self.unit,
                "instrument_symbol": self.instrument_symbol,
                "effective_date": self.effective_date,
            }
            missing = tuple(name for name, value in required_text.items() if not value.strip())
            if missing:
                raise ValueError(
                    "canonical Source Fact requires nonblank " + ", ".join(missing)
                )
            if self.normalized_value is None:
                raise ValueError("canonical Source Fact requires normalized_value")
            try:
                datetime.fromisoformat(self.effective_date)
            except ValueError as exc:
                raise ValueError(
                    "canonical Source Fact effective_date must be ISO-8601"
                ) from exc
        return self


class SourceArtifact(BaseModel):
    """Exact immutable tool result whose digest is cited by Source Facts."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, pattern=r".*\S.*")
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(min_length=1, pattern=r".*\S.*")
    raw_text: str = Field(min_length=1, pattern=r".*\S.*")

    @model_validator(mode="after")
    def _validate_content_digest(self) -> SourceArtifact:
        expected = sha256(self.raw_text.encode("utf-8")).hexdigest()
        if self.artifact_sha256 != expected:
            raise ValueError("artifact_sha256 does not match exact UTF-8 raw_text")
        return self


class AcquisitionUnavailableReason(str, Enum):
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    EMPTY_FRAME = "empty_frame"
    NOT_CONFIGURED = "not_configured"
    NO_DATA = "no_data"
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    INSUFFICIENT_HISTORY = "insufficient_history"
    PROVIDER_ERROR = "provider_error"
    UPSTREAM_BUSY = "upstream_busy"
    USAGE_NOT_ENTITLED = "usage_not_entitled"
    REGISTRY_NOT_CONFIGURED = "registry_not_configured"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    IDENTITY_NOT_FOUND = "identity_not_found"
    INTEGRITY_FAILURE = "integrity_failure"
    CIRCUIT_OPEN = "circuit_open"


class SourceAcquisitionAvailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    outcome: Literal["available"] = "available"
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(default=0, ge=0)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    attempt: int = Field(ge=1)
    acquisition_id: str | None = Field(default=None, pattern=ACQUISITION_TOKEN_PATTERN)
    retrieved_at: str = Field(min_length=1)
    retryable: Literal[False] = False
    artifact: SourceArtifact

    @model_serializer(mode="wrap")
    def _serialize_optional_acquisition_identity(self, handler) -> dict[str, Any]:
        # Pydantic 2.12 union serialization warns when exclude_if removes a field.
        payload = handler(self)
        if self.acquisition_id is None:
            payload.pop("acquisition_id", None)
        return payload

    @field_validator("retrieved_at")
    @classmethod
    def _validate_retrieved_at(cls, value: str) -> str:
        return _require_concrete_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_artifact_binding(self) -> SourceAcquisitionAvailable:
        if self.artifact.source_ref != self.source_ref:
            raise ValueError("available artifact source_ref does not match outcome")
        return self


class CalculationReadinessDiagnostic(BaseModel):
    """Closed, payload-free metadata for a rejected deterministic calculation."""

    model_config = _CLOSED_MODEL_CONFIG

    calculation_id: str = Field(min_length=1)
    required_observations: int = Field(ge=0)
    available_observations: int = Field(ge=0)
    input_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceAcquisitionUnavailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    outcome: Literal["unavailable"] = "unavailable"
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(default=0, ge=0)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    attempt: int = Field(ge=1)
    acquisition_id: str | None = Field(default=None, pattern=ACQUISITION_TOKEN_PATTERN)
    retrieved_at: str = Field(min_length=1)
    retryable: bool
    reason: AcquisitionUnavailableReason
    error_code: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
    )
    retry_after_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    calculation_readiness: CalculationReadinessDiagnostic | None = None

    @model_serializer(mode="wrap")
    def _serialize_optional_acquisition_identity(self, handler) -> dict[str, Any]:
        payload = handler(self)
        if self.acquisition_id is None:
            payload.pop("acquisition_id", None)
        return payload

    @field_validator("retrieved_at")
    @classmethod
    def _validate_retrieved_at(cls, value: str) -> str:
        return _require_concrete_utc_timestamp(value)


SourceAcquisitionOutcome = Annotated[
    SourceAcquisitionAvailable | SourceAcquisitionUnavailable,
    Field(discriminator="outcome"),
]


def _canonicalize_source_acquisition_outcomes(
    outcomes: Iterable[SourceAcquisitionOutcome],
) -> tuple[SourceAcquisitionOutcome, ...]:
    by_attempt: dict[tuple[str, str, str, str | None, int], SourceAcquisitionOutcome] = {}
    for outcome in outcomes:
        identity = (
            outcome.provider,
            outcome.capability,
            outcome.source_ref,
            outcome.acquisition_id,
            outcome.attempt,
        )
        existing = by_attempt.get(identity)
        if existing is not None and existing != outcome:
            raise ValueError(
                "source acquisition attempt was redefined for "
                f"{outcome.provider}:{outcome.capability}:{outcome.attempt}"
            )
        by_attempt[identity] = outcome
    return tuple(
        sorted(
            by_attempt.values(),
            key=lambda outcome: (
                outcome.capability,
                outcome.source_ref,
                outcome.provider_order,
                outcome.acquisition_id or "",
                outcome.attempt,
                outcome.provider,
                outcome.retrieved_at,
                outcome.outcome,
            ),
        )
    )


class ToolExecutionEvidenceEnvelope(BaseModel):
    """Checkpoint-safe provenance emitted alongside one exact tool result."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = Field(min_length=1)
    selected_artifact: SourceArtifact | None = None

    @model_validator(mode="after")
    def _validate_local_bindings(self) -> ToolExecutionEvidenceEnvelope:
        available = tuple(
            outcome
            for outcome in self.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        )
        for outcome in self.acquisition_outcomes:
            if outcome.source_ref != self.source_ref:
                raise ValueError("acquisition outcome source_ref does not match envelope")
            if outcome.capability != self.capability:
                raise ValueError("acquisition outcome capability does not match envelope")
        if len(available) > 1:
            raise ValueError("envelope may select at most one available acquisition outcome")
        if not available:
            if self.selected_artifact is not None:
                raise ValueError("unavailable-only envelope cannot expose an artifact")
            return self
        if self.selected_artifact is None:
            raise ValueError("available envelope must expose its selected artifact")
        if available[0].artifact != self.selected_artifact:
            raise ValueError("selected artifact does not match available acquisition outcome")
        artifact = self.selected_artifact
        if artifact.tool_call_id != self.tool_call_id:
            raise ValueError("selected artifact tool_call_id does not match envelope")
        if artifact.tool_name != self.tool_name:
            raise ValueError("selected artifact tool_name does not match envelope")
        if artifact.source_ref != self.source_ref:
            raise ValueError("selected artifact source_ref does not match envelope")
        return self

    @model_validator(mode="after")
    def _bind_news_acquisition_identity(self) -> ToolExecutionEvidenceEnvelope:
        if self.tool_name != "get_news":
            return self
        acquisition_id = stable_acquisition_source_ref(
            "tool-call", self.tool_name, self.tool_call_id
        )
        if any(
            outcome.acquisition_id not in (None, acquisition_id)
            for outcome in self.acquisition_outcomes
        ):
            raise ValueError("news acquisition identity does not match its tool call")
        # Older checkpoints already retain this binding in the envelope. Adapt
        # it in memory, without renumbering retries or rewriting saved results.
        # Bare legacy outcomes have no such authority and keep their old key.
        object.__setattr__(self, "acquisition_outcomes", tuple(
            outcome.model_copy(update={"acquisition_id": acquisition_id})
            if outcome.acquisition_id is None else outcome
            for outcome in self.acquisition_outcomes
        ))
        return self


def _unavailable_reason_for_text(content: str) -> AcquisitionUnavailableReason:
    normalized = content.casefold()
    if (
        "too many requests" in normalized
        or "rate limit" in normalized
        or re.search(r"\b(?:http\s*)?429\b", normalized)
    ):
        return AcquisitionUnavailableReason.RATE_LIMITED
    if (
        "timeout" in normalized
        or "timed out" in normalized
        or re.search(r"\b(?:http\s*)?408\b", normalized)
    ):
        return AcquisitionUnavailableReason.TIMEOUT
    if "not configured" in normalized or "missing configuration" in normalized:
        return AcquisitionUnavailableReason.NOT_CONFIGURED
    if (
        "unauthorized" in normalized
        or "authentication" in normalized
        or "forbidden" in normalized
        or re.search(r"\b(?:http\s*)?(?:401|403)\b", normalized)
    ):
        return AcquisitionUnavailableReason.AUTHENTICATION
    if "malformed" in normalized or "invalid response" in normalized:
        return AcquisitionUnavailableReason.MALFORMED_RESPONSE
    if "no data" in normalized or "unavailable" in normalized or "not_available" in normalized:
        return AcquisitionUnavailableReason.NO_DATA
    return AcquisitionUnavailableReason.PROVIDER_ERROR


def make_source_acquisition_outcome(
    *,
    provider: str,
    capability: str,
    attempt: int,
    retrieved_at: str,
    source_ref: str,
    tool_call_id: str,
    tool_name: str,
    content: str,
    status: str,
    retry_after_seconds: float | None = None,
) -> SourceAcquisitionOutcome:
    """Convert a provider attempt into evidence or a diagnostic, never both."""
    normalized_status = status.strip().casefold()
    availability_content = "\n".join(
        line
        for line in content.splitlines()
        if not line.lstrip().casefold().startswith("data_degraded:")
    )
    unavailable_marker = _source_quote_is_unavailable(availability_content) or any(
        marker in availability_content.casefold()
        for marker in (
            "too many requests",
            "rate limit",
            "data_unavailable",
            "timed out",
            "timeout",
            "unauthorized",
            "forbidden",
            "not configured",
            "missing configuration",
            "invalid response",
            "malformed response",
        )
    )
    unavailable_marker = unavailable_marker or bool(
        re.search(
            r"\b(?:http\s*)?(?:401|403|408|429|5\d{2})\b",
            availability_content,
            re.IGNORECASE,
        )
    )
    if normalized_status in {"error", "failed", "unavailable"} or unavailable_marker:
        reason = _unavailable_reason_for_text(content)
        status_match = re.search(r"\b([1-5]\d{2})\b", content)
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=capability,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            retryable=reason
            in {
                AcquisitionUnavailableReason.RATE_LIMITED,
                AcquisitionUnavailableReason.TIMEOUT,
                AcquisitionUnavailableReason.PROVIDER_ERROR,
            },
            reason=reason,
            retry_after_seconds=retry_after_seconds,
            http_status=int(status_match.group(1)) if status_match else None,
        )
    artifact_digest = sha256(content.encode()).hexdigest()
    return SourceAcquisitionAvailable(
        provider=provider,
        capability=capability,
        source_ref=source_ref,
        attempt=attempt,
        retrieved_at=retrieved_at,
        artifact=SourceArtifact(
            artifact_sha256=artifact_digest,
            source_ref=source_ref,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw_text=content,
        ),
    )


def calculation_readiness_outcome(
    definition: CalculationDefinition,
    *,
    observations_available: int,
    adjustment_basis: str,
    input_artifact_sha256: str,
    provider: str,
    attempt: int,
    retrieved_at: str,
) -> SourceAcquisitionUnavailable | None:
    """Return a typed diagnostic instead of computing with invalid inputs."""
    readiness = CalculationReadinessDiagnostic(
        calculation_id=definition.calculation_id,
        required_observations=definition.required_observations,
        available_observations=observations_available,
        input_artifact_sha256=input_artifact_sha256,
    )
    source_ref = stable_acquisition_source_ref(
        "calculation",
        definition.calculation_id,
        definition.version,
    )
    if observations_available < definition.required_observations:
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=definition.output_field,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            # A calculation for a fixed historical snapshot cannot gain rows by
            # retrying the same acquisition within the run.
            retryable=False,
            reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY,
            calculation_readiness=readiness,
        )
    if adjustment_basis != definition.adjustment_basis:
        return SourceAcquisitionUnavailable(
            provider=provider,
            capability=definition.output_field,
            source_ref=source_ref,
            attempt=attempt,
            retrieved_at=retrieved_at,
            retryable=False,
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            calculation_readiness=readiness,
        )
    return None


class ClaimValidation(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    claim_id: str
    status: ClaimValidationStatus
    detail: str = ""
    fact_ids: tuple[str, ...] = ()
    comparison_evidence: NumericComparisonEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SubmittedEvidenceNote(BaseModel):
    """Source limitations submitted by the model, never financial premises."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    note_id: str = Field(pattern=_CLAIM_ID_PATTERN)
    kind: Literal["provenance", "availability", "coverage", "applicability"]
    statement: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)


class EvidenceNote(SubmittedEvidenceNote):
    """A traceable limitation, separate from Source Facts and claim validation.

    Quote binding establishes what the tool reported, not the semantics of the
    model's paraphrase. Source necessity is the observed source-level policy;
    it does not establish whether an individual metric is required by a rule.
    """

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    analyst: str
    binding_status: Literal["source_bound_quote", "unverified"] = "unverified"
    detail: str = "not bound"
    source_status: EvidenceStatus | None = None
    source_required: bool | None = None
    metric_requirement: Literal["not_assessed"] = "not_assessed"
    semantic_status: Literal["not_established"] = "not_established"
    decision_qualified: Literal[False] = False
    tool_call_id: str | None = None
    source_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_quote_binding(self) -> EvidenceNote:
        proof = (self.tool_call_id, self.source_content_sha256, self.quote_start, self.quote_end)
        if self.binding_status == "source_bound_quote":
            if (any(value is None for value in proof) or not self.tool_call_id
                    or self.source_status is None or self.source_required is None
                    or self.quote_end - self.quote_start != len(self.source_quote)):
                raise ValueError("source-bound note requires a complete, consistent quote location")
        elif any(value is not None for value in proof):
            raise ValueError("unverified note cannot retain a prior quote binding")
        return self


class AnalystEvidenceReport(BaseModel):
    """Typed final payload emitted by a tool-calling analyst."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    report_markdown: str
    material_claims: tuple[SubmittedMaterialClaim, ...] = ()
    evidence_notes: tuple[SubmittedEvidenceNote, ...] = ()


class EvidenceSource(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    source_id: str = Field(min_length=1, pattern=r".*\S.*")
    status: EvidenceStatus
    required: bool
    detail: str = ""


_FINANCIAL_FAMILY_TOKEN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
_FINANCIAL_FAMILY_SHA_REF = r"^[A-Za-z0-9._:-]+=sha256:[0-9a-f]{64}$"
_UNSAFE_FINANCIAL_FAMILY_PROJECTION_TEXT = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "client_secret",
    "credential",
    "password",
    "payload_body",
    "raw_error",
    "secret",
    "traceback",
)
_UNSAFE_FINANCIAL_FAMILY_URL_QUERY = re.compile(r"https?://\S*\?", re.IGNORECASE)


class _FinancialFamilyEvidenceProjection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    @model_validator(mode="after")
    def _validate_safe_projection(self) -> _FinancialFamilyEvidenceProjection:
        serialized = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        folded = serialized.casefold()
        if any(
            marker in folded
            for marker in _UNSAFE_FINANCIAL_FAMILY_PROJECTION_TEXT
        ) or _UNSAFE_FINANCIAL_FAMILY_URL_QUERY.search(serialized) is not None:
            raise ValueError("financial family evidence projection is unsafe")
        return self


class FinancialFamilyAuthorityEvidence(_FinancialFamilyEvidenceProjection):
    authority: Literal[
        "canonical_evidence_contract",
        "capability_profile",
        "strategy_rules",
    ]
    authority_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    required: bool


class FinancialFamilyRequirementEvidence(_FinancialFamilyEvidenceProjection):
    family: Literal[
        "profit",
        "operation",
        "growth",
        "balance",
        "cash_flow",
        "dupont",
    ]
    status: Literal["required", "optional"]
    authority_bindings: tuple[FinancialFamilyAuthorityEvidence, ...] = Field(
        min_length=3,
        max_length=3,
    )
    material_premise_ids: tuple[str, ...] = ()


class FinancialFamilyPeriodCoverageEvidence(_FinancialFamilyEvidenceProjection):
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    period: date
    disposition: Literal[
        "accepted",
        "current_only",
        "rejected",
        "conflicted",
        "unselected",
    ]
    critical_threshold: Decimal
    core_threshold: Decimal
    critical_coverage: Decimal = Field(ge=0, le=1)
    core_coverage: Decimal = Field(ge=0, le=1)
    declared_critical_fields: tuple[str, ...] = ()
    declared_core_fields: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_fixed_thresholds(self) -> FinancialFamilyPeriodCoverageEvidence:
        if self.critical_threshold != Decimal("1") or self.core_threshold != Decimal(
            "0.9"
        ):
            raise ValueError("financial family evidence thresholds are fixed")
        return self


class FinancialMaterialPremiseFamilyEvidence(_FinancialFamilyEvidenceProjection):
    premise_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    family: Literal[
        "profit",
        "operation",
        "growth",
        "balance",
        "cash_flow",
        "dupont",
    ]
    affected_periods: tuple[date, ...]


class FinancialFamilyEvidenceAssessment(_FinancialFamilyEvidenceProjection):
    """Payload-free family outcome retained in Evidence State."""

    contract_version: Literal["financial-family-assessment-v2"]
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]
    requested_analysis_scope: Literal["current_only", "strict_pit_eligible"]
    company_type: Literal[
        "bank",
        "industrial/non-bank",
        "insurance",
        "securities",
        "other-financial",
        "unknown",
    ]
    family: Literal[
        "profit",
        "operation",
        "growth",
        "balance",
        "cash_flow",
        "dupont",
    ]
    target_periods: tuple[date, ...]
    candidate_identities: tuple[str, ...]
    selected_periods: tuple[date, ...]
    current_only_periods: tuple[date, ...]
    rejected_periods: tuple[date, ...]
    conflicted_periods: tuple[date, ...]
    missing_periods: tuple[date, ...]
    coverage: tuple[FinancialFamilyPeriodCoverageEvidence, ...]
    requirement: FinancialFamilyRequirementEvidence
    missing_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyEvidence, ...
    ] = ()
    conflicted_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyEvidence, ...
    ] = ()
    complete: bool
    disposition: Literal[
        "current_only",
        "strict_pit_eligible",
        "degraded",
        "insufficient",
        "conflicted",
    ]
    typed_reasons: tuple[str, ...]
    artifact_identities: tuple[str, ...]
    selection_identities: tuple[str, ...]
    conflict_identities: tuple[str, ...]
    assessment_ref: str = Field(pattern=_FINANCIAL_FAMILY_SHA_REF)

    @model_validator(mode="after")
    def _validate_family_binding(self) -> FinancialFamilyEvidenceAssessment:
        from tradingagents.dataflows.financial_contracts import (
            FinancialIndicatorFamilyAssessment,
        )

        FinancialIndicatorFamilyAssessment.model_validate(
            self.model_dump(mode="python")
        )
        return self


class FinancialFamilyAggregateEvidenceAssessment(
    _FinancialFamilyEvidenceProjection
):
    """Payload-free aggregate retaining its independent family assessments."""

    contract_version: Literal["financial-family-aggregate-v2"]
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]
    requested_analysis_scope: Literal["current_only", "strict_pit_eligible"]
    company_type: Literal[
        "bank",
        "industrial/non-bank",
        "insurance",
        "securities",
        "other-financial",
        "unknown",
    ]
    requirements_identity: str = Field(
        pattern=r"^financial-family-requirements:v1:[0-9a-f]{64}$"
    )
    requested_families: tuple[str, ...]
    family_assessments: tuple[FinancialFamilyEvidenceAssessment, ...]
    complete_families: tuple[str, ...]
    missing_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyEvidence, ...
    ] = ()
    conflicted_material_premise_bindings: tuple[
        FinancialMaterialPremiseFamilyEvidence, ...
    ] = ()
    disposition: Literal[
        "current_only",
        "strict_pit_eligible",
        "degraded",
        "insufficient",
        "conflicted",
    ]
    typed_reasons: tuple[str, ...]
    assessment_ref: str = Field(pattern=_FINANCIAL_FAMILY_SHA_REF)

    @model_validator(mode="after")
    def _validate_aggregate_binding(
        self,
    ) -> FinancialFamilyAggregateEvidenceAssessment:
        from tradingagents.dataflows.financial_contracts import (
            FinancialIndicatorAggregateAssessment,
        )

        FinancialIndicatorAggregateAssessment.model_validate(
            self.model_dump(mode="python")
        )
        return self


class FinancialRevisionCandidateDispositionEvidence(
    _FinancialFamilyEvidenceProjection
):
    contract_version: Literal["financial-revision-selection-v1"]
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    provider_route_rank: int = Field(ge=0)
    disposition: Literal[
        "selected",
        "operational_winner_conflicted",
        "superseded",
        "equivalent_unselected",
        "non_critical_difference_unselected",
        "conflicted_unselected",
    ]


class FinancialRevisionSelectionEvidence(_FinancialFamilyEvidenceProjection):
    """Exact Ticket 05 selection state retained in Evidence State."""

    contract_version: Literal["financial-revision-selection-v1"]
    selection_key_identity: str = Field(
        pattern=r"^financial-selection-key:v1:[0-9a-f]{64}$"
    )
    instrument_identity: InstrumentIdentityEvidence
    company_type: Literal[
        "bank",
        "industrial/non-bank",
        "insurance",
        "securities",
        "other-financial",
        "unknown",
    ]
    capability: Literal["financial_statement", "financial_ratio_family"]
    statement_type: Literal[
        "comprehensive_fundamentals",
        "balance_sheet",
        "cash_flow",
        "income_statement",
    ] | None = None
    ratio_family: Literal[
        "profit",
        "operation",
        "growth",
        "balance",
        "cash_flow",
        "dupont",
    ] | None = None
    period_end: date
    frequency: Literal["annual", "quarterly", "not_applicable"]
    consolidation_scope: Literal[
        "consolidated",
        "parent",
        "unknown",
    ]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    normalized_unit: str = Field(min_length=1, max_length=64)
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    selected_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    selected_provider_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    selected_provider_route_rank: int = Field(ge=0)
    ordering_basis: Literal[
        "qualified_provider_revision_sequence",
        "ann_date_observed_at_retrieved_at_local_revision_identity",
    ]
    chronology_claim: Literal["qualified_provider_sequence", "operational_only"]
    provider_selection_basis: Literal["immutable_route_rank"]
    requested_analysis_scope: Literal[
        "current_only",
        "strict_pit_eligible",
    ] | None = None
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]
    conflicted: bool
    candidate_dispositions: tuple[
        FinancialRevisionCandidateDispositionEvidence, ...
    ]
    selection_identity: str = Field(
        pattern=r"^financial-revision-selection:v1:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_revision_selection(self) -> FinancialRevisionSelectionEvidence:
        from tradingagents.dataflows.financial_contracts import (
            FinancialRevisionSelection,
        )

        FinancialRevisionSelection.model_validate(self.model_dump(mode="python"))
        return self


class FinancialRevisionObservationEvidence(_FinancialFamilyEvidenceProjection):
    """Payload-free identity and disposition of every retained local observation."""

    contract_version: Literal["financial-revision-observation-v1"] = (
        "financial-revision-observation-v1"
    )
    manifest_identity: str = Field(
        pattern=r"^financial-manifest:v2:[0-9a-f]{64}$"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    provider_route_rank: int = Field(ge=0)
    family: str = Field(pattern=_FINANCIAL_FAMILY_TOKEN)
    period: date
    frequency: Literal["annual", "quarterly", "not_applicable"]
    company_type: Literal[
        "bank",
        "industrial/non-bank",
        "insurance",
        "securities",
        "other-financial",
        "unknown",
    ]
    consolidation_scope: Literal["consolidated", "parent", "unknown"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    normalized_units: tuple[str, ...]
    assessment_disposition: Literal[
        "accepted",
        "current_only",
        "rejected",
        "conflicted",
        "unselected",
    ]
    revision_disposition: Literal[
        "selected",
        "operational_winner_conflicted",
        "superseded",
        "equivalent_unselected",
        "non_critical_difference_unselected",
        "conflicted_unselected",
        "rejected",
    ]
    revision_selection_identity: str | None = Field(
        default=None,
        pattern=r"^financial-revision-selection:v1:[0-9a-f]{64}$",
    )
    rejection_reasons: tuple[str, ...]
    conflicted: bool
    requested_analysis_scope: Literal[
        "current_only",
        "strict_pit_eligible",
    ] | None = None
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ]

    @model_validator(mode="after")
    def _validate_observation(self) -> FinancialRevisionObservationEvidence:
        if self.local_provider_revision_identity != self.artifact_identity:
            raise ValueError("financial revision observation identity is contradictory")
        if self.normalized_units != tuple(sorted(set(self.normalized_units))):
            raise ValueError("financial revision observation units must be canonical")
        if (self.revision_selection_identity is None) != (
            self.revision_disposition == "rejected"
        ):
            raise ValueError("financial revision observation selection is incomplete")
        return self


def _canonicalize_source_facts(
    facts: Iterable[SourceFact],
) -> tuple[SourceFact, ...]:
    first_seen: dict[str, SourceFact] = {}
    canonical: list[SourceFact] = []
    for fact in facts:
        existing = first_seen.get(fact.fact_id)
        if existing is None:
            first_seen[fact.fact_id] = fact
            canonical.append(fact)
        elif existing.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ) != fact.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ):
            raise ValueError(f"Source fact ID {fact.fact_id!r} was redefined.")
    return tuple(canonical)


class EvidenceState(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    instrument_identity: InstrumentIdentityEvidence | None = None
    market_snapshot: MarketSnapshotEvidence | None = None
    material_claims: tuple[MaterialClaim, ...] = ()
    evidence_notes: tuple[EvidenceNote, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    source_facts: tuple[SourceFact, ...] = ()
    current_analysis_source_facts: tuple[SourceFact, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    source_artifacts: tuple[SourceArtifact, ...] = ()
    claim_validations: tuple[ClaimValidation, ...] = ()
    sources: tuple[EvidenceSource, ...] = ()
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = ()
    physical_attempt_events: tuple[ProviderPhysicalAttemptEvidence, ...] = ()
    physical_attempt_count: int = Field(default=0, ge=0)
    financial_family_assessments: tuple[FinancialFamilyEvidenceAssessment, ...] = (
        ()
    )
    financial_family_aggregate_assessments: tuple[
        FinancialFamilyAggregateEvidenceAssessment, ...
    ] = ()
    financial_revision_selections: tuple[
        FinancialRevisionSelectionEvidence, ...
    ] = Field(default=(), exclude_if=lambda value: not value)
    financial_revision_observations: tuple[
        FinancialRevisionObservationEvidence, ...
    ] = Field(default=(), exclude_if=lambda value: not value)

    def source_facts_for_analysis_scope(
        self,
        requested_scope: Literal["current_only", "strict_pit_eligible"],
    ) -> tuple[SourceFact, ...]:
        """Return only Source Facts whose availability contract accepts the scope."""

        if requested_scope not in {"current_only", "strict_pit_eligible"}:
            raise ValueError("unknown Source Fact analysis scope")
        admitted = (
            self.current_analysis_source_facts
            if requested_scope == "current_only"
            else ()
        )
        return (*admitted, *(
            fact
            for fact in self.source_facts
            if fact.availability_lineage is not None
            and fact.availability_lineage.supports_analysis_scope(requested_scope)
        ))

    @model_validator(mode="after")
    def _canonicalize_source_fact_collection(self) -> EvidenceState:
        canonical_facts = _canonicalize_source_facts(self.source_facts)
        if canonical_facts != self.source_facts:
            object.__setattr__(self, "source_facts", canonical_facts)
        current_facts = _canonicalize_source_facts(
            self.current_analysis_source_facts
        )
        if current_facts != self.current_analysis_source_facts:
            object.__setattr__(
                self,
                "current_analysis_source_facts",
                current_facts,
            )
        if any(
            fact.availability_lineage is not None
            and fact.availability_lineage.requested_analysis_scope == "current_only"
            for fact in canonical_facts
        ):
            raise ValueError(
                "current-only Source Facts cannot enter general Evidence Admission"
            )
        if any(
            fact.availability_lineage is None
            or fact.availability_lineage.requested_analysis_scope != "current_only"
            for fact in current_facts
        ):
            raise ValueError(
                "current-analysis Source Facts require explicit availability lineage"
            )
        if {fact.fact_id for fact in canonical_facts} & {
            fact.fact_id for fact in current_facts
        }:
            raise ValueError("Source Fact cannot cross analysis-scope collections")
        if self.financial_eligibility_binding is not None:
            policy_version = (
                self.financial_eligibility_binding.eligibility_policy_version
            )
            if any(
                assessment.eligibility_policy_version != policy_version
                for assessment in (
                    *self.financial_family_assessments,
                    *self.financial_family_aggregate_assessments,
                )
            ) or any(
                selection.eligibility_policy_version != policy_version
                for selection in self.financial_revision_selections
            ):
                raise ValueError(
                    "Evidence State financial eligibility binding is contradictory"
                )
        return self

    @field_validator("acquisition_outcomes")
    @classmethod
    def _canonicalize_acquisition_outcomes(
        cls,
        value: tuple[SourceAcquisitionOutcome, ...],
    ) -> tuple[SourceAcquisitionOutcome, ...]:
        return _canonicalize_source_acquisition_outcomes(value)

    @model_validator(mode="after")
    def validate_physical_attempt_accounting(self) -> EvidenceState:
        events = self.physical_attempt_events
        count = self.physical_attempt_count
        snapshot = self.market_snapshot
        run_attempt_fields_omitted = {
            "physical_attempt_events",
            "physical_attempt_count",
        }.isdisjoint(self.model_fields_set)
        if run_attempt_fields_omitted and snapshot is not None:
            events = snapshot.physical_attempt_events
            count = snapshot.physical_attempt_count
            object.__setattr__(self, "physical_attempt_events", events)
            object.__setattr__(self, "physical_attempt_count", count)
        _validate_physical_attempt_accounting(
            events,
            count,
        )
        if snapshot is not None and (
            snapshot.physical_attempt_events != events
            or snapshot.physical_attempt_count != count
        ):
            snapshot_payload = snapshot.model_dump(mode="python")
            snapshot_payload["physical_attempt_events"] = events
            snapshot_payload["physical_attempt_count"] = count
            object.__setattr__(
                self,
                "market_snapshot",
                MarketSnapshotEvidence.model_validate(snapshot_payload),
            )
        return self


class _ToolExecutionEvidenceView(NamedTuple):
    tool_call_id: str
    tool_name: str
    source_ref: str
    capability: str
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...]
    selected_artifact: SourceArtifact | None


def _validated_tool_execution_evidence(
    raw_envelope: object,
) -> ToolExecutionEvidenceEnvelope | _ToolExecutionEvidenceView:
    """Validate either the ordinary or duplicate-aware financial envelope."""

    try:
        return ToolExecutionEvidenceEnvelope.model_validate(raw_envelope)
    except ValidationError as ordinary_error:
        from tradingagents.dataflows.financial_dispatch import (
            FinancialToolMessageAuditEnvelope,
            FinancialToolMessageManifestEnvelope,
        )

        try:
            financial = FinancialToolMessageAuditEnvelope.model_validate(raw_envelope)
        except ValidationError:
            try:
                manifest = FinancialToolMessageManifestEnvelope.model_validate(
                    raw_envelope
                )
            except ValidationError:
                raise ordinary_error from None
            return _ToolExecutionEvidenceView(
                tool_call_id=manifest.tool_call_id,
                tool_name=manifest.tool_name,
                source_ref=manifest.request_ref,
                capability=manifest.capability,
                acquisition_outcomes=(),
                selected_artifact=manifest.selected_artifact,
            )
        available = tuple(
            outcome
            for outcome in financial.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        )
        selected_artifact = available[0].artifact if available else None
        if selected_artifact is not None and (
            selected_artifact.tool_name != financial.tool_name
            or selected_artifact.source_ref != financial.request_ref
        ):
            raise ValueError(
                "financial tool artifact contradicts its audit envelope"
            ) from None
        return _ToolExecutionEvidenceView(
            tool_call_id=financial.tool_call_id,
            tool_name=financial.tool_name,
            source_ref=financial.request_ref,
            capability=financial.capability,
            acquisition_outcomes=financial.acquisition_outcomes,
            selected_artifact=selected_artifact,
        )


def merge_material_claims(
    evidence: EvidenceState | Mapping[str, Any] | None,
    claims: Iterable[MaterialClaim],
) -> EvidenceState:
    """Merge analyst claims into checkpoint-safe shared evidence by claim ID."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {claim.claim_id: claim for claim in current.material_claims}
    for claim in claims:
        if any(note.note_id == claim.claim_id for note in current.evidence_notes):
            raise ValueError("an evidence note cannot be reclassified as a material claim")
        existing = merged.get(claim.claim_id)
        if existing is not None and existing != claim:
            raise ValueError(
                f"Material claim ID {claim.claim_id!r} was redefined; claim IDs are immutable."
            )
        merged[claim.claim_id] = claim
    return current.model_copy(update={"material_claims": tuple(merged.values())})


def merge_evidence_notes(
    evidence: EvidenceState,
    notes: Iterable[EvidenceNote],
) -> EvidenceState:
    """Preserve limitations without creating facts or replacing financial claims."""
    merged = {note.note_id: note for note in evidence.evidence_notes}
    claim_ids = {claim.claim_id for claim in evidence.material_claims}
    for note in notes:
        if note.note_id in claim_ids:
            raise ValueError("a material claim cannot be reclassified as an evidence note")
        previous = merged.get(note.note_id)
        if previous is not None and previous != note:
            raise ValueError("immutable evidence note ID was redefined")
        merged[note.note_id] = note
    return evidence.model_copy(update={"evidence_notes": tuple(merged.values())})


def merge_source_facts(
    evidence: EvidenceState | Mapping[str, Any] | None,
    facts: Iterable[SourceFact],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    canonical = _canonicalize_source_facts((*current.source_facts, *facts))
    return current.model_copy(update={"source_facts": canonical})


def merge_source_artifacts(
    evidence: EvidenceState | Mapping[str, Any] | None,
    artifacts: Iterable[SourceArtifact],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {
        (artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in current.source_artifacts
    }
    for artifact in artifacts:
        key = (artifact.tool_call_id, artifact.source_ref)
        existing = merged.get(key)
        if existing is not None and existing != artifact:
            raise ValueError(
                f"Source artifact {artifact.artifact_sha256!r} was redefined."
            )
        merged[key] = artifact
    return current.model_copy(update={"source_artifacts": tuple(merged.values())})


def merge_source_acquisition_outcomes(
    evidence: EvidenceState | Mapping[str, Any] | None,
    outcomes: Iterable[SourceAcquisitionOutcome],
) -> EvidenceState:
    """Merge attempt outcomes idempotently without turning failures into evidence."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )

    canonical = _canonicalize_source_acquisition_outcomes(
        (*current.acquisition_outcomes, *outcomes)
    )
    return current.model_copy(update={"acquisition_outcomes": canonical})


def merge_claim_validations(
    evidence: EvidenceState | Mapping[str, Any] | None,
    validations: Iterable[ClaimValidation],
) -> EvidenceState:
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {
        validation.claim_id: validation for validation in current.claim_validations
    }
    for validation in validations:
        existing = merged.get(validation.claim_id)
        if existing is not None and existing != validation:
            raise ValueError(
                f"Claim validation for {validation.claim_id!r} was redefined."
            )
        merged[validation.claim_id] = validation
    return current.model_copy(update={"claim_validations": tuple(merged.values())})


def merge_evidence_sources(
    evidence: EvidenceState | Mapping[str, Any] | None,
    sources: Iterable[EvidenceSource],
) -> EvidenceState:
    """Merge deterministic source statuses into shared evidence by source ID."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    merged = {source.source_id: source for source in current.sources}
    severity = {
        EvidenceStatus.NOT_APPLICABLE: -1,
        EvidenceStatus.AVAILABLE: 0,
        EvidenceStatus.UNAVAILABLE: 1,
        EvidenceStatus.CONFLICTED: 2,
    }
    for source in sources:
        existing = merged.get(source.source_id)
        if existing is None:
            merged[source.source_id] = source
            continue
        selected = (
            existing
            if severity[existing.status] >= severity[source.status]
            else source
        )
        merged[source.source_id] = selected.model_copy(
            update={"required": existing.required or source.required}
        )
    return current.model_copy(update={"sources": tuple(merged.values())})


def evidence_sources_from_tool_messages(
    messages: Iterable[Any],
    claims: Iterable[MaterialClaim],
    *,
    tool_call_ids_by_source: Mapping[str, Iterable[str]],
) -> tuple[EvidenceSource, ...]:
    """Compatibility wrapper returning source availability from paired results.

    Claim support is deliberately represented separately in
    ``EvidenceState.claim_validations`` so one bad claim cannot poison every
    other claim that cites the same available source.
    """
    return build_tool_evidence_state(
        messages,
        claims,
        tool_call_ids_by_source=tool_call_ids_by_source,
    ).sources


def build_tool_evidence_state(
    messages: Iterable[Any],
    claims: Iterable[MaterialClaim],
    *,
    tool_call_ids_by_source: Mapping[str, Iterable[str]],
    tool_calls_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    instrument_identity: InstrumentIdentityEvidence | None = None,
    notes: Iterable[EvidenceNote] = (),
    include_catalog_sources: bool = False,
) -> EvidenceState:
    """Materialize immutable source facts and per-claim validation results."""
    message_list = tuple(messages)
    claim_list = tuple(claims)
    note_list = tuple(notes)
    call_catalog = tool_calls_by_id or {}

    def calculation_ids_for_span(
        message: Any,
        content: str,
        span_start: int,
    ) -> tuple[str, ...]:
        if getattr(message, "name", None) != "get_indicators":
            return ()
        call = call_catalog.get(str(getattr(message, "tool_call_id", "")))
        if call is None:
            return ()
        args = call.get("args") or {}
        raw_indicators = args.get("indicator", "") if isinstance(args, Mapping) else ""
        indicators = tuple(
            indicator.strip().casefold()
            for indicator in str(raw_indicators).split(",")
            if indicator.strip()
        )
        section_starts = []
        for indicator in indicators:
            match = re.search(
                rf"(?mi)^##\s+{re.escape(indicator)}\s+values\b",
                content,
            )
            if match:
                section_starts.append((match.start(), indicator))
        section_starts.sort()
        for index, (section_start, indicator) in enumerate(section_starts):
            section_end = (
                section_starts[index + 1][0]
                if index + 1 < len(section_starts)
                else len(content)
            )
            if section_start <= span_start < section_end:
                return (indicator,)
        return ()
    tool_messages: dict[str, list[Any]] = {}
    for message in message_list:
        tool_call_id = getattr(message, "tool_call_id", None)
        if getattr(message, "name", None) and tool_call_id:
            tool_messages.setdefault(str(tool_call_id), []).append(message)

    claims_by_source: dict[str, list[MaterialClaim]] = {}
    for claim in claim_list:
        for source_ref in claim.source_refs:
            claims_by_source.setdefault(source_ref, []).append(claim)
    allowed_refs = frozenset(tool_call_ids_by_source)

    sources: list[EvidenceSource] = []
    acquisition_outcomes: list[SourceAcquisitionOutcome] = []
    available_messages_by_source: dict[str, tuple[Any, ...]] = {}
    validated_messages_by_source: dict[str, tuple[Any, ...]] = {}
    selected_artifacts_by_source_call: dict[tuple[str, str], SourceArtifact] = {}
    requested_refs = dict.fromkeys((
        *claims_by_source, *(note.source_ref for note in note_list),
        *(tool_call_ids_by_source if include_catalog_sources else ()),
    ))
    for source_ref in requested_refs:
        if source_ref not in allowed_refs:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="source ref is not in allowed catalog",
                )
            )
            continue
        source_kind = source_ref.partition(":")[0]
        allowed_tool_names = (
            frozenset({"get_verified_market_snapshot", "get_indicators"})
            if source_kind == "snapshot"
            else frozenset({source_kind})
        )

        def message_matches_source(
            message: Any,
            bound_tool_names: frozenset[str] = allowed_tool_names,
            bound_source_ref: str = source_ref,
        ) -> bool:
            message_tool_name = str(getattr(message, "name", ""))
            if message_tool_name in bound_tool_names:
                return True
            try:
                envelope = _validated_tool_execution_evidence(
                    getattr(message, "artifact", None)
                )
            except (TypeError, ValidationError, ValueError):
                return False
            return (
                envelope.source_ref == bound_source_ref
                and envelope.tool_call_id
                == str(getattr(message, "tool_call_id", ""))
                and envelope.tool_name == message_tool_name
            )

        matching_messages = tuple(
            message
            for tool_call_id in tool_call_ids_by_source[source_ref]
            for message in tool_messages.get(str(tool_call_id), ())
            if message_matches_source(message)
        )
        if not matching_messages:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="no matching tool result",
                )
            )
            continue
        envelopes: list[tuple[Any, ToolExecutionEvidenceEnvelope]] = []
        envelopes_by_call_id: dict[str, ToolExecutionEvidenceEnvelope] = {}
        envelope_error = False
        for message in sorted(
            matching_messages,
            key=lambda item: str(getattr(item, "tool_call_id", "")),
        ):
            try:
                envelope = _validated_tool_execution_evidence(
                    getattr(message, "artifact", None)
                )
            except (TypeError, ValidationError, ValueError):
                envelope_error = True
                break
            message_tool_call_id = str(getattr(message, "tool_call_id", ""))
            message_tool_name = str(getattr(message, "name", ""))
            expected_capability = (
                envelope.capability
                if source_ref.startswith(("acq.v1:", "financial-request:v1:"))
                else (
                    EvidenceCapability.MARKET_SNAPSHOT.value
                    if source_kind == "snapshot"
                    and message_tool_name == "get_verified_market_snapshot"
                    else (
                        message_tool_name if source_kind == "snapshot" else source_kind
                    )
                )
            )
            selected = envelope.selected_artifact
            previous = envelopes_by_call_id.get(message_tool_call_id)
            if (
                (previous is not None and previous != envelope)
                or envelope.tool_call_id != message_tool_call_id
                or envelope.tool_name != message_tool_name
                or envelope.source_ref != source_ref
                or envelope.capability != expected_capability
                or (
                    selected is not None
                    and selected.raw_text != str(getattr(message, "content", ""))
                )
            ):
                envelope_error = True
                break
            envelopes_by_call_id[message_tool_call_id] = envelope
            envelopes.append((message, envelope))
        if envelope_error or not envelopes:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="trusted acquisition metadata not exposed",
                )
            )
            continue
        attempt_outcomes = tuple(
            outcome
            for _, envelope in envelopes
            for outcome in envelope.acquisition_outcomes
        )
        acquisition_outcomes.extend(attempt_outcomes)
        validated_messages_by_source[source_ref] = tuple(
            message for message, _ in envelopes
        )
        available_messages = tuple(
            message
            for message, envelope in envelopes
            if envelope.selected_artifact is not None
        )
        if not available_messages:
            sources.append(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="tool returned unavailable",
                )
            )
            continue
        available_messages_by_source[source_ref] = available_messages
        selected_artifacts_by_source_call.update(
            {
                (source_ref, envelope.tool_call_id): envelope.selected_artifact
                for _, envelope in envelopes
                if envelope.selected_artifact is not None
            }
        )
        sources.append(
            EvidenceSource(
                source_id=source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=False,
            )
        )

    facts: list[SourceFact] = []
    artifacts: dict[tuple[str, str, str], SourceArtifact] = {}
    validations: list[ClaimValidation] = []
    enriched_claims: list[MaterialClaim] = []
    for claim in claim_list:
        claim_facts: list[SourceFact] = []
        unavailable_refs: list[str] = []
        numeric_diagnostics: list[str] = []
        ambiguous_quote = False
        quote = claim.source_quote.strip() or claim.statement.strip()
        indicator_observation = None
        indicator_projection_bound = False
        snapshot_row = None
        bound_symbol = None
        for source_ref in claim.source_refs:
            available_messages = available_messages_by_source.get(source_ref, ())
            if not available_messages:
                unavailable_refs.append(source_ref)
                continue
            for message in available_messages:
                content = str(getattr(message, "content", ""))
                if quote and content.count(quote) > 1:
                    ambiguous_quote = True
                    continue
                span_start = content.find(quote) if quote else -1
                if span_start < 0:
                    continue
                wrapper_tool_call_id = str(getattr(message, "tool_call_id", ""))
                artifact = selected_artifacts_by_source_call[
                    (source_ref, wrapper_tool_call_id)
                ]
                artifact_tool_call_id = artifact.tool_call_id
                artifact_digest = artifact.artifact_sha256
                artifact_key = (artifact_digest, artifact_tool_call_id, source_ref)
                artifacts[artifact_key] = artifact
                # This opt-in name interpretation requires one unambiguous
                # artifact. Its fact metadata is source-derived, independent of
                # claim wording/order when several claims share a fact ID.
                if len(available_messages) == 1:
                    bound_symbol = _bound_instrument_symbol(
                        message, call_catalog.get(wrapper_tool_call_id), instrument_identity)
                    if (getattr(message, "name", None) == "get_stock_data"
                            and _has_unbound_source_projection(source_ref, available_messages, message_list)):
                        bound_symbol = None
                    indicator_observation = _bound_indicator_observation(
                        message, content, quote, span_start, call_catalog.get(wrapper_tool_call_id))
                elif len(claim.source_refs) == 1:
                    bound_symbol = _bound_stock_projection_symbol(
                        message, available_messages, message_list, call_catalog, instrument_identity,
                        source_ref, quote, claim.statement)
                if len(claim.source_refs) == 1:
                    projected = _bound_indicator_projection(message, available_messages, message_list,
                        call_catalog, instrument_identity, source_ref, quote)
                    if projected is not None:
                        indicator_observation = projected
                        indicator_projection_bound = True
                # Different tools can project the same snapshot ID. Only one
                # verified-snapshot projection may establish its row context;
                # duplicate/conflicting projections are never chosen by value.
                snapshot_messages = tuple(m for m in available_messages
                    if getattr(m, "name", None) == "get_verified_market_snapshot")
                if len(snapshot_messages) == 1 and message is snapshot_messages[0]:
                    snapshot_row = _bound_snapshot_row(message, content, quote, span_start,
                        call_catalog.get(wrapper_tool_call_id), source_ref)
                    if snapshot_row is not None and snapshot_row.field in _MOVING_AVERAGE_NAMES:
                        indicator_observation = _BoundIndicatorObservation(
                            snapshot_row.field, snapshot_row.effective_date, snapshot_row.value)
                calculation_ids = ((indicator_observation.indicator,) if indicator_observation
                    else calculation_ids_for_span(message, content, span_start))
                financial_observation = None
                if bound_symbol and instrument_identity is not None and instrument_identity.currency == "CNY":
                    financial_observation = bind_current_statement_observation(
                        raw_text=content, quote=quote, source_ref=source_ref,
                        tool_call_id=artifact_tool_call_id, tool_name=artifact.tool_name,
                        artifact_sha256=artifact_digest, symbol=bound_symbol,
                    )
                fact_id = stable_source_fact_id(
                    source_ref=source_ref,
                    artifact_sha256=artifact_digest,
                    source_span_start=span_start,
                    source_span_end=span_start + len(quote),
                )
                claim_facts.append(
                    SourceFact(
                        fact_id=fact_id,
                        source_ref=source_ref,
                        tool_call_id=artifact_tool_call_id,
                        tool_name=artifact.tool_name,
                        artifact_sha256=artifact_digest,
                        raw_text=quote,
                        source_span_start=span_start,
                        source_span_end=span_start + len(quote),
                        normalized_numeric_tokens=_numeric_claim_tokens(quote),
                        calculation_ids=calculation_ids,
                        instrument_symbol=bound_symbol if financial_observation is not None else "",
                        financial_observation=financial_observation,
                    )
                )
                break

        unique_facts = {fact.fact_id: fact for fact in claim_facts}
        claim_fact_ids = tuple(unique_facts)
        comparison_recorder = ComparisonRecorder(claim.statement, quote, *claim.source_refs)
        comparison_digests = {fact.artifact_sha256 for fact in unique_facts.values()}
        comparison_digest = next(iter(comparison_digests)) if len(comparison_digests) == 1 else None
        for comparison_artifact in artifacts.values():
            if comparison_artifact.artifact_sha256 in comparison_digests:
                comparison_recorder.screen(comparison_artifact.raw_text)
        instrument_statement = _instrument_numeric_statement(claim.statement, bound_symbol)
        dated_statement, snapshot_metadata_error = _snapshot_dated_statement(
            instrument_statement, snapshot_row)
        numeric_statement, observation_mismatches, indicator_metadata_error = _moving_average_numeric_statement(
            dated_statement, indicator_observation, snapshot_context=snapshot_row is not None,
            indicator_context=indicator_projection_bound,
            comparison=comparison_recorder.observer("indicator_observation", comparison_digest))
        # A date-role exemption cannot remove the claimed observation itself or
        # leave only an indicator period. Check after all name normalization.
        if (snapshot_row is not None and dated_statement != instrument_statement
                and not any(_numeric_token_supported(token, (snapshot_row.value,),
                            comparison=comparison_recorder.observer("snapshot_value", comparison_digest))
                            for token in _numeric_claim_tokens(numeric_statement))):
            snapshot_metadata_error = "snapshot observation value is absent from the dated claim"
        indicator_metadata_error = indicator_metadata_error or snapshot_metadata_error
        if claim_fact_ids:
            unsupported_tokens = (*observation_mismatches, *unsupported_numeric_claim_tokens(
                (claim.model_copy(update={"statement": numeric_statement}),), (quote,),
                comparison=comparison_recorder.observer("tool_excerpt", comparison_digest)))
            numeric_diagnostics.extend(
                token for token in unsupported_tokens if token not in numeric_diagnostics
            )
        if not claim_fact_ids:
            if unavailable_refs and len(unavailable_refs) == len(claim.source_refs):
                detail = "cited source unavailable: " + ", ".join(unavailable_refs)
            elif ambiguous_quote:
                detail = "source quote is ambiguous within the cited tool result"
            else:
                detail = "source quote is absent from the cited tool result"
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail=detail,
                )
            )
        elif _source_quote_is_unavailable(quote):
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail="source quote reports unavailable evidence",
                    fact_ids=claim_fact_ids,
                )
            )
        elif _source_quote_is_metadata(quote):
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail="source quote contains provenance metadata, not a material fact",
                    fact_ids=claim_fact_ids,
                )
            )
        elif indicator_metadata_error:
            validations.append(
                ClaimValidation(claim_id=claim.claim_id, status=ClaimValidationStatus.UNSUPPORTED,
                    detail=indicator_metadata_error, fact_ids=claim_fact_ids)
            )
        elif numeric_diagnostics:
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.UNSUPPORTED,
                    detail=(
                        "source does not contain numeric claim(s): "
                        + ", ".join(numeric_diagnostics)
                    ),
                    fact_ids=claim_fact_ids,
                )
            )
        else:
            validations.append(
                ClaimValidation(
                    claim_id=claim.claim_id,
                    status=ClaimValidationStatus.SUPPORTED,
                    fact_ids=claim_fact_ids,
                )
            )
        validations[-1] = validations[-1].model_copy(update={
            "comparison_evidence": comparison_recorder.finish(unique_facts.values())
        })
        # Observe the exact local decision outside the canonical audit contract.
        from tradingagents.run_diagnostics import record_claim

        record_claim(claim=claim, validation=validations[-1], facts=tuple(unique_facts.values()),
                     sources=sources, numeric_statement=numeric_statement,
                     indicator=indicator_observation, bound_symbol=bound_symbol,
                     unsupported=tuple(numeric_diagnostics), indicator_error=indicator_metadata_error,
                     snapshot_row=snapshot_row)
        facts.extend(unique_facts.values())
        enriched_claims.append(claim.model_copy(update={"fact_ids": claim_fact_ids}))

    source_catalog = {source.source_id: source for source in sources}
    bound_notes = []
    for note in note_list:
        source = source_catalog.get(note.source_ref)
        matches = []
        for message in validated_messages_by_source.get(note.source_ref, ()):
            content = str(getattr(message, "content", ""))
            # Even unavailable results may document a limitation. They never
            # acquire a SourceArtifact or SourceFact through this diagnostic path.
            starts = tuple(match.start() for match in re.finditer(
                re.escape(note.source_quote), content,
            ))
            matches.extend((message, content, start) for start in starts)
        fields: dict[str, Any] = {
            "binding_status": "unverified",
            "tool_call_id": None,
            "source_content_sha256": None,
            "quote_start": None,
            "quote_end": None,
            "source_status": source.status if source else None,
            "source_required": source.required if source else None,
            "detail": "quote is not uniquely bound to a validated tool result",
        }
        if len(matches) == 1:
            message, content, start = matches[0]
            fields.update(
                binding_status="source_bound_quote",
                detail="exact tool quote; paraphrase semantics not established",
                tool_call_id=str(getattr(message, "tool_call_id", "")),
                source_content_sha256=sha256(content.encode("utf-8")).hexdigest(),
                quote_start=start, quote_end=start + len(note.source_quote),
            )
        bound_note = note.model_copy(update=fields)
        from tradingagents.run_diagnostics import record_note_binding

        record_note_binding(bound_note, validated_message_count=len(
            validated_messages_by_source.get(note.source_ref, ())), match_count=len(matches))
        bound_notes.append(bound_note)
    return EvidenceState(
        material_claims=tuple(enriched_claims),
        evidence_notes=tuple(bound_notes),
        source_facts=tuple({fact.fact_id: fact for fact in facts}.values()),
        source_artifacts=tuple(artifacts.values()),
        claim_validations=tuple(validations),
        sources=tuple(sources),
        acquisition_outcomes=tuple(acquisition_outcomes),
    )


def build_inline_evidence_state(
    source_text_by_ref: Mapping[str, str | None],
    claims: Iterable[MaterialClaim],
    *,
    source_artifact_by_ref: Mapping[str, SourceArtifact] | None = None,
    required_source_refs: Iterable[str] = (),
) -> EvidenceState:
    """Validate claims against pre-fetched source blocks kept in the prompt.

    ``source_artifact_by_ref`` binds a prompt-facing source ref to the exact
    acquired artifact that supplied its text, without minting alias provenance.
    """
    claim_list = tuple(claims)
    bound_artifacts = dict(source_artifact_by_ref or {})
    required_refs = frozenset(required_source_refs)
    normalized_sources: dict[str, str | None] = {}
    acquisition_outcomes: list[SourceAcquisitionOutcome] = []
    for source_ref, text in source_text_by_ref.items():
        bound_artifact = bound_artifacts.get(source_ref)
        if bound_artifact is not None:
            normalized_sources[source_ref] = bound_artifact.raw_text
            continue
        content = text if text is not None else ""
        outcome = make_source_acquisition_outcome(
            provider=source_ref.partition(".")[2] or "inline",
            capability=source_ref,
            attempt=1,
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            source_ref=source_ref,
            tool_call_id=f"inline:{source_ref}",
            tool_name="inline_source",
            content=content,
            status="success" if content.strip() else "unavailable",
        )
        acquisition_outcomes.append(outcome)
        normalized_sources[source_ref] = (
            outcome.artifact.raw_text
            if isinstance(outcome, SourceAcquisitionAvailable)
            else None
        )
    for source_ref, artifact in bound_artifacts.items():
        normalized_sources.setdefault(source_ref, artifact.raw_text)
    sources = tuple(
        EvidenceSource(
            source_id=source_ref,
            status=(
                EvidenceStatus.AVAILABLE
                if text is not None
                else EvidenceStatus.UNAVAILABLE
            ),
            required=source_ref in required_refs,
            detail="" if text is not None else "source returned unavailable",
        )
        for source_ref, text in normalized_sources.items()
    )
    facts: list[SourceFact] = []
    artifacts: dict[tuple[str, str, str], SourceArtifact] = {
        (artifact.artifact_sha256, artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in bound_artifacts.values()
    }
    validations: list[ClaimValidation] = []
    enriched_claims: list[MaterialClaim] = []
    for claim in claim_list:
        quote = claim.source_quote.strip() or claim.statement.strip()
        claim_facts: dict[str, SourceFact] = {}
        comparison_recorder = ComparisonRecorder(claim.statement, quote, *claim.source_refs)
        unavailable_refs: list[str] = []
        numeric_diagnostics: list[str] = []
        ambiguous_quote = False
        for source_ref in claim.source_refs:
            content = normalized_sources.get(source_ref)
            if content is None:
                unavailable_refs.append(source_ref)
                continue
            comparison_recorder.screen(content)
            numeric_diagnostics.extend(
                token
                for token in unsupported_numeric_claim_tokens((claim,), (content,),
                    comparison=comparison_recorder.observer("inline_source", sha256(content.encode()).hexdigest()))
                if token not in numeric_diagnostics
            )
            if quote and content.count(quote) > 1:
                ambiguous_quote = True
                continue
            span_start = content.find(quote) if quote else -1
            if span_start < 0:
                continue
            bound_artifact = bound_artifacts.get(source_ref)
            if bound_artifact is None:
                artifact_digest = sha256(content.encode()).hexdigest()
                artifact_source_ref = source_ref
                tool_call_id = f"inline:{source_ref}"
                tool_name = "inline_source"
                artifact_key = (artifact_digest, tool_call_id, artifact_source_ref)
                artifacts[artifact_key] = SourceArtifact(
                    artifact_sha256=artifact_digest,
                    source_ref=artifact_source_ref,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    raw_text=content,
                )
            else:
                artifact_digest = bound_artifact.artifact_sha256
                artifact_source_ref = bound_artifact.source_ref
                tool_call_id = bound_artifact.tool_call_id
                tool_name = bound_artifact.tool_name
            fact = SourceFact(
                fact_id=stable_source_fact_id(
                    source_ref=artifact_source_ref,
                    artifact_sha256=artifact_digest,
                    source_span_start=span_start,
                    source_span_end=span_start + len(quote),
                ),
                source_ref=artifact_source_ref,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                artifact_sha256=artifact_digest,
                raw_text=quote,
                source_span_start=span_start,
                source_span_end=span_start + len(quote),
                normalized_numeric_tokens=_numeric_claim_tokens(quote),
            )
            claim_facts[fact.fact_id] = fact

        claim_fact_ids = tuple(claim_facts)
        if not claim.source_refs:
            detail = "claim cites no source refs"
            status = ClaimValidationStatus.UNSUPPORTED
        elif not claim_fact_ids:
            status = ClaimValidationStatus.UNSUPPORTED
            detail = (
                "cited source unavailable: " + ", ".join(unavailable_refs)
                if unavailable_refs and len(unavailable_refs) == len(claim.source_refs)
                else (
                    "source quote is ambiguous within the cited source block"
                    if ambiguous_quote
                    else "source quote is absent from the cited source block"
                )
            )
        elif _source_quote_is_unavailable(quote):
            status = ClaimValidationStatus.UNSUPPORTED
            detail = "source quote reports unavailable evidence"
        elif _source_quote_is_metadata(quote):
            status = ClaimValidationStatus.UNSUPPORTED
            detail = "source quote contains provenance metadata, not a material fact"
        elif numeric_diagnostics:
            status = ClaimValidationStatus.UNSUPPORTED
            detail = (
                "source does not contain numeric claim(s): "
                + ", ".join(numeric_diagnostics)
            )
        else:
            status = ClaimValidationStatus.SUPPORTED
            detail = ""
        validations.append(
            ClaimValidation(
                claim_id=claim.claim_id,
                status=status,
                detail=detail,
                fact_ids=claim_fact_ids,
                comparison_evidence=comparison_recorder.finish(claim_facts.values()),
            )
        )
        facts.extend(claim_facts.values())
        enriched_claims.append(claim.model_copy(update={"fact_ids": claim_fact_ids}))

    known_source_ids = set(normalized_sources)
    unknown_source_ids = tuple(
        dict.fromkeys(
            source_ref
            for claim in claim_list
            for source_ref in claim.source_refs
            if source_ref not in known_source_ids
        )
    )
    return EvidenceState(
        material_claims=tuple(enriched_claims),
        source_facts=tuple({fact.fact_id: fact for fact in facts}.values()),
        source_artifacts=tuple(artifacts.values()),
        claim_validations=tuple(validations),
        sources=(
            *sources,
            *(
                EvidenceSource(
                    source_id=source_ref,
                    status=EvidenceStatus.UNAVAILABLE,
                    required=False,
                    detail="source ref is not in allowed catalog",
                )
                for source_ref in unknown_source_ids
            ),
        ),
        acquisition_outcomes=tuple(acquisition_outcomes),
    )


_CALENDAR_DATE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|"
    r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)(?![0-9]|\.[0-9])"
)


# Recognize complete colon clock tokens, optionally attached to an ISO date.
# Do not infer compact/partial dates, local-language times, units or UTC instants.
_CLOCK_TOKEN_PATTERN = (
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)
_TIMESTAMP_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})[T ]"
    r"(?P<clock>" + _CLOCK_TOKEN_PATTERN + r")(?![A-Za-z0-9_:+-]|\.[0-9])"
)
_CLOCK_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.:+-])" + _CLOCK_TOKEN_PATTERN
    + r"(?![A-Za-z0-9_:+-]|\.[0-9])"
)


def _clock_identity(raw: str) -> str | None:
    # Validate ranges with datetime, but retain all fractional digits: Python's
    # microsecond conversion must not equate distinct higher-precision times.
    value = raw.removesuffix("Z") + ("+00:00" if raw.endswith("Z") else "")
    try:
        datetime.fromisoformat("2000-01-01T" + value)
        zone = re.search(r"[+-]([0-9]{2}):([0-9]{2})$", value)
        if zone and (int(zone[1]) > 23 or int(zone[2]) > 59):
            return None
    except ValueError:
        return None
    return re.sub(r"\.([0-9]+)", lambda m: ("." + m[1].rstrip("0")).rstrip("."), value)


def _numeric_claim_roles(text: str):
    """Separate explicit calendar/clock identities from amounts without units."""
    normalized = _normalize_numeric_text(text)
    temporal = []

    def timestamp_token(match):
        raw, day, clock = match.group(), match["day"], match["clock"]
        value = _clock_identity(clock)
        try:
            day = date.fromisoformat(day).isoformat()
        except ValueError:
            value = None
        if value is None:
            temporal.append((raw, None))
        else:
            # A full timestamp needs its own identity: never assemble a claimed
            # date/time from two different source timestamps. Date-only/clock-
            # only statements may still cite those explicit components.
            temporal.extend(((raw, "datetime:" + day + "T" + value),
                             (day, day), (clock, "time:" + value)))
        return " " * len(raw)

    def clock_token(match):
        raw = match.group()
        value = _clock_identity(raw)
        temporal.append((raw, "time:" + value if value is not None else None))
        return " " * len(raw)

    def calendar_token(match):
        raw = match.group()
        parts = raw.replace("年", "-").replace("月", "-").removesuffix("日").split("-")
        try:
            value = date(*map(int, parts)).isoformat()
        except ValueError:
            value = None
        temporal.append((raw, value))
        return " " * len(raw)

    numeric_text = _TIMESTAMP_TOKEN.sub(timestamp_token, normalized)
    numeric_text = _CLOCK_TOKEN.sub(clock_token, numeric_text)
    numeric_text = _CALENDAR_DATE_TOKEN.sub(calendar_token, numeric_text)
    return _numeric_claim_tokens(numeric_text), tuple(temporal)


def unsupported_numeric_claim_tokens(
    claims: Iterable[MaterialClaim],
    source_texts: Iterable[str],
    *, comparison=None,
) -> tuple[str, ...]:
    """Match amounts to non-temporal numbers; explicit times/dates by identity."""
    source_roles = tuple(_numeric_claim_roles(text) for text in source_texts)
    source_tokens = tuple(token for tokens, _ in source_roles for token in tokens)
    source_dates = {value for _, dates in source_roles for _, value in dates if value is not None}
    unsupported = []
    for claim in claims:
        claim_tokens, claim_dates = _numeric_claim_roles(claim.statement)
        # Temporal identity is an exact structural check, not a numerical
        # tolerance comparison. Do not manufacture numeric execution evidence.
        for raw, value in claim_dates:
            if value is None or value not in source_dates:
                unsupported.extend(token for token in _numeric_claim_tokens(raw) if token not in unsupported)
        unsupported.extend(
            token
            for token in claim_tokens
            if token not in unsupported
            and not _numeric_token_supported(token, source_tokens, comparison=comparison)
        )
    return tuple(unsupported)


def _normalize_material_text(text: str) -> str:
    tokens = re.findall(r"\w+(?:\.\w+)*|[%+-]", text.casefold())
    return " ".join(tokens)


def unsupported_material_claim_ids(
    claims: Iterable[MaterialClaim],
    source_texts: Iterable[str],
) -> tuple[str, ...]:
    """Return claims whose source-expressed statement is absent from all sources."""
    normalized_sources = tuple(
        _normalize_material_text(source_text) for source_text in source_texts
    )
    unsupported = []
    for claim in claims:
        source_expression = _normalize_material_text(
            claim.source_quote or claim.statement
        )
        if not source_expression or not any(
            source_expression in source_text for source_text in normalized_sources
        ):
            unsupported.append(claim.claim_id)
    return tuple(unsupported)


def _claim_provenance_failure(
    claim: MaterialClaim,
    evidence: EvidenceState,
) -> str | None:
    validations = {
        validation.claim_id: validation for validation in evidence.claim_validations
    }
    validation = validations.get(claim.claim_id)
    if validation is None:
        return "claim has no deterministic validation record"
    if validation.status is not ClaimValidationStatus.SUPPORTED:
        return validation.detail or "claim validation is unsupported"
    if not claim.fact_ids or set(validation.fact_ids) != set(claim.fact_ids):
        return "claim validation is not bound to immutable Source Facts"
    facts = {fact.fact_id: fact for fact in evidence.source_facts}
    artifacts = {
        (artifact.artifact_sha256, artifact.tool_call_id, artifact.source_ref): artifact
        for artifact in evidence.source_artifacts
    }
    bound_facts = tuple(facts.get(fact_id) for fact_id in claim.fact_ids)
    if any(fact is None for fact in bound_facts):
        return "claim cites an unregistered Source Fact"
    calculation_ids: list[str] = []
    for fact in bound_facts:
        assert fact is not None
        if (
            fact.source_ref not in claim.source_refs
            or fact.raw_text != claim.source_quote
            or not fact.tool_call_id.strip()
            or not fact.tool_name.strip()
            or len(fact.artifact_sha256) != 64
            or fact.source_span_end - fact.source_span_start != len(fact.raw_text)
        ):
            return "claim Source Fact lacks an exact call, artifact, source, or span binding"
        artifact = artifacts.get(
            (fact.artifact_sha256, fact.tool_call_id, fact.source_ref)
        )
        if (
            artifact is None
            or artifact.tool_name != fact.tool_name
            or sha256(artifact.raw_text.encode()).hexdigest()
            != artifact.artifact_sha256
            or fact.source_span_end > len(artifact.raw_text)
            or artifact.raw_text[fact.source_span_start : fact.source_span_end]
            != fact.raw_text
        ):
            return (
                "claim Source Fact is not verifiable against its immutable "
                "Source Artifact"
            )
        calculation_ids.extend(fact.calculation_ids)
        if fact.fact_kind == "excerpt" and fact.tool_name in FINANCIAL_TOOLS:
            return "financial excerpt has no canonical semantic qualification; source-row observations are not decision premises"
    if calculation_ids:
        from tradingagents.dataflows.market_snapshot import (
            _minimum_history_for_indicator,
        )

        required_rows = max(
            _minimum_history_for_indicator(calculation_id)
            for calculation_id in calculation_ids
        )
        if claim.minimum_history_rows < required_rows:
            return (
                "claim understates calculation history: "
                f"{claim.minimum_history_rows} declared; {required_rows} required"
            )
    return None


def verified_financial_observations(
    evidence: EvidenceState | Mapping[str, Any], *, claim_ids: Iterable[str] | None = None,
) -> tuple[FinancialObservation, ...]:
    """Rederive display-only observations, including after checkpoint loading."""
    current = evidence if isinstance(evidence, EvidenceState) else EvidenceState.model_validate(evidence)
    identity = current.instrument_identity
    if identity is None or not identity.is_authoritative or identity.currency != "CNY":
        return ()
    if (identity.symbol.endswith(".SS") and identity.venue != "XSHG") or (
        identity.symbol.endswith(".SZ") and identity.venue != "XSHE"
    ):
        return ()
    selected = None if claim_ids is None else frozenset(claim_ids)
    supported = {v.claim_id: v for v in current.claim_validations if v.status is ClaimValidationStatus.SUPPORTED}
    claims = tuple(claim for claim in current.material_claims
                   if (selected is None or claim.claim_id in selected)
                   and claim.claim_id in supported
                   and set(claim.fact_ids) == set(supported[claim.claim_id].fact_ids))
    sources = {source.source_id: source for source in current.sources}
    artifacts = {(a.artifact_sha256, a.tool_call_id, a.source_ref): a for a in current.source_artifacts}
    verified = {}
    for fact in current.source_facts:
        if (fact.fact_kind != "excerpt"
                or fact.financial_observation is None or fact.instrument_symbol != identity.symbol):
            continue
        if not any(fact.fact_id in claim.fact_ids and fact.source_ref in claim.source_refs
                   and fact.raw_text == claim.source_quote for claim in claims):
            continue
        source = sources.get(fact.source_ref)
        if source is None or source.status is not EvidenceStatus.AVAILABLE:
            continue
        if fact.fact_id != stable_source_fact_id(
            source_ref=fact.source_ref, artifact_sha256=fact.artifact_sha256,
            source_span_start=fact.source_span_start, source_span_end=fact.source_span_end,
        ):
            continue
        artifact = artifacts.get((fact.artifact_sha256, fact.tool_call_id, fact.source_ref))
        if (artifact is None or artifact.tool_name != fact.tool_name
                or artifact.raw_text[fact.source_span_start:fact.source_span_end] != fact.raw_text):
            continue
        expected = bind_current_statement_observation(
            raw_text=artifact.raw_text, quote=fact.raw_text, source_ref=fact.source_ref,
            tool_call_id=fact.tool_call_id, tool_name=fact.tool_name,
            artifact_sha256=fact.artifact_sha256, symbol=identity.symbol,
        )
        if expected is not None and expected == fact.financial_observation:
            verified[expected.observation_id] = expected
    return tuple(verified.values())


def decision_ready_material_claims(
    evidence: EvidenceState | Mapping[str, Any],
) -> tuple[MaterialClaim, ...]:
    """Return claims whose individual validation and cited sources permit use."""
    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence)
    )
    sources = {source.source_id: source for source in current.sources}

    def source_is_ready(source_ref: str) -> bool:
        snapshot = current.market_snapshot
        if (
            snapshot is not None
            and snapshot.snapshot_id
            and source_ref.startswith("snapshot:")
            and source_ref != snapshot.snapshot_id
        ):
            return False
        source = sources.get(source_ref)
        return source is not None and source.status is EvidenceStatus.AVAILABLE

    return tuple(
        claim
        for claim in current.material_claims
        if _claim_provenance_failure(claim, current) is None
        and all(source_is_ready(source_ref) for source_ref in claim.source_refs)
    )


class AdmissionGateResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    admitted: bool
    readiness: EvidenceReadiness
    diagnostics: tuple[str, ...] = ()
    diagnostic_codes: tuple[AnalysisDiagnosticCode, ...] = ()

    @field_validator("diagnostic_codes")
    @classmethod
    def _canonicalize_diagnostic_codes(
        cls,
        value: tuple[AnalysisDiagnosticCode, ...],
    ) -> tuple[AnalysisDiagnosticCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @model_validator(mode="after")
    def _admitted_gate_has_only_advisory_diagnostics(self) -> AdmissionGateResult:
        if self.admitted and any(
            code is not AnalysisDiagnosticCode.OPTIONAL_EVIDENCE_UNAVAILABLE
            for code in self.diagnostic_codes
        ):
            raise ValueError("admitted gate cannot carry blocker diagnostic codes")
        return self


class EvidencePreflightResult(BaseModel):
    """Baseline evidence validation before any model-mediated analysis."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = EVIDENCE_CONTRACT_VERSION
    passed: bool
    readiness: EvidenceReadiness
    blockers: tuple[str, ...] = ()
    diagnostic_codes: tuple[AnalysisDiagnosticCode, ...] = ()

    @field_validator("diagnostic_codes")
    @classmethod
    def _canonicalize_diagnostic_codes(
        cls,
        value: tuple[AnalysisDiagnosticCode, ...],
    ) -> tuple[AnalysisDiagnosticCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @model_validator(mode="after")
    def _passed_gate_has_no_blockers(self) -> EvidencePreflightResult:
        if self.passed and (self.blockers or self.diagnostic_codes):
            raise ValueError("passed preflight cannot carry blocker diagnostics")
        return self


class DraftThesis(BaseModel):
    """Structured directional thesis evaluated before a decision is rendered."""

    model_config = ConfigDict(frozen=True)

    rating: str
    narrative: str
    material_claim_ids: tuple[str, ...] = ()


class DecisionGateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    permitted: bool
    readiness: EvidenceReadiness
    evidence_coverage: float = Field(ge=0.0, le=1.0)
    confidence: DecisionConfidence
    diagnostics: tuple[str, ...] = ()
    revision_applied: bool = False


def _evidence_coverage(evidence: EvidenceState) -> float:
    snapshot = evidence.market_snapshot
    authoritative_snapshot_ids = (
        {
            snapshot.snapshot_id,
            f"snapshot:{snapshot.symbol}:{snapshot.effective_trading_date}",
        }
        if snapshot is not None
        else set()
    )
    authoritative_snapshot_ids.discard("")
    applicable_sources = tuple(
        source
        for source in evidence.sources
        if source.status is not EvidenceStatus.NOT_APPLICABLE
        and not (
            any(
                source.source_id == snapshot_id
                or source.source_id.startswith(f"{snapshot_id}:")
                for snapshot_id in authoritative_snapshot_ids
            )
        )
    )
    total_evidence = 2 + len(applicable_sources)
    available_evidence = int(evidence.instrument_identity is not None) + int(
        evidence.market_snapshot is not None
    )
    available_evidence += sum(
        source.status is EvidenceStatus.AVAILABLE for source in applicable_sources
    )
    return available_evidence / total_evidence


def evaluate_decision_gate(
    draft: DraftThesis,
    evidence: EvidenceState,
    *,
    original_draft: DraftThesis | None = None,
) -> DecisionGateResult:
    """Validate the material premises used by a directional draft thesis."""
    evidence_coverage = _evidence_coverage(evidence)
    if original_draft is not None and draft.rating != original_draft.rating:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=(
                "Constrained revision cannot change rating from "
                f"{original_draft.rating} to {draft.rating}.",
            ),
            revision_applied=True,
        )
    if original_draft is not None:
        original_claim_ids = set(original_draft.material_claim_ids)
        added_claim_ids = tuple(
            claim_id
            for claim_id in draft.material_claim_ids
            if claim_id not in original_claim_ids
        )
        if added_claim_ids:
            return DecisionGateResult(
                permitted=False,
                readiness=EvidenceReadiness.INSUFFICIENT,
                evidence_coverage=evidence_coverage,
                confidence=DecisionConfidence.LOW,
                diagnostics=tuple(
                    f"Constrained revision cannot add material claim: {claim_id}."
                    for claim_id in added_claim_ids
                ),
                revision_applied=True,
            )
    if not draft.material_claim_ids:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=("Draft thesis cites no material claims.",),
            revision_applied=original_draft is not None,
        )
    claims_by_id = {claim.claim_id: claim for claim in evidence.material_claims}
    missing_claim_ids = tuple(
        claim_id
        for claim_id in draft.material_claim_ids
        if claim_id not in claims_by_id
    )
    if missing_claim_ids:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise missing from shared evidence: {claim_id}."
                for claim_id in missing_claim_ids
            ),
        )
    unsupported_claims = tuple(
        (claims_by_id[claim_id], detail)
        for claim_id in draft.material_claim_ids
        for detail in (_claim_provenance_failure(claims_by_id[claim_id], evidence),)
        if detail is not None
    )
    if unsupported_claims:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} is unsupported"
                f" ({detail})."
                for claim, detail in unsupported_claims
            ),
            revision_applied=original_draft is not None,
        )
    sources_by_id = {source.source_id: source for source in evidence.sources}
    authoritative_snapshot_id = (
        evidence.market_snapshot.snapshot_id if evidence.market_snapshot else ""
    )
    snapshot_mismatches = tuple(
        (claim, source_ref)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        if authoritative_snapshot_id
        and source_ref.startswith("snapshot:")
        and source_ref != authoritative_snapshot_id
    )
    if snapshot_mismatches:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} cites snapshot {source_ref}, not "
                f"the Authoritative Market Snapshot {authoritative_snapshot_id}."
                for claim, source_ref in snapshot_mismatches
            ),
            revision_applied=original_draft is not None,
        )
    conflicted_sources = tuple(
        (claim, source)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        for source in (sources_by_id.get(source_ref),)
        if source is not None and source.status is EvidenceStatus.CONFLICTED
    )
    if conflicted_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} uses conflicted source "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''}."
                for claim, source in conflicted_sources
            ),
        )
    unavailable_sources = tuple(
        (claim, source)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        for source in (sources_by_id.get(source_ref),)
        if source is not None and source.status is EvidenceStatus.UNAVAILABLE
    )
    if unavailable_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} uses unavailable source "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''}."
                for claim, source in unavailable_sources
            ),
        )
    unregistered_sources = tuple(
        (claim, source_ref)
        for claim_id in draft.material_claim_ids
        for claim in (claims_by_id[claim_id],)
        for source_ref in claim.source_refs
        if source_ref not in sources_by_id
        and not (
            evidence.market_snapshot is not None
            and source_ref
            == (
                f"snapshot:{evidence.market_snapshot.symbol}:"
                f"{evidence.market_snapshot.effective_trading_date}"
            )
        )
    )
    if unregistered_sources:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft premise {claim.claim_id} cites unregistered source "
                f"{source_ref}."
                for claim, source_ref in unregistered_sources
            ),
        )
    cited_roles = tuple(
        _numeric_claim_roles(source_text)
        for claim_id in draft.material_claim_ids
        for source_text in (
            claims_by_id[claim_id].statement,
            claims_by_id[claim_id].source_quote,
        )
    )
    supported_numeric_claims = {token for tokens, _ in cited_roles for token in tokens}
    supported_dates = {value for _, dates in cited_roles for _, value in dates if value is not None}
    numeric_narrative = draft.narrative
    for claim_id in draft.material_claim_ids:
        # The closed-ledger renderer annotates each premise as ``[claim_id]``.
        # Digits inside that immutable identifier are provenance, not a factual
        # assertion in the thesis, and must not be compared with source values.
        numeric_narrative = numeric_narrative.replace(f"[{claim_id}]", "")
    narrative_tokens, narrative_dates = _numeric_claim_roles(numeric_narrative)
    unsupported_dates = tuple(
        token for raw, value in narrative_dates
        if value is None or value not in supported_dates
        for token in _numeric_claim_tokens(raw)
    )
    unsupported_numeric_claims = unsupported_dates + tuple(
        token
        for token in narrative_tokens
        if not _numeric_token_supported(token, supported_numeric_claims)
    )
    if unsupported_numeric_claims:
        return DecisionGateResult(
            permitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            evidence_coverage=evidence_coverage,
            confidence=DecisionConfidence.LOW,
            diagnostics=tuple(
                f"Draft narrative contains unsupported numeric claim: {token}."
                for token in unsupported_numeric_claims
            ),
        )
    readiness = (
        EvidenceReadiness.DEGRADED
        if any(
            not source.required and source.status is EvidenceStatus.UNAVAILABLE
            for source in evidence.sources
        )
        else EvidenceReadiness.DECISION_READY
    )
    confidence = (
        DecisionConfidence.HIGH
        if readiness is EvidenceReadiness.DECISION_READY
        else (
            DecisionConfidence.MEDIUM
            if evidence_coverage >= 0.5
            else DecisionConfidence.LOW
        )
    )
    return DecisionGateResult(
        permitted=True,
        readiness=readiness,
        evidence_coverage=evidence_coverage,
        confidence=confidence,
        revision_applied=original_draft is not None,
    )


def project_history_store_access(
    status: str, diagnostic: str | None,
) -> tuple[str, str | None]:
    """Project trusted runtime access into v1 without promoting PIT eligibility.

    ``stored`` describes access only. Preserve the finer runtime state and its
    original diagnostic; never turn CURRENT reuse into qualified strict history.
    Unknown states pass through so the closed evidence model rejects them.
    """
    if status not in {"refreshed", "current_only_refreshed", "current_only_cached"}:
        return status, diagnostic
    markers = [f"runtime_history_store_status={status}"]
    if status.startswith("current_only_"):
        markers.append("strict_history_qualified=false")
    if diagnostic:
        markers.append(diagnostic)
    return "stored", ";".join(markers)


def build_evidence_state(
    *,
    symbol: str,
    identity: Mapping[str, Any],
    snapshot: Any | None,
) -> EvidenceState:
    """Convert acquired identity and snapshot data into checkpoint-safe evidence."""
    name = identity.get("company_name") or identity.get("name")
    raw_kind = str(
        identity.get("instrument_kind") or identity.get("quote_type") or "unknown"
    ).strip().casefold()
    kind_aliases = {
        "stock": InstrumentKind.EQUITY,
        "equity": InstrumentKind.EQUITY,
        "etf": InstrumentKind.FUND,
        "fund": InstrumentKind.FUND,
        "mutualfund": InstrumentKind.FUND,
        "index": InstrumentKind.INDEX,
        "bond": InstrumentKind.BOND,
        "cryptocurrency": InstrumentKind.CRYPTO,
        "crypto": InstrumentKind.CRYPTO,
    }
    provenance_value = identity.get("provenance")
    if isinstance(provenance_value, Mapping):
        canonical_provenance = dict(provenance_value)
        provider = canonical_provenance.get("provider")
        source_ref = canonical_provenance.get("source_ref")
        artifact_sha256 = canonical_provenance.get("artifact_sha256")
        if all(
            isinstance(value, str)
            for value in (provider, source_ref, artifact_sha256)
        ):
            canonical_provenance["source_ref"] = (
                checkpoint_safe_identity_source_ref(
                    provider=provider,
                    source_ref=source_ref,
                    artifact_sha256=artifact_sha256,
                )
            )
        provenance_value = canonical_provenance
    instrument_identity = None
    if identity:
        instrument_identity = InstrumentIdentityEvidence(
            symbol=str(identity.get("canonical_symbol") or symbol),
            venue=str(identity.get("venue") or identity.get("exchange") or ""),
            instrument_kind=kind_aliases.get(raw_kind, InstrumentKind.UNKNOWN),
            currency=str(identity.get("currency") or ""),
            provenance=provenance_value,
            display_name=str(name) if name else None,
        )
    market_snapshot = None
    if snapshot is not None:
        history_access, history_diagnostic = project_history_store_access(
            getattr(snapshot, "history_store_status", "live"),
            getattr(snapshot, "history_store_diagnostic", None),
        )
        status_provenance = getattr(snapshot, "current_status_provenance", None)
        canonical_status_provenance = (
            TradingStatusProvenanceEvidence(
                provider=status_provenance.provider,
                provider_dataset_id=status_provenance.provider_dataset_id,
                session_date=status_provenance.session_date.isoformat(),
                status=status_provenance.status.value,
                observed_at=status_provenance.observed_at.isoformat(),
                revision_id=status_provenance.revision_id,
            )
            if status_provenance is not None
            else None
        )
        market_snapshot = MarketSnapshotEvidence(
            symbol=snapshot.symbol,
            provider=snapshot.provider,
            retrieved_at=snapshot.retrieved_at,
            adjustment_basis=snapshot.adjustment_basis,
            requested_date=snapshot.requested_date,
            effective_trading_date=snapshot.effective_trading_date,
            history_rows=len(snapshot.frame),
            frame_sha256=snapshot.frame_sha256,
            snapshot_id=snapshot.snapshot_id,
            snapshot_id_version=getattr(snapshot, "snapshot_id_version", "v1"),
            pin_membership_digest=getattr(
                snapshot,
                "pin_membership_digest",
                None,
            ),
            snapshot_manifest_json=getattr(
                snapshot,
                "snapshot_manifest_json",
                None,
            ),
            current_tradeability=getattr(snapshot, "current_tradeability", "unknown"),
            current_status_provenance=canonical_status_provenance,
            latest_traded_close=getattr(snapshot, "latest_traded_close", None),
            latest_traded_close_diagnostic=getattr(
                snapshot,
                "latest_traded_close_diagnostic",
                None,
            ),
            carried_suspension_close=getattr(
                snapshot,
                "carried_suspension_close",
                None,
            ),
            history_gap_dates=getattr(snapshot, "history_gap_dates", ()),
            history_store_status=history_access,
            history_store_diagnostic=history_diagnostic,
            physical_attempt_events=tuple(
                ProviderPhysicalAttemptEvidence(
                    attempt_event_id=event.attempt_event_id,
                    sequence_id=event.sequence_id,
                    request_key=event.request_key,
                    upstream_service_id=event.upstream_service_id,
                    upstream_service_name=event.upstream_service_name,
                    capacity_scope=getattr(event, "capacity_scope", "all"),
                    attempt_index=event.attempt_index,
                    attempted_at=event.attempted_at.isoformat(),
                    pacing_event=event.pacing_event,
                    pacing_wait_seconds=event.pacing_wait_seconds,
                    outcome=event.outcome.value,
                    retryable=event.retryable,
                    status_code=event.status_code,
                    error_code=event.error_code,
                    retry_after_seconds=event.retry_after_seconds,
                    cooldown_changed=event.cooldown_changed,
                    cooldown_until=(
                        event.cooldown_until.isoformat()
                        if event.cooldown_until is not None
                        else None
                    ),
                    final_physical_attempt_count=event.final_physical_attempt_count,
                )
                for event in getattr(snapshot, "physical_attempt_events", ())
            ),
            physical_attempt_count=len(
                getattr(snapshot, "physical_attempt_events", ())
            ),
        )
    return EvidenceState(
        instrument_identity=instrument_identity,
        market_snapshot=market_snapshot,
    )


def acquire_run_evidence(
    symbol: str,
    requested_date: str,
    *,
    decision_policy: DecisionPolicyEngine | None = None,
    decision_horizon: DecisionHorizon | None = None,
    asset_configuration: RunAssetConfiguration | None = None,
) -> EvidenceState:
    """Acquire run identity and the validated five-year market snapshot."""
    if asset_configuration is None:
        from tradingagents.dataflows.crypto_universe import is_crypto_pair_syntax

        if is_crypto_pair_syntax(symbol):
            from tradingagents.asset_configuration import (
                RunAssetConfigurationError,
                resolve_run_asset_configuration,
            )
            from tradingagents.dataflows.config import get_config

            try:
                asset_configuration = resolve_run_asset_configuration(
                    symbol,
                    config=get_config(),
                )
            except RunAssetConfigurationError:
                # Preserve the existing typed registry-unavailable EvidenceState
                # projection below for unsupported or invalid registry inputs.
                asset_configuration = None
    from tradingagents.dataflows.errors import NoMarketDataError
    from tradingagents.dataflows.instrument_identity import (
        IdentityRegistryAvailable,
        IdentityRegistryUnavailable,
        RegistryFailureReason,
        resolve_authoritative_instrument_identity,
    )
    from tradingagents.dataflows.market_snapshot import (
        get_active_market_snapshot_acquisition_record,
        get_active_run_telemetry,
        get_authoritative_market_snapshot,
    )
    from tradingagents.strategy_registry import (
        CRYPTO_DECISION_HORIZON,
        DEFAULT_DECISION_HORIZON,
        MARKET_RETURN_OBSERVATIONS,
        InsufficientStrategyHistoryError,
        build_market_return_fact,
        create_production_decision_policy,
        market_return_calculation_id,
    )

    requested = datetime.strptime(requested_date, "%Y-%m-%d")
    start_date = (requested - relativedelta(years=5)).strftime("%Y-%m-%d")
    using_default_policy = decision_policy is None
    if using_default_policy:
        decision_policy = create_production_decision_policy()
    identity: dict[str, Any] = {}
    resolved_instrument_kind = InstrumentKind.UNKNOWN
    acquired_at = datetime.now(timezone.utc).isoformat()
    market_artifact: SourceArtifact | None = None

    if asset_configuration is not None:
        configured_identity = asset_configuration.instrument_identity
        if asset_configuration.instrument_kind is InstrumentKind.CRYPTO:
            from tradingagents.dataflows.crypto_universe import (
                canonical_supported_crypto_symbol,
            )

            requested_identity = canonical_supported_crypto_symbol(symbol)
        else:
            requested_identity = symbol.strip().upper()
        if requested_identity != configured_identity.symbol:
            raise ValueError("run asset identity does not match requested instrument")
        if (
            decision_horizon is not None
            and decision_horizon != asset_configuration.horizon
        ):
            raise ValueError("run asset horizon does not match evidence configuration")
        decision_horizon = asset_configuration.horizon
        resolved_instrument_kind = asset_configuration.instrument_kind
        identity = {
            "canonical_symbol": configured_identity.symbol,
            "venue": configured_identity.venue,
            "exchange": configured_identity.venue,
            "instrument_kind": configured_identity.instrument_kind.value,
            "currency": configured_identity.currency,
            "provenance": configured_identity.provenance,
            "company_name": configured_identity.display_name,
        }
        registry_available = True
        registry_sha256 = asset_configuration.registry_digest
        registry_source_ref = asset_configuration.registry_source_ref
        registry_raw_artifact = asset_configuration.registry_artifact
    else:
        registry_result = resolve_authoritative_instrument_identity(symbol)
        registry_available = isinstance(registry_result, IdentityRegistryAvailable)
        if registry_available:
            registry_sha256 = registry_result.registry_sha256
            registry_source_ref = registry_result.registry_source_ref
            registry_raw_artifact = registry_result.raw_artifact

    if registry_available:
        if asset_configuration is None:
            canonical_symbol = registry_result.identity.canonical_symbol
            resolved_instrument_kind = InstrumentKind(
                registry_result.identity.instrument_kind
            )
            identity = registry_result.identity.as_mapping()
            identity["exchange"] = registry_result.identity.venue
        else:
            canonical_symbol = asset_configuration.instrument_identity.symbol
        if using_default_policy and decision_horizon is None:
            decision_horizon = (
                CRYPTO_DECISION_HORIZON
                if resolved_instrument_kind is InstrumentKind.CRYPTO
                else DEFAULT_DECISION_HORIZON
            )
        minimum_history_rows = (
            decision_policy.preflight_minimum_history_rows(
                resolved_instrument_kind,
                decision_horizon,
            )
            if decision_horizon is not None
            else None
        )
        history_requirement = (
            {"minimum_history_rows": minimum_history_rows}
            if minimum_history_rows is not None
            else {}
        )
        try:
            snapshot = get_authoritative_market_snapshot(
                canonical_symbol,
                start_date,
                requested_date,
                asset_configuration=asset_configuration,
                **history_requirement,
            )
        except NoMarketDataError:
            snapshot = None
        snapshot_record = get_active_market_snapshot_acquisition_record(
            canonical_symbol, requested_date
        )
        identity_source_ref = stable_acquisition_source_ref(
            "identity-registry",
            registry_source_ref,
            registry_sha256,
        )
        artifact = SourceArtifact(
            artifact_sha256=registry_sha256,
            source_ref=identity_source_ref,
            tool_call_id="identity-registry",
            tool_name="instrument_identity_registry",
            raw_text=registry_raw_artifact,
        )
        identity_outcome: SourceAcquisitionOutcome = SourceAcquisitionAvailable(
            provider="instrument-identity-registry",
            capability="instrument_identity",
            source_ref=identity_source_ref,
            attempt=1,
            retrieved_at=acquired_at,
            artifact=artifact,
        )
        market_artifact = (
            snapshot_record.source_artifact
            if snapshot_record is not None
            else getattr(snapshot, "source_artifact", None)
        )
        market_status_artifact = (
            snapshot_record.status_artifact
            if snapshot_record is not None
            else getattr(snapshot, "status_artifact", None)
        )
        artifacts = (
            artifact,
            *((market_artifact,) if market_artifact is not None else ()),
            *(
                (market_status_artifact,)
                if market_status_artifact is not None
                else ()
            ),
        )
        market_outcomes = (
            snapshot_record.outcomes
            if snapshot_record is not None
            else tuple(getattr(snapshot, "acquisition_outcomes", ()))
        )
    else:
        # Identity failure is deterministic for this registry configuration.
        # Do not spend market-data calls on a run that preflight must reject.
        assert isinstance(registry_result, IdentityRegistryUnavailable)
        snapshot = None
        reason_map = {
            RegistryFailureReason.NOT_CONFIGURED: (
                AcquisitionUnavailableReason.REGISTRY_NOT_CONFIGURED
            ),
            RegistryFailureReason.NOT_FOUND: AcquisitionUnavailableReason.IDENTITY_NOT_FOUND,
            RegistryFailureReason.MALFORMED: (
                AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ),
            RegistryFailureReason.INTEGRITY_FAILURE: (
                AcquisitionUnavailableReason.INTEGRITY_FAILURE
            ),
            RegistryFailureReason.UNAVAILABLE: (
                AcquisitionUnavailableReason.REGISTRY_UNAVAILABLE
            ),
        }
        identity_outcome = SourceAcquisitionUnavailable(
            provider="instrument-identity-registry",
            capability="instrument_identity",
            source_ref=stable_acquisition_source_ref(
                "identity-registry",
                registry_result.source_ref,
                registry_result.reason.value,
            ),
            attempt=1,
            retrieved_at=acquired_at,
            retryable=False,
            reason=reason_map[registry_result.reason],
        )
        artifacts = ()
        market_outcomes = ()

    source_facts: tuple[SourceFact, ...] = ()
    calculation_outcomes: tuple[SourceAcquisitionOutcome, ...] = ()
    if snapshot is not None and market_artifact is not None:
        calculation_source_ref = stable_acquisition_source_ref(
            "calculation",
            market_artifact.source_ref,
            "market_return_20d",
        )
        if len(snapshot.frame) < MARKET_RETURN_OBSERVATIONS:
            calculation_outcomes = (
                SourceAcquisitionUnavailable(
                    provider="deterministic-calculation",
                    capability="market_return_20d",
                    source_ref=calculation_source_ref,
                    attempt=1,
                    retrieved_at=acquired_at,
                    retryable=False,
                    reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY,
                    calculation_readiness=CalculationReadinessDiagnostic(
                        calculation_id=market_return_calculation_id(
                            snapshot.adjustment_basis,
                            resolved_instrument_kind,
                        ),
                        required_observations=MARKET_RETURN_OBSERVATIONS,
                        available_observations=len(snapshot.frame),
                        input_artifact_sha256=market_artifact.artifact_sha256,
                    ),
                ),
            )
        else:
            try:
                source_facts = (
                    build_market_return_fact(
                        snapshot,
                        market_artifact,
                        instrument_kind=resolved_instrument_kind,
                    ),
                )
            except InsufficientStrategyHistoryError:
                calculation_outcomes = (
                    SourceAcquisitionUnavailable(
                        provider="deterministic-calculation",
                        capability="market_return_20d",
                        source_ref=calculation_source_ref,
                        attempt=1,
                        retrieved_at=acquired_at,
                        retryable=False,
                        reason=AcquisitionUnavailableReason.INSUFFICIENT_HISTORY,
                        calculation_readiness=CalculationReadinessDiagnostic(
                            calculation_id=market_return_calculation_id(
                                snapshot.adjustment_basis,
                                resolved_instrument_kind,
                            ),
                            required_observations=MARKET_RETURN_OBSERVATIONS,
                            available_observations=len(snapshot.frame),
                            input_artifact_sha256=market_artifact.artifact_sha256,
                        ),
                    ),
                )
            except ValueError:
                calculation_outcomes = (
                    SourceAcquisitionUnavailable(
                        provider="deterministic-calculation",
                        capability="market_return_20d",
                        source_ref=calculation_source_ref,
                        attempt=1,
                        retrieved_at=acquired_at,
                        retryable=False,
                        reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
                    ),
                )

    telemetry_ledger = get_active_run_telemetry()
    if telemetry_ledger is not None:
        telemetry_ledger.record_source_outcome(
            tool_call_id="identity-registry",
            tool_name="instrument_identity_registry",
            outcome=identity_outcome,
        )
        for outcome in calculation_outcomes:
            telemetry_ledger.record_source_outcome(
                tool_call_id=outcome.source_ref,
                tool_name="deterministic_calculation",
                outcome=outcome,
            )

    state = build_evidence_state(
        symbol=symbol,
        identity=identity,
        snapshot=snapshot,
    )
    return state.model_copy(
        update={
            "source_artifacts": artifacts,
            "source_facts": source_facts,
            "acquisition_outcomes": (
                identity_outcome,
                *market_outcomes,
                *calculation_outcomes,
            ),
        }
    )


def asset_configuration_failure_evidence(
    error: Any,
    *,
    observed_at: str | None = None,
) -> EvidenceState:
    """Project a typed pre-graph asset failure into non-directional evidence."""

    from tradingagents.dataflows.instrument_identity import RegistryFailureReason

    reason_map = {
        RegistryFailureReason.NOT_CONFIGURED: (
            AcquisitionUnavailableReason.REGISTRY_NOT_CONFIGURED
        ),
        RegistryFailureReason.NOT_FOUND: AcquisitionUnavailableReason.IDENTITY_NOT_FOUND,
        RegistryFailureReason.MALFORMED: AcquisitionUnavailableReason.MALFORMED_RESPONSE,
        RegistryFailureReason.INTEGRITY_FAILURE: (
            AcquisitionUnavailableReason.INTEGRITY_FAILURE
        ),
        RegistryFailureReason.UNAVAILABLE: (
            AcquisitionUnavailableReason.REGISTRY_UNAVAILABLE
        ),
    }
    reason = reason_map[RegistryFailureReason(error.reason)]
    source_ref = stable_acquisition_source_ref(
        "asset-configuration",
        error.source_ref,
        error.diagnostic_code,
        reason.value,
    )
    outcome = SourceAcquisitionUnavailable(
        provider="instrument-identity-registry",
        capability="instrument_identity",
        source_ref=source_ref,
        attempt=1,
        retrieved_at=observed_at or datetime.now(timezone.utc).isoformat(),
        retryable=False,
        reason=reason,
    )
    return EvidenceState(acquisition_outcomes=(outcome,))


class AnalysisOutcomeReason(str, Enum):
    PREFLIGHT_BLOCKED = "preflight_blocked"
    ADMISSION_BLOCKED = "admission_blocked"
    DECISION_GATE_BLOCKED = "decision_gate_blocked"
    PORTFOLIO_GATE_BLOCKED = "portfolio_gate_blocked"
    SHADOW_MODE_BLOCKED = "shadow_mode_blocked"
    INSTRUMENT_CURRENTLY_SUSPENDED = "instrument_currently_suspended"


class CurrentSuspensionOutcome(BaseModel):
    """Typed, non-directional rendering inputs for ADR-0023 outcomes."""

    model_config = _CLOSED_MODEL_CONFIG

    current_tradeability: Literal["suspended"] = "suspended"
    latest_traded_close_status: Literal["available", "unavailable"]
    latest_traded_close: Decimal | None = None

    @model_validator(mode="after")
    def _validate_latest_traded_close(self) -> CurrentSuspensionOutcome:
        if self.latest_traded_close_status == "available":
            if self.latest_traded_close is None:
                raise ValueError(
                    "available latest traded close status requires a value"
                )
        elif self.latest_traded_close is not None:
            raise ValueError(
                "unavailable latest traded close status cannot carry a value"
            )
        return self


class AnalysisOutcome(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["2.0"] = ANALYSIS_OUTCOME_CONTRACT_VERSION
    readiness: EvidenceReadiness
    reason: AnalysisOutcomeReason
    diagnostic_codes: tuple[AnalysisDiagnosticCode, ...] = ()
    current_suspension: CurrentSuspensionOutcome | None = None

    @field_validator("diagnostic_codes")
    @classmethod
    def _canonicalize_diagnostic_codes(
        cls,
        value: tuple[AnalysisDiagnosticCode, ...],
    ) -> tuple[AnalysisDiagnosticCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @model_validator(mode="after")
    def _validate_current_suspension(self) -> AnalysisOutcome:
        is_suspension = (
            self.reason is AnalysisOutcomeReason.INSTRUMENT_CURRENTLY_SUSPENDED
        )
        if is_suspension and self.current_suspension is None:
            raise ValueError(
                "instrument_currently_suspended requires current suspension details"
            )
        if not is_suspension and self.current_suspension is not None:
            raise ValueError(
                "current suspension details require instrument_currently_suspended"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_contract(self, handler) -> dict[str, Any]:
        payload = handler(self)
        if self.current_suspension is None:
            payload.pop("current_suspension", None)
        return payload


_ANALYSIS_OUTCOME_SUMMARIES = {
    AnalysisOutcomeReason.PREFLIGHT_BLOCKED: (
        "Analysis stopped before model-mediated work because one or more "
        "deterministic preflight requirements were not decision-ready."
    ),
    AnalysisOutcomeReason.ADMISSION_BLOCKED: (
        "Analysis stopped before thesis synthesis because required evidence and "
        "rule-backed assertions were not decision-ready."
    ),
    AnalysisOutcomeReason.DECISION_GATE_BLOCKED: (
        "Analysis completed without a Trading Decision because the final direction "
        "proposal did not satisfy the deterministic Decision Gate."
    ),
    AnalysisOutcomeReason.PORTFOLIO_GATE_BLOCKED: (
        "Analysis completed without a Trading Decision because the Portfolio Manager "
        "could not produce a decision-ready evidence chain."
    ),
    AnalysisOutcomeReason.SHADOW_MODE_BLOCKED: (
        "Analysis completed without a Trading Decision because shadow evidence mode "
        "is diagnostic-only."
    ),
    AnalysisOutcomeReason.INSTRUMENT_CURRENTLY_SUSPENDED: (
        "Analysis completed without a Trading Decision because the Mainland Instrument "
        "is currently suspended and cannot be traded."
    ),
}

_ANALYSIS_DIAGNOSTIC_LABELS = {
    AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE: (
        "Authoritative instrument identity was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE: (
        "The authoritative market snapshot was unavailable."
    ),
    AnalysisDiagnosticCode.SNAPSHOT_INVALID: (
        "The authoritative market snapshot failed deterministic validation."
    ),
    AnalysisDiagnosticCode.HISTORY_INSUFFICIENT: (
        "The authoritative snapshot did not contain enough usable history."
    ),
    AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE: (
        "A required evidence capability was unavailable or not applicable."
    ),
    AnalysisDiagnosticCode.REQUIRED_EVIDENCE_CONFLICTED: (
        "Required evidence contained an unresolved material conflict."
    ),
    AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID: (
        "Deterministic decision configuration was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.STRATEGY_RULE_INVALID: (
        "No valid registered Strategy Rule application could authorize direction."
    ),
    AnalysisDiagnosticCode.DIRECTION_CONTEXT_INVALID: (
        "The validated direction context was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.DIRECTION_ASSERTIONS_CONFLICTED: (
        "Validated Decision Assertions did not authorize one unambiguous rating."
    ),
    AnalysisDiagnosticCode.DIRECTION_SELECTION_INVALID: (
        "The direction selection was unavailable or incompatible with its context."
    ),
    AnalysisDiagnosticCode.DECISION_ASSERTION_INVALID: (
        "A decision assertion failed deterministic validation."
    ),
    AnalysisDiagnosticCode.STRUCTURED_OUTPUT_INVALID: (
        "Required structured portfolio output was unavailable or invalid."
    ),
    AnalysisDiagnosticCode.SHADOW_MODE: (
        "Shadow evidence mode cannot publish a directional decision."
    ),
    AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED: (
        "A deterministic trust-boundary check rejected publication."
    ),
    AnalysisDiagnosticCode.OPTIONAL_EVIDENCE_UNAVAILABLE: (
        "An optional evidence source was unavailable; this alone does not block a decision."
    ),
    AnalysisDiagnosticCode.INSTRUMENT_CURRENTLY_SUSPENDED: (
        "The latest applicable mainland session authoritatively confirms suspension."
    ),
}


def analysis_diagnostic_codes(
    diagnostics: Iterable[str],
) -> tuple[AnalysisDiagnosticCode, ...]:
    """Collapse internal blocker details into a closed, publication-safe taxonomy."""

    codes: set[AnalysisDiagnosticCode] = set()
    for diagnostic in diagnostics:
        normalized = " ".join(str(diagnostic).replace("_", " ").casefold().split())
        if "currently suspended" in normalized:
            code = AnalysisDiagnosticCode.INSTRUMENT_CURRENTLY_SUSPENDED
        elif "shadow" in normalized:
            code = AnalysisDiagnosticCode.SHADOW_MODE
        elif "direction context" in normalized or "validated decision context" in normalized:
            code = AnalysisDiagnosticCode.DIRECTION_CONTEXT_INVALID
        elif (
            "direction assertion ids duplicate" in normalized
            or "direction assertions target multiple ratings" in normalized
        ):
            code = AnalysisDiagnosticCode.DIRECTION_ASSERTIONS_CONFLICTED
        elif "identity" in normalized:
            code = AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE
        elif "history" in normalized or " rows" in normalized:
            code = AnalysisDiagnosticCode.HISTORY_INSUFFICIENT
        elif "snapshot" in normalized and any(
            marker in normalized for marker in ("missing", "unavailable")
        ):
            code = AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE
        elif "snapshot" in normalized:
            code = AnalysisDiagnosticCode.SNAPSHOT_INVALID
        elif "conflict" in normalized:
            code = AnalysisDiagnosticCode.REQUIRED_EVIDENCE_CONFLICTED
        elif "required evidence" in normalized or "required capability" in normalized:
            code = AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE
        elif any(
            marker in normalized
            for marker in (
                "configuration",
                "not configured",
                "registry",
                "calculation",
                "resolver",
                "evaluator",
            )
        ):
            code = AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID
        elif "strategy rule" in normalized:
            code = AnalysisDiagnosticCode.STRATEGY_RULE_INVALID
        elif "direction" in normalized or "rating" in normalized:
            code = AnalysisDiagnosticCode.DIRECTION_SELECTION_INVALID
        elif "assertion" in normalized:
            code = AnalysisDiagnosticCode.DECISION_ASSERTION_INVALID
        elif any(
            marker in normalized
            for marker in ("structured", "portfolio manager", "revision", "selection")
        ):
            code = AnalysisDiagnosticCode.STRUCTURED_OUTPUT_INVALID
        else:
            code = AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED
        codes.add(code)
    return tuple(sorted(codes, key=lambda item: item.value))


def render_analysis_outcome(outcome: AnalysisOutcome) -> str:
    """Render a non-directional outcome without portfolio recommendation fields."""
    labels = {
        EvidenceReadiness.INSUFFICIENT: "Insufficient Evidence",
        EvidenceReadiness.CONFLICTED: "Conflicted Evidence",
    }
    lines = [
        f"**Analysis Outcome:** {labels[outcome.readiness]}",
        "",
        f"**Reason Code:** `{outcome.reason.value}`",
        "",
        _ANALYSIS_OUTCOME_SUMMARIES[outcome.reason],
    ]
    if outcome.current_suspension is not None:
        latest_close = (
            str(outcome.current_suspension.latest_traded_close)
            if outcome.current_suspension.latest_traded_close_status == "available"
            else "unavailable"
        )
        lines.extend(
            [
                "",
                "**Current Tradeability:** `suspended`",
                "",
                f"**Latest Genuinely Traded Close:** `{latest_close}`",
            ]
        )
    lines.extend(["", "No Trading Decision was issued."])
    if outcome.diagnostic_codes:
        lines.extend(["", "### Deterministic Blockers", ""])
        lines.extend(
            f"- `{code.value}`: {_ANALYSIS_DIAGNOSTIC_LABELS[code]}"
            for code in sorted(outcome.diagnostic_codes, key=lambda item: item.value)
        )
    return "\n".join(lines)


def analysis_outcome_publication(outcome: AnalysisOutcome) -> dict[str, Any]:
    """Publish the typed outcome and its sole deterministic prose rendering."""

    return {
        "analysis_outcome_contract": outcome.model_dump(
            mode="json",
            exclude_none=True,
        ),
        "analysis_outcome": render_analysis_outcome(outcome),
    }


def evaluate_preflight_gate(
    evidence: EvidenceState | Mapping[str, Any] | None,
    *,
    minimum_history_rows: int = 1,
) -> EvidencePreflightResult:
    """Validate authoritative baseline evidence without requiring analyst output."""
    if minimum_history_rows < 1:
        raise ValueError("minimum_history_rows must be at least 1")
    state = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    blockers: list[str] = []
    diagnostic_codes: set[AnalysisDiagnosticCode] = set()
    identity = state.instrument_identity
    snapshot = state.market_snapshot
    profile = None

    if identity is None:
        blockers.append("authoritative instrument identity is missing")
        diagnostic_codes.add(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE)
    elif not identity.is_authoritative:
        blockers.append(
            "instrument identity requires canonical symbol, venue, kind, currency, "
            "and provenance"
        )
        diagnostic_codes.add(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE)
    else:
        try:
            profile = capability_profile_for(identity.instrument_kind)
        except ValueError as exc:
            blockers.append(str(exc))
            diagnostic_codes.add(
                AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID
            )

    if profile is not None:
        unsupported_required = set(profile.required_capabilities) - {
            EvidenceCapability.MARKET_SNAPSHOT
        }
        blockers.extend(
            "unsupported required capability: " + capability.value
            for capability in sorted(unsupported_required, key=lambda item: item.value)
        )
        if unsupported_required:
            diagnostic_codes.add(
                AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID
            )

    if snapshot is None:
        blockers.append("authoritative market snapshot is missing")
        diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE)
    else:
        if identity is not None and snapshot.symbol != identity.symbol:
            blockers.append("instrument identity and market snapshot symbols differ")
            diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        unknown_adjustment = snapshot.adjustment_basis.strip().casefold() in {
            "",
            "unknown",
            "none",
            "n/a",
        }
        if unknown_adjustment:
            blockers.append("authoritative market snapshot adjustment basis is unknown")
            diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        try:
            requested_date = datetime.fromisoformat(snapshot.requested_date)
            effective_date = datetime.fromisoformat(snapshot.effective_trading_date)
        except ValueError:
            blockers.append("authoritative market snapshot dates must be ISO-8601")
            diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        else:
            if effective_date > requested_date:
                blockers.append(
                    "effective trading date cannot be later than the requested date"
                )
                diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot.frame_sha256):
            blockers.append("authoritative market snapshot frame digest is invalid")
            diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        if not snapshot.snapshot_id.strip():
            blockers.append("authoritative market snapshot ID is missing")
            diagnostic_codes.add(AnalysisDiagnosticCode.SNAPSHOT_INVALID)
        if snapshot.history_rows < minimum_history_rows:
            blockers.append(
                "authoritative market snapshot has "
                f"{snapshot.history_rows} rows; at least {minimum_history_rows} are required"
            )
            diagnostic_codes.add(AnalysisDiagnosticCode.HISTORY_INSUFFICIENT)
        if snapshot.current_tradeability == "suspended":
            latest_close = (
                str(snapshot.latest_traded_close)
                if snapshot.latest_traded_close is not None
                else "unavailable"
            )
            blockers.append(
                "instrument currently suspended; latest genuinely traded close: "
                f"{latest_close}"
            )
            diagnostic_codes.add(
                AnalysisDiagnosticCode.INSTRUMENT_CURRENTLY_SUSPENDED
            )

    return EvidencePreflightResult(
        passed=not blockers,
        readiness=(
            EvidenceReadiness.DECISION_READY
            if not blockers
            else EvidenceReadiness.INSUFFICIENT
        ),
        blockers=tuple(blockers),
        diagnostic_codes=tuple(diagnostic_codes),
    )


def evaluate_admission_gate(
    evidence: EvidenceState,
    minimum_history_rows: int = 1,
) -> AdmissionGateResult:
    """Evaluate unchanged admission rules and retain opt-in safe gate context."""
    from tradingagents.run_diagnostics import record_admission_gate

    result = _evaluate_admission_gate(evidence, minimum_history_rows)
    record_admission_gate(evidence, result)
    return result


def _evaluate_admission_gate(
    evidence: EvidenceState,
    minimum_history_rows: int = 1,
) -> AdmissionGateResult:
    """Evaluate whether acquired evidence may proceed to thesis synthesis."""
    if evidence.instrument_identity is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,),
            diagnostics=(
                "Required evidence missing: resolved instrument identity.",
            ),
        )

    if evidence.market_snapshot is None:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE,),
            diagnostics=(
                "Required evidence missing: Authoritative Market Snapshot.",
            ),
        )

    if not evidence.market_snapshot.adjustment_basis.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_INVALID,),
            diagnostics=(
                "Required evidence missing: known Adjustment Basis.",
            ),
        )

    if not evidence.market_snapshot.effective_trading_date.strip():
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_INVALID,),
            diagnostics=(
                "Required evidence missing: Effective Trading Date.",
            ),
        )

    if (
        len(evidence.market_snapshot.frame_sha256) != 64
        or not evidence.market_snapshot.snapshot_id.startswith("snapshot:")
    ):
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_INVALID,),
            diagnostics=(
                "Required evidence missing: immutable Authoritative Market "
                "Snapshot ID and frame digest.",
            ),
        )

    ready_claims = decision_ready_material_claims(evidence)
    required_history_rows = max(
        (claim.minimum_history_rows for claim in ready_claims),
        default=minimum_history_rows,
    )
    required_history_rows = max(required_history_rows, minimum_history_rows)
    if evidence.market_snapshot.history_rows < required_history_rows:
        calculations = tuple(
            claim.claim_id
            for claim in ready_claims
            if claim.minimum_history_rows == required_history_rows
            and claim.minimum_history_rows > minimum_history_rows
        )
        calculation_suffix = (
            f" by {', '.join(calculations)}" if calculations else ""
        )
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(AnalysisDiagnosticCode.HISTORY_INSUFFICIENT,),
            diagnostics=(
                "Insufficient market history: "
                f"{evidence.market_snapshot.history_rows} rows available; "
                f"{required_history_rows} required{calculation_suffix}.",
            ),
        )

    required_not_applicable = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.NOT_APPLICABLE
    )
    if required_not_applicable:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(
                AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,
            ),
            diagnostics=tuple(
                "Required evidence configuration invalid: "
                f"{source.source_id}"
                f"{f' ({source.detail})' if source.detail else ''} "
                "is marked not applicable."
                for source in required_not_applicable
            ),
        )

    required_conflicted = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.CONFLICTED
    )
    if required_conflicted:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.CONFLICTED,
            diagnostic_codes=(
                AnalysisDiagnosticCode.REQUIRED_EVIDENCE_CONFLICTED,
            ),
            diagnostics=tuple(
                "Required evidence conflicted: "
                f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
                for source in required_conflicted
            ),
        )

    required_unavailable = tuple(
        source
        for source in evidence.sources
        if source.required and source.status is EvidenceStatus.UNAVAILABLE
    )
    if required_unavailable:
        return AdmissionGateResult(
            admitted=False,
            readiness=EvidenceReadiness.INSUFFICIENT,
            diagnostic_codes=(
                AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE,
            ),
            diagnostics=tuple(
                "Required evidence unavailable: "
                f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
                for source in required_unavailable
            ),
        )

    optional_unavailable = tuple(
        source
        for source in evidence.sources
        if not source.required and source.status is EvidenceStatus.UNAVAILABLE
    )
    diagnostics = tuple(
        "Optional evidence unavailable: "
        f"{source.source_id}{f' ({source.detail})' if source.detail else ''}."
        for source in optional_unavailable
    )

    return AdmissionGateResult(
        admitted=True,
        readiness=(
            EvidenceReadiness.DEGRADED
            if optional_unavailable
            else EvidenceReadiness.DECISION_READY
        ),
        diagnostics=diagnostics,
        diagnostic_codes=(
            (AnalysisDiagnosticCode.OPTIONAL_EVIDENCE_UNAVAILABLE,)
            if optional_unavailable
            else ()
        ),
    )
