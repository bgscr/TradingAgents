"""Personal account's dated Eastmoney main-net percentage (Tushare document 349)."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from scripts import personal_bootstrap as cold

ENDPOINT = "moneyflow_dc"
FIELDS = "ts_code,trade_date,net_amount_rate"
SOURCE = "eastmoney.via_tushare.moneyflow_dc"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def request_key(binding, account):
    scope = [SOURCE, cold.BASE, FIELDS, "percent", account, binding]
    return (
        "personal-dc-ratio-v1:"
        + hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _percent(value):
    if type(value) not in (int, Decimal):
        raise ValueError("ratio_not_numeric")
    value = Decimal(value)
    if not value.is_finite() or abs(value) > 100 or abs(value.as_tuple().exponent) > 12:
        raise ValueError("ratio_out_of_range")
    return format(value, "f")


def project(raw, binding):
    """Reject wide/ambiguous replies before persisting anything in the coordinator."""
    from tradingagents.market_history.coordinator import (
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
    )

    def reject(reason="dc_ratio_unverified", *, denied=False):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED
            if denied
            else PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code=reason,
        )

    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            reject()
        return result

    if type(raw) is not bytes or len(raw) > 16384:
        reject()
    try:
        obj = json.loads(
            raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=lambda _: reject()
        )
        if type(obj) is not dict or type(obj.get("code")) is not int:
            reject()
        if obj["code"] != 0:
            reject(
                "dc_ratio_access_denied" if obj["code"] == 2002 else "dc_ratio_provider_rejected",
                denied=obj["code"] == 2002,
            )
        table = obj["data"]
        if (
            type(table) is not dict
            or set(table) - {"fields", "items", "has_more"}
            or table.get("has_more", False) is not False
            or table["fields"] != FIELDS.split(",")
            or type(table["items"]) is not list
            or len(table["items"]) > 1
        ):
            reject()
        ratio = None
        if table["items"]:
            row = table["items"][0]
            if type(row) is not list or len(row) != 3 or row[0] != binding["symbol"]:
                reject()
            if row[1] != binding["trade_date"].replace("-", ""):
                reject("dc_ratio_date_mismatch")
            ratio = _percent(row[2])
    except (ValueError, KeyError, TypeError, RecursionError, ArithmeticError):
        reject()
    return {
        "symbol": binding["symbol"],
        "trade_date": binding["trade_date"],
        "percent": ratio,
        "observed_at": cold.now().astimezone(timezone.utc).isoformat(),
    }


def cached(authority, key, binding):
    """Only the most recent matching physical single-send result may be reused."""
    from tradingagents.market_history.config import DataUsageMode
    from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
    from tradingagents.market_history.result_codec import (
        decode_coordinated_result,
        encode_coordinated_result,
    )

    if authority.data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH:
        return {"status": "cache_unverified"}
    miss = {"status": "cache_missing"}
    invalid = {"status": "cache_unverified"}
    if not authority.database_path.is_file():
        return miss
    upstream, service = upstream_service_identity_for_provider("tushare")
    with closing(sqlite3.connect(authority.database_path.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        seqs = c.execute(
            """
            SELECT sequence_id, owner_id, started_at, completed_at, status, final_physical_attempt_count,
                   result_sha256, length(result_payload) AS size, julianday(started_at) AS started,
                   failure_outcome, failure_outcome_kind, failure_status_code, failure_error_code,
                   failure_retryable, failure_retry_after_seconds
            FROM provider_request_sequences WHERE request_key=? AND upstream_service_id=?
            AND capacity_scope='market-snapshot'
            ORDER BY started IS NULL DESC, started DESC LIMIT 2
        """,
            (key, upstream),
        ).fetchall()
        if not seqs:
            return miss
        s = seqs[0]
        if s["started"] is None or len(seqs) == 2 and s["started"] == seqs[1]["started"]:
            return invalid
        if s["status"] != "succeeded":
            return {"status": "prior_attempt_unresolved_or_failed"}
        if (
            s["final_physical_attempt_count"] != 1
            or not isinstance(s["size"], int)
            or not 0 < s["size"] <= 4096
            or any(
                s[n] is not None
                for n in (
                    "failure_outcome",
                    "failure_outcome_kind",
                    "failure_status_code",
                    "failure_error_code",
                    "failure_retryable",
                    "failure_retry_after_seconds",
                )
            )
        ):
            return invalid
        attempts = c.execute(
            """
            SELECT attempted_at, attempt_index, owner_id, request_key, upstream_service_id, upstream_service_name,
                   operation, capacity_scope, outcome, terminal_outcome, terminal_outcome_kind, retryable, status_code,
                   error_code, cooldown_changed, retry_after_seconds, cooldown_until, final_physical_attempt_count
            FROM provider_request_attempts WHERE sequence_id=? LIMIT 2
        """,
            (s["sequence_id"],),
        ).fetchall()
        if len(attempts) != 1:
            return invalid
        a = attempts[0]
        expected = {
            "owner_id": s["owner_id"],
            "request_key": key,
            "upstream_service_id": upstream,
            "upstream_service_name": service,
            "operation": ENDPOINT,
            "capacity_scope": "market-snapshot",
            "attempt_index": 1,
            "final_physical_attempt_count": 1,
            "outcome": "available",
            "terminal_outcome": "available",
            "terminal_outcome_kind": "available",
            "retryable": 0,
            "error_code": None,
            "cooldown_changed": 0,
            "cooldown_until": None,
            "retry_after_seconds": None,
        }
        if any(a[k] != v for k, v in expected.items()) or a["status_code"] not in (200, None):
            return invalid
        raw = c.execute(
            "SELECT result_payload FROM provider_request_sequences WHERE sequence_id=? AND length(result_payload)<=4096",
            (s["sequence_id"],),
        ).fetchone()
        if (
            not raw
            or type(raw[0]) is not bytes
            or hashlib.sha256(raw[0]).hexdigest() != s["result_sha256"]
        ):
            return invalid
    try:
        v = decode_coordinated_result(raw[0], projection_only=True)
        if (
            encode_coordinated_result(v) != raw[0]
            or type(v) is not dict
            or set(v) != {"symbol", "trade_date", "percent", "observed_at"}
            or (v["symbol"], v["trade_date"]) != (binding["symbol"], binding["trade_date"])
        ):
            return invalid
        started, attempted, observed, completed = map(
            datetime.fromisoformat,
            (s["started_at"], a["attempted_at"], v["observed_at"], s["completed_at"]),
        )
        now = cold.now()
        if (
            any(x.utcoffset() != timedelta(0) for x in (started, attempted, observed, completed))
            or not started <= attempted <= observed <= completed <= now
            or observed.astimezone(SHANGHAI).date() <= date.fromisoformat(v["trade_date"])
        ):
            return invalid
        if now - observed >= timedelta(days=1):
            return {"status": "cache_stale"}
        if v["percent"] is not None and (
            type(v["percent"]) is not str or _percent(Decimal(v["percent"])) != v["percent"]
        ):
            return invalid
    except (KeyError, ValueError, TypeError, ArithmeticError, RecursionError):
        return invalid
    result = {
        "source": SOURCE,
        "trade_date": v["trade_date"],
        "observed_at": v["observed_at"],
        "acquisition": "coordinator_cache",
    }
    if v["percent"] is None:
        return dict(result, status="valid_empty")
    return dict(result, status="verified", main_net_percent=v["percent"])


def observe(session, binding, token, authority, *, fetch):
    from tradingagents.market_history.config import provider_request_authority_config
    from tradingagents.market_history.coordinator import PhysicalAttemptBudgetExhausted

    if (
        authority != provider_request_authority_config(session.history)
        or not isinstance(token, str)
        or "personal-" + cold.digest_bytes(("personal-tushare-v1\0" + token).encode())
        != session.account_scope
    ):
        return {"status": "account_scope_unverified"}
    if date.fromisoformat(binding["trade_date"]) >= cold.now().astimezone(SHANGHAI).date():
        return {"status": "publication_pending"}
    key = request_key(binding, session.account_scope)
    value = cached(authority, key, binding)
    if not fetch or value["status"] not in ("cache_missing", "cache_stale"):
        return value
    if not session.financial_capacity_available(ENDPOINT):
        return {"status": "financial_budget_unavailable"}
    try:
        session.query(
            token,
            ENDPOINT,
            {"ts_code": binding["symbol"], "trade_date": binding["trade_date"].replace("-", "")},
            FIELDS,
            observation_binding=binding,
        )
    except PhysicalAttemptBudgetExhausted as exc:
        status = exc.failure.outcome.value
        if exc.failure.error_code == "dc_ratio_date_mismatch":
            status = "date_mismatch"
        return {
            "status": status
            if status in {"permission_denied", "authentication", "date_mismatch", "rate_limited"}
            else "unverified"
        }
    # Re-read the terminal event and digest even for a freshly acquired value.
    value = cached(authority, key, binding)
    if value["status"] in {"verified", "valid_empty"}:
        value["acquisition"] = "account_request"
    return value
