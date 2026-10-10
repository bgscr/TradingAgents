"""Use the exact returned version; never guess which of several exports is current."""

import json
from pathlib import Path


def export_root(report_or_root):
    path = Path(report_or_root)
    if path.name == "complete_report.md":
        assert (path.parent / "manifest.json").is_file()
        return path.parent
    # Convenience only for fixtures that create exactly one independent export.
    manifests = list(path.glob("report_exports/*/manifest.json"))
    assert len(manifests) == 1, "multiple exports require the exact returned report path"
    return manifests[0].parent


def export_manifest(report_or_root):
    return json.loads((export_root(report_or_root) / "manifest.json").read_text(encoding="utf-8"))
