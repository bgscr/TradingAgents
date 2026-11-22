"""Reviewed CURRENT ordinary auction schedule. No historical availability claims."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
VERSION = "mainland-ordinary-auction-2026-07-06.v1"
EFFECTIVE = date(2026, 7, 6)
RULES = {
    "XSHG": ("SH", "2.4.2", "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml"),
    "XSHE": ("SZ", "2.3.2", "https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf"),
}
DAILY_DOCUMENTATION = "https://tushare.pro/document/2?doc_id=27"

def schedule(symbol, venue, day, now):
    from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
    instrument = resolve_mainland_instrument(symbol)
    rule = RULES.get(venue)
    if (now.utcoffset() is None or not EFFECTIVE <= day <= now.astimezone(SH).date()
        or rule is None or instrument is None or instrument.instrument_kind != "equity"
        or instrument.akshare_code.startswith(("200", "900"))
        or not symbol.replace(".SS", ".SH").endswith("." + rule[0])):
        raise ValueError("auction_rule_inapplicable")
    cutoff = datetime.combine(day,time(15),SH)
    return dict(version=VERSION, venue=venue, rule_url=rule[2], section=rule[1],
                scheduled_auction_end=cutoff.isoformat(), earliest_daily_read=datetime.combine(day,time(16),SH).isoformat(),
                purpose="current_retrospective_auction_close_only")

def reject_known_exception(source, symbol, day, scheduled):
    """Read the installed market database only for vetoes; it cannot qualify a close."""
    import os, sqlite3
    from contextlib import closing
    from full_financial_channel import ordinary
    base = source/"data/market_history"
    db = ordinary(base/"market_history.sqlite3")
    marker = ordinary(base/"payloads/.market-history-database-owner")
    if marker.read_bytes() != os.path.normcase(str(db)).encode() + b"\n":
        raise ValueError("calendar_store_owner_mismatch")
    with closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        aliases=(symbol,symbol.replace(".SS",".SH"))
        suspended = connection.execute("""SELECT 1 FROM trading_status_revisions s JOIN instruments i
            USING(instrument_id) WHERE i.canonical_symbol IN (?,?) AND s.session_date=?
            AND s.trading_status='suspended' LIMIT 1""",(*aliases,day.isoformat())).fetchone()
        if suspended:raise ValueError("known_security_suspension")
        from tradingagents.market_history.config import MarketHistoryConfig,MarketHistoryMode,DataUsageMode
        from tradingagents.market_history.store import MarketHistoryStore
        from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window
        store=MarketHistoryStore(MarketHistoryConfig(MarketHistoryMode.CURRENT,db,base/"payloads",
            base/"backups",DataUsageMode.PERSONAL_RESEARCH),connection)
        calendar=read_mainland_calendar_window(store,day,day)
        if calendar is not None:
            rows=[s for s in calendar.sessions if s.session_date==day]
            if (len(rows)!=1 or rows[0].status.value!="open"
                or (rows[0].closes_at is not None and rows[0].closes_at!=scheduled)):
                raise ValueError("special_session")

def check_calendar_freshness(observed, now):
    """Conservative latest-selection reuse window, measured from original observation."""
    from datetime import timedelta
    if observed.utcoffset() is None or now.utcoffset() is None or observed>now:
        raise ValueError("calendar_not_observed")
    if now-observed>=timedelta(hours=24):
        raise ValueError("latest_calendar_expired")
