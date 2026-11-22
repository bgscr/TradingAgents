"""Scoped identity preparation for an already admitted unresolved Full session."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from hashlib import sha256
from pathlib import Path

def token_for_identity(source, project):
    if project:
        from project_credentials import read_project_credentials
        return read_project_credentials(source,names=("TUSHARE_TOKEN",)).pop("TUSHARE_TOKEN")
    import getpass,sys,warnings
    if not sys.stdin.isatty():raise ValueError("interactive_hidden_input_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error",getpass.GetPassWarning)
        return getpass.getpass("Tushare Token for confirmed single-security preparation: ")

def acquire_identity(source,root,clock):
    from full_financial_channel import binding,write
    from tradingagents.full_session_ledger import owner,preparation_role
    from tradingagents.dataflows import bank_identity_snapshot as bank
    from tradingagents.dataflows.bank_identity_acquisition import CaptureClock,update
    from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
    clock.remaining()
    own=owner()
    if own is None or own.child_role is not None or own.key!=root.name or own.path!=root/"physical-ledger"/"ledger.sqlite3":
        raise ValueError("intake_owner_required")
    spec=binding(source,root)
    if own.params!=spec["params"] or own.params.get("contract")!="default-full-latest-intake-v1" or own.phase!="preparation":
        raise ValueError("intake_owner_mismatch")
    sym=own.params["ticker"];query=bank.exact_query(sym)
    asset=resolve_mainland_instrument(sym)
    if (not asset or asset.instrument_kind!="equity" or sym.startswith(("200","900")) or
        (query["params"]["exchange"],own.params["venue"]) not in {("SSE","XSHG"),("SZSE","XSHE")}):
        raise ValueError("unsupported_security")
    today=bank.current_time().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    ref,state=bank.find_current_reference(source,sym,today,equity=True)
    equity = True
    if state=="refresh_required":
        # Independently validated legacy bank identity remains sufficient evidence of equity.
        old_ref,old_state=bank.find_current_reference(source,sym,today)
        if old_state=="retained_snapshot":
            ref,state,equity=old_ref,old_state,False
    if state=="base_registry":return None
    if state=="refresh_required":
        write(root/"intake-identity-attempt.json",dict(params_sha256=spec["params_sha256"],query=query))
        timer=CaptureClock();timer.end=min(timer.end,clock.deadline_monotonic)
        timer.end_epoch=min(timer.end_epoch,clock.effective_deadline());timer.remaining()
        with preparation_role("identity"):
            token=token_for_identity(source,own.params["use_project_env"])
            try:result=update(source,token,clock=timer,exact_symbol=sym,equity=True)
            finally:del token
        if result.get("status")!="exact_positive" or result.get("physical_attempts")!=1:
            raise ValueError("exact_identity_unavailable")
        ref=result["snapshot_ref"]
    elif state!="retained_snapshot":raise ValueError("identity_discovery_unverified")
    # Re-read genuine READY, normalized row, query binding, expiry and listing interval.
    clock.remaining()
    validated,raw=bank._overlay(source,root,sym,today,ref,bank.current_time(),equity=equity)
    dest=root/"intake-identity-registry.json"
    with dest.open("xb") as out:
        out.write(raw);out.flush();os.fsync(out.fileno())
    if sha256(dest.read_bytes()).hexdigest()!=validated["registry_sha256"]:raise ValueError("identity_overlay_changed")
    write(root/"intake-identity-binding.json",validated)
    clock.shorten_to(bank.instant(validated["expires_at"]).timestamp())
    return ref

def reader_config(source,root,symbol):
    from full_financial_channel import binding,load
    from tradingagents.dataflows import bank_identity_snapshot as bank
    spec=binding(source,root)
    if spec["params"]["ticker"]!=symbol.replace(".SS",".SH"):raise ValueError("wrong_security")
    saved=load(root/"intake-identity-binding.json")
    validated,raw=bank._overlay(source,root,symbol,saved["as_of"],saved["snapshot_ref"],bank.current_time(),equity=saved["contract"]=="equity-stock-basic-session-v1")
    if validated!=saved or raw!=bank.read_bound(root/"intake-identity-registry.json",validated["registry_sha256"]):
        raise ValueError("identity_overlay_changed")
    return dict(instrument_identity_registry_path=str(root/"intake-identity-registry.json"),instrument_identity_registry_sha256=validated["registry_sha256"])
