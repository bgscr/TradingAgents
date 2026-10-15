"""Capture one authorized public news response, or replay a retained response offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import install  # noqa: E402


def _content_source_passed(source, kind):
    if not isinstance(source, dict) or kind not in {"exact_text", "validated_projection"}:
        return False
    retention, saved = (
        ("retention", "body_saved") if kind == "exact_text" else ("projection", "projection_saved")
    )
    checks = source.get("checks")
    return source.get(saved) is True and isinstance(checks, dict) and all(
        isinstance(checks.get(name), dict) and checks[name].get("status") == "passed"
        for name in ("network", "http", "decode", "sdk", "production", "receipt", retention)
    )


def _acceptance(result, mode, captured=None):
    if result["mode"] == "capture":
        original = result["capture_passed"]
        content = bool(result.get("sdk_frame_sha256")) and (
            original or result["projection_saved"]
        )
        source = result
        kind = "exact_text" if result["body_saved"] else "validated_projection"
    else:
        original = result["acceptance_passed"]
        content = (
            (original or result["projection_replay_passed"])
            and result.get("same_sdk_frame") is True
        )
        source, kind = captured, result.get("replay_kind")
    source_passed = _content_source_passed(source, kind)
    content = content and source_passed
    goals = {"original-text": original, "news-content": content}
    return {"mode": mode, "passed": goals[mode], "goals": goals, "content_source_passed": source_passed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--acceptance-mode", choices=("original-text", "news-content"), default="original-text",
        help="exit-status goal for this capture/replay phase (default: original-text)",
    )
    parser.add_argument("--authorize-one-public-get-and-save", action="store_true")
    args = parser.parse_args()
    if bool(args.replay) == args.authorize_one_public_get_and_save:
        parser.error(
            "choose offline --replay or explicitly authorized one GET with public-response retention"
        )
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("use Python -I -S -B")
    root = args.output.resolve()
    if root.exists() or root.is_relative_to(SOURCE):
        parser.error("fresh isolated directory outside source required")
    root.mkdir()
    # Copy only the two named retained public diagnostic files before the worker
    # guard. Never read arbitrary source directories/configuration or credentials.
    captured = None
    if args.replay:
        def read_input(name):
            source = args.replay.resolve() / name
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
                parser.error("invalid retained response file")
            return source.read_bytes()

        receipt = read_input("capture.json")
        try:
            captured = json.loads(receipt)
        except (ValueError, UnicodeError):
            parser.error("invalid capture receipt")
        if not isinstance(captured, dict):
            parser.error("invalid capture receipt")
        if captured.get("body_saved") is True:
            body_name = "public-response.txt"
        elif captured.get("projection_saved") is True:
            body_name = "public-projection.txt"
        else:
            parser.error("capture has no retained exact text or validated projection")
        for name, content in (("capture.json", receipt), (body_name, read_input(body_name))):
            (root / name).write_bytes(content)
    (root / "reservation.json").write_text(
        json.dumps(
            {
                "live": args.authorize_one_public_get_and_save,
                "acceptance_mode": args.acceptance_mode,
                "physical_request_limit": 1 if not args.replay else 0,
                "redirects": 0,
                "retries": 0,
                "symbol": "600895",
                "page": 1,
                "page_size": 10,
                "endpoint": "https://search-api-web.eastmoney.com/search/jsonp",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    counts, blocked_network = install(root, SOURCE, offline=bool(args.replay))
    from tradingagents import news_diagnostic

    if args.replay:
        result = news_diagnostic.replay(root, start=args.start, end=args.end)
    else:
        from curl_cffi import requests

        # Use the SDK's transport, params, fixed timestamp, static headers and
        # Response.text decoding. The diagnostic only tightens timeout/redirects.
        result = news_diagnostic.capture(root, get=requests.get, start=args.start, end=args.end)
    result["guard_counts"] = counts
    result["blocked_network"] = blocked_network
    result["acceptance"] = _acceptance(result, args.acceptance_mode, captured)
    (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "stage": result["stage"], "body_saved": result["body_saved"],
        "capture_passed": result["capture_passed"],
        "acceptance_passed": result["acceptance_passed"], "checks": result["checks"],
        "projection_saved": result["projection_saved"],
        "projection_replay_passed": result["projection_replay_passed"],
        "replay_kind": result.get("replay_kind"),
        "acceptance": result["acceptance"],
    }))
    return 0 if result["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
