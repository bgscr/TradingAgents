"""Local finite release publication; never obtains data or starts analysis."""

import os
from datetime import datetime, timezone
from pathlib import Path


def read_bound_current(qualification):
    """Read one explicitly authorized bundle; never discover, copy or repair artifacts."""
    from tradingagents.dataflows import finite_disclosure_admission as a

    bundle = qualification.get("current_artifact")
    if not isinstance(bundle, dict) or set(bundle) != {"manifest", "evidence", "export"}:
        raise a.Rejected("publication_current_bundle_missing")
    names = {
        "manifest": "current_cache_compatibility.json",
        "evidence": "current-cache-matrix-evidence.json",
        "export": "current-qualification-export.json",
    }
    paths = {}
    for key, record in bundle.items():
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise a.Rejected("publication_current_bundle_shape")
        path = Path(record["path"])
        if (
            not path.is_absolute()
            or path.name != names[key]
            or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))
        ):
            raise a.Rejected("publication_current_bundle_path")
        paths[key] = path
    if len({p.parent for p in paths.values()}) != 1 or str(paths["manifest"]) != str(
        Path(qualification["current_path"])
    ):
        raise a.Rejected("publication_current_bundle_identity")
    content = {}
    for key, path in paths.items():
        # PermissionError propagates without a fallback or retry. No writes occur here.
        with path.open("rb") as handle:
            raw = handle.read(a.MAX_BYTES + 1)
        if len(raw) > a.MAX_BYTES or a.digest(raw) != bundle[key]["sha256"]:
            raise a.Rejected("publication_current_bundle_digest")
        content[key] = raw
    descriptor = a.decode(content["export"])
    expected = {
        "contract": "finite-current-export-v1",
        "scope": "isolated-synthetic-current-cache-only",
        "nodeid": "tests/test_market_history_persistent_reuse.py::test_current_public_entry_persistent_tushare_hit",
        "suite_passed": True,
        "manifest": {"name": names["manifest"], "sha256": bundle["manifest"]["sha256"]},
        "evidence": {"name": names["evidence"], "sha256": bundle["evidence"]["sha256"]},
    }
    if not a.equal(descriptor, expected):
        raise a.Rejected("publication_current_export_binding")
    manifest = a.decode(content["manifest"])
    if manifest.get("evidence") != {
        "path": names["evidence"],
        "sha256": bundle["evidence"]["sha256"],
        "scope": "isolated-synthetic-current-cache-only",
    }:
        raise a.Rejected("publication_current_evidence_binding")
    report = {
        "matrix": manifest["matrix"],
        "details": manifest["details"],
        "verification_sources": {
            name: manifest["sources"][name]
            for name in a.decode(content["evidence"])["verification_sources"]
        },
        "scope": "isolated-synthetic-current-cache-only",
        "mechanism_phase": "direct-codec-before-manifest",
    }
    if not a.equal(a.decode(content["evidence"]), report):
        raise a.Rejected("publication_current_evidence_content")
    return content["manifest"]


