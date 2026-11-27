"""Stage an ordinary Full release from locked Git blobs into a prepared local runtime."""

import argparse
import hashlib
import json
import ntpath
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

RECIPE = Path(__file__).resolve().parents[1] / "config/ordinary-full-release-lock.json"
RECIPE_SHA256 = "394c46073fd92166ca983f5d00cccfe7602df17ece2b38ef066bbd815da0cbea"
RECEIPT = "config/ordinary-full-package.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_recipe():
    raw = RECIPE.read_bytes().replace(b"\r\n", b"\n")
    if digest(raw) != RECIPE_SHA256:
        raise ValueError("release_recipe_changed")
    return json.loads(raw)


def ordinary_path(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("linked_release_path")
    return path


def backup_path_info(path):
    # Inspect the parent before any lookup below it can traverse a link.
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("linked_release_path")
    return info


def check_local_backup_roots(repo, target):
    for raw in (repo, target):
        windows = PureWindowsPath(raw)
        if windows.drive.startswith("\\\\") or (windows.drive and not windows.root):
            raise ValueError("local_backup_roots_required")
    for raw in (repo, target):
        absolute = raw.absolute()
        if absolute.drive.startswith("\\\\"):
            raise ValueError("local_backup_roots_required")
        if os.name == "nt":
            from ctypes import windll

            # Reject mapped network drives as well as UNC and device paths.
            if windll.kernel32.GetDriveTypeW(absolute.anchor) not in (2, 3, 5, 6):
                raise ValueError("local_backup_roots_required")
        backup_path_info(absolute)


def source_manifest_path(repo, manifest_backup=None, target=None):
    if manifest_backup is None:
        return ordinary_path(repo / "config/bounded_mainland_release.json")

    # Only numbered transaction preimages under this checkout are eligible.
    parts = str(manifest_backup).replace("\\", "/").split("/")
    if (len(parts) < 3 or parts[0] != ".scratch"
            or not re.fullmatch(r"[0-9]+\.before", parts[-1])
            or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", p)
                   or ntpath.isreserved(p) or p.endswith(".") for p in parts[1:-1])):
        raise ValueError("invalid_manifest_backup")
    if target is None or target.is_relative_to(repo) or repo.is_relative_to(target):
        raise ValueError("use_separate_target")
    check_local_backup_roots(repo, target)
    candidate = repo.joinpath(*parts)
    info = backup_path_info(candidate)
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError("invalid_manifest_backup")
    return candidate


def read_sources(repo, revision, recipe, *, manifest_backup=None, target=None):
    manifest = source_manifest_path(repo, manifest_backup, target)
    git_env = None
    if manifest_backup is not None:
        git_env = dict(os.environ, GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1",
                       GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    git = ["git", "--no-replace-objects", "-C", str(repo)]
    commit = subprocess.check_output(git + ["rev-parse", "--verify", "--end-of-options",
                                             revision + "^{commit}"], text=True, env=git_env).strip()
    local = recipe["local_evidence"]
    manifest_bytes = manifest.read_bytes()
    if digest(manifest_bytes) != local["source_manifest_sha256"]:
        raise ValueError("local_evidence_manifest_changed")
    manifest = json.loads(manifest_bytes)
    if Path(manifest["deployment_root"]) != repo:
        raise ValueError("local_evidence_root_mismatch")
    local_paths = set(local["paths"])
    if not local_paths <= recipe["project"].keys():
        raise ValueError("local_evidence_unbound")
    output = {}
    for name, expected in recipe["project"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("invalid_release_path")
        if name in local_paths:
            rows = [row["sha256"] for row in manifest["files"]
                    if row["root"] == "project" and row["relative"] == name]
            if rows != [expected]:
                raise ValueError("local_evidence_source_unbound")
            data = ordinary_path(repo / name).read_bytes()
            if digest(data) != expected:
                raise ValueError("local_evidence_digest_mismatch")
            output[name] = data
            continue
        result = subprocess.run(git + ["cat-file", "blob", commit + ":" + name], capture_output=True, env=git_env)
        if result.returncode:
            raise ValueError("missing_git_source: " + name)
        data = result.stdout
        lf = data.replace(b"\r\n", b"\n")
        for candidate in (data, lf, lf.replace(b"\n", b"\r\n")):
            if digest(candidate) == expected:
                output[name] = candidate
                break
        else:
            raise ValueError("unreviewed_git_source: " + name)
    return output


def verify_runtime(target, recipe):
    venv = ordinary_path(target / ".venv")
    cfg = ordinary_path(venv / "pyvenv.cfg")
    settings = dict(line.split(" = ", 1) for line in cfg.read_text(encoding="utf8").splitlines() if " = " in line)
    if (settings.get("include-system-site-packages", "").lower() != "false"
            or Path(settings.get("home", "")).resolve() != Path(sys.base_prefix).resolve()):
        raise ValueError("unexpected_runtime_base")
    roots = {"python": target / ".venv", "stdlib": Path(sys.base_prefix)}
    for group, files in recipe["runtime"].items():
        for name, expected in files.items():
            path = ordinary_path(roots[group] / name)
            if digest(path.read_bytes()) != expected:
                raise ValueError("runtime_digest_mismatch: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--verify", action="store_true", help="Verify an already staged candidate")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source-manifest-backup", metavar="RELATIVE_PREIMAGE",
                        help="Read a numbered transaction preimage under the source checkout .scratch directory")
    args = parser.parse_args()
    if args.source_manifest_backup is not None:
        check_local_backup_roots(args.repo, args.target)
    repo = args.repo.resolve(strict=True)
    target = ordinary_path(args.target.absolute()).resolve(strict=True)
    if target.is_relative_to(repo) or repo.is_relative_to(target):
        raise ValueError("use_separate_target")
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("run_builder_with_-I_-S_-B")
    recipe = load_recipe()
    sources = read_sources(repo, args.revision, recipe, manifest_backup=args.source_manifest_backup, target=target)
    verify_runtime(target, recipe)
    if not args.verify:
        if {p.name for p in target.iterdir()} != {".venv"}:
            raise ValueError("target_requires_only_prepared_venv")
        (target / ".ordinary-full-build").mkdir()
        for name, data in sources.items():
            path = ordinary_path(target / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
    files = [{"root": group, "relative": name, "sha256": expected}
             for group, entries in {"project": recipe["project"], **recipe["runtime"]}.items()
             for name, expected in entries.items()]
    manifest = dict(contract="bounded-mainland-release-v1", deployment_root=str(target), files=files,
                    old_authorization_reused=False, installation_required=True)
    # Execute only the verifier whose source just matched the compiled recipe pin.
    entry = "scripts/analyze_mainland_bounded.py"
    scope = {"__name__": "ordinary_release_check", "__file__": str(target / entry)}
    exec(compile(sources[entry], str(target / entry), "exec"), scope)
    scope["verify_release"](manifest, source=target, python_root=target / ".venv")
    receipt = dict(contract="ordinary-full-staged-candidate-v1", target=str(target),
                   recipe_sha256=RECIPE_SHA256)
    path = ordinary_path(target / RECEIPT)
    if args.verify:
        if json.loads(path.read_bytes()) != receipt:
            raise ValueError("candidate_receipt_mismatch")
    else:
        with path.open("x", encoding="utf8") as stream:
            json.dump(receipt, stream, indent=2)
    print(f"Verified {len(sources)} project files and {len(files)-len(sources)} runtime files")


if __name__ == "__main__":
    main()
