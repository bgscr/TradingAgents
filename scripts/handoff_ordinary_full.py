"""Export public Git bytes and stage them on a separately provisioned Windows target."""

import argparse
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).absolute().parent
R = runpy.run_path(str(HERE / "prepare_ordinary_first_install_review.py"))
require, sha, canonical = R["require"], R["sha"], R["canonical"]
RELEASE, PIN = R["RELEASE"], R["LOCK_SHA256"]
LOCK = "config/ordinary-full-release-lock.json"
INDEX = "ordinary-full-transfer.json"
RESERVATION = ".ordinary-full-build"
BUILDER_SHA = "bd757d6b67b1348f30801fb0e876df895719d146597d8c79528065f289fa68db"


def plain(value):
    path = Path(value)
    require(not any(os.path.isreserved(p) or p == ".." for p in path.parts[1:]), "invalid_path")
    R["ordinary"](path)
    require(path.resolve() == path, "path_alias")
    return path


def separate(a, b):
    require(not a.is_relative_to(b) and not b.is_relative_to(a), "separate_directories_required")


def git(repo, args, data=b""):
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        folder, kind = winreg.QueryValueEx(key, "ProgramFilesDir")
    require(kind == winreg.REG_SZ, "system_git_required")
    exe = plain(Path(folder) / "Git/cmd/git.exe")
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
    )
    p = subprocess.run(
        [str(exe), "--no-replace-objects", "-C", str(repo), *args],
        cwd=exe.parent,
        input=data,
        capture_output=True,
        env=env,
        timeout=90,
    )
    require(p.returncode == 0, "reviewed_git_unavailable")
    return p.stdout


def recipe(source):
    raw = R["small_file"](source / LOCK, 2_000_000).replace(b"\r\n", b"\n")
    require(sha(raw) == PIN, "reviewed_recipe_required")
    return json.loads(raw)


def public(spec):
    private = set(spec["local_evidence"]["paths"])
    require(len(private) == 3 and private <= spec["project"].keys(), "invalid_evidence_set")
    for name in spec["project"]:
        require(
            all(
                part and part not in {".", ".."} and not os.path.isreserved(part)
                for part in name.split("/")
            )
            and "\\" not in name,
            "invalid_recipe_path",
        )
    require(
        not {R["STAGED"], R["FORMAL"], INDEX}.intersection(spec["project"]), "receipt_in_recipe"
    )
    return {n: dg for n, dg in spec["project"].items() if n not in private}


def packet(files):
    return {
        "contract": "ordinary-full-public-handoff-v1",
        "revision": RELEASE,
        "recipe_sha256": PIN,
        "files": files,
    }


def inventory(root, allowed, opaque=()):
    """Reject extra names and links before reading any file contents."""
    directories = {p.as_posix() for n in allowed for p in Path(n).parents}
    seen = set()

    def visit(directory):
        for p in directory.iterdir():
            name = p.relative_to(root).as_posix()
            require(name in allowed or name in directories or name in opaque, "unexpected_member")
            plain(p)
            if name in opaque:
                require(p.is_dir(), "invalid_directory")
            elif name in allowed:
                R["file_info"](p)
                seen.add(name)
            else:
                require(p.is_dir(), "invalid_directory")
                visit(p)

    visit(root)
    require(seen == set(allowed), "missing_member")


def create(path, data):
    plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def export(source, output, spec):
    separate(source, output)
    require(not output.exists(), "output_exists")
    require(
        Path(git(source, ["rev-parse", "--show-toplevel"]).decode().strip()) == source,
        "exact_source_required",
    )
    files = public(spec)
    requested = "".join(f"{RELEASE}:{name}\n" for name in files).encode()
    stream = io.BytesIO(git(source, ["cat-file", "--batch"], requested))
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="handoff-") as temp:
        staged = Path(temp) / "public"
        staged.mkdir()
        for name, expected in files.items():
            header = stream.readline().split()
            require(len(header) == 3 and header[1] == b"blob", "missing_reviewed_blob")
            data = stream.read(int(header[2]))
            require(stream.read(1) == b"\n", "invalid_batch")
            lf = data.replace(b"\r\n", b"\n")
            for candidate in (data, lf, lf.replace(b"\n", b"\r\n")):
                if sha(candidate) == expected:
                    create(staged / name, candidate)
                    break
            else:
                raise R["Refused"]("unreviewed_blob")
        require(stream.read(1) == b"", "invalid_batch")
        create(staged / INDEX, canonical(packet(files)))
        staged.rename(output)  # Windows refuses an existing destination.


