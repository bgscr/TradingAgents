"""Explicit bounded Full factory. Missing source owners remain visible in the report."""
from contextlib import contextmanager
from decimal import Decimal
import re
from tradingagents.full_lpr import BankLpr
from tradingagents.full_notice import NoticeSource
from tradingagents.full_publication import full_sources

@contextmanager
def session_sources(selection, mode, *, bulletin=None, local_sources=None, selected=None, producer_config=None, pair_runtime=None):
    if mode == "as_known_on_date":
        raise ValueError("strict_full_evidence_not_qualified")
    ticker, cutoff = selection["ticker"], selection["analysis_date"]
    from datetime import date
    date.fromisoformat(cutoff)
    if not re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)",ticker):
        raise ValueError("unreviewed_full_selection")
    pin=("211d95462c5898127aabcf5a00de0953b9721b0578b40b5f4401ab89ff06b7c3",
         cutoff, Decimal("3.00"), Decimal("3.50"))
    sources={"stock_individual_notice_report":NoticeSource(ticker,cutoff,mode=mode),
             "bank_lpr":BankLpr(ticker,cutoff,article_pin=pin,retrospective=mode=="retrospective")} if ticker=="601328.SS" and cutoff=="2026-11-20" else {}
    if bulletin is not None:
        from tradingagents.scoped_bulletin import ReviewedBulletin
        if type(bulletin) is not ReviewedBulletin:
            raise ValueError("invalid_reviewed_bulletin")
        sources["stock_info_global_em"]=bulletin
    from tradingagents.full_matrix import FIELDS,FLOW,LocalSource,margin_source
    for name in (*FIELDS,FLOW,margin_source(ticker)):
        sources[name]=LocalSource(name,*((local_sources or {}).get(name,(None,None))))
        sources[name].mode=mode
    if pair_runtime and mode in (None,"retrospective"):
        from tradingagents.full_pair_producer import ProducedSource
        for name in (FLOW,margin_source(ticker)):
            if name not in (local_sources or {}) and (selected is None or name in selected):
                sources[name]=ProducedSource(name,producer_config,pair_runtime)
    if bulletin is None:
        missing=LocalSource("stock_info_global_em")
        missing.mode=mode
        sources["stock_info_global_em"]=missing
    from tradingagents.full_publication import selected_sources
    scope=selected_sources.set(None if selected is None else frozenset(selected))
    token=full_sources.set(sources if selected is None else {k:v for k,v in sources.items() if k in selected})
    try:
        yield
    finally:
        full_sources.reset(token)
        selected_sources.reset(scope)





