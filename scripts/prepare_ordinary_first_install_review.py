"""Produce an unsigned, target-local review request. This command never installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

RELEASE = "2c3c9470dadf5a2b6d5800b5c2d84c36e18b710c"
LOCK_SHA256 = "dd23fdef3b22241801f690c673f507e8118024de4dc04b419cc2dbc60485f838"
STAGED = "config/ordinary-full-package.json"
FORMAL = "config/bounded_mainland_release.json"


class Refused(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def ordinary(path):
    from ctypes import windll

    path = Path(path)
    require(path.is_absolute() and len(path.drive) == 2 and path.drive.endswith(":")
            and ".." not in path.parts, "absolute_local_path_required")
    require(windll.kernel32.GetDriveTypeW(path.anchor) == 3, "fixed_local_volume_required")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        require(not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT, "linked_path")
    return path


def file_info(path):
    info = ordinary(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "ordinary_file_required")
    return info


def small_file(path, limit):
    require(file_info(path).st_size <= limit, "oversize_input")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "oversize_input")
    return data


def file_hash(path):
    file_info(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpublished(target):
    try:
        ordinary(target / FORMAL).lstat()
    except FileNotFoundError:
        return
    raise Refused("already_installed")


def machine_binding():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                        access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        value, kind = winreg.QueryValueEx(key, "MachineGuid")
    from uuid import UUID
    require(kind == winreg.REG_SZ and UUID(value).int != 0, "host_binding_unavailable")
    return sha(canonical(["ordinary-first-install-host-v1", str(UUID(value))]))


def target_binding(path):
    directory = ordinary(path)
    info = directory.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_ino, "directory_identity_unavailable")
    return sha(canonical([os.path.normcase(str(directory)), info.st_dev, info.st_ino]))


def unique(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "duplicate_field")
        result[name] = value
    return result


def verify_venv(path, base):
    # Accept only unambiguous settings also understood by the existing builder.
    settings = {}
    for line in small_file(path, 65536).decode("utf8").splitlines():
        name, separator, value = line.partition("=")
        key = name.strip().casefold()
        if key in {"home", "include-system-site-packages"}:
            require(separator and line.startswith(key + " = ") and key not in settings,
                    "ambiguous_runtime_settings")
            settings[key] = value.strip()
    require(settings.get("include-system-site-packages", "").lower() == "false"
            and ordinary(Path(settings.get("home", ""))) == base, "runtime_base_mismatch")


def review(source, target, pwsh):
    ordinary(source)
    ordinary(target)
    require(not target.is_relative_to(source) and not source.is_relative_to(target), "separate_target_required")
    unpublished(target)
    directory = target_binding(target)
    raw = small_file(source / "config/ordinary-full-release-lock.json", 2_000_000).replace(b"\r\n", b"\n")
    require(sha(raw) == LOCK_SHA256, "reviewed_recipe_required")
    recipe = json.loads(raw)
    packet = json.loads(small_file(target / STAGED, 8192), object_pairs_hook=unique)
    require(packet == dict(contract="ordinary-full-staged-candidate-v1", target=str(target),
                           recipe_sha256=LOCK_SHA256), "staged_receipt_mismatch")
    base = ordinary(Path(sys.base_prefix))
    verify_venv(target / ".venv/pyvenv.cfg", base)
    roots = {"project": target, "python": target / ".venv", "stdlib": base}
    rows = []
    for group, files in {"project": recipe["project"], **recipe["runtime"]}.items():
        for relative, expected in files.items():
            require(file_hash(roots[group] / relative) == expected, "bound_file_mismatch")
            rows.append(dict(root=group, relative=relative, sha256=expected))
    policy_name = "config/ordinary-full-pair-policy.json"
    policy = small_file(target / policy_name, 8192)
    require(sha(policy) == recipe["project"][policy_name], "host_policy_changed")
    policy = json.loads(policy)
    override = os.environ.get("TRADINGAGENTS_ORDINARY_FULL_PWSH")
    if override is not None:
        require(ordinary(Path(override)) == ordinary(pwsh), "powershell_locator_conflict")
    require(pwsh.name.lower() == policy["executable_name"] and file_hash(pwsh) == policy["executable_sha256"],
            "powershell_mismatch")
    # Same byte layout as the reviewed transition; never emit a launcher-consumable file.
    manifest = dict(contract="bounded-mainland-release-v1", deployment_root=str(target), files=rows,
                    old_authorization_reused=False, installation_required=True)
    require(directory == target_binding(target), "target_changed")
    unpublished(target)
    return dict(contract="ordinary-first-install-review-request-v1", release=RELEASE,
                recipe_sha256=LOCK_SHA256, host_binding=machine_binding(), target_binding=directory,
                manifest_sha256=sha(json.dumps(manifest, indent=2).encode()),
                powershell_sha256=policy["executable_sha256"], bound_files=len(rows),
                authorization="absent", status="awaiting_independent_review")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--pwsh", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(os.name == "nt" and sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
                "windows_isolated_python_required")
        result = review(Path(__file__).absolute().parents[1], args.candidate, args.pwsh)
    except (OSError, ValueError, KeyError, RecursionError) as error:
        print(json.dumps(dict(status="blocked", authorization="absent",
                              reason=str(error) if type(error) is Refused else "input_unverifiable")))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
