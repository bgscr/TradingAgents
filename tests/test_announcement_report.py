"""Synthetic report integration; fake only the subprocess boundary and storage."""

import copy
import json
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_bounded_mainland_entry import entry
from tests.test_local_announcement import claims, synthetic_pdf
from tests.test_supplemental_presentation import MemoryStore, state
from tradingagents import announcement_report as bridge, local_announcement as document, reporting


def report_state(symbol="600919.SH"):
    value = state()
    value["trade_date"] = "2026-11-13"
    value["company_of_interest"] = symbol
    value["evidence_state"]["instrument_identity"].update(
        symbol=symbol, venue="XSHE" if symbol.endswith(".SZ") else "XSHG")
    return value


def request():
    return bridge.AnnouncementRequest("synthetic.pdf", "2026-11-13", claims()["source_url"])


def install_worker(tmp_path, monkeypatch, *, symbol="600919.SH", announced="2026-11-13", mutate=None, failure=None):
    raw = synthetic_pdf(f"SYNTHETIC ONLY\nSecurity code: {symbol}\nAnnouncement date: {announced}\n</pre><script>UNTRUSTED-INSTRUCTION</script>")
    base = tmp_path / ".scratch/user-announcements/inbox"
    base.mkdir(parents=True)
    (base / "synthetic.pdf").write_bytes(raw)
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    calls = []

    def worker(arguments, timeout):
        calls.append((arguments, timeout))
        if failure:
            raise failure
        args = dict(zip(arguments[::2], arguments[1::2], strict=True))
        report = document.prepare(raw, document.declaration(args["--ticker"], args["--announcement-date"],
                                                           args["--as-of"], args["--source-url"]))
        result = {"contract": bridge.PROTOCOL, "nonce": args["--inspection-nonce"],
                  "report_binding": args["--report-binding"], "report": report}
        if mutate:
            mutate(result)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result, ensure_ascii=False).encode())

    monkeypatch.setattr(bridge, "_worker", worker)
    return calls, raw


@pytest.fixture
def memory(monkeypatch):
    MemoryStore.opened = []
    audit_states = []
    monkeypatch.setattr(reporting, "ReportStore", MemoryStore)
    monkeypatch.setattr(reporting, "write_immutable_decision_audit",
                        lambda value, *_args, **_kwargs: audit_states.append(copy.deepcopy(value)))
    return audit_states


def publish(value, attachment, language="Chinese"):
    reporting.write_report_tree(value, value["company_of_interest"], Path("synthetic-report"),
                                output_language=language, company_announcement=attachment)
    return {name.split("/", 2)[2]: data for name, data in MemoryStore.opened[-1].files.items()}


@pytest.mark.parametrize("symbol", ["600919.SH", "000001.SZ"])
@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_actual_report_composer_adds_only_report_appendix(tmp_path, monkeypatch, memory, symbol, language):
    calls, raw = install_worker(tmp_path, monkeypatch, symbol=symbol)
    value = report_state(symbol)
    value["news_report"] = "SYNTHETIC: source unavailable; no complete news coverage."
    value["analysis_budget"] = {"data_gaps": [{"tool": "get_news", "provider": "akshare", "reason": "news_unavailable"}]}
    before = copy.deepcopy(value)
    baseline = publish(value, None, language)
    augmented = publish(value, request(), language)
    text = augmented["complete_report.md"].decode()
    assert ("来源未经认证" if language == "Chinese" else "source unverified") in text
    assert ("同日公告发布时间未知" if language == "Chinese" else "Same-day publication time is unknown") in text
    assert ("外部模型使用许可未确认" if language == "Chinese" else "external-model use not established") in text
    assert symbol in text and sha256(raw).hexdigest() in text
    assert "&lt;script&gt;UNTRUSTED-INSTRUCTION&lt;/script&gt;" in text
    assert "<script>" not in text and "news_unavailable" in text
    assert value == before and memory[0] == memory[1]
    assert augmented["5_portfolio/analysis_outcome.md"] == baseline["5_portfolio/analysis_outcome.md"]
    assert len(calls) == 1
    assert json.loads(augmented["manifest.json"])["files"]["complete_report.md"] == sha256(augmented["complete_report.md"]).hexdigest()


