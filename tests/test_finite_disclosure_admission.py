"""Pinned retained evidence in temporary installations; never provider fixtures as admission."""

import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from tradingagents.dataflows import (
    finite_disclosure_admission as a,
    finite_disclosure_cache as cache,
)

NOW = a.utc("2026-10-22T04:00:00+00:00")


@pytest.fixture
def retained():
    path = os.environ.get("FINITE_TEST_VIEW")
    if not path:
        pytest.skip("requires explicitly supplied pinned retained evidence; never obtains data")
    view = a.decode(Path(path).read_bytes())
    config = a.decode(Path(os.environ["FINITE_TEST_CONFIG"]).read_bytes())
    a.validate_view_binding(view, config)
    return view, config


@pytest.fixture
def installation(tmp_path, monkeypatch, retained):
    view, admission_config = retained
    stage, source = Path(os.environ["FINITE_TEST_STAGE"]), Path(os.environ["FINITE_TEST_ROOT"])
    current_rel = "tradingagents/market_history/current_cache_compatibility.json"
    current = a.decode((source / current_rel).read_bytes())
    for rel in (*cache.PRODUCT_SOURCES, *current["sources"], current_rel):
        src = stage / rel if (stage / rel).exists() else source / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
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
    }
    # Isolated binding fixture uses the actually executed CURRENT matrix, not a production release.
    from tradingagents.market_history import current_cache

    current_v2_raw = Path(os.environ["FINITE_TEST_CURRENT"]).read_bytes()
    current_v2 = a.decode(current_v2_raw)
    assert all(x["passed"] for x in current_v2["matrix"].values())
    current_dest = (
        tmp_path / "tradingagents/market_history/current_cache_compatibility_finite_v2.json"
    )
    current_dest.write_bytes(current_v2_raw)
    release = {
        "contract": "finite-runtime-release-v2",
        "sources": {rel: a.digest((tmp_path / rel).read_bytes()) for rel in cache.PRODUCT_SOURCES},
        "CURRENT_sha256": a.digest(current_v2_raw),
    }
    release_raw = a.encode(release)
    profile["release_sha256"] = a.digest(release_raw)
    dest = tmp_path / "releases/finite-601328-v2" / profile["release_sha256"] / "manifest.json"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(release_raw)
    monkeypatch.setattr(current_cache, "SOURCE_ROOT", tmp_path)
    compat = {
        "contract": "finite-disclosure-compatibility-v2",
        "profile_sha256": a.digest(a.encode(profile)),
        "view_sha256": admission_config["view_sha256"],
        "scope_sha256": a.digest(a.encode(a.SCOPE)),
        "original_CURRENT_sha256": a.digest((source / current_rel).read_bytes()),
        "original_CURRENT_sources": current["sources"],
        "product_sources": {
            rel: a.digest((tmp_path / rel).read_bytes()) for rel in cache.PRODUCT_SOURCES
        },
    }
    active = {
        "contract": "finite-disclosure-activation-v1",
        "enabled": True,
        "profile_sha256": a.digest(a.encode(profile)),
        "compatibility_sha256": a.digest(a.encode(compat)),
    }
    for rel, value in (
        (cache.PROFILE_PATH, profile),
        (cache.COMPAT_PATH, compat),
        (cache.ACTIVE_PATH, active),
        (profile["cache_relative_path"], view),
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(a.encode(value))
    monkeypatch.setattr(cache, "PROJECT_ROOT", tmp_path)
    config = cache.select_profile({}, cache.PROFILE)
    return config, view, profile


def test_pinned_six_groups_and_original_members(retained):
    view, _ = retained
    result = a.assess(view, NOW)
    assert result["financial_runtime_fresh"]
    assert [p["present_core"] for p in result["periods"]] == [10] * 6
    assert sum(len(s["retained_records"]) for s in view["sources"].values()) == 9
    assert result["formal_admission"] is False
    assert result["earliest_expiry"] == "2026-10-22T17:56:25.556279+00:00"


def test_installed_profile_fresh_read_and_expiry(installation):
    config, _, _ = installation
    assert cache.load_view(config, "601328.SH", "2026-10-16", now=NOW)[1]["financial_runtime_fresh"]
    expiry = a.utc(a.OBSERVED["balancesheet"]) + timedelta(seconds=86400)
    assert cache.load_view(
        config, "601328.SS", "2026-10-16", now=expiry - timedelta(microseconds=1)
    )[2]
    with pytest.raises(a.Rejected):
        cache.load_view(config, "601328.SH", "2026-10-16", now=expiry)


@pytest.mark.parametrize(
    "field,value", [("symbol", "600000.SH"), ("account", "other"), ("ann_cutoff", "20261017")]
)
def test_cross_scope_rejected(retained, field, value):
    view, _ = retained
    view["scope"][field] = value
    assert not a.assess(view, NOW)["candidate_scope_usable"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_period",
        "critical",
        "threshold",
        "future_ann",
        "conflict",
        "ttl",
        "metadata",
        "fixture",
    ],
)
def test_negative_evidence(retained, failure):
    view, _ = retained
    if failure == "missing_period":
        view["groups"].pop()
    elif failure in ("critical", "threshold"):
        fields = (
            {"total_assets"} if failure == "critical" else {"cash_reser_cb", "depos_in_oth_bfi"}
        )
        for row in view["rows"]:
            if (
                row["table"] == "balancesheet"
                and row["period"] == "20260630"
                and row["field"] in fields
            ):
                row["missing"] = True
    elif failure == "future_ann":
        view["sources"]["income"]["retained_records"][0]["ann_date"] = "20261017"
    elif failure == "conflict":
        view["sources"]["balancesheet"]["retained_records"][0]["values"]["total_assets"][
            "raw_numeric"
        ] = "1"
    elif failure == "ttl":
        view["sources"]["income"]["time"]["ttl_seconds"] = 86401
    elif failure == "metadata":
        view["sources"]["income"]["original_metadata"]["data"]["paging"]["has_more"]["state"] = (
            "true"
        )
    else:
        view["fixture"] = True
    assert not a.assess(view, NOW)["financial_runtime_fresh"]


