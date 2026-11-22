"""Isolated release/migration checks; synthetic calendars are never real evidence."""

import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.dataflows import (
    finite_disclosure_admission as a,
    finite_disclosure_cache as cache,
)

pytest_plugins = ["test_finite_disclosure_admission"]
ROOT = Path(os.environ["FINITE_TEST_ROOT"])


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calendar_actual_store_round_trip_without_acquisition(tmp_path, monkeypatch):
    from tradingagents.dataflows.tushare_market_history import (
        ADJUSTMENT_METHODOLOGY,
        NORMALIZER_VERSION,
    )
    from tradingagents.market_history.calendar import (
        MarketSession,
        MarketSessionCalendarPublication,
        MarketSessionStatus,
    )
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.current_cache import (
        prepared_sse_calendar_provider,
    )
    from tradingagents.market_history.models import ProvenanceClass
    from tradingagents.market_history.store import MarketHistoryStore

    module = script("migrate_finite_disclosure_calendar")
    source = tmp_path / "source"
    target = tmp_path / "target"
    days = [date(2021, 10, 16) + timedelta(days=i) for i in range(1827)]
    rows = [
        {"exchange": "SSE", "cal_date": d.strftime("%Y%m%d"), "is_open": str(int(i < 1212))}
        for i, d in enumerate(days)
    ]
    raw = json.dumps(
        {
            "calendar": rows,
            "normalizer_version": NORMALIZER_VERSION,
            "adjustment_methodology": ADJUSTMENT_METHODOLOGY,
        }
    ).encode()
    publication = MarketSessionCalendarPublication(
        provider=prepared_sse_calendar_provider(),
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        observed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=raw,
        sessions=tuple(
            MarketSession(d, MarketSessionStatus.OPEN if i < 1212 else MarketSessionStatus.CLOSED)
            for i, d in enumerate(days)
        ),
        payload_contract="",
        endpoint="",
        normalizer_version="",
    )
    settings = MarketHistoryConfig.from_mapping(
        {
            "market_history_mode": "current",
            "data_usage_mode": "personal_research",
            "market_history_database_path": str(source / "market_history.sqlite3"),
            "market_history_payload_root": str(source / "payloads"),
            "market_history_backup_root": str(source / "backups"),
        }
    )
    with MarketHistoryStore.open(settings) as store:
        revision = store.publish_session_calendar(publication).calendar_revision_id
    # Explicit synthetic identity substitution; real entrypoint has no such option.
    monkeypatch.setattr(module, "REVISION", revision)
    before = (source / "market_history.sqlite3").read_bytes()
    result = module.migrate(source, target)
    assert result["calendar"]["days"] == 1827 and result["calendar"]["open"] == 1212
    assert result["calendar"]["closed"] == 615 and result["physical_requests"] == 0
    assert (source / "market_history.sqlite3").read_bytes() == before
    assert not (target / "market_history.sqlite3.provider-requests.sqlite3").exists()
    with pytest.raises(ValueError, match="target_already_exists"):
        module.migrate(source, target)


def test_calendar_wrong_counts_or_revision_rejected():
    module = script("migrate_finite_disclosure_calendar")
    with pytest.raises((ValueError, AttributeError, IndexError)):
        module.require_authorized_calendar(SimpleNamespace(sessions=()))


def test_release_needs_current_review_and_original_ttl(installation, tmp_path):
    _, view, profile = installation
    module = script("publish_finite_disclosure")
    tmp_path = tmp_path / "new-release-target"
    current = Path(os.environ["FINITE_TEST_CURRENT"]).read_bytes()
    with pytest.raises(ValueError, match="qualification_missing"):
        module.publish(
            tmp_path,
            view,
            profile["admission_config"],
            current,
            {},
            now=a.utc("2026-10-22T04:00:00Z"),
        )
    with pytest.raises(ValueError, match="original_ttl_expired"):
        module.publish(
            tmp_path,
            view,
            profile["admission_config"],
            current,
            {},
            now=a.utc("2026-10-22T17:56:25.556279Z"),
        )
    assert not (tmp_path / cache.ACTIVE_PATH).exists()


