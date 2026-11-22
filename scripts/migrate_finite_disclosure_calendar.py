"""One authorized calendar revision only; no acquisition or coordinator access."""

from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

REVISION = "market-session-calendar=sha256:30547d4ba90760ac3b07702b506ca3f7a0617d776a7a7433b5bee50d18b87048"


def calendar_summary(calendar):
    dates = [item.session_date for item in calendar.sessions]
    counts = Counter(item.status.value for item in calendar.sessions)
    return {
        "revision": calendar.calendar_revision_id,
        "days": len(dates),
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "unique": len(set(dates)) == len(dates),
        "continuous": dates == [dates[0] + timedelta(days=i) for i in range(len(dates))],
        "open": counts["open"],
        "closed": counts["closed"],
        "payload_sha256": sha256(calendar.raw_payload).hexdigest(),
        "observed_at": calendar.observed_at.isoformat(),
        "provider": asdict(calendar.provider),
    }


def require_authorized_calendar(calendar):
    summary = calendar_summary(calendar)
    if (
        summary["revision"] != REVISION
        or summary["days"] != 1827
        or summary["first"] != "2021-10-16"
        or summary["last"] != "2026-10-16"
        or summary["unique"] is not True
        or summary["continuous"] is not True
        or summary["open"] != 1212
        or summary["closed"] != 615
    ):
        raise ValueError("authorized_calendar_mismatch")
    return summary


def migrate(source_root, target_root):
    """Parameters support isolated synthetic tests; CLI uses only fixed paths."""
    from tradingagents.budget_readiness import _readonly_history
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.current_cache import verify_prepared_sse_revision
    from tradingagents.market_history.store import MarketHistoryStore

    source_root, target_root = Path(source_root), Path(target_root)
    target = target_root / "market_history.sqlite3"
    if target_root.exists():
        raise ValueError("calendar_target_already_exists")
    if any(
        p.is_symlink() or p.is_junction()
        for p in (source_root, *source_root.parents, target_root, *target_root.parents)
    ):
        raise ValueError("calendar_path_alias")

    def config(root):
        return {
            "market_history_mode": "current",
            "data_usage_mode": "personal_research",
            "market_history_database_path": str(root / "market_history.sqlite3"),
            "market_history_payload_root": str(root / "payloads"),
            "market_history_backup_root": str(root / "backups"),
        }

    source_db = source_root / "market_history.sqlite3"
    before = source_db.stat()
    with _readonly_history(config(source_root)) as source:
        calendar = verify_prepared_sse_revision(source, REVISION)
        expected = require_authorized_calendar(calendar)
    # New target only, with unchanged source observation and exact content revision.
    from tradingagents.market_history.calendar import MarketSessionCalendarPublication

    # The stored reader's scoped proof marker is not a publication schema. Restore
    # the original Tushare publication shape while retaining exactly its bytes,
    # provider, membership and observation; verify the resulting revision below.
    publication = MarketSessionCalendarPublication(
        provider=calendar.provider,
        reference_market=calendar.reference_market,
        timezone_name=calendar.timezone_name,
        observed_at=calendar.observed_at,
        provenance_class=calendar.provenance_class,
        raw_payload=calendar.raw_payload,
        sessions=calendar.sessions,
    )
    target_root.parent.mkdir(parents=True, exist_ok=True)
    target_root.mkdir(exist_ok=False)
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config(target_root))) as store:
        published = store.publish_session_calendar(publication)
        if published.calendar_revision_id != REVISION:
            raise ValueError("calendar_target_revision_changed")
        actual = require_authorized_calendar(verify_prepared_sse_revision(store, REVISION))
        if actual != expected:
            raise ValueError("calendar_target_evidence_changed")
        tables = {
            row[0]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        # Fail if any price/financial bundle unexpectedly appeared in the new store.
        allowed_nonempty = {
            "schema_migrations",
            "upstream_services",
            "provider_datasets",
            "payload_artifacts",
            "ingestion_runs",
            "market_session_calendars",
            "market_sessions",
            "publication_watermarks",
        }
        for table in tables - allowed_nonempty:
            if not table.replace("_", "").isalnum():
                raise ValueError("calendar_target_schema_identifier")
            if store._connection.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]:
                raise ValueError("calendar_target_unexpected_data")
    after = source_db.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("calendar_source_changed")
    return {
        "contract": "finite-calendar-migration-v1",
        "source_database": str(source_db),
        "target_database": str(target),
        "calendar": actual,
        "source_stat_unchanged": True,
        "physical_requests": 0,
        "coordinator_copied": False,
        "price_payloads_copied": False,
    }


if __name__ == "__main__":
    # Deliberately no general-purpose CLI or network operation.
    import json

    raise SystemExit("No deployment roots or migration authorization configured")