def _plan(root, view, admission_config, current_raw, qualification, *, now):
    from tradingagents.dataflows import (
        finite_disclosure_admission as a,
        finite_disclosure_cache as cache,
    )
    from tradingagents.dataflows.current_cache_qualification import compatibility_diagnostic

    root = Path(root)
    a.validate_view_binding(view, admission_config)
    assessment = a.assess(view, now)
    if not assessment["financial_runtime_fresh"]:
        raise a.Rejected("publication_original_ttl_expired")
    if (
        qualification.get("contract") != "finite-release-qualification-v2"
        or qualification.get("passed") is not True
    ):
        raise a.Rejected("publication_qualification_missing")
    sources = {rel: a.digest((root / rel).read_bytes()) for rel in sorted(cache.PRODUCT_SOURCES)}
    if qualification.get("sources") != sources:
        raise a.Rejected("publication_review_source_changed")
    for name in ("primary_tests", "independent_tests", "independent_review", "preservation"):
        proof = qualification.get(name)
        if (
            not isinstance(proof, dict)
            or proof.get("passed") is not True
            or a.digest(Path(proof["path"]).read_bytes()) != proof.get("sha256")
        ):
            raise a.Rejected("publication_evidence_not_bound")
    bound_current = read_bound_current(qualification)
    if current_raw is not None and current_raw != bound_current:
        raise a.Rejected("publication_current_argument_mismatch")
    current_raw = bound_current
    current = a.decode(current_raw)
    if len(current.get("matrix", {})) != 12 or not all(
        x.get("passed") is True for x in current["matrix"].values()
    ):
        raise a.Rejected("publication_current_matrix_failed")
    # Validate against the actual independently qualified source tree before writes.
    current_path = Path(qualification["current_path"])
    if current_path.read_bytes() != current_raw or compatibility_diagnostic(
        manifest_path=current_path, source_root=root
    ):
        raise a.Rejected("publication_current_source_failed")
    oldraw = (root / "tradingagents/market_history/current_cache_compatibility.json").read_bytes()
    old = a.decode(oldraw)
    if a.digest(oldraw) != a.FIXED_CONFIG["CURRENT_manifest_sha256"]:
        raise a.Rejected("publication_old_current_changed")
    release = {
        "contract": "finite-runtime-release-v2",
        "sources": sources,
        "CURRENT_sha256": a.digest(current_raw),
        "qualification": qualification,
        "scope": a.SCOPE,
        "original_observed_at": a.OBSERVED,
        "earliest_expiry": assessment["earliest_expiry"],
        "new_physical_requests": 0,
    }
    release_raw = a.encode(release)
    release_sha = a.digest(release_raw)
    profile = {
        "contract": "finite-disclosure-profile-v2",
        "profile": cache.PROFILE,
        "scope": a.SCOPE,
        "namespace": a.namespace(),
        "cache_relative_path": "data/finite-financial-disclosures/"
        + a.namespace()
        + "/finite-view.json",
        "admission_config": admission_config,
        "runtime_overrides": cache.OVERRIDES,
        "limits": a.LIMITS,
        "user_authorization": "historical-reference-withheld",
        "release_sha256": release_sha,
    }
    profile_raw = a.encode(profile)
    compat = {
        "contract": "finite-disclosure-compatibility-v2",
        "profile_sha256": a.digest(profile_raw),
        "view_sha256": admission_config["view_sha256"],
        "scope_sha256": a.digest(a.encode(a.SCOPE)),
        "original_CURRENT_sha256": a.digest(oldraw),
        "original_CURRENT_sources": old["sources"],
    }
    compat_raw = a.encode(compat)
    active = {
        "contract": "finite-disclosure-activation-v1",
        "enabled": True,
        "profile_sha256": a.digest(profile_raw),
        "compatibility_sha256": a.digest(compat_raw),
    }
    items = {
        f"releases/finite-601328-v2/{release_sha}/manifest.json": release_raw,
        "tradingagents/market_history/current_cache_compatibility_finite_v2.json": current_raw,
        cache.PROFILE_PATH: profile_raw,
        cache.COMPAT_PATH: compat_raw,
        profile["cache_relative_path"]: a.encode(view),
    }
    active_path = root / cache.ACTIVE_PATH
    for relative in (
        *items,
        cache.ACTIVE_PATH,
        str(Path(cache.ACTIVE_PATH).with_suffix(".pending")),
    ):
        path = root / relative
        if path.exists() or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise a.Rejected("publication_target_exists_or_alias")
        if any(parent.exists() and not parent.is_dir() for parent in path.parents):
            raise a.Rejected("publication_parent_not_directory")
    return items, active_path, active, assessment, release_sha


def preflight(root, view, admission_config, qualification, *, now=None):
    """Same product validation as publish, but no mkdir/open-for-write/activation."""
    items, active_path, _, assessment, release_sha = _plan(
        root,
        view,
        admission_config,
        None,
        qualification,
        now=now or datetime.now(timezone.utc),
    )
    return {
        "contract": "finite-publication-readonly-preflight-v1",
        "ready_for_local_publication": True,
        "published": False,
        "release_sha256": release_sha,
        "active": str(active_path),
        "planned_paths": list(items),
        "earliest_expiry": assessment["earliest_expiry"],
        "new_physical_requests": 0,
        "analysis_started": False,
    }


def publish(root, view, admission_config, current_raw, qualification, *, now=None, _clock=None):
    from tradingagents.dataflows import finite_disclosure_admission as a

    from tradingagents.dataflows.finite_disclosure_cache import require_current_release_authority
    require_current_release_authority()
    root = Path(root)
    clock = _clock or (lambda: datetime.now(timezone.utc))
    items, active_path, active, assessment, release_sha = _plan(
        root, view, admission_config, current_raw, qualification, now=now or clock()
    )
    # The activation pointer is last. A stopped write leaves retained, inactive evidence.
    for relative, raw in items.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    temp = active_path.with_suffix(".pending")
    with temp.open("xb") as handle:
        handle.write(a.encode(active))
        handle.flush()
        os.fsync(handle.fileno())
    if active_path.exists():
        raise a.Rejected("publication_activation_race")
    if not a.assess(view, clock())["financial_runtime_fresh"]:
        raise a.Rejected("publication_expired_before_activation")
    # Atomic no-clobber publication on the same filesystem.
    os.link(temp, active_path)
    temp.unlink()
    return {
        "release_sha256": release_sha,
        "active": str(active_path),
        "cache": str(root / next(name for name in items if name.endswith("/finite-view.json"))),
        "earliest_expiry": assessment["earliest_expiry"],
        "new_physical_requests": 0,
        "analysis_started": False,
    }