def test_release_full_inventory_required(installation):
    _, _, profile = installation
    path = (
        cache.PROJECT_ROOT
        / "releases/finite-601328-v2"
        / profile["release_sha256"]
        / "manifest.json"
    )
    release = a.decode(path.read_bytes())
    assert set(release["sources"]) == cache.PRODUCT_SOURCES
    assert {
        "tradingagents/finite_disclosure_runtime.py",
        "tradingagents/evidence.py",
        "tradingagents/decision_audit.py",
        "tradingagents/dataflows/finite_disclosure_contracts.py",
        "cli/runtime_artifacts.py",
    } <= cache.PRODUCT_SOURCES
    source = cache.PROJECT_ROOT / "tradingagents/finite_disclosure_runtime.py"
    source.write_bytes(source.read_bytes() + b"\n# tampered synthetic installation\n")
    with pytest.raises(ValueError, match="product_source_mismatch"):
        cache.installed_profile()


@pytest.mark.parametrize(
    "failure",
    [None, "expires_during_publication", "expires_during_pending_sync", "activation_race"],
)
def test_publication_tail_is_fresh_and_no_clobber(installation, tmp_path, monkeypatch, failure):
    import shutil

    _, view, profile = installation
    module = script("publish_finite_disclosure")
    target = tmp_path / "tail-fixture"
    for rel in (
        *cache.PRODUCT_SOURCES,
        "tradingagents/market_history/current_cache_compatibility.json",
    ):
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cache.PROJECT_ROOT / rel, dest)
    proof = tmp_path / "synthetic-review.json"
    proof.write_text("Synthetic tail-path test. This is not a real release qualification.")
    sources = {rel: a.digest((target / rel).read_bytes()) for rel in cache.PRODUCT_SOURCES}
    exporter_module = script("run_finite_disclosure_offline")
    exporter = exporter_module.QualificationExport()
    exporter.capture(
        exporter_module.QUALIFICATION_NODE,
        "call",
        "passed",
        Path(os.environ["FINITE_TEST_CURRENT"]),
    )
    bundle_dir = tmp_path / "synthetic-tail-bundle"
    bundle_dir.mkdir()
    qualification = {
        "contract": "finite-release-qualification-v2",
        "passed": True,
        "sources": sources,
        **exporter.retain(bundle_dir, suite_passed=True),
    }
    for name in ("primary_tests", "independent_tests", "independent_review", "preservation"):
        qualification[name] = {
            "passed": True,
            "path": str(proof),
            "sha256": a.digest(proof.read_bytes()),
        }
    # Only qualification adapter is replaced here; exercised production binding
    # and final no-clobber/clock behavior are real. CURRENT has a separate real matrix.
    from tradingagents.dataflows import current_cache_qualification

    monkeypatch.setattr(current_cache_qualification, "compatibility_diagnostic", lambda **kwargs: None)
    fresh = a.utc("2026-10-22T04:00:00Z")
    expired = a.utc("2026-10-22T17:56:25.556279Z")
    clock = iter([fresh, expired if failure == "expires_during_publication" else fresh])
    if failure == "expires_during_pending_sync":
        sync_count = [0]
        original_sync = module.os.fsync

        def sync(fd):
            result = original_sync(fd)
            sync_count[0] += 1
            return result

        monkeypatch.setattr(module.os, "fsync", sync)
        # Five publication files precede the pending activation file. Its sync
        # crosses the original expiry; no activation pointer may be published.
        clock = None
    original_link = module.os.link
    if failure == "activation_race":

        def race(source, destination):
            Path(destination).write_bytes(b"existing-concurrent-pointer")
            return original_link(source, destination)

        monkeypatch.setattr(module.os, "link", race)
    kwargs = {
        "_clock": (lambda: expired if sync_count[0] >= 6 else fresh)
        if failure == "expires_during_pending_sync"
        else lambda: next(clock)
    }
    if failure:
        with pytest.raises((ValueError, FileExistsError)):
            module.publish(
                target,
                view,
                profile["admission_config"],
                Path(os.environ["FINITE_TEST_CURRENT"]).read_bytes(),
                qualification,
                **kwargs,
            )
        if failure == "activation_race":
            assert (target / cache.ACTIVE_PATH).read_bytes() == b"existing-concurrent-pointer"
        else:
            assert not (target / cache.ACTIVE_PATH).exists()
    else:
        result = module.publish(
            target,
            view,
            profile["admission_config"],
            Path(os.environ["FINITE_TEST_CURRENT"]).read_bytes(),
            qualification,
            **kwargs,
        )
        assert result["new_physical_requests"] == 0
        assert (target / cache.ACTIVE_PATH).exists()
        copied = a.decode(Path(result["cache"]).read_bytes())
        assert copied["sources"] == view["sources"]
        assert result["earliest_expiry"] == "2026-10-22T17:56:25.556279+00:00"


