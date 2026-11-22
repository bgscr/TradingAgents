"""Read only owner-reviewed, per-source local projections; no transport or model calls."""
from datetime import date,datetime
from decimal import Decimal
from hashlib import sha256
import json,re
from pathlib import Path
from zoneinfo import ZoneInfo

FIELDS={
 "stock_lhb_detail_em":("listing_reason",),
 "stock_hot_rank_latest_em":("rank",),
 "stock_hot_keyword_em":("keyword",),
 "stock_zh_a_disclosure_report_cninfo":("announcement_title",),
 "stock_individual_info_em":("industry",),
 "stock_sector_fund_flow_rank":("sector","rank"),
}
FLOW="stock_individual_fund_flow"
MARGIN="stock_margin_detail_sse"
HISTORY={
 FLOW:("powershell-fundflow.json","10c7c2483be4ca82919db31dc1eea85403da77bf3a4308ebad0a85d0b12efdac"),
 MARGIN:("margin-balance.json","4a1b87564247598e8c35b137136d64a5a37d79a3210c7c261feb683e02c4fd9c")}
SZ_MARGIN = "stock_margin_detail_szse"

def margin_source(ticker):
    return SZ_MARGIN if ticker.endswith(".SZ") else MARGIN

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous_evidence")
        result[key] = value
    return result

def reviewed_pair(source, obj, ticker, cutoff):
    from tradingagents import margin_balance, powershell_fundflow as flow
    binding = getattr(source, "binding", None)
    if (not binding or obj.get("binding") != binding or binding["symbol"] != flow._symbol(ticker)
            or binding["as_of"] != cutoff or binding["trade_date"] != cutoff
            or obj.get("source") != source.name):
        raise ValueError("wrong_snapshot_or_source")
    ratio = source.name == FLOW
    provider = "eastmoney.push2his" if ratio else "tushare.margin_detail"
    if (not ratio and source.name != margin_source(ticker)) or obj.get("provider") != provider:
        raise ValueError("wrong_provider")
    received = datetime.fromisoformat(obj["received_at"])
    from datetime import timezone
    if (received.utcoffset() != timezone.utc.utcoffset(received) or received > datetime.now(timezone.utc)
            or received.astimezone(ZoneInfo("Asia/Shanghai")).date() <= date.fromisoformat(cutoff)):
        raise ValueError("retrospective_receipt_time")
    raw = source.path.with_suffix(".bin").read_bytes()
    if sha256(raw).hexdigest() != obj["raw_sha256"]:
        raise ValueError("unbound_raw")
    parsed = json.loads(raw, object_pairs_hook=unique_object)
    if obj.get("http_status") != 200 or obj.get("physical_attempts") != 1 or type(obj["physical_attempts"]) is not int:
        raise ValueError("unverified_capture")
    if ratio:
        route = flow.request_url(ticker, obj["request_stamp"])
        delay = received.timestamp() - int(obj["request_stamp"]) / 1000
        if not 0 <= delay <= 21:
            raise ValueError("request_time_unbound")
        expected = {"method":"GET","url":route}
    else:
        expected = {"method":"POST","endpoint":"https://api.waditu.com/dataapi/margin_detail",
                    "ts_code":binding["symbol"],"trade_date":cutoff.replace("-",""),
                    "fields":list(margin_balance.FIELDS)}
    if obj.get("request") != expected:
        raise ValueError("unverified_request")
    result = dict(status="empty", rows=[], provider=provider, received_at=obj["received_at"],
                  snapshot_id=binding["snapshot_id"], raw_sha256=obj["raw_sha256"],
                  coverage="事后回补；不证明交易日当时已公开。")
    if not ratio:
        rows = margin_balance.decode(raw, binding)
        result.update(status="available" if rows else "empty",
                      rows=[dict(date=day,text="融资余额 "+amount+" 元") for day,amount in rows])
        return result
    if (flow.render_observation(parsed,ticker,cutoff) == flow._INVALID_OBSERVATION
            or parsed["identity_sha256"] != binding["identity_sha256"]
            or parsed["received_at"] != obj["received_at"] or parsed["counts"] is None
            or parsed["counts"]["duplicate"] or parsed["status"] not in {"empty","available_partial"}):
        raise ValueError("invalid_ratio_projection")
    if parsed["status"] == "empty":
        if parsed["counts"]["returned"]:
            raise ValueError("no_target_coverage")
        return result
    if not any(row[0] == cutoff for row in parsed["rows"]):
        raise ValueError("no_target_coverage")
    result.update(status="available_partial", requested_ratio_verified=True,
                  rows=[dict(date=row[0],text="主力净流入占比 "+row[1]+"%") for row in parsed["rows"]])
    return result

