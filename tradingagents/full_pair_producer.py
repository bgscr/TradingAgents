"""Full-only retrospective pair backed by the existing single-attempt coordinators."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re

from tradingagents.full_matrix import FLOW, LocalSource, margin_source
from tradingagents.full_session_ledger import finance, owner

POLICY = "config/ordinary-full-pair-policy.json"
HELPER = "scripts/Get-PublicFundflow.ps1"


def load_policy(root, bound):
    """The reviewed policy pins binaries. The optional locator grants no authority."""
    import os
    from tradingagents.powershell_news import _ordinary_file

    if POLICY not in bound:
        return None
    raw = (root / POLICY).read_bytes()
    if hashlib.sha256(raw).hexdigest() != bound[POLICY]:
        raise ValueError("full_pair_policy_changed")
    policy = json.loads(raw)
    if (set(policy) != {"contract", "ratio", "margin", "executable_name", "executable_sha256"}
            or policy["contract"] != "ordinary-full-pair-policy-v1"
            or policy["ratio"] != "verified-cache-only" or policy["margin"] != "existing-owner-budget"
            or policy["executable_name"] != "pwsh.exe"
            or not re.fullmatch("[a-f0-9]{64}", policy["executable_sha256"])):
        raise ValueError("full_pair_runtime_unverified")
    locator = os.environ.get("TRADINGAGENTS_ORDINARY_FULL_PWSH")
    if not locator:
        return None
    executable = Path(locator)
    if not executable.is_absolute() or executable.name.lower() != policy["executable_name"]:
        raise ValueError("full_pair_runtime_unverified")
    script = root / HELPER
    pins = (policy["executable_sha256"], bound.get(HELPER))
    if (_ordinary_file(executable), _ordinary_file(script)) != pins:
        raise ValueError("full_pair_runtime_unverified")
    return executable, script, pins


class ProducerStopped(Exception):
    """An optional source stopped the remaining supplemental collection."""
    def __init__(self,reason):
        self.reason=reason
        super().__init__(reason)


class ProducedSource(LocalSource):
    def __init__(self, name, config, runtime):
        super().__init__(name)
        self.config = dict(config,bounded_observation_cache_mode="retrospective")
        self.runtime = runtime
        self.mode = "retrospective"
        self.run = self.acquire = None
        self.used = False
        self.blocker = "pair_binding_or_time_unavailable"

    def bind_snapshot(self, state, ticker, cutoff):
        from tradingagents.analysis_budget import AnalysisBudgetStopped
        from tradingagents.powershell_fundflow import FundflowStop
        from tradingagents.powershell_news import PowerShellNewsStop
        try:
            self._prepare(state,ticker,cutoff)
        except (ValueError, KeyError, TypeError, OSError, AnalysisBudgetStopped, FundflowStop, PowerShellNewsStop):
            self.binding = self.run = None
            self.blocker = "source_preparation_failed"

    def _prepare(self,state,ticker,cutoff):
        super().bind_snapshot(state,ticker,cutoff)
        if not self.binding:
            return
        from tradingagents import margin_balance, powershell_fundflow as flow
        from tradingagents.evidence import EvidenceState
        if self.name == FLOW:
            executable,script,approved = self.runtime
            self.run = flow.PowerShellFundflow(executable,script,ticker,cutoff,after_report=True,approved_files=approved)
            if self.run.binding != approved or not all(approved):
                raise ValueError("full_pair_runtime_changed")
            self.run.bind_identity(EvidenceState.model_validate(state["evidence_state"]).instrument_identity)
        else:
            self.run = margin_balance._Run(dict(self.config,bounded_margin_date=cutoff),ticker,cutoff,cutoff)
            self.acquire = self.run.prepare(state)

    def read(self,ticker,cutoff,mode,cancel):
        from tradingagents import powershell_fundflow as flow
        binding = getattr(self,"binding",None)
        if (cancel.is_set() or self.used or mode != "retrospective" or owner() is None or not binding
                or binding["symbol"] != flow._symbol(ticker) or binding["as_of"] != cutoff
                or binding["trade_date"] != cutoff or self.run is None
                or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat() <= cutoff):
            return dict(status="unavailable",reason=self.blocker,rows=[])
        self.used=True
        if self.name==FLOW:
            self.run._cancelled=cancel
        else:
            self.run.cancelled=cancel
        key = hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        config=dict(self.config,bounded_observation_cache_mode="retrospective")
        receipt=dict(status="unavailable",rows=[],snapshot_id=binding["snapshot_id"],
                     binding_sha256=key,coverage="事后回补；不证明交易日当时已公开。")
        if self.name == FLOW:
            ob, proof = self._cached_ratio(binding, key)
            if ob is None:
                return dict(receipt, reason="verified_ratio_cache_missing")
            receipt.update(status="available_partial", rows=[dict(date=cutoff, text="主力净流入占比 "+row[1]+"%")
                           for row in ob["rows"] if row[0] == cutoff], requested_ratio_verified=True)
        else:
            if self.name!=margin_source(ticker) or self.acquire is None:
                return dict(receipt,reason="unbound_margin")
            token=finance.set(True)
            from tradingagents.analysis_budget import AnalysisBudgetStopped
            try:
                ob=self.acquire()
            except AnalysisBudgetStopped as exc:
                if exc.reason!="financial_limit_exceeded":
                    raise
                return dict(receipt,status="not_run",reason=exc.reason)
            finally:
                finance.reset(token)
            if ob.get("status") in ("authentication","permission_denied"):
                raise PermissionError("full_pair_access_denied")
            if ob.get("status") not in ("available","empty"):
                return dict(receipt,reason=ob.get("status","unavailable"))
            from tradingagents.margin_balance import cached_receipt_time
            from tradingagents.moneyflow import _projection_digest
            if (ob["status"]=="empty" and (ob["rows"] or ob["projected_data_sha256"]!=_projection_digest("margin_detail",binding,[]))
                    or ob["status"]=="available" and not cached_receipt_time((ob["rows"],ob["projected_data_sha256"],ob["received_at"]),binding)):
                return dict(receipt,reason="unverified_projection")
            receipt.update(status=ob["status"],rows=[dict(date=day,text="融资余额 "+value+" 元") for day,value in ob["rows"]])
        if (datetime.fromisoformat(ob["received_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() <= cutoff):
            return dict(status="unavailable",reason="retrospective_time_unverified",rows=[])
        receipt.update(provider=ob["provider"],received_at=ob["received_at"],normalized_sha256=ob["normalized_sha256"])
        if self.name != FLOW:
            proof=self._coordinator_receipt(binding,key,ob)
        if proof is None:
            return dict(status="unavailable",reason="coordinator_provenance_unverified",rows=[])
        submitted = (False if self.name==FLOW
                     else self.run.transport.get("io_registered") is True)
        receipt.update(acquisition="session_transport" if submitted else "coordinator_reuse",
                       current_physical_attempts=int(submitted), coordinator=proof)
        raw=json.dumps(receipt,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
        digest=hashlib.sha256(raw).hexdigest()
        owner().register(self.name,raw,digest,"coordinator:"+proof["sequence_id"],int(submitted))
        receipt["receipt_sha256"]=digest
        return receipt

    def _cached_ratio(self, binding, key):
        from datetime import date
        from tradingagents.evidence import stable_acquisition_source_ref
        from tradingagents.market_history.config import MarketHistoryConfig, provider_request_authority_config
        from tradingagents.retrospective_observations import retrospective_observation

        observation = None

        def validate(value):
            nonlocal observation
            received = self.run.cached_receipt_time(value, self.config.get("bounded_fundflow_observation_sha256"))
            if received is None:
                return None
            projection = value["projection"]
            if projection["counts"]["duplicate"] or not any(row[0] == binding["trade_date"] for row in projection["rows"]):
                return None
            observation = self.run._observation("available_partial", projection, received)
            return received

        operation = "stock_individual_fund_flow.powershell"
        request_key = stable_acquisition_source_ref("financial-provider-physical-request", "full-pair-v1:"+key, "1", operation)
        proof = retrospective_observation(provider_request_authority_config(MarketHistoryConfig.from_mapping(self.config)),
            request_key=request_key, provider="akshare", operation="financial-fundamentals:"+operation,
            as_of=date.fromisoformat(binding["as_of"]), receipt_time=validate, with_receipt=True)
        return (observation, proof) if proof is not None else (None, None)

    def _coordinator_receipt(self,binding,key,ob):
        from datetime import date
        from tradingagents.evidence import stable_acquisition_source_ref
        from tradingagents import margin_balance, moneyflow
        from tradingagents.market_history.config import MarketHistoryConfig,provider_request_authority_config
        from tradingagents.retrospective_observations import retrospective_observation
        provider="tushare"
        operation="margin_detail"
        request_key=operation+":"+moneyflow._digest({"contract":margin_balance.VERSION,"binding":binding,
            "fields":margin_balance.FIELDS,"destination":moneyflow.HOST+margin_balance.PATH,
            "account_scope":self.config["tushare_account_scope_label"]})
        expected=(ob["rows"],ob["projected_data_sha256"],ob["received_at"])
        return retrospective_observation(provider_request_authority_config(MarketHistoryConfig.from_mapping(self.config)),
            request_key=request_key,provider=provider,operation=operation,as_of=date.fromisoformat(binding["as_of"]),
            receipt_time=lambda value: ob["received_at"] if value==expected else None,with_receipt=True)
