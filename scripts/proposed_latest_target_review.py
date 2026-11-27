"""Proposed latest-only CURRENT evidence contract. Unused by the accepted release."""

import json
import re
from datetime import datetime
from hashlib import sha256


def read_review(root, manifest, key):
    from scripts.full_financial_channel import ordinary

    matches = [
        r["sha256"]
        for r in manifest["files"]
        if r["root"] == "project" and r["relative"].replace(chr(92), "/") == key
    ]
    if len(matches) != 1:
        raise ValueError("reviewed_input_missing")
    path = ordinary(root / key)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("invalid_target_review")
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192 or sha256(raw).hexdigest() != matches[0]:
        raise ValueError("target_review_digest_mismatch")

    def unique(pairs):
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError("duplicate_target_review_field")
        return values

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def check(selection, root, manifest, projection_key, packet):
    def require(ok, reason):
        if not ok:
            raise ValueError(reason)

    symbol = selection["security"]
    venue = selection["venue"]
    require(
        re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", symbol) and venue in {"XSHG", "XSHE"},
        "invalid_target_identity",
    )
    key = f"config/full_evidence/{venue}/{symbol}/latest-target-review.json"
    expected = read_review(root, manifest, key)
    direct = ("security", "venue", "trading_date", "calendar_revision")
    refs = ("raw_sha256", "reservation_sha256", "receipt_sha256")
    require(
        isinstance(expected, dict)
        and set(expected)
        == {
            "contract",
            "purpose",
            *direct,
            "calendar_observed_at",
            "projection_sha256",
            "contract_sha256",
            *refs,
        },
        "invalid_target_review",
    )
    require(
        expected["contract"] == "ordinary-latest-target-review-v1"
        and selection["purpose"] == expected["purpose"] == "current_retrospective_selection"
        and selection["counts_as_full_acceptance"] is False,
        "wrong_evidence_purpose",
    )
    for field in direct:
        require(selection[field] == expected[field], "target_evidence_mismatch_" + field)
    schedule = selection.get("auction_schedule")
    require(
        isinstance(schedule, dict) and schedule["venue"] == venue,
        "exchange_scoped_calendar_required",
    )
    times = [datetime.fromisoformat(x["calendar_observed_at"]) for x in (expected, selection)]
    require(
        all(t.utcoffset() is not None for t in times) and times[0] == times[1],
        "target_calendar_observation_mismatch",
    )
    matches = [
        r["sha256"]
        for r in manifest["files"]
        if r["root"] == "project" and r["relative"].replace(chr(92), "/") == projection_key
    ]
    require(
        matches == [expected["projection_sha256"]]
        and packet.get("source") == "https://api.waditu.com/dataapi/daily"
        and packet.get("contract_sha256") == expected["contract_sha256"],
        "target_projection_mismatch",
    )
    close = selection["close_evidence"]
    require(
        isinstance(close, dict)
        and close.get("scope") == "auction_close_price"
        and close.get("trading_date") == expected["trading_date"],
        "wrong_close_scope",
    )
    for field in refs:
        require(
            type(expected[field]) is str
            and re.fullmatch("[0-9a-f]{64}", expected[field])
            and close.get(field) == expected[field],
            "target_close_reference_mismatch",
        )
