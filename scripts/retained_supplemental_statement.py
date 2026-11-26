"""Display-only source lookup from this invocation's verified preparation receipt.

No acquisition, cache initialization, migration, checkpoint or latest-row policy.
Caller must supply the trusted deep copy produced after Session.verify_refresh.
Self-consistent arbitrary receipts are not provider/source authentication.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re


class SupplementalSourceRejected(ValueError):
    """Fixed local failure reason; no provider values or database exception text."""


def _require(value, reason):
    if not value:
        raise SupplementalSourceRejected(reason)


def _day(value):
    _require(type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is not None,
             'supplemental_source_date_invalid')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SupplementalSourceRejected('supplemental_source_date_invalid') from None


def _provider_day(value):
    _require(type(value) is str and re.fullmatch(r'[0-9]{8}', value) is not None,
             'supplemental_source_date_invalid')
    return _day(value[:4]+'-'+value[4:6]+'-'+value[6:])


def _aware(value):
    _require(type(value) is str, 'supplemental_source_time_invalid')
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise SupplementalSourceRejected('supplemental_source_time_invalid') from None
    _require(result.utcoffset() is not None, 'supplemental_source_time_invalid')
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'supplemental_source_duplicate_key')
        result[key] = value
    return result


def _nonfinite(_value):
    raise SupplementalSourceRejected('supplemental_source_nonfinite')


def load_retained_supplemental_statement(
    cache_directory, *, verified_view, shared_binding, ticker, as_of, report_period,
):
    """Return one exact original-payload row request or fail closed.

    Expected payload hashes come exclusively from the supplied verified receipt.
    A snapshot proves those bytes are still active at lookup time, not future cache
    immutability. The returned immutable bytes are pinned for subsequent display.
    """
    try:
        return _load(cache_directory, verified_view=verified_view, shared_binding=shared_binding,
                     ticker=ticker, as_of=as_of, report_period=report_period)
    except SupplementalSourceRejected:
        raise
    except Exception:
        raise SupplementalSourceRejected('supplemental_retained_source_unavailable') from None


def _load(cache_directory, *, verified_view, shared_binding, ticker, as_of, report_period):
    from tradingagents.budget_readiness import _readonly_database
    from tradingagents.dataflows.current_statement_diagnostics import scopes_from_binding, unavailable_view
    from tradingagents.supplemental_observation import MAX_BYTES, observe_retained_payload
    from tradingagents.reporting import SupplementalStatementRequest

    cutoff, period = _day(as_of), _day(report_period)
    _require(period <= cutoff, 'supplemental_period_after_analysis')
    _require(type(ticker) is str and re.fullmatch(r'[0-9]{6}\.(?:SS|SZ)', ticker) is not None,
             'supplemental_source_ticker_invalid')
    provider_ticker = ticker[:-3]+'.SH' if ticker.endswith('.SS') else ticker
    scope = scopes_from_binding(shared_binding)['get_balance_sheet']
    _require(type(verified_view) is dict and verified_view.get('status') == 'unavailable',
             'supplemental_gap_receipt_required')
    diagnostic = verified_view.get('diagnostics')
    expected = unavailable_view(diagnostic, ticker=ticker, tool='get_balance_sheet',
        frequency='quarterly', as_of=as_of, expected_scope=scope)
    _require(verified_view == expected, 'supplemental_gap_receipt_mismatch')
    missing = {'financial_assets_at_amortized_cost', 'financial_assets_at_fair_value'}
    periods = [item for item in diagnostic['periods']
               if item['period_end'] == report_period and item['company_type'] == 'bank'
               and item['disposition'] == 'rejected'
               and missing <= set(item['missing_core_fields'])]
    _require(bool(periods), 'supplemental_requested_gap_missing')
    matches = {}
    # Only this fixed filename under the Session-owned cache directory is read.
    database = Path(cache_directory)/'current-financial-statements.sqlite3'
    with _readonly_database(database) as connection:
        for observation in diagnostic['cache_observations']:
            digest = observation['payload_sha256']
            left, right = _day(observation['announcement_start']), _day(observation['announcement_end'])
            _require(left <= right <= cutoff, 'supplemental_receipt_range_invalid')
            # Inspect every intersecting active window BEFORE filtering a digest.
            # Otherwise a different overlapping observation or orphan could hide.
            active = connection.execute('''
                SELECT a.range_start, a.range_end, a.observation_id, o.scope,
                       o.range_start, o.range_end, o.checked_at, o.payload_sha256
                FROM active_windows a LEFT JOIN observations o ON o.id=a.observation_id
                WHERE a.scope=? AND a.range_end>=? AND a.range_start<=?
                ORDER BY a.range_start
            ''', (scope, left.isoformat(), right.isoformat())).fetchall()
            covering = []
            previous_end = None
            for active_start, active_end, observation_id, origin_scope, origin_start, origin_end, checked, stored_digest in active:
                _require(type(observation_id) is int and origin_scope == scope,
                         'supplemental_active_metadata_invalid')
                a, b, c, e = map(_day, (active_start, active_end, origin_start, origin_end))
                _require(c <= a <= b <= e, 'supplemental_active_range_mismatch')
                _aware(checked)
                _require(type(stored_digest) is str and re.fullmatch(r'[0-9a-f]{64}', stored_digest) is not None,
                         'supplemental_active_metadata_invalid')
                _require(previous_end is None or previous_end < a, 'supplemental_active_overlap')
                previous_end = b
                if a <= left <= right <= b:
                    covering.append((observation_id, checked, stored_digest))
            _require(len(covering) == 1, 'supplemental_active_observation_not_unique')
            observation_id, checked, stored_digest = covering[0]
            _require(stored_digest == digest, 'supplemental_active_digest_mismatch')
            _require(_aware(checked) == _aware(observation['checked_at']), 'supplemental_checked_at_mismatch')
            records = connection.execute('SELECT payload FROM observations WHERE id=? AND scope=? AND payload_sha256=?',
                                         (observation_id, scope, digest)).fetchmany(2)
            _require(len(records) == 1, 'supplemental_active_observation_not_unique')
            raw = records[0][0]
            _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, 'supplemental_payload_type_or_size')
            _require(stored_digest == digest and sha256(raw).hexdigest() == digest,
                     'supplemental_payload_digest_mismatch')
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                 parse_float=Decimal, parse_constant=_nonfinite)
            _require(type(payload) is dict and type(payload.get('rows')) is list,
                     'supplemental_payload_shape_invalid')
            _require(_aware(payload.get('retrieved_at')) == _aware(observation['retrieved_at'])
                     and _aware(payload.get('observed_at')) == _aware(observation['observed_at']),
                     'supplemental_payload_time_mismatch')
            # Enumerate the untouched raw rows: never filter/reindex before binding.
            for index, row in enumerate(payload['rows']):
                _require(type(row) is dict, 'supplemental_payload_row_invalid')
                if row.get('ts_code') != provider_ticker or row.get('end_date') != period.strftime('%Y%m%d'):
                    continue
                if not (type(row.get('report_type')) in (str, int) and row['report_type'] in ('1', 1)
                        and type(row.get('comp_type')) in (str, int) and row['comp_type'] in ('2', 2)):
                    continue
                announcement = _provider_day(row.get('ann_date'))
                final_announcement = _provider_day(row.get('f_ann_date'))
                if not left <= announcement <= right:
                    continue
                _require(any(item['ann_date'] == announcement.isoformat()
                             and item['f_ann_date'] == final_announcement.isoformat() for item in periods),
                         'supplemental_row_gap_binding_mismatch')
                # Full reviewed narrow decoder checks source schema, bank/consolidated
                # scope, dates, original-null fields, finite Decimal and unknown units.
                observe_retained_payload(raw, expected_sha256=digest, ticker=provider_ticker,
                    report_period=report_period, as_of=as_of, row_index=index)
                key = (digest, index)
                matches[key] = SupplementalStatementRequest(raw, digest, report_period, index)
    _require(len(matches) == 1, 'supplemental_row_not_unique')
    return next(iter(matches.values()))
