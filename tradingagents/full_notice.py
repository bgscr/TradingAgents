"""Offline review of the Eastmoney announcement boundary; no I/O or credentials."""
from datetime import date, datetime
from hashlib import sha256
import json
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ENDPOINT = 'https://np-anotice-stock.eastmoney.com/api/security/ann'

def request_url(ticker, begin, cutoff):
    if not re.fullmatch(r'[0-9]{6}\.(?:SS|SH|SZ)', ticker) or date.fromisoformat(begin) > date.fromisoformat(cutoff):
        raise ValueError('invalid_selection')
    return ENDPOINT + '?' + urlencode(dict(sr='-1', page_size=100, page_index=1, ann_type='A',
        client_source='web', f_node=0, s_node=0, stock_list=ticker[:6], begin_time=begin.replace("-", ""), end_time=cutoff.replace("-", "")))

def project(raw, received_url, ticker, begin, cutoff):
    expected = request_url(ticker, begin, cutoff)
    if expected != received_url:
        raise ValueError('response_request_mismatch')
    body = json.loads(raw)
    if body.get('success') is not True or body.get('errorCode') != 0:
        raise ValueError('supplier_response_unverified')
    data = body['data']
    rows = data['list']
    if not isinstance(rows, list) or type(data['total_hits']) is not int or data['total_hits'] != len(rows) or len(rows)>100:
        raise ValueError('coverage_unverified')
    projections = []
    for row in rows:
        if not any(c['stock_code'] == ticker[:6] for c in row['codes']):
            raise ValueError('instrument_mismatch')
        day = date.fromisoformat(row['notice_date'][:10])
        stamp = datetime.fromisoformat(row['display_time'])
        if stamp.utcoffset() is None or not (date.fromisoformat(begin) <= day <= stamp.astimezone(ZoneInfo('Asia/Shanghai')).date() <= date.fromisoformat(cutoff)):
            raise ValueError('publication_unverified')
        if not isinstance(row['title'], str) or not row['title'].strip():
            raise ValueError('missing_title')
        projections.append(dict(ticker=ticker,date=day.isoformat(),published_at=stamp.isoformat(),text=row['title']))
    return {'status':'available' if projections else 'empty','raw_sha256':sha256(raw).hexdigest(),'rows':projections}


from contextvars import ContextVar
from datetime import timedelta
from tradingagents.analysis_budget import active_budget
from tradingagents.budget_data import core_data_policy

_expected = ContextVar("full_notice_operation", default=None)

def latest_notice_owner(ticker, cutoff):
    """Resolve the grant only against this active latest Full owner's selection."""
    from tradingagents.full_session_ledger import owner
    current = owner()
    if current is None or current.child_role is not None:
        return None
    params = current.params
    if (params.get("contract") != "default-full-latest-intake-v1"
            or params.get("mode") != "default_full" or params.get("requested_date") != "latest"
            or params.get("venue") != "XSHG" or not re.fullmatch(r"[0-9]{6}\.SS", ticker)
            or params.get("ticker") != ticker.replace(".SS", ".SH")):
        return None
    from full_financial_channel import digest, financial_context, selection_record
    root = current.folder.parent
    if current.key != root.name or current.path != root / "physical-ledger/ledger.sqlite3":
        raise ValueError("notice_owner_mismatch")
    spec = dict(run_id=current.key, params=params, params_sha256=digest(params))
    record = selection_record(spec, root)
    day, registered_digest = financial_context(spec, root, cutoff, owner_side=True)
    selection = record["selection"]
    if (registered_digest != digest(record) or day != cutoff
            or selection.get("purpose") != "current_retrospective_selection"
            or not selection.get("calendar_revision")
            or selection.get("close_evidence", {}).get("trading_date") != cutoff):
        raise ValueError("notice_selection_mismatch")
    return current


def matches(request):
    return (_expected.get() is not None and request.method == 'GET'
            and request.url == _expected.get() and not request.body
            and set(k.lower() for k in request.headers) <= {
                'user-agent', 'accept', 'accept-encoding', 'connection'})

