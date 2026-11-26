"""Real supplemental parsing and report export with in-memory storage, no API calls."""
import copy
import json
from hashlib import sha256
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from tradingagents import reporting
from tradingagents.evidence import (
    AnalysisOutcome,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    render_analysis_outcome,
)
from tradingagents.supplemental_observation import observe_retained_payload, render_supplemental
from tradingagents.terminal_contract import apply_terminal_contract


def payload():
    row = {'ts_code': '600919.SH', 'report_type': '1', 'comp_type': '2', 'end_date': '20251231',
           'ann_date': '20260301', 'f_ann_date': '20260301', 'cost_fin_assets': None,
           'debt_invest': '123.456789012345678901', 'oth_debt_invest': 0}
    return {'artifact_contract_version': 'tushare-statement-adapter-v1', 'provider_id': 'tushare',
            'endpoint_id': 'balancesheet', 'dataset_id': 'single_stock_history',
            'schema_identity': 'tushare_balancesheet_single_stock_v1',
            'retrieved_at': '2026-11-12T00:00:00Z', 'observed_at': '2026-11-12T00:00:00Z',
            'columns': list(row), 'rows': [row], 'adapter_metadata': {'response_completeness': {}}}


def request(value=None):
    value = payload() if value is None else value
    value['columns'] = list(value['rows'][0])
    raw = json.dumps(value, ensure_ascii=False).encode()
    return reporting.SupplementalStatementRequest(raw, sha256(raw).hexdigest(), '2025-12-31', 0)


def state():
    identity = InstrumentIdentityEvidence(
        symbol='600919.SH', venue='XSHG', instrument_kind='equity', currency='CNY',
        provenance=IdentityProvenance(provider='synthetic_registry', source_ref='synthetic:identity',
                                     retrieved_at='2026-11-11T00:00:00Z', artifact_sha256='a' * 64),
    )
    outcome = AnalysisOutcome(readiness='insufficient', reason='preflight_blocked')
    value = {'company_of_interest': '600919.SH', 'trade_date': '2026-11-11',
             'evidence_state': EvidenceState(instrument_identity=identity).model_dump(mode='json'),
             'analysis_outcome_contract': outcome.model_dump(mode='json'),
             'analysis_outcome': render_analysis_outcome(outcome),
             'evidence_gate_mode': 'enforce', 'asset_type': 'stock'}
    apply_terminal_contract(value, audit_digest='b' * 64)
    value['decision_audit_created_at'] = '2026-11-11T00:00:00Z'
    return value


class MemoryStore:
    """Storage substitute only; the actual report composer and export publisher run."""
    opened = []

    def __init__(self, root):
        self.root = Path(root)
        self.files = {}
        self.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def inspect(self, path):
        pass

    def new_directory(self, path):
        pass

    def write_bytes(self, path, data):
        assert path not in self.files
        self.files[path] = data


@pytest.fixture
def memory_publication(monkeypatch):
    MemoryStore.opened = []
    audit_inputs = []
    monkeypatch.setattr(reporting, 'ReportStore', MemoryStore)
    monkeypatch.setattr(reporting, 'write_immutable_decision_audit',
                        lambda value, *args, **kwargs: audit_inputs.append(copy.deepcopy(value)))
    return audit_inputs


def export(value, req, language='English'):
    reporting.write_report_tree(value, value['company_of_interest'], Path('synthetic-report'),
                                output_language=language, supplemental_statement=req)
    return {name.split('/', 2)[2]: data for name, data in MemoryStore.opened[-1].files.items()}


@pytest.mark.parametrize('language,chinese', [('English', False), ('Chinese', True), ('zh-CN', True)])
def test_appendix_matches_report_language_and_has_separate_markdown_sections(memory_publication, language, chinese):
    value, req = state(), request()
    before = copy.deepcopy(value)
    baseline = export(value, None, language)
    augmented = export(value, req, language)
    text = augmented['complete_report.md'].decode()
    title = '## 原始观察补充栏目（不参与分析）' if chinese else '## Supplemental retained observations (display only)'
    assert title in text
    appendix = text.split(title, 1)[1]
    assert ('### 原目标字段（仍缺失）' if chinese else '### Original target fields (still missing)') in appendix
    assert ('### 独立补充科目' if chinese else '### Separate supplemental items') in appendix
    assert ('| 字段 | 状态 | 保留原值 |' if chinese else '| Field | Status | Retained value |') in appendix
    assert '| --- | --- | --- |' in appendix
    tokens = MarkdownIt('commonmark').enable('table').parse(title + appendix)
    assert sum(token.type == 'table_open' for token in tokens) == 2
    assert sum(token.type == 'heading_open' for token in tokens) == 4
    for token in ['600919.SH', '2025-12-31', '2026-11-11', '2026-03-01',
                  '2026-11-12T00:00:00+00:00', req.expected_sha256,
                  'cost_fin_assets', 'fair_value_fin_assets', 'debt_invest', 'oth_debt_invest']:
        assert token in appendix
    assert ('未知' if chinese else 'Unknown') in appendix
    assert ('不是原始 PDF' if chinese else 'not an original PDF') in appendix
    assert ('不计入完整性评分、比率或决策' if chinese else 'not included in completeness scores, ratios or decisions') in appendix
    assert ('不是决策审计的资格认证' if chinese else 'not qualification by the decision audit') in appendix
    if not chinese:
        assert not any('\u4e00' <= char <= '\u9fff' for char in appendix)
    assert '123.456789012345678901' in appendix
    assert ('| `cost_fin_assets` | 缺失（原响应为 null） | — |' if chinese
            else '| `cost_fin_assets` | Missing (response null) | — |') in appendix
    assert ('| `fair_value_fin_assets` | 缺失（原响应无该键） | — |' if chinese
            else '| `fair_value_fin_assets` | Missing (key absent) | — |') in appendix
    assert ('| 其他债权投资 (`oth_debt_invest`) | 已报告 | 0 |' if chinese
            else '| Other debt investments (`oth_debt_invest`) | Reported | 0 |') in appendix
    assert value == before and memory_publication[0] == memory_publication[1]
    assert augmented['5_portfolio/analysis_outcome.md'] == baseline['5_portfolio/analysis_outcome.md']
    assert b'123.456789012345678901' not in augmented['5_portfolio/analysis_outcome.md']
    manifest = json.loads(augmented['manifest.json'])
    assert manifest['output_language'] == language
    assert manifest['files']['complete_report.md'] == sha256(augmented['complete_report.md']).hexdigest()


