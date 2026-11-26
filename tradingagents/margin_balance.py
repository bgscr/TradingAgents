"""One explicitly selected session's financing balance, advisory only.

No acquisition outside the bounded launcher scope, no latest-date fallback,
and no conversion to decision facts. Provider text never reaches the model.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import http.client
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradingagents import moneyflow as shared
from tradingagents.analysis_budget import AnalysisBudgetStopped, active_budget
from tradingagents.evidence import EvidenceState, SourceAcquisitionAvailable

VERSION = "bounded-margin-balance-v2"
LEGACY_VERSION = "bounded-margin-balance-v1"
FIELDS = ("trade_date", "ts_code", "rzye")
PATH = "/dataapi/margin_detail"
_SCOPE = contextvars.ContextVar("bounded_margin_balance", default=None)
_MISSING_BINDING = {"contract": VERSION, "status": "binding_unavailable"}


def _binding(state, ticker, as_of, trade_date):
    base = shared._binding(state, ticker, as_of)
    day = shared._date(trade_date)
    if day > shared._date(as_of):
        raise shared.MoneyflowUnavailable("identity_invalid")
    ev = EvidenceState.model_validate(state.get("evidence_state", {}))
    snap = ev.market_snapshot
    if (snap is None or snap.snapshot_id_version != "v2" or snap.snapshot_manifest_json is None
            or shared._symbol(snap.symbol) != base["symbol"] or snap.requested_date != as_of
            or snap.effective_trading_date != trade_date or snap.history_rows < 1
            or re.fullmatch(r"[a-f0-9]{64}", snap.frame_sha256) is None):
        raise shared.MoneyflowUnavailable("identity_invalid")
    manifest = json.loads(snap.snapshot_manifest_json)
    if (manifest["requested_date"] != as_of or manifest["effective_trading_date"] != trade_date
            or shared._symbol(manifest["instrument"]["canonical_symbol"]) != base["symbol"]
            or manifest["instrument"]["currency"] != "CNY"
            or manifest["instrument"]["instrument_kind"] != "equity"
            or manifest["instrument"]["reference_market"] not in {
                ev.instrument_identity.venue, "shanghai" if base["symbol"].endswith(".SH") else "shenzhen"}
            or manifest["provider"]["provider_name"] != snap.provider
            or manifest["frame"] != {"rows": snap.history_rows, "sha256": snap.frame_sha256}):
        raise shared.MoneyflowUnavailable("identity_invalid")
    bound = [a for a in ev.source_artifacts if a.artifact_sha256 == snap.frame_sha256 and any(
        isinstance(o, SourceAcquisitionAvailable) and o.capability == "market_snapshot"
        and o.provider == snap.provider and o.artifact == a for o in ev.acquisition_outcomes)]
    if (not bound or len({a.raw_text for a in bound}) != 1
            or hashlib.sha256(bound[0].raw_text.encode("utf-8")).hexdigest() != snap.frame_sha256):
        raise shared.MoneyflowUnavailable("identity_invalid")
    return {"symbol": base["symbol"], "as_of": as_of, "trade_date": trade_date,
            "identity_sha256": base["identity_sha256"], "snapshot_id": snap.snapshot_id}


def _require_active_snapshot(state):
    """Read existing run ownership, never acquire or infer a trading day."""
    from tradingagents.dataflows.market_snapshot import (
        _frame_sha256,
        get_active_authoritative_market_snapshot,
    )

    snap = EvidenceState.model_validate(state["evidence_state"]).market_snapshot
    current = get_active_authoritative_market_snapshot(snap.symbol, snap.requested_date)
    names = ("symbol", "provider", "requested_date", "effective_trading_date", "snapshot_id",
             "snapshot_id_version", "snapshot_manifest_json", "frame_sha256")
    if (current is None or any(getattr(current, k) != getattr(snap, k) for k in names)
            or len(current.frame) != snap.history_rows or _frame_sha256(current.frame) != snap.frame_sha256):
        raise shared.MoneyflowUnavailable("identity_invalid")


def _amount(value):
    number = shared._amount(value)
    if Decimal(number) < 0:
        shared._reject("amount_range")
    return number


def cached_receipt_time(result, binding):
    if type(result) is not tuple or len(result) != 3:
        return None
    rows, digest, received = result
    if (type(rows) is not list or len(rows) != 1 or type(rows[0]) is not list or len(rows[0]) != 2
            or rows[0][0] != binding["trade_date"] or type(rows[0][1]) is not str
            or re.fullmatch(r"[0-9]{1,15}(?:\.[0-9]{1,6})?", rows[0][1]) is None
            or _amount(Decimal(rows[0][1])) != rows[0][1]
            or shared._projection_digest("margin_detail", binding, rows) != digest):
        return None
    return received


def decode(content, binding):
    try:
        table = shared._table(content, FIELDS, 1)
        rows = []
        for row in table["items"]:
            if type(row) is not list or len(row) != 3:
                shared._reject("row_shape")
            cells = dict(zip(table["fields"], row, strict=True))
            if type(cells["ts_code"]) is not str:
                shared._reject("stock_type")
            if cells["ts_code"] != binding["symbol"]:
                shared._reject("stock_mismatch")
            day = cells["trade_date"]
            if type(day) is not str or re.fullmatch(r"[0-9]{8}", day) is None:
                shared._reject("date_format")
            try:
                day = datetime.strptime(day, "%Y%m%d").date().isoformat()
            except ValueError:
                shared._reject("date_invalid")
            if day != binding["trade_date"] or day > binding["as_of"]:
                shared._reject("date_window")
            rows.append([day, _amount(cells["rzye"])])
        return rows
    except (shared.MoneyflowUnavailable, AnalysisBudgetStopped, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass
    shared._reject("decoder_internal")


def _send(binding, token, *, connection_factory=http.client.HTTPSConnection, clock=time.monotonic):
    expected = {"symbol", "as_of", "trade_date", "identity_sha256", "snapshot_id"}
    if (type(binding) is not dict or set(binding) != expected
            or shared._symbol(binding["symbol"]) != binding["symbol"]
            or shared._date(binding["trade_date"]) > shared._date(binding["as_of"])
            or shared._date(binding["as_of"]) > datetime.now(shared.ZoneInfo("Asia/Shanghai")).date()
            or re.fullmatch(r"[a-f0-9]{64}", binding["identity_sha256"]) is None
            or re.fullmatch(r"snapshot:v2:[a-f0-9]{64}", binding["snapshot_id"]) is None):
        raise shared.MoneyflowUnavailable("identity_invalid")
    if type(token) is not str or not token:
        raise shared.MoneyflowUnavailable("authentication")
    body = json.dumps({"api_name": "margin_detail", "token": token, "fields": ",".join(FIELDS),
        "params": {"ts_code": binding["symbol"], "trade_date": binding["trade_date"].replace("-", ""),
                   "ts_type_name": "https://api.waditu.com/dataapi"}}, allow_nan=False).encode()
    return shared._post(body, PATH, connection_factory=connection_factory, clock=clock)


class _Run:
    def __init__(self, config, ticker, as_of, trade_date):
        self.config, self.ticker, self.as_of, self.trade_date = dict(config), ticker, as_of, trade_date
        self.result = None
        self.used = False
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.transport = {"io_registered": False, "request_submission": "not_submitted"}
        self.prepared = False

    def cancel(self):
        self.cancelled.set()

    def _check_cancel(self):
        if self.cancelled.is_set():
            raise shared.MoneyflowUnavailable("cancelled")

    def acquire(self, state):
        with self.lock:
            budget = active_budget()
            if budget is None:
                raise shared.MoneyflowUnavailable("not_configured")
            budget.check()
            if self.used and self.result is None:
                raise AnalysisBudgetStopped("margin_attempt_already_started")
            try:
                binding = _binding(state, self.ticker, self.as_of, self.trade_date)
                _require_active_snapshot(state)
            except (ValueError, TypeError, KeyError):
                if self.used and self.result != _MISSING_BINDING:
                    raise AnalysisBudgetStopped("margin_binding_changed") from None
                self.used, self.result = True, dict(_MISSING_BINDING)
                return dict(self.result)
            if self.used:
                if self.result == _MISSING_BINDING or any(self.result.get(k) != v for k, v in binding.items()):
                    raise AnalysisBudgetStopped("margin_binding_changed")
                return json.loads(json.dumps(self.result))
            if state.get("margin_balance_observation") is not None:
                raise AnalysisBudgetStopped("margin_restore_unsupported")
            self.used = True
            return self._collect(binding, budget)

    def prepare(self, state):
        """Validate the active snapshot here; no graph state or frame crosses threads."""
        budget = active_budget()
        if budget is None:
            raise shared.MoneyflowUnavailable("not_configured")
        budget.check()
        with self.lock:
            if self.prepared or self.used:
                raise AnalysisBudgetStopped("margin_attempt_already_started")
            self.prepared = True
        if state.get("margin_balance_observation") is not None:
            raise AnalysisBudgetStopped("margin_restore_unsupported")
        try:
            binding = tuple(_binding(state, self.ticker, self.as_of, self.trade_date).items())
            _require_active_snapshot(state)
        except (ValueError, TypeError, KeyError):
            self.result = dict(_MISSING_BINDING)
            return None

        def collect():
            with self.lock:
                if self.used or active_budget() is not budget:
                    raise AnalysisBudgetStopped("margin_binding_changed")
                self.used = True
                budget.check()
                self._check_cancel()
                token = shared._DEFERRED_MARGIN.set((self._check_cancel, self.transport))
                try:
                    return self._collect(dict(binding), budget)
                finally:
                    shared._DEFERRED_MARGIN.reset(token)
        return collect

    def _collect(self, binding, budget):
        before = budget.receipt()["data_attempts"]
        rows, projection_sha, received_at, diagnostic, status = [], None, None, None, "available"
        try:
            rows, projection_sha, received_at = shared._coordinate(self.config, binding, api="margin_detail")
            if not rows:
                status = "empty"
        except shared.MoneyflowUnavailable as error:
            status, diagnostic = shared._reason(error), error.diagnostic
        budget.check()
        value = {"contract": VERSION, **binding, "provider": "tushare.margin_detail", "unit": "CNY",
                 "received_at": received_at, "provenance": "retrospective_backfill", "status": status,
                 "rows": rows, "projected_data_sha256": projection_sha, "diagnostic": diagnostic,
                 "attempts": budget.receipt()["data_attempts"] - before}
        value["normalized_sha256"] = shared._digest(value)
        self.result = value
        return json.loads(json.dumps(value))


@contextlib.contextmanager
def margin_balance_scope(config, *, ticker, as_of, trade_date):
    if _SCOPE.get() is not None:
        raise shared.MoneyflowUnavailable("not_configured")
    marker = _SCOPE.set(_Run(config, ticker, as_of, trade_date))
    try:
        yield
    finally:
        _SCOPE.reset(marker)


def margin_balance_for_state(state):
    run = _SCOPE.get()
    return None if run is None else run.acquire(state)


def margin_balance_markdown(state, *, language="Chinese"):
    value = state.get("margin_balance_observation")
    if value is None:
        return ""
    chinese = language.lower() in {"chinese", "zh", "zh-cn", "中文"}
    invalid = "融资余额：材料绑定无效，未采用。" if chinese else "Financing balance: invalid binding; excluded."
    if value in (_MISSING_BINDING, {"contract": LEGACY_VERSION, "status": "binding_unavailable"}):
        return ("## 融资余额（辅助观察）\n\n指定交易日与已验证市场快照不能绑定；未发起查询，不代表余额为零。"
                if chinese else "## Financing balance (advisory)\n\nSelected date lacks a matching verified snapshot; no query, not zero balance.")
    try:
        binding = _binding(state, state.get("company_of_interest"), state.get("trade_date"), value["trade_date"])
        current = type(value) is dict and value.get("contract") == VERSION
        digest_key = "projected_data_sha256" if current else "raw_response_sha256"
        expected = {"contract", *binding, "provider", "unit", "received_at", "provenance", "status", "rows",
                    digest_key, "diagnostic", "attempts", "normalized_sha256"}
        if (type(value) is not dict or set(value) != expected or value["contract"] not in {VERSION, LEGACY_VERSION}
                or any(value[k] != v for k, v in binding.items())
                or value["normalized_sha256"] != shared._digest({k: v for k, v in value.items() if k != "normalized_sha256"})
                or value["provider"] != "tushare.margin_detail" or value["unit"] != "CNY"
                or value["provenance"] != "retrospective_backfill" or value["status"] not in shared._REASONS
                or type(value["attempts"]) is not int or value["attempts"] not in (0, 1)
                or not shared._valid_diagnostic(value["diagnostic"])):
            return invalid
        diagnostic = value["diagnostic"]
        if diagnostic is not None and (diagnostic["contract"] != ("moneyflow-diagnostic-v3" if current else "moneyflow-diagnostic-v2")
                                       or diagnostic["category"] != value["status"]):
            return invalid
        if value["received_at"] is not None:
            received = datetime.fromisoformat(value["received_at"])
            if received.utcoffset() != timedelta(0) or received > datetime.now(timezone.utc) + timedelta(seconds=1):
                return invalid
        if current:
            if value["status"] in {"available", "empty"}:
                if value[digest_key] != shared._projection_digest("margin_detail", binding, value["rows"]):
                    return invalid
            elif value[digest_key] is not None or value["received_at"] is not None:
                return invalid
        title = "## 融资余额（辅助观察，不改变评级）" if chinese else "## Financing balance (advisory; rating unchanged)"
        scope = f"\n\n{binding['symbol']} | as-of: {binding['as_of']} | trade_date: {binding['trade_date']}\n"
        limits = ("\n单日余额不是净流入、持仓变化或趋势；不能据此预测或改变评级。\n"
                  "事后历史回补（retrospective_backfill），不证明截止时已经发布，不适用严格时点回测。\n"
                  "未返回记录不代表零余额、无杠杆、清偿或停牌；不自动改用另一日期。") if chinese else (
                  "\nOne-day balance is not net flow, holdings change or a trend; no prediction or rating change.\n"
                  "Retrospective backfill (retrospective_backfill), not proof of publication by the cutoff or strict PIT eligibility.\n"
                  "No row does not imply zero balance, no leverage, repayment or suspension; no date fallback.")
        if value["status"] != "available":
            if value["rows"]:
                return invalid
            text = "未取得合格融资余额；状态：" if chinese else "No qualified financing balance; status: "
            return title + scope + text + value["status"] + limits
        if (diagnostic is not None or value["received_at"] is None
                or re.fullmatch(r"[a-f0-9]{64}", value[digest_key] or "") is None
                or type(value["rows"]) is not list or len(value["rows"]) != 1):
            return invalid
        row = value["rows"][0]
        if (type(row) is not list or len(row) != 2 or row[0] != binding["trade_date"]
                or type(row[1]) is not str or len(row[1]) > 30):
            return invalid
        amount = _amount(Decimal(row[1]))
        label = "融资余额 rzye：" if chinese else "Financing balance rzye: "
        unit = " 元人民币" if chinese else " CNY (yuan)"
        return (title + scope + label + amount + unit + limits
                + "\n\nSource: https://tushare.pro/document/2?doc_id=59"
                + f"\nReceived UTC: {value['received_at']}\nNew sends: {value['attempts']}"
                + " (0 = reused result; original receipt time retained)"
                + ("\n只展示已校验投影，不证明响应全量；额外元数据未留存或哈希。" if chinese and current else
                   "\nValidated projection only; completeness unproven; extra metadata neither retained nor hashed." if current else "")
                + f"\nSnapshot: {binding['snapshot_id']}\n{'Validated projection' if current else 'Raw response'} SHA256: {value[digest_key]}"
                + f"\nNormalized observation SHA256: {value['normalized_sha256']}")
    except Exception:
        return invalid
