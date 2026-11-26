"""Explicit report-only local PDF review. No graph/model/SourceFact integration."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).absolute().parent.parent
PROTOCOL = "local-announcement-inspection-v1"
MAX_RESULT = 512 * 1024


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


@dataclass(frozen=True, slots=True)
class AnnouncementRequest:
    """User declarations only; the report supplies identity and cutoff."""

    filename: str
    announcement_date: str
    source_url: str


def _symbol(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{6}\.(SH|SS|SZ)", value), "report_identity")
    return value[:-3] + ".SH" if value.endswith(".SS") else value


def _binding(state, ticker):
    from tradingagents.decision_policy import ValidatedDecisionContext
    from tradingagents.evidence import EvidenceState, InstrumentKind
    from tradingagents.terminal_contract import (
        CanonicalRunIdentity,
        TerminalContract,
        TerminalOutcomeKind,
        validated_trading_decision_from_state,
    )

    terminal = TerminalContract.model_validate(state["terminal_contract"])
    require(terminal.terminal_outcome_kind in {
        TerminalOutcomeKind.ANALYSIS_OUTCOME, TerminalOutcomeKind.TRADING_DECISION,
    }, "report_identity")
    identity = EvidenceState.model_validate(state["evidence_state"]).instrument_identity
    require(identity is not None and identity.is_authoritative, "report_identity")
    symbol = _symbol(identity.symbol)
    venue = {"SH": "XSHG", "SZ": "XSHE"}[symbol[-2:]]

    def matches(item):
        return (_symbol(item.symbol) == symbol and item.venue == venue
                and item.instrument_kind is InstrumentKind.EQUITY and item.currency == "CNY")

    require(matches(identity) and _symbol(ticker) == symbol
            and _symbol(state["company_of_interest"]) == symbol, "report_identity")
    raw_date = state["trade_date"]
    require(type(raw_date) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_date), "report_cutoff")
    cutoff = date.fromisoformat(raw_date)
    if state.get("validated_decision_context") is not None:
        context = ValidatedDecisionContext.model_validate(state["validated_decision_context"])
        require(matches(context.instrument) and context.as_of_date == cutoff, "report_identity")
    if state.get("trading_decision") is not None or terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
        decision = validated_trading_decision_from_state(state)
        require(decision is not None and matches(decision.instrument)
                and decision.as_of_date == cutoff, "report_identity")
    run = CanonicalRunIdentity.model_validate(state["run_identity"])
    require(run.audit_digest is not None, "report_identity")
    binding = {"ticker": symbol, "venue": venue, "kind": "equity", "currency": "CNY",
               "as_of": raw_date, "run_id": run.run_id, "audit_sha256": run.audit_digest,
               "instrument_identity": identity.model_dump(mode="json")}
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    return binding, digest


def _worker(args, deadline):
    """Only this reviewed local interpreter/script; no shell, secrets, or retry."""
    executable = ROOT / ".venv/Scripts/python.exe"
    require(os.name == "nt" and Path(sys.executable).absolute() == executable, "worker_environment")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    timeout = deadline - time.monotonic()
    require(0 < timeout <= 25, "worker_deadline")
    return subprocess.run(
        [str(executable), "-I", "-S", "-B", str(ROOT / "scripts/prepare_company_announcement.py"), *args],
        executable=str(executable), cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=timeout, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _decode_result(raw, *, pdf, claims, nonce, binding):
    from tradingagents import local_announcement as document

    require(type(raw) is bytes and 0 < len(raw) <= MAX_RESULT, "worker_result")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "worker_result")
            result[key] = value
        return result

    parsed = json.loads(raw, object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(Rejected("worker_result")))
    require(type(parsed) is dict and set(parsed) == {"contract", "nonce", "report_binding", "report"}, "worker_result")
    require(parsed["contract"] == PROTOCOL and parsed["nonce"] == nonce
            and parsed["report_binding"] == binding, "worker_result")
    report = parsed["report"]
    require(type(report) is dict, "worker_result")
    # Rebuild every content/flag field from the original pinned bytes and the
    # current worker's text. Never load a persisted supplement.json or READY.
    pages = [item["extracted_text"] for item in report["observed"]["pages"]]
    expected = document._supplement(pdf, pages, claims)
    require(set(report) == set(expected), "worker_result")
    require(document.json_bytes({k: v for k, v in report.items() if k != "reviewed_at"})
            == document.json_bytes({k: v for k, v in expected.items() if k != "reviewed_at"}), "worker_result")
    # Do not accept a worker-provided historical retrieval assertion.
    expected["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    return expected


def _inspect(request, binding, digest, timeout, remaining=None):
    from tradingagents import local_announcement as document
    from tradingagents.dataflows.bank_identity_snapshot import read_bound
    from tradingagents.report_storage import _hold_windows_object

    require(type(request) is AnnouncementRequest, "request_invalid")
    require(type(timeout) in {int, float} and math.isfinite(timeout)
            and 0 < timeout <= 25, "worker_deadline")
    claims = document.declaration(binding["ticker"], request.announcement_date,
                                  binding["as_of"], request.source_url)
    filename = document.input_name(request.filename)
    base = ROOT / ".scratch/user-announcements"
    with ExitStack() as stack:
        for path in (ROOT, ROOT / ".scratch", base, base / "inbox"):
            stack.enter_context(_hold_windows_object(path, directory=True))
        path = base / "inbox" / filename
        stack.enter_context(_hold_windows_object(path, directory=False))
        pdf = read_bound(path, maximum=document.MAX_BYTES)
        nonce = uuid4().hex
        # Report assembly and file binding have already consumed session time.
        # Recalculate at launch; never reuse the save-hook's earlier remainder.
        allowance = timeout if remaining is None else min(timeout, remaining())
        require(type(allowance) in {int, float} and math.isfinite(allowance)
                and 0 < allowance <= 25, "worker_deadline")
        deadline = time.monotonic() + allowance
        result = _worker([
            "--file", filename, "--ticker", binding["ticker"],
            "--announcement-date", request.announcement_date, "--as-of", binding["as_of"],
            "--source-url", request.source_url, "--report-binding", digest, "--inspection-nonce", nonce,
        ], deadline)
        require(result.returncode == 0, "pdf_review_rejected")
        report = _decode_result(result.stdout, pdf=pdf, claims=claims, nonce=nonce, binding=digest)
    require(time.monotonic() < deadline and (remaining is None or remaining() > 0), "worker_deadline")
    return report


def _render(report, binding, digest, chinese):
    def text(en, zh):
        return zh if chinese else en

    observed, claims = report["observed"], report["declared"]
    age = (date.fromisoformat(binding["as_of"]) - date.fromisoformat(claims["announcement_date"])).days
    lines = [text("## User-provided company announcement — source unverified", "## 用户提供的公司公告——来源未经认证"), "",
             text("Local text review only. Not media news, financial facts, model input or decision evidence; no rating change.",
                  "仅供人工复核的本地文本；不作为媒体新闻、财务事实、模型输入或决策证据，不改变评级。"), "",
             f"- {text('Report instrument / cutoff', '本报告证券 / 截止日')}: `{binding['ticker']}` / {binding['as_of']}",
             f"- {text('Declared publication date / age in days', '声明公告日期 / 距截止日天数')}: {claims['announcement_date']} / {age}",
             f"- {text('User-declared URL, not fetched or authenticated', '用户声明 URL，未访问、未认证')}: `{claims['source_url']}`",
             f"- PDF SHA256: `{observed['input_sha256']}`; {text('pages', '页数')}: {observed['page_count']}",
             f"- {text('Run / audit SHA256', '运行 / 审计 SHA256')}: `{binding['run_id']}` / `{binding['audit_sha256']}`",
             f"- {text('Report binding SHA256', '报告绑定 SHA256')}: `{digest}`",
             text("- Text binding: original local PDF re-parsed in a bounded process for this export; saved JSON is not accepted.",
                  "- 文本绑定：本次导出在受限进程中重新解析原 PDF；不接纳已保存 JSON 自证。"),
             text("- Source authentication: unverified; matching document labels do not authenticate an issuer or exchange. The report identity comes from the existing run.",
                  "- 来源认证：未核验；文内标签一致不认证发行人或交易所。报告证券身份来自原运行。"),
             text("- Usage permission: external-model use not established; text was not sent to a model. Local review only.",
                  "- 使用许可：外部模型使用许可未确认；正文未发送模型，仅本地人工复核。"),
             text("- Historical availability: unknown. Date-only metadata cannot prove availability before an intraday cutoff; no PIT replay qualification.",
                  "- 历史可得性：未知。只有日期不能证明在当日分析时点前已公开，不具备历史时点回测资格。"),
             text("- Coverage: one document, no period completeness or freshness claim. An older announcement is not necessarily current; no numeric completeness benefit.",
                  "- 覆盖：单份文件，不证明期间完整性或内容仍然有效；不提高任何完整性评分。"),
             text("- Export hashes cover this appendix's bytes, not its source authority. Embedded instructions are untrusted text only.",
                  "- 导出摘要只覆盖附录字节，不认证来源；正文中的指令仅作为不受信文本。"), ""]
    if age == 0:
        lines += [text("Same-day publication time is unknown; availability before the analysis time is unverified.",
                       "同日公告发布时间未知，不能确认在分析时点前可得。"), ""]
    for page in observed["pages"]:
        lines += [f"### {text('Page', '第')} {page['page']}" + (" 页" if chinese else ""), "",
                  f"SHA256: `{page['text_sha256']}`", "", "<pre>", html.escape(page["extracted_text"], quote=True), "</pre>", ""]
    return "\n\n---\n\n" + "\n".join(lines)


def announcement_appendix(state, ticker, request, *, language, timeout=25, remaining=None):
    if request is None:
        return ""
    from tradingagents.report_presentation import is_chinese

    chinese = is_chinese(language)
    try:
        binding, digest = _binding(state, ticker)
        report = _inspect(request, binding, digest, timeout, remaining)
        return _render(report, binding, digest, chinese)
    except Exception as error:
        # Fixed categories only; never echo payload, source paths, process stdout,
        # original errors or credentials. A failed optional appendix is not news.
        code = str(error) if type(error) is Rejected else "local_review_failed"
        if code not in {"report_identity", "report_cutoff", "request_invalid", "worker_environment",
                        "worker_deadline", "worker_result", "pdf_review_rejected"}:
            code = "local_review_failed"
        return ("\n\n---\n\n## 公司公告补充未纳入\n\n"
                f"本次附录校验被拒绝（`{code}`）；未改用旧材料，不代表没有公司公告。"
                "未向模型提供正文，不影响既有评级。\n" if chinese else
                "\n\n---\n\n## Company announcement supplement excluded\n\n"
                f"Local review rejected (`{code}`); no saved-material fallback. This does not mean no announcements. "
                "No text was sent to models; the existing rating is unchanged.\n")
