"""Entirely synthetic PDFs and labels; no issuer material or network requests."""

import argparse
import hashlib
import importlib.util
import json
import os
import zlib
from pathlib import Path

import pytest

from tradingagents import local_announcement as doc

ROOT = Path(__file__).absolute().parents[1]
spec = importlib.util.spec_from_file_location("announcement_cli", ROOT / "scripts/prepare_company_announcement.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def synthetic_pdf(text=None, pages=1, extra="", stream_extra="", extra_objects=(), stream_bytes=None):
    """Independent, minimal ASCII PDF with invented text, no external generator."""
    if text is None:
        text = "SYNTHETIC TEST DOCUMENT - NOT AN ISSUER ANNOUNCEMENT\nSecurity code: 600919.SH\nAnnouncement date: 2026-11-13\nInvented example only."
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in text.splitlines()]
    stream = ("BT /F1 10 Tf 40 760 Td " + " 0 -16 Td ".join(f"({line}) Tj" for line in escaped) + " ET").encode("ascii")
    if stream_bytes is not None:
        stream = stream_bytes
    objects = [f"<< /Type /Catalog /Pages 2 0 R {extra} >>".encode(),
               f"<< /Type /Pages /Kids [{' '.join(f'{5+i} 0 R' for i in range(pages))}] /Count {pages} >>".encode(),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               f"<< /Length {len(stream)} {stream_extra} >>\nstream\n".encode() + stream + b"\nendstream"]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"] * pages
    objects += list(extra_objects)
    raw, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(raw)
    raw += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    raw += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return raw + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def claims(**changes):
    values = {"ticker": "600919.SH", "announcement_date": "2026-11-13", "as_of": "2026-11-13",
              "source_url": "https://www.sse.com.cn/synthetic-not-a-real-announcement.pdf"}
    values.update(changes)
    return doc.declaration(**values)


