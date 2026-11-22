"""Pure, advisory price observations from an existing run-bound mainland snapshot.

No acquisition, cache access, SourceFact creation or Strategy Rule registration.
Missing calendar proof stays unknown; observed prices never prove total return or PIT.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from hashlib import sha256

from tradingagents.evidence import CalculationLineage, EvidenceState, SourceAcquisitionAvailable
from tradingagents.report_presentation import is_chinese

VERSION = "bound-qfq-price-trend-1"
_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


class TrendUnavailable(ValueError):
    """Only a fixed code crosses the presentation boundary."""


@dataclass(frozen=True)
class AnnualPriceReturn:
    year: int
    baseline_date: str
    end_date: str
    baseline_close: str
    end_close: str
    observations: int
    basis: str
    cutoff_partial: bool
    price_return: str | None


@dataclass(frozen=True)
class MultiyearTrend:
    symbol: str
    as_of: str
    requested_start: str
    start: str
    end: str
    rows: int
    provider: str
    retrieved_at: str
    provenance: str
    first_close: str
    last_close: str
    price_return: str
    max_drawdown: str
    peak_date: str | None
    trough_date: str | None
    peak_close: str | None
    trough_close: str | None
    annual: tuple[AnnualPriceReturn, ...]
    lineage: CalculationLineage


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}\.(?:SH|SS|SZ)", value):
        raise TrendUnavailable("unsupported_instrument")
    return value[:-3] + ".SS" if value.endswith(".SH") else value


def _day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TrendUnavailable("invalid_dates")
    return date.fromisoformat(value)


def _ratio(value: Fraction) -> str:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        return format(
            (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.00000001")),
            "f",
        )


def _price(text: str, *, volume: bool = False) -> Decimal:
    if not isinstance(text, str) or len(text) > 80:
        raise TrendUnavailable("invalid_prices")
    value = Decimal(text)
    if (
        not value.is_finite()
        or value < 0
        or (not volume and value == 0)
        or abs(value.adjusted()) > 100
    ):
        raise TrendUnavailable("invalid_prices")
    return value


def build_multiyear_trend(
    evidence: EvidenceState | dict, *, symbol: str, as_of: str
) -> MultiyearTrend:
    """Calculate only from exact retained UTF-8 CSV and its available acquisition.

    Return ratios rounded half-even to 8 places; comparisons use exact fractions.
    Max drawdown is close-to-close, with earliest peak/trough on tied extrema.
    Annual rows use the last observed prior-year close when present, otherwise
    the first close of that year and an explicit partial-period label.
    """
    try:
        # Revalidate even an instance: model_copy is intentionally unchecked.
        raw = (
            evidence.model_dump(mode="python") if isinstance(evidence, EvidenceState) else evidence
        )
        ev = EvidenceState.model_validate(raw)
        requested = _day(as_of)
        canonical = _symbol(symbol)
        venue, market = ("XSHG", "shanghai") if canonical.endswith(".SS") else ("XSHE", "shenzhen")
        snap, identity = ev.market_snapshot, ev.instrument_identity
        if (
            snap is None
            or identity is None
            or _symbol(snap.symbol) != canonical
            or _symbol(identity.symbol) != canonical
            or not identity.is_authoritative
            or identity.instrument_kind.value != "equity"
            or identity.currency != "CNY"
            or identity.venue != venue
            or snap.requested_date != as_of
            or snap.adjustment_basis != "qfq"
            or snap.snapshot_id_version != "v2"
            or snap.snapshot_manifest_json is None
        ):
            raise TrendUnavailable("snapshot_binding_missing")
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", snap.retrieved_at
        ) or datetime.fromisoformat(
            snap.retrieved_at.replace("Z", "+00:00")
        ).utcoffset() != timedelta(0):
            raise TrendUnavailable("invalid_retrieval_timestamp")
        manifest = json.loads(snap.snapshot_manifest_json)
        if (
            manifest["frame"] != {"rows": snap.history_rows, "sha256": snap.frame_sha256}
            or _symbol(manifest["instrument"]["canonical_symbol"]) != canonical
            or manifest["instrument"]["currency"] != identity.currency
            or manifest["instrument"]["instrument_kind"] != "equity"
            or manifest["instrument"]["reference_market"] not in {market, venue}
            or manifest["provider"]["provider_name"] != snap.provider
            or manifest["adjustment_basis"] != "qfq"
            or manifest["requested_date"] != as_of
            or manifest["effective_trading_date"] != snap.effective_trading_date
        ):
            raise TrendUnavailable("snapshot_binding_mismatch")
        provenance = manifest["provenance_class"]
        if provenance not in {
            "retrospective_backfill",
            "observed_point_in_time",
            "live",
            "stored",
            "degraded",
        }:
            raise TrendUnavailable("unknown_provenance")
        artifacts = [a for a in ev.source_artifacts if a.artifact_sha256 == snap.frame_sha256]
        bound = [
            a
            for a in artifacts
            if any(
                isinstance(o, SourceAcquisitionAvailable)
                and o.capability == "market_snapshot"
                and o.provider == snap.provider
                and o.artifact == a
                for o in ev.acquisition_outcomes
            )
        ]
        if not bound or len({a.raw_text for a in bound}) != 1:
            raise TrendUnavailable("snapshot_artifact_unavailable")
        artifact = bound[0]
        live_identity = manifest.get("live_artifact_identity")
        if (
            live_identity is not None
            and (
                not isinstance(live_identity, str)
                or not re.fullmatch(
                    r"artifact:sha256:"
                    + snap.frame_sha256
                    + r"(?:;current-history=sha256:[0-9a-f]{64})?",
                    live_identity,
                )
            )
        ) or sha256(artifact.raw_text.encode("utf-8")).hexdigest() != snap.frame_sha256:
            raise TrendUnavailable("snapshot_binding_mismatch")
        if snap.history_gap_dates:
            raise TrendUnavailable("known_history_gaps")
        if len(artifact.raw_text) > 2_000_000 or not 2 <= snap.history_rows <= 1831:
            raise TrendUnavailable("insufficient_or_excess_history")
        try:
            window_start = requested.replace(year=requested.year - 5)
        except ValueError:  # February 29 -> February 28, same five-calendar-year rule.
            window_start = requested.replace(year=requested.year - 5, day=28)
        reader = csv.DictReader(io.StringIO(artifact.raw_text), strict=True)
        if reader.fieldnames != _COLUMNS:
            raise TrendUnavailable("invalid_frame_schema")
        points = []
        for row in reader:
            if set(row) != set(_COLUMNS) or None in row.values():
                raise TrendUnavailable("invalid_frame_schema")
            day = _day(row["Date"])
            if day < window_start or day > requested or (points and day <= points[-1][0]):
                raise TrendUnavailable("invalid_dates")
            prices = [_price(row[key]) for key in _COLUMNS[1:5]]
            _price(row["Volume"], volume=True)
            if prices[1] < max(prices) or prices[2] > min(prices):
                raise TrendUnavailable("invalid_prices")
            points.append((day, prices[3], row["Close"]))
        if len(points) != snap.history_rows or points[-1][0] != _day(snap.effective_trading_date):
            raise TrendUnavailable("row_count_or_end_mismatch")
        annual = []
        for year in sorted({p[0].year for p in points}):
            indices = [i for i, p in enumerate(points) if p[0].year == year]
            first, end = indices[0], indices[-1]
            prior = first > 0 and points[first - 1][0].year == year - 1
            base = first - 1 if prior else first
            annual.append(
                AnnualPriceReturn(
                    year,
                    points[base][0].isoformat(),
                    points[end][0].isoformat(),
                    points[base][2],
                    points[end][2],
                    len(indices),
                    "prior_year_last_observed_close" if prior else "first_observed_close_partial",
                    year == requested.year and requested < date(year, 12, 31),
                    _ratio(Fraction(points[end][1]) / Fraction(points[base][1]) - 1)
                    if end > base
                    else None,
                )
            )
        peak = points[0]
        maximum = Fraction(0)
        peak_point = trough_point = None
        for point in points:
            if point[1] > peak[1]:
                peak = point
            drawdown = 1 - Fraction(point[1]) / Fraction(peak[1])
            if drawdown > maximum:
                maximum, peak_point, trough_point = drawdown, peak, point
        values = {
            "symbol": canonical,
            "as_of": as_of,
            "requested_start": window_start.isoformat(),
            "start": points[0][0].isoformat(),
            "end": points[-1][0].isoformat(),
            "rows": len(points),
            "provider": snap.provider,
            "retrieved_at": snap.retrieved_at,
            "provenance": provenance,
            "first_close": points[0][2],
            "last_close": points[-1][2],
            "price_return": _ratio(Fraction(points[-1][1]) / Fraction(points[0][1]) - 1),
            "max_drawdown": _ratio(maximum),
            "peak_date": peak_point[0].isoformat() if peak_point else None,
            "trough_date": trough_point[0].isoformat() if trough_point else None,
            "peak_close": peak_point[2] if peak_point else None,
            "trough_close": trough_point[2] if trough_point else None,
        }

        digest = sha256(
            json.dumps(
                {**values, "annual": [asdict(a) for a in annual]},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return MultiyearTrend(
            **values,
            annual=tuple(annual),
            lineage=CalculationLineage(
                calculation_id="advisory.multiyear_qfq_price",
                calculation_version="1",
                input_artifact_sha256=snap.frame_sha256,
                input_snapshot_id=snap.snapshot_id,
                effective_range_start=values["start"],
                effective_range_end=values["end"],
                observations_used=len(points),
                adjustment_basis="qfq",
                implementation_version=VERSION,
                result_digest=digest,
            ),
        )
    except TrendUnavailable:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, csv.Error):
        raise TrendUnavailable("invalid_bound_snapshot") from None


def render_multiyear_trend(summary: MultiyearTrend, language: str = "English") -> str:
    zh = is_chinese(language)

    def label(chinese, english):
        return chinese if zh else english

    def percent(value):
        if value is None:
            return "—"
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            return f"{Decimal(value) * 100:.2f}%"

    s = summary
    lines = [
        label(
            "## 多年价格趋势（确定性辅助观察）",
            "## Multi-year price trend (deterministic advisory observations)",
        ),
        "",
        label(
            "仅供辅助分析；不新增决定性来源事实，不改变评级、评分或阈值。",
            "Advisory only; creates no decision facts and changes no rating, score or threshold.",
        ),
        "",
        f"- {label('股票 / 截至日期', 'Instrument / as-of')}: `{s.symbol}` / {s.as_of}",
        f"- {label('请求的五年窗口', 'Requested five-year window')}: {s.requested_start} → {s.as_of}",
        f"- {label('实际观测区间 / 行数', 'Observed range / rows')}: {s.start} → {s.end} / {s.rows}",
        f"- {label('来源 / 取得时间', 'Provider / retrieved at')}: `{s.provider}` / {s.retrieved_at}",
        f"- {label('价格口径', 'Price basis')}: qfq / "
        + label(
            "人民币前复权收盘价，非红利再投资总回报。",
            "CNY forward-adjusted closes; not dividend-reinvested total return.",
        ),
        f"- {label('覆盖状态', 'Coverage')}: "
        + label(
            "仅限绑定快照已有行；本栏目未取得可独立复核的日历覆盖证明，不能据此宣称五年或完整年度无缺日。",
            "Retained snapshot rows only; no independently verifiable calendar coverage in this projection, so five-year/full-year session completeness is unverified.",
        ),
        f"- {label('时间证据', 'Temporal evidence')}: `{s.provenance}`; "
        + label(
            "CURRENT 事后补录不能证明历史当时可得性；本栏目不授予严格时点回测资格。"
            if s.provenance == "retrospective_backfill"
            else "本栏目不授予严格时点回测资格。",
            "CURRENT retrospective data does not prove historical availability; this projection grants no strict PIT/backtest qualification."
            if s.provenance == "retrospective_backfill"
            else "This projection grants no strict PIT/backtest qualification.",
        ),
        "",
        f"- **{label('实际区间累计前复权价格收益', 'Observed-window cumulative qfq price return')}: {percent(s.price_return)}** ({s.first_close} → {s.last_close})",
        f"- **{label('观测收盘价最大回撤', 'Maximum observed close-to-close drawdown')}: {percent(s.max_drawdown)}**",
        f"- {label('峰值 / 谷值日期及价格', 'Peak / trough dates and closes')}: {s.peak_date or '—'} ({s.peak_close or '—'}) → {s.trough_date or '—'} ({s.trough_close or '—'})",
        "",
        label(
            "### 分年价格区间（基准日显式列出）",
            "### Yearly price intervals (explicit baseline dates)",
        ),
        "",
        label(
            "| 年份 | 基准日 / 收盘价 | 末日 / 收盘价 | 年内行数 | 价格收益 | 区间口径 |",
            "| Year | Baseline / close | End / close | Rows in year | Price return | Interval basis |",
        ),
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in s.annual:
        basis = (
            label("首个可用收盘价起算，非完整年度", "First observed close; partial year")
            if item.basis == "first_observed_close_partial"
            else label(
                "上年末条至本年末条；年度完整性未核验",
                "Prior-year last observed close; full-year completeness unverified",
            )
        )
        if item.cutoff_partial:
            basis += label("；截至期内，非全年", "; through cutoff, not a full year")
        if item.price_return is None:
            basis += label(
                "；基准与末日相同，收益不可用", "; only one endpoint, return unavailable"
            )
        lines.append(
            f"| {item.year} | {item.baseline_date} / {item.baseline_close} | {item.end_date} / {item.end_close} | {item.observations} | {percent(item.price_return)} | {basis} |"
        )
    lines += [
        "",
        label(
            "计算：末价÷基准价−1；回撤为观测收盘价相对先前最高收盘价的最大跌幅（非盘中回撤），并列取最早峰谷。比率精确分数比较后按 half-even 保留 8 位，百分比显示 2 位；缺日可能漏掉真实极值，不补齐、不年化。",
            "Method: end/base−1; drawdown is the largest decline from a prior observed closing high (not intraday), earliest peak/trough on ties. Exact fraction comparisons, half-even ratios to 8 decimals, percentages to 2. Missing sessions may hide extrema; no filling or annualization.",
        ),
        "",
        f"- Snapshot: `{s.lineage.input_snapshot_id}`",
        f"- SHA-256: `{s.lineage.input_artifact_sha256}`",
        f"- {label('计算版本 / 结果摘要', 'Calculation version / result digest')}: `{VERSION}` / `{s.lineage.result_digest}`",
    ]
    return "\n".join(lines) + "\n"


def multiyear_trend_markdown(evidence, *, symbol, as_of, language="English") -> str:
    """Optional mainland projection. Invalid/missing inputs never become numbers."""
    try:
        _symbol(symbol)
    except TrendUnavailable:
        return ""
    try:
        return render_multiyear_trend(
            build_multiyear_trend(evidence, symbol=symbol, as_of=as_of), language
        )
    except TrendUnavailable as exc:
        title = (
            "## 多年价格趋势（不可用）"
            if is_chinese(language)
            else "## Multi-year price trend (unavailable)"
        )
        note = (
            "绑定历史不足或校验未通过；不补值、不改变评级。"
            if is_chinese(language)
            else "Bound history is missing or failed validation; no filled values or rating changes."
        )
        return f"{title}\n\n{note} `{exc.args[0]}`\n"


def bound_multiyear_limitations(evidence, *, symbol, as_of, active_snapshot=None):
    """Closed presentation lines from a freshly validated bound snapshot.

    This is not an evidence source: callers may only route these exact lines
    back to advisory commentary. It does not certify a model's paraphrase.
    """
    summary = build_multiyear_trend(evidence, symbol=symbol, as_of=as_of)
    prefixes = {
        "- 价格口径:": "applicability", "- Price basis:": "applicability",
        "- 覆盖状态:": "availability", "- Coverage:": "availability",
        "- 时间证据:": "applicability", "- Temporal evidence:": "applicability",
    }
    # Retrieval time is observation metadata, not a price-frame digest field.
    # Include the rendered line only with a matching live run snapshot.
    if active_snapshot is not None and (
        active_snapshot.snapshot_id == summary.lineage.input_snapshot_id
        and active_snapshot.frame_sha256 == summary.lineage.input_artifact_sha256
        and active_snapshot.requested_date == as_of
        and active_snapshot.provider == summary.provider
        and active_snapshot.retrieved_at == summary.retrieved_at
    ):
        prefixes.update({"- 来源 / 取得时间:": "availability", "- Provider / retrieved at:": "availability"})
    quotes = {}
    for language in ("Chinese", "English"):
        for line in render_multiyear_trend(summary, language).splitlines():
            for prefix, kind in prefixes.items():
                if line.startswith(prefix):
                    quotes[line] = kind
    return summary, quotes
