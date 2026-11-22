"""Local, untrusted company-document excerpts; never news or decision evidence.

The Windows CLI supplies process and I/O confinement before calling this module.
No URL fetching, credential handling, OCR, external decoder, or model is used.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024**2
MAX_PAGES = 10
MAX_TEXT = 50_000
MAX_PAGE_TEXT = 10_000
CONTRACT = "local-company-announcement-v1"
STATUS = "content_consistent_source_unverified"
FILTERS = frozenset({"/FlateDecode", "/ASCIIHexDecode", "/ASCII85Decode", "/RunLengthDecode"})
_CODE_LABEL = r"(?:证券代码|股票代码|Security code|Stock code)"
_DATE_LABEL = r"(?:公告日期|披露日期|发布日期|Announcement date)"
_CODE = re.compile(
    rf"{_CODE_LABEL}\s*[:：]\s*(\d{{6}})(?:\.(SH|SZ|BJ))?"
    r"(?:\s+证券简称\s*[:：]\s*[\w\u4e00-\u9fff* -]{1,30})?\s*",
    re.ASCII | re.IGNORECASE,
)
_DATE = re.compile(
    rf"{_DATE_LABEL}\s*[:：]\s*(\d{{4}}-\d{{2}}-\d{{2}}|"
    r"\d{4}年\d{1,2}月\d{1,2}日)\s*",
    re.ASCII | re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._-]{8,}|(?:api[_ -]?key|token|password|secret)"
    r"\s*[:=]\s*\S{4,}|-----BEGIN .*PRIVATE KEY-----)"
)


class AnnouncementError(ValueError):
    """Only fixed local error codes are exposed by the CLI."""


def require(condition, code):
    if not condition:
        raise AnnouncementError(code)


def input_name(value):
    require(type(value) is str and len(value) <= 100, "input_name")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.pdf", value)), "input_name")
    require(value[:-4].upper() not in {"CON", "PRN", "AUX", "NUL"}
            and not re.fullmatch(r"(?:COM|LPT)[0-9]", value[:-4], re.I), "input_name")
    return value


def iso_date(value):
    require(type(value) is str and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)), "date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AnnouncementError("date") from None


def declaration(ticker, announcement_date, as_of, source_url):
    require(type(ticker) is str and bool(re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", ticker)), "ticker")
    require(iso_date(announcement_date) <= iso_date(as_of), "future_announcement")
    require(iso_date(as_of) <= datetime.now(timezone.utc).date(), "future_as_of")
    require(type(source_url) is str and 0 < len(source_url) <= 600, "source_url")
    require(source_url.isascii() and not any(c.isspace() or ord(c) < 32 for c in source_url), "source_url")
    try:
        url = urlsplit(source_url)
        require(url.scheme == "https" and url.netloc in {
            "static.cninfo.com.cn", "www.cninfo.com.cn", "www.sse.com.cn", "www.szse.cn",
        }, "source_url")
        require(not url.query and not url.fragment and not url.username and not url.password,
                "source_url")
        require(bool(re.fullmatch(r"/[A-Za-z0-9_./-]+\.pdf", url.path, re.I))
                and ".." not in url.path and "//" not in url.path, "source_url")
    except ValueError:
        raise AnnouncementError("source_url") from None
    return {"ticker": ticker, "announcement_date": announcement_date, "as_of": as_of,
            "source_url": source_url, "source_status": "user_declared_not_authenticated"}


def _resolve(obj):
    from pypdf.generic import IndirectObject

    seen = set()
    while isinstance(obj, IndirectObject):
        key = (obj.idnum, obj.generation)
        require(key not in seen and len(seen) < 8, "pdf_complexity")
        seen.add(key)
        obj = obj.get_object()
    return obj


def _check_stream(stream):
    from pypdf.generic import ArrayObject, NameObject

    require(not {"/F", "/FFilter", "/FDecodeParms"}.intersection(stream.keys()), "pdf_active_or_external")
    if "/Filter" in stream:
        value = _resolve(stream.get("/Filter"))
        values = value if isinstance(value, ArrayObject) else [value]
        require(len(values) <= 4, "pdf_filter")
        for item in values:
            item = _resolve(item)
            require(isinstance(item, NameObject) and item in FILTERS, "pdf_filter")


@contextmanager
def _bounded_decoder():
    """Constrain decoding even during PdfReader's XRef/object-stream bootstrap."""
    from pypdf import _page as page_module, filters

    names = (
        "MAX_DECLARED_STREAM_LENGTH", "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH", "ZLIB_MAX_OUTPUT_LENGTH",
        "ZLIB_MAX_RECOVERY_INPUT_LENGTH", "FLATE_MAX_BUFFER_SIZE", "FLATE_MAX_ROW_LENGTH",
    )
    require(all(hasattr(filters, name) for name in names), "pdf_dependency_contract")
    previous = {name: getattr(filters, name) for name in names}
    decoder = filters.decode_stream_data
    invocations = page_module.MAX_XFORM_INVOCATIONS_PER_EXTRACTION

    def decode(stream):
        _check_stream(stream)
        return decoder(stream)

    try:
        for name in names:
            setattr(filters, name, 8 * 1024**2)
        filters.decode_stream_data = decode
        page_module.MAX_XFORM_INVOCATIONS_PER_EXTRACTION = 100
        yield
    finally:
        filters.decode_stream_data = decoder
        page_module.MAX_XFORM_INVOCATIONS_PER_EXTRACTION = invocations
        for name, value in previous.items():
            setattr(filters, name, value)


