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
            "linked_release_path", "release_recipe_changed", "invalid_release_path",
            "local_evidence_manifest_changed", "local_evidence_root_mismatch",
            "local_evidence_unbound", "local_evidence_source_unbound",
            "local_evidence_digest_mismatch", "missing_git_source", "unreviewed_git_source",
            "runtime_digest_mismatch", "unexpected_runtime_base",
        }:
            return code
        return "invalid_input"
    return "verified"


def inspect(repo, target, build):
    plain = build["ordinary_path"]
    recipe = build["load_recipe"]()
    inputs = ["config/bounded_mainland_release.json", *recipe["local_evidence"]["paths"]]
    missing = [name for name in inputs if not plain(repo / name).is_file()]
    source = "source_inputs_missing" if inputs[0] in missing else checked(
        lambda: build["read_sources"](repo, "HEAD", recipe)
    )
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
    report = {"source": source, "missing": missing, "runtime": runtime}
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
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("run with -I -S -B")
    # Keep the builder's Git object reads local, including in partial clones.
    os.environ.update(GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
                      GIT_OPTIONAL_LOCKS="0")
    build = runpy.run_path(str(Path(__file__).with_name("build_ordinary_full.py")))
    result = {}

    def run_check():
        paths = [build["ordinary_path"](p.absolute()).resolve() for p in (args.repo, args.target)]
        result.update(inspect(*paths, build))

    status = checked(run_check)
    if status != "verified":
        result = {"error": status}
    print(json.dumps(result))
    return 0 if result.get("source") == result.get("runtime") == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
