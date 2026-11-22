"""One reviewed PDF excerpt; real report composition with memory-only publication."""
import copy
import json
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest
from markdown_it import MarkdownIt

from tests.fixtures.synthetic_supplemental_statement import SYNTHETIC_DEBT, synthetic_balancesheet
from tests.test_supplemental_presentation import (
    MemoryStore,
    export,
    memory_publication as _memory_publication,
    payload,
    state,
)
from tradingagents import supplemental_observation as observations
from tradingagents.reporting import SupplementalStatementRequest

URL = 'https://static.cninfo.com.cn/finalpage/2026-10-30/1225642695.PDF'
PDF_VALUES = ('771,621,601', '956,573,604', '630,229')
memory_publication = _memory_publication


def document_request(*, period='2026-09-30', changes=None):
    value = payload()
    row = value['rows'][0]
    row.update(end_date=period.replace('-', ''), ann_date='20261030', f_ann_date='20261030',
               cost_fin_assets=None, fair_value_fin_assets=None)
    row.update(changes or {})
    value['columns'] = list(row)
    raw = json.dumps(value, ensure_ascii=False).encode()
    return SupplementalStatementRequest(raw, sha256(raw).hexdigest(), period, 0)


@pytest.mark.parametrize('language', ['English', 'Chinese'])
def test_reviewed_pdf_values_and_metadata_reach_only_the_bound_appendix(memory_publication, language):
    final_state, request = state(), document_request()
    before = copy.deepcopy(final_state)
    baseline = export(final_state, None, language)
    augmented = export(final_state, request, language)
    text = augmented['complete_report.md'].decode()
    heading = '### 公开 PDF 独立来源（仅展示）' if language == 'Chinese' else '### Separate public PDF source (display only)'
    assert heading in text
    api_text, document_text = text.split(heading, 1)
    assert ('未知' if language == 'Chinese' else 'Unknown') in api_text
    assert ('缺失（原响应为 null）' if language == 'Chinese' else 'Missing (response null)') in api_text
    assert '123.456789012345678901' in api_text  # Deliberately unlike PDF data; never overwrite API values.
    assert '600919.SH' in document_text and '2026-09-30' in document_text
    assert '2025-12-31' in document_text and '2026-10-30' in document_text
    assert '江苏银行 2026 年第三季度报告' in document_text
    assert URL + '#page=9' in document_text
    for value in PDF_VALUES:
        assert value in document_text
    for label in (('人民币千元', '合并资产负债表', '未经审计', '9 / 15', '本地未留存 PDF 字节或摘要')
                  if language == 'Chinese' else
                  ('CNY thousand', 'Consolidated balance sheet', 'Unaudited', '9 / 15',
                   'No local PDF bytes or digest retained')):
        assert label in document_text
    assert ('不用于推断 API 响应单位' if language == 'Chinese' else
            'not used to infer API response units') in document_text
    assert ('不合计为 AC / FVOCI 总额' if language == 'Chinese' else
            'not summed into AC / FVOCI totals') in document_text
    tokens = MarkdownIt('commonmark').enable('table').parse(text)
    assert sum(token.type == 'table_open' for token in tokens) == 3
    links = [token.attrGet('href') for block in tokens for token in (block.children or [])
             if token.type == 'link_open']
    assert URL + '#page=9' in links
    assert final_state == before and memory_publication[0] == memory_publication[1]
    assert baseline.keys() == augmented.keys()
    for name in baseline.keys() - {'complete_report.md', 'manifest.json'}:
        assert augmented[name] == baseline[name]
    manifest = json.loads(augmented['manifest.json'])
    assert manifest['files']['complete_report.md'] == sha256(augmented['complete_report.md']).hexdigest()


@pytest.mark.parametrize('ticker,period,scope,cutoff', [
    ('600000.SH', date(2026, 9, 30), 'consolidated', date(2026, 11, 1)),
    ('600919.SZ', date(2026, 9, 30), 'consolidated', date(2026, 11, 1)),
    ('600919.SH', date(2025, 12, 31), 'consolidated', date(2026, 11, 1)),
    ('600919.SH', date(2026, 6, 30), 'consolidated', date(2026, 11, 1)),
    ('600919.SH', date(2026, 9, 30), 'parent_company', date(2026, 11, 1)),
    ('600919.SH', date(2026, 9, 30), 'consolidated', date(2026, 10, 29)),
])
def test_other_stock_period_scope_or_early_cutoff_cannot_select_document(ticker, period, scope, cutoff):
    assert observations._render_reviewed_pdf_excerpt(
        ticker=ticker, report_period=period, as_of=cutoff,
        consolidation_scope=scope, chinese=False) == ''


