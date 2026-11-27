"""Offline regression tests for the run-owned PowerShell news source."""

import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import io
import json
from threading import Event
from unittest.mock import Mock

import pytest

from tradingagents import analysis_budget, powershell_news as news


class MemoryChild:
    """A Popen-shaped child with in-memory pipes; killing it records an exit."""

    def __init__(self, stdout=b"", stderr=b"", *, running=False):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else 0
        self.events = []

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = 1

    def wait(self, timeout):
        assert timeout > 0 and self.returncode is not None
        self.events.append("wait")
        return self.returncode

    def pipes_closed(self):
        return all(pipe.closed for pipe in (self.stdin, self.stdout, self.stderr))


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_interrupted_child_is_reaped_and_pipes_closed(tmp_path, stop):
    child = MemoryChild(running=True)
    launch = Mock(return_value=child)
    cancelled = analysis_budget.AnalysisBudgetStopped("cancelled")
    metadata = {}

    def check():
        if stop == "cancel" and child.stdin.closed:
            raise cancelled

    call = partial(news.run_child, tmp_path / "pwsh.exe", tmp_path / "helper.ps1",
                   "https://fixture.invalid/", remaining=2, launch=launch, check=check,
                   clock=Mock(side_effect=[0, 3]), metadata=metadata)
    if stop == "cancel":
        with pytest.raises(analysis_budget.AnalysisBudgetStopped) as caught:
            call()
        assert caught.value is cancelled
        assert metadata["status"] == "interrupted"
    else:
        body, result = call()
        assert body is None and result["status"] == "timeout"
    assert launch.call_count == 1
    assert child.events == ["kill", "wait"]
    assert metadata["process_closed"] and child.pipes_closed()


@pytest.mark.parametrize(("stream", "status"), [("stdout", "ipc_invalid"), ("stderr", "child_stderr")])
def test_unexpected_child_text_stays_out_of_diagnostics(tmp_path, stream, status):
    marker = "ordinary fixture text outside the protocol"
    child = MemoryChild(**{stream: marker.encode()})
    launch = Mock(return_value=child)
    body, meta = news.run_child(tmp_path / "pwsh.exe", tmp_path / "helper.ps1",
                                "https://fixture.invalid/", remaining=2, check=lambda: None,
                                launch=launch)
    assert body is None and meta["status"] == status
    assert marker not in json.dumps(meta)
    assert meta[stream + "_bytes"] == len(marker)
    assert launch.call_count == 1 and child.events == ["wait"]
    assert meta["process_closed"] and child.pipes_closed()


class ObservedLock:
    """Signal when the second caller reaches the already held production lock."""

    def __init__(self, lock):
        self.lock = lock
        self.contended = Event()

    def acquire(self, timeout):
        if self.lock.locked():
            self.contended.set()
        return self.lock.acquire(timeout=timeout)

    def release(self):
        self.lock.release()


@pytest.mark.parametrize("success", [True, False], ids=["accepted", "unavailable"])
def test_overlapping_news_calls_share_one_attempt_and_budget(tmp_path, monkeypatch, success):
    executable, script = tmp_path / "pwsh.exe", tmp_path / "Get-PublicNews.ps1"
    for path in (executable, script):
        path.write_text("inert fixture", encoding="utf-8")
    source = news.PowerShellNews(executable, script, "600919.SH", "2026-11-13")
    source.lock = ObservedLock(source.lock)
    budget = analysis_budget.AnalysisBudget(1, 60, allowed_data_vendors=["akshare"])
    monkeypatch.setattr(analysis_budget, "active_budget", lambda: budget)

    row = {"date": source.request.end + " 12:00:00", "code": "article-1",
           "title": "600919 合成新闻", "content": "合成内容", "mediaName": "合成媒体"}
    text = source.request.callback + "(" + json.dumps({"result": {"cmsArticleWebOld": [row]}}) + ")"
    envelope = {"status": "ok", "http_status": 200, "cleanup": "closed",
                "request_attempted": True, "body": base64.b64encode(text.encode()).decode()}
    marker = "ordinary fixture message"
    child = MemoryChild(json.dumps(envelope).encode(), b"" if success else marker.encode())
    started = Event()

    def launch_child(*args, **kwargs):
        started.set()
        assert source.lock.contended.wait(5), "second caller did not reach in-flight collection"
        return child

    launch = Mock(side_effect=launch_child)
    monkeypatch.setattr(news, "run_child", partial(news.run_child, launch=launch))

    def acquire(call_id):
        with news.powershell_news_scope(source):
            return news.acquire_powershell_news(
                source.request.ticker, source.request.start, source.request.end,
                tool_call_id=call_id, source_ref="news:synthetic", capability="get_news",
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(acquire, "first")
        assert started.wait(5)
        second = workers.submit(acquire, "second")
        results = [first.result(timeout=10), second.result(timeout=10)]
    results.append(acquire("later"))
    assert launch.call_count == 1 and budget._counts == {"model": 0, "data": 1}
    assert all((result.artifact is not None) == success for result in results)
    assert source.value.status == ("accepted" if success else "powershell_child_stderr")
    summary = source.summary()
    assert summary["transport"]["status"] == ("ok" if success else "child_stderr")
    assert summary["transport"]["process_closed"] and child.pipes_closed()
    assert summary["automatic_retry"] is False and summary["raw_body_retained"] is False
    assert marker not in json.dumps(summary)