def synthetic_existing_authority(tmp_path):
    from dataclasses import replace

    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import (
        ProviderRequestCoordinator,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    base = MarketHistoryConfig.from_mapping(
        {
            "market_history_mode": "current",
            "data_usage_mode": "personal_research",
            "market_history_database_path": str(tmp_path / "primary.sqlite3"),
            "market_history_payload_root": str(tmp_path / "primary-payloads"),
            "market_history_backup_root": str(tmp_path / "primary-backups"),
        }
    )
    with MarketHistoryStore.open_provider_request_authority(base) as authority:
        coordinator = ProviderRequestCoordinator(authority)
        identity, name = upstream_service_identity_for_provider("tushare")
        coordinator.register_upstream_service(identity, name)
        coordinator.configure_operator_ceiling(
            upstream_service_id=identity,
            minimum_interval=timedelta(seconds=1.5),
            allow_prewarming=False,
            configured_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
        authority._connection.execute(
            "INSERT INTO request_cooldowns VALUES (?, ?, ?, ?, ?, ?)",
            (
                identity,
                "synthetic-preserved",
                "2026-10-23T00:00:00Z",
                "test",
                60,
                "2026-10-21T00:00:00Z",
            ),
        )
        path = authority.config.database_path
        owner = authority.config.payload_root / ".market-history-database-owner"
    # Explicit synthetic path substitution at the dataclass seam. Production
    # from_mapping still requires the exact fixed shared authority path.
    return replace(base, coordinator_path=path), path, owner


def test_finite_existing_authority_retains_policy_and_rows(tmp_path, monkeypatch):
    from tradingagents.market_history.store import MarketHistoryStore

    settings, path, owner = synthetic_existing_authority(tmp_path)
    before, owner_before = path.read_bytes(), owner.read_bytes()

    def denied(*args):
        raise AssertionError("finite opener must not migrate, claim or import")

    monkeypatch.setattr(MarketHistoryStore, "_apply_migrations", denied)
    monkeypatch.setattr(MarketHistoryStore, "_claim_payload_root", denied)
    monkeypatch.setattr(MarketHistoryStore, "_import_legacy_provider_request_state", denied)
    with MarketHistoryStore.open_provider_request_authority(settings) as authority:
        assert authority.config.database_path == path
        assert authority._connection.execute(
            "SELECT cooldown_scope FROM request_cooldowns"
        ).fetchall() == [("synthetic-preserved",)]
        assert authority._connection.execute(
            "SELECT account_scope FROM upstream_services"
        ).fetchall() == [("",)]
    assert path.read_bytes() == before and owner.read_bytes() == owner_before
    assert not settings.database_path.exists()


@pytest.mark.parametrize(
    "defect",
    [
        "missing_database",
        "missing_owner",
        "wrong_owner",
        "empty_identity",
        "wrong_scope",
        "weak_policy",
        "schema",
        "missing_table",
    ],
)
def test_finite_existing_authority_refuses_repair_or_empty_quota(tmp_path, defect):
    import sqlite3

    from tradingagents.market_history.store import (
        MarketHistoryStore,
        ProviderRequestAuthorityUnavailableError,
    )

    settings, path, owner = synthetic_existing_authority(tmp_path)
    if defect == "missing_database":
        path.unlink()
    elif defect == "missing_owner":
        owner.unlink()
    elif defect == "wrong_owner":
        owner.write_bytes(b"different-owner\n")
    else:
        with sqlite3.connect(path) as connection:
            if defect == "empty_identity":
                connection.execute("DELETE FROM upstream_services")
            elif defect == "wrong_scope":
                connection.execute("UPDATE upstream_services SET account_scope='personal-2000'")
            elif defect == "weak_policy":
                connection.execute("UPDATE upstream_services SET operator_ceiling_json='{}'")
            elif defect == "schema":
                connection.execute("DELETE FROM schema_migrations WHERE version=9")
            else:
                connection.execute("DROP TABLE provider_request_attempts")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(
        ProviderRequestAuthorityUnavailableError,
        match="finite_existing_provider_request_authority_unavailable",
    ):
        MarketHistoryStore.open_provider_request_authority(settings)
    assert (path.read_bytes() if path.exists() else None) == before


def test_finite_authority_disappearing_before_connect_is_not_recreated(tmp_path, monkeypatch):
    from tradingagents.market_history import store as module

    settings, path, _ = synthetic_existing_authority(tmp_path)
    connect = module.sqlite3.connect

    def disappear(name, *args, **kwargs):
        assert name == path.as_uri() + "?mode=rw" and kwargs.get("uri") is True
        path.unlink()
        return connect(name, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", disappear)
    with pytest.raises(module.ProviderRequestAuthorityUnavailableError):
        module.MarketHistoryStore.open_provider_request_authority(settings)
    assert not path.exists()


def test_default_authority_creation_remains_available(tmp_path):
    settings, path, _ = synthetic_existing_authority(tmp_path)
    assert path.is_file() and settings.coordinator_path == path


def test_finite_authority_checks_literal_alias_before_canonicalization(tmp_path, monkeypatch):
    from tradingagents.market_history import store as module

    settings, path, _ = synthetic_existing_authority(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path or original(self))

    def must_not_resolve(config):
        raise AssertionError("alias must be rejected before canonicalization")

    monkeypatch.setattr(module, "provider_request_authority_config", must_not_resolve)
    with pytest.raises(module.ProviderRequestAuthorityUnavailableError):
        module.MarketHistoryStore.open_provider_request_authority(settings)


def test_finite_authority_rejects_partial_policy_contract(tmp_path):
    import sqlite3

    from tradingagents.market_history.store import (
        MarketHistoryStore,
        ProviderRequestAuthorityUnavailableError,
    )

    settings, path, _ = synthetic_existing_authority(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE upstream_services SET operator_ceiling_json=?",
            (json.dumps({"minimum_interval_seconds": 1.5, "allow_prewarming": False}),),
        )
    with pytest.raises(ProviderRequestAuthorityUnavailableError):
        MarketHistoryStore.open_provider_request_authority(settings)


# Entirely synthetic release-chain fixtures. They cannot qualify the actual tree.
def lifecycle_setup(root, monkeypatch, *, create):
    from tradingagents.market_history import current_cache

    product = root / "synthetic-product"
    bundle = root / "synthetic-bundle"
    monkeypatch.setattr(current_cache, "SOURCE_ROOT", product)
    monkeypatch.setattr(cache, "PRODUCT_SOURCES", frozenset({"synthetic-only.py"}))
    monkeypatch.setattr(a, "namespace", lambda: "synthetic-test-not-a-real-qualification")
    monkeypatch.setattr(
        a,
        "validate_view_binding",
        lambda view, cfg: (
            None if view == {"fixture": True} else (_ for _ in ()).throw(ValueError("fixture"))
        ),
    )
    monkeypatch.setattr(
        a,
        "assess",
        lambda view, now: {
            "financial_runtime_fresh": now < a.utc("2026-10-22T17:56:25.556279Z"),
            "earliest_expiry": "2026-10-22T17:56:25.556279+00:00",
        },
    )
    old = a.encode({"sources": {"synthetic-only.py": "0" * 64}})
    monkeypatch.setitem(a.FIXED_CONFIG, "CURRENT_manifest_sha256", a.digest(old))
    if create:
        product.mkdir()
        bundle.mkdir()
        for rel in (
            *current_cache.SOURCE_FILES,
            *current_cache.VERIFICATION_FILES,
            "synthetic-only.py",
        ):
            target = product / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"# SYNTHETIC TEST ONLY; NEVER REAL QUALIFICATION\n")
        old_path = product / "tradingagents/market_history/current_cache_compatibility.json"
        old_path.write_bytes(old)
        details = {
            name: {
                "live": "test-value",
                "restored": "test-value",
                "lineage": "test-lineage",
                "rows": 25,
            }
            for name in current_cache.SCENARIOS
        }
        matrix = {
            name: {"passed": True, "evidence_sha256": current_cache.digest(detail)}
            for name, detail in details.items()
        }
        sources = current_cache.source_fingerprints()
        report = {
            "matrix": matrix,
            "details": details,
            "verification_sources": {
                name: sources[name] for name in current_cache.VERIFICATION_FILES
            },
            "scope": "isolated-synthetic-current-cache-only",
            "mechanism_phase": "direct-codec-before-manifest",
        }
        evidence = current_cache.canonical(report).encode()
        manifest = {
            "contract": current_cache.MANIFEST_CONTRACT,
            "mechanism": current_cache.CONTRACT,
            "sources": sources,
            "matrix": matrix,
            "details": details,
            "matrix_sha256": current_cache.digest(matrix),
            "evidence": {
                "path": "current-cache-matrix-evidence.json",
                "sha256": a.digest(evidence),
                "scope": "isolated-synthetic-current-cache-only",
            },
        }
        scratch = root / "producer-scratch"
        scratch.mkdir()
        origin = scratch / "current-cache-compatibility-manifest.json"
        origin.write_bytes(current_cache.canonical(manifest).encode())
        (scratch / "current-cache-matrix-evidence.json").write_bytes(evidence)
        runner = script("run_finite_disclosure_offline")
        exporter = runner.QualificationExport()
        exporter.capture(runner.QUALIFICATION_NODE, "call", "passed", origin)
        binding = exporter.retain(bundle, suite_passed=True)
        proof = root / "synthetic-proof.json"
        proof.write_text('{"synthetic":true,"not_release_authority":true}')
        qualification = {
            "contract": "finite-release-qualification-v2",
            "passed": True,
            "sources": {
                "synthetic-only.py": a.digest((product / "synthetic-only.py").read_bytes())
            },
            **binding,
        }
        for name in ("primary_tests", "independent_tests", "independent_review", "preservation"):
            qualification[name] = {
                "passed": True,
                "path": str(proof),
                "sha256": a.digest(proof.read_bytes()),
            }
        (root / "synthetic-qualification.json").write_bytes(a.encode(qualification))
    qualification = a.decode((root / "synthetic-qualification.json").read_bytes())
    return (
        product,
        {"fixture": True},
        {"view_sha256": a.digest(a.encode({"fixture": True}))},
        qualification,
    )


def test_lifecycle_explicit_positive_export_only(tmp_path):
    runner = script("run_finite_disclosure_offline")
    exporter = runner.QualificationExport()
    exporter.capture(
        "tests/test_market_history_persistent_reuse.py::test_current_gate_or_disabled_mode_uses_independent_live_acquisition[source_mismatch]",
        "call",
        "passed",
        tmp_path / "must-not-read.json",
    )
    exporter.capture(runner.QUALIFICATION_NODE, "setup", "passed", None)
    with pytest.raises(ValueError, match="complete_suite_required"):
        exporter.retain(tmp_path, suite_passed=True)
    with pytest.raises(ValueError, match="not_passed"):
        exporter.capture(runner.QUALIFICATION_NODE, "call", "failed", None)
    assert list(tmp_path.iterdir()) == []


def test_lifecycle_bundle_immutable_and_not_glob_selected(tmp_path, monkeypatch):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    publisher = script("publish_finite_disclosure")
    runner = script("run_finite_disclosure_offline")
    origin = tmp_path / "producer-scratch/current-cache-compatibility-manifest.json"
    exporter = runner.QualificationExport()
    exporter.capture(runner.QUALIFICATION_NODE, "call", "passed", origin)
    with pytest.raises(ValueError, match="duplicate_export"):
        exporter.capture(runner.QUALIFICATION_NODE, "call", "passed", origin)
    with pytest.raises(ValueError, match="output_exists"):
        exporter.retain(tmp_path / "synthetic-bundle", suite_passed=True)
    with pytest.raises(ValueError, match="complete_suite_required"):
        exporter.retain(tmp_path, suite_passed=False)
    (tmp_path / "aaa-current-cache-compatibility-manifest.json").write_bytes(b"negative decoy")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = publisher.preflight(product, view, cfg, q, now=a.utc("2026-10-22T04:00:00Z"))
    assert result["ready_for_local_publication"] and result["published"] is False
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not (product / cache.ACTIVE_PATH).exists()


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "evidence",
        "export",
        "missing_evidence",
        "old_unbound",
        "argument",
        "partial_proof",
        "stale_source",
        "pending",
        "expired",
    ],
)
def test_lifecycle_preflight_failures_write_nothing(tmp_path, monkeypatch, change):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    publisher = script("publish_finite_disclosure")
    now = a.utc("2026-10-22T04:00:00Z")
    if change in {"manifest", "evidence", "export"}:
        Path(q["current_artifact"][change]["path"]).write_bytes(b"tampered")
    elif change == "missing_evidence":
        Path(q["current_artifact"]["evidence"]["path"]).unlink()
    elif change == "old_unbound":
        q.pop("current_artifact")
    elif change == "partial_proof":
        q["independent_tests"]["sha256"] = "0" * 64
    elif change == "stale_source":
        (product / "synthetic-only.py").write_bytes(b"changed")
    elif change == "pending":
        pending = (product / cache.ACTIVE_PATH).with_suffix(".pending")
        pending.parent.mkdir(parents=True)
        pending.write_bytes(b"existing")
    elif change == "expired":
        now = a.utc("2026-10-22T17:56:25.556279Z")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises((ValueError, FileNotFoundError)):
        if change == "argument":
            publisher.publish(product, view, cfg, b"wrong", q, now=now)
        else:
            publisher.preflight(product, view, cfg, q, now=now)
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not (product / cache.ACTIVE_PATH).exists()


