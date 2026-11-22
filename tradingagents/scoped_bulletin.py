"""Replay one independently reviewed vendor bulletin page, limited to the collector's first 30."""
from datetime import date, datetime
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

URL = ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724"
       "&fastColumn=102&sortEnd=&pageSize=200&req_trace=1710315450384")

def project(raw, *, reviewed_capture, ticker, cutoff):
    # The caller must supply reviewed capture and instrument-profile metadata independently.
    proof=reviewed_capture
    digest=sha256(raw).hexdigest()
    if (proof["url"]!=URL or proof["method"]!="GET" or proof["http_status"]!=200
        or proof["redirected"] is not False or proof["ticker"]!=ticker or digest!=proof["sha256"]):
        raise ValueError("unbound_capture")
    day=date.fromisoformat(cutoff)
    observed=datetime.fromisoformat(proof["observed_at"])
    terms=proof["profile_terms"]
    if (observed.utcoffset() is None or observed.astimezone(ZoneInfo("Asia/Shanghai")).date()!=day
        or proof["profile_as_of"]!=cutoff or not isinstance(terms,list) or not terms
        or any(not isinstance(term,str) or not term.strip() for term in terms)):
        raise ValueError("unbound_profile_or_observation")
    rows=json.loads(raw)["data"]["fastNewsList"]
    if not isinstance(rows,list) or len(rows)>200:
        raise ValueError("invalid_bulletin_page")
    selected=[]
    for row in rows[:30]:
        title=row["title"]
        if not isinstance(title,str) or not title.strip():
            raise ValueError("invalid_headline")
        if not any(term in title for term in terms):
            continue
        shown=datetime.fromisoformat(row["showTime"])
        if shown.utcoffset() is None or shown>observed:
            raise ValueError("unverified_display_time")
        if len(selected)<3:
            selected.append(dict(date=shown.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),text=title))
    return dict(category="industry_policy",status="available" if selected else "empty",rows=selected,
                coverage="东方财富当次快讯前30条按行业词匹配，最多展示3条；无官方政策原文核验",
                snapshot_sha256=digest,observed_at=proof["observed_at"],inspected=min(len(rows),30))

class ReviewedBulletin:
    def __init__(self, raw, metadata):
        self.raw=bytes(raw)
        self.metadata=json.dumps(metadata)
    def read(self,ticker,cutoff):
        return project(self.raw,reviewed_capture=json.loads(self.metadata),ticker=ticker,cutoff=cutoff)