def reviewed_bytes(path,digest):
    raw=path.read_bytes()
    if sha256(raw).hexdigest()!=digest:
        raise ValueError("unexpected_source_bytes")
    return json.loads(raw, object_pairs_hook=unique_object)
class LocalSource:
    def __init__(self,name,path=None,digest=None):
        self.name,self.path,self.digest=name,path,digest
    def bind_snapshot(self,state,ticker,cutoff):
        from tradingagents.margin_balance import _binding, _require_active_snapshot
        from tradingagents.margin_balance import shared
        try:
            binding=_binding(state,ticker,cutoff,cutoff)
            _require_active_snapshot(state)
        except (shared.MoneyflowUnavailable,ValueError,KeyError,TypeError,AttributeError):
            binding=None
        self.binding=binding
    def read(self,ticker,cutoff,mode):
        if not re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)",ticker):
            raise ValueError("unsupported_source_selection")
        item=dict(status="unavailable",rows=[],reason="reviewed_capture_missing")
        if self.path is None or not self.path.is_file():return item
        if (self.name in HISTORY or self.name == SZ_MARGIN) and mode!="retrospective":
            return dict(item,reason="historical_availability_unverified")
        obj=reviewed_bytes(self.path,self.digest)
        if self.name in (FLOW,MARGIN,SZ_MARGIN) and obj.get("contract")=="reviewed-bound-observation-v1":
            return reviewed_pair(self,obj,ticker,cutoff)
        item=dict(status="available",rows=[],raw_sha256=self.digest)
        if self.name==MARGIN and obj.get("contract")=="reviewed-margin-wire-v1":
            from tradingagents.margin_balance import decode
            if not getattr(self,"binding",None) or any(obj.get(k)!=v for k,v in self.binding.items()):
                raise ValueError("unbound_margin_snapshot")
            payload=self.path.with_suffix(".bin").read_bytes()
            if sha256(payload).hexdigest()!=obj["raw_sha256"]:raise ValueError("unbound_raw")
            rows=decode(payload,dict(symbol=obj["symbol"],trade_date=cutoff,as_of=cutoff))
            return dict(item,status="available" if rows else "empty",coverage="已审融资响应与本轮所有者验证的快照绑定一致",snapshot_id=obj["snapshot_id"],rows=[dict(date=day,text="融资余额 "+value+" 元") for day,value in rows])
        if self.name in HISTORY:
            if self.digest != HISTORY[self.name][1] or ticker!="601328.SS" or cutoff!="2026-11-20":raise ValueError("prior_receipt_wrong_date")
            ob=obj["observation"]
            if (ob["symbol"],ob["as_of"],obj["basic_export_id"])!=("601328.SH",cutoff,"57095674a5a84c7caeee07f86fc1824f"):
                raise ValueError("prior_run_binding")
            item.update(provider=ob["provider"],received_at=ob["received_at"],coverage="已核对历史有界回执，事后回补。")
            if self.name==MARGIN:
                if ob["status"]!="empty" or ob["provider"]!="tushare.margin_detail" or ob["trade_date"]!=cutoff:
                    raise ValueError("unexpected_margin_receipt")
                return dict(item,status="empty",reason="no_financing_balance_returned")
            if ob["provider"]!="eastmoney.push2his" or ob["ratio_unit"]!="percent" or ob["amounts_and_prices"]!="excluded_unit_unverified":
                raise ValueError("unexpected_flow_receipt")
            for day,*values in ob["rows"]:
                if ob["start"]<=day<=cutoff and Decimal(values[0]).is_finite():
                    item["rows"].append(dict(date=day,text="主力净流入占比 "+values[0]+"%"))
            if not item["rows"] or ob["status"]!="available_partial" or not any(row["date"]==cutoff for row in item["rows"]):
                raise ValueError("incomplete_ratio_receipt")
            return dict(item,status="available_partial",requested_ratio_verified=True,start=ob["start"],as_of=cutoff,original_status=ob["status"])
        if (obj["contract"],obj["source"],obj["ticker"],obj["cutoff"])!=("reviewed-source-projection-v1",self.name,ticker,cutoff):
            raise ValueError("wrong_projection")
        stamp=datetime.fromisoformat(obj["observed_at"])
        if stamp.utcoffset() is None or stamp.astimezone(ZoneInfo("Asia/Shanghai")).date()!=date.fromisoformat(cutoff):
            raise ValueError("source_observation_date")
        if not re.fullmatch("[0-9a-f]{64}",obj["raw_sha256"]):raise ValueError("raw_reference_required")
        original=self.path.with_suffix(".bin").read_bytes()
        if sha256(original).hexdigest()!=obj["raw_sha256"]:raise ValueError("raw_digest_mismatch")
        verify_projection(self.name,json.loads(original),obj)
        for row in obj["rows"]:
            if set(row)!={"date",*FIELDS[self.name]} or date.fromisoformat(row["date"])>date.fromisoformat(cutoff):
                raise ValueError("unsupported_source_fields")
            values=[str(row[key]) for key in FIELDS[self.name]]
            if any(not val.strip() for val in values):raise ValueError("empty_value")
            item["rows"].append(dict(date=row["date"],text=" · ".join(values)))
        item.update(status="available" if item["rows"] else "empty",source_raw_sha256=obj["raw_sha256"])
        return item