def test_trading_rating_trend_and_news_failure_remain_unchanged(tmp_path, monkeypatch, memory):
    from tests.test_multiyear_integration import combined_state
    from tradingagents.terminal_contract import apply_terminal_contract

    value, _ = combined_state()
    apply_terminal_contract(value, audit_digest="b" * 64)
    value["decision_audit_created_at"] = "2026-07-18T00:00:00Z"
    before = copy.deepcopy(value)
    calls, _ = install_worker(tmp_path, monkeypatch, symbol="600895.SH", announced="2026-07-18")
    baseline = publish(value, None)
    req = bridge.AnnouncementRequest("synthetic.pdf", "2026-07-18", claims()["source_url"])
    augmented = publish(value, req)
    assert "来源未经认证" in augmented["complete_report.md"].decode()
    assert "news_unavailable" in augmented["complete_report.md"].decode()
    assert augmented["5_portfolio/decision.md"] == baseline["5_portfolio/decision.md"]
    assert augmented["1_analysts/multiyear_trend.md"] == baseline["1_analysts/multiyear_trend.md"]
    assert value == before and memory[0] == memory[1] and len(calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(company_of_interest="600895.SH"),
    lambda value: value["evidence_state"].update(instrument_identity=None),
    lambda value: value["evidence_state"]["instrument_identity"].update(venue="XSHE"),
    lambda value: value["evidence_state"]["instrument_identity"].update(currency="USD"),
    lambda value: value.update(trade_date="invalid"),
])
def test_bad_report_binding_never_starts_worker(monkeypatch, mutation):
    value = report_state()
    mutation(value)
    monkeypatch.setattr(bridge, "_worker", lambda *_: pytest.fail("unbound worker start"))
    text = bridge.announcement_appendix(value, "600919.SH", request(), language="Chinese")
    assert "未纳入" in text and "UNTRUSTED" not in text


def test_ss_alias_and_basic_bj_boundary():
    value = report_state("600919.SS")
    binding, _ = bridge._binding(value, "600919.SH")
    assert binding["ticker"] == "600919.SH"
    with pytest.raises(bridge.Rejected):
        bridge._binding(report_state("830001.BJ"), "830001.BJ")


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(nonce="0" * 32),
    lambda value: value.update(report_binding="f" * 64),
    lambda value: value.update(extra="unexpected"),
    lambda value: value["report"].update(source_authenticated=True),
    lambda value: value["report"].update(source_authenticated=0),
    lambda value: value["report"]["observed"].update(input_sha256="0" * 64),
    lambda value: value["report"]["observed"]["pages"][0].update(extracted_text="forged"),
    lambda value: value["report"]["declared"].update(ticker="600895.SH"),
    lambda value: value["report"]["observed"].update(page_count=True),
])
def test_replayed_or_tampered_worker_result_excluded(tmp_path, monkeypatch, mutation):
    calls, _ = install_worker(tmp_path, monkeypatch, mutate=mutation)
    text = bridge.announcement_appendix(report_state(), "600919.SH", request(), language="Chinese")
    assert "未纳入" in text and "forged" not in text and "unexpected" not in text
    assert len(calls) == 1


def test_future_wrong_stock_timeout_and_expired_budget_no_retry(tmp_path, monkeypatch):
    calls, _ = install_worker(tmp_path, monkeypatch, symbol="600895.SH")
    text = bridge.announcement_appendix(report_state(), "600919.SH", request(), language="Chinese")
    assert "未纳入" in text and len(calls) == 1
    calls.clear()
    future = bridge.AnnouncementRequest("synthetic.pdf", "2026-11-14", claims()["source_url"])
    assert "未纳入" in bridge.announcement_appendix(report_state(), "600919.SH", future, language="Chinese")
    assert calls == []
    assert "未纳入" in bridge.announcement_appendix(report_state(), "600919.SH", request(), language="Chinese", timeout=0)
    assert calls == []


