"""Offline coverage of publication order and the unresolved historic dispatch."""
import hashlib
import json
import sys

import pytest

from tests.test_bounded_mainland_entry import entry
from tradingagents import full_publication


@pytest.mark.parametrize("case, admissions, sends", [
    ("export_error", 0, 0), ("digest_error", 0, 0), ("success", 1, 1), ("exhausted", 1, 0),
])
def test_optional_send_follows_verified_basic_publication(tmp_path, case, admissions, sends):
    report = tmp_path / "analysis" / "601328.SS" / "2026-11-13" / "runs" / "synthetic"
    export = report / "export"
    export.mkdir(parents=True)
    attempts, observations = [], []
    receipt = report / "basic-publication.json"

    class Budget:
        def admit(self, kind, optional):
            assert (kind, optional) == ("data", True)
            attempts.append(kind)
            return None if case == "exhausted" else 5

        def check(self):
            pass

    def provider(ticker, cutoff, cancel, admit):
        if admit() is not None:
            observations.append(json.loads(receipt.read_bytes())["sha256"] if receipt.exists() else None)
        return []

    content = b"synthetic basic publication"
    digest = hashlib.sha256(content).hexdigest()

    def publish_basic():
        if case == "export_error":
            raise ValueError("synthetic_export_failure")
        path = export / "basic.md"
        path.write_bytes(content)
        (export / "manifest.json").write_text(json.dumps({
            "files": {path.name: "0" * 64 if case == "digest_error" else digest}
        }), encoding="utf8")
        return path

    def publish():
        return full_publication.publish_full(
            {"stock_lhb_detail_em": provider}, Budget(), "601328.SS", "2026-11-13",
            report, publish_basic, graph_valid=True,
        )

    # On the former concurrent publisher, force the worker to finish at submit()
    # so a premature send cannot pass just because the test scheduler was slow.
    if hasattr(full_publication, "ThreadPoolExecutor"):
        import concurrent.futures
        from unittest.mock import patch

        class DrainAtSubmit(concurrent.futures.ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                future.result(timeout=4)
                return future

        scope = patch.object(full_publication, "ThreadPoolExecutor", DrainAtSubmit)
    else:
        from contextlib import nullcontext
        scope = nullcontext()

    with scope:
        if case in ("export_error", "digest_error"):
            reason = "synthetic_export_failure" if case == "export_error" else "basic_export_digest_mismatch"
            with pytest.raises(ValueError, match=reason):
                publish()
            assert not receipt.exists()
        else:
            publish()
            assert json.loads(receipt.read_bytes())["sha256"] == digest

    assert len(attempts) == admissions
    assert observations == [digest] * sends


@pytest.mark.parametrize("ticker", ["601328.SH", "601328.SS"])
@pytest.mark.parametrize("full", [False, True])
def test_check_only_identifies_unreconciled_prior_dispatch(tmp_path, monkeypatch, capsys, ticker, full):
    module = entry()
    required = set(module.FULL_BOUND_FILES)
    required.update("tradingagents/" + name + ".py" for name in (
        "full_matrix", "full_factory", "full_notice", "notice_pages", "full_lpr",
        "full_publication", "scoped_bulletin", "analysis_budget", "budget_data", "full_pair_producer",
    ))
    manifest = {"files": [{"root": "project", "relative": name} for name in sorted(required)]}
    monkeypatch.setattr(module, "SOURCE", tmp_path)
    monkeypatch.setattr(module, "read_json", lambda path: manifest)
    monkeypatch.setattr(module, "verify_release", lambda value: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".venv/Scripts/python.exe"))
    args = ["--ticker", ticker, "--analysis-date", "2026-11-13", "--check-only"]
    monkeypatch.setattr(sys, "argv", ["entry", *args, *(["--full"] if full else [])])
    assert module.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["selection_errors"] == ["prior_dispatch_unreconciled"]
    assert result["ready_for_live"] is False
    assert result["identity"] == result["provider_inputs"] == "not_checked"

def test_research_policy_matches_portable_source_lock(tmp_path, monkeypatch):
    import copy
    from pathlib import Path

    from scripts import build_ordinary_full as builder

    module = entry()
    root = Path(__file__).resolve().parents[1]
    lock = builder.load_recipe()
    filename = "config/portfolio_research_activation.json"
    reviewed = (root / filename).read_bytes()
    # The release recipe permits CRLF normalization when staging Git blobs.
    lf = reviewed.replace(b"\r\n", b"\n")
    candidates = (reviewed, lf, lf.replace(b"\n", b"\r\n"))
    staged = next(raw for raw in candidates if hashlib.sha256(raw).hexdigest() == lock["project"][filename])
    target = tmp_path / filename
    target.parent.mkdir()
    target.write_bytes(staged)
    monkeypatch.setattr(module, "SOURCE", tmp_path)
    rows = [{"root": "project", "relative": name, "sha256": digest}
            for name, digest in lock["project"].items()]
    assert module.research_policy({"files": rows})["budget"] == [60, 3, 1200]
    for source in ("scripts/Start-MainlandAnalysis.ps1", "scripts/analyze_mainland_bounded.py",
                   "tradingagents/full_publication.py"):
        bad = copy.deepcopy(rows)
        next(row for row in bad if row["relative"] == source)["sha256"] = "0" * 64
        with pytest.raises(module.Stop, match="research_activation_unbound"):
            module.research_policy({"files": bad})
    target.write_bytes(staged + b" ")
    with pytest.raises(module.Stop, match="research_activation_changed"):
        module.research_policy({"files": rows})