def verify_projection(name, capture, obj):
    """Require each selected field in the retained provider frame, at its original row."""
    from datetime import timedelta
    if (capture.get("source"),capture.get("ticker"),capture.get("as_of"),capture.get("observed_at")) != (
        name,obj["ticker"],obj["cutoff"],obj["observed_at"]):
        raise ValueError("provider_frame_binding")
    limit={"stock_lhb_detail_em":3,"stock_hot_rank_latest_em":1,"stock_hot_keyword_em":5,
           "stock_zh_a_disclosure_report_cninfo":5,"stock_individual_info_em":1,"stock_sector_fund_flow_rank":3}[name]
    days={"stock_lhb_detail_em":14,"stock_zh_a_disclosure_report_cninfo":90}.get(name,0)
    target=date.fromisoformat(obj["cutoff"])
    retained=capture["records"]
    if not isinstance(retained,list) or len(obj["rows"])>limit:
        raise ValueError("frame_row_limit")
    if name=="stock_sector_fund_flow_rank":retained=retained[:30]
    expected=[]
    for row in retained:
        if row["ticker"]!=obj["ticker"]:continue
        day=date.fromisoformat(row["date"])
        if not target-timedelta(days=days)<=day<=target:continue
        selected={key:row[key] for key in ("date",*FIELDS[name])}
        if any(not isinstance(selected[k],str) or not selected[k].strip() for k in FIELDS[name] if k!="rank"):
            raise ValueError("invalid_text_field")
        if "rank" in selected and (not str(selected["rank"]).isascii() or not str(selected["rank"]).isdigit() or int(selected["rank"])<1):
            raise ValueError("invalid_rank")
        if name=="stock_sector_fund_flow_rank":
            terms=capture.get("profile_terms")
            if not isinstance(terms,list) or not terms or any(not isinstance(term,str) or not term.strip() for term in terms):
                raise ValueError("invalid_sector_profile")
            if not any(term in row["sector"] for term in terms):
                continue
        expected.append(selected)
        if len(expected)==limit:break
    if expected!=obj["rows"]:raise ValueError("projection_frame_disagreement")