def test_lifecycle_denied_dependency_has_no_retry_or_write(tmp_path, monkeypatch):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    publisher = script("publish_finite_disclosure")
    denied = Path(q["current_artifact"]["manifest"]["path"])
    real_open = Path.open
    attempts = []

    def selective_open(path, *args, **kwargs):
        if path == denied:
            attempts.append(path)
            raise PermissionError(13, "synthetic denial")
        return real_open(path, *args, **kwargs)

    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", selective_open)
        with pytest.raises(PermissionError):
            publisher.preflight(product, view, cfg, q, now=a.utc("2026-10-22T04:00:00Z"))
    assert attempts == [denied]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_lifecycle_actual_publisher_uses_the_preflight_plan(tmp_path, monkeypatch):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    publisher = script("publish_finite_disclosure")
    now = a.utc("2026-10-22T04:00:00Z")
    result = publisher.preflight(product, view, cfg, q, now=now)
    published = publisher.publish(product, view, cfg, None, q, now=now, _clock=lambda: now)
    assert published["release_sha256"] == result["release_sha256"]
    assert published["new_physical_requests"] == 0
    assert a.decode(Path(published["cache"]).read_bytes()) == {"fixture": True}


@pytest.mark.parametrize(
    "change", ["wrong_node", "missing_suite", "swapped_evidence", "moved_reference"]
)
def test_lifecycle_rehashed_descriptor_is_not_self_authorizing(tmp_path, monkeypatch, change):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    publisher = script("publish_finite_disclosure")
    descriptor_path = Path(q["current_artifact"]["export"]["path"])
    descriptor = a.decode(descriptor_path.read_bytes())
    if change == "wrong_node":
        descriptor["nodeid"] = "negative-fixture"
    elif change == "missing_suite":
        descriptor["suite_passed"] = False
    elif change == "swapped_evidence":
        descriptor["evidence"]["sha256"] = "0" * 64
    else:
        q["current_path"] = str(tmp_path / "other.json")
    descriptor_path.write_bytes(a.encode(descriptor))
    q["current_artifact"]["export"]["sha256"] = a.digest(descriptor_path.read_bytes())
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        publisher.preflight(product, view, cfg, q, now=a.utc("2026-10-22T04:00:00Z"))
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_lifecycle_export_rejects_alias_before_creation(tmp_path, monkeypatch):
    lifecycle_setup(tmp_path, monkeypatch, create=True)
    runner = script("run_finite_disclosure_offline")
    exporter = runner.QualificationExport()
    exporter.capture(
        runner.QUALIFICATION_NODE,
        "call",
        "passed",
        tmp_path / "producer-scratch/current-cache-compatibility-manifest.json",
    )
    output = tmp_path / "new-output"
    output.mkdir()
    alias = output / runner.MANIFEST_NAME
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == alias or original(path))
    with pytest.raises(ValueError, match="output_exists"):
        exporter.retain(output, suite_passed=True)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("entry", ["preflight", "publish"])
def test_lifecycle_ancestor_file_collision_stops_before_any_write(tmp_path, monkeypatch, entry):
    product, view, cfg, q = lifecycle_setup(tmp_path, monkeypatch, create=True)
    (product / "configs").write_bytes(b"existing ordinary file")
    publisher = script("publish_finite_disclosure")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="parent_not_directory"):
        if entry == "preflight":
            publisher.preflight(product, view, cfg, q, now=a.utc("2026-10-22T04:00:00Z"))
        else:
            publisher.publish(product, view, cfg, None, q, now=a.utc("2026-10-22T04:00:00Z"))
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not (product / "releases").exists()
