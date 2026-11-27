"""Read-only verification of a detached SSH signature on a target-specific review."""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SOURCE = Path(__file__).absolute().parents[1]
R = runpy.run_path(str(SOURCE / "scripts/prepare_ordinary_first_install_review.py"))
require = R["require"]
PRINCIPAL = "tradingagents-first-install"
NAMESPACE = "tradingagents-ordinary-first-install-v1"
PURPOSE = "approve-ordinary-full-first-install"
BINDINGS = (
    "release",
    "recipe_sha256",
    "host_binding",
    "target_binding",
    "manifest_sha256",
    "powershell_sha256",
)


def local(path):
    path = Path(path)
    require(not any(os.path.isreserved(s) for s in path.parts[1:]), "invalid_local_path")
    R["ordinary"](path)
    require(path == path.resolve(), "canonical_local_path_required")
    return path


def system_ssh_keygen():
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    require(0 < length < len(buffer), "windows_openssh_required")
    path = local(Path(buffer.value) / "OpenSSH/ssh-keygen.exe")
    require(path.is_file(), "windows_openssh_required")
    return path


def independent_directories(*directories):
    for i, a in enumerate(directories):
        for b in directories[:i]:
            require(
                not a.is_relative_to(b) and not b.is_relative_to(a), "separate_directories_required"
            )


@contextmanager
def stable_input(path, limit):
    """Keep the same ordinary file read-shared only while OpenSSH reopens its name."""
    import msvcrt
    from ctypes import wintypes

    original = R["file_info"](path)
    require(original.st_size <= limit, "oversize_input")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = api.CreateFileW
    open_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    open_file.restype = wintypes.HANDLE
    handle = open_file(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        api.CloseHandle(wintypes.HANDLE(handle))
        raise
    with os.fdopen(fd, "rb") as stream:
        stat = os.fstat(stream.fileno())
        require(os.path.samestat(original, stat) and stat.st_nlink == 1, "approval_input_changed")
        contents = stream.read(limit + 1)
        require(len(contents) <= limit, "oversize_input")
        yield contents
        require(os.path.samestat(stat, R["file_info"](path)), "approval_input_changed")


def validate_signer(raw):
    # Exactly one pinned plain key. No wildcard principals, CAs, options or fallback.
    pattern = PRINCIPAL.encode() + rb" ssh-ed25519 ([A-Za-z0-9+/]{68})(?:\r?\n)?"
    match = re.fullmatch(pattern, raw)
    require(match is not None, "invalid_independent_signer")
    blob = base64.b64decode(match[1], validate=True)
    require(
        len(blob) == 51 and blob[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20",
        "invalid_independent_signer",
    )
    return b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")


def openssh_environment(executable):
    # Windows OpenSSH also requires the OS common application-data location.
    # Obtain the known folder without creating it or inheriting caller overrides.
    folder = ctypes.create_unicode_buffer(260)
    require(
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x23, None, 0, folder) == 0,
        "windows_openssh_required",
    )
    windows = str(executable.parents[2])
    return {"SystemRoot": windows, "WINDIR": windows, "ProgramData": str(local(folder.value))}


def verify_signature(executable, signer, signature, message, fingerprint):
    # The OS component verifies its standard SSHSIG format, namespace and principal.
    # No user SSH configuration, agent, signing command or candidate executable is used.
    result = subprocess.run(
        [
            str(executable),
            "-Y",
            "verify",
            "-f",
            str(signer),
            "-I",
            PRINCIPAL,
            "-n",
            NAMESPACE,
            "-s",
            str(signature),
        ],
        cwd=executable.parent,
        input=message,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=openssh_environment(executable),
        timeout=15,
        check=False,
    )
    require(result.returncode == 0, "signature_not_verified")
    # OpenSSH reopens its inputs by name. Bind its actual successful verification
    # to the key bytes already read, even if a parent name were to change.
    expected = (
        f'Good "{NAMESPACE}" signature for {PRINCIPAL} with ED25519 key '.encode() + fingerprint
    )
    require(result.stdout.splitlines() == [expected], "verified_signer_not_confirmed")


def validity(claims):
    start, end = claims["not_before"], claims["expires"]
    require(type(start) is int and type(end) is int and start < end, "invalid_validity_window")
    now = int(time.time())
    require(start <= now < end, "approval_outside_validity_window")


def verify(source, candidate, inbox, pwsh):
    trust = local(source.parent / "ordinary-full-first-install-authority")
    independent_directories(source, candidate, inbox, trust)
    signer = trust / "allowed_signers"
    message_path, signature = inbox / "approval.json", inbox / "approval.json.sig"
    try:
        R["file_info"](signer)
    except FileNotFoundError:
        raise R["Refused"]("independent_signer_missing") from None
    with (
        stable_input(signer, 1024) as trusted_bytes,
        stable_input(signature, 4096) as signature_bytes,
    ):
        fingerprint = validate_signer(trusted_bytes)
        message = R["small_file"](message_path, 16384)
        verify_signature(system_ssh_keygen(), signer, signature, message, fingerprint)
    claims = json.loads(message.decode("utf-8"), object_pairs_hook=R["unique"])
    require(
        isinstance(claims, dict)
        and set(claims) == {*BINDINGS, "contract", "purpose", "not_before", "expires"},
        "invalid_approval_fields",
    )
    require(
        claims["contract"] == "ordinary-first-install-approval-v1" and claims["purpose"] == PURPOSE,
        "wrong_approval_purpose",
    )
    validity(claims)
    # Re-read the live candidate and reviewed runtime. Never accept a saved request as a measurement.
    measured = R["review"](source, candidate, pwsh)
    for field in BINDINGS:
        require(claims[field] == measured[field], "approval_mismatch_" + field)
    require(
        R["small_file"](signer, 1024) == trusted_bytes
        and R["small_file"](signature, 4096) == signature_bytes
        and R["small_file"](message_path, 16384) == message,
        "approval_inputs_changed",
    )
    require(R["target_binding"](candidate) == measured["target_binding"], "target_changed")
    R["unpublished"](candidate)
    validity(claims)
    return {
        "status": "signature_and_target_verified",
        "release": measured["release"],
        "approval_sha256": R["sha"](message),
        "manifest_sha256": measured["manifest_sha256"],
        "deployment_performed": False,
        "deployment_authority_granted_by_tool": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "approval-dir", "pwsh"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        require(
            os.name == "nt"
            and sys.flags.isolated
            and sys.flags.no_site
            and sys.dont_write_bytecode,
            "isolated_windows_required",
        )
        result = verify(
            local(SOURCE), local(args.candidate), local(args.approval_dir), local(args.pwsh)
        )
    except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError) as error:
        reason = str(error) if type(error) is R["Refused"] else "input_unverifiable"
        print(json.dumps({"status": "blocked", "reason": reason, "deployment_performed": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