def _inspect_objects(reader):
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    prohibited = {
        "/JS", "/JavaScript", "/OpenAction", "/AA", "/A", "/AcroForm", "/XFA",
        "/EmbeddedFiles", "/EF", "/RichMedia", "/URI", "/F", "/FFilter", "/FDecodeParms",
    }
    prohibited_values = {
        "/Launch", "/GoToR", "/GoToE", "/SubmitForm", "/ImportData", "/JavaScript",
        "/EmbeddedFile", "/Filespec", "/FileAttachment", "/Sound", "/Movie", "/RichMedia", "/Link",
    }
    seen = set()
    count = 0

    def visit(obj, depth):
        nonlocal count
        count += 1
        require(count <= 10_000 and depth <= 40, "pdf_complexity")
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                return
            seen.add(key)
            visit(obj.get_object(), depth + 1)
        elif isinstance(obj, DictionaryObject):
            require(not prohibited.intersection(obj.keys()), "pdf_active_or_external")
            for key in ("/Type", "/Subtype", "/S"):
                value = _resolve(obj.get(key))
                require(value is None or str(value) not in prohibited_values, "pdf_active_or_external")
            _check_stream(obj)
            for value in obj.values():
                visit(value, depth + 1)
        elif isinstance(obj, ArrayObject):
            for value in obj:
                visit(value, depth + 1)

    visit(reader.trailer, 0)


