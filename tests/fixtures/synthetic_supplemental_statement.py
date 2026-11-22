"""Independent synthetic input for the supplemental observer, never provider data.

Amounts and timestamps are invented. Stock/period/scope are test selectors for
the separately reviewed PDF excerpt; they do not make these amounts financial
facts. Provider identifiers below are required schema selectors, not provenance.
No retained response is read or used to generate this one-row fixture.
"""
import json

SYNTHETIC_DEBT = '7.125000000000000019'


def synthetic_balancesheet(*, row_changes=None):
    """Return deterministic synthetic bytes, with no file or network access."""
    row = {
        'ts_code': '600919.SH', 'report_type': '1', 'comp_type': '2',
        'end_date': '20260930', 'ann_date': '20261030', 'f_ann_date': '20261030',
        'cost_fin_assets': None, 'fair_value_fin_assets': None,
        'debt_invest': SYNTHETIC_DEBT, 'oth_debt_invest': 0,
    }
    row.update(row_changes or {})
    value = {
        'artifact_contract_version': 'tushare-statement-adapter-v1',
        'provider_id': 'tushare', 'endpoint_id': 'balancesheet',
        'dataset_id': 'single_stock_history',
        'schema_identity': 'tushare_balancesheet_single_stock_v1',
        'retrieved_at': '2026-11-10T03:04:05Z', 'observed_at': '2026-11-10T03:04:05Z',
        'columns': list(row), 'rows': [row],
        'adapter_metadata': {'response_completeness': {
            'synthetic': True, 'origin': 'independent-test-generator-no-provider-request',
        }},
    }
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