def verify(package, spec):
    files = public(spec)
    inventory(package, set(files) | {INDEX})
    require(
        R["small_file"](package / INDEX, 2_000_000) == canonical(packet(files)),
        "transfer_index_mismatch",
    )
    for name, expected in files.items():
        require(R["file_hash"](package / name) == expected, "transfer_digest_mismatch")
    return files


def reservation(target):
    return canonical(
        {
            "contract": "ordinary-full-fresh-stage-v1",
            "revision": RELEASE,
            "recipe_sha256": PIN,
            "directory": R["target_binding"](target),
        }
    )


def reserve(source, target):
    separate(source, target)
    target.mkdir()  # No adoption of an existing directory, including an empty one.
    create(target / RESERVATION / "reservation.json", reservation(target))


def provenance(source, origin, target, backup, spec):
    """Read the exact existing builder, only from the independent tool checkout."""
    path = source / "scripts/build_ordinary_full.py"
    data = R["small_file"](path, 200_000).replace(b"\r\n", b"\n")
    require(sha(data) == BUILDER_SHA, "reviewed_builder_required")
    builder = {"__name__": "reviewed_source_locator", "__file__": str(path)}
    exec(compile(data, str(path), "exec"), builder)
    manifest_path = builder["source_manifest_path"](origin, backup, target)
    provenance_from_manifest(origin, manifest_path, target, spec)


def provenance_from_manifest(origin, manifest_path, target, spec):
    raw = R["small_file"](plain(manifest_path), 2_000_000)
    evidence = spec["local_evidence"]
    require(sha(raw) == evidence["source_manifest_sha256"], "evidence_source_mismatch")
    manifest = json.loads(raw, object_pairs_hook=R["unique"])
    require(Path(manifest["deployment_root"]) == origin, "evidence_root_mismatch")
    for name in evidence["paths"]:
        expected = spec["project"][name]
        bound = [
            r["sha256"]
            for r in manifest["files"]
            if r["root"] == "project" and r["relative"] == name
        ]
        require(
            bound == [expected]
            and R["file_hash"](origin / name) == expected
            and R["file_hash"](target / name) == expected,
            "evidence_binding_mismatch",
        )


def release_check(target, spec):
    # Use exactly the release verifier that the pinned builder runs, under the same
    # isolated interpreter. Only recipe-matching source bytes enter compile().
    name = "scripts/analyze_mainland_bounded.py"
    data = R["small_file"](target / name, 2_000_000)
    require(sha(data) == spec["project"][name], "release_verifier_changed")
    scope = {"__name__": "ordinary_release_check", "__file__": str(target / name)}
    exec(compile(data, str(target / name), "exec"), scope)
    rows = [
        {"root": group, "relative": path, "sha256": digest}
        for group, table in {"project": spec["project"], **spec["runtime"]}.items()
        for path, digest in table.items()
    ]
    manifest = {
        "contract": "bounded-mainland-release-v1",
        "deployment_root": str(target),
        "files": rows,
        "old_authorization_reused": False,
        "installation_required": True,
    }
    scope["verify_release"](manifest, source=target, python_root=target / ".venv")


