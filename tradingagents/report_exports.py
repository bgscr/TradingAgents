"""Independent report versions; never replace a user's existing entrypoint."""

from __future__ import annotations

import json
import uuid
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
    relative = f"advisory_drafts/{uuid.uuid4().hex}/{filename}"
    with ReportStore(root) as store:
        store.new_directory(relative.rsplit("/", 1)[0])
        store.write_bytes(relative, data)
        return store.root / relative


def preflight_report_paths(store):
    # Legacy files are retained, never followed, adopted as current, or deleted.
    for path in (*LEGACY_DIRECTORIES, *LEGACY_FILES, "complete_report.md", "decision-audit.json"):
        store.inspect(path)


def publish_report_export(store, identity, language, documents, stages):
    label = "本次导出清单" if is_chinese(language) else "This export manifest"
    note = (
        "本次阶段正文仅以同目录清单列出的文件为准。其他版本、根目录旧报告与别名文件均保留，不属于本次导出。"
        if is_chinese(language)
        else "Only files listed in this directory's manifest belong to this export. "
        "Other versions, root reports and old aliases are retained separately."
    )
    documents = {
        **documents,
        "complete_report.md": f"[{label}](manifest.json)\n\n> {note}\n\n"
        + documents["complete_report.md"],
    }
    encoded = {path: text.encode("utf-8") for path, text in documents.items()}
    manifest = {
        "contract_version": "report-export-v2",
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
    content_id = sha256(canonical.encode("utf-8")).hexdigest()
    export_id = uuid.uuid4().hex
    relative = "report_exports/" + export_id
    store.new_directory(relative)
    # Every call owns a fresh version. There is no mutable "current" alias.
    for path, data in sorted(encoded.items()):
        store.write_bytes(f"{relative}/{path}", data)
    store.write_bytes(
        f"{relative}/manifest.json",
        (
            json.dumps(
                {**manifest, "export_id": export_id, "content_id": content_id},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )
    return store.root / relative / "complete_report.md"
