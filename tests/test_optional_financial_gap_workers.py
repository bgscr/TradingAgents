"""Actual owned child processes; synthetic HTTP data only, original guards retained."""

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.diagnostic_runtime import safe_environment

OWNED = []


def test_native_workers_compare_six_outcomes_and_original_timestamps(tmp_path):
    tree = tmp_path / "native"
    tree.mkdir()
    (tree / "acquire").mkdir()
    helper = Path(__file__).with_name("financial_gap_worker.py")
    receipts = []
    for mode in ("synthetic", "reuse"):
        command = [sys.executable, "-I", "-S", "-B", str(helper), str(tree), mode]
        with (
            (tree / (mode + "-stdout.txt")).open("xb") as output,
            (tree / (mode + "-stderr.txt")).open("xb") as error,
        ):
            child = subprocess.Popen(
                command,
                executable=sys.executable,
                env=safe_environment(tree),
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=error,
                cwd=helper.parents[1],
            )
            OWNED.append(child)
            (tree / (mode + "-started.json")).write_text(
                json.dumps({"pid": child.pid, "ppid": os.getpid(), "command": command}),
                encoding="utf-8",
            )
            # Keep owned object; never kill or restart on an uncertain timeout.
            try:
                child.communicate(b"", timeout=90)
            except subprocess.TimeoutExpired:
                (tree / (mode + "-unknown.json")).write_text(
                    json.dumps({"pid": child.pid, "state": "unknown_timeout", "restarted": False}),
                    encoding="utf-8",
                )
                raise
        (tree / (mode + "-exit.json")).write_text(
            json.dumps({"pid": child.pid, "os_exit": child.returncode}), encoding="utf-8"
        )
        assert child.returncode == 0, (mode, child.returncode, str(tree / (mode + "-stderr.txt")))
        folder = tree / "acquire" if mode == "synthetic" else tree
        receipt = json.loads((folder / (mode + "-result.json")).read_text())
        probe = json.loads((folder / "loopback-probe-policy.json").read_text())
        assert probe["status"] == "completed"
        assert receipt["cold_parent_stdlib_only"] is True
        assert receipt["import_access"]["credentials"] == 0
        assert receipt["guard_counts"]["outside_write"] == 0
        receipts.append(receipt)
    first, second = receipts
    assert first["pid"] != second["pid"]
    assert first["physical_requests"] == 3
    assert second["physical_requests"] == second["client_attempts"] == 0
    assert len(first["views"]) == 6 and first["views"] == second["views"]
    assert second["guard_probes"] == {
        "socket": "blocked",
        "environment": "blocked",
        "file": "blocked",
    }
    assert all(value == 0 for value in second["access_during_application"].values())
    assert sum(v["status"] == "unavailable" for v in first["views"].values()) == 2
    assert first["guard_counts"]["network"] == 0
    assert first["guard_counts"]["credentials"] == 0
    assert second["guard_counts"]["network"] == 1  # intentional blocked DNS probe, no send
    assert second["guard_counts"]["credentials"] == 2  # intentional blocked env/file probes
    (Path(os.environ["TEMP"]) / "native-summary.json").write_text(
        json.dumps(
            {
                "pids": [first["pid"], second["pid"]],
                "os_exit": [p.returncode for p in OWNED],
                "physical_requests": [first["physical_requests"], second["physical_requests"]],
                "same_six_outcomes_and_timestamps": first["views"] == second["views"],
                "cold_parent_stdlib_only": True,
                "reuse_access": second["access_during_application"],
                "guard_counts": [first["guard_counts"], second["guard_counts"]],
                "live_qualification": False,
            }
        ),
        encoding="utf-8",
    )
