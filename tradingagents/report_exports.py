"""Immutable presentation versions with one atomic current-report entrypoint."""

from __future__ import annotations

import json
from hashlib import sha256

from tradingagents.report_presentation import is_chinese
from tradingagents.report_storage import ReportStore

LEGACY_DIRECTORIES = ("1_analysts", "2_research", "3_trading", "4_risk", "5_portfolio")
LEGACY_FILES = (
    "market_report.md",
    "sentiment_report.md",
    "news_report.md",
    "fundamentals_report.md",
    "investment_plan.md",
    "trader_investment_plan.md",
    "final_trade_decision.md",
    "1_analysts/market.md",
    "1_analysts/sentiment.md",
    "1_analysts/news.md",
    "1_analysts/fundamentals.md",
    "2_research/bull.md",
    "2_research/bear.md",
    "2_research/manager.md",
    "3_trading/trader.md",
    "4_risk/aggressive.md",
    "4_risk/conservative.md",
    "4_risk/neutral.md",
    "4_risk/manager.md",
    "5_portfolio/decision.md",
    "5_portfolio/analysis_outcome.md",
)


def write_advisory_draft(root, filename, text):
    """Keep streaming stage revisions without replacing legacy root aliases."""
    data = text.encode("utf-8")
    relative = f"advisory_drafts/{sha256(data).hexdigest()}/{filename}"
    with ReportStore(root) as store:
        store.write_bytes(relative, data)
        return store.root / relative


def preflight_report_paths(store):
    # Legacy files are retained, never followed, adopted as current, or deleted.
    for path in (*LEGACY_DIRECTORIES, *LEGACY_FILES, "complete_report.md", "decision-audit.json"):
        store.inspect(path)


def publish_report_export(store, identity, language, documents, stages):
    encoded = {path: text.encode("utf-8") for path, text in documents.items()}
    manifest = {
        "contract_version": "report-export-v1",
        "run_id": identity.run_id,
        "audit_sha256": identity.audit_digest,
        "output_language": language,
        "files": {path: sha256(data).hexdigest() for path, data in sorted(encoded.items())},
        "advisory_stages": sorted(stages),
        "unversioned_root_paths": {
            "disposition": "historical-not-current",
            "directories": list(LEGACY_DIRECTORIES),
            "aliases": list(LEGACY_FILES[:7]),
        },
        "streaming_drafts": "advisory_drafts are historical, not current stage exports",
    }
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    export_id = sha256(canonical.encode("utf-8")).hexdigest()
    relative = "report_exports/" + export_id
    # A changed body creates another presentation version, even for the same
    # immutable decision audit. Repeating identical content is idempotent.
    for path, data in sorted(encoded.items()):
        store.write_bytes(f"{relative}/{path}", data)
    store.write_bytes(
        f"{relative}/manifest.json",
        (
            json.dumps({**manifest, "export_id": export_id}, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
    )
    label = "当前导出清单" if is_chinese(language) else "Current export manifest"
    note = (
        "当前阶段正文仅以本清单中的版本路径为准。根目录旧阶段目录和别名文件保留为历史，不属于当前导出。"
        if is_chinese(language)
        else "Only the version paths in this manifest belong to the current export. "
        "Unversioned stage directories and aliases at the report root are retained history."
    )
    entry = (
        f"<!-- tradingagents-export: {relative}/manifest.json -->\n"
        f"[{label}]({relative}/manifest.json)\n\n> {note}\n\n" + documents["complete_report.md"]
    )
    entry_bytes = entry.encode("utf-8")
    previous = store.read_bytes("complete_report.md")
    if previous == entry_bytes:
        return store.root / "complete_report.md"
    if previous is not None:
        # Preserve manual edits too, even when they retain a valid-looking
        # current marker. Never infer ownership from that marker alone.
        previous_id = sha256(previous).hexdigest()
        backup = (
            f"report_exports/entry-history/{previous_id}.md"
            if previous.startswith(b"<!-- tradingagents-export: ")
            else f"report_exports/legacy-{previous_id}/complete_report.md"
        )
        store.write_bytes(backup, previous)
    # This is the sole current pointer. A crash before replacement leaves the
    # previous complete report current and any incomplete version unreferenced.
    store.write_bytes("complete_report.md", entry_bytes, replace=True)
    return store.root / "complete_report.md"
