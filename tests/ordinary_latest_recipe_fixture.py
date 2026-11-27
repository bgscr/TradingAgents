"""Non-installable test worker. The unchanged PowerShell scripts call it only in a disposable lab."""

import atexit
import json
import os
import runpy
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

DAY = "2026-11-26"
NOW = "2026-11-26T17:00:00+08:00"


def worker(site, public, deps, station, role):
    site, public, station = map(Path, (site, public, station))
    reader = site / "scripts/full_calendar_cache.py"
    if (
        not (site.parent / "SYNTHETIC-ONLY").is_file()
        or (site / "config/bounded_mainland_release.json").exists()
    ):
        raise ValueError("training_directory_required")

    audit = {"unexpected": 0, "blocked_local_probes": {}, "permitted_reader_children": 0}

    def finish():
        with (site / ("fixture-guard-" + role + ".jsonl")).open("a", encoding="ascii") as out:
            out.write(json.dumps(audit) + "\n")

    atexit.register(finish)

    def guard(event, args):
        def rejected():
            # Platform identification and urllib3's import-time IPv6 capability probe
            # are both rejected before even creating a socket. Account for them separately.
            frame = sys._getframe(1)
            while frame:
                caller = (frame.f_globals.get("__name__"), frame.f_code.co_name)
                if (event == "socket.gethostname" and caller == ("platform", "_node")) or (
                    event == "socket.__new__" and caller == ("urllib3.util.connection", "_has_ipv6")
                ):
                    counts = audit["blocked_local_probes"]
                    counts[event] = counts.get(event, 0) + 1
                    return
                frame = frame.f_back
            audit["unexpected"] += 1
            audit.setdefault("kinds", []).append(event)

        if event.startswith("socket.") or event in {"os.system", "os.startfile", "os.posix_spawn"}:
            rejected()
            raise PermissionError("training_transport_disabled")
        if event == "subprocess.Popen":
            exe, cmd, cwd, _ = args
            if not (
                role == "entry"
                and audit["permitted_reader_children"] == 0
                and Path(exe) == Path(sys.executable)
                and Path(cwd) == site
                and cmd == expected_command
            ):
                rejected()
                raise PermissionError("training_child_disabled")
            audit["permitted_reader_children"] += 1
        if (
            event == "open"
            and isinstance(args[0], str)
            and Path(args[0]).name.casefold() in {".env", "tradingagents.local.json"}
        ):
            raise PermissionError("training_configuration_disabled")

    import subprocess

    expected_command = subprocess.list2cmdline(
        [sys.executable, "-I", "-B", str(reader), "601328.SS", "latest", NOW]
    )
    sys.addaudithook(guard)
    sys.path.insert(0, str(public))
    sys.path.append(str(deps))  # Explicit dependency directory, without executing any .pth files.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE"] = "1"
    if role == "seed":
        print(json.dumps(seed(site, sys.argv[1])))
        return 0
    try:
        pins = json.loads((station / (site.name + "-pins.json")).read_text())
        files = [{"root": "project", "relative": k, "sha256": v} for k, v in pins.items()]
        index = {"contract": "synthetic-evidence-index-only", "files": files}
        if role in {"reader", "reader_parent_control"}:
            symbol, request, now = sys.argv[1:]
            if (symbol, request, now) != ("601328.SS", "latest", NOW):
                raise ValueError("unexpected_training_request")
            print(json.dumps(read_selection(site, public, index, symbol, now, role)))
            return 0
        bounded = runpy.run_path(str(public / "scripts/analyze_mainland_bounded.py"))
        if "--check-only" not in sys.argv or "--run" in sys.argv or "--use-project-env" in sys.argv:
            raise ValueError("check_only_required")
        if "--auto-latest" not in sys.argv:
            # The historical route is exercised against its own real evidence reader.
            args, _ = bounded["resolve_full_arguments"](
                [s for s in sys.argv[1:] if s != "--check-only"],
                site,
                index,
                datetime.fromisoformat(NOW),
            )
            print(
                json.dumps(
                    {
                        "status": "synthetic_historical_selection",
                        "day": args.analysis_date,
                        "ready_for_live": False,
                    }
                )
            )
            return 0
        entry = SimpleNamespace(
            SOURCE=site, FULL_BOUND_FILES=bounded["FULL_BOUND_FILES"], require=bounded["require"]
        )
        latest = runpy.run_path(str(public / "scripts/full_latest_intake.py"))

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(NOW).astimezone(tz)

        args = [s for s in sys.argv[1:] if s != "--auto-latest"]
        with patch("datetime.datetime", Clock):
            return latest["run"](entry, args, None, None, index)
    except (ValueError, RuntimeError, FileNotFoundError, ModuleNotFoundError) as exc:
        reason = (
            "missing_module_" + str(exc.name)
            if isinstance(exc, ModuleNotFoundError)
            else type(exc).__name__
            if isinstance(exc, OSError)
            else str(exc)
        )
        print(json.dumps({"error": reason, "training_only": True}))
        return 2