@pytest.mark.usefixtures('memory_publication')
@pytest.mark.parametrize('period', ['2025-12-31', '2026-06-30'])
def test_other_valid_api_period_does_not_receive_this_pdf_record(period):
    text = export(state(), document_request(period=period))['complete_report.md'].decode()
    assert URL not in text and all(value not in text for value in PDF_VALUES)
    assert 'Unknown' in text


@pytest.mark.parametrize('changes', [{'ts_code': '600000.SH'}, {'report_type': '2'},
                                    {'comp_type': '1'}, {'cost_fin_assets': '1'}])
def test_original_binding_and_missing_field_guards_still_reject_before_publication(memory_publication, changes):
    with pytest.raises(ValueError, match='supplemental_report_binding_invalid'):
        export(state(), document_request(changes=changes))
    assert MemoryStore.opened == [] and memory_publication == []


@pytest.mark.usefixtures('memory_publication')
@pytest.mark.parametrize('language', ['English', 'Chinese'])
def test_synthetic_api_unknown_unit_and_two_null_targets_remain_separate(language):
    raw = synthetic_balancesheet()
    # This self-digest tests byte binding only; it is not supplier authentication.
    digest = sha256(raw).hexdigest()
    observation = observations.observe_retained_payload(
        raw, expected_sha256=digest, ticker='600919.SH', report_period='2026-09-30',
        as_of='2026-11-11', row_index=0)
    assert observation.unit is observations.UnitLabel.UNKNOWN
    assert observation.cost_fin_assets_missing is observations.CellState.NULL
    assert observation.fair_value_fin_assets_missing is observations.CellState.NULL
    assert observation.debt_invest.state is observations.CellState.REPORTED
    assert observation.debt_invest.value == Decimal(SYNTHETIC_DEBT)
    assert observation.oth_debt_invest.state is observations.CellState.REPORTED
    assert observation.oth_debt_invest.value == Decimal(0)
    request = SupplementalStatementRequest(raw, digest, '2026-09-30', 0)
    report = export(state(), request, language)['complete_report.md'].decode()
    heading = '### 公开 PDF 独立来源（仅展示）' if language == 'Chinese' else '### Separate public PDF source (display only)'
    api_text, pdf_text = report.split(heading, 1)
    assert ('未知' if language == 'Chinese' else 'Unknown') in api_text
    assert all(value in pdf_text for value in PDF_VALUES)
    assert ('未换算' if language == 'Chinese' else 'no conversion') in api_text
    assert SYNTHETIC_DEBT in api_text and SYNTHETIC_DEBT not in pdf_text
    assert ('| 其他债权投资 (`oth_debt_invest`) | 已报告 | 0 |' if language == 'Chinese'
            else '| Other debt investments (`oth_debt_invest`) | Reported | 0 |') in api_text


@pytest.mark.parametrize('changes,bad_digest,row_index', [
    ({'ts_code': '600000.SH'}, False, 0),
    ({'end_date': '20251231'}, False, 0),
    ({'report_type': '2'}, False, 0),
    ({'cost_fin_assets': 0}, False, 0),
    ({'debt_invest': True}, False, 0),
    ({'debt_invest': 'NaN'}, False, 0),
    ({}, True, 0),
    ({}, False, 1),
])
def test_synthetic_rejections_precede_publication(memory_publication, changes, bad_digest, row_index):
    raw = synthetic_balancesheet(row_changes=changes)
    digest = '0' * 64 if bad_digest else sha256(raw).hexdigest()
    request = SupplementalStatementRequest(raw, digest, '2026-09-30', row_index)
    with pytest.raises(ValueError, match='supplemental_report_binding_invalid'):
        export(state(), request)
    assert MemoryStore.opened == [] and memory_publication == []


@pytest.mark.usefixtures('memory_publication')
def test_no_supplemental_optin_does_not_add_public_document():
    assert URL not in export(state(), None)['complete_report.md'].decode()
