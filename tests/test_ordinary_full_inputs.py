"""Read-only first-install diagnostics with synthetic source evidence and runtime pins."""
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CHECK = SCRIPTS / "check_ordinary_full_inputs.py"
BUILD = SCRIPTS / "build_ordinary_full.py"


def test_clean_machine_cli_lists_source_and_runtime_gaps(tmp_path):
    repo = tmp_path / "source checkout"
    repo.mkdir()
    target = tmp_path / "future runtime"
    command = [sys.executable, "-I", "-S", "-B", str(CHECK), "--repo", str(repo),
               "--target", str(target)]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
        assert result.returncode == 1 and not result.stderr
        report = json.loads(result.stdout)
        assert report["source"] == "source_inputs_missing"
        assert report["runtime"] == "target_directory_missing"
        recipe = runpy.run_path(str(BUILD))["load_recipe"]()
        assert report["missing"] == ["config/bounded_mainland_release.json",
                                     *recipe["local_evidence"]["paths"]]
        assert list(repo.iterdir()) == [] and not target.exists()


@pytest.mark.parametrize("damage,expected", [
    ("none", ("verified", "verified")),
    ("manifest", ("local_evidence_manifest_changed", "verified")),
    ("manifest_and_missing", ("local_evidence_manifest_changed", "verified")),
    ("root", ("local_evidence_root_mismatch", "verified")),
    ("evidence", ("local_evidence_digest_mismatch", "verified")),
    ("runtime", ("verified", "runtime_digest_mismatch")),
    ("missing_runtime", ("verified", "required_file_missing")),
    ("occupied", ("verified", "target_requires_only_prepared_venv")),
])
def test_local_checks_use_builder_validators(tmp_path, monkeypatch, damage, expected):
    inspect = runpy.run_path(str(CHECK))["inspect"]
    build = runpy.run_path(str(BUILD))
    repo = tmp_path / "source"
    config = repo / "config"
    config.mkdir(parents=True)
    target = tmp_path / "target"
    venv = target / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\ninclude-system-site-packages = false\n", encoding="utf-8")
    digest = build["digest"]
    evidence = b"synthetic calendar"
    name = "config/example-input.bin"
    pin = digest(evidence)
    manifest = json.dumps({"deployment_root": str(target if damage == "root" else repo),
                           "files": [{"root": "project", "relative": name, "sha256": pin}]}).encode()
    recipe = {"project": {name: pin}, "runtime": {"python": {"marker": digest(b"fixture")}},
              "local_evidence": {"paths": [name], "source_manifest_sha256": digest(manifest)}}
    build["load_recipe"] = lambda: recipe
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fixture-revision")
    (config / "bounded_mainland_release.json").write_bytes(
        manifest + b" " if damage in {"manifest", "manifest_and_missing"} else manifest)
    if damage != "manifest_and_missing":
        (repo / name).write_bytes(b"changed" if damage == "evidence" else evidence)
    if damage != "missing_runtime":
        (venv / "marker").write_bytes(b"changed" if damage == "runtime" else b"fixture")
    if damage == "occupied":
        (target / "keep.txt").write_bytes(b"keep")
    result = inspect(repo, target, build)
    assert (result["source"], result["runtime"]) == expected
    assert result["missing"] == ([name] if damage == "manifest_and_missing" else [])
    if damage == "missing_runtime":
        assert result["missing_runtime"] == [{"root": "python", "relative": "marker"}]
