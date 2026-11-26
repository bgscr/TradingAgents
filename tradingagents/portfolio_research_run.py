"""Selection and research-mode ownership at the confirmed latest Full boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import json

from tradingagents.full_session_ledger import owner

MODE = "portfolio-research-v3"


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    symbol: str
    as_of: str
    selection_sha256: str


_active = ContextVar("confirmed_portfolio_research", default=None)


def current_research_run():
    return _active.get()


@contextmanager
def confirmed_research_scope(args, root, selection):
    from full_financial_channel import digest, load

    current = owner()
    requested = getattr(args, "research_mode", None)
    owned_mode = current.params.get("decision_mode") if current is not None else None
    if requested is None and owned_mode is None:
        if _active.get() is not None:
            raise ValueError("nested_research_scope")
        yield None
        return
    if requested != MODE or owned_mode != MODE or _active.get() is not None:
        raise ValueError("research_mode_binding")
    root = Path(root)
    intent = current.params
    if (current.child_role is not None or current.folder != root / "physical-ledger"
            or root.name != current.key or not args.full or intent.get("mode") != "default_full"
            or intent.get("requested_date") != "latest" or args.ticker != intent.get("ticker")
            or args.session_max_calls != intent.get("session_max_calls")):
        raise ValueError("research_owner_binding")
    proof = load(root / "resolved-selection.json")
    receipt_hash = digest(proof)
    if (proof.get("run_id") != current.key or proof.get("params_sha256") != digest(intent)
            or proof.get("selection") != selection or not isinstance(selection, dict)
            or selection.get("trading_date") != args.analysis_date
            or args.ticker.replace(".SH", ".SS") != selection.get("security","").replace(".SH",".SS")
            or selection.get("venue") != intent.get("venue")
            or ("latest_selection", receipt_hash, "verified-local-reader", 0) not in current.snapshot()["evidence"]):
        raise ValueError("research_selection_binding")
    from hashlib import sha256
    launch=load(root/"research-launch.json")
    release=(root.parents[2]/"config/bounded_mainland_release.json").read_bytes()
    if (launch["run"]!=current.key or launch["mode"]!=MODE or launch["release"]!=sha256(release).hexdigest()
            or ("portfolio_research_launch",digest(launch),"verified-supervisor-launch-v1",0) not in current.snapshot()["evidence"]):
        raise ValueError("research_launch_owner_mismatch")
    raw = json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    current.register("portfolio_research_selection", raw,
                     receipt_hash,
                     MODE, 0)
    context = ResearchRun(current.key, args.ticker.replace(".SH",".SS"), args.analysis_date, receipt_hash)
    token = _active.set(context)
    try:
        yield context
    finally:
        _active.reset(token)



def active_selection():
    current=owner()
    run=current_research_run()
    if current is None or current.params.get("decision_mode") is None:
        if run is not None:
            raise ValueError("research_context_detached")
        return None
    if (current.params["decision_mode"]!=MODE or current.child_role is not None or run is None
            or run.run_id!=current.key or run.symbol!=current.params["ticker"].replace(".SH",".SS")):
        raise ValueError("research_context_detached")
    return current,run


