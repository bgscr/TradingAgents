"""Replay exact captured pages as a vendor index; never assert first-publication time."""
from datetime import datetime
from hashlib import sha256
import json,re
from urllib.parse import urlencode
def project(samples,begin,cutoff,size=2,*,code="601328"):
    if not re.fullmatch(r"[0-9]{6}",code):raise ValueError("unsupported_issuer")
    start=datetime.strptime(begin,"%Y%m%d").date()
    end=datetime.strptime(cutoff,"%Y%m%d").date()
    if start>end or not samples or not 1<=size<=100:raise ValueError("invalid_window_or_pages")
    result=[]; seen=set(); total=None
    for index,(receipt,raw) in enumerate(samples,1):
        url="https://np-anotice-stock.eastmoney.com/api/security/ann?"+urlencode(dict(sr=-1,page_size=size,page_index=index,ann_type="A",client_source="web",f_node=0,s_node=0,stock_list=code,begin_time=begin,end_time=cutoff))
        if (receipt["requested_url"]!=url or receipt["final_url"]!=url or receipt["http_status"]!=200 or receipt["redirected"] is not False or receipt["length"]!=len(raw) or receipt["sha256"]!=sha256(raw).hexdigest()):
            raise ValueError("unbound_response")
        obj=json.loads(raw)
        if type(obj["success"]) is not int or obj["success"]!=1 or obj["error"]!="":raise ValueError("source_error")
        page=obj["data"];hits=page["total_hits"]
        if (type(hits) is not int or hits<0 or page["page_index"]!=index or page["page_size"]!=size or (total is not None and hits!=total)):raise ValueError("page_binding")
        total=hits
        rows=page["list"]
        if not isinstance(rows,list) or len(rows)!=min(size,max(0,hits-(index-1)*size)):raise ValueError("short_page")
        for row in rows:
            ident=row["art_code"]
            if not re.fullmatch(r"AN\d+",ident) or ident in seen:raise ValueError("duplicate_or_invalid_article")
            seen.add(ident)
            if not any(x["stock_code"]==code and x["market_code"]=="1" and x["ann_type"]=="A,SHA" for x in row["codes"]):raise ValueError("wrong_market")
            day=datetime.strptime(row["notice_date"],"%Y-%m-%d %H:%M:%S").date()
            if not start<=day<=end or not row["title"].strip():raise ValueError("invalid_announcement")
            datetime.strptime(row["display_time"],"%Y-%m-%d %H:%M:%S:%f")
            result.append(dict(article=ident,title=row["title"],notice_date=str(day),vendor_display_time=row["display_time"]))
    complete=len(samples)==max(1,(total+size-1)//size) and len(result)==total
    return dict(status=("available" if total else "empty") if complete else "incomplete_pages",total_hits=total,unique_rows=len(result),rows=result,first_publication_verified=False)
