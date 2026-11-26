"""Windows-only, one local PDF, no network/model/credentials. Run with -I -S -B."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).absolute().parent.parent
BASE = ROOT / ".scratch/user-announcements"
ERROR_CODES = frozenset({
    "input_name", "date", "ticker", "source_url", "future_announcement", "future_as_of",
    "pdf_complexity", "pdf_active_or_external", "pdf_filter", "pdf_size", "pdf_type",
    "pdf_dependency_contract", "pdf_encrypted", "pdf_pages", "pdf_text_unavailable",
    "pdf_text_limit", "pdf_sensitive_marker", "pdf_text_controls", "pdf_parse_rejected",
    "declaration_contract", "stock_label_ambiguous", "stock_conflict", "date_label_ambiguous",
    "date_conflict", "stock_label_missing", "date_label_missing", "output_readback",
    "bank_linked_path", "bank_not_regular_file", "bank_file_size", "bank_file_unreadable",
})


def audit_guard(event, args):
    if event.startswith("socket.") or event.startswith("subprocess.") or event in {
        "os.system", "os.startfile", "os.exec", "os.posix_spawn", "os.fork",
    }:
        raise PermissionError("local_announcement_network_or_process_denied")
    if event == "os.mkdir":
        path = Path(os.path.abspath(os.fsdecode(args[0])))
        if path != BASE / "output" and not (
            path.parent == BASE / "output" and re.fullmatch(r"[0-9a-f]{32}", path.name)
        ):
            raise PermissionError("local_announcement_directory_boundary")
    if event in {"os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.chmod"}:
        raise PermissionError("local_announcement_mutation_denied")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.path.abspath(os.fsdecode(args[0])))
        name = path.name.lower()
        if name.startswith(".env") or name in {"auth.json", "credentials"}:
            raise PermissionError("local_announcement_credentials_denied")
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        )
        if writing:
            allowed = path.parent.parent == BASE / "output" and path.name in {
                "supplement.json", "supplement.md", "READY.json",
            }
        else:
            allowed = any(path.is_relative_to(root) for root in (
                Path(sys.base_prefix) / "Lib", Path(sys.base_prefix) / "DLLs",
                ROOT / ".venv/Lib/site-packages",
            )) or (path.is_relative_to(ROOT / "tradingagents") and path.suffix in {".py", ".pyc"})
            allowed = allowed or path == getattr(audit_guard, "input_path", None)
        if not allowed:
            raise PermissionError("local_announcement_file_boundary")


def bootstrap():
    if os.name != "nt" or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise ValueError("isolated_windows_python_required")
    if Path.cwd() != ROOT:
        raise ValueError("project_working_directory_required")
    keep = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", PYDANTIC_DISABLE_PLUGINS="__all__",
                      PYTHONDONTWRITEBYTECODE="1", LANGCHAIN_TRACING_V2="false")
    logging.disable(logging.CRITICAL)
    sys.addaudithook(audit_guard)
    spec = importlib.util.spec_from_file_location("local_limits", ROOT / "tradingagents/policy_evidence_limits.py")
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    limits.bound_evidence_process(seconds=20, memory_bytes=512 * 1024**2)
    sys.path[:0] = [str(ROOT), str(ROOT / ".venv/Lib/site-packages")]


def run(args):
    from tradingagents import local_announcement as document
    from tradingagents.dataflows.bank_identity_snapshot import read_bound
    from tradingagents.report_storage import _hold_windows_object, _open_windows_exclusive

    claims = document.declaration(args.ticker, args.announcement_date, args.as_of, args.source_url)
    source = BASE / "inbox" / document.input_name(args.file)
    audit_guard.input_path = source
    with ExitStack() as stack:
        # Only project-root and descendants; no drive/outer directory handles.
        for folder in (ROOT, ROOT / ".scratch", BASE, BASE / "inbox"):
            stack.enter_context(_hold_windows_object(folder, directory=True))
        with _hold_windows_object(source, directory=False):
            raw = read_bound(source, maximum=document.MAX_BYTES)
        report = document.prepare(raw, claims)
        if getattr(args, "report_binding", None) is not None:
            # A report caller consumes this bounded pipe directly; no persisted
            # JSON/READY is accepted as proof and no independent output is written.
            content = json.dumps({"contract": "local-announcement-inspection-v1",
                                  "nonce": args.inspection_nonce, "report_binding": args.report_binding,
                                  "report": report}, ensure_ascii=False, allow_nan=False).encode("utf-8")
            document.require(len(content) <= 512 * 1024, "pdf_text_limit")
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
            return 0
        output_parent = BASE / "output"
        output_parent.mkdir(exist_ok=True)
        stack.enter_context(_hold_windows_object(output_parent, directory=True))
        output = output_parent / uuid4().hex
        output.mkdir(exist_ok=False)
        stack.enter_context(_hold_windows_object(output, directory=True))
        hashes = {}
        for name, content in (("supplement.json", document.json_bytes(report)),
                              ("supplement.md", document.render(report))):
            stream = stack.enter_context(_open_windows_exclusive(output / name))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            document.require(stream.read(len(content) + 1) == content, "output_readback")
            hashes[name] = hashlib.sha256(content).hexdigest()
        ready = document.json_bytes({"contract": document.CONTRACT, "files": hashes,
                                     "input_sha256": report["observed"]["input_sha256"]})
        stream = stack.enter_context(_open_windows_exclusive(output / "READY.json"))
        stream.write(ready)
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        document.require(stream.read(len(ready) + 1) == ready, "output_readback")
    print("LOCAL_ANNOUNCEMENT_READY " + str(output.relative_to(ROOT)))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="basename in .scratch/user-announcements/inbox")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--announcement-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--source-url", required=True, help="declared PDF URL, never fetched")
    parser.add_argument("--report-binding", help=argparse.SUPPRESS)
    parser.add_argument("--inspection-nonce", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if ((args.report_binding is None) != (args.inspection_nonce is None)
            or (args.report_binding is not None and (
                re.fullmatch(r"[0-9a-f]{64}", args.report_binding) is None
                or re.fullmatch(r"[0-9a-f]{32}", args.inspection_nonce) is None))):
        parser.error("invalid report inspection binding")
    stage = "bootstrap"
    try:
        bootstrap()
        stage = "local_document"
        return run(args)
    except Exception as error:
        # No dependency messages, document text, traceback, paths, or arbitrary codes.
        from_types = {"AnnouncementError", "BankIdentityError"}
        code = str(error) if type(error).__name__ in from_types else "operation_rejected"
        if code not in ERROR_CODES:
            code = "operation_rejected"
        print(f"LOCAL_ANNOUNCEMENT_STOPPED stage={stage} code={code}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