def test_external_pin_rejects_unit_version_or_derived_change(retained):
    view, config = retained
    for key in ("raw_unit", "precision", "semantic", "pdf_page", "field_meaning"):
        changed = deepcopy(view)
        changed["rows"][0][key] = "untrusted"
        with pytest.raises(a.Rejected):
            a.validate_view_binding(changed, config)


def test_original_current_and_product_hashes_required(installation):
    config, _, _ = installation
    target = cache.PROJECT_ROOT / "tradingagents/default_config.py"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(a.Rejected, match="finite_product_source_mismatch"):
        cache.validate_runtime(config)


def test_missing_cache_does_not_fallback(installation):
    config, _, profile = installation
    (cache.PROJECT_ROOT / profile["cache_relative_path"]).unlink()
    with pytest.raises(a.Rejected):
        cache.load_view(config, "601328.SH", "2026-10-16", now=NOW)


@pytest.mark.parametrize(
    "key,value",
    [
        ("financial_cache_only", False),
        ("analysis_max_calls", 31),
        ("checkpoint_enabled", True),
        ("tushare_account_scope_label", "other"),
    ],
)
def test_runtime_guards(installation, key, value):
    config, _, _ = installation
    config[key] = value
    with pytest.raises(a.Rejected):
        cache.validate_runtime(config)


def test_four_analysts_retained(installation):
    config, _, _ = installation
    cache.validate_runtime(config, selected_analysts=("market", "social", "news", "fundamentals"))
    with pytest.raises(a.Rejected):
        cache.validate_runtime(config, selected_analysts=("fundamentals",))