def test_actual_parser_and_plain_text_report():
    raw = synthetic_pdf()
    report = doc.prepare(raw, claims())
    assert report["status"] == doc.STATUS
    assert report["observed"]["input_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["observed"]["stock_labels"][0]["page"] == 1
    assert report["observed"]["date_labels"][0]["date"] == "2026-11-13"
    for field in ("source_authenticated", "historical_availability_verified", "news_eligible", "decision_eligible", "model_used"):
        assert report[field] is False
    rendered = doc.render(report).decode()
    assert "来源未经核验" in rendered and "SYNTHETIC TEST DOCUMENT" in rendered
    assert "第 1 页" in rendered and "不改变评分或最终评级" in rendered
    assert json.loads(doc.json_bytes(report)) == report


@pytest.mark.parametrize("text,code", [
    ("Announcement date: 2026-11-13", "stock_label_missing"),
    ("Security code: 600919.SH", "date_label_missing"),
    ("Security code: 600919.SH\nSecurity code: 600895.SH\nAnnouncement date: 2026-11-13", "stock_conflict"),
    ("Security code: 600919.SZ\nAnnouncement date: 2026-11-13", "stock_conflict"),
    ("Security code: 600919.SH;600895.SH\nAnnouncement date: 2026-11-13", "stock_label_ambiguous"),
    ("Security code: 600919.SH\nAnnouncement date: 2026-11-14", "date_conflict"),
    ("Security code: 600919.SH\nAnnouncement date: 2026-11-13\nAnnouncement date: 2026-11-12", "date_conflict"),
    ("Security code: 600919.SH\nAnnouncement date: 2026-02-30", "date"),
    ("Security code: 600919.SH\nAnnouncement date: 13/11/2026", "date_label_ambiguous"),
    ("Security code: 600919.SH\nReporting period: 2026-11-13", "date_label_missing"),
    ("Security code: 600919.SH\nAnnouncement date: 2026-11-13\ntoken=synthetic-secret-marker", "pdf_sensitive_marker"),
])
def test_binding_fail_closed_with_real_pdf_parser(text, code):
    with pytest.raises(doc.AnnouncementError, match=f"^{code}$"):
        doc.prepare(synthetic_pdf(text), claims())


def test_chinese_labels_and_exchange_remains_declared():
    report = doc._supplement(b"synthetic bytes", ["证券代码：600919\n公告日期：2026年11月13日\n合成测试"], claims())
    assert report["observed"]["stock_labels"][0]["exchange_in_text"] is False
    assert report["source_authenticated"] is False


@pytest.mark.parametrize("change", [
    {"announcement_date": "2026-11-14"}, {"as_of": "bad"}, {"ticker": "600919.SH\r\nInjected"},
    {"announcement_date": "2099-01-01", "as_of": "2099-01-01"},
    {"ticker": "600919"}, {"source_url": "http://www.sse.com.cn/a.pdf"},
    {"source_url": "https://www.sse.com.cn.evil.invalid/a.pdf"},
    {"source_url": (('https://' + 'fixture-user' + ':' + 'fixture-pass' + '@www.sse.com.cn/a.pdf'))},
    {"source_url": "https://www.sse.com.cn/a.pdf?token=secret"},
    {"source_url": "file:///C:/secret.pdf"}, {"source_url": "https://www.sse.com.cn/../a.pdf"},
])
def test_declaration_rejections(change):
    with pytest.raises(doc.AnnouncementError):
        claims(**change)


@pytest.mark.parametrize("name", ["../x.pdf", "D:\\secret.pdf", "a.pdf:secret", "x.txt", "CON.pdf", "LPT1.pdf", "a.pdf ", "a\nb.pdf"])
def test_filename_boundary(name):
    with pytest.raises(doc.AnnouncementError):
        doc.input_name(name)


@pytest.mark.parametrize("extra", ["/OpenAction << /S /JavaScript /JS (x) >>", "/AcroForm << >>", "/Names << /EmbeddedFiles [] >>", "/Open#41ction << >>", "/URI (https://example.invalid)", "/Example << /Subtype /Link >>"])
def test_active_and_external_pdf_rejected(extra):
    with pytest.raises(doc.AnnouncementError, match="pdf_active_or_external"):
        doc.extract_pdf(synthetic_pdf(extra=extra))


@pytest.mark.parametrize("stream_extra", ["/Filter /JBIG2Decode", "/Filter /DCTDecode", "/F (C:/secret)"])
def test_unsupported_decoding_never_executed(stream_extra):
    with pytest.raises(doc.AnnouncementError, match="pdf_filter|pdf_active_or_external"):
        doc.extract_pdf(synthetic_pdf(stream_extra=stream_extra))


def test_indirect_activity_name_rejected():
    raw = synthetic_pdf(extra="/Example << /Subtype 6 0 R >>", extra_objects=[b"/Movie"])
    with pytest.raises(doc.AnnouncementError, match="pdf_active_or_external"):
        doc.extract_pdf(raw)


def xref_pdf(*, object_stream=False):
    """Synthetic PDF exercising decoder entry before the post-parse graph walk."""
    raw = b"%PDF-1.7\n"
    if not object_stream:
        start = len(raw)
        raw += b"1 0 obj\n<< /Type /XRef /Size 1 /W [1 4 2] /Length 1 /Filter /JBIG2Decode >>\nstream\nx\nendstream\nendobj\n"
    else:
        offsets = {}
        objects = {2: b"<< /Type /Pages /Count 0 /Kids [] >>",
                   3: b"<< /Type /ObjStm /N 1 /First 4 /Length 1 /Filter /JBIG2Decode >>\nstream\nx\nendstream"}
        for number, value in objects.items():
            offsets[number] = len(raw)
            raw += f"{number} 0 obj\n".encode() + value + b"\nendobj\n"
        start = len(raw)
        rows = [(0, 0, 65535), (2, 3, 0), (1, offsets[2], 0), (1, offsets[3], 0), (1, start, 0)]
        stream = b"".join(bytes([kind]) + offset.to_bytes(4, "big") + generation.to_bytes(2, "big") for kind, offset, generation in rows)
        raw += f"4 0 obj\n<< /Type /XRef /Size 5 /W [1 4 2] /Root 1 0 R /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream\nendobj\n"
    return raw + f"startxref\n{start}\n%%EOF\n".encode()


@pytest.mark.parametrize("object_stream", [False, True])
def test_decoder_blocked_during_reader_bootstrap(monkeypatch, object_stream):
    from pypdf import filters

    calls = []
    checked = []
    original_check = doc._check_stream

    def checked_stream(stream):
        checked.append(stream.get("/Filter"))
        return original_check(stream)

    monkeypatch.setattr(doc, "_check_stream", checked_stream)
    monkeypatch.setattr(filters.JBIG2Decode, "decode", lambda *args: calls.append(args))
    original_decoder = filters.decode_stream_data
    # PdfReader wraps an XRef bootstrap ValueError; later object streams retain
    # our fixed category. Both must reach the guard, never the decoder.
    with pytest.raises(doc.AnnouncementError, match="^pdf_(filter|parse_rejected)$"):
        doc.extract_pdf(xref_pdf(object_stream=object_stream))
    assert calls == []
    assert "/JBIG2Decode" in checked
    assert filters.decode_stream_data is original_decoder


def test_capacity_parser_and_empty_boundaries():
    for raw, code in [(b"%PDF-" + b"x" * doc.MAX_BYTES, "pdf_size"),
                      (b"not a PDF", "pdf_type"), (b"%PDF-1.4\n%%EOF", "pdf_parse_rejected"),
                      (synthetic_pdf(pages=11), "pdf_pages"), (synthetic_pdf(""), "pdf_text_unavailable"),
                      (synthetic_pdf("x" * (doc.MAX_PAGE_TEXT + 1)), "pdf_text_limit")]:
        with pytest.raises(doc.AnnouncementError, match=f"^{code}$"):
            doc.extract_pdf(raw)


def test_compressed_expansion_and_total_text_limit():
    raw = synthetic_pdf(stream_bytes=zlib.compress(b"x" * (9 * 1024**2)), stream_extra="/Filter /FlateDecode")
    with pytest.raises(doc.AnnouncementError, match="pdf_parse_rejected"):
        doc.extract_pdf(raw)
    with pytest.raises(doc.AnnouncementError, match="pdf_text_limit"):
        doc.extract_pdf(synthetic_pdf("x" * 9000, pages=6))


def test_render_does_not_activate_document_markup():
    report = doc.prepare(synthetic_pdf("Security code: 600919.SH\nAnnouncement date: 2026-11-13\n</pre><script>bad()</script>"), claims())
    rendered = doc.render(report).decode()
    assert "&lt;/pre&gt;&lt;script&gt;" in rendered and "<script>" not in rendered


@pytest.mark.parametrize("event,args", [("socket.connect", ()), ("socket.getaddrinfo", ()), ("subprocess.Popen", ()), ("os.system", ()), ("open", (str(ROOT / ".env"), "r", 0)), ("open", ("C:/private/secret.pdf", "rb", 0)), ("os.mkdir", ("C:/outside/new",)), ("os.remove", ("anything",))])
def test_cli_audit_guard_denies_network_process_and_secrets(event, args):
    with pytest.raises(PermissionError):
        cli.audit_guard(event, args)


def test_windows_real_publication_and_input_immutability(tmp_path, monkeypatch, capsys):
    """Real pinned handles/CREATE_NEW/readback, synthetic input only, no bootstrap."""
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    base = tmp_path / ".scratch/user-announcements"
    monkeypatch.setattr(cli, "BASE", base)
    (base / "inbox").mkdir(parents=True)
    raw = synthetic_pdf()
    source = base / "inbox/synthetic.pdf"
    source.write_bytes(raw)
    args = argparse.Namespace(file="synthetic.pdf", **{key: value for key, value in claims().items() if key != "source_status"})
    assert cli.run(args) == 0
    output = next((base / "output").iterdir())
    ready = json.loads((output / "READY.json").read_bytes())
    for name, digest in ready["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert source.read_bytes() == raw
    assert "LOCAL_ANNOUNCEMENT_READY" in capsys.readouterr().out
    # All native handles released; reopening existing files for writing is possible.
    # No bytes are changed, and a second run creates a distinct result.
    with (output / "supplement.json").open("r+b"):
        pass
    assert cli.run(args) == 0
    assert len(list((base / "output").iterdir())) == 2


def test_invalid_pdf_creates_no_output(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    base = tmp_path / ".scratch/user-announcements"
    monkeypatch.setattr(cli, "BASE", base)
    (base / "inbox").mkdir(parents=True)
    (base / "inbox/synthetic.pdf").write_bytes(synthetic_pdf("no labelled fields"))
    args = argparse.Namespace(file="synthetic.pdf", **{key: value for key, value in claims().items() if key != "source_status"})
    with pytest.raises(doc.AnnouncementError, match="stock_label_missing"):
        cli.run(args)
    assert not (base / "output").exists()


def test_hardlinked_input_rejected_before_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    base = tmp_path / ".scratch/user-announcements"
    monkeypatch.setattr(cli, "BASE", base)
    (base / "inbox").mkdir(parents=True)
    source = base / "inbox/synthetic.pdf"
    source.write_bytes(synthetic_pdf())
    os.link(source, base / "inbox/alias.pdf")
    monkeypatch.setattr(doc, "prepare", lambda *_: pytest.fail("aliased input reached parser"))
    args = argparse.Namespace(file="synthetic.pdf", **{key: value for key, value in claims().items() if key != "source_status"})
    with pytest.raises(ValueError):
        cli.run(args)
    assert not (base / "output").exists()


def test_parse_failure_never_echoes_untrusted_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["cli", "--file", "synthetic.pdf", "--ticker", "600919.SH",
                                       "--announcement-date", "2026-11-13", "--as-of", "2026-11-13",
                                       "--source-url", claims()["source_url"]])
    monkeypatch.setattr(cli, "bootstrap", lambda: None)

    def fail(_):
        raise doc.AnnouncementError("syntheticsecretvalue")

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main() == 2
    out = capsys.readouterr()
    assert "syntheticsecretvalue" not in out.out + out.err
    assert "code=operation_rejected" in out.out