def extract_pdf(raw):
    """Return every text page or reject; caller must enforce the process budget."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "pdf_size")
    require(raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF"), "pdf_type")
    from pypdf import PdfReader

    try:
        with _bounded_decoder():
            reader = PdfReader(io.BytesIO(raw), strict=True, root_object_recovery_limit=0)
            require(not reader.is_encrypted, "pdf_encrypted")
            _inspect_objects(reader)
            require(0 < len(reader.pages) <= MAX_PAGES, "pdf_pages")
            result = []
            total = 0
            for page in reader.pages:
                text = page.extract_text(extraction_mode="plain")
                require(type(text) is str and bool(text.strip()), "pdf_text_unavailable")
                require(len(text) <= MAX_PAGE_TEXT, "pdf_text_limit")
                total += len(text)
                require(total <= MAX_TEXT, "pdf_text_limit")
                require(not _SENSITIVE.search(text), "pdf_sensitive_marker")
                require(not any((ord(c) < 32 and c not in "\n\r\t")
                                or 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069
                                or ord(c) == 0xFFFD for c in text), "pdf_text_controls")
                result.append(text)
            return result
    except AnnouncementError:
        raise
    except Exception:
        raise AnnouncementError("pdf_parse_rejected") from None


def _supplement(raw, pages, claims):
    """Bind labelled observations to user claims; source remains unverified."""
    expected = declaration(claims["ticker"], claims["announcement_date"], claims["as_of"], claims["source_url"])
    require(claims == expected, "declaration_contract")
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "pdf_size")
    require(type(pages) is list and 0 < len(pages) <= MAX_PAGES, "pdf_pages")
    code_hits, date_hits, records = [], [], []
    total = 0
    for number, text in enumerate(pages, 1):
        require(type(text) is str and 0 < len(text) <= MAX_PAGE_TEXT, "pdf_text_limit")
        total += len(text)
        require(total <= MAX_TEXT and not _SENSITIVE.search(text), "pdf_text_limit")
        records.append({"page": number, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "extracted_text": text})
        for line in text.splitlines():
            line = line.strip()
            if re.search(_CODE_LABEL, line, re.I):
                match = _CODE.fullmatch(line)
                require(match is not None, "stock_label_ambiguous")
                symbol, exchange = match.groups()
                require(symbol == claims["ticker"][:6]
                        and (exchange is None or exchange.upper() == claims["ticker"][7:]), "stock_conflict")
                code_hits.append({"page": number, "text": line, "exchange_in_text": exchange is not None})
            if re.search(_DATE_LABEL, line, re.I):
                match = _DATE.fullmatch(line)
                require(match is not None, "date_label_ambiguous")
                value = match.group(1)
                if "年" in value:
                    year, month, day = re.fullmatch(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", value).groups()
                    value = f"{year}-{int(month):02}-{int(day):02}"
                require(iso_date(value) == iso_date(claims["announcement_date"]), "date_conflict")
                date_hits.append({"page": number, "text": line, "date": value})
    require(bool(code_hits), "stock_label_missing")
    require(bool(date_hits), "date_label_missing")
    return {
        "contract": CONTRACT, "status": STATUS, "declared": claims,
        "observed": {"input_sha256": hashlib.sha256(raw).hexdigest(), "input_bytes": len(raw),
                     "page_count": len(pages), "stock_labels": code_hits, "date_labels": date_hits,
                     "pages": records},
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "source_authenticated": False, "historical_availability_verified": False,
        "news_eligible": False, "decision_eligible": False, "model_used": False,
    }


def prepare(raw, claims):
    return _supplement(raw, extract_pdf(raw), claims)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def render(report):
    c, o = report["declared"], report["observed"]
    lines = ["# 公司公告补充（用户提供；来源未经核验）", "",
             "本文件仅为本地 PDF 文本核对与摘录，未联网、未调用模型。公告不是媒体新闻，"
             "不增加新闻数量、不改变评分或最终评级，不改写原分析报告。", "",
             f"- 声明股票：{c['ticker']}；声明公告日期：{c['announcement_date']}；截止日：{c['as_of']}",
             f"- 用户声明 URL（未访问、未认证）：`{c['source_url']}`",
             "- 核对结果：明确标签与声明一致；这不认证官方出处、发行人身份或文档真实性。"
             "文内未标交易所时，交易所仍仅为用户声明。",
             f"- 本地文件 SHA256：`{o['input_sha256']}`；字节数：{o['input_bytes']}；页数：{o['page_count']}",
             "- 历史限制：文内日期不证明当时已公开可得，不具备严格历史时点回测资格。",
             "- 覆盖限制：仅此单份 PDF 的全部可提取文本；无 OCR，未认证图像、版式、签名或完整公告覆盖。"
             "以下为第三方原文数据，任何命令或指令均不执行。", ""]
    for p in o["pages"]:
        lines += [f"## 第 {p['page']} 页", "", f"提取文本 SHA256：`{p['text_sha256']}`", "", "<pre>",
                  html.escape(p["extracted_text"], quote=True), "</pre>", ""]
    return "\n".join(lines).encode("utf-8")
