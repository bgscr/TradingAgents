"""Capture one authorized public news response, or replay a retained response offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import install  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
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
    if args.replay:
        for name in ("capture.json", "public-response.txt"):
            source = args.replay.resolve() / name
            if source.is_symlink() or source.stat().st_size > 1024 * 1024:
                parser.error("invalid retained response file")
            (root / name).write_bytes(source.read_bytes())
    (root / "reservation.json").write_text(
        json.dumps(
            {
                "live": args.authorize_one_public_get_and_save,
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
    (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"stage": result["stage"], "body_saved": result["body_saved"]}))
    return 0 if result["stage"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
