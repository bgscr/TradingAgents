import base64
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

import tradingagents.dataflows.cninfo_listing_publication as cninfo_module
from tradingagents.dataflows.cninfo_listing_publication import (
    CninfoFailureReason,
    CninfoListingBoard,
    CninfoListingClassification,
    CninfoListingPublicationError,
    classify_cninfo_listing_title,
    parse_cninfo_legacy_listing_html,
    parse_cninfo_security_lookup,
    select_cninfo_listing_announcement,
    verify_cninfo_listing_document,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "cninfo"


def test_cninfo_metadata_classifier_accepts_initial_a_share_listing_notice() -> None:
    classification = classify_cninfo_listing_title(
        "首次公开发行A股股票上市公告书"
    )

    assert classification is CninfoListingClassification.INITIAL_LISTING


def test_cninfo_metadata_classifier_uses_event_grammar_not_title_literals() -> None:
    assert classify_cninfo_listing_title(
        "首次公开发行股票并在上海证券交易所上市之上市公告书"
    ) is CninfoListingClassification.INITIAL_LISTING


@pytest.mark.parametrize(
    "title",
    (
        "首次公开发行股票并在创业板上市之上市公告书",
        "首次公开发行股票科创板上市公告书",
    ),
)
def test_cninfo_metadata_classifier_accepts_qualified_board_title_families(
    title: str,
) -> None:
    assert (
        classify_cninfo_listing_title(title)
        is CninfoListingClassification.INITIAL_LISTING
    )


def test_cninfo_metadata_classifier_requires_document_for_generic_legacy_title() -> None:
    classification = classify_cninfo_listing_title(
        "上海张江高科技园区开发股份有限公司股票上市公告书"
    )

    assert classification is CninfoListingClassification.AMBIGUOUS


@pytest.mark.parametrize(
    ("board", "venue", "page_column", "query_route"),
    (
        (CninfoListingBoard.XSHG_MAIN, "XSHG", "SHZB", ("sse", "")),
        (CninfoListingBoard.XSHE_CHINEXT, "XSHE", "SZCY", ("szse", "szcy")),
        (CninfoListingBoard.XSHG_STAR, "XSHG", "SHKCB", ("sse", "shkcp")),
    ),
)
def test_cninfo_board_routes_are_external_authority_bindings(
    board: CninfoListingBoard,
    venue: str,
    page_column: str,
    query_route: tuple[str, str],
) -> None:
    assert board.venue == venue
    assert board.page_column == page_column
    assert board.query_route == query_route


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        (
            "首次公开发行股票（A股）招股说明书",
            CninfoListingClassification.RELATED_IPO_BUT_NOT_LISTING,
        ),
        (
            "首次公开发行股票并在创业板上市之上市公告书提示性公告",
            CninfoListingClassification.RELATED_IPO_BUT_NOT_LISTING,
        ),
        (
            "关于公司首次公开发行人民币普通股并在深圳证券交易所创业板上市之法律意见书",
            CninfoListingClassification.RELATED_IPO_BUT_NOT_LISTING,
        ),
        (
            "中信建投证券股份有限公司关于公司股票上市保荐书",
            CninfoListingClassification.RELATED_IPO_BUT_NOT_LISTING,
        ),
        (
            "首次公开发行股票并在科创板上市发行结果公告",
            CninfoListingClassification.RELATED_IPO_BUT_NOT_LISTING,
        ),
        (
            "非公开发行限售股上市流通公告",
            CninfoListingClassification.LATER_SHARE_LISTING,
        ),
        ("公司章程", CninfoListingClassification.UNRELATED),
        ("关于上市的公告", CninfoListingClassification.UNRELATED),
    ),
)
def test_cninfo_metadata_classifier_rejects_non_primary_candidates(
    title: str,
    expected: CninfoListingClassification,
) -> None:
    assert classify_cninfo_listing_title(title) is expected


