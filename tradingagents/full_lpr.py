"""Bank LPR evidence. Only exact anonymous operations may use the optional wire."""
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import re


HISTORY = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN"
NOVEMBER = "https://www.chinamoney.com.cn/chinese/rdgz/20261120/3453464.html"
_operation = ContextVar("bank_lpr_operation", default=None)

def matches(request):
    return (request.url in (HISTORY, NOVEMBER) and request.url == _operation.get()
            and request.method == "GET" and not request.body
            and set(k.lower() for k in request.headers) <= {
                "user-agent", "accept", "accept-encoding", "connection"})

def rate(value):
    if not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,4})?", str(value)):
        raise ValueError("invalid_rate")
    return Decimal(str(value))

class BankLpr:
    def __init__(self, ticker, cutoff, *, article_pin=None, retrospective=False):
        if ticker != "601328.SS":
            raise ValueError("unreviewed_instrument")
        self.selection = ticker, date.fromisoformat(cutoff)
        # Supplied by the reviewed composition root; never derived from inbound bytes.
        self.article_pin = article_pin
        self.retrospective = retrospective
        self.cache = {}
        self.budget = None

    def _get(self, url):
        from tradingagents.analysis_budget import active_budget
        from tradingagents.budget_data import core_data_policy
        import requests
        budget = active_budget()
        if budget is None or self.budget is not budget or not core_data_policy():
            raise ValueError("bounded_session_required")
        if url != HISTORY and not (url == NOVEMBER and self.selection[1] == date(2026,11,20) and self.article_pin):
            raise ValueError("unbound_lpr_operation")
        budget.check()
        if url in self.cache:
            return self.cache[url]
        with requests.Session() as session:
            session.trust_env = False
            token = _operation.set(url)
            try:
                response = session.get(url, allow_redirects=False, timeout=10)
            finally:
                _operation.reset(token)
        if response.status_code in (401,403):
            raise PermissionError("lpr_access_denied")
        if response.url != url or response.status_code != 200:
            raise ValueError("lpr_source_failure")
        self.cache[url] = response.content
        return response.content

    def read(self,ticker,cutoff):
        from tradingagents.analysis_budget import active_budget
        from tradingagents.budget_data import core_data_policy
        budget = active_budget()
        target = date.fromisoformat(cutoff)
        if self.selection != (ticker,target) or not budget or not core_data_policy():
            raise ValueError("lpr_selection_mismatch")
        if self.budget is not None and self.budget is not budget:
            raise ValueError("lpr_session_mismatch")
        self.budget = budget
        budget.check()
        raw = self._get(HISTORY)
        data = json.loads(raw)
        head, meta = data["head"], data["data"]
        stamp = datetime.strptime(head["tstext"], "%Y-%m-%d %H:%M:%S")
        begin,end = map(date.fromisoformat,(meta["startDateCN"],meta["endDateCN"]))
        if (head["provider"] != "CWAP" or head["rep_code"] != "200" or
            meta["baseCurveCfgList"] != ["1Y","5Y"] or begin > end or end > stamp.date()):
            raise ValueError("lpr_contract")
        proof = dict(category="bank_lpr",status="unavailable",rows=[],history_url=HISTORY,
                     history_sha256=sha256(raw).hexdigest(),window=[str(begin),str(end)],
                     provider_timestamp=head["tstext"])
        if not begin <= target <= end or (stamp.date() > target and not self.retrospective):
            return dict(proof,reason="window_or_point_in_time_unverified")
        values = {}
        for entry in data["records"]:
            day=date.fromisoformat(entry["showDateCN"])
            if day in values or not begin <= day <= end:
                raise ValueError("lpr_row_binding")
            values[day]=rate(entry["1Y"]),rate(entry["5Y"])
        if target not in values:
            return dict(proof,reason="no_verified_publication_for_date")
        one,five=values[target]
        proof.update(observed_date=str(target),observed_rates=[str(one),str(five)])
        if target != date(2026,11,20) or not self.article_pin:
            return dict(proof,reason="official_article_unverified")
        article=self._get(NOVEMBER)
        digest=sha256(article).hexdigest()
        # The independent pin binds exact bytes to a reviewed publication date and rates.
        if (digest,str(target),one,five) != self.article_pin:
            return dict(proof,reason="article_projection_mismatch")
        return dict(proof,status="available",coverage="官方原文与滚动历史表核对；服务时标 "+head["tstext"],retrospective=stamp.date()>target,article_url=NOVEMBER,article_sha256=digest,
                    rows=[dict(date=str(target),text=f"银行贷款市场报价利率：1年期 {one}%，5年期以上 {five}%。")])


