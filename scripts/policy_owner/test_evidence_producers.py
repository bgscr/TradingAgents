"""Real source/capture CLI rehearsal against fresh disposable inputs only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    source = Path(__file__).resolve().parents[2]
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external disposable output required")
    output.mkdir()
    unrelated = output / "unrelated-cwd"
    unrelated.mkdir()
    repository = output / "DISPOSABLE-SOURCE"
    repository.mkdir()
    commands = []

    def run(argv, *, cwd=unrelated, expected=0):
        if "produce-source" in argv and "--git-executable" not in argv:
            selected = Path(shutil.which("git"))
            argv = [
                *argv,
                "--git-executable",
                str(selected),
                "--git-sha256",
                hashlib.sha256(selected.read_bytes()).hexdigest(),
            ]
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        commands.append(
            {
                "argv": list(map(str, argv)),
                "cwd": str(cwd),
                "exit": result.returncode,
                "stdout": result.stdout.decode("utf-8", "replace"),
                "stderr": result.stderr.decode("utf-8", "replace"),
            }
        )
        (output / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
        assert result.returncode == expected, commands[-1]
        return result.stdout

    def git(*values):
        return run(
            [
                "git",
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Isolated Evidence Fixture",
                "-c",
                "user.email=fixture.invalid@example.invalid",
                *values,
            ],
            cwd=repository,
        )

    git("init", "--initial-branch=fixture")
    (repository / "README.md").write_bytes(
        b"SYNTHETIC GIT FIXTURE. NO APPROVAL OR QUALIFICATION.\n"
    )
    git("add", "--", "README.md")
    git("commit", "-m", "Synthetic parent only")
    (repository / "tradingagents").mkdir()
    (repository / "tradingagents/__init__.py").write_bytes(b'"""Disposable fixture package."""\n')
    (repository / "tradingagents/example.py").write_bytes(b"value = 17\n")
    git("add", "--", "tradingagents")
    git("commit", "-m", "Synthetic inventory fixture only")
    commit, tree, parent = (
        git("show", "-s", "--format=%H%n%T%n%P", "HEAD").decode().strip().splitlines()
    )
    candidate = output / "candidate.json"
    candidate.write_text(
        json.dumps({"commit": commit, "tree": tree, "parent": parent}), encoding="utf-8"
    )
    cli = source / "scripts/policy_owner/acquire_evidence.py"
    prefix = [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(cli),
        "--development-dependencies",
        str(args.dependencies),
        "--fixture-only",
    ]
    inventory = output / "source-inventory.json"
    run(
        [
            *prefix,
            "produce-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--output",
            str(inventory),
        ]
    )
    selected = Path(shutil.which("git"))
    run(
        [
            *prefix,
            "produce-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--output",
            str(output / "must-not-produce.json"),
            "--git-executable",
            str(selected),
            "--git-sha256",
            "0" * 64,
        ],
        expected=1,
    )
    assert not (output / "must-not-produce.json").exists()
    run(
        [
            *prefix,
            "verify-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--inventory",
            str(inventory),
        ]
    )
    # Existing Windows worktrees may use Git's CRLF checkout representation.
    # Preserve raw physical pins separately instead of pretending they equal LF blobs.
    git("config", "core.autocrlf", "true")
    (repository / "tradingagents/example.py").write_bytes(b"value = 17\r\n")
    crlf_inventory = output / "source-inventory-crlf.json"
    run(
        [
            *prefix,
            "produce-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--output",
            str(crlf_inventory),
        ]
    )
    run(
        [
            *prefix,
            "verify-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--inventory",
            str(crlf_inventory),
        ]
    )
    crlf_value = json.loads(crlf_inventory.read_bytes())
    assert any(
        row["checkout_form"] == "UTF8_CRLF_CHECKOUT" for row in crlf_value["physical_entries"]
    )
    source_alias = output / "source-output-alias"
    run(["cmd.exe", "/d", "/c", "mklink", "/J", str(source_alias), str(repository)])
    run(
        [
            *prefix,
            "produce-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--output",
            str(source_alias / "must-not-publish.json"),
        ],
        expected=1,
    )
    assert (
        not (repository / "must-not-publish.json").exists()
        and not (repository / "must-not-publish.json.partial").exists()
    )
    (repository / "tradingagents/example.py").write_bytes(b"value = 17\n")
    (repository / "tradingagents/example.py").write_bytes(b"value = 99\n")
    run(
        [
            *prefix,
            "verify-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--inventory",
            str(inventory),
        ],
        expected=1,
    )
    (repository / "tradingagents/example.py").write_bytes(b"value = 17\n")
    (repository / "tradingagents/unregistered.py").write_bytes(
        b'raise RuntimeError("must never be imported")\n'
    )
    run(
        [
            *prefix,
            "verify-source",
            "--repository-root",
            str(repository),
            "--candidate",
            str(candidate),
            "--inventory",
            str(inventory),
        ],
        expected=1,
    )
    capture_root = output / "DISPOSABLE-CAPTURE"
    capture_root.mkdir()
    (capture_root / "sample.txt").write_bytes(b"actual disposable capture\n")
    capture_alias = output / "capture-output-alias"
    run(["cmd.exe", "/d", "/c", "mklink", "/J", str(capture_alias), str(capture_root)])
    roots = output / "roots.json"
    roots.write_text(
        json.dumps(
            {
                "contract_version": "policy-evidence-root-list-v1",
                "fixture_only": True,
                "roots": [{"path": str(capture_root), "security_descriptors_requested": True}],
            }
        ),
        encoding="utf-8",
    )
    before, after, changed = (
        output / name for name in ("before.json", "after.json", "changed.json")
    )
    run(
        [
            *prefix,
            "capture",
            "--root-list",
            str(roots),
            "--output",
            str(capture_alias / "must-not-publish.json"),
            "--phase",
            "before",
        ],
        expected=1,
    )
    assert (
        not (capture_root / "must-not-publish.json").exists()
        and not (capture_root / "must-not-publish.json.partial").exists()
    )
    run(
        [
            *prefix,
            "capture",
            "--root-list",
            str(roots),
            "--output",
            str(before),
            "--phase",
            "before",
        ]
    )
    run(
        [
            *prefix,
            "capture",
            "--root-list",
            str(roots),
            "--output",
            str(after),
            "--phase",
            "after",
            "--previous",
            str(before),
        ]
    )
    run(
        [
            *prefix,
            "compare",
            "--before",
            str(before),
            "--after",
            str(after),
            "--output",
            str(output / "equal.json"),
        ]
    )
    (capture_root / "sample.txt").write_bytes(b"NEW DIFFERENCE MUST BLOCK\n")
    run(
        [
            *prefix,
            "capture",
            "--root-list",
            str(roots),
            "--output",
            str(changed),
            "--phase",
            "after",
            "--previous",
            str(before),
        ]
    )
    run(
        [
            *prefix,
            "compare",
            "--before",
            str(before),
            "--after",
            str(changed),
            "--output",
            str(output / "unequal.json"),
        ],
        expected=2,
    )
    report = {
        "status": "PASS_ACTUAL_FIXTURE_PRODUCER_CLIS",
        "source_producer": True,
        "independent_physical_validation": True,
        "explicit_crlf_physical_and_git_pins": True,
        "output_junction_into_observed_scopes_rejected": True,
        "changed_source_rejected": True,
        "extra_import_file_rejected": True,
        "equal_captures": True,
        "new_difference_blocks": True,
        "unrelated_working_directory": True,
        "fixture_git_commits_only": [parent, commit],
        "real_source_candidate_committed": False,
        "human_authentication": False,
        "qualification": False,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
