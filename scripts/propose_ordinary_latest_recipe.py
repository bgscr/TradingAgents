"""Print a non-installable successor proposal from the independently pinned parent recipe."""

import argparse
import json
import re
import runpy
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
H = runpy.run_path(str(ROOT / "scripts/handoff_ordinary_full.py"))
DAILY_REFS = ("contract_sha256", "reservation_sha256", "raw_sha256", "receipt_sha256")
# The isolated, no-run-ID reader adds the project root only. Qualify the two
# imports it reaches, without adding the whole scripts directory to sys.path.
IMPORTS = {
    "scripts/full_calendar_cache.py": ("full_auction_schedule", 4),
    "scripts/full_date_evidence.py": ("full_auction_schedule", 1),
    "scripts/full_auction_schedule.py": ("full_financial_channel", 1),
}


NEW_HELPER = "scripts/proposed_latest_target_review.py"
INIT = "scripts/__init__.py"
INIT_SOURCE = b'"""Ordinary latest candidate helpers."""\n'
INTAKE = "scripts/full_latest_intake.py"
ACTIVATION = "config/portfolio_research_activation.json"
# Independently reviewed byte identities for the one activation-policy entry that moves.
INTAKE_BEFORE = "e44852edefe43d83d50cba5554c4e399e72541a9522331a8ae5e23d50033f846"
INTAKE_AFTER = "aba5edd59ceb3dffa9441255731a697c63a230c00e470acf4bc82532723d0b5c"
CHANGED = {*IMPORTS, INTAKE, ACTIVATION}


def added_sources():
    return {
        INIT: INIT_SOURCE,
        NEW_HELPER: H["R"]["small_file"](ROOT / NEW_HELPER, 32768).replace(b"\r\n", b"\n"),
    }


def change_once(data, before, after):
    H["require"](data.count(before) == 1, "unexpected_parent_source")
    return data.replace(before, after)


def proposed_source(name, data, expected):
    lf = data.replace(b"\r\n", b"\n")
    original = next(
        (b for b in (data, lf, lf.replace(b"\n", b"\r\n")) if H["sha"](b) == expected), None
    )
    H["require"](original is not None, "parent_bytes_required")
    if name == ACTIVATION:
        return change_once(original, INTAKE_BEFORE.encode("ascii"), INTAKE_AFTER.encode("ascii"))
    if name == INTAKE:
        result = change_once(
            original,
            b'"scripts/full_calendar_cache.py",',
            b'"scripts/full_calendar_cache.py","scripts/__init__.py","scripts/proposed_latest_target_review.py",',
        )
        H["require"](
            expected == INTAKE_BEFORE and H["sha"](result) == INTAKE_AFTER, "intake_pin_mismatch"
        )
        return result
    module, count = IMPORTS[name]
    old = f"from {module} import ".encode("ascii")
    H["require"](original.count(old) == count, "unexpected_parent_imports")
    new = original.replace(old, f"from scripts.{module} import ".encode("ascii"))
    if name == "scripts/full_calendar_cache.py":
        nl = b"\r\n" if b"\r\n" in original else b"\n"
        new = change_once(new, b"if latest and daily_run:", b"if latest:")
        new = change_once(
            new, b"    matched = None", b"    used_owned_close = False" + nl + b"    matched = None"
        )
        new = change_once(
            new,
            b"            matched=read_registered(",
            b"            used_owned_close = True" + nl + b"            matched=read_registered(",
        )
        new = change_once(
            new,
            b"    return dict(trading_date=str(day),",
            b"    selection = dict(trading_date=str(day),",
        )
        anchor = b"counts_as_full_acceptance=False)"
        new = change_once(
            new,
            anchor,
            nl.join(
                [
                    anchor,
                    b"    if latest and not used_owned_close:",
                    b"        from scripts.proposed_latest_target_review import check",
                    b"        check(selection, release_root, manifest, key, packet)",
                    b"    return selection",
                ]
            ),
        )
    return new


