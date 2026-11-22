"""Run-owned, single-worker supplemental publication. No live provider is registered here."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from datetime import date, datetime
from hashlib import sha256
import json
from threading import Event
from zoneinfo import ZoneInfo

from tradingagents.analysis_budget import AnalysisBudgetStopped
from tradingagents.full_notice import OptionalNoticeSkipped

full_sources = ContextVar("full_sources", default=None)
selected_sources = ContextVar("selected_full_sources", default=None)
SOURCES = {
    "flow_sentiment": ("stock_individual_fund_flow", "stock_lhb_detail_em",
                       "stock_margin_detail_sse", "stock_hot_rank_latest_em", "stock_hot_keyword_em"),
    "announcements": ("stock_individual_notice_report", "stock_zh_a_disclosure_report_cninfo"),
    "industry_policy": ("stock_individual_info_em", "stock_sector_fund_flow_rank", "stock_info_global_em"),
}


def write_new(path, value):
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)


def collect(owner, budget, ticker, cutoff, cancel):
    receipts = {}
    for category, names in SOURCES.items():
        if ticker.endswith(".SZ"):
            names = tuple("stock_margin_detail_szse" if x == "stock_margin_detail_sse" else x for x in names)
        for source in names:
            item = {"category": category, "status": "not_run", "rows": []}
            receipts[source] = item
            if selected_sources.get() is not None and source not in selected_sources.get():
                item["status"]="not_selected"
                continue
            if budget is None or cancel.is_set() or source not in owner:
                continue
            admitted = False
            try:
                # Each adapter supplies already source-validated projections. It must
                # call admit immediately before each physical send, including retries.
                def admit():
                    nonlocal admitted
                    if cancel.is_set() or admitted:
                        return None
                    seconds = budget.admit("data", optional=True)
                    admitted = seconds is not None
                    return seconds
                from tradingagents.full_matrix import LocalSource
                from tradingagents.full_pair_producer import ProducedSource, ProducerStopped
                if type(owner[source]) is ProducedSource:
                    budget.check()
                    receipts[source]=dict(category=category,**owner[source].read(ticker,cutoff,owner[source].mode,cancel))
                    continue
                if type(owner[source]) is LocalSource:
                    budget.check()
                    receipts[source] = dict(category=category, **owner[source].read(ticker, cutoff, owner[source].mode))
                    continue
                from tradingagents.scoped_bulletin import ReviewedBulletin
                if source == "stock_info_global_em" and type(owner[source]) is ReviewedBulletin:
                    budget.check()
                    receipts[source] = owner[source].read(ticker, cutoff)
                    continue
                from tradingagents.full_notice import NoticeSource
                physical_notice = (source == "stock_individual_notice_report"
                                   and type(owner[source]) is NoticeSource)
                if physical_notice:
                    proof = owner[source].read(ticker, cutoff)
                    receipts[source] = proof
                    continue
                else:
                    rows = owner[source](ticker, cutoff, cancel, admit)
                if rows is None:
                    continue
                if not (admitted or physical_notice):
                    raise ValueError("missing_admission")
                budget.check()
                accepted = []
                for row in rows:
                    if set(row) != {"ticker", "date", "published_at", "text"} or row["ticker"] != ticker:
                        raise ValueError("projection_binding")
                    day = date.fromisoformat(row["date"])
                    published = datetime.fromisoformat(row["published_at"])
                    if published.utcoffset() is None or not isinstance(row["text"], str) or not row["text"].strip():
                        raise ValueError("projection_content")
                    publication_day = published.astimezone(ZoneInfo("Asia/Shanghai")).date()
                    if day <= publication_day <= date.fromisoformat(cutoff):
                        accepted.append(row)
                item.update(status="available" if accepted else "empty", rows=accepted)
            except ProducerStopped as error:
                item.update(status="unavailable",reason=error.reason)
                cancel.set()
            except OptionalNoticeSkipped:
                item["reason"] = "optional_budget_unavailable"
            except TimeoutError:
                item["status"] = "timeout"
            except AnalysisBudgetStopped as error:
                item["status"] = "timeout" if error.reason == "deadline_exceeded" else "not_run"
                item["reason"] = error.reason
                cancel.set()
            except PermissionError:
                item["status"] = "unavailable"
                item["reason"] = "access_denied"
                cancel.set()
            except Exception:
                item["status"] = "unavailable"
    selected=selected_sources.get()
    if selected is not None and "bank_lpr" not in selected:
        receipts["bank_lpr"]=dict(category="bank_lpr",status="not_selected",rows=[])
        return receipts
    if ticker != "601328.SS":
        receipts["bank_lpr"]=dict(category="bank_lpr",status="not_run",reason="reviewed_bank_policy_binding_missing",rows=[])
        return receipts
    from tradingagents.full_lpr import BankLpr
    lpr = {"category":"bank_lpr","status":"not_run","rows":[]}
    if not cancel.is_set() and type(owner.get("bank_lpr")) is BankLpr:
        try:
            lpr = owner["bank_lpr"].read(ticker, cutoff)
        except OptionalNoticeSkipped:
            lpr["reason"] = "optional_budget_unavailable"
        except AnalysisBudgetStopped as error:
            lpr.update(status="timeout" if error.reason == "deadline_exceeded" else "not_run",reason=error.reason)
        except TimeoutError:
            lpr["status"] = "timeout"
        except PermissionError:
            lpr.update(status="unavailable",reason="access_denied")
        except Exception:
            lpr["status"] = "unavailable"
    receipts["bank_lpr"] = lpr
    return receipts


def publish_full(owner, budget, ticker, cutoff, report_dir, publish_basic, *, graph_valid):
    cancel = Event()
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(copy_context().run, collect, owner if graph_valid else {}, budget, ticker, cutoff, cancel)
        try:
            report = publish_basic()
            digest = sha256(report.read_bytes()).hexdigest()
            manifest = json.loads((report.parent / "manifest.json").read_bytes())
            if manifest["files"][report.name] != digest:
                raise ValueError("basic_export_digest_mismatch")
            basic = {"preset": "basic", "status": "published", "export_id": report.parent.name, "sha256": digest}
            write_new(report_dir / "basic-publication.json", basic)
            receipts = future.result()
        finally:
            cancel.set()
    status = "completed" if graph_valid and all(x["status"] == "available" or (k == "stock_individual_fund_flow" and x["status"] == "available_partial" and x.get("requested_ratio_verified") is True) for k,x in receipts.items() if k != "bank_lpr") else "incomplete"
    result = {"requested_preset": "all", "ticker": ticker, "cutoff": cutoff, "basic": basic,
              "status": status, "sources": {k:v["status"] for k,v in receipts.items()}, "evidence": receipts}
    if selected_sources.get() is not None:
        result.update(scope="custom_full",selected=sorted(selected_sources.get()),default_acceptance=False)
        result["status"]=status="custom_complete" if graph_valid and all(receipts.get(k,{}).get("status")=="available" or (k=="stock_individual_fund_flow" and receipts.get(k,{}).get("status")=="available_partial" and receipts[k].get("requested_ratio_verified") is True) for k in selected_sources.get()) else "custom_incomplete"
    lines = ["# 完整模式补充报告", "", f"标的 {ticker}，截止日 {cutoff}。",
             f"完整状态 {status}。基础报告导出编号 {basic['export_id']}，SHA-256 为 {digest}。", ""]
    for name, item in receipts.items():
        lines.append(f"- {item['category']} / {name} — {item['status']}")
        if item.get("coverage"):
            lines.append("  - " + item["coverage"])
        for row in item["rows"]:
            lines.append(f"  - {row['date']} · {row['text']}")
    with (report_dir / "full-report.md").open("x", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    result["supplement_sha256"] = sha256((report_dir / "full-report.md").read_bytes()).hexdigest()
    write_new(report_dir / "full-result.json", result)
    return result








