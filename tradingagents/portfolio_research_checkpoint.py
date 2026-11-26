"""Use the regular durable physical budget without enabling graph state replay."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path

from tradingagents.analysis_budget import active_budget, checkpoint_budget_binding, checkpoint_storage_directory
from tradingagents.graph.checkpointer import get_checkpointer, thread_id
from tradingagents.portfolio_research_run import active_selection


def _binding(graph, symbol, day, asset_type):
    owner, run = active_selection()
    if (symbol, str(day), asset_type) != (run.symbol, run.as_of, "stock"):
        raise ValueError("research_checkpoint_selection_mismatch")
    directory = Path(checkpoint_storage_directory(graph.config))
    if directory != owner.folder.parent / "checkpoint-state" or directory.resolve() != directory:
        raise ValueError("research_checkpoint_storage_mismatch")
    fields = dict(mode=owner.params["decision_mode"], owner=run.run_id, symbol=symbol, day=str(day),
                  selection=run.selection_sha256, graph=graph._run_signature(asset_type))
    raw = json.dumps(fields,sort_keys=True,separators=(",",":")).encode()
    signature = "portfolio-research-budget-v1:" + sha256(raw).hexdigest()
    thread = thread_id(symbol,str(day),signature)
    return owner, directory, signature, thread


@contextmanager
def research_checkpoint(graph, symbol, day, asset_type):
    owner, directory, signature, thread = _binding(graph,symbol,day,asset_type)
    budget = active_budget()
    if (not graph.config.get("checkpoint_enabled") or not graph.config.get("analysis_budget_enabled")
            or budget is None or not budget._checkpoint_required or budget.max_calls > owner.params["session_max_calls"]
            or budget._session_deadline_epoch is None or budget._session_monotonic_deadline is None):
        raise ValueError("research_checkpoint_budget_missing")
    policy = checkpoint_budget_binding(graph.config,signature)
    raw = json.dumps(dict(signature=signature,thread=thread,binding=policy),sort_keys=True,separators=(",",":")).encode()
    # Commit once, before opening the database. A stopped or crashed owner cannot reenter.
    owner.register("research-checkpoint",raw,sha256(raw).hexdigest(),"portfolio-research-budget-v1",0)
    database = directory/"checkpoints"/(symbol+".db")
    if any(Path(str(database)+suffix).exists() for suffix in ("","-wal","-shm","-journal")):
        raise ValueError("research_preexisting_checkpoint")
    if database.parent != database.parent.resolve():
        raise ValueError("research_checkpoint_storage_mismatch")
    if getattr(graph,"_active_checkpointer",None) is not None:
        raise ValueError("research_existing_graph_saver")
    with get_checkpointer(directory,symbol) as saver:
        # The analysis budget owns the durable SQLite journal. Graph channels,
        # pending writes and command/replay material are intentionally never saved.
        graph.graph = graph.workflow.compile()
        with budget.checkpoint_session(saver.conn,saver.lock,thread,policy,has_checkpoint=False):
            yield {"configurable":{"thread_id":thread}}, signature


def validate_cleanup(graph, symbol, day, asset_type, signature):
    _, _, expected, thread = _binding(graph,symbol,day,asset_type)
    budget = active_budget()
    if (signature != expected or budget is None or budget._journal is None
            or budget._journal[2] != thread):
        raise ValueError("research_checkpoint_cleanup_mismatch")
    return expected