def proposed_close_bindings(ticker, day, projection, expected_digest):
    """Expand an independently pinned one-day projection for review. This grants no trust."""
    require = H["require"]
    require(
        re.fullmatch(r"[0-9]{6}\.(?:SS|SZ)", ticker) and date.fromisoformat(day).isoformat() == day,
        "invalid_selection",
    )
    require(H["sha"](projection) == expected_digest, "projection_digest_mismatch")
    packet = json.loads(projection, object_pairs_hook=H["R"]["unique"])
    require(
        isinstance(packet, dict)
        and packet.get("ticker") == ticker
        and packet.get("source") == "https://api.waditu.com/dataapi/daily",
        "wrong_close_source",
    )
    rows = packet.get("rows")
    require(
        isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and rows[0].get("trading_date") == day,
        "wrong_close_date",
    )
    refs = [packet.get("contract_sha256"), *(rows[0].get(k) for k in DAILY_REFS[1:])]
    require(
        all(type(v) is str and re.fullmatch("[0-9a-f]{64}", v) for v in refs)
        and len(set(refs)) == len(refs),
        "invalid_close_references",
    )
    prefix = "config/full_evidence/"
    key = prefix + ("" if ticker == "601328.SS" else ticker + "/") + "market-releases.json"
    return {key: expected_digest, **{prefix + "originals/" + v + ".bin": v for v in refs}}


def proposal():
    base = H["recipe"](ROOT)
    public = H["public"](base)
    changes = {}
    for name in sorted(CHANGED):
        before = public[name]
        after = proposed_source(name, H["R"]["small_file"](ROOT / name, 2_000_000), before)
        public[name] = H["sha"](after)
        changes[name] = {"before": before, "proposed": public[name]}
    additions = {n: H["sha"](b) for n, b in added_sources().items()}
    return describe_proposal(base, public, changes, additions)


def describe_proposal(base, public, changes, additions):
    H["require"](not (public.keys() & additions.keys()), "unexpected_existing_helper")
    public.update(additions)
    return {
        "contract": "ordinary-full-latest-recipe-proposal-v1",
        "status": "unapproved",
        "base_product": H["RELEASE"],
        "base_recipe_sha256": H["PIN"],
        "public_project": public,
        "proposed_changes": changes,
        "proposed_additions": additions,
        "target_runtime": base["runtime"],
        "explicit_historical_replay": {
            "requires_parent_recipe": H["PIN"],
            **base["local_evidence"],
        },
        "latest_evidence": {
            "scope": "current_retrospective_selection",
            "calendar": "target-owned CURRENT personal-research exchange calendar with verified revision and observation",
            "security_close": "target-reviewed ticker-specific market-releases projection",
            "daily_original_bindings": list(DAILY_REFS),
            "target_review_pattern": "config/full_evidence/{venue}/{canonical_security}/latest-target-review.json",
            "target_review_fields": [
                "contract",
                "purpose",
                "projection_sha256",
                "contract_sha256",
                "security",
                "venue",
                "trading_date",
                "calendar_revision",
                "calendar_observed_at",
                *DAILY_REFS[1:],
            ],
            "admission": "independent release review must bind exact target evidence bytes and provenance",
        },
        "existing_limits": {
            "check_only_external_attempts": 0,
            "full_max_attempts": 60,
            "financial_max_attempts": 3,
            "full_deadline_seconds": 1200,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=sorted(CHANGED | {NEW_HELPER, INIT}),
        help="Write one proposed public postimage to stdout as exact bytes",
    )
    args = parser.parse_args()
    try:
        H["require"](
            sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "isolated_python_required",
        )
        candidate = proposal()
        if args.source:
            name = args.source
            if name in CHANGED:
                data = proposed_source(
                    name,
                    H["R"]["small_file"](ROOT / name, 2_000_000),
                    candidate["proposed_changes"][name]["before"],
                )
            else:
                data = added_sources()[name]
            H["require"](H["sha"](data) == candidate["public_project"][name], "proposal_changed")
            sys.stdout.buffer.write(data)
        else:
            print(json.dumps(candidate, ensure_ascii=True, indent=2))
    except (ValueError, OSError, KeyError):
        print("Reviewed parent recipe unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
