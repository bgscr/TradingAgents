"""Resolve only the version explicitly named by the current report entrypoint."""

import json
import re
from pathlib import Path


def current_export_root(root):
    root = Path(root)
    line = (root / "complete_report.md").read_text(encoding="utf-8").splitlines()[0]
    match = re.fullmatch(
        r"<!-- tradingagents-export: (report_exports/[0-9a-f]{64})/manifest.json -->", line
    )
    assert match, "current export marker is missing or malformed"
    return root / match.group(1)


def current_export_manifest(root):
    return json.loads((current_export_root(root) / "manifest.json").read_text(encoding="utf-8"))
