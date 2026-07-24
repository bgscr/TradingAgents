"""Shipped deterministic Strategy Rules for production decision routing.

The initial catalog is intentionally small.  It turns one source-bound,
20-trading-day close return into mutually exclusive Buy, Hold, or Sell support.
Missing or invalid canonical evidence still produces an Analysis Outcome.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from hashlib import sha256

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.decision_policy import (
    CanonicalFactAdapterResult,
    DecisionFact,
    DecisionHorizon,
    DecisionPolicyEngine,
    HorizonUnit,
    MissingFactBehavior,
    RulePolarity,
    RulePredicateResult,
    StrategyRuleDefinition,
    stable_decision_value_digest,
)
from tradingagents.evidence import (
    CalculationDefinition,
    CalculationLineage,
    InstrumentKind,
    MissingValuePolicy,
    SourceArtifact,
    SourceFact,
    stable_source_fact_id,
)

DEFAULT_DECISION_HORIZON = DecisionHorizon(
    count=20,
    unit=HorizonUnit.TRADING_DAYS,
)
MARKET_RETURN_FIELD = "market.close_return_20d"
MARKET_RETURN_UNIT = "ratio"
MARKET_RETURN_OBSERVATIONS = 21
MARKET_RETURN_PRECISION = Decimal("0.00000001")
MARKET_RETURN_IMPLEMENTATION_VERSION = "decimal-close-return-1"
_SUPPORTED_ADJUSTMENT_BASES = ("auto_adjusted", "qfq")


def market_return_calculation_id(adjustment_basis: str) -> str:
    return f"market.close_return_20d.{adjustment_basis}"


def market_return_calculation_definitions() -> tuple[CalculationDefinition, ...]:
    return tuple(
        CalculationDefinition(
            calculation_id=market_return_calculation_id(adjustment_basis),
            version="1.0",
            input_fields=("Close",),
            input_frequency="trading_day",
            minimum_history_rows=MARKET_RETURN_OBSERVATIONS,
            warmup_rows=MARKET_RETURN_OBSERVATIONS - 1,
            adjustment_basis=adjustment_basis,
            missing_value_policy=MissingValuePolicy.FAIL,
            formula="(close[t] / close[t-20]) - 1",
            implementation_version=MARKET_RETURN_IMPLEMENTATION_VERSION,
            output_field=MARKET_RETURN_FIELD,
            output_unit=MARKET_RETURN_UNIT,
            precision=8,
        )
        for adjustment_basis in _SUPPORTED_ADJUSTMENT_BASES
    )


def _tail_observation_span(raw_text: str) -> tuple[int, int]:
    lines = raw_text.splitlines(keepends=True)
    if len(lines) < MARKET_RETURN_OBSERVATIONS + 1:
        raise ValueError("market snapshot has insufficient history")
    start = sum(len(line) for line in lines[:-MARKET_RETURN_OBSERVATIONS])
    return start, len(raw_text)


def calculate_market_return(raw_text: str) -> Decimal:
    rows = tuple(csv.DictReader(io.StringIO(raw_text)))
    if len(rows) < MARKET_RETURN_OBSERVATIONS:
        raise ValueError("market snapshot has insufficient history")
    selected = rows[-MARKET_RETURN_OBSERVATIONS:]
    try:
        first_close = Decimal(selected[0]["Close"])
        last_close = Decimal(selected[-1]["Close"])
    except (InvalidOperation, KeyError, TypeError) as exc:
        raise ValueError("market snapshot close values are invalid") from exc
    if not first_close.is_finite() or not last_close.is_finite() or first_close <= 0:
        raise ValueError("market snapshot close values are invalid")
    return ((last_close / first_close) - Decimal(1)).quantize(
        MARKET_RETURN_PRECISION,
        rounding=ROUND_HALF_EVEN,
    )


def build_market_return_fact(snapshot, artifact: SourceArtifact) -> SourceFact:
    definitions = {
        definition.adjustment_basis: definition
        for definition in market_return_calculation_definitions()
    }
    try:
        definition = definitions[snapshot.adjustment_basis]
    except KeyError as exc:
        raise ValueError("market snapshot adjustment basis is not registered") from exc

    rows = tuple(csv.DictReader(io.StringIO(artifact.raw_text)))
    if len(rows) < MARKET_RETURN_OBSERVATIONS:
        raise ValueError("market snapshot has insufficient history")
    selected = rows[-MARKET_RETURN_OBSERVATIONS:]
    try:
        effective_range_start = selected[0]["Date"]
        effective_range_end = selected[-1]["Date"]
    except (KeyError, TypeError) as exc:
        raise ValueError("market snapshot dates are invalid") from exc
    value = calculate_market_return(artifact.raw_text)
    span_start, span_end = _tail_observation_span(artifact.raw_text)
    fact_id = stable_source_fact_id(
        source_ref=artifact.source_ref,
        artifact_sha256=artifact.artifact_sha256,
        source_span_start=span_start,
        source_span_end=span_end,
        canonical_field=MARKET_RETURN_FIELD,
        instrument_symbol=snapshot.symbol,
        effective_date=snapshot.effective_trading_date,
    )
    lineage = CalculationLineage(
        calculation_id=definition.calculation_id,
        calculation_version=definition.version,
        input_artifact_sha256=artifact.artifact_sha256,
        input_snapshot_id=snapshot.snapshot_id,
        effective_range_start=effective_range_start,
        effective_range_end=effective_range_end,
        observations_used=MARKET_RETURN_OBSERVATIONS,
        adjustment_basis=snapshot.adjustment_basis,
        implementation_version=definition.implementation_version,
        result_digest=stable_decision_value_digest(
            canonical_field=MARKET_RETURN_FIELD,
            normalized_value=value,
            unit=MARKET_RETURN_UNIT,
        ),
    )
    return SourceFact(
        fact_kind="canonical",
        fact_id=fact_id,
        source_ref=artifact.source_ref,
        tool_call_id=artifact.tool_call_id,
        tool_name=artifact.tool_name,
        artifact_sha256=artifact.artifact_sha256,
        raw_text=artifact.raw_text[span_start:span_end],
        source_span_start=span_start,
        source_span_end=span_end,
        calculation_ids=(definition.calculation_id,),
        canonical_field=MARKET_RETURN_FIELD,
        normalized_value=value,
        unit=MARKET_RETURN_UNIT,
        instrument_symbol=snapshot.symbol,
        effective_date=snapshot.effective_trading_date,
        calculation_lineage=lineage,
    )


def _market_return_adapter(
    artifact: SourceArtifact,
    source_span_start: int,
    source_span_end: int,
) -> CanonicalFactAdapterResult:
    if (source_span_start, source_span_end) != _tail_observation_span(
        artifact.raw_text
    ):
        raise ValueError("canonical market-return span is not the registered window")
    rows = tuple(csv.DictReader(io.StringIO(artifact.raw_text)))
    selected = rows[-MARKET_RETURN_OBSERVATIONS:]
    return CanonicalFactAdapterResult(
        canonical_field=MARKET_RETURN_FIELD,
        normalized_value=calculate_market_return(artifact.raw_text),
        unit=MARKET_RETURN_UNIT,
        effective_range_start=selected[0]["Date"],
        effective_range_end=selected[-1]["Date"],
        observations_used=len(selected),
    )


def _evaluation_digest(
    rule: StrategyRuleDefinition,
    fact: DecisionFact,
    as_of_date: date,
    satisfied: bool,
) -> str:
    payload = {
        "as_of_date": as_of_date.isoformat(),
        "fact_id": fact.fact_id,
        "normalized_value": str(fact.normalized_value),
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "satisfied": satisfied,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _market_return_rule_evaluator(
    rule: StrategyRuleDefinition,
    facts: tuple[DecisionFact, ...],
    as_of_date: date,
) -> RulePredicateResult:
    blockers: list[str] = []
    if len(facts) != 1:
        blockers.append("market_return_rule_requires_one_fact")
        value = Decimal(0)
        fact = facts[0] if facts else None
    else:
        fact = facts[0]
        try:
            value = Decimal(str(fact.normalized_value))
        except InvalidOperation:
            value = Decimal(0)
            blockers.append("market_return_value_invalid")
        if fact.canonical_field != MARKET_RETURN_FIELD or fact.unit != MARKET_RETURN_UNIT:
            blockers.append("market_return_fact_semantics_invalid")

    threshold = rule.threshold
    if threshold is None:
        blockers.append("market_return_threshold_missing")
        threshold = Decimal(0)

    satisfied = False
    if not blockers:
        if rule.comparator == "greater_than_or_equal":
            satisfied = value >= threshold
        elif rule.comparator == "less_than_or_equal":
            satisfied = value <= threshold
        elif rule.comparator == "absolute_value_less_than":
            satisfied = abs(value) < threshold
        else:
            blockers.append("market_return_comparator_invalid")

    digest_fact = fact or DecisionFact(
        fact_id="fact:unavailable",
        canonical_field=MARKET_RETURN_FIELD,
        normalized_value=Decimal(0),
        unit=MARKET_RETURN_UNIT,
        instrument_symbol="unavailable",
        effective_date=as_of_date.isoformat(),
        source_ref="unavailable",
        artifact_sha256="unavailable",
        source_span_start=0,
        source_span_end=0,
    )
    return RulePredicateResult(
        satisfied=satisfied,
        fact_ids=tuple(sorted(fact.fact_id for fact in facts)),
        evaluation_digest=_evaluation_digest(
            rule,
            digest_fact,
            as_of_date,
            satisfied,
        ),
        blockers=tuple(sorted(blockers)),
    )


def production_strategy_rules() -> tuple[StrategyRuleDefinition, ...]:
    common = {
        "version": "1.0",
        "applicable_instrument_kinds": (
            InstrumentKind.EQUITY,
        ),
        "required_canonical_fields": (MARKET_RETURN_FIELD,),
        "polarity": RulePolarity.SUPPORTS,
        "horizon": DEFAULT_DECISION_HORIZON,
        "predicate_id": "market_return_threshold",
        "max_fact_age_days": 10,
        "minimum_history_rows": MARKET_RETURN_OBSERVATIONS,
        "missing_fact_behavior": MissingFactBehavior.BLOCK,
        "block_on_conflict": True,
        "implementation_version": "market-return-threshold-1",
    }
    return (
        StrategyRuleDefinition(
            rule_id="market.return_20d.buy",
            target_rating=PortfolioRating.BUY,
            comparator="greater_than_or_equal",
            threshold=Decimal("0.05"),
            **common,
        ),
        StrategyRuleDefinition(
            rule_id="market.return_20d.hold",
            target_rating=PortfolioRating.HOLD,
            comparator="absolute_value_less_than",
            threshold=Decimal("0.05"),
            **common,
        ),
        StrategyRuleDefinition(
            rule_id="market.return_20d.sell",
            target_rating=PortfolioRating.SELL,
            comparator="less_than_or_equal",
            threshold=Decimal("-0.05"),
            **common,
        ),
    )


def create_production_decision_policy() -> DecisionPolicyEngine:
    definitions = market_return_calculation_definitions()
    return DecisionPolicyEngine(
        rules=production_strategy_rules(),
        evaluators={"market_return_threshold": _market_return_rule_evaluator},
        calculation_definitions=definitions,
        fact_adapters={
            (definition.calculation_id, definition.version): _market_return_adapter
            for definition in definitions
        },
    )


__all__ = [
    "DEFAULT_DECISION_HORIZON",
    "MARKET_RETURN_FIELD",
    "MARKET_RETURN_IMPLEMENTATION_VERSION",
    "MARKET_RETURN_OBSERVATIONS",
    "MARKET_RETURN_PRECISION",
    "MARKET_RETURN_UNIT",
    "calculate_market_return",
    "build_market_return_fact",
    "create_production_decision_policy",
    "market_return_calculation_definitions",
    "market_return_calculation_id",
    "production_strategy_rules",
]