def stage(source, package, origin, target, pwsh, backup, spec):
    for a, b in (
        (target, source),
        (target, package),
        (target, origin),
        (package, source),
        (package, origin),
    ):
        separate(a, b)
    files = verify(package, spec)
    marker = RESERVATION + "/reservation.json"
    inventory(target, {marker, *spec["local_evidence"]["paths"]}, opaque={".venv"})
    initial = reservation(target)
    require(R["small_file"](target / marker, 4096) == initial, "not_reserved")
    provenance(source, origin, target, backup, spec)
    base = plain(Path(sys.base_prefix))
    R["verify_venv"](target / ".venv/pyvenv.cfg", base)
    for group, table in spec["runtime"].items():
        root = {"python": target / ".venv", "stdlib": base}[group]
        for name, expected in table.items():
            require(R["file_hash"](root / name) == expected, "runtime_digest_mismatch")
    policy_name = "config/ordinary-full-pair-policy.json"
    policy_data = R["small_file"](package / policy_name, 8192)
    require(sha(policy_data) == files[policy_name], "host_policy_changed")
    policy = json.loads(policy_data)
    override = os.environ.get("TRADINGAGENTS_ORDINARY_FULL_PWSH")
    require(override is None or plain(override) == pwsh, "powershell_locator_conflict")
    require(
        pwsh.name.lower() == policy["executable_name"]
        and R["file_hash"](pwsh) == policy["executable_sha256"],
        "powershell_mismatch",
    )
    # The operator owns this reserved directory exclusively throughout staging.
    # Every write is create-only. An interruption leaves the stage unpublished.
    for name, expected in files.items():
        data = R["small_file"](package / name, 32_000_000)
        require(sha(data) == expected, "transfer_changed")
        create(target / name, data)
    require(reservation(target) == initial, "target_changed")
    inventory(target, {marker, *spec["project"]}, opaque={".venv"})
    for name, expected in spec["project"].items():
        require(R["file_hash"](target / name) == expected, "staged_digest_mismatch")
    release_check(target, spec)
    receipt = canonical(
        {
            "contract": "ordinary-full-staged-candidate-v1",
            "target": str(target),
            "recipe_sha256": PIN,
        }
    )
    # Rename the complete receipt last. Never replace another receipt.
    with tempfile.TemporaryDirectory(dir=target, prefix="receipt-") as temp:
        prepared = Path(temp) / "pending"
        create(prepared, receipt)
        require(
            reservation(target) == initial and R["small_file"](target / marker, 4096) == initial,
            "target_changed",
        )
        R["unpublished"](target)
        prepared.rename(plain(target / R["STAGED"]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="action", required=True)
    for cmd in ("export", "verify", "reserve", "stage"):
        sub = commands.add_parser(cmd)
        if cmd == "export":
            sub.add_argument(
                "--source-repo", type=Path, help="Local Git source for the fixed product commit"
            )
        if cmd != "reserve":
            sub.add_argument("--package", type=Path, required=True)
        if cmd in {"reserve", "stage"}:
            sub.add_argument("--target", type=Path, required=True)
        if cmd == "stage":
            sub.add_argument("--evidence-origin", type=Path, required=True)
            sub.add_argument("--source-manifest-backup")
            sub.add_argument("--pwsh", type=Path, required=True)
    args = p.parse_args()
    try:
        require(
            os.name == "nt"
            and sys.flags.isolated
            and sys.flags.no_site
            and sys.dont_write_bytecode,
            "isolated_windows_required",
        )
        source = plain(HERE.parent)
        spec = recipe(source)
        if args.action == "reserve":
            reserve(source, plain(args.target))
        elif args.action == "export":
            package = plain(args.package)
            separate(source, package)
            export(
                plain(args.source_repo) if args.source_repo is not None else source, package, spec
            )
        elif args.action == "verify":
            package = plain(args.package)
            separate(source, package)
            verify(package, spec)
        else:
            stage(
                source,
                plain(args.package),
                plain(args.evidence_origin),
                plain(args.target),
                plain(args.pwsh),
                args.source_manifest_backup,
                spec,
            )
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        reason = str(error) if type(error) is R["Refused"] else "input_unverifiable"
        print(json.dumps({"status": "blocked", "reason": reason, "approval_granted": False}))
        return 1
    print(
        json.dumps(
            {
                "status": args.action + "_complete",
                "revision": RELEASE,
                "public_files": len(public(spec)),
                "approval_granted": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
