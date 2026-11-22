"""Display-only retained observations and one reviewed PDF excerpt; no I/O."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256

MAX_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r'[0-9a-f]{64}')
_TICKER = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)')
_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_RAW_DATE = re.compile(r'[0-9]{8}')
_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})')
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_IDENTITY = {
    'artifact_contract_version': 'tushare-statement-adapter-v1',
    'provider_id': 'tushare',
    'endpoint_id': 'balancesheet',
    'dataset_id': 'single_stock_history',
    'schema_identity': 'tushare_balancesheet_single_stock_v1',
}
_TOP_KEYS = frozenset((*_IDENTITY, 'retrieved_at', 'observed_at', 'columns', 'rows', 'adapter_metadata'))


class ObservationRejected(ValueError):
    """Fixed safe reason only; never includes raw provider values or keys."""


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ObservationRejected(reason)


class CellState(Enum):
    ABSENT = 'missing_key'
    NULL = 'null'
    REPORTED = 'reported'


class UnitLabel(Enum):
    UNKNOWN = 'unknown'
    EXPLICIT_CNY_YUAN = 'explicit_cny_yuan'


def _bounded_decimal(value: Decimal) -> bool:
    if type(value) is not Decimal or not value.is_finite():
        return False
    parts = value.as_tuple()
    return len(parts.digits) <= 128 and type(parts.exponent) is int and abs(parts.exponent) <= 100


@dataclass(frozen=True, slots=True)
class InvestmentObservation:
    state: CellState
    value: Decimal | None

    def __post_init__(self) -> None:
        _require(type(self.state) is CellState, 'observation_state_invalid')
        _require(_bounded_decimal(self.value) if self.state is CellState.REPORTED else self.value is None,
                 'observation_value_invalid')


@dataclass(frozen=True, slots=True)
class SupplementalObservation:
    """Display-only closed record, deliberately unrelated to financial fact types."""
    payload_sha256: str
    row_index: int
    ticker: str
    report_period: date
    as_of: date
    ann_date: date
    f_ann_date: date
    retrieved_at: datetime
    observed_at: datetime
    cost_fin_assets_missing: CellState
    fair_value_fin_assets_missing: CellState
    debt_invest: InvestmentObservation
    oth_debt_invest: InvestmentObservation
    unit: UnitLabel

    def __post_init__(self) -> None:
        _require(type(self.payload_sha256) is str and _SHA.fullmatch(self.payload_sha256) is not None,
                 'observation_hash_invalid')
        _require(type(self.row_index) is int and self.row_index >= 0, 'observation_index_invalid')
        _require(type(self.ticker) is str and _TICKER.fullmatch(self.ticker) is not None,
                 'observation_ticker_invalid')
        for value in (self.report_period, self.as_of, self.ann_date, self.f_ann_date):
            _require(type(value) is date, 'observation_date_invalid')
        _require(all(value <= self.as_of for value in (self.report_period, self.ann_date, self.f_ann_date)),
                 'observation_date_after_as_of')
        for value in (self.retrieved_at, self.observed_at):
            _require(type(value) is datetime and value.tzinfo is timezone.utc, 'observation_time_invalid')
        for value in (self.cost_fin_assets_missing, self.fair_value_fin_assets_missing):
            _require(type(value) is CellState and value in (CellState.ABSENT, CellState.NULL),
                     'original_fields_must_be_missing')
        _require(type(self.debt_invest) is InvestmentObservation
                 and type(self.oth_debt_invest) is InvestmentObservation, 'observation_items_invalid')
        _require(type(self.unit) is UnitLabel, 'observation_unit_invalid')


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ObservationRejected('nonfinite_json_number')


def _json_decimal(text: str) -> Decimal:
    _require(len(text) <= 256, 'json_number_size')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ObservationRejected('json_number_invalid') from None
    _require(result.is_finite(), 'nonfinite_json_number')
    return result


def _date(value: object, *, raw: bool) -> date:
    pattern = _RAW_DATE if raw else _ISO_DATE
    _require(type(value) is str and pattern.fullmatch(value) is not None, 'date_invalid')
    text = value[:4] + '-' + value[4:6] + '-' + value[6:] if raw else value
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ObservationRejected('date_invalid') from None


def _timestamp(value: object) -> datetime:
    _require(type(value) is str and _TIME.fullmatch(value) is not None, 'observation_time_invalid')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        _require(stamp.tzinfo is not None and stamp.utcoffset() is not None, 'observation_time_invalid')
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ObservationRejected('observation_time_invalid') from None


def _original_missing(row: dict, name: str) -> CellState:
    if name not in row:
        return CellState.ABSENT
    _require(row[name] is None, 'original_field_has_value')
    return CellState.NULL


def _investment(row: dict, name: str) -> InvestmentObservation:
    if name not in row:
        return InvestmentObservation(CellState.ABSENT, None)
    value = row[name]
    if value is None:
        return InvestmentObservation(CellState.NULL, None)
    _require(type(value) in (str, int, Decimal), 'investment_value_type')
    if type(value) is str:
        _require(len(value) <= 256 and _NUMBER.fullmatch(value) is not None, 'investment_number_invalid')
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ObservationRejected('investment_number_invalid') from None
    _require(_bounded_decimal(number), 'investment_number_bounds')
    return InvestmentObservation(CellState.REPORTED, number)


def observe_retained_payload(
    raw_payload: bytes, *, expected_sha256: str, ticker: str,
    report_period: str, as_of: str, row_index: int,
) -> SupplementalObservation:
    """Read one caller-selected row; caller must supply an authorized retained source.

    Request dates use YYYY-MM-DD, provider row dates use YYYYMMDD. The expected
    digest is a caller trust input: self-hashing arbitrary bytes is not provenance.
    No file access, latest-row selection, merge, provider call or completeness check.
    """
    _require(type(raw_payload) is bytes and 0 < len(raw_payload) <= MAX_BYTES, 'payload_type_or_size')
    _require(type(expected_sha256) is str and _SHA.fullmatch(expected_sha256) is not None,
             'expected_hash_invalid')
    _require(sha256(raw_payload).hexdigest() == expected_sha256, 'payload_digest_mismatch')
    _require(type(ticker) is str and _TICKER.fullmatch(ticker) is not None, 'request_ticker_invalid')
    _require(type(row_index) is int and row_index >= 0, 'request_row_index_invalid')
    requested_period, cutoff = _date(report_period, raw=False), _date(as_of, raw=False)
    try:
        payload = json.loads(raw_payload.decode('utf-8'), object_pairs_hook=_pairs,
                             parse_float=_json_decimal, parse_constant=_constant)
    except ObservationRejected:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise ObservationRejected('payload_json_invalid') from None
    _require(type(payload) is dict and set(payload) == _TOP_KEYS, 'payload_schema_invalid')
    _require(all(type(payload[key]) is str and payload[key] == value for key, value in _IDENTITY.items()),
             'payload_identity_invalid')
    metadata = payload['adapter_metadata']
    _require(type(metadata) is dict and set(metadata) in (
        {'response_completeness'}, {'response_completeness', 'response_values'}), 'metadata_shape_invalid')
    _require(all(type(value) is dict for value in metadata.values()), 'metadata_shape_invalid')
    # Metadata contents are deliberately NOT assessed or represented as qualified.
    columns, rows = payload['columns'], payload['rows']
    _require(type(columns) is list and bool(columns) and all(type(key) is str and key for key in columns),
             'columns_invalid')
    _require(len(columns) == len(set(columns)), 'columns_duplicate')
    _require(type(rows) is list and row_index < len(rows), 'rows_or_index_invalid')
    keys = set(columns)
    _require(all(type(row) is dict and set(row) == keys for row in rows), 'row_shape_invalid')
    selected = rows[row_index]
    _require(type(selected.get('ts_code')) is str and selected['ts_code'] == ticker, 'row_ticker_mismatch')
    for name, expected in (('report_type', 1), ('comp_type', 2)):
        value = selected.get(name)
        _require(type(value) in (str, int) and value in (expected, str(expected)), 'row_scope_invalid')
    period = _date(selected.get('end_date'), raw=True)
    announcement = _date(selected.get('ann_date'), raw=True)
    final_announcement = _date(selected.get('f_ann_date'), raw=True)
    _require(period == requested_period, 'row_period_mismatch')
    _require(all(value <= cutoff for value in (period, announcement, final_announcement)), 'row_date_after_as_of')
    unit = (UnitLabel.EXPLICIT_CNY_YUAN
            if type(selected.get('currency')) is str and selected['currency'] == 'CNY'
            and type(selected.get('unit')) is str and selected['unit'] == '元'
            else UnitLabel.UNKNOWN)
    return SupplementalObservation(
        expected_sha256, row_index, ticker, period, cutoff, announcement, final_announcement,
        _timestamp(payload['retrieved_at']), _timestamp(payload['observed_at']),
        _original_missing(selected, 'cost_fin_assets'), _original_missing(selected, 'fair_value_fin_assets'),
        _investment(selected, 'debt_invest'), _investment(selected, 'oth_debt_invest'), unit,
    )


@dataclass(frozen=True, slots=True)
class _ReviewedPdfExcerpt:
    """Fixed page-text transcription, not a PDF artifact or qualified financial fact."""
    title_zh: str
    translated_title_en: str
    url: str
    page: int
    page_count: int
    ticker: str
    report_period: date
    comparison_period: date
    consolidation_scope: str
    currency: str
    unit_zh: str
    unit_en: str
    url_date: date
    reviewed_on: date
    items: tuple[tuple[str, str, str], ...]


# Verified against the specified public PDF page text on the review date.
# There are no retained local PDF bytes or PDF digest. The URL date is only a
# conservative display lower bound; it does not prove historical availability.
_JIANGSU_2026Q3_PDF = _ReviewedPdfExcerpt(
    title_zh='江苏银行 2026 年第三季度报告',
    translated_title_en='Bank of Jiangsu 2026 Third Quarter Report',
    url='https://static.cninfo.com.cn/finalpage/2026-10-30/1225642695.PDF',
    page=9, page_count=15, ticker='600919.SH', report_period=date(2026, 9, 30),
    comparison_period=date(2025, 12, 31), consolidation_scope='consolidated', currency='CNY',
    unit_zh='人民币千元', unit_en='CNY thousand', url_date=date(2026, 10, 30), reviewed_on=date(2026, 11, 14),
    items=(('债权投资', 'Debt investments', '771,621,601'),
           ('其他债权投资', 'Other debt investments', '956,573,604'),
           ('其他权益工具投资', 'Other equity instrument investments', '630,229')),
)


def _render_reviewed_pdf_excerpt(*, ticker: str, report_period: date, as_of: date,
                                 consolidation_scope: str, chinese: bool) -> str:
    """Select this one exact public source only; never infer API units or values."""
    _require(type(chinese) is bool, 'presentation_language_invalid')
    _require(type(ticker) is str and type(report_period) is date and type(as_of) is date
             and type(consolidation_scope) is str, 'document_binding_invalid')
    source = _JIANGSU_2026Q3_PDF
    if (ticker != source.ticker or report_period != source.report_period
            or consolidation_scope != source.consolidation_scope or as_of < source.url_date):
        return ''

    def text(english: str, zh: str) -> str:
        return zh if chinese else english

    unit = source.unit_zh if chinese else source.unit_en
    title = source.title_zh if chinese else source.translated_title_en + ' (title translation)'
    lines = [
        '', '', text('### Separate public PDF source (display only)', '### 公开 PDF 独立来源（仅展示）'), '',
        f'- {text("Document", "文档")}：[{title}]({source.url}#page={source.page})',
        f'- {text("Original document title", "原文标题")}：{source.title_zh}',
        f'- {text("Page", "页码")}：{source.page} / {source.page_count}',
        text('- Statement: Consolidated balance sheet; Unaudited.', '- 报表：合并资产负债表；未经审计。'),
        f'- {text("Stock", "股票")}：`{source.ticker}`',
        f'- {text("Selected report column", "所选报告期列")}：{source.report_period.isoformat()}',
        f'- {text("Comparison column (not selected)", "对照列（未选用）")}：{source.comparison_period.isoformat()}',
        f'- {text("Currency and original unit", "币种与原单位")}：{source.currency} / {unit}',
        f'- {text("URL date (display lower bound only)", "URL 日期（仅作展示下界）")}：{source.url_date.isoformat()}',
        f'- {text("Public page text reviewed on", "公开页面文字核对日期")}：{source.reviewed_on.isoformat()}',
        text('- No local PDF bytes or digest retained; this is a reviewed page-text excerpt, not a captured PDF artifact.',
             '- 本地未留存 PDF 字节或摘要；这是已核对的页面文字摘录，不是程序留存的 PDF 制品。'),
        '',
        text('The URL date and text review do not establish historical availability at the analysis cutoff.',
             'URL 日期和文字核对不证明分析截至日的历史可获得性。'),
        '',
        text('| Document line item | Original reported value | Original unit |', '| 文档独立科目 | 原报告值 | 原单位 |'),
        '| --- | --- | --- |',
    ]
    lines.extend(f'| {zh if chinese else english} | {value} | {unit} |'
                 for zh, english, value in source.items)
    lines.extend(('', text('No conversion. These PDF units are not used to infer API response units. '
                          'The three items are not summed into AC / FVOCI totals, do not fill '
                          '`cost_fin_assets` or `fair_value_fin_assets`, and do not enter completeness scores, ratios or decisions.',
                          '未换算。PDF 单位不用于推断 API 响应单位；三项不合计为 AC / FVOCI 总额，'
                          '不填补 `cost_fin_assets` 或 `fair_value_fin_assets`，不计入完整性评分、比率或决策。'), ''))
    return '\n'.join(lines)


def render_supplemental(observation: SupplementalObservation, *, chinese: bool = True) -> str:
    """Separate Markdown display only; no CSV/fact conversion or analysis entry."""
    _require(type(observation) is SupplementalObservation, 'observation_type_invalid')
    _require(type(chinese) is bool, 'presentation_language_invalid')
    observation.__post_init__()

    def text(english: str, zh: str) -> str:
        return zh if chinese else english

    missing = {
        CellState.ABSENT: text('Missing (key absent)', '缺失（原响应无该键）'),
        CellState.NULL: text('Missing (response null)', '缺失（原响应为 null）'),
    }

    def item_row(label: str, value: InvestmentObservation) -> str:
        value.__post_init__()
        reported = value.state is CellState.REPORTED
        status = text('Reported', '已报告') if reported else missing[value.state]
        number = format(value.value, 'f') if reported else '—'
        return f'| {label} | {status} | {number} |'

    unit = (text('Explicit CNY / yuan in response; no conversion', '原响应显式标注 CNY / 元；未换算')
            if observation.unit is UnitLabel.EXPLICIT_CNY_YUAN else
            text('Unknown (no supported explicit currency/unit pair); no conversion',
                 '未知（无受支持的显式币种/单位组合）；未换算'))
    header = text('| Field | Status | Retained value |', '| 字段 | 状态 | 保留原值 |')
    api_text = '\n'.join((
        text('## Supplemental retained observations (display only)', '## 原始观察补充栏目（不参与分析）'),
        '',
        text('> Observations only, not financial facts. Completeness eligibility has not been assessed; '
             'not included in completeness scores, ratios or decisions.',
             '> 仅展示观察，不是金融事实；未做完整性资格校验，不计入完整性评分、比率或决策。'),
        '',
        text('Debt investments and other debt investments do not replace or fill the two missing target fields below.',
             '债权投资与其他债权投资不是下列两个缺失字段的替代或补值。'),
        '',
        text('### Source and scope', '### 来源与口径'),
        '',
        text('- Source label: retained Tushare response (label and digest do not establish authenticity; not an original PDF).',
             '- 来源标签：Tushare 保留响应（标签与摘要不证明真实性；不是原始 PDF）。'),
        text('- Response scope: bank (comp_type=2), consolidated statement (report_type=1). '
             'Only the selected original row; no mixing of periods or versions.',
             '- 原响应口径：银行（comp_type=2），合并报表（report_type=1）；仅所选原始行，不合并其他期次或版本。'),
        f'- {text("Stock", "股票")}：`{observation.ticker}`',
        f'- {text("Report period", "报告期")}：{observation.report_period.isoformat()}',
        f'- {text("Analysis cutoff", "截至日期")}：{observation.as_of.isoformat()}',
        f'- {text("Announcement date", "公告日")}：{observation.ann_date.isoformat()}',
        f'- {text("Actual announcement date", "实际公告日")}：{observation.f_ann_date.isoformat()}',
        f'- {text("Response retrieved at", "保留响应取得时间")}：{observation.retrieved_at.isoformat()}',
        f'- {text("Observed at", "观察时间")}：{observation.observed_at.isoformat()}',
        f'- {text("Exact payload binding", "精确来源绑定")}：SHA-256 `{observation.payload_sha256}`',
        f'- {text("Row index (zero-based)", "行索引（从 0 起）")}：{observation.row_index}',
        f'- {text("Currency and unit", "币种与单位")}：{unit}',
        '',
        text('Retrieval and observation times do not establish that the data was available at the analysis cutoff.',
             '取得/观察时间不证明该数据在截至日期时已可获得。'),
        '',
        text('### Original target fields (still missing)', '### 原目标字段（仍缺失）'),
        '', header, '| --- | --- | --- |',
        f'| `cost_fin_assets` | {missing[observation.cost_fin_assets_missing]} | — |',
        f'| `fair_value_fin_assets` | {missing[observation.fair_value_fin_assets_missing]} | — |',
        '',
        text('### Separate supplemental items', '### 独立补充科目'),
        '', header, '| --- | --- | --- |',
        item_row(text('Debt investments (`debt_invest`)', '债权投资 (`debt_invest`)'), observation.debt_invest),
        item_row(text('Other debt investments (`oth_debt_invest`)', '其他债权投资 (`oth_debt_invest`)'), observation.oth_debt_invest),
        '',
    ))
    # The retained observer admits only consolidated report_type=1 bank rows.
    # Keep the independently sourced PDF section outside the API observation model.
    return api_text + _render_reviewed_pdf_excerpt(
        ticker=observation.ticker, report_period=observation.report_period, as_of=observation.as_of,
        consolidation_scope='consolidated', chinese=chinese,
    )