def test_cninfo_security_lookup_resolves_one_exact_a_share_org_id() -> None:
    match = parse_cninfo_security_lookup(
        (_FIXTURES / "601328-security-lookup.json").read_bytes(),
        sec_code="601328",
    )

    assert match.sec_code == "601328"
    assert match.org_id == "9900002841"


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (
            b'[{"code":"000001","pinyin":"x","sjstsBond":"false",'
            b'"category":"A\xe8\x82\xa1","type":"shj","delisted":"false",'
            b'"orgId":"org1","zwjc":"x"}]',
            CninfoFailureReason.LOOKUP_UNAVAILABLE,
        ),
        (
            json.dumps(
                [
                    {
                        "code": "601328",
                        "pinyin": "a",
                        "sjstsBond": "false",
                        "category": "A股",
                        "type": "shj",
                        "delisted": "false",
                        "orgId": "org1",
                        "zwjc": "a",
                    },
                    {
                        "code": "601328",
                        "pinyin": "b",
                        "sjstsBond": "false",
                        "category": "A股",
                        "type": "shj",
                        "delisted": "false",
                        "orgId": "org2",
                        "zwjc": "b",
                    },
                ],
                ensure_ascii=False,
            ).encode(),
            CninfoFailureReason.ORG_ID_AMBIGUOUS,
        ),
        (
            json.dumps(
                [
                    {
                        "code": "601328",
                        "pinyin": "a",
                        "sjstsBond": "false",
                        "category": "A股",
                        "type": "shj",
                        "delisted": "false",
                        "orgId": "unsafe,org",
                        "zwjc": "a",
                    }
                ],
                ensure_ascii=False,
            ).encode(),
            CninfoFailureReason.SCHEMA_INCOMPATIBLE,
        ),
        (b"not-json", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
    ),
)
def test_cninfo_security_lookup_fails_closed_without_one_exact_org(
    payload: bytes,
    reason: CninfoFailureReason,
) -> None:
    with pytest.raises(CninfoListingPublicationError) as exc_info:
        parse_cninfo_security_lookup(payload, sec_code="601328")

    assert exc_info.value.reason is reason


def test_cninfo_announcement_query_selects_one_initial_listing_candidate() -> None:
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / "601328-announcement-query.json").read_bytes(),
        sec_code="601328",
        org_id="9900002841",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(2007, 5, 10),
        window_end=date(2007, 5, 15),
    )

    assert selection.announcement_id == "23418766"
    assert selection.publication_date == date(2007, 5, 14)
    assert selection.exact_title == "首次公开发行A股股票上市公告书"
    assert selection.page_column == "SHZB"
    assert selection.classification is CninfoListingClassification.INITIAL_LISTING


def test_cninfo_legacy_metadata_requires_qualified_document_verification() -> None:
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / "600895-announcement-query.json").read_bytes(),
        sec_code="600895",
        org_id="gssh0600895",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )

    assert selection.classification is CninfoListingClassification.AMBIGUOUS
    assert selection.document_required is True


def test_cninfo_announcement_query_rejects_unproven_pagination() -> None:
    payload = json.loads(
        (_FIXTURES / "601328-announcement-query.json").read_text(encoding="utf-8")
    )
    payload["totalpages"] = 1

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        select_cninfo_listing_announcement(
            json.dumps(payload, ensure_ascii=False).encode(),
            sec_code="601328",
            org_id="9900002841",
            board=CninfoListingBoard.XSHG_MAIN,
            window_start=date(2007, 5, 10),
            window_end=date(2007, 5, 15),
        )

    assert exc_info.value.reason is CninfoFailureReason.INCOMPLETE_PAGINATION


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("has_more", CninfoFailureReason.INCOMPLETE_PAGINATION),
        ("count_mismatch", CninfoFailureReason.INCOMPLETE_PAGINATION),
        ("security_mismatch", CninfoFailureReason.IDENTITY_MISMATCH),
        ("org_mismatch", CninfoFailureReason.IDENTITY_MISMATCH),
        ("venue_mismatch", CninfoFailureReason.VENUE_MISMATCH),
        ("adjunct_mismatch", CninfoFailureReason.IDENTITY_MISMATCH),
        ("adjunct_type_mismatch", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
        ("zero_candidates", CninfoFailureReason.ANNOUNCEMENT_UNAVAILABLE),
        ("multiple_candidates", CninfoFailureReason.ANNOUNCEMENT_AMBIGUOUS),
        ("coerced_type", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
        ("timestamp_out_of_range", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
        ("duplicate_key", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
        ("schema_drift", CninfoFailureReason.SCHEMA_INCOMPATIBLE),
    ),
)
def test_cninfo_announcement_query_fails_closed(
    mutation: str,
    reason: CninfoFailureReason,
) -> None:
    payload = json.loads(
        (_FIXTURES / "601328-announcement-query.json").read_text(encoding="utf-8")
    )
    if mutation == "has_more":
        payload["hasMore"] = True
    elif mutation == "count_mismatch":
        payload["totalRecordNum"] = 4
    elif mutation == "security_mismatch":
        payload["announcements"][0]["secCode"] = "000001"
    elif mutation == "org_mismatch":
        payload["announcements"][0]["orgId"] = "another-org"
    elif mutation == "venue_mismatch":
        payload["announcements"][0]["pageColumn"] = "SZCY"
    elif mutation == "adjunct_mismatch":
        payload["announcements"][0]["adjunctUrl"] = (
            "finalpage/2007-05-14/999999.PDF"
        )
    elif mutation == "adjunct_type_mismatch":
        payload["announcements"][0]["adjunctType"] = "HTML"
    elif mutation == "zero_candidates":
        payload["announcements"][0]["announcementTitle"] = "关于上市的公告"
    elif mutation == "multiple_candidates":
        second = dict(payload["announcements"][0])
        second["announcementId"] = "23418768"
        second["adjunctUrl"] = "finalpage/2007-05-14/23418768.PDF"
        payload["announcements"].append(second)
        payload["totalAnnouncement"] = 4
        payload["totalRecordNum"] = 4
    elif mutation == "coerced_type":
        payload["hasMore"] = "false"
    elif mutation == "timestamp_out_of_range":
        payload["announcements"][0]["announcementTime"] = 10**30
    elif mutation == "schema_drift":
        payload["unexpected"] = "field"
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if mutation == "duplicate_key":
        encoded = encoded.replace(
            b'"hasMore": false',
            b'"hasMore": false, "hasMore": false',
        )

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        select_cninfo_listing_announcement(
            encoded,
            sec_code="601328",
            org_id="9900002841",
            board=CninfoListingBoard.XSHG_MAIN,
            window_start=date(2007, 5, 10),
            window_end=date(2007, 5, 15),
        )

    assert exc_info.value.reason is reason


@pytest.mark.parametrize(
    ("fixture", "sec_code", "org_id", "board", "window_start", "window_end", "announcement_id"),
    (
        (
            "300750-announcement-query.json",
            "300750",
            "GD165627",
            CninfoListingBoard.XSHE_CHINEXT,
            date(2018, 6, 8),
            date(2018, 6, 11),
            "1205044219",
        ),
        (
            "688981-announcement-query.json",
            "688981",
            "gshk0000981",
            CninfoListingBoard.XSHG_STAR,
            date(2020, 7, 13),
            date(2020, 7, 16),
            "1208033037",
        ),
    ),
)
def test_cninfo_announcement_query_selects_board_specific_qualified_metadata(
    fixture: str,
    sec_code: str,
    org_id: str,
    board: CninfoListingBoard,
    window_start: date,
    window_end: date,
    announcement_id: str,
) -> None:
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / fixture).read_bytes(),
        sec_code=sec_code,
        org_id=org_id,
        board=board,
        window_start=window_start,
        window_end=window_end,
    )

    assert selection.announcement_id == announcement_id
    assert selection.classification is CninfoListingClassification.INITIAL_LISTING
    assert selection.document_required is False


