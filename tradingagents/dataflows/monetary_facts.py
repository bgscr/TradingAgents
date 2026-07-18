from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal


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
        r"\d+(?:\.\d+)?(?P<lower_unit>亿元|万元|元)"
        r"[~～]\d+(?:\.\d+)?(?P<upper_unit>亿元|万元|元)"
    )
    ambiguous_spans = [
        match.span()
        for match in ambiguous_range_pattern.finditer(text)
        if match.group("lower_unit") != match.group("upper_unit")
    ]
    pattern = re.compile(
        r"(?<![\d.+＋\-−])(?P<lower>\d+(?:\.\d+)?)(?P<unit>亿元|万元|元)"
        r"(?:[~～](?P<upper>\d+(?:\.\d+)?)(?P=unit))?"
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
                value=Decimal(match.group("lower")) * multiplier,
                currency="CNY",
                source_ref=source_ref,
                span=match.span(),
                unit=unit,
                upper_value=Decimal(upper) * multiplier if upper is not None else None,
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
