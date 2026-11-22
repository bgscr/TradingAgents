"""Finite disclosure validation; external pins are installed independently. No I/O transport."""

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

TTL = 86400
MAX_BYTES = 2 * 1024 * 1024
PERIODS = {
    "balancesheet": ["20260630", "20251231"],
    "income": ["20260630", "20250630"],
    "cashflow": ["20260630", "20250630"],
}
CORE = {
    "balancesheet": [
        "cash_reser_cb",
        "depos",
        "cost_fin_assets",
        "fair_value_fin_assets",
        "depos_in_oth_bfi",
        "depos_oth_bfi",
        "decr_in_disbur",
        "total_assets",
        "total_hldr_eqy_inc_min_int",
        "total_liab",
    ],
    "income": [
        "comm_income",
        "income_tax",
        "int_exp",
        "int_income",
        "invest_income",
        "n_commis_income",
        "n_income_attr_p",
        "oper_exp",
        "operate_profit",
        "total_profit",
    ],
    "cashflow": [
        "n_cash_flows_fnc_act",
        "n_cashflow_inv_act",
        "n_cashflow_act",
        "c_inf_fr_operate_a",
        "c_pay_dist_dpcp_int_exp",
        "c_paid_for_taxes",
        "c_cash_equ_end_period",
        "eff_fx_flu_cash",
        "n_incr_cash_cash_equ",
        "c_cash_equ_beg_period",
    ],
}
CRITICAL = {
    "balancesheet": [
        "depos",
        "decr_in_disbur",
        "total_assets",
        "total_hldr_eqy_inc_min_int",
        "total_liab",
    ],
    "income": ["int_exp", "int_income", "n_income_attr_p", "operate_profit"],
    "cashflow": ["n_cashflow_act", "c_cash_equ_end_period", "n_incr_cash_cash_equ"],
}
NEGATE = {
    ("income", "income_tax"),
    ("income", "int_exp"),
    ("income", "oper_exp"),
    ("cashflow", "c_pay_dist_dpcp_int_exp"),
    ("cashflow", "c_paid_for_taxes"),
}
SPECIFIC_MEANINGS = {
    (
        "balancesheet",
        "decr_in_disbur",
    ): "group_loans_net_of_principal_and_accrued_interest_impairment_in_this_filing",
    ("balancesheet", "depos"): "customer_deposits_including_accrued_interest_in_this_filing",
    ("balancesheet", "fair_value_fin_assets"): "FVOCI_investments_not_all_fair_value_assets",
    ("balancesheet", "cost_fin_assets"): "amortized_cost_financial_investments_in_this_filing",
    (
        "balancesheet",
        "total_hldr_eqy_inc_min_int",
    ): "group_total_equity_including_minority_not_parent_equity",
    ("income", "n_income_attr_p"): "profit_attributable_to_parent_shareholders_not_group_profit",
}
PDF_HASH = "480ad7717978eaeb61eb7477bce2d5042c966e65a5314a2c57a8ac0ce819cde5"
PINS = {'bundle': 'b294877081625e763d018c1de382f9ca8e7ec33e74030e615b8e288811425c31', 'rules': '8054fa540fdf63a76c3d23162d437c76aeaf898c3eb74ba7e5604b28d4f0fd24', 'cash_proof': '65cd0a00883645ccd36b31c0c9c0ab2c2410065076e70fc26bfad79d266e2542', 'balancesheet': '6e492791879f1012121e6e36351b365b9413916280c612fe6960f5a4c52f6367', 'income': 'a2da2de8451a26b203e53e027cfd1dc4850723be2efc4bf0151865867f0c8a30', 'cashflow': 'f43964c4bba8f7e6c92eebf73c9d9b2f60a44a37aeb47d9d53e2c787c665ba39', 'pdf': 'd12317611a235e81a799e9a91157f9171c8b83040dcafd39f627f600ef462e44'}
CONFIRMATION = {
    "user_message_id": "historical-reference-withheld",
    "source_trust": "user_confirmed_bank_official_site_original_file",
    "pdf_sha256": PDF_HASH,
    "independent_official_same_bytes_verified": False,
    "comparison_version": "2026_H1_report_2025_comparatives",
    "reclassification_risk_acknowledged": True,
    "strict_PIT_claim": False,
    "authority": "offline_candidate_only_not_production_admission",
}
SCOPE = {
    "contract": "finite-disclosure-601328-2026H1-v2",
    "symbol": "601328.SH",
    "account": "personal-2000",
    "ann_cutoff": "20261016",
    "periods": PERIODS,
    "report_type": "1",
    "comp_type": "2",
    "currency": "CNY",
    "basis": "group",
    "comparison_version": CONFIRMATION["comparison_version"],
    "pdf_sha256": PDF_HASH,
    "coverage": "six_filing_period_groups_not_announcement_window",
    "endpoint_identity": "https://api.waditu.com/dataapi",
    "mapping_version": "five_field_explicit_negation_filing_bound_v2",
}
LIMITS = [
    "no_latest_report",
    "no_full_history",
    "no_strict_PIT",
    "no_annual_growth",
    "no_single_quarter",
    "no_TTM",
    "no_bank_risk_metrics",
    "no_valuation",
    "no_supplier_unique_revision_claim",
    "no_exact_yuan_precision_claim",
]
OBSERVED = {'balancesheet': '2026-10-23T04:06:47.475742+00:00', 'income': '2026-10-23T04:06:49.100392+00:00', 'cashflow': '2026-10-23T04:06:50.713748+00:00'}
DATES = {"20260630": "20260829", "20251231": "20260328", "20250630": "20250830"}
METADATA_DECISION = {
    "state": "unresolved_extensions_finite_disclosure_corroboration_only",
    "unknown_keys_recovered": False,
    "unknown_extensions_assumed_harmless": False,
    "has_more_false_proves_completeness": False,
    "permitted_basis": "only_pinned_selected_fields_matched_to_user_confirmed_filing",
    "residual_risk": "unretained_extension_semantics_and_unique_supplier_revision_unverified",
}
FIXED_CONFIG = {
    "scope": SCOPE,
    "financial_cache_only": True,
    "ttl_seconds": TTL,
    "force_refresh": False,
    "checkpoint_enabled": False,
    "analysts": ["market", "social", "news", "fundamentals"],
    "physical_request_budget": 30,
    "cooperative_deadline_seconds": 600,
    "quick_model": "deepseek-flash",
    "deep_model": "deepseek-v4-pro",
    "model_provider": "deepseek",
    "depth": "basic",
    "report_limits": LIMITS,
    "purpose": "offline_candidate",
    "baseline_HEAD": "d343d8a7b69caf80223fc591d834305732e7ed3b",
    "CURRENT_manifest_sha256": "0c6a597ba62da8d90170e07ebab947c7d9c9af655584b135cc5a9c4ef6cb06d3",
    "production_release_authorized": False,
}