def test_cninfo_legacy_html_extracts_qualified_initial_listing_fields() -> None:
    raw_html = base64.b64decode(
        (_FIXTURES / "141828-legacy-gb2312.html.b64").read_text(encoding="ascii")
    )
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / "600895-announcement-query.json").read_bytes(),
        sec_code="600895",
        org_id="gssh0600895",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )

    extraction = parse_cninfo_legacy_listing_html(
        raw_html,
        selection=selection,
        expected_venue="XSHG",
    )

    assert hashlib.sha256(raw_html).hexdigest() == (
        "d061f2195b4a095d3ffbbe88ffe9af5345b17c62437a0e7b3e00752b5d35fb05"
    )
    assert extraction.security_code == "600895"
    assert extraction.venue == "XSHG"
    assert extraction.event_kind == "initial_a_share_listing"
    assert extraction.listing_effective_date == date(1996, 4, 22)
    assert extraction.publication_date == date(1996, 4, 18)
    assert extraction.conversion_provenance == "unknown"
    assert extraction.structured_evidence_sha256 == (
        "310996be1a081fb338801c034e16c2c7971b0f9a1f9a8b295573d3773349b629"
    )


def _legacy_html_inputs() -> tuple[bytes, object]:
    raw_html = base64.b64decode(
        (_FIXTURES / "141828-legacy-gb2312.html.b64").read_text(encoding="ascii")
    )
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / "600895-announcement-query.json").read_bytes(),
        sec_code="600895",
        org_id="gssh0600895",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )
    return raw_html, selection


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("wrong_encoding", CninfoFailureReason.DOCUMENT_PARSE_FAILED),
        ("malformed_encoding", CninfoFailureReason.DOCUMENT_PARSE_FAILED),
        ("missing_code", CninfoFailureReason.DOCUMENT_BINDING_INVALID),
        ("missing_venue", CninfoFailureReason.DOCUMENT_BINDING_INVALID),
        ("missing_effective_date", CninfoFailureReason.DOCUMENT_BINDING_INVALID),
        ("later_issuance", CninfoFailureReason.DOCUMENT_BINDING_INVALID),
        ("coexisting_conflicts", CninfoFailureReason.DOCUMENT_BINDING_INVALID),
        ("malformed_html", CninfoFailureReason.DOCUMENT_PARSE_FAILED),
        ("external_entity", CninfoFailureReason.DOCUMENT_PARSE_FAILED),
    ),
)
def test_cninfo_legacy_html_fails_closed_on_unqualified_or_conflicting_content(
    mutation: str,
    reason: CninfoFailureReason,
) -> None:
    raw_html, selection = _legacy_html_inputs()
    if mutation == "wrong_encoding":
        changed = raw_html.replace(b"charset=gb2312", b"charset=big5")
    elif mutation == "malformed_encoding":
        changed = raw_html[:20_000] + b"\xff" + raw_html[20_001:]
    elif mutation == "missing_code":
        changed = raw_html.replace(b"600895", b"600896")
    elif mutation == "missing_venue":
        changed = raw_html.decode("gb2312").replace(
            "上海证券交易所", "深圳证券交易所"
        ).encode("gb2312")
    elif mutation == "missing_effective_date":
        changed = raw_html.decode("gb2312").replace(
            "股票上市日期:1996年4月22日", "股票上市日期:日期不详"
        ).encode("gb2312")
    elif mutation == "later_issuance":
        changed = raw_html.decode("gb2312").replace(
            "向社会公开发行2500万股A股", "向特定对象增发2500万股A股"
        ).encode("gb2312")
    elif mutation == "coexisting_conflicts":
        changed = raw_html.decode("gb2312").replace(
            "股票代码:600895",
            (
                "股票代码:600895 股票代码:600896 "
                "股票上市日期:1996年4月23日 深圳证券交易所 非公开发行"
            ),
            1,
        ).encode("gb2312")
    elif mutation == "malformed_html":
        changed = raw_html[:20_000]
    else:
        changed = (
            b'<!DOCTYPE html SYSTEM "https://example.invalid/evil.dtd">' + raw_html
        )

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        parse_cninfo_legacy_listing_html(
            changed,
            selection=selection,
            expected_venue="XSHG",
        )

    assert exc_info.value.reason is reason