def test_secret_exception_and_saved_json_are_not_sources(tmp_path, monkeypatch):
    calls, _ = install_worker(tmp_path, monkeypatch, failure=TimeoutError("FAKE-SECRET"))
    value = report_state()
    value["company_announcement"] = {"source_authenticated": True, "text": "FORGED-SAVED-JSON"}
    assert bridge.announcement_appendix(value, "600919.SH", None, language="Chinese") == ""
    assert calls == []
    text = bridge.announcement_appendix(value, "600919.SH", request(), language="Chinese")
    assert "未纳入" in text and "FAKE-SECRET" not in text and "FORGED" not in text and len(calls) == 1


def test_worker_command_environment_and_timeout(monkeypatch):
    captured = []
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-must-not-forward")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-must-not-forward")
    monkeypatch.setenv("HTTPS_PROXY", "synthetic-must-not-forward")
    monkeypatch.setattr(bridge.subprocess, "run", lambda command, **kw: captured.append((command, kw)))
    bridge._worker(["--file", "synthetic.pdf"], time.monotonic() + 3.5)
    command, kw = captured[0]
    assert command[1:4] == ["-I", "-S", "-B"]
    assert command[4].endswith("prepare_company_announcement.py")
    assert kw["executable"] == command[0]
    assert set(kw["env"]) <= {"SystemRoot", "WINDIR", "TEMP", "TMP"}
    assert 0 < kw["timeout"] <= 3.5 and kw["check"] is False
    assert "shell" not in kw and len(captured) == 1


def test_entry_argument_validation_is_offline_and_explicit(monkeypatch):
    module = entry()
    args = ["--ticker", "000001.SZ", "--analysis-date", "2026-11-13",
            "--announcement-file", "synthetic.pdf", "--announcement-date", "2026-11-13",
            "--announcement-source-url", claims()["source_url"]]
    monkeypatch.setattr(bridge, "_worker", lambda *_: pytest.fail("parse starts child"))
    assert module.parse_arguments(args).announcement_options == ("synthetic.pdf", "2026-11-13", claims()["source_url"])
    assert module.parse_arguments([]).announcement_options is None
    for invalid in (args[:-2], args[4:], [*args[:5], "../bad.pdf", *args[6:]]):
        with pytest.raises(module.Stop):
            module.parse_arguments(invalid)


def test_real_basic_save_hook_does_not_modify_graph_config_or_read_pdf(tmp_path, monkeypatch):
    import cli.main as cli

    module = entry()
    options = ("synthetic.pdf", "2026-11-13", claims()["source_url"])
    session = module.Session(tmp_path, announcement_options=options)
    session.clock = SimpleNamespace(remaining=lambda: 7.5)
    saved = []
    monkeypatch.setattr(cli, "save_report_to_disk", lambda *a, **kw: saved.append((a, kw)))
    original = cli.save_report_to_disk
    config = {"synthetic": True}
    before = copy.deepcopy(config)
    with session.analysis_hooks(cli, config, {}):
        cli.save_report_to_disk("synthetic-state", "600919.SH", "synthetic-output")
        with pytest.raises(module.Stop, match="announcement_request_override_forbidden"):
            cli.save_report_to_disk("state", "ticker", "output", company_announcement={})
    assert cli.save_report_to_disk is original and config == before
    assert saved[0][1]["company_announcement"] == bridge.AnnouncementRequest(*options)
    assert saved[0][1]["announcement_timeout_seconds"] == 25
    assert saved[0][1]["announcement_remaining"]() == 7.5
    assert len(saved) == 1


@pytest.mark.parametrize("remainders,expected_calls", [([0], 0), ([5, 0], 1)])
def test_shared_deadline_checked_at_launch_and_after_worker(tmp_path, monkeypatch, remainders, expected_calls):
    calls, _ = install_worker(tmp_path, monkeypatch)
    values = iter(remainders)
    result = bridge.announcement_appendix(report_state(), "600919.SH", request(), language="Chinese",
                                         remaining=lambda: next(values))
    assert "未纳入" in result and "worker_deadline" in result and len(calls) == expected_calls