def seed(site, exchange):
    """Publish synthetic membership through the product's normal CURRENT SQLite store."""
    from dataclasses import replace

    import pandas as pd

    from tradingagents.dataflows.tushare_market_history import load_calendar
    from tradingagents.market_history.calendar import _session_fields
    from tradingagents.market_history.config import (
        DataUsageMode,
        MarketHistoryConfig,
        MarketHistoryMode,
    )
    from tradingagents.market_history.revisions import RevisionKind, revision_identity
    from tradingagents.market_history.store import MarketHistoryStore

    base = site / "data/market_history"
    mixed = exchange in {"mixed", "conflict"}
    expired = exchange == "expired"
    generic_open = exchange == "mixed"
    if mixed or expired:
        exchange = "SSE"
    config = MarketHistoryConfig(
        MarketHistoryMode.CURRENT,
        base / "market_history.sqlite3",
        base / "payloads",
        base / "backups",
        DataUsageMode.PERSONAL_RESEARCH,
    )

    class FixtureCalendar:
        def fetch_range(self, endpoint, *, start_date, end_date, request_executor, **kwargs):
            if (endpoint, kwargs["exchange"], start_date, end_date) != (
                "trade_cal",
                exchange,
                "20261126",
                "20261126",
            ):
                raise ValueError("unexpected_calendar_fixture_query")
            return request_executor(
                endpoint,
                lambda: pd.DataFrame(
                    [
                        {
                            "exchange": exchange,
                            "cal_date": "20261126",
                            "is_open": "1",
                            "pretrade_date": "20261125",
                        }
                    ]
                ),
            )

    with MarketHistoryStore.open(config) as store:
        if exchange != "missing":
            symbol = "601328.SS" if exchange == "SSE" else "000001.SZ"
            publication = load_calendar(symbol, DAY, DAY, provider=FixtureCalendar())
            # Independent fixture input: compute the revision from the synthetic acquisition,
            # before it enters the candidate database or the selector returns any evidence.
            revision = revision_identity(
                RevisionKind.MARKET_SESSION_CALENDAR,
                {
                    "payload_digest": publication.payload_digest,
                    "provider_dataset_id": publication.provider.provider_dataset_id,
                    "reference_market": publication.reference_market,
                    "sessions": _session_fields(publication.sessions),
                    "timezone_name": publication.timezone_name,
                },
            )
            store.publish_session_calendar(
                replace(
                    publication,
                    observed_at=datetime.fromisoformat(
                        "2026-11-25T17:00:00+08:00" if expired else NOW
                    ),
                )
            )
            if mixed:
                from tradingagents.dataflows.baostock_calendar import (
                    MainlandSessionCalendarMembership,
                    _content_identity,
                )
                from tradingagents.market_history.calendar import (
                    publication_from_baostock_session_calendar_acquisition,
                )

                acquisition = runpy.run_path(
                    str(Path(__file__).parent / "test_market_session_calendar_publication.py")
                )["_acquisition"]
                member = (
                    MainlandSessionCalendarMembership(
                        calendar_date=date.fromisoformat(DAY), is_trading_day=generic_open
                    ),
                )
                observed = datetime.fromisoformat(DAY + "T16:30:00+08:00")
                generic = acquisition(
                    requested_start_date=date.fromisoformat(DAY),
                    requested_end_date=date.fromisoformat(DAY),
                    requested_at=observed,
                    retrieved_at=observed,
                    observed_at=observed,
                    membership=member,
                    content_identity=_content_identity(member),
                )
                store.publish_session_calendar(
                    publication_from_baostock_session_calendar_acquisition(generic)
                )
            return revision
    return None


def read_selection(site, public, index, symbol, now, role):
    from tradingagents.market_history.config import (
        DataUsageMode,
        MarketHistoryConfig,
        MarketHistoryMode,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    reader_root = public.parent / "frozen control" if role == "reader_parent_control" else public
    selector = runpy.run_path(str(reader_root / "scripts/full_calendar_cache.py"))
    verifier = runpy.run_path(str(public / "scripts/full_date_evidence.py"))
    registry = "config/instrument_identity_registry.json"
    (digest,) = [row["sha256"] for row in index["files"] if row["relative"] == registry]
    cfg = {
        "instrument_identity_registry_path": str(public / registry),
        "instrument_identity_registry_sha256": digest,
    }
    base = site / "data/market_history"
    db = base / "market_history.sqlite3"
    marker = base / "payloads/.market-history-database-owner"
    entry = runpy.run_path(str(public / "scripts/analyze_mainland_bounded.py"))
    selector["verify_market_store_paths"](db, marker, entry)
    if marker.read_bytes() != os.path.normcase(str(db)).encode() + b"\n":
        raise ValueError("calendar_store_owner_mismatch")
    config = MarketHistoryConfig(
        MarketHistoryMode.CURRENT,
        db,
        base / "payloads",
        base / "backups",
        DataUsageMode.PERSONAL_RESEARCH,
    )
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        store = MarketHistoryStore(config, con)
        result = selector["select"](
            store,
            cfg,
            symbol,
            "latest",
            datetime.fromisoformat(now),
            release_root=site,
            manifest=index,
            verifier=verifier,
        )
    from hashlib import sha256

    for name in (
        "scripts",
        "scripts.full_auction_schedule",
        "scripts.full_financial_channel",
        "scripts.proposed_latest_target_review",
    ):
        path = Path(sys.modules[name].__file__).resolve(strict=True)
        rel = "scripts/__init__.py" if name == "scripts" else name.replace(".", "/") + ".py"
        (pinned,) = [r["sha256"] for r in index["files"] if r["relative"] == rel]
        if path != public / rel or sha256(path.read_bytes()).hexdigest() != pinned:
            raise ValueError("unexpected_helper_source")
    return result
