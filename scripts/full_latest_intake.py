"""Default Full with an unresolved date; standard launcher owns consent, Job and release."""
import argparse
from datetime import date
from pathlib import Path
import re

def intake(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False, exit_on_error=False)
    for name in ("full","check-only","use-project-env","run","calendar-only"):
        parser.add_argument("--"+name,action="store_true")
    parser.add_argument("--ticker",required=True)
    parser.add_argument("--analysis-date",choices=["latest"],required=True)
    parser.add_argument("--session-max-calls",type=int,default=60)
    parser.add_argument("--expected-trading-date")
    parser.add_argument("--decision-mode", choices=("portfolio-research-v3",))
    flags = [x for x in argv if x.startswith("--")]
    if any("=" in f for f in flags) or len(flags)!=len(set(flags)):
        raise ValueError("ambiguous_intake")
    opts=parser.parse_args(argv)
    if not opts.full or not re.fullmatch(r"[0-9]{6}\.(?:SH|SS|SZ)",opts.ticker) or not 1<=opts.session_max_calls<=60:
        raise ValueError("invalid_default_full_intake")
    if opts.run and (opts.check_only or not opts.use_project_env):
        raise ValueError("invalid_explicit_run")
    if not opts.check_only and not opts.run:
        raise ValueError("explicit_run_required")
    if opts.calendar_only and (not opts.run or opts.session_max_calls != 1):
        raise ValueError("invalid_calendar_diagnostic")
    if opts.decision_mode and opts.calendar_only:
        raise ValueError("research_requires_latest_full")
    symbol=opts.ticker.replace(".SS",".SH")
    intent = dict(contract="default-full-latest-intake-v1",ticker=symbol,venue="XSHE" if symbol.endswith(".SZ") else "XSHG",
                requested_date="latest",mode="calendar_only" if opts.calendar_only else "default_full",use_project_env=opts.use_project_env,
                session_max_calls=opts.session_max_calls, **({"decision_mode":opts.decision_mode} if opts.decision_mode else {}))
    if opts.expected_trading_date is not None:
        expected = opts.expected_trading_date
        if opts.calendar_only or date.fromisoformat(expected).isoformat() != expected:
            raise ValueError("invalid_expected_trading_date")
        intent["expected_trading_date"] = expected
    return intent, opts.check_only

