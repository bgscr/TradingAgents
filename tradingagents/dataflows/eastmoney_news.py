"""Pure, conservative Eastmoney JSONP news boundary; no provider success claim.

The SDK establishes only ``result.cmsArticleWebOld`` as its article collection,
not a business-code contract. Our local policy accepts absent envelope status as
``not_supplied``. At root/result only, nonempty ``error``, boolean ``success=False``
or textual ``status=error/failed/failure`` are explicit failure signals. Any other
present ``code/status/success/error`` is unverified and unavailable, even code=0
alone. A closed, locally observed public-news envelope is separately recognized
by all of its status, schema, callback and public-field checks below. This is a
compatibility policy from user-pasted browser samples, not a supplier-wide rule.
These are conservative local rejection rules, not a verified supplier contract.
Unknown provider extensions are ignored; only selected article fields leave this
boundary. Article ``code`` is an article ID, never business status or stock identity.

Association requires the supplied mainland symbol or an authoritative caller-
supplied alias in title/content. Local query/keyword fields prove nothing. Dates
are inclusive Shanghai calendar dates (naive provider timestamps mean Shanghai).
One search page cannot establish historical completeness: coverage stays unknown.
Synthetic parser tests cannot qualify unretained responses or real supplier data.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

MAX_BODY_BYTES = 1024 * 1024
_WS = " \t\r\n\f\v"
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_RESERVED = frozenset(
    ["await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else", "enum", "export", "extends", "false", "finally", "for", "function", "if", "implements", "import", "in", "instanceof", "interface", "let", "new", "null", "package", "private", "protected", "public", "return", "static", "super", "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield"]
)
_SHANGHAI = timezone(timedelta(hours=8))
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?"
)
SENSITIVE_TEXT = re.compile(
    r"(?i)(?:https?://|\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|secret)\b"
    r"[\"\x27]?\s*[=:]|\bbearer\s+|\bsk-[A-Za-z0-9_-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_OBSERVED_FIELDS = frozenset({'bizCode', 'bizMsg', 'code', 'extra', 'hitsTotal', 'msg', 'result', 'searchId'})
_OBSERVED_ARTICLE_FIELDS = frozenset({'date', 'image', 'code', 'title', 'content', 'mediaName', 'url'})
ARTICLE_SOURCE_ISSUES = frozenset({
    'non_string', 'empty', 'length_limit', 'unsafe_characters', 'url_format',
    'scheme_not_allowed', 'credentials_present', 'host_not_allowed',
    'query_or_fragment', 'path_format', 'article_id_mismatch',
    'https_not_in_current_policy', 'representation_mismatch',
})


def news_query(symbol, callback, cache_buster):
    """One public query shape, shared by automatic and manual acquisition."""
    if (not isinstance(symbol, str) or re.fullmatch(r'[0-9]{6}', symbol) is None
            or not _name_ok(callback) or len(callback) > 96
            or not isinstance(cache_buster, str) or re.fullmatch(r'[0-9]{13}', cache_buster) is None):
        raise NewsResponseError('request_invalid')
    parameters = {
        'uid': '', 'keyword': symbol, 'type': ['cmsArticleWebOld'],
        'client': 'web', 'clientType': 'web', 'clientVersion': 'curr',
        'param': {'cmsArticleWebOld': {
            'searchScope': 'default', 'sort': 'default', 'pageIndex': 1,
            'pageSize': 10, 'preTag': '<em>', 'postTag': '</em>',
        }},
    }
    return {'cb': callback, 'param': json.dumps(parameters, ensure_ascii=False), '_': cache_buster}


def public_news_payload(body, callback):
    """Existing conservative retention policy; no raw bytes are persisted here.

    This is separate from business/date/association admission. Unknown metadata
    or sensitive article text is not eligible for downstream evidence retention.
    """
    try:
        value, matches = _decode(body, callback)
    except NewsResponseError as error:
        return error.reason, None, None, None
    result = value.get('result')
    if not isinstance(result, dict):
        return 'invalid_envelope', None, value, matches
    if 'cmsArticleWebOld' not in result:
        return 'articles_missing', None, value, matches
    rows = result['cmsArticleWebOld']
    if rows is None:
        return 'articles_null', None, value, matches
    if not isinstance(rows, list):
        return 'articles_not_list', None, value, matches
    if observed_public_news_envelope(value, callback_matches=matches):
        return None, rows, value, matches
    counts = {'code', 'hits', 'total', 'count'}
    if set(value) - {'result', 'success'} - counts:
        return 'unknown_envelope_fields', rows, value, matches
    if set(result) - {'cmsArticleWebOld'} - counts:
        return 'unknown_result_fields', rows, value, matches
    for obj in (value, result):
        if any(type(obj[key]) is not int or not 0 <= obj[key] <= 2**53 for key in counts & obj.keys()):
            return 'invalid_public_metadata', rows, value, matches
    if 'success' in value and value['success'] is not True:
        return 'unsuccessful_envelope', rows, value, matches
    if len(rows) > 10:
        return 'article_limit', rows, value, matches
    fields = {'date', 'mediaName', 'code', 'title', 'content', 'image'}
    for row in rows:
        if (not isinstance(row, dict)
                or not {'date', 'mediaName', 'code', 'title', 'content'} <= set(row) <= fields):
            return 'unknown_or_missing_article_fields', rows, value, matches
        if any(not isinstance(item, str) for item in row.values()):
            return 'non_string_article_field', rows, value, matches
        if any(SENSITIVE_TEXT.search(item) for item in row.values()):
            return 'sensitive_article_text', rows, value, matches
    return None, rows, value, matches


def observed_public_news_envelope(payload, *, callback_matches):
    """Closed local compatibility shape observed in browser pastes, 2026-11-13.

    No HTTP/status-code-only success claim. Unknown extensions remain rejected;
    public URLs must match the same article ID and one observed publisher host.
    This validates the whole page before any date/stock selection, so excluded
    rows cannot hide unsafe extensions. Neither hitsTotal nor a UUID proves PIT
    availability, source authenticity, or complete historical coverage.
    """
    if (callback_matches is not True or type(payload) is not dict or set(payload) != _OBSERVED_FIELDS
            or type(payload['code']) is not int or payload['code'] != 0
            or payload['msg'] != 'OK' or payload['bizCode'] != '' or payload['bizMsg'] != ''
            or type(payload['extra']) is not dict or payload['extra']
            or type(payload['hitsTotal']) is not int or not 0 <= payload['hitsTotal'] <= 2**53
            or not isinstance(payload['searchId'], str)
            or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                            payload['searchId']) is None):
        return False
    result = payload['result']
    if type(result) is not dict or set(result) != {'cmsArticleWebOld'}:
        return False
    rows = result['cmsArticleWebOld']
    if type(rows) is not list or not 1 <= len(rows) <= 10 or payload['hitsTotal'] < len(rows):
        return False
    for row in rows:
        if (type(row) is not dict or set(row) != _OBSERVED_ARTICLE_FIELDS
                or any(type(value) is not str for value in row.values())
                or row['image'] != '' or re.fullmatch(r'[0-9]{18}', row['code']) is None
                or not row['title'].strip() or not row['mediaName'].strip()
                or re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', row['date']) is None
                or _publication(row['date']) is None
                or any(SENSITIVE_TEXT.search(value) for key, value in row.items() if key != 'url')
                or row['url'] not in {
                    f'http://{host}.eastmoney.com/a/{row["code"]}.html'
                    for host in ('stock', 'finance', 'bank')}):
            return False
    return True


@dataclass(frozen=True)
class NewsPage:
    rows: tuple[dict[str, str], ...]
    business_status: str
    callback_matches: bool
    excluded_counts: dict[str, int]
    coverage_status: str = "unknown"
    source_selection: "NewsSelectionCounts | None" = None


@dataclass(frozen=True)
class NewsSelectionCounts:
    """Completed, mutually exclusive single-page processing counts, not facts."""
    returned: int
    source_rejected: int
    malformed: int
    undated: int
    outside_window: int
    wrong_stock: int
    duplicate: int
    accepted: int

    def __post_init__(self):
        values = self.as_dict()
        if (any(type(v) is not int or not 0 <= v <= 10 for v in values.values())
                or self.returned != sum(v for k, v in values.items() if k != 'returned')):
            raise ValueError('invalid_news_selection_counts')

    def as_dict(self):
        return {key: getattr(self, key) for key in ('returned', 'source_rejected', 'malformed',
                'undated', 'outside_window', 'wrong_stock', 'duplicate', 'accepted')}


class NewsResponseError(ValueError):
    """Fixed safe reason and optional counts; never provider text or callback."""

    def __init__(self, reason: str, *, excluded_counts: dict[str, int] | None = None,
                 source_issue: str | None = None, source_selection: NewsSelectionCounts | None = None):
        super().__init__(reason)
        self.reason = reason
        self.excluded_counts = dict(excluded_counts or {})
        self.source_issue = (source_issue if reason == 'article_source_invalid'
                             and type(source_issue) is str and source_issue in ARTICLE_SOURCE_ISSUES else None)
        self.source_selection = source_selection if type(source_selection) is NewsSelectionCounts else None


def _name_ok(value):
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and value not in _RESERVED


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NewsResponseError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise NewsResponseError("nonfinite_json_number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise NewsResponseError("nonfinite_json_number")
    return number


def _decode(body, expected_callback):
    if not isinstance(body, bytes):
        raise NewsResponseError("body_type")
    if len(body) > MAX_BODY_BYTES:
        raise NewsResponseError("body_limit")
    if not _name_ok(expected_callback):
        raise NewsResponseError("expected_callback_invalid")
    try:
        text = body.decode("utf-8-sig").strip(_WS)
    except UnicodeError:
        raise NewsResponseError("encoding_invalid") from None
    match = _NAME.match(text)
    if match is None or not _name_ok(match.group()):
        raise NewsResponseError("callback_invalid")
    tail = text[match.end():].lstrip(_WS)
    if not tail.startswith("("):
        raise NewsResponseError("wrapper_invalid")
    inner = tail[1:].lstrip(_WS)
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    try:
        payload, end = decoder.raw_decode(inner)
    except NewsResponseError:
        raise
    except (ValueError, RecursionError):
        raise NewsResponseError("json_invalid") from None
    if re.fullmatch(r"\)[ \t\r\n\f\v]*;?", inner[end:].strip(_WS)) is None:
        raise NewsResponseError("trailing_syntax")
    if not isinstance(payload, dict):
        raise NewsResponseError("root_not_object")
    return payload, match.group() == expected_callback


def _known_article_fields(payload):
    """Project the observed profile for parsing only; retention uses original bytes."""
    signals = {"code", "status", "success", "error", "msg", "bizCode", "bizMsg",
               "hitsTotal", "searchId", "hits", "total", "count"}
    result = payload.get("result")
    if (type(result) is not dict or signals.intersection(result)
            or (signals - _OBSERVED_FIELDS).intersection(payload)):
        return payload
    rows = result.get("cmsArticleWebOld")
    if type(rows) is not list or len(rows) > 10 or any(type(row) is not dict for row in rows):
        return payload
    if any((signals - _OBSERVED_ARTICLE_FIELDS).intersection(row) for row in rows):
        return payload
    projected = {k: payload[k] for k in _OBSERVED_FIELDS if k in payload}
    projected["result"] = {"cmsArticleWebOld": [
        {k: row[k] for k in _OBSERVED_ARTICLE_FIELDS if k in row} for row in rows
    ]}
    return projected


def parse_transport_news_page(body, *, expected_callback, ticker, start_date, end_date, instrument_aliases=()):
    """Only the fixed Eastmoney HTTP adapter uses the extension-compatible profile."""
    payload, matches = _decode(body, expected_callback)
    if matches:
        core = _known_article_fields(payload)
        if core != payload and observed_public_news_envelope(core, callback_matches=True):
            wire = json.dumps(core, ensure_ascii=False, separators=(",", ":"))
            try:
                body = (expected_callback + "(" + wire + ")").encode("utf-8")
            except UnicodeEncodeError:
                raise NewsResponseError("encoding_invalid") from None
    return parse_news_page(body, expected_callback=expected_callback, ticker=ticker,
                           start_date=start_date, end_date=end_date, instrument_aliases=instrument_aliases)


def _business_status(payload, result, *, callback_matches=False):
    from tradingagents.run_diagnostics import record_news_business_status

    record_news_business_status(payload, result)
    unverified = False
    for obj in (payload, result):
        if not isinstance(obj, dict):
            continue
        if obj.get("success") is False or ("error" in obj and bool(obj["error"])):
            raise NewsResponseError("business_failure")
        status = obj.get("status")
        if isinstance(status, str) and status.strip().lower() in {"error", "failed", "failure"}:
            raise NewsResponseError("business_failure")
        unverified |= bool({"code", "status", "success", "error"}.intersection(obj))
    if observed_public_news_envelope(payload, callback_matches=callback_matches):
        return "observed_public_news_envelope"
    if unverified or {'bizCode', 'bizMsg', 'hitsTotal', 'searchId'}.intersection(payload):
        raise NewsResponseError("business_status_unverified")
    return "not_supplied"


def _article_source_issue(value, code):
    """Describe the first source failure without returning any provider value.

    Exact membership remains the ONLY acceptance rule, including for legacy IDs.
    Subsequent lexical checks only classify a refusal; they never normalize a URL,
    trust a new host/protocol, inspect a remote resource or execute provider text.
    """
    if type(value) is not str:
        return 'non_string'
    if value in {f'http://{host}.eastmoney.com/a/{code}.html' for host in ('stock', 'finance', 'bank')}:
        return None
    if not value:
        return 'empty'
    if len(value) > 2048:
        return 'length_limit'
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return 'unsafe_characters'
    parts = re.fullmatch(r'([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)(.*)', value)
    if parts is None:
        return 'url_format'
    scheme, authority, path, suffix = parts.groups()
    if scheme not in {'http', 'https'}:
        return 'scheme_not_allowed'
    if '@' in authority:
        return 'credentials_present'
    if authority not in {'stock.eastmoney.com', 'finance.eastmoney.com', 'bank.eastmoney.com'}:
        return 'host_not_allowed'
    if suffix:
        return 'query_or_fragment'
    article_path = re.fullmatch(r'/a/([A-Za-z0-9_-]+)\.html', path)
    if article_path is None:
        return 'path_format'
    if article_path.group(1) != code:
        return 'article_id_mismatch'
    if scheme == 'https':
        return 'https_not_in_current_policy'
    return 'representation_mismatch'


def project_news_payload(body, expected_callback):
    """Existing strict three-value API; optional isolation never changes it."""
    return _project_news_payload(body, expected_callback)[:3]


def _sensitive_source_value(value):
    # The response has already passed depth/node bounds. Inspect only to refuse;
    # never normalize, return or persist URL values, including malformed objects.
    secret = re.compile(SENSITIVE_TEXT.pattern.replace('https?://|', '', 1))
    pending = [value]
    secret_keys = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
                   'token', 'password', 'secret', 'api_key', 'access_token', 'refresh_token'}
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if (secret.search(item) or secret.search(_text(item))
                    or any(re.search(r'//[^/?#]*@', text)
                           for text in (item, _text(item)))):
                return True
        elif isinstance(item, dict):
            if any(key.lower() in secret_keys for key in item):
                return True
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _project_news_payload(body, expected_callback, *, isolate_invalid_sources=False):
    """Validate core protocol, returning ONLY an allowlisted in-memory projection.

    This is not permission to retain the original body. The older diagnostic
    public_news_payload policy deliberately remains unchanged for raw retention.
    Ignored metadata (including article image/extra fields) never reaches callers,
    diagnostics or hashes. Known business signals cannot be dropped as extensions.
    """
    payload, matches = _decode(body, expected_callback)
    if not matches:
        raise NewsResponseError('callback_mismatch')
    pending = [(payload, 0)]
    seen = 0
    while pending:
        value, depth = pending.pop()
        seen += 1
        if depth > 32 or seen > 4096:
            raise NewsResponseError('structure_limit')
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    result = payload.get('result')
    if not isinstance(result, dict):
        raise NewsResponseError('invalid_envelope')
    if 'cmsArticleWebOld' not in result:
        raise NewsResponseError('articles_missing')
    articles = result['cmsArticleWebOld']
    if articles is None:
        raise NewsResponseError('articles_null')
    if not isinstance(articles, list):
        raise NewsResponseError('articles_not_list')
    if len(articles) > 10:
        raise NewsResponseError('article_limit')
    signals = {'code', 'msg', 'bizCode', 'bizMsg', 'status', 'success', 'error'}
    for obj in (payload, result):
        if (obj.get('success') is False or ('error' in obj and bool(obj['error']))
                or isinstance(obj.get('status'), str)
                and obj['status'].strip().lower() in {'error', 'failed', 'failure'}):
            raise NewsResponseError('business_failure')
    # No new meanings for nested statuses or generic success/error fields.
    if signals.intersection(result) or {'status', 'success', 'error'}.intersection(payload):
        raise NewsResponseError('business_status_unverified')
    if signals.intersection(payload):
        if (type(payload.get('code')) is not int or payload['code'] != 0
                or payload.get('msg') != 'OK'
                or any(key in payload and payload[key] != '' for key in ('bizCode', 'bizMsg'))):
            raise NewsResponseError('business_status_unverified')
        business = 'projected_public_news_envelope'
    else:
        business = 'not_supplied'  # Existing status-absent compatibility, not success.
    if isolate_invalid_sources and business != 'projected_public_news_envelope':
        raise NewsResponseError('business_status_unverified')
    rows = []
    source_rejected = 0
    for article in articles:
        if not isinstance(article, dict) or not {'date', 'code', 'title'} <= set(article):
            raise NewsResponseError('article_core_missing')
        row = {key: article[key] for key in ('date', 'code', 'title', 'content', 'mediaName') if key in article}
        if any(type(value) is not str for value in row.values()):
            raise NewsResponseError('non_string_article_field')
        code = row['code']
        code_pattern = r'[0-9]{18}' if business == 'projected_public_news_envelope' else r'[A-Za-z0-9_-]+'
        if re.fullmatch(code_pattern, code) is None or not row['title'].strip():
            raise NewsResponseError('article_core_invalid')
        if _publication(row['date']) is None:
            raise NewsResponseError('invalid_article_date')
        if any(SENSITIVE_TEXT.search(value) or SENSITIVE_TEXT.search(_text(value)) for value in row.values()):
            raise NewsResponseError('sensitive_article_text')
        if isolate_invalid_sources and 'url' not in article:
            source_rejected += 1
            continue  # This explicit mode retains only independently matched links.
        # Optional URL is never trusted or returned, but a supplied conflicting
        # article source still fails. Missing URL uses the existing canonical ID link.
        if 'url' in article:
            if isolate_invalid_sources and _sensitive_source_value(article['url']):
                raise NewsResponseError('sensitive_article_source')
            source_issue = _article_source_issue(article['url'], code)
            if source_issue is not None:
                if not isolate_invalid_sources:
                    raise NewsResponseError('article_source_invalid', source_issue=source_issue)
                source_rejected += 1
                continue  # Drop the entire article; no text/URL survives projection.
        rows.append(row)
    projected = {'result': {'cmsArticleWebOld': rows}}
    if business == 'projected_public_news_envelope':
        projected.update(code=0, msg='OK')  # Values verified above, never inferred.
    return projected, matches, business, (len(articles), source_rejected)


def _request(ticker, start_date, end_date, aliases):
    # A caller-resolved symbol is required; never infer identity from article IDs.
    if not isinstance(ticker, str):
        raise NewsResponseError("request_invalid")
    match = re.fullmatch(r"([0-9]{6})(?:\.(?:SH|SS|SZ))?", ticker)
    if match is None or not isinstance(aliases, tuple):
        raise NewsResponseError("request_invalid")
    if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
        raise NewsResponseError("request_invalid")
    try:
        if any(not isinstance(value, str) or not _DATE.fullmatch(value) for value in (start_date, end_date)):
            raise ValueError
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end:
            raise ValueError
    except ValueError:
        raise NewsResponseError("request_invalid") from None
    return match.group(1), start, end


def _publication(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (_DATE.fullmatch(value) or _TIMESTAMP.fullmatch(value)):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=_SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(_SHANGHAI)
    except (ValueError, OverflowError):
        return None


class _NewsText(HTMLParser):
    """Read text only: attributes/comments/script/style cannot supply identity."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = tag

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(data)