@pytest.mark.parametrize('language', ['English', 'Chinese'])
@pytest.mark.parametrize('currency,unit,explicit', [(None, None, False), ('CNY', None, False),
                                                  ('USD', '元', False), ('CNY', '元', True)])
def test_units_are_never_inferred_or_converted(memory_publication, language, currency, unit, explicit):
    value = payload()
    for key, item in [('currency', currency), ('unit', unit)]:
        if item is not None:
            value['rows'][0][key] = item
    text = export(state(), request(value), language)['complete_report.md'].decode()
    assert '123.456789012345678901' in text
    expected = (('原响应显式标注 CNY / 元' if language == 'Chinese' else 'Explicit CNY / yuan') if explicit
                else ('未知' if language == 'Chinese' else 'Unknown'))
    assert expected in text
    assert ('未换算' if language == 'Chinese' else 'no conversion') in text


@pytest.mark.parametrize('language', ['English', 'Chinese'])
@pytest.mark.parametrize('missing', ['absent', 'null'])
def test_missing_supplemental_items_stay_missing(memory_publication, language, missing):
    value = payload()
    if missing == 'absent':
        del value['rows'][0]['debt_invest']
    else:
        value['rows'][0]['debt_invest'] = None
    text = export(state(), request(value), language)['complete_report.md'].decode()
    row = next(line for line in text.splitlines() if '`debt_invest`' in line)
    assert ('缺失' if language == 'Chinese' else 'Missing') in row
    expected = 'null' if missing == 'null' else ('无该键' if language == 'Chinese' else 'key absent')
    assert expected in row
    assert row.endswith('| — |') and '123.456789' not in text


@pytest.mark.parametrize('field,value', [
    ('cost_fin_assets', 0), ('fair_value_fin_assets', False), ('debt_invest', True),
    ('debt_invest', 'NaN'), ('ts_code', '600000.SH'), ('end_date', '20241231'),
    ('ann_date', '20270101'), ('f_ann_date', '20270101'), ('report_type', '2'), ('comp_type', '1'),
])
def test_existing_payload_rejections_precede_storage(memory_publication, field, value):
    data = payload()
    data['rows'][0][field] = value
    with pytest.raises(ValueError, match='supplemental_report_binding_invalid'):
        export(state(), request(data))
    assert MemoryStore.opened == [] and memory_publication == []


@pytest.mark.parametrize('mutation', ['stock', 'identity', 'cutoff', 'digest'])
def test_invalid_report_bindings_still_fail_before_storage(memory_publication, mutation):
    value, req = state(), request()
    if mutation == 'stock':
        value['company_of_interest'] = '600000.SH'
    elif mutation == 'identity':
        value['evidence_state']['instrument_identity'] = None
    elif mutation == 'cutoff':
        value['trade_date'] = '2025-01-01'
    else:
        req = reporting.SupplementalStatementRequest(req.raw_payload, '0' * 64, req.report_period, req.row_index)
    with pytest.raises(ValueError, match='supplemental_report_binding_invalid'):
        export(value, req)
    assert MemoryStore.opened == [] and memory_publication == []


def test_default_direct_renderer_remains_chinese_and_default_report_has_no_appendix(memory_publication):
    req = request()
    observation = observe_retained_payload(
        req.raw_payload, expected_sha256=req.expected_sha256, ticker='600919.SH',
        report_period=req.report_period, as_of='2026-11-11', row_index=0,
    )
    assert '原始观察补充栏目' in render_supplemental(observation)
    value = state()
    value['supplemental_statement'] = {'untrusted': 'DO-NOT-RENDER'}
    text = export(value, None)['complete_report.md'].decode()
    assert 'Supplemental retained observations' not in text and 'DO-NOT-RENDER' not in text
