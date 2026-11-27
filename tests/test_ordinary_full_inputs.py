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
    ("missing_evidence", ("required_file_missing", "verified")),
    ("runtime", ("verified", "runtime_digest_mismatch")),
    ("missing_runtime", ("verified", "required_file_missing")),
    ("occupied", ("verified", "target_requires_only_prepared_venv")),
])
@pytest.mark.parametrize("use_backup", [False, True])
def test_local_checks_use_builder_validators(tmp_path, monkeypatch, damage, expected, use_backup):
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
    canonical = config / "bounded_mainland_release.json"
    backup = ".scratch/test-transaction/0.before" if use_backup else None
    manifest_file = repo / backup if use_backup else canonical
    if use_backup:
        canonical.write_bytes(b"unrelated installation manifest")
        manifest_file.parent.mkdir(parents=True)
    manifest_file.write_bytes(
        manifest + b" " if damage in {"manifest", "manifest_and_missing"} else manifest)
    if damage not in {"manifest_and_missing", "missing_evidence"}:
        (repo / name).write_bytes(b"changed" if damage == "evidence" else evidence)
    if damage != "missing_runtime":
        (venv / "marker").write_bytes(b"changed" if damage == "runtime" else b"fixture")
    if damage == "occupied":
        (target / "keep.txt").write_bytes(b"keep")
    result = inspect(repo, target, build, manifest_backup=backup)
    if use_backup:
        assert canonical.read_bytes() == b"unrelated installation manifest"
    assert (result["source"], result["runtime"]) == expected
    assert result["missing"] == ([name] if damage in {"manifest_and_missing", "missing_evidence"} else [])
    if damage == "missing_runtime":
        assert result["missing_runtime"] == [{"root": "python", "relative": "marker"}]


@pytest.mark.parametrize("relative", ["../0.before", ".scratch/../0.before", ".scratch/run/.env",
                                      ".scratch/run/0.before:other", ".scratch/CON/0.before"])
def test_backup_requires_a_plain_transaction_preimage(tmp_path, relative):
    locate = runpy.run_path(str(BUILD))["source_manifest_path"]
    with pytest.raises(ValueError, match="invalid_manifest_backup"):
        locate(tmp_path / "repo", relative, tmp_path / "target")


def test_backup_cannot_be_a_target_or_a_link(tmp_path):
    locate = runpy.run_path(str(BUILD))["source_manifest_path"]
    repo = tmp_path / "repo"
    rel = ".scratch/txn/0.before"
    backup = repo / rel
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="use_separate_target"):
        locate(repo, rel, backup)
    alias = backup.with_name("1.before")
    alias.hardlink_to(backup)
    with pytest.raises(ValueError, match="invalid_manifest_backup"):
        locate(repo, rel, tmp_path / "target")


def test_network_root_rejected_before_path_lookup(tmp_path, monkeypatch):
    check = runpy.run_path(str(BUILD))["check_local_backup_roots"]
    monkeypatch.setattr(Path, "absolute", lambda *_: pytest.fail("unexpected path lookup"))
    with pytest.raises(ValueError, match="local_backup_roots_required"):
        check(tmp_path, Path("//example.invalid/share/target"))


def test_backup_git_reads_are_local_only(tmp_path, monkeypatch):
    build = runpy.run_path(str(BUILD))
    repo = tmp_path / "repo"
    backup = repo / ".scratch/txn/0.before"
    backup.parent.mkdir(parents=True)
    manifest = json.dumps({"deployment_root": str(repo), "files": []}).encode()
    backup.write_bytes(manifest)
    source = b"reviewed"
    recipe = {"local_evidence": {"paths": [], "source_manifest_sha256": build["digest"](manifest)},
              "project": {"README.md": build["digest"](source)}}
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        env = kwargs["env"]
        assert env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
        if "rev-parse" in command:
            return "fixture"
        return subprocess.CompletedProcess(command, 0, source)

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "check_output", record)
    assert build["read_sources"](repo, "HEAD", recipe, manifest_backup=".scratch/txn/0.before",
                                 target=tmp_path / "target") == {"README.md": source}
    assert len(calls) == 2


def test_backup_parent_is_checked_before_descendants(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace

    inspect_path = runpy.run_path(str(BUILD))["backup_path_info"]
    parent = tmp_path / "parent"
    leaf = parent / "child" / "0.before"
    original = Path.lstat

    def recorded(path):
        if path == parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        assert parent not in path.parents, "a rejected parent must not be traversed"
        return original(path)

    monkeypatch.setattr(Path, "lstat", recorded)
    with pytest.raises(ValueError, match="linked_release_path"):
        inspect_path(leaf)


@pytest.mark.parametrize("case,expected", [
    ("match", "verified"), ("wrong_digest", "pwsh_digest_mismatch"),
    ("missing_host", "pwsh_file_missing"), ("no_locator", "pwsh_locator_required"),
    ("relative", "pwsh_locator_invalid"), ("wrong_name", "pwsh_locator_invalid"),
    ("missing_policy", "pwsh_policy_missing"), ("modified_policy", "pwsh_policy_unverified"),
])
def test_readonly_powershell_check(tmp_path, case, expected):
    check = runpy.run_path(str(CHECK))
    build = runpy.run_path(str(BUILD))
    locator = tmp_path / "pwsh.exe"
    reviewed = b"synthetic host bytes"
    if case != "missing_host":
        locator.write_bytes(b"changed host" if case == "wrong_digest" else reviewed)
    name = "config/ordinary-full-pair-policy.json"
    raw = json.dumps({"executable_name": "pwsh.exe", "executable_sha256": build["digest"](reviewed)}).encode()
    recipe = {"project": {name: build["digest"](raw)}}
    path = tmp_path / name
    path.parent.mkdir()
    if case != "missing_policy":
        path.write_bytes(raw + b" " if case == "modified_policy" else raw)
    locator = {"relative": "pwsh.exe", "wrong_name": tmp_path / "other.exe", "no_locator": None}.get(case, locator)
    assert check["checked"](lambda: check["verify_powershell"](tmp_path, recipe, build, locator)) == expected


def test_preflight_cli_preserves_explicit_locator_precedence(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    target = tmp_path / "target"
    monkeypatch.setenv("TRADINGAGENTS_ORDINARY_FULL_PWSH", "relative.exe")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(CHECK), "--repo", str(repo),
                             "--target", str(target), "--pwsh", str(tmp_path / "pwsh.exe")],
                            capture_output=True, timeout=20)
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout)["powershell"] == "pwsh_locator_invalid"
    assert not target.exists() and list(repo.iterdir()) == []
