"""Optional Eastmoney order-class flow from a verified retrospective cache.

Amounts and prices are excluded pending independent unit verification. Ratios
are not statement facts, Tushare all-order moneyflow, or investment-rule inputs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from tradingagents import analysis_budget as budget_api
from tradingagents.powershell_news import _ordinary_file

VERSION = 'eastmoney-powershell-fundflow-v1'
ENDPOINT = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
_ACTIVE = ContextVar('powershell_fundflow', default=None)
MAX_BYTES = 1048576
FIELDS = ('date', 'main_pct', 'small_pct', 'medium_pct', 'large_pct', 'superlarge_pct', 'change_pct')
_REASONS = {'available_partial', 'empty', 'runtime_missing', 'transport_unavailable',
            'budget_unavailable', 'invalid_response', 'business_error', 'stock_mismatch', 'date_conflict', 'numeric_invalid'}


class FundflowStop(BaseException):
    """Fixed integrity/authority failure, never optional-provider prose."""


class Rejected(ValueError):
    def __init__(self, reason='invalid_response'):
        super().__init__(reason if reason in _REASONS else 'invalid_response')


def _symbol(value):
    if type(value) is not str or not re.fullmatch(r'(6[0-9]{5}\.(SH|SS)|[03][0-9]{5}\.SZ)', value):
        raise FundflowStop('fundflow_selection_invalid')
    return value.replace('.SS', '.SH')


def _day(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        raise Rejected()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Rejected() from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def matches_retained_url(url, ticker, stamp):
    if type(url) is not str or type(stamp) is not str:
        return False
    values = parse_qs(urlsplit(url).query).get("ut", [])
    if len(values) != 1 or hashlib.sha256(values[0].encode()).hexdigest() != "bde29c306df22b9966f2558543071ed18b51b80630f8d52ee49d324dade105f0":
        return False
    symbol = _symbol(ticker)
    stamp = str(int(time.time() * 1000)) if stamp is None else stamp
    if type(stamp) is not str or not re.fullmatch('[0-9]{13}', stamp):
        raise FundflowStop('fundflow_timestamp_invalid')
    return url == ENDPOINT + '?' + urlencode({
        'lmt': '0', 'klt': '101', 'secid': ('1.' if symbol.endswith('.SH') else '0.') + symbol[:6],
        'fields1': 'f1,f2,f3,f7', 'fields2': ','.join('f' + str(i) for i in range(51, 66)),
        'ut': values[0], '_': stamp})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected()
        result[key] = value
    return result


def _number(value):
    if type(value) is not str or len(value) > 30 or not re.fullmatch(r'-?[0-9]{1,19}(?:\.[0-9]{1,6})?', value):
        raise Rejected('numeric_invalid')
    number = Decimal(value)
    if abs(number) > Decimal('1e18'):
        raise Rejected('numeric_invalid')
    return format(number.normalize(), 'f')


def project_response(content, ticker, start, end):
    """Project only expected dates/numbers; unknown keys/values never survive."""
    symbol = _symbol(ticker)
    first, last = _day(start), _day(end)
    if first > last or type(content) is not bytes or len(content) > MAX_BYTES:
        raise Rejected()
    try:
        # Shared bounded JSON structure checks, with no Tushare schema reuse.
        from tradingagents.moneyflow import _json_depth, _json_nodes, MoneyflowUnavailable
        _json_depth(content)
        obj = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(Rejected()))
        _json_nodes(obj)
    except (UnicodeError, ValueError, RecursionError, MoneyflowUnavailable):
        raise Rejected() from None
    if type(obj) is not dict or type(obj.get('rc')) is not int:
        raise Rejected()
    if obj['rc'] != 0:
        raise Rejected('business_error')
    data = obj.get('data')
    if type(data) is not dict or type(data.get('code')) is not str or data['code'] != symbol[:6]:
        raise Rejected('stock_mismatch')
    market = 1 if symbol.endswith('.SH') else 0
    if type(data.get('market')) is not int or data['market'] != market:
        raise Rejected('stock_mismatch')
    lines = data.get('klines')
    if type(lines) is not list or len(lines) > 2500:
        raise Rejected()
    unique = {}
    older = future = duplicates = 0
    for line in lines:
        if type(line) is not str or len(line) > 512:
            raise Rejected()
        cells = line.split(',')
        if not 13 <= len(cells) <= 15:
            raise Rejected()
        day = _day(cells[0])
        # f64/f65 have no adopted meaning; their presence/value cannot become
        # a fact or block otherwise valid core fields.
        values = [_number(c) for c in cells[1:13]]
        if any(abs(Decimal(x)) > 100 for x in values[5:10]) or Decimal(values[10]) <= 0:
            raise Rejected('numeric_invalid')
        # Compare all expected values for conflicting duplicates, but retain
        # only the documented percentage fields. No unknown-unit amounts.
        projected = [cells[0], *values]
        previous = unique.get(day)
        if previous is not None:
            if previous != projected:
                raise Rejected('date_conflict')
            duplicates += 1
            continue
        unique[day] = projected
    rows = []
    for day, row in sorted(unique.items()):
        if day > last:
            future += 1
        elif day < first:
            older += 1
        else:
            rows.append([row[0], *row[6:11], row[12]])
    return {'rows': rows, 'counts': {'returned': len(lines), 'duplicate': duplicates,
            'older': older, 'future': future, 'accepted': len(rows)}}


def configured_owner(config):
    enabled = config.get('bounded_powershell_fundflow_enabled', False)
    if type(enabled) is not bool:
        raise FundflowStop('fundflow_configuration_invalid')
    if not enabled:
        return None
    owner = _ACTIVE.get()
    if owner is None or config.get('analysis_skip_optional_fundamentals') is not True:
        raise FundflowStop('fundflow_context_missing')
    return owner


class PowerShellFundflow:
    def __init__(self, executable, script, ticker, as_of, *, after_report=False, approved_files=None):
        self.executable, self.script = Path(executable), Path(script)
        self.symbol, self.end = _symbol(ticker), _day(as_of).isoformat()
        self.start = (_day(as_of) - timedelta(days=7)).isoformat()
        if self.executable.name.lower() != 'pwsh.exe' or self.script.name != 'Get-PublicFundflow.ps1':
            raise FundflowStop('fundflow_path_invalid')
        self.lock, self.used, self.value = threading.Lock(), False, None
        self.identity_sha = self.budget = None
        self.after_report = after_report
        self.approved_files = approved_files
        self._pending = None
        self._cancelled = threading.Event()
        self._identity_lock = threading.Lock()
        self.metadata = {'status': 'not_attempted'}
        try:
            self.binding = (_ordinary_file(self.executable), _ordinary_file(self.script))
        except FileNotFoundError:
            self.binding = None

    def bind_identity(self, identity):
        if (not identity.is_authoritative or _symbol(identity.symbol) != self.symbol
                or identity.instrument_kind.value != 'equity' or identity.currency != 'CNY'
                or identity.venue != ('XSHG' if self.symbol.endswith('.SH') else 'XSHE')):
            raise FundflowStop('fundflow_identity_invalid')
        digest = _digest(identity.model_dump(mode='json'))
        # Report audit rebinds the same immutable identity during collection.
        with self._identity_lock:
            if self.identity_sha is not None and self.identity_sha != digest:
                raise FundflowStop('fundflow_identity_changed')
            self.identity_sha = digest

    def schedule_after_report(self, key, acquire):
        budget = budget_api.active_budget()
        if budget is None or self.identity_sha is None:
            raise FundflowStop('fundflow_binding_required')
        budget.check()
        with self.lock:
            if self._pending is None:
                self._pending = (key, budget, acquire)
            elif self._pending[:2] != (key, budget):
                raise FundflowStop('fundflow_deferred_binding_changed')

    def cancel_collection(self):
        self._cancelled.set()

    def _check_collection(self, budget):
        if self._cancelled.is_set():
            raise FundflowStop('fundflow_collection_cancelled')
        return budget.check()

    def collect_after_report(self):
        from tradingagents.dataflows.acquisition import AcquisitionFailure, AcquisitionUnavailableReason

        with self.lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        _, budget, acquire = pending
        if budget_api.active_budget() is not budget:
            raise FundflowStop('fundflow_budget_changed')
        self._check_collection(budget)
        try:
            acquire()
        except AcquisitionFailure as error:
            if error.reason not in (AcquisitionUnavailableReason.NO_DATA,
                                     AcquisitionUnavailableReason.PROVIDER_ERROR,
                                     AcquisitionUnavailableReason.UPSTREAM_BUSY):
                raise
            self.metadata['unavailable_reason'] = error.error_code or error.reason.value

    def read_basic_cache(self, ticker, as_of, store_config, *, request_key, mode, pinned_digest):
        from tradingagents.dataflows.acquisition import AcquisitionFailure, AcquisitionUnavailableReason
        from tradingagents.retrospective_observations import retrospective_observation

        if not self.after_report or self.identity_sha is None or _symbol(ticker) != self.symbol or as_of != self.end:
            raise FundflowStop('fundflow_binding_required')
        budget = budget_api.active_budget()
        if budget is None:
            raise FundflowStop('fundflow_binding_required')
        self._check_collection(budget)
        cached = None
        if mode == 'retrospective':
            try:
                store_config.database_path.stat()
            except FileNotFoundError:
                pass
            else:
                cached = retrospective_observation(
                    store_config, request_key=request_key, provider='akshare',
                    operation='financial-fundamentals:stock_individual_fund_flow.powershell',
                    as_of=_day(as_of),
                    receipt_time=lambda value: self.cached_receipt_time(value, pinned_digest),
                )
        self._check_collection(budget)
        if cached is None:
            self.metadata = {'status': 'retrospective_cache_unavailable', 'attempted': False}
            reason = ('fundflow_historical_availability_unverified' if mode == 'as_known_on_date'
                      else 'fundflow_cache_unavailable')
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA, retryable=False, error_code=reason)
        self.value = self._observation('available_partial', cached['projection'], cached['received_at'])
        self.metadata = {'status': 'retrospective_cache_hit', 'attempted': False}
        return self._tool_result()

    def acquire(self, ticker, as_of, *, physical_request):
        raise FundflowStop("fundflow_live_transport_unavailable")

    def _observation(self, status, projection, received_at):
        value = {'contract': VERSION, 'symbol': self.symbol, 'as_of': self.end, 'start': self.start,
            'identity_sha256': self.identity_sha, 'provider': 'eastmoney.push2his', 'transport': 'powershell',
            'endpoint': ENDPOINT, 'received_at': received_at, 'provenance': 'retrospective_backfill',
            'amounts_and_prices': 'excluded_unit_unverified',
            'ratio_unit': 'percent', 'status': status, **projection}
        value['normalized_sha256'] = _digest(value)
        return value

    def cached_receipt_time(self, result, pinned_digest):
        if type(result) is not dict or set(result) not in (
                {'received_at', 'projection'}, {'received_at', 'projection', 'normalized_sha256'}):
            return None
        projection = result['projection']
        if type(projection) is not dict or set(projection) != {'counts', 'rows'} or not projection['rows']:
            return None
        value = self._observation('available_partial', projection, result['received_at'])
        # Older physical results lack the original full identity and version.
        # An operator may supply the digest from a separately verified receipt.
        digest = value['normalized_sha256']
        if result.get('normalized_sha256', pinned_digest) != digest or pinned_digest not in (None, digest):
            return None
        if render_observation(value, self.symbol, self.end) == _INVALID_OBSERVATION:
            return None
        return result['received_at']

    def _tool_result(self):
        from tradingagents.dataflows.acquisition import AcquisitionFailure, AcquisitionUnavailableReason
        if self.value['status'] != 'available_partial':
            raise AcquisitionFailure(reason=(AcquisitionUnavailableReason.NO_DATA if self.value['status'] == 'empty'
                else AcquisitionUnavailableReason.PROVIDER_ERROR), retryable=False,
                error_code='eastmoney_fundflow_' + self.value['status'])
        rendered = render_observation(self.value, self.symbol, self.end)
        if rendered == _INVALID_OBSERVATION:
            raise FundflowStop('fundflow_projection_invalid')
        return ('# Company Fundamentals for ' + self.symbol.replace('.SH', '.SS')
                + '\n# Primary source: Eastmoney push2his / PowerShell; individual fund flow only\n'
                + '# Data retrieved on: ' + self.value['received_at'] + '\n'
                + rendered)

    def summary(self):
        return {'transport': copy.deepcopy(self.metadata), 'observation': copy.deepcopy(self.value),
                'raw_body_retained': False, 'automatic_retry': False,
                'executable_sha256': self.binding[0] if self.binding else None,
                'script_sha256': self.binding[1] if self.binding else None}


_INVALID_OBSERVATION = '东方财富个股资金流：绑定无效，未采用。'


def render_observation(value, ticker, as_of):
    invalid = _INVALID_OBSERVATION
    try:
        expected = {'contract', 'symbol', 'as_of', 'start', 'identity_sha256', 'provider', 'transport',
                    'endpoint', 'received_at', 'provenance', 'amounts_and_prices', 'ratio_unit',
                    'status', 'rows', 'counts', 'normalized_sha256'}
        if (type(value) is not dict or set(value) != expected or value.get('contract') != VERSION
                or value.get('symbol') != _symbol(ticker) or value.get('as_of') != as_of
                or value.get('normalized_sha256') != _digest({k: v for k, v in value.items() if k != 'normalized_sha256'})
                or type(value.get('status')) is not str or value['status'] not in _REASONS
                or value['provider'] != 'eastmoney.push2his' or value['transport'] != 'powershell'
                or value['endpoint'] != ENDPOINT or value['provenance'] != 'retrospective_backfill'
                or value['amounts_and_prices'] != 'excluded_unit_unverified' or value['ratio_unit'] != 'percent'
                or value['start'] != (_day(as_of) - timedelta(days=7)).isoformat()
                or type(value['identity_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', value['identity_sha256'])
                or type(value['rows']) is not list or len(value['rows']) > 8):
            return invalid
        if value['received_at'] is not None:
            if type(value['received_at']) is not str or len(value['received_at']) > 40:
                return invalid
            received = datetime.fromisoformat(value['received_at'])
            if received.utcoffset() != timedelta(0) or received > datetime.now(timezone.utc) + timedelta(seconds=1):
                return invalid
        available = value['status'] in {'available_partial', 'empty'}
        if available:
            counts = value['counts']
            if (value['received_at'] is None or type(counts) is not dict
                    or set(counts) != {'returned', 'duplicate', 'older', 'future', 'accepted'}
                    or any(type(n) is not int or not 0 <= n <= 2500 for n in counts.values())
                    or counts['accepted'] != len(value['rows'])
                    or counts['returned'] != sum(counts[k] for k in ('duplicate', 'older', 'future', 'accepted'))
                    or (value['status'] == 'empty') != (len(value['rows']) == 0)):
                return invalid
        elif value['rows'] or value['counts'] is not None:
            return invalid
        # Recheck projected rows, independently of hash consistency.
        for row in value['rows']:
            if type(row) is not list or len(row) != 7 or not value['start'] <= row[0] <= value['as_of']:
                return invalid
            _day(row[0])
            for cell in row[1:]:
                _number(cell)
            if any(abs(Decimal(cell)) > 100 for cell in row[1:6]):
                return invalid
        dates = [r[0] for r in value['rows']]
        if dates != sorted(set(dates)):
            return invalid
        lines = ['## 东方财富个股资金流（独立辅助观察）',
            '本项不是财报指标，不填补任何财报缺项，不参与最终评级。主力及订单分类与 Tushare 全单净额口径不同，不相加、不替代。',
            f"股票：{value['symbol']}；窗口：{value['start']} 至 {value['as_of']}；状态：{value['status']}。",
            '来源：东方财富 push2his；传输：PowerShell；单次请求，无分页补采。',
            '金额和收盘价单位倍率尚未独立核实，已排除，不进入模型或报告；仅提供净占比及涨跌幅，单位为%。',
            f"采集时刻（UTC）：{value['received_at'] or '未取得'}；历史回补不证明截至日当时已公开。",
            '只覆盖本次返回的合格记录，未验证完整交易日覆盖；缺失不补零。']
        if value['counts'] is not None:
            c = value['counts']
            lines.append(f"返回 {c['returned']} 条；重复 {c['duplicate']}；窗前 {c['older']}；未来 {c['future']}；准入 {c['accepted']}。")
        if dates:
            lines += [f'实际记录起止：{dates[0]} 至 {dates[-1]}；记录数：{len(dates)}。',
                '| 日期 | 主力净占比% | 小单净占比% | 中单净占比% | 大单净占比% | 超大单净占比% | 涨跌幅% |',
                '|' + '---|' * 7]
            lines += ['| ' + ' | '.join(row) + ' |' for row in value['rows']]
        lines += [f"投影绑定：{value['normalized_sha256']}；身份绑定：{value['identity_sha256']}。",
                  '仅规范投影被留存；未保留完整响应，不是原始字节回放证据。']
        return '\n'.join(lines)
    except (KeyError, TypeError, ValueError, FundflowStop):
        return invalid


def fundflow_markdown(state):
    value = state.get('eastmoney_fundflow_observation')
    if value is None:
        return ''
    from tradingagents.evidence import EvidenceState
    try:
        identity = EvidenceState.model_validate(state.get('evidence_state', {})).instrument_identity
        if identity is None or not identity.is_authoritative or value.get('identity_sha256') != _digest(identity.model_dump(mode='json')):
            return '东方财富个股资金流：绑定无效，未采用。'
    except (ValueError, TypeError, AttributeError):
        return '东方财富个股资金流：绑定无效，未采用。'
    return render_observation(value, state.get('company_of_interest'), state.get('trade_date'))


@contextmanager
def powershell_fundflow_scope(owner):
    if _ACTIVE.get() is not None:
        raise FundflowStop('fundflow_scope_nested')
    token = _ACTIVE.set(owner)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