def bind_selection(owner,root,intent,receipt):
    from full_financial_channel import digest,write
    if owner.key!=root.name or owner.params!=intent or owner.child_role is not None:
        raise ValueError("intake_owner_mismatch")
    day=date.fromisoformat(receipt["trading_date"])
    if (receipt["security"].replace(".SS",".SH")!=intent["ticker"] or receipt["venue"]!=intent["venue"]
        or receipt.get("purpose")!="current_retrospective_selection" or not receipt.get("calendar_revision")
        or not receipt.get("close_evidence") or receipt["close_evidence"]["trading_date"]!=day.isoformat()):
        raise ValueError("resolution_binding_mismatch")
    proof=dict(run_id=owner.key,params_sha256=digest(intent),selection=receipt)
    # The digest enters the same ledger as physical sends; workers must check both artifacts.
    encoded=digest(proof)
    import json
    raw=json.dumps(proof,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    owner.register("latest_selection",raw,encoded,"verified-local-reader",0)
    write(root/"resolved-selection.json",proof)
    return day.isoformat()
def run(entry,argv,clock,run_id,manifest):
    import os,sys,ctypes,subprocess,json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    src=entry.SOURCE
    intent,check_only=intake(argv)
    launch=None
    if intent.get("decision_mode") and not check_only:
        entry.research_policy(manifest)
        entry.require(intent.get("session_max_calls")==60 and intent["use_project_env"],"research_launch_limits")
        from runpy import run_path
        protocol=run_path(str(src/"scripts/research_launch.py"))
        launch=protocol["receive"](getattr(clock,"research_pipe",None),src,manifest,sys.argv[1:],run_id,clock.deadline_monotonic)
        del clock.research_pipe
    paths={row["relative"].replace(chr(92),"/") for row in manifest["files"] if row["root"]=="project"}
    needed=set(entry.FULL_BOUND_FILES)|{"tradingagents/market_history/calendar_preparation.py","tradingagents/market_history/calendar_preparation_runtime.py","tradingagents/market_history/calendar_discovery.py","tradingagents/market_history/current_cache.py","scripts/full_latest_intake.py","scripts/full_exchange_revision.py","scripts/full_auction_schedule.py","tradingagents/dataflows/bank_identity_snapshot.py","scripts/full_cold_identity.py","scripts/full_cold_calendar.py","scripts/full_cold_daily.py","scripts/full_calendar_cache.py",
        "tradingagents/full_pair_producer.py","tradingagents/full_publication.py","tradingagents/full_notice.py",
        "tradingagents/notice_pages.py","tradingagents/full_lpr.py","tradingagents/scoped_bulletin.py",
        "tradingagents/budget_data.py","config/instrument_identity_registry.json"}
    entry.require(needed<=paths,"full_owner_sources_unbound")
    class DailyRequired(Exception):pass
    def require_expected_day(day, reason="unexpected_trading_date"):
        if "expected_trading_date" in intent:
            entry.require(day == intent["expected_trading_date"], reason)

    def select(identity_run=None,calendar_run=None,claimed_run=None):
        env={k:os.environ[k] for k in ("SystemRoot","WINDIR") if k in os.environ}
        env.update(PYTHON_DOTENV_DISABLED="1",TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE="1")
        outcome=subprocess.run([sys.executable,"-I","-B",str(src/"scripts/full_calendar_cache.py"),
            intent["ticker"].replace(".SH",".SS"),"latest",datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),*([identity_run or "-",calendar_run or "-",claimed_run] if claimed_run else [])],
            executable=sys.executable,cwd=src,env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=min(30,clock.remaining()) if clock else 30)
        data=json.loads(outcome.stdout)
        if claimed_run and outcome.returncode==2 and data.get("error")=="same_day_close_required":
            require_expected_day(data["need"].get("trading_date"), "unexpected_daily_request_date")
            raise DailyRequired(data["need"])
        entry.require(outcome.returncode==0,data.get("error","calendar_reader_failed"))
        return data
    if check_only:
        selection = select()
        require_expected_day(selection.get("trading_date"))
        print(json.dumps(dict(status="selection_verified",selection=selection,ready_for_live=None)))
        return 0
    entry.require(clock is not None and run_id is not None,"session_supervisor_required")
    entry.require(not ctypes.windll.shell32.IsUserAnAdmin(),"non_admin_required")
    entry.require(not os.environ.get("TRADINGAGENTS_PROTECTION_CONTEXT"),"bound_protection_context")
    entry.require(os.environ.get("TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE","0")=="0","offline_test_mode")
    entry.check_required_local_stores(src)
    calendar_only = intent["mode"] == "calendar_only"
    if calendar_only:
        print(json.dumps(dict(run_id=run_id,intent=intent,launch_confirmation="explicit_run_flag",
            scope="one SSE or SZSE trade_cal POST only; no retries, security data, reports or models")))
    else:
        print(json.dumps(dict(run_id=run_id,intent=intent,launch_confirmation="explicit_run_flag",financial_max=3,job_seconds=1200,
        date_selection="validated cache; missing requested exchange calendar allows one trade_cal POST for today in Shanghai and previous 7 calendar days; one same-security daily close POST for the selected eligible session after its 16:00 Shanghai threshold, zero retries",identity="cache first; at most one exact stock_basic POST, fixed issuer api.waditu.com, current listed security only, zero retries",
        following_calendar="Latest intake: at most one eight-day trade_cal. After selection: at most one separate five-year trade_cal for the confirmed ticker exchange (SSE or SZSE), limit 5000, offset 0. Both charge this same session.",
        following_full="existing personal-research Full: market history, statements, news, approved retrospective enhancements and DeepSeek; official report; no added hosts or retries",
        credentials="project configured credentials" if intent["use_project_env"] else "hidden credential entry",
        session_writes="session, coordinator/WAL, coverage and report files")))
    clock.remaining()
    os.environ["PYTHON_DOTENV_DISABLED"]="1"
    sys.path.insert(0,str(src));sys.path.insert(0,str(src/"scripts"))
    from full_financial_channel import binding,digest
    from tradingagents.full_session_ledger import Ledger,bind
    root=entry.reserve_session(src/"reports/bounded-runs",run_id=run_id)
    spec=dict(source=str(src),run_id=root.name,params=intent,params_sha256=digest(intent),account=entry.FIXED["tushare_account_scope_label"])
    entry.write_once(root,"full-owner.json",spec)
    entry.require(binding(src,root)==spec,"full_run_binding")
    owned=Ledger(root/"physical-ledger",root.name,intent)
    with bind(owned):
        terminal="failed"
        def prepare_calendar():
            from full_cold_calendar import CALENDAR_FAILURES, prepare
            from tradingagents.market_history.coordinator import PhysicalAttemptBudgetExhausted
            try:
                prepare(src,root,clock)
            except PhysicalAttemptBudgetExhausted as blocked:
                category=blocked.failure.error_code
                if category in CALENDAR_FAILURES:
                    raise entry.Stop(category) from None
                raise
        try:
            if launch is not None:
                raw=protocol["canonical"](launch)
                owned.register("portfolio_research_launch",raw,digest(launch),protocol["STAGE"],0)
                entry.write_once(root,"research-launch.json",launch)
            if calendar_only:
                prepare_calendar()
                result=dict(status="calendar_verified",phase="calendar_diagnostic",run_id=root.name)
                entry.write_once(root,"result.json",result)
                terminal="completed"
                print(json.dumps(result))
                return 0
            from full_cold_identity import acquire_identity
            reference=acquire_identity(src,root,clock)
            identity_run=root.name if reference else None
            calendar_run=None
            try:
                try:
                    selection=select(identity_run,None,root.name)
                except entry.Stop as error:
                    if str(error) not in {"calendar_coverage_missing","latest_calendar_expired","prepared SSE calendar range incomplete","prepared SZSE calendar range incomplete"}:raise
                    prepare_calendar()
                    calendar_run=root.name
                    selection=select(identity_run,calendar_run,root.name)
            except DailyRequired as missing:
                from full_cold_daily import acquire, DAILY_STOP_CODES
                from tradingagents.market_history.coordinator import PhysicalAttemptBudgetExhausted
                try:
                    acquire(src,root,clock,missing.args[0])
                except PhysicalAttemptBudgetExhausted as blocked:
                    reason = blocked.failure.error_code
                    raise entry.Stop(reason if reason in DAILY_STOP_CODES else "daily_attempt_failed") from None
                selection=select(identity_run,calendar_run,root.name)
            day=bind_selection(owned,root,intent,selection)
            entry.require((intent["ticker"],day)!=("601328.SH","2026-11-13"),"prior_dispatch_unreconciled")
            require_expected_day(day)
            from tradingagents.dataflows.bank_identity_snapshot import find_current_reference
            from full_financial_channel import load
            equity = bool(reference and load(root/"intake-identity-binding.json")["contract"]=="equity-stock-basic-session-v1")
            ref,state=find_current_reference(src,intent["ticker"],day,equity=equity)
            entry.require(state in ("base_registry","retained_snapshot"),"identity_refresh_not_admitted")
            args=entry.parse_arguments(["--full","--ticker",intent["ticker"],"--analysis-date",day,
                 "--session-max-calls",str(intent["session_max_calls"])]+(["--use-project-env"] if intent["use_project_env"] else []))
            entry.require(ref==reference,"identity_changed_during_selection")
            if equity: args.equity_reference=ref
            else: args.bank_snapshot=ref
            import runpy
            module=runpy.run_path(str(src/"scripts/full_date_evidence.py"));module["selection_receipt"]=selection
            code=export_after_verified_selection(entry,intent,day,args,root,clock,manifest,module)
            terminal="completed" if code==0 else "failed"
            return code
        except BaseException as error:
            from full_cold_calendar import CALENDAR_FAILURES
            if not (root/"result.json").exists():
                entry.write_once(root,"result.json",dict(status="stopped",phase="calendar_diagnostic" if calendar_only else "latest_preparation",
                    reason=str(error) if type(error) is entry.Stop else "preparation_failed",
                    reason_zh={"unexpected_daily_request_date":"待取日线日期与本次要求不符，未发送该日线请求。","unexpected_trading_date":"经核验的交易日与本次要求不符，已停止。","known_security_suspension":"已知该证券当日停牌。","special_session":"交易时段与常规竞价规则不符。","daily_window_not_open":"当日尚未到日线采集窗口。","auction_rule_inapplicable":"该日期或证券不在已审阅规则范围。","verified_close_time_missing":"交易日历仅证明开市，缺少经审阅的收盘时点。","security_close_missing":"缺少该证券该交易日可核验的正式收盘记录。"}.get(str(error),CALENDAR_FAILURES.get(str(error),"未能核验所需证据。")),resume=False))
            raise
        finally:
            ledger_hash=owned.finish(terminal)
            entry.write_once(root,"physical-total.json",dict(total_external_attempts=len(owned.snapshot()["events"]),
                ledger_sha256=ledger_hash,run_id=root.name,params_sha256=spec["params_sha256"]))


def export_after_verified_selection(entry,intent,day,args,root,clock,manifest,module):
    from tradingagents.reporting import _current_full_target
    token = _current_full_target.set((intent["ticker"].replace(".SH", ".SS"), day))
    try:
        if "decision_mode" in intent:
            args.research_mode = intent["decision_mode"]
        return entry.run_confirmed(args,root,clock,manifest,module)
    finally:
        _current_full_target.reset(token)