def test_cninfo_legacy_html_requires_the_qualified_parser_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_html, selection = _legacy_html_inputs()
    monkeypatch.setattr(cninfo_module, "version", lambda _package: "6.0.1")

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        parse_cninfo_legacy_listing_html(
            raw_html,
            selection=selection,
            expected_venue="XSHG",
        )

    assert exc_info.value.reason is CninfoFailureReason.DOCUMENT_PARSER_UNAVAILABLE


def test_cninfo_legacy_html_fails_typed_when_qualified_parser_import_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_html, selection = _legacy_html_inputs()
    monkeypatch.setattr(cninfo_module, "version", lambda _package: "6.1.1")
    monkeypatch.setitem(sys.modules, "lxml", None)

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        parse_cninfo_legacy_listing_html(
            raw_html,
            selection=selection,
            expected_venue="XSHG",
        )

    assert exc_info.value.reason is CninfoFailureReason.DOCUMENT_PARSER_UNAVAILABLE


def test_cninfo_ambiguous_pdf_returns_document_automation_unavailable() -> None:
    payload = json.loads(
        (_FIXTURES / "600895-announcement-query.json").read_text(encoding="utf-8")
    )
    payload["announcements"][0]["adjunctUrl"] = "finalpage/1996-04-18/141828.PDF"
    payload["announcements"][0]["adjunctType"] = "PDF"
    selection = select_cninfo_listing_announcement(
        json.dumps(payload, ensure_ascii=False).encode(),
        sec_code="600895",
        org_id="gssh0600895",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(1996, 4, 18),
        window_end=date(1996, 4, 22),
    )

    with pytest.raises(CninfoListingPublicationError) as exc_info:
        verify_cninfo_listing_document(
            None,
            selection=selection,
            expected_venue="XSHG",
        )

    assert (
        exc_info.value.reason
        is CninfoFailureReason.DOCUMENT_AUTOMATION_UNAVAILABLE
    )


def test_cninfo_unambiguous_metadata_never_invokes_document_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = select_cninfo_listing_announcement(
        (_FIXTURES / "601328-announcement-query.json").read_bytes(),
        sec_code="601328",
        org_id="9900002841",
        board=CninfoListingBoard.XSHG_MAIN,
        window_start=date(2007, 5, 10),
        window_end=date(2007, 5, 15),
    )
    monkeypatch.setattr(
        cninfo_module,
        "parse_cninfo_legacy_listing_html",
        lambda *_args, **_kwargs: pytest.fail("document parser was invoked"),
    )

    assert (
        verify_cninfo_listing_document(
            None,
            selection=selection,
            expected_venue="XSHG",
        )
        is None
    )