class Rejected(ValueError):
    """Only fixed internal reason codes, never exception text from dependencies."""


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def equal(a, b):
    return encode(a) == encode(b)


def pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise Rejected("duplicate_json_key")
        out[k] = v
    return out


def decode(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise Rejected("input_size_or_type")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(Rejected("nonfinite_json")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Rejected("invalid_json") from None
    return value


def amount(value):
    if (
        type(value) is not str
        or len(value) > 40
        or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", value)
    ):
        raise Rejected("invalid_amount")
    result = Decimal(value)
    if not -Decimal("1e16") <= result <= Decimal("1e16"):
        raise Rejected("amount_range")
    return result


def utc(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() != timedelta(0):
            raise ValueError
        return stamp
    except (TypeError, ValueError):
        raise Rejected("invalid_UTC_timestamp") from None


def namespace():
    return "finite-disclosure-v2/" + digest(encode(SCOPE)) + "/" + PINS["bundle"]


def assess(view, now):
    """Evaluate finite numerical usability and freshness, never grant analysis authority."""
    result = {
        "candidate_scope_usable": False,
        "financial_runtime_fresh": False,
        "formal_admission": False,
        "full_analysis_ready": False,
        "model_start_allowed": False,
        "analysis_started": False,
        "issues": [],
        "periods": [],
        "expiry": {},
        "metadata_decision": copy.deepcopy(METADATA_DECISION),
        "report_limits": list(LIMITS),
        "fixture": view.get("fixture")
        if isinstance(view, dict) and type(view.get("fixture")) is bool
        else None,
    }
    if type(now) is not datetime or now.utcoffset() != timedelta(0):
        result["issues"] = ["invalid_clock"]
        return result
    try:
        if (
            view["contract"] != "finite-financial-view-candidate-v2"
            or not equal(view["scope"], SCOPE)
            or view["namespace"] != namespace()
            or not equal(view["source_pins"], PINS)
            or not equal(view["source_confirmation"], CONFIRMATION)
            or not equal(view["metadata_decision"], METADATA_DECISION)
            or not equal(view["report_limits"], LIMITS)
            or type(view["minimum_core_fields"]) is not int
            or view["minimum_core_fields"] != 9
            or any(
                view[k] is not False
                for k in (
                    "formal_cache_written",
                    "financial_data_accepted",
                    "production_release_authorized",
                )
            )
        ):
            raise Rejected("scope_or_contract_mismatch")
        expected = {(t, p, f) for t, ps in PERIODS.items() for p in ps for f in CORE[t]}
        if (
            view["fixture"] is not False
            or len(view["groups"]) != 6
            or set(view["sources"]) != set(PERIODS)
        ):
            raise Rejected("source_shape")
        groups = {}
        for group in view["groups"]:
            table, period = group["table"], group["period"]
            if table not in PERIODS or period not in PERIODS[table] or (table, period) in groups:
                raise Rejected("group_scope")
            source = view["sources"][table]
            members = [
                (i, r)
                for i, r in enumerate(source["retained_records"], 1)
                if r["end_date"] == period
            ]
            if (
                not members
                or group["record_indexes_1based"] != [i for i, r in members]
                or group["update_flags"] != [r["update_flag"] for i, r in members]
                or group["source_sha256"] != PINS[table]
                or group["selected_member"] is not None
                or group["latest_claimed"] is not False
                or group["whole_row_equivalence_claimed"] is not False
            ):
                raise Rejected("member_group_mismatch")
            projected = []
            for _, member in members:
                if (
                    member["ts_code"] != "601328.SH"
                    or member["comp_type"] != "2"
                    or member["report_type"] != "1"
                    or member["ann_date"] != DATES[period]
                    or member["f_ann_date"] != DATES[period]
                    or member["update_flag"] not in ("0", "1")
                ):
                    raise Rejected("member_identity_or_date")
                projected.append(
                    {
                        k: v
                        for k, v in member.items()
                        if k not in ("update_flag", "issues", "comparisons")
                    }
                )
            if any(not equal(projected[0], item) for item in projected):
                raise Rejected("member_conflict")
            groups[(table, period)] = members
        rows = {}
        for row in view["rows"]:
            key = (row["table"], row["period"], row["field"])
            if key not in expected or key in rows:
                raise Rejected("duplicate_or_unexpected_field")
            rows[key] = row
            if row["missing"] is not False:
                continue
            members = groups[key[:2]]
            if (
                row["source_sha256"] != PINS[key[0]]
                or row["member_indexes"] != [i for i, r in members]
                or not equal(
                    row["original_member_values"], [r["values"][key[2]] for i, r in members]
                )
                or any(
                    r["values"][key[2]]["state"] != "numeric"
                    or r["values"][key[2]]["raw_numeric"] != row["raw_api_value"]
                    for i, r in members
                )
            ):
                raise Rejected("member_value_mismatch")
            raw = amount(row["raw_api_value"])
            flip = (key[0], key[2]) in NEGATE
            if flip and raw <= 0:
                raise Rejected("unexpected_expense_or_payment_sign")
            derived = -raw if flip else raw
            if amount(row["derived_disclosure_contribution"]) != derived:
                raise Rejected("mapping_mismatch")
            if (
                not Decimal("-0.5")
                < derived / Decimal(1000000) - amount(row["pdf_million"])
                < Decimal("0.5")
            ):
                raise Rejected("display_precision_not_satisfied")
        stale = []
        for table, periods in PERIODS.items():
            source = view["sources"][table]
            if (
                source["sha256"] != PINS[table]
                or source["time"]["original_observed_at"] != OBSERVED[table]
            ):
                raise Rejected("original_source_or_time_changed")
            for level in ("top", "data"):
                for signal in ("has_more", "truncated"):
                    if source["original_metadata"][level]["paging"][signal]["state"] not in (
                        "missing",
                        "false",
                    ):
                        raise Rejected("pagination_requires_review")
            observed = utc(source["time"]["original_observed_at"])
            expires = observed + timedelta(seconds=TTL)
            result["expiry"][table] = expires.isoformat()
            if utc(source["time"]["expires_at"]) != expires or source["time"]["ttl_seconds"] != TTL:
                raise Rejected("TTL_or_expiry_mismatch")
            if observed > now or now >= expires:
                stale.append("stale_or_future:" + table)
            for period in periods:
                available = {
                    f
                    for f in CORE[table]
                    if (table, period, f) in rows and rows[(table, period, f)]["missing"] is False
                }
                missing = [f for f in CORE[table] if f not in available]
                critical = [f for f in CRITICAL[table] if f not in available]
                result["periods"].append(
                    {
                        "table": table,
                        "period": period,
                        "present_core": len(available),
                        "missing_core": missing,
                        "missing_critical": critical,
                    }
                )
                if len(available) < 9 or critical:
                    result["issues"].append("insufficient_fields:" + table + ":" + period)
        result["candidate_scope_usable"] = not result["issues"]
        result["issues"].extend(stale)
        result["financial_runtime_fresh"] = result["candidate_scope_usable"] and not stale
        result["earliest_expiry"] = min(result["expiry"].values())
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        result["issues"] = ["invalid_candidate_view"]
        result["candidate_scope_usable"] = result["financial_runtime_fresh"] = False
        return result


def validate_config(config):
    if (
        set(config)
        != set(FIXED_CONFIG) | {"view_sha256", "namespace", "source_confirmation_sha256", "fixture"}
        or any(not equal(config[k], v) for k, v in FIXED_CONFIG.items())
        or config["namespace"] != namespace()
        or config["source_confirmation_sha256"] != digest(encode(CONFIRMATION))
        or config["fixture"] is not False
        or type(config["view_sha256"]) is not str
        or not re.fullmatch("[0-9a-f]{64}", config["view_sha256"])
    ):
        raise Rejected("configuration_mismatch")


def validate_view_binding(view, config):
    validate_config(config)
    if digest(encode(view)) != config["view_sha256"]:
        raise Rejected("view_pin_mismatch")
    if view.get("fixture") is not config["fixture"]:
        raise Rejected("fixture_scope_mismatch")


def read_candidate(root, config, now, *, approved_root):
    """Read only a caller-bound path with external digest. No discovery or fallback."""
    validate_config(config)
    root, approved_root = Path(root), Path(approved_root)
    for path in (root, approved_root):
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise Rejected("cache_path_invalid")
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise Rejected("cache_path_alias")
    if root.resolve() != approved_root.resolve():
        raise Rejected("cache_root_not_bound")
    path = root / "finite-view.json"
    if path.is_symlink() or path.is_junction():
        raise Rejected("cache_path_alias")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        raise Rejected("cache_missing_or_unreadable") from None
    if digest(raw) != config["view_sha256"]:
        raise Rejected("cache_content_pin_mismatch")
    view = decode(raw)
    validate_view_binding(view, config)
    result = assess(view, now)
    result["fixture"] = view["fixture"]
    if not result["financial_runtime_fresh"]:
        return result, None
    return result, view


def consume(view, config, now, *, symbol, cutoff, table, periods, fields, comparison):
    """Deterministic finite tool payload; no freeform claims or analyst invocation."""
    validate_view_binding(view, config)
    report = assess(view, now)
    if not report["financial_runtime_fresh"]:
        raise Rejected("financial_preflight_stopped")
    if (
        symbol != SCOPE["symbol"]
        or cutoff != SCOPE["ann_cutoff"]
        or table not in PERIODS
        or not equal(periods, PERIODS[table])
        or comparison != ("year_end_comparison" if table == "balancesheet" else "midyear_yoy")
        or type(fields) is not list
        or not fields
        or any(type(f) is not str for f in fields)
        or len(fields) != len(set(fields))
        or any(f not in CORE[table] for f in fields)
    ):
        raise Rejected("consumer_scope_mismatch")
    rows = [r for r in view["rows"] if r["table"] == table and r["field"] in fields]
    if len(rows) != 2 * len(fields) or any(r["missing"] is not False for r in rows):
        raise Rejected("requested_conclusion_fields_missing")
    return {
        "contract": "finite-disclosure-tool-payload-v2",
        "scope": copy.deepcopy(SCOPE),
        "comparison": comparison,
        "rows": copy.deepcopy(rows),
        "groups": [copy.deepcopy(g) for g in view["groups"] if g["table"] == table],
        "expiry": report["expiry"][table],
        "report_limits": list(LIMITS),
        "metadata_decision": copy.deepcopy(METADATA_DECISION),
        "fixture": view["fixture"],
        "formal_admission": False,
        "model_start_allowed": False,
    }


def startup_gate(view, config, now):
    validate_view_binding(view, config)
    result = assess(view, now)
    result["other_required_gates"] = [
        "market_price_window_not_checked",
        "calendar_binding_not_rechecked",
        "all_analyst_routes_required",
        "production_integration_not_authorized",
    ]
    result["full_analysis_ready"] = result["model_start_allowed"] = False
    return result
