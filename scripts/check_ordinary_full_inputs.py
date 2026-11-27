"""Check ordinary Full staging inputs without creating a candidate or publishing a release."""
import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def checked(operation):
    try:
        operation()
    except FileNotFoundError:
        return "required_file_missing"
    except OSError:
        return "input_unreadable"
    except subprocess.CalledProcessError:
        return "git_revision_unavailable"
    except ValueError as exc:
        # Report only known builder codes. Exceptions can contain local paths.
        code = str(exc).partition(":")[0]
        if code in {
            "invalid_manifest_backup", "use_separate_target", "local_backup_roots_required", "linked_release_path", "release_recipe_changed", "invalid_release_path",
            "local_evidence_manifest_changed", "local_evidence_root_mismatch",
            "local_evidence_unbound", "local_evidence_source_unbound",
            "local_evidence_digest_mismatch", "missing_git_source", "unreviewed_git_source",
            "runtime_digest_mismatch", "unexpected_runtime_base",
            "pwsh_locator_required", "pwsh_locator_invalid", "pwsh_file_missing",
            "pwsh_digest_mismatch", "pwsh_policy_unverified", "pwsh_policy_missing",
        }:
            return code
        return "invalid_input"
    return "verified"


def verify_powershell(repo, recipe, build, locator):
    if not locator:
        raise ValueError("pwsh_locator_required")
    executable = Path(locator)
    if not executable.is_absolute() or executable.name.lower() != "pwsh.exe" or ".." in executable.parts:
        raise ValueError("pwsh_locator_invalid")

    build["check_local_backup_roots"](repo, executable)
    if not executable.is_file():
        raise ValueError("pwsh_file_missing")
    policy_name = "config/ordinary-full-pair-policy.json"
    policy_path = repo / policy_name
    if build["backup_path_info"](policy_path) is None or not policy_path.is_file():
        raise ValueError("pwsh_policy_missing")
    raw = policy_path.read_bytes()
    # The packager can materialize reviewed text from either checkout line ending.
    lf = raw.replace(b"\r\n", b"\n")
    expected = recipe["project"].get(policy_name)
    for data in (raw, lf, lf.replace(b"\n", b"\r\n")):
        if build["digest"](data) == expected:
            break
    else:
        raise ValueError("pwsh_policy_unverified")
    policy = json.loads(data)
    if policy["executable_name"] != "pwsh.exe" or build["digest"](executable.read_bytes()) != policy["executable_sha256"]:
        raise ValueError("pwsh_digest_mismatch")


def inspect(repo, target, build, *, manifest_backup=None, pwsh=None):
    plain = build["ordinary_path"]
    recipe = build["load_recipe"]()
    manifest = build["source_manifest_path"](repo, manifest_backup, target)
    missing = [name for name in recipe["local_evidence"]["paths"] if not plain(repo / name).is_file()]
    if not manifest.is_file():
        missing.insert(0, "source_manifest_backup" if manifest_backup is not None else "config/bounded_mainland_release.json")
        source = "source_inputs_missing"
    else:
        source = checked(lambda: build["read_sources"](repo, "HEAD", recipe, manifest_backup=manifest_backup, target=target))
    if target.is_relative_to(repo) or repo.is_relative_to(target):
        runtime = "use_separate_target"
    elif not target.is_dir():
        runtime = "target_directory_missing"
    elif not plain(target / ".venv/pyvenv.cfg").is_file():
        runtime = "prepared_venv_missing"
    elif {p.name for p in target.iterdir()} != {".venv"}:
        runtime = "target_requires_only_prepared_venv"
    else:
        runtime = checked(lambda: build["verify_runtime"](target, recipe))
    locator = os.environ.get("TRADINGAGENTS_ORDINARY_FULL_PWSH", pwsh)
    host = checked(lambda: verify_powershell(repo, recipe, build, locator))
    report = {"source": source, "missing": missing, "runtime": runtime, "powershell": host}
    if runtime == "required_file_missing":
        roots = {"python": target / ".venv", "stdlib": Path(sys.base_prefix)}
        report["missing_runtime"] = [
            {"root": group, "relative": name}
            for group, files in recipe["runtime"].items()
            for name in files
            if not plain(roots[group] / name).is_file()
        ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Source checkout of the reviewed revision")
    parser.add_argument("--target", type=Path, required=True, help="Separate candidate directory")
    parser.add_argument("--source-manifest-backup", metavar="RELATIVE_PREIMAGE",
                        help="Read a numbered transaction preimage under the source checkout .scratch directory")
    parser.add_argument("--pwsh", type=Path, help="Path of the PowerShell 7 host intended for Full; inspected without execution")
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("run with -I -S -B")
    # Keep the builder's Git object reads local, including in partial clones.
    os.environ.update(GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
                      GIT_OPTIONAL_LOCKS="0")
    build = runpy.run_path(str(Path(__file__).with_name("build_ordinary_full.py")))
    result = {}

    def run_check():
        if args.source_manifest_backup is not None:
            build["check_local_backup_roots"](args.repo, args.target)
        paths = [build["ordinary_path"](p.absolute()).resolve() for p in (args.repo, args.target)]
        result.update(inspect(*paths, build, manifest_backup=args.source_manifest_backup, pwsh=args.pwsh))

    status = checked(run_check)
    if status != "verified":
        result = {"error": status}
    print(json.dumps(result))
    return 0 if all(result.get(key) == "verified" for key in ("source", "runtime", "powershell")) else 1


if __name__ == "__main__":
    sys.exit(main())