def _text(value):
    parser = _NewsText()
    parser.feed(unescape(value))
    parser.close()
    return "".join(parser.parts).replace("\u3000", "").replace("\r\n", " ").strip()


def _associated(text, symbol, aliases):
    for token in (symbol, *aliases):
        token = token.strip()
        # Avoid a numeric ticker or Latin alias matching a longer identifier.
        left = r"(?<![A-Za-z0-9_])" if token[0].isascii() and token[0].isalnum() else ""
        right = r"(?![A-Za-z0-9_])" if token[-1].isascii() and token[-1].isalnum() else ""
        if re.search(left + re.escape(token) + right, text, flags=re.IGNORECASE):
            return True
    return False


def parse_news_page(
    body: bytes, *, expected_callback: str, ticker: str,
    start_date: str, end_date: str, instrument_aliases: tuple[str, ...] = (),
    project_only: bool = False, isolate_invalid_sources: bool = False,
) -> NewsPage:
    """Return selected associated articles, or a fixed unavailable reason.

    Exclusion counts are mutually exclusive in validation order: malformed row,
    undated row, outside the requested window, then wrong stock. Missing optional
    mediaName/content become empty strings; present nonstrings are malformed.
    Title and article ID must be nonempty strings. Returned dates are normalized
    Shanghai timestamps; URLs are formed only from safe article IDs.
    """
    from tradingagents.run_diagnostics import record_news_response_shape, record_news_selection

    symbol, start, end = _request(ticker, start_date, end_date, instrument_aliases)
    if type(isolate_invalid_sources) is not bool or (isolate_invalid_sources and not project_only):
        raise NewsResponseError('invalid_source_isolation_mode')
    if project_only:
        payload, callback_matches, business_status, source_counts = _project_news_payload(
            body, expected_callback, isolate_invalid_sources=isolate_invalid_sources)
    else:
        payload, callback_matches = _decode(body, expected_callback)
    shape_payload = ({'result': {'cmsArticleWebOld': [None] * source_counts[0]}}
                     if isolate_invalid_sources else payload)
    record_news_response_shape(shape_payload, callback_matches=callback_matches)
    result = payload.get("result")
    if not project_only:
        business_status = _business_status(payload, result, callback_matches=callback_matches)
    if not isinstance(result, dict) or not isinstance(result.get("cmsArticleWebOld"), list):
        raise NewsResponseError("schema_invalid")
    counts = {"malformed": 0, "undated": 0, "wrong_stock": 0, "outside_window": 0}
    rows = []
    seen_rows = {}
    selected_codes = set()
    duplicate_count = 0
    for item in result["cmsArticleWebOld"]:
        if (not isinstance(item, dict)
                or any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("title", "code"))
                or any(not isinstance(item.get(key, ""), str) for key in ("content", "mediaName"))):
            counts["malformed"] += 1
            continue
        title, content = _text(item["title"]), _text(item.get("content", ""))
        code = item["code"].strip()
        if not title or re.fullmatch(r"[A-Za-z0-9_-]+", code) is None:
            counts["malformed"] += 1
            continue
        published = _publication(item.get("date"))
        if published is None:
            counts["undated"] += 1
            continue
        row = {"date": published.strftime("%Y-%m-%d %H:%M:%S"),
               "mediaName": _text(item.get("mediaName", "")), "code": code,
               "title": title, "content": content,
               "url": f"https://finance.eastmoney.com/a/{code}.html"}
        if isolate_invalid_sources:
            if code in seen_rows and seen_rows[code] != row:
                raise NewsResponseError('conflicting_article_duplicate')
            seen_rows[code] = row
        if not start <= published.date() <= end:
            counts["outside_window"] += 1
            continue
        if not _associated(title + "\n" + content, symbol, instrument_aliases):
            counts["wrong_stock"] += 1
            continue
        if isolate_invalid_sources and code in selected_codes:
            duplicate_count += 1
            continue
        selected_codes.add(code)
        rows.append(row)
    source_selection = (NewsSelectionCounts(returned=source_counts[0], source_rejected=source_counts[1],
                        duplicate=duplicate_count, accepted=len(rows), **counts)
                        if isolate_invalid_sources else None)
    record_news_selection(selected_count=len(rows), excluded_counts=counts)
    if not rows:
        raise NewsResponseError("no_usable_articles", excluded_counts=counts, source_selection=source_selection)
    return NewsPage(tuple(rows), business_status, callback_matches, counts, source_selection=source_selection)
