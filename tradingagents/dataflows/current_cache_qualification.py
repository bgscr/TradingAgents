"""Import-light CURRENT manifest validation; no runtime/provider initialization.

All historical 38 CURRENT entries are retained; this new runtime additionally
binds the extracted validator, for 39 entries. Old saved manifests are untouched.
"""
import json
from hashlib import sha256
from pathlib import Path

CONTRACT = "mainland-current-cache-v1"
SSE_CALENDAR_PROOF = "tushare-sse-current-calendar-v1"
SSE_CALENDAR_SYMBOL = "601328.SS"
MANIFEST_CONTRACT = "current-cache-compatibility-manifest-v1"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "market_history/current_cache_compatibility.json"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "tradingagents/dataflows/current_cache_qualification.py",
    "tradingagents/market_history/current_cache.py",
    "tradingagents/market_history/current_incremental.py",
    "tradingagents/market_history/config.py",
    "tradingagents/market_history/store.py",
    "tradingagents/market_history/shadow.py",
    "tradingagents/market_history/current.py",
    "tradingagents/market_history/calendar_discovery.py",
    "tradingagents/market_history/calendar.py",
    "tradingagents/market_history/frames.py",
    "tradingagents/market_history/models.py",
    "tradingagents/market_history/revisions.py",
    "tradingagents/market_history/schema.py",
    "tradingagents/market_history/snapshot_identity.py",
    "tradingagents/market_history/maintenance.py",
    "tradingagents/market_history/bundle.py",
    "tradingagents/market_history/coordinator.py",
    "tradingagents/dataflows/market_snapshot.py",
    "tradingagents/dataflows/acquisition.py",
    "tradingagents/dataflows/stockstats_utils.py",
    "tradingagents/dataflows/symbol_utils.py",
    "tradingagents/dataflows/baostock_data.py",
    "tradingagents/dataflows/baostock_calendar.py",
    "tradingagents/dataflows/tushare_market_history.py",
    "tradingagents/picker/tushare_provider.py",
    "tradingagents/tushare_https.py",
    "tradingagents/dataflows/akshare_data.py",
    "tradingagents/dataflows/y_finance.py",
    "tradingagents/strategy_registry.py",
    "tradingagents/decision_policy.py",
    "tradingagents/agents/schemas.py",
    "tradingagents/evidence.py",
)
VERIFICATION_FILES = (
    "tests/test_tushare_current_incremental.py",
    "tests/test_tushare_current_acceptance.py",
    "scripts/accept_tushare_current.py",
    "tradingagents/tushare_acceptance.py",
    "tests/test_market_history_persistent_reuse.py",
    "tests/test_tushare_pit_provider.py",
    "scripts/run_current_cache_compatibility.py",
)
SCENARIOS = (
    "shanghai_primary", "shenzhen_primary", "baostock_fallback", "yahoo_fallback",
    "confirmed_suspension", "representation_noise", "in_range_gap", "factor_revision",
    "rate_limit_fallback", "store_corruption_degradation", "concurrent_write", "crash_recovery",
)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def source_fingerprints(*, include_verification=True, source_root=SOURCE_ROOT):
    # Only UTF-8 decoding and CRLF-to-LF normalization; no semantic filtering.
    paths = SOURCE_FILES + VERIFICATION_FILES if include_verification else SOURCE_FILES
    return {name: sha256((Path(source_root) / name).read_bytes().decode("utf-8").replace(
        "\r\n", "\n").encode("utf-8")).hexdigest() for name in paths}


def compatibility_diagnostic(*, manifest_path, source_root=SOURCE_ROOT):
    """Fail closed independently of strict-history/qualification DB records."""
    try:
        selected_path = Path(manifest_path)
        manifest = json.loads(selected_path.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_json_object)
        if set(manifest) != {"contract", "mechanism", "sources", "matrix", "matrix_sha256", "evidence", "details"}:
            raise ValueError("shape")
        if manifest["contract"] != MANIFEST_CONTRACT or manifest["mechanism"] != CONTRACT:
            raise ValueError("version")
        sources = manifest["sources"]
        if (set(sources) != set(SOURCE_FILES + VERIFICATION_FILES)
                or {name: sources[name] for name in SOURCE_FILES} != source_fingerprints(include_verification=False, source_root=source_root)
                or any(len(sources[name]) != 64 or any(char not in "0123456789abcdef" for char in sources[name])
                       for name in VERIFICATION_FILES)):
            raise ValueError("source_mismatch")
        matrix = manifest["matrix"]
        if set(matrix) != set(SCENARIOS) or digest(matrix) != manifest["matrix_sha256"]:
            raise ValueError("matrix")
        if set(manifest["details"]) != set(SCENARIOS):
            raise ValueError("details")
        for name, result in matrix.items():
            if (set(result) != {"passed", "evidence_sha256"} or result["passed"] is not True
                    or not isinstance(result["evidence_sha256"], str)
                    or len(result["evidence_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in result["evidence_sha256"])):
                raise ValueError("scenario")
            proof = manifest["details"][name]
            if (digest(proof) != result["evidence_sha256"] or proof["live"] != proof["restored"]
                    or not proof["lineage"] or proof["rows"] < 21):
                raise ValueError("scenario_evidence")
        if (set(manifest["evidence"]) != {"path", "sha256", "scope"}
                or manifest["evidence"]["scope"] != "isolated-synthetic-current-cache-only"
                or len(manifest["evidence"]["sha256"]) != 64):
            raise ValueError("evidence")
        report = {"matrix": matrix, "details": manifest["details"],
                  "verification_sources": {name: sources[name] for name in VERIFICATION_FILES},
                  "scope": "isolated-synthetic-current-cache-only", "mechanism_phase": "direct-codec-before-manifest"}
        if digest(report) != manifest["evidence"]["sha256"]:
            raise ValueError("evidence_digest")
    except Exception as exc:
        return f"current_cache_gate_denied:{type(exc).__name__}:{exc}"
    return None

