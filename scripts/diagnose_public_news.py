"""Capture one authorized public news response, or replay a retained response offline."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import install  # noqa: E402

_ACCEPT_VARIANTS = {
    "accept-javascript-v1": "text/javascript",
    "accept-edge-value-v1": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
}
_FRESH_QUERY_VARIANT = "fresh-cachebuster-v1"


def _cache_response_metadata(headers):
    """Small standard cache observation, not cache/source certification.

    Opaque validators, addresses and arbitrary directive/field names are never
    read back. Unsupported syntax is hidden whole, not partially salvaged.
    """
    view = {"contract": "news-cache-metadata-v1", "qualification_authority": False}
    for name in ("Date", "Expires", "Last-Modified"):
        raw = headers.get(name)
        value, status = None, "absent" if raw is None else "hidden"
        if type(raw) is str and len(raw) == 29 and re.fullmatch(
                r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT", raw):
            try:
                parsed = parsedate_to_datetime(raw)
                if format_datetime(parsed, usegmt=True) == raw:
                    value, status = raw, "standard_http_date"
            except (ValueError, TypeError, OverflowError):
                pass
        view[name.lower()] = {"status": status, "value": value}
    raw = headers.get("Age")
    age = int(raw) if type(raw) is str and re.fullmatch(r"[0-9]{1,10}", raw) and int(raw) <= 2147483648 else None
    view["age"] = {"status": "absent" if raw is None else "delta_seconds" if age is not None else "hidden", "seconds": age}
    raw = headers.get("Cache-Control")
    directives = {}
    status = "absent" if raw is None else "hidden"
    if type(raw) is str and 0 < len(raw) <= 256:
        parts = raw.split(",")
        if len(parts) <= 16:
            for part in parts:
                part = part.strip(" \t").lower()
                if part in {"no-cache", "no-store", "public", "private", "must-revalidate", "proxy-revalidate", "must-understand", "no-transform", "immutable"}:
                    key, value = part, True
                else:
                    match = re.fullmatch(r"(max-age|s-maxage|stale-while-revalidate|stale-if-error)=([0-9]{1,10})", part)
                    if not match or int(match[2]) > 2147483648:
                        break
                    key, value = match[1], int(match[2])
                if key in directives:
                    break
                directives[key] = value
            else:
                status = "recognized_directives"
    if "public" in directives and "private" in directives:
        status = "hidden"
    view["cache_control"] = {"status": status, "directives": directives if status == "recognized_directives" else {}}
    view["presence_only"] = {name.lower(): name in headers for name in (
        "ETag", "Vary", "Via", "Cache-Status", "X-Cache", "CF-Cache-Status")}
    return view


def _input_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink, info.st_mode


def _read_replay_input(root, name):
    """Validate original input before copying; never resolve away a reparse root."""
    if name not in {"capture.json", "public-response.bin"} or ".." in root.parts:
        raise ValueError("invalid retained input")
    path = root / name
    root_info = root.lstat()
    before = path.lstat()
    if any(stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024
           for info in (root_info, before)):
        raise ValueError("linked retained input")
    if (not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1 or before.st_size > 1024 * 1024):
        raise ValueError("invalid retained input")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if (_input_identity(os.fstat(stream.fileno())) != _input_identity(before)
                or _input_identity(root.lstat()) != _input_identity(root_info)):
            raise ValueError("changed retained input")
        data = stream.read(1024 * 1024 + 1)
        if (len(data) > 1024 * 1024
                or _input_identity(os.fstat(stream.fileno())) != _input_identity(before)
                or _input_identity(path.lstat()) != _input_identity(before)
                or _input_identity(root.lstat()) != _input_identity(root_info)):
            raise ValueError("changed retained input")
    return data


def _content_source_passed(source, kind):
    from tradingagents.news_diagnostic import source_capture_passed

    return kind == "exact_bytes" and source_capture_passed(source)


def _acceptance(result, mode, captured=None):
    if result["mode"] == "live_metadata_only":
        passed = result.get("metadata_passed") is True
        return {"mode": "metadata-only", "passed": passed,
                "goals": {"original-text": False, "news-content": False, "metadata-only": passed},
                "content_source_passed": False}
    if result["mode"] == "capture":
        original = result["capture_passed"]
        content = bool(result.get("adapter_frame_sha256")) and original
        source = result
        kind = "exact_bytes"
    else:
        original = result["acceptance_passed"]
        content = (
            original and result.get("same_adapter_frame_sha256") is True
        )
        source, kind = captured, result.get("replay_kind")
    source_passed = _content_source_passed(source, kind)
    content = content and source_passed
    goals = {"original-text": original, "news-content": content}
    return {"mode": mode, "passed": goals[mode], "goals": goals, "content_source_passed": source_passed}


def _response_metadata(response):
    """Closed negotiation fields only; never body, headers-as-a-whole or URLs."""
    headers = response.headers
    content_type = headers.get("Content-Type")
    if isinstance(content_type, str) and len(content_type) <= 100:
        content_type = re.sub(r"\s+", "", content_type).lower()
        if re.fullmatch(r"(?:text/(?:javascript|plain|html)|application/(?:javascript|json|x-javascript))"
                        r"(?:;charset=(?:utf-8|utf8|gbk|gb2312))?", content_type) is None:
            content_type = "other"
    else:
        content_type = None if content_type is None else "other"
    result = {"content_type": content_type}
    for key, field, allowed in (
        ("Content-Encoding", "content_encoding", {"gzip", "deflate", "br", "zstd", "identity"}),
        ("Transfer-Encoding", "transfer_encoding", {"chunked", "identity"}),
    ):
        value = headers.get(key)
        result[field] = (value.lower().strip() if type(value) is str and len(value) <= 32 and value.lower().strip() in allowed
                         else None if value is None else "other")
    length = headers.get("Content-Length")
    result["content_length_header"] = (
        int(length) if type(length) is str and re.fullmatch(r"[0-9]{1,16}", length)
        and int(length) <= 2**53 else None)
    version = getattr(getattr(response, "raw", None), "version", None)
    result["http_protocol"] = {10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}.get(version) if type(version) is int else None
    result["cache_source"] = "unknown"
    return result


@contextmanager
def _metadata_session(metadata):
    import requests

    session = requests.Session()
    primary = None
    try:
        yield session
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            session.close()
        except BaseException as error:
            metadata["session_close"] = "failed"
            # Ordinary cleanup failures become closed evidence. Cancellation or
            # another terminal error still propagates; never replace a first one.
            if primary is None and not isinstance(error, Exception):
                raise
        else:
            metadata["session_close"] = "closed"


def _capture_direct(root, *, start, end, ticker, metadata_only=False, percent_encoded_spaces=False,
                    request_variant="production", public_structure_metadata=False):
    """A caller-selected, fresh session; no implicit proxy/netrc/CA discovery."""
    import requests
    import urllib3

    from tradingagents import news_diagnostic

    if type(metadata_only) is not bool or type(percent_encoded_spaces) is not bool or (percent_encoded_spaces and not metadata_only):
        raise ValueError("invalid_metadata_transport_options")
    if (type(request_variant) is not str or request_variant not in {"production", "omit-referer-v1", _FRESH_QUERY_VARIANT, *_ACCEPT_VARIANTS}
            or (request_variant != "production" and (not metadata_only or not percent_encoded_spaces
                                                       or ticker != "600919.SH"))):
        raise ValueError("invalid_metadata_request_variant")
    if (type(public_structure_metadata) is not bool or (public_structure_metadata and (
            not metadata_only or not percent_encoded_spaces or ticker != "600919.SH"
            or request_variant not in {"production", _FRESH_QUERY_VARIANT}))
            or (request_variant == _FRESH_QUERY_VARIANT and not public_structure_metadata)):
        raise ValueError("invalid_public_structure_scope")
    metadata = {"send_attempts": 0, "route": "unconfirmed", "trust_env": False,
                "redirects_allowed": False, "retries": 0, "timeout_seconds": 15,
                "percent_encoded_spaces": percent_encoded_spaces, "request_variant": request_variant}
    if public_structure_metadata:
        metadata["public_structure_metadata"] = True
    prepared_public_request = False
    for name, value in (("requests_version", requests.__version__), ("urllib3_version", urllib3.__version__)):
        metadata[name] = value if type(value) is str and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value) else None

    with (_metadata_session(metadata) if metadata_only else requests.Session()) as session:
        session.trust_env = False
        session.proxies = {}
        session.verify = True

        def direct_get(url, *, params, headers, allow_redirects, timeout):
            nonlocal prepared_public_request
            # Fail closed if the installed Requests defaults drift. Do not alter
            # its retry policy, TLS validation, production timeout or redirects.
            if (session.get_adapter(url).max_retries.total != 0
                    or allow_redirects is not False or timeout != 15):
                raise ValueError("direct transport settings rejected")
            if metadata_only:
                symbol = ticker[:6]
                expected = {"uid": "", "keyword": symbol, "type": ["cmsArticleWebOld"],
                            "client": "web", "clientType": "web", "clientVersion": "curr",
                            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                                      "pageIndex": 1, "pageSize": 10, "preTag": "<em>", "postTag": "</em>"}}}
                if (url != news_diagnostic.URL or set(params) != {"cb", "param", "_"}
                        or params["cb"] != "jQuery35101792940631092459_1764599530165"
                        or params["_"] != "1764599530176" or json.loads(params["param"]) != expected
                        or metadata["send_attempts"] != 0
                        or {key.lower() for key in headers} & {"cookie", "authorization", "proxy-authorization"}):
                    raise ValueError("metadata_request_binding_rejected")
                request = session.prepare_request(requests.Request("GET", url, params=params, headers=headers))
                original_url = request.url
                canonical_params = {"cb": "jQuery35101792940631092459_1764599530165",
                                    "param": json.dumps(expected, ensure_ascii=False), "_": "1764599530176"}
                if (params != canonical_params or original_url != url + "?" + urlencode(canonical_params)
                        or {key.lower() for key in request.headers} & {"cookie", "authorization", "proxy-authorization"}
                        or request.body is not None):
                    raise ValueError("metadata_prepared_request_rejected")
                if percent_encoded_spaces:
                    request.url = original_url.replace("+", "%20")
                original_pairs = parse_qsl(urlsplit(original_url).query, keep_blank_values=True)
                pairs = parse_qsl(urlsplit(request.url).query, keep_blank_values=True)
                if pairs != original_pairs or [key for key, _ in pairs] != ["cb", "param", "_"]:
                    raise ValueError("metadata_prepared_url_rejected")
                # A single fixed diagnostic change, never arbitrary header
                # injection. Snapshot after the separately bound URL encoding.
                baseline = (request.method, request.url, request.body)
                expected_url = request.url
                baseline_headers = dict(request.headers)
                expected_headers = dict(baseline_headers)
                if request_variant == "omit-referer-v1":
                    if request.headers.get("referer") != f"https://so.eastmoney.com/news/s?keyword={symbol}":
                        raise ValueError("metadata_baseline_referer_rejected")
                    referer_key = next(key for key in expected_headers if key.lower() == "referer")
                    del expected_headers[referer_key]
                    del request.headers["referer"]
                elif request_variant in _ACCEPT_VARIANTS:
                    if request.headers.get("accept") != "*/*":
                        raise ValueError("metadata_baseline_accept_rejected")
                    accept_key = next(key for key in expected_headers if key.lower() == "accept")
                    expected_headers[accept_key] = _ACCEPT_VARIANTS[request_variant]
                    request.headers["accept"] = _ACCEPT_VARIANTS[request_variant]
                elif request_variant == _FRESH_QUERY_VARIANT:
                    fresh = str(time.time_ns() // 1_000_000)
                    if (not re.fullmatch(r"[0-9]{13}", fresh) or not 1577836800000 <= int(fresh) <= 4102444800000
                            or fresh == canonical_params["_"] or not request.url.endswith("&_=" + canonical_params["_"])):
                        raise ValueError("metadata_fresh_query_rejected")
                    expected_url = request.url.rsplit("&_=", 1)[0] + "&_=" + fresh
                    request.url = expected_url
                    changed_pairs = parse_qsl(urlsplit(request.url).query, keep_blank_values=True)
                    if changed_pairs != [*pairs[:-1], ("_", fresh)]:
                        raise ValueError("metadata_query_delta_rejected")
                    metadata["request_query_delta"] = [{"field": "_", "before": canonical_params["_"], "after": fresh}]
                if ((request.method, request.url, request.body) != (baseline[0], expected_url, baseline[2])
                        or dict(request.headers) != expected_headers):
                    raise ValueError("metadata_variant_delta_rejected")
                if public_structure_metadata:
                    if (request.method != "GET" or session.auth is not None or session.cookies
                            or session.trust_env is not False or session.proxies or session.verify is not True):
                        raise ValueError("public_metadata_prepared_binding_rejected")
                    prepared_public_request = True
                metadata.update(method="GET", prepared_url_original=original_url, sent_url=request.url,
                                decoded_query_equal=parse_qsl(urlsplit(request.url).query, keep_blank_values=True) == original_pairs,
                                byte_url_equal=original_url == request.url)
                metadata["request_header_delta"] = (
                    [{"field": "referer", "operation": "remove"}] if request_variant == "omit-referer-v1" else
                    [{"field": "accept", "operation": "replace", "before": "*/*", "after": _ACCEPT_VARIANTS[request_variant]}]
                    if request_variant in _ACCEPT_VARIANTS else [])
                metadata["prepared_variant_verified"] = True
                metadata["request_fingerprint_scope"] = "production_request_before_diagnostic_variant"
                # Retain only exact, already-public production header values.
                safe_headers = {"accept": "*/*", "accept-encoding": "gzip, deflate",
                                "accept-language": "en,zh-CN;q=0.9,zh;q=0.8", "cache-control": "no-cache",
                                "referer": f"https://so.eastmoney.com/news/s?keyword={symbol}"}
                if request_variant in _ACCEPT_VARIANTS:
                    safe_headers["accept"] = _ACCEPT_VARIANTS[request_variant]
                if request_variant == _FRESH_QUERY_VARIANT:
                    safe_headers["pragma"] = "no-cache"
                actual_headers = {key.lower(): value for key, value in request.headers.items()}
                metadata["request_headers"] = {key: value if actual_headers.get(key) == value else "other_or_absent"
                                               for key, value in safe_headers.items()}
                if request_variant == "omit-referer-v1":
                    metadata["request_headers"]["referer"] = "absent"
                metadata["started_utc"] = datetime.now(timezone.utc).isoformat()
                started = time.monotonic()
                metadata["send_attempts"] += 1
                try:
                    response = session.send(request, allow_redirects=False, timeout=15, proxies={}, verify=True)
                finally:
                    metadata["finished_utc"] = datetime.now(timezone.utc).isoformat()
                    metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
                try:
                    metadata["response"] = _response_metadata(response)
                    metadata["final_url_matches_sent"] = response.url == request.url
                    metadata["redirect_status"] = 300 <= response.status_code < 400
                except Exception:
                    metadata["response_metadata_unavailable"] = True
                if request_variant == _FRESH_QUERY_VARIANT:
                    metadata["cache_response"] = {"contract": "news-cache-metadata-v1", "status": "unavailable"}
                    try:
                        metadata["cache_response"] = _cache_response_metadata(response.headers)
                    except Exception:
                        pass
                    except BaseException:
                        # Ownership has not reached bounded_get; close here on
                        # terminal observer failure without replacing that error.
                        try:
                            response.close()
                        except BaseException:
                            metadata["response_close"] = "failed"
                        else:
                            metadata["response_close"] = "closed"
                        raise
                return response
            return session.get(url, params=params, headers=headers,
                               allow_redirects=False, timeout=15, proxies={}, verify=True)

        if not metadata_only:
            return news_diagnostic.capture(root, get=direct_get, start=start, end=end,
                                           ticker=ticker, transport="direct")
        result = news_diagnostic._run(root, get=direct_get, start=start, end=end, retain=True,
                                      ticker=ticker, transport="direct", metadata_only=True,
                                      public_metadata_binding=(lambda: prepared_public_request and metadata["send_attempts"] == 1)
                                      if public_structure_metadata else None)
    result["transport_metadata"] = metadata
    if metadata["session_close"] == "failed":
        news_diagnostic._check(result, "cleanup", "failed", "session_close_failed", stage="session_close")
    return news_diagnostic._save_capture_receipt(result, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--ticker", required=True, choices=("600895.SH", "600919.SH"),
                        help="explicit authorized diagnostic target; no default")
    parser.add_argument("--transport", required=True, choices=("direct",),
                        help="explicit transport choice; no implicit proxy discovery or fallback")
    parser.add_argument(
        "--acceptance-mode", choices=("original-text", "news-content", "metadata-only"), default=None,
        help="exit-status goal for this capture/replay phase (default: original-text)",
    )
    parser.add_argument("--authorize-one-public-get-and-save", action="store_true")
    parser.add_argument("--authorize-one-public-get-metadata-only", action="store_true",
                        help="one live GET; same validation, no body/hash/article retention or byte replay")
    parser.add_argument("--percent-encoded-spaces", action="store_true",
                        help="metadata-only comparison: encode query spaces as %%20; decoded parameters unchanged")
    parser.add_argument("--request-variant", choices=("production", "omit-referer-v1", _FRESH_QUERY_VARIANT, *_ACCEPT_VARIANTS), default="production",
                        help="one fixed diagnostic variant; non-production requires metadata-only, %%20 and 600919.SH")
    parser.add_argument("--public-structure-metadata", action="store_true",
                        help="opt-in public callback/result names; production or fresh-cachebuster-v1, metadata-only, %%20, 600919.SH only")
    args = parser.parse_args()
    metadata_only = args.authorize_one_public_get_metadata_only
    if sum((bool(args.replay), args.authorize_one_public_get_and_save, metadata_only)) != 1:
        parser.error(
            "choose exactly one of offline replay, live capture, or live metadata-only"
        )
    args.acceptance_mode = args.acceptance_mode or ("metadata-only" if metadata_only else "original-text")
    if ((args.acceptance_mode == "metadata-only") != metadata_only
            or (metadata_only and args.acceptance_mode != "metadata-only")
            or (args.percent_encoded_spaces and not metadata_only)):
        parser.error("metadata-only options require explicit live metadata-only mode")
    if args.request_variant != "production" and (not metadata_only or not args.percent_encoded_spaces
                                                 or args.ticker != "600919.SH"):
        parser.error("request variant requires metadata-only, percent-encoded-spaces and 600919.SH")
    if args.public_structure_metadata and (not metadata_only or not args.percent_encoded_spaces
                                           or args.ticker != "600919.SH" or args.request_variant not in {"production", _FRESH_QUERY_VARIANT}):
        parser.error("public structure metadata requires production or fresh-cachebuster-v1, metadata-only, percent-encoded-spaces and 600919.SH")
    if args.request_variant == _FRESH_QUERY_VARIANT and not args.public_structure_metadata:
        parser.error("fresh query requires explicit public structure metadata")
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("use Python -I -S -B")
    try:
        dates = [date.fromisoformat(value) for value in (args.start, args.end)]
        if [value.isoformat() for value in dates] != [args.start, args.end] or dates[0] > dates[1]:
            raise ValueError
    except ValueError:
        parser.error("invalid date window; use ordered YYYY-MM-DD dates")
    root = args.output.resolve()
    if root.exists() or root.is_relative_to(SOURCE):
        parser.error("fresh isolated directory outside source required")
    root.mkdir()
    # Copy only the two named retained public diagnostic files before the worker
    # guard. Never read arbitrary source directories/configuration or credentials.
    captured = None
    if args.replay:
        def read_input(name):
            try:
                return _read_replay_input(args.replay.absolute(), name)
            except (OSError, ValueError):
                parser.error("invalid retained response file")

        receipt = read_input("capture.json")
        try:
            captured = json.loads(receipt)
        except (ValueError, UnicodeError):
            parser.error("invalid capture receipt")
        if not isinstance(captured, dict):
            parser.error("invalid capture receipt")
        if captured.get("contract") != "news-production-diagnostic-v4":
            parser.error("unsupported historical capture contract; preserve the original evidence")
        if captured.get("ticker") != args.ticker:
            parser.error("capture ticker does not match explicit diagnostic target")
        if captured.get("transport") != args.transport:
            parser.error("capture transport does not match explicit transport choice")
        if captured.get("body_saved") is not True:
            parser.error("capture has no retained original response bytes")
        body_name = "public-response.bin"
        for name, content in (("capture.json", receipt), (body_name, read_input(body_name))):
            (root / name).write_bytes(content)
    (root / "reservation.json").write_text(
        json.dumps(
            {
                "live": args.authorize_one_public_get_and_save or metadata_only,
                "acceptance_mode": args.acceptance_mode,
                "physical_request_limit": 1 if not args.replay else 0,
                "redirects": 0,
                "retries": 0,
                "ticker": args.ticker,
                "transport": args.transport,
                "symbol": args.ticker[:6],
                "window": {"start": args.start, "end": args.end},
                "page": 1,
                "page_size": 10,
                "endpoint": "https://search-api-web.eastmoney.com/search/jsonp",
                **({"request_variant": args.request_variant} if metadata_only else {}),
                **({"public_structure_metadata": True} if args.public_structure_metadata else {}),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    counts, blocked_network = install(root, SOURCE, offline=bool(args.replay))
    from tradingagents import news_diagnostic

    if args.replay:
        result = news_diagnostic.replay(root, start=args.start, end=args.end,
                                        ticker=args.ticker, transport=args.transport)
    elif metadata_only:
        result = _capture_direct(root, start=args.start, end=args.end, ticker=args.ticker,
                                 metadata_only=True, percent_encoded_spaces=args.percent_encoded_spaces,
                                 request_variant=args.request_variant, public_structure_metadata=args.public_structure_metadata)
    else:
        result = _capture_direct(root, start=args.start, end=args.end, ticker=args.ticker)
    result["guard_counts"] = counts
    result["blocked_network"] = blocked_network
    result["acceptance"] = _acceptance(result, args.acceptance_mode, captured)
    (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "ticker": result["ticker"], "transport": result["transport"], "window": result["window"],
        "request_count": result["request_count"], "http_status": result.get("http_status"),
        "callback_matches": result.get("callback_matches"),
        "stage": result["stage"], "body_saved": result["body_saved"],
        "capture_passed": result["capture_passed"],
        "acceptance_passed": result["acceptance_passed"], "checks": result["checks"],
        "projection_saved": result["projection_saved"],
        "projection_replay_passed": result["projection_replay_passed"],
        "replay_kind": result.get("replay_kind"),
        "selection": result["selection"], "data_gaps": result["data_gaps"],
        "acceptance": result["acceptance"],
        **({"metadata_passed": result["metadata_passed"], "byte_replay_available": False,
            "transport_metadata": result.get("transport_metadata")} if metadata_only else {}),
    }))
    return 0 if result["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
