"""Checked terminal handoff used by the streaming CLI and bounded wrapper."""
from hashlib import sha256
import json
from tradingagents.portfolio_research_run import active_selection


def verified_research(state, *, stream_finished=False):
    if active_selection() is None:
        if state.get("portfolio_research_terminal"):
            raise ValueError("unowned_research_terminal")
        return None
    from tradingagents.portfolio_research_terminal import finish,read_terminal
    if stream_finished and not state.get("portfolio_research_terminal"):
        pre=state.get("evidence_preflight",{})
        admission=state.get("admission_gate",{})
        if pre.get("passed") is not False and admission.get("admitted") is not False:
            raise ValueError("research_stream_terminal_missing")
        return finish(state)
    return read_terminal(state)


def full_graph_valid(state):
    result=verified_research(state)
    if result is not None:
        return result[0]["status"]=="research_opinion"
    from tradingagents.terminal_contract import build_terminal_contract
    return build_terminal_contract(state).terminal_outcome_kind.value=="trading_decision"


def verify_export(state,report_path):
    from tradingagents.report_storage import ReportStore
    from tradingagents.portfolio_research_inputs.storage import canonical
    item,digest=verified_research(state)
    with ReportStore(report_path.parent.parent.parent) as store:
        folder=report_path.parent.relative_to(store.root)
        raw_manifest=store.read_bytes(folder/"manifest.json")
        manifest=json.loads(raw_manifest)
        current,_=active_selection()
        if ("research-export/"+folder.name,sha256(raw_manifest).hexdigest(),"portfolio-research-v3",0) not in current.snapshot()["evidence"]:
            raise ValueError("research_export_manifest_unbound")
        report=store.read_bytes(folder/report_path.name)
        machine=store.read_bytes(folder/"research-terminal.json")
        audit=store.read_bytes("research-audit.json")
        if (manifest["run_id"]!=item["run"] or manifest["audit_sha256"]!=digest
                or machine!=canonical(item) or audit!=machine
                or sha256(machine).hexdigest()!=manifest["files"]["research-terminal.json"]
                or sha256(report).hexdigest()!=manifest["files"][report_path.name]):
            raise ValueError("research_export_changed")
    return item,digest

def bind_full_publication(state,folder,expected,budget):
    from tradingagents.portfolio_research_inputs.storage import canonical
    from tradingagents.report_storage import ReportStore
    current,run=active_selection()
    if budget is None:
        raise ValueError("research_budget_missing")
    budget.check()
    with ReportStore(folder) as store:
        result_raw=store.read_bytes("full-result.json")
        basic_raw=store.read_bytes("basic-publication.json")
        supplement=store.read_bytes("full-report.md")
        result=json.loads(result_raw)
        basic=json.loads(basic_raw)
        if (result!=expected or result["basic"]!=basic or result["ticker"]!=run.symbol
                or result["cutoff"]!=run.as_of or result["supplement_sha256"]!=sha256(supplement).hexdigest()):
            raise ValueError("research_full_publication_mismatch")
        import re
        export=basic["export_id"]
        if not isinstance(export,str) or not re.fullmatch(r"[0-9a-f]{32}",export):
            raise ValueError("invalid_export_id")
        path=store.root/"report_exports"/export/"complete_report.md"
        item,digest=verify_export(state,path)
        if basic["sha256"]!=sha256(store.read_bytes(path)).hexdigest():
            raise ValueError("research_basic_digest_mismatch")
        proof=dict(version=1,owner=run.run_id,graph=item["graph"],selection=run.selection_sha256,
                   audit=digest,export=export,basic_sha256=sha256(basic_raw).hexdigest(),
                   full_sha256=sha256(result_raw).hexdigest(),status=result["status"])
        raw=canonical(proof)
        name="research-full-binding.json"
        store.write_bytes(name,raw)
        observed=store.read_bytes(name)
        if observed!=raw:
            raise ValueError("research_full_binding_changed")
        current.register("research-full-publication",observed,sha256(raw).hexdigest(),"portfolio-research-v3",0)
    budget.check()

def verified_full_binding(state,folder):
    from tradingagents.report_storage import ReportStore
    current,run=active_selection()
    item,digest=verified_research(state)
    with ReportStore(folder) as store:
        raw=store.read_bytes("research-full-binding.json")
        if raw is None or ("research-full-publication",sha256(raw).hexdigest(),"portfolio-research-v3",0) not in current.snapshot()["evidence"]:
            raise ValueError("research_full_binding_unavailable")
        proof=json.loads(raw)
        full=store.read_bytes("full-result.json")
        basic=store.read_bytes("basic-publication.json")
        if (proof["audit"]!=digest or proof["owner"]!=run.run_id or proof["graph"]!=item["graph"]
                or proof["selection"]!=run.selection_sha256 or sha256(full).hexdigest()!=proof["full_sha256"]
                or sha256(basic).hexdigest()!=proof["basic_sha256"]):
            raise ValueError("research_full_binding_changed")
        result=json.loads(full)
        if (result["basic"]!=json.loads(basic) or result["basic"]["export_id"]!=proof["export"]
                or sha256(store.read_bytes("full-report.md")).hexdigest()!=result["supplement_sha256"]):
            raise ValueError("research_supplement_changed")
        verify_export(state,store.root/"report_exports"/proof["export"]/"complete_report.md")
        return sha256(raw).hexdigest()
