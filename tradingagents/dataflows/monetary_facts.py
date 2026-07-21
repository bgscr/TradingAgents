from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

_NUMERIC_TOKEN_PATTERN = (
    r"(?:[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+|[0-9０-９]+)"
    r"(?:[.．][0-9０-９]+)?"
)
_NUMERIC_TOKEN_PREFIX_CHARS = r"0-9０-９.,，．+＋\-−﹣－"


def _numeric_decimal(token: str) -> Decimal:
    normalized = unicodedata.normalize("NFKC", token).replace(",", "")
    return Decimal(normalized)


@dataclass(frozen=True)
class MonetarySourceFact:
    raw_text: str
    value: Decimal
    currency: str
    source_ref: str
    span: tuple[int, int]
    unit: str
    upper_value: Decimal | None = None

    @property
    def is_range(self) -> bool:
        return self.upper_value is not None


def extract_chinese_monetary_facts(
    text: str, source_ref: str
) -> tuple[MonetarySourceFact, ...]:
    ambiguous_range_pattern = re.compile(
        rf"{_NUMERIC_TOKEN_PATTERN}(?P<lower_unit>亿元|万元|元)"
        rf"[~～]{_NUMERIC_TOKEN_PATTERN}(?P<upper_unit>亿元|万元|元)"
    )
    ambiguous_spans = [
        match.span()
        for match in ambiguous_range_pattern.finditer(text)
        if match.group("lower_unit") != match.group("upper_unit")
    ]
    pattern = re.compile(
        rf"(?<![{_NUMERIC_TOKEN_PREFIX_CHARS}])"
        rf"(?P<lower>{_NUMERIC_TOKEN_PATTERN})(?P<unit>亿元|万元|元)"
        rf"(?:[~～](?P<upper>{_NUMERIC_TOKEN_PATTERN})(?P=unit))?"
    )
    non_cny_prefix = re.compile(
        r"(?:美元|港元|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP|US\$|HK\$|\$)\s*$",
        re.IGNORECASE,
    )
    multipliers = {
        "元": Decimal("1"),
        "万元": Decimal("10000"),
        "亿元": Decimal("100000000"),
    }
    facts = []
    for match in pattern.finditer(text):
        if non_cny_prefix.search(text[: match.start()]):
            continue
        if any(
            match.start() < ambiguous_end and match.end() > ambiguous_start
            for ambiguous_start, ambiguous_end in ambiguous_spans
        ):
            continue
        unit = match.group("unit")
        multiplier = multipliers[unit]
        upper = match.group("upper")
        facts.append(
            MonetarySourceFact(
                raw_text=match.group(0),
                value=_numeric_decimal(match.group("lower")) * multiplier,
                currency="CNY",
                source_ref=source_ref,
                span=match.span(),
                unit=unit,
                upper_value=(
                    _numeric_decimal(upper) * multiplier if upper is not None else None
                ),
            )
        )
    return tuple(facts)


def render_monetary_source_facts(facts: tuple[MonetarySourceFact, ...]) -> str:
    def canonical_decimal(value: Decimal) -> str:
        return format(value.normalize(), "f")

    records = []
    for fact in facts:
        record = {
            "currency": fact.currency,
            "is_range": fact.is_range,
            "raw_text": fact.raw_text,
            "source_ref": fact.source_ref,
            "span": list(fact.span),
            "unit": fact.unit,
            "value": canonical_decimal(fact.value),
        }
        if fact.upper_value is not None:
            record["upper_value"] = canonical_decimal(fact.upper_value)
        records.append(record)
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"## Structured Monetary Source Facts\n```json\n{payload}\n```"