def fetch(ticker, cutoff, cache):
    import requests
    from tradingagents.notice_pages import project as project_pages
    budget = active_budget()
    if budget is None or not core_data_policy() or not re.fullmatch(r"[0-9]{6}\.SS",ticker):
        raise ValueError("unbound_full_notice")
    budget.check()
    from tradingagents.full_publication import full_sources
    source = (full_sources.get() or {}).get("stock_individual_notice_report")
    if (type(source) is not NoticeSource or source._selection != (ticker, cutoff)
            or source._cache is not cache or source._budget is not budget
            or source._mode != "retrospective"):
        raise ValueError("unbound_full_notice")
    legacy = (ticker, cutoff) == ("601328.SS", "2026-11-20")
    current = latest_notice_owner(ticker, cutoff) if source._latest else None
    if current is not source._latest or (current is None and not legacy):
        raise ValueError("unbound_full_notice")
    if "complete" in cache:
        return cache["complete"]
    end = date.fromisoformat(cutoff)
    if end.isoformat() != cutoff:
        raise ValueError("invalid_cutoff")
    begin = max(end.replace(day=1), end - timedelta(days=19))
    if current is not None:
        # Unique ledger key survives errors, cancellation and factory re-entry.
        raw = json.dumps([ticker, cutoff], separators=(",", ":")).encode()
        current.register("latest_notice_attempt", raw, sha256(raw).hexdigest(), "notice-owner", 0)
    samples=[]
    with requests.Session() as session:
        session.trust_env=False
        for page in (1,2):
            url=ENDPOINT+"?"+urlencode(dict(sr=-1,page_size=2,page_index=page,ann_type="A",client_source="web",f_node=0,s_node=0,stock_list=ticker[:6],begin_time=begin.strftime("%Y%m%d"),end_time=end.strftime("%Y%m%d")))
            token=_expected.set(url)
            try:
                response=session.get(url,timeout=8,allow_redirects=False)
            finally:
                _expected.reset(token)
            if response.status_code in (401,403):raise PermissionError("source_access_denied")
            raw=response.content
            rec=dict(requested_url=url,final_url=response.url,http_status=response.status_code,redirected=bool(response.history),length=len(raw),sha256=sha256(raw).hexdigest())
            samples.append((rec,raw))
            result=project_pages(samples,begin.strftime("%Y%m%d"),cutoff.replace("-",""),code=ticker[:6])
            if result["status"]!="incomplete_pages":break
    proof=dict(category="announcements",status=result["status"],page_sha256=[rec["sha256"] for rec,_ in samples],
               ticker=ticker, begin=begin.isoformat(), cutoff=cutoff, mode="retrospective",
               observed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), first_publication_verified=False,
               coverage=f"东财{begin.isoformat()}至{cutoff}回顾性索引，最多两页。展示时刻不作首次公开证明。",
               rows=[dict(date=row["notice_date"],text=row["title"],article=row["article"]) for row in result["rows"]])
    if result["status"]!="incomplete_pages":cache["complete"]=proof
    return proof
class NoticeSource:
    def __init__(self, ticker, cutoff, *, mode=None, latest=None):
        self._latest = latest
        self._mode = mode
        if not re.fullmatch(r"[0-9]{6}\.SS",ticker):
            raise ValueError("unreviewed_market_binding")
        date.fromisoformat(cutoff)
        self._selection = ticker, cutoff
        self._cache = {}
        self._budget = None

    def read(self, ticker, cutoff):
        if (ticker, cutoff) != self._selection:
            raise ValueError("selection_mismatch")
        current = active_budget()
        if self._budget is None:
            self._budget = current
        if self._budget is not current:
            raise ValueError('session_mismatch')
        if current is None:
            raise ValueError("bounded_session_required")
        current.check()
        if self._mode != "retrospective":
            return dict(category="announcements",status="not_run",rows=[],reason="historical_availability_unverified")
        return fetch(ticker, cutoff, self._cache)

class OptionalNoticeSkipped(Exception):
    pass




