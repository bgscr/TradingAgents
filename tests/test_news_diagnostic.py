import hashlib
import inspect
import json
import sys
from pathlib import Path
from types import FunctionType, SimpleNamespace

import pytest

from tests.fixtures.akshare_news_parser import stock_news_em

# Fixed source identity, not a hash computed from whichever SDK is installed.
TEST_PARSER_SHA256 = "fc1515244d5cc2afc16603b3f655820e0903ded18745c6221ffd7f299dde77ee"


@pytest.fixture(autouse=True)
def reviewed_parser(monkeypatch):
    """These unit tests use a reviewed, sanitized parser; no live SDK approval."""
    from tradingagents import news_diagnostic as diagnostic

    assert hashlib.sha256(inspect.getsource(stock_news_em).encode()).hexdigest() == TEST_PARSER_SHA256
    # Preserve the SDK namespace dispatch so the real production adapter still
    # supplies its per-call pandas string handling. Do not change global options.
    parser = FunctionType(
        stock_news_em.__code__,
        {**stock_news_em.__globals__, "__name__": "akshare.news.reviewed_test_fixture"},
        stock_news_em.__name__, stock_news_em.__defaults__,
    )
    monkeypatch.setattr(diagnostic.akshare_data.ak, "stock_news_em", parser)
    monkeypatch.setattr(diagnostic, "PROJECTION_SDK_SHA256", TEST_PARSER_SHA256)


def synthetic_get(*, extra=None, dates=None, mutate=None):
    """Code-defined public fixture, never a reconstruction of a live response."""
    def get(url, *, params, **kwargs):
        rows = [
            {
                "date": date,
                "mediaName": "Synthetic public fixture",
                "code": "202610100000",
                "title": "Synthetic title",
                "content": "Synthetic content",
            }
            for date in (
                dates if dates is not None else
                ("2026-10-01 12:00:00", "2026-10-10 12:00:00", "invalid-date")
            )
        ]
        value = {"result": {"cmsArticleWebOld": rows}, **(extra or {})}
        if mutate:
            mutate(value)
        text = params["cb"] + "(" + json.dumps(value) + ")"
        return SimpleNamespace(status_code=200, text=text, content=text.encode(), headers={})
    return get


def test_public_metadata_capture_keeps_production_counts_and_replays(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"code": 0, "hits": 3, "success": True}),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["artifact_sha256"]  # fixed parser + get_news + acquisition succeeded
    assert result["body_saved"], "public envelope metadata must not lose the replay body"
    assert result["checks"]["retention"]["status"] == "passed"
    assert result["articles"]["raw"]["count"] == 3
    assert result["articles"]["raw"]["invalid_dates"] == 1
    assert result["articles"]["raw"]["date_min"] == "2026-10-01T12:00:00"
    assert result["articles"]["raw"]["date_max"] == "2026-10-10T12:00:00"
    assert result["articles"]["sdk"]["count"] == 3
    assert result["articles"]["filtered"]["count"] == 1
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["acceptance_passed"]
    assert replay["artifact_sha256"] == result["artifact_sha256"]


def test_public_schema_rejection_survives_production_success(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"private_token": "SYNTHETIC-DO-NOT-RETAIN"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["artifact_sha256"]
    assert not result["body_saved"]
    assert result["stage"] != "available", "production must not overwrite retention failure"
    assert result["checks"]["retention"]["reason"] == "unknown_envelope_fields"
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["capture_passed"]
    assert "SYNTHETIC-DO-NOT-RETAIN" not in (tmp_path / "capture.json").read_text()


def test_unknown_envelope_retains_only_validated_projection_and_safe_shape(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={
            "message": "SYNTHETIC-PRIVATE-VALUE",
            "SYNTHETIC-PRIVATE-KEY": {"token": "SYNTHETIC-PRIVATE-VALUE"},
        }), start="2026-10-05", end="2026-10-12",
    )
    assert result["checks"]["retention"]["reason"] == "unknown_envelope_fields"
    assert not result["body_saved"]
    assert not result["capture_passed"]
    assert result["projection_saved"]
    assert not (tmp_path / "public-response.txt").exists()
    projection = (tmp_path / "public-projection.txt").read_text(encoding="utf-8")
    receipt = (tmp_path / "capture.json").read_text(encoding="utf-8")
    assert "SYNTHETIC-PRIVATE" not in projection + receipt
    fields = result["structure"]["envelope"]["fields"]
    assert any(field.get("name") == "message" and field["type"] == "string" for field in fields)
    assert any("name_sha256" in field and field["type"] == "object" for field in fields)
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["replay_kind"] == "validated_projection"
    assert replay["same_sdk_frame"]
    assert replay["same_artifact"]
    assert replay["projection_replay_passed"]
    assert not replay["acceptance_passed"]  # no exact original was retained


def test_unknown_result_metadata_can_be_projected_without_guessing_field_names(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(mutate=lambda v: v["result"].update(
            opaque_future_data={"password": "SYNTHETIC-PRIVATE"},
        )), start="2026-10-05", end="2026-10-12",
    )
    assert result["checks"]["retention"]["reason"] == "unknown_result_fields"
    assert result["projection_saved"]
    assert diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")["projection_replay_passed"]
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "public-projection.txt").read_text()


def test_observed_seven_field_shape_retains_core_and_replays_all_sdk_rows(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    # Only counts, field roles and date bounds came from a safe live receipt.
    # All contents, URLs and unobserved envelope names are invented, not a live fixture.
    dates = ["2026-08-20 09:29:18", "2026-10-14 17:47:00"] + ["2026-09-01 12:00:00"] * 8

    def add_fields(value):
        for row in value["result"]["cmsArticleWebOld"]:
            row.update(image="", url="https://synthetic.invalid/article?token=SYNTHETIC-OMITTED")

    fixture = synthetic_get(dates=dates, mutate=add_fields, extra={
        "code": 0, "msg": "SYNTHETIC-OMITTED",
        "synthetic_empty_a": "", "synthetic_empty_b": "", "synthetic_object": {},
        "synthetic_number": 1, "synthetic_string": "SYNTHETIC-OMITTED",
    })
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return fixture(*args, **kwargs)

    captured = diagnostic.capture(tmp_path, get=get, start="2026-10-09", end="2026-10-15")
    assert captured["checks"]["sdk"]["status"] == "passed"
    assert captured["checks"]["production"]["status"] == "passed"
    assert captured["projection_saved"]
    assert not captured["body_saved"]
    assert captured["checks"]["retention"]["reason"] == "unknown_envelope_fields"
    assert captured["articles"]["raw"]["count"] == captured["articles"]["sdk"]["count"] == 10
    assert captured["articles"]["filtered"]["count"] == 1
    assert captured["articles"]["raw"]["date_min"] == "2026-08-20T09:29:18"
    assert captured["articles"]["filtered"]["date_max"] == "2026-10-14T17:47:00"
    projection = (tmp_path / "public-projection.txt").read_text(encoding="utf-8")
    rows = json.loads(projection[projection.index("(") + 1:-1])["result"]["cmsArticleWebOld"]
    assert all(set(row) == {"date", "mediaName", "code", "title", "content"} for row in rows)
    assert "SYNTHETIC-OMITTED" not in projection + (tmp_path / "capture.json").read_text()
    replay = diagnostic.replay(tmp_path, start="2026-10-09", end="2026-10-15")
    assert calls == [1]  # one fixture response supplies capture and offline replay
    assert replay["same_sdk_frame"] and replay["same_artifact"]
    assert replay["articles"] == captured["articles"]
    assert replay["projection_replay_passed"] and not replay["acceptance_passed"]


@pytest.mark.parametrize("omitted", [
    {"url": "https://synthetic.invalid/?token=SYNTHETIC-OMITTED"},
    {"url": {"Authorization": "Basic SYNTHETIC-OMITTED"}},
    {"url": None},
    {"image": "api_key=SYNTHETIC-OMITTED"},
    {"image": ["SYNTHETIC-OMITTED"]},
    {"关键词": {"secret": "SYNTHETIC-OMITTED"}},
    {"url": False, "image": None, "关键词": "SYNTHETIC-OMITTED"},
], ids=["url-secret", "url-object", "url-null", "image-secret", "image-list", "keyword-object", "all"])
def test_projection_omits_only_sdk_overwritten_or_discarded_fields(tmp_path, omitted):
    from tradingagents import news_diagnostic as diagnostic

    def mutate(value):
        rows = value["result"]["cmsArticleWebOld"]
        rows[0].update(omitted)
        rows[0] = dict(reversed(list(rows[0].items())))  # SDK selects names, not positions

    captured = diagnostic.capture(
        tmp_path, get=synthetic_get(mutate=mutate), start="2026-10-05", end="2026-10-12",
    )
    assert captured["checks"]["sdk"]["status"] == "passed"
    assert captured["checks"]["production"]["status"] == "passed"
    assert captured["projection_saved"] and not captured["body_saved"]
    projection = (tmp_path / "public-projection.txt").read_text()
    assert "SYNTHETIC-OMITTED" not in projection + (tmp_path / "capture.json").read_text()
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["same_sdk_frame"] and replay["same_artifact"]
    assert replay["projection_replay_passed"] and not replay["acceptance_passed"]


@pytest.mark.parametrize("extra,reason", [
    ({"hits": "token=SYNTHETIC-OMITTED"}, "invalid_public_metadata"),
    ({"success": False}, "unsuccessful_envelope"),
])
def test_projection_ignores_metadata_without_endorsing_original_status(tmp_path, extra, reason):
    from tradingagents import news_diagnostic as diagnostic

    captured = diagnostic.capture(
        tmp_path, get=synthetic_get(extra=extra), start="2026-10-05", end="2026-10-12",
    )
    assert captured["checks"]["retention"]["reason"] == reason
    assert captured["projection_saved"] and not captured["capture_passed"]
    assert "SYNTHETIC-OMITTED" not in (tmp_path / "public-projection.txt").read_text()
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["projection_replay_passed"] and not replay["acceptance_passed"]


@pytest.mark.parametrize("name", ["opaque", "-", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"])
def test_projection_rejects_other_columns_including_sdk_output_aliases(tmp_path, name):
    from tradingagents import news_diagnostic as diagnostic

    captured = diagnostic.capture(
        tmp_path, get=synthetic_get(mutate=lambda value: value["result"]["cmsArticleWebOld"][0].update(
            {"url": "SYNTHETIC-OMITTED", name: "SYNTHETIC-OMITTED"},
        )), start="2026-10-05", end="2026-10-12",
    )
    assert not captured["body_saved"] and not captured["projection_saved"]
    assert captured["checks"]["projection"]["reason"] == "unknown_or_missing_article_fields"
    assert "SYNTHETIC-OMITTED" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("field", ["date", "mediaName", "code", "title", "content"])
def test_omitted_url_never_bypasses_sensitive_core_validation(tmp_path, field):
    from tradingagents import news_diagnostic as diagnostic

    captured = diagnostic.capture(
        tmp_path, get=synthetic_get(mutate=lambda value: value["result"]["cmsArticleWebOld"][0].update(
            {"url": "SYNTHETIC-OMITTED", field: 'Authorization: Basic SYNTHETIC-CORE'},
        )), start="2026-10-05", end="2026-10-12",
    )
    assert not captured["body_saved"] and not captured["projection_saved"]
    assert captured["checks"]["projection"]["reason"] == "sensitive_article_text"
    assert "SYNTHETIC-CORE" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("change,reason", [
    ({"新闻标题": "SDK column alias"}, "unknown_or_missing_article_fields"),
    ({"content": "token=SYNTHETIC-PRIVATE"}, "sensitive_article_text"),
    ({"content": None}, "non_string_article_field"),
])
def test_projection_does_not_drop_or_weaken_article_validation(tmp_path, change, reason):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "extra envelope"}, mutate=lambda v: v["result"]["cmsArticleWebOld"][0].update(change)),
        start="2026-10-05", end="2026-10-12",
    )
    assert not result["projection_saved"]
    assert not (tmp_path / "public-projection.txt").exists()
    assert result["checks"]["projection"]["reason"] == reason
    assert "SYNTHETIC-PRIVATE" not in (tmp_path / "capture.json").read_text()


def test_projection_requires_reviewed_sdk(tmp_path, monkeypatch):
    from tradingagents import news_diagnostic as diagnostic

    captured = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "fixture"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert captured["projection_saved"]
    monkeypatch.setattr(diagnostic, "PROJECTION_SDK_SHA256", "0" * 64)
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["same_sdk"] and replay["same_sdk_frame"] and replay["same_artifact"]
    assert replay["checks"]["replay"]["reason"] == "sdk_projection_not_reviewed"
    assert not replay["projection_replay_passed"]
    assert not replay["acceptance_passed"]
    output = tmp_path / "unreviewed"
    output.mkdir()
    result = diagnostic.capture(
        output, get=synthetic_get(extra={"message": "fixture"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert not result["projection_saved"]
    assert result["checks"]["projection"]["reason"] == "sdk_projection_not_reviewed"
    assert not (output / "public-projection.txt").exists()


def test_structure_summary_is_bounded_and_never_copies_unknown_names_or_values(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={f"PRIVATE-NAME-{i}": "PRIVATE-VALUE" for i in range(100)}),
        start="2026-10-05", end="2026-10-12",
    )
    summary = result["structure"]["envelope"]
    assert summary["field_count"] == 101
    assert len(summary["fields"]) == 32
    assert summary["truncated"]
    receipt = (tmp_path / "capture.json").read_text()
    assert "PRIVATE-NAME" not in receipt
    assert "PRIVATE-VALUE" not in receipt


def test_projection_replay_compares_full_sdk_frame_not_just_in_window_artifact(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "fixture"}),
        start="2026-10-05", end="2026-10-12",
    )
    path = tmp_path / "public-projection.txt"
    changed = path.read_text(encoding="utf-8").replace("Synthetic title", "Changed outside window", 1)
    path.write_text(changed, encoding="utf-8")
    receipt_path = tmp_path / "capture.json"
    captured = json.loads(receipt_path.read_text(encoding="utf-8"))
    captured["projection_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    receipt_path.write_text(json.dumps(captured), encoding="utf-8")
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["same_artifact"]  # only an out-of-window row was changed
    assert not replay["same_sdk_frame"]
    assert replay["checks"]["replay"]["reason"] == "sdk_frame_mismatch"
    assert not replay["projection_replay_passed"]
    assert not replay["acceptance_passed"]


@pytest.mark.parametrize("change,reason", [
    ({"sdk_frame_sha256": None}, "sdk_frame_mismatch"),
    ({"checks": {}}, "source_production_unverified"),
])
def test_projection_replay_needs_original_execution_evidence(tmp_path, change, reason):
    from tradingagents import news_diagnostic as diagnostic

    diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "fixture"}),
        start="2026-10-05", end="2026-10-12",
    )
    receipt_path = tmp_path / "capture.json"
    captured = json.loads(receipt_path.read_text(encoding="utf-8"))
    captured.update(change)
    receipt_path.write_text(json.dumps(captured), encoding="utf-8")
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["checks"]["replay"]["reason"] == reason
    assert not replay["projection_replay_passed"]


def test_projection_write_failure_preserves_existing_file(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    path = tmp_path / "public-projection.txt"
    path.write_text("existing evidence", encoding="utf-8")
    result = diagnostic.capture(
        tmp_path, get=synthetic_get(extra={"message": "fixture"}),
        start="2026-10-05", end="2026-10-12",
    )
    assert path.read_text() == "existing evidence"
    assert result["checks"]["projection"]["reason"] == "projection_write_failed"
    assert not result["projection_saved"]


def test_projection_expansion_respects_replay_size_limit(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    def get(*args, **kwargs):
        response = synthetic_get(extra={"message": "fixture"}, dates=["2026-10-10 12:00:00"])(*args, **kwargs)
        response.text = response.text.replace("Synthetic content", "文" * (diagnostic.LIMIT // 6 + 1))
        response.content = response.text.encode()
        return response

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["decode"]["status"] == "passed"
    assert result["checks"]["projection"]["reason"] == "projection_size_limit"
    assert not result["projection_saved"]


@pytest.mark.parametrize("mutate,reason", [
    (lambda v: v.update(hits="SYNTHETIC-SECRET"), "invalid_public_metadata"),
    (lambda v: v.update(success=False), "unsuccessful_envelope"),
    (lambda v: v["result"].update(private_token="SYNTHETIC-SECRET"), "unknown_result_fields"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(token="SYNTHETIC-SECRET"), "unknown_or_missing_article_fields"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].pop("title"), "unknown_or_missing_article_fields"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(content={"secret": "SYNTHETIC-SECRET"}), "non_string_article_field"),
    (lambda v: v["result"]["cmsArticleWebOld"][0].update(content="api_key=SYNTHETIC-SECRET"), "sensitive_article_text"),
    (lambda v: v["result"].update(cmsArticleWebOld={}), "articles_not_list"),
    (lambda v: v.update(result=[]), "invalid_envelope"),
])
def test_unrecognized_or_sensitive_response_is_never_saved(tmp_path, mutate, reason):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(mutate=mutate), start="2026-10-05", end="2026-10-12",
    )
    assert result["checks"]["retention"]["reason"] == reason
    assert not result["body_saved"]
    assert not result["capture_passed"]
    assert not (tmp_path / "public-response.txt").exists()
    assert "SYNTHETIC-SECRET" not in (tmp_path / "capture.json").read_text()


@pytest.mark.parametrize("extra", [None, {"message": "unknown envelope"}], ids=["exact", "projection"])
@pytest.mark.parametrize("content", [
    'Authorization: Basic SYNTHETIC-CREDENTIAL',
    '{"Authorization": "Basic SYNTHETIC-CREDENTIAL"}',
    "{'aUtHoRiZaTiOn' : 'Basic SYNTHETIC-CREDENTIAL'}",
], ids=["header", "json", "single-quoted"])
def test_authorization_markers_never_enter_retained_text(tmp_path, extra, content):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path,
        get=synthetic_get(
            extra=extra, dates=["2026-10-10 12:00:00"],
            mutate=lambda value: value["result"]["cmsArticleWebOld"][0].update(content=content),
        ),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["checks"]["sdk"]["status"] == "passed"
    assert result["checks"]["production"]["status"] == "passed"
    check = "projection" if extra else "retention"
    assert result["checks"][check]["reason"] == "sensitive_article_text"
    assert not result["body_saved"]
    assert not result["projection_saved"]
    assert not result["capture_passed"]
    assert not (tmp_path / "public-response.txt").exists()
    assert not (tmp_path / "public-projection.txt").exists()
    assert "SYNTHETIC-CREDENTIAL" not in (tmp_path / "capture.json").read_text()


def test_raw_count_is_observed_even_when_response_exceeds_requested_page_size(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(dates=["2026-10-10 12:00:00"] * 11),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["articles"]["raw"]["count"] == 11
    assert result["checks"]["retention"]["reason"] == "article_limit"
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["body_saved"]


@pytest.mark.parametrize("dates,filtered,invalid", [
    (["2026-10-05 00:00:00", "2026-10-12 23:59:59", "2026-10-13 00:00:00"], 2, 0),
    (["2026-10-01 00:00:00"], 0, 0),
    (["", "invalid-date"], 0, 2),
])
def test_counts_observe_actual_production_window(tmp_path, dates, filtered, invalid):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(dates=dates), start="2026-10-05", end="2026-10-12",
    )
    assert result["articles"]["raw"]["count"] == len(dates)
    assert result["articles"]["sdk"]["invalid_dates"] == invalid
    assert result["articles"]["filtered"]["count"] == filtered
    assert result["capture_passed"] == bool(filtered)
    assert result["body_saved"]  # capture precedes production date admission
    assert not result["acceptance_passed"]  # capture alone is not replay acceptance
    if not filtered:
        assert result["articles"]["filtered"]["date_min"] is None
        assert result["checks"]["production"]["status"] == "failed"
        assert result["outcomes"] == ["no_data"]


@pytest.mark.parametrize("status", [302, 403, 500])
def test_http_failure_never_claims_sdk_or_capture_success(tmp_path, status):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=lambda *a, **k: SimpleNamespace(status_code=status),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["checks"]["network"]["status"] == "passed"
    assert result["checks"]["http"]["reason"] == "http_failure_or_redirect"
    assert result["stage"] == "http"
    assert result["request_count"] == 1
    assert result["articles"]["raw"]["count"] is None
    assert not result["body_saved"]


def test_transport_failure_retains_safe_reason_without_exception_text(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    def get(*a, **k):
        raise TimeoutError("SYNTHETIC-SECRET")

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["network"]["exception_type"] == "TimeoutError"
    assert result["stage"] == "transport"
    assert result["request_count"] == 1
    assert "SYNTHETIC-SECRET" not in (tmp_path / "capture.json").read_text()


def test_response_size_limit_is_independent_of_sdk_failure(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    text = "x" * (diagnostic.LIMIT + 1)
    result = diagnostic.capture(
        tmp_path, get=lambda *a, **k: SimpleNamespace(
            status_code=200, text=text, content=text.encode(), headers={},
        ), start="2026-10-05", end="2026-10-12",
    )
    assert result["stage"] == "response_limit"
    assert result["checks"]["decode"]["reason"] == "response_limit"
    assert not result["body_saved"]


@pytest.mark.parametrize("name,check,reason", [
    ("public-response.txt", "retention", "body_write_failed"),
    ("capture.json", "receipt", "capture_receipt_write_failed"),
])
def test_persistence_failure_does_not_overwrite_existing_evidence(tmp_path, name, check, reason):
    from tradingagents import news_diagnostic as diagnostic

    path = tmp_path / name
    path.write_text("existing synthetic evidence", encoding="utf-8")
    result = diagnostic.capture(tmp_path, get=synthetic_get(), start="2026-10-05", end="2026-10-12")
    assert path.read_text(encoding="utf-8") == "existing synthetic evidence"
    assert result["checks"][check]["reason"] == reason
    assert result["checks"][check]["exception_type"] == "FileExistsError"
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["capture_passed"]


@pytest.mark.parametrize("mutation,reason", [
    ("body", "retained_text_digest_mismatch"),
    ("sdk_sha256", "sdk_mismatch"),
    ("request_fingerprint", "request_mismatch"),
    ("artifact_sha256", "artifact_mismatch"),
    ("window", "window_mismatch"),
])
def test_replay_rejects_changed_evidence_or_bindings(tmp_path, mutation, reason):
    from tradingagents import news_diagnostic as diagnostic

    diagnostic.capture(tmp_path, get=synthetic_get(), start="2026-10-05", end="2026-10-12")
    if mutation == "body":
        with (tmp_path / "public-response.txt").open("a", encoding="utf-8") as handle:
            handle.write(" ")
    else:
        path = tmp_path / "capture.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt[mutation] = {} if mutation == "window" else "0" * 64
        path.write_text(json.dumps(receipt), encoding="utf-8")
    result = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["replay"]["reason"] == reason
    assert not result["acceptance_passed"]


def test_replay_missing_body_is_a_reported_failure(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["replay"]["reason"] == "invalid_replay_file"
    assert result["request_count"] == 0
    assert not result["acceptance_passed"]


def test_duplicate_json_keys_cannot_hide_unretained_data(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    def get(*args, **kwargs):
        response = synthetic_get()(*args, **kwargs)
        response.text = response.text.replace(
            '"content": "Synthetic content"',
            '"content": "token=SYNTHETIC-SECRET", "content": "Synthetic content"',
        )
        response.content = response.text.encode()
        return response

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["retention"]["reason"] == "invalid_json_or_duplicate_fields"
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["body_saved"]


def test_decode_failure_is_not_reported_as_json_or_sdk_failure(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    class Response:
        status_code = 200
        headers = {}
        content = b"synthetic undecodable bytes"

        @property
        def text(self):
            raise UnicodeError("SYNTHETIC-SECRET")

    result = diagnostic.capture(
        tmp_path, get=lambda *a, **k: Response(), start="2026-10-05", end="2026-10-12",
    )
    assert result["stage"] == "decode"
    assert result["checks"]["decode"]["reason"] == "decode_exception"
    assert result["checks"]["decode"]["exception_type"] == "UnicodeError"
    assert not result["body_saved"]
    assert "SYNTHETIC-SECRET" not in (tmp_path / "capture.json").read_text()


def test_production_filter_exception_preserves_reason(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path, get=synthetic_get(dates=["2026-10-10 12:00:00+08:00"]),
        start="2026-10-05", end="2026-10-12",
    )
    assert result["body_saved"]
    assert result["checks"]["sdk"]["status"] == "passed"
    assert result["checks"]["production"]["reason"] == "date_window_exception"
    assert result["checks"]["production"]["exception_type"] == "TypeError"
    assert result["articles"]["filtered"]["count"] is None


def test_unrecognized_headers_are_not_copied_into_receipts(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    def get(*args, **kwargs):
        response = synthetic_get()(*args, **kwargs)
        response.headers = {
            "Content-Type": "application/javascript; token=SYNTHETIC-SECRET",
            "Content-Encoding": "SYNTHETIC-SECRET",
        }
        return response

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["content_type"] == "application/javascript"
    assert result["content_encoding"] == "unrecognized"
    assert "SYNTHETIC-SECRET" not in (tmp_path / "capture.json").read_text()


def test_replay_cannot_pass_acceptance_for_failed_source_capture(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    diagnostic.capture(tmp_path, get=synthetic_get(), start="2026-10-05", end="2026-10-12")
    path = tmp_path / "capture.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["capture_passed"] = False
    path.write_text(json.dumps(receipt), encoding="utf-8")
    result = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert result["checks"]["production"]["status"] == "passed"
    assert not result["source_capture_passed"]
    assert not result["acceptance_passed"]


def test_news_diagnostic_replays_fixed_parser_and_production_path(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    calls = []

    def get(url, *, params, headers, **options):
        calls.append((url, params, options))
        assert params["_"] == "1764599530176"
        assert "referer" in headers and "cookie" in headers
        assert options == {"allow_redirects": False, "timeout": 20}
        text = (
            params["cb"]
            + "("
            + json.dumps(
                {
                    "result": {
                        "cmsArticleWebOld": [
                            {
                                "date": "2026-10-10 12:00:00",
                                "mediaName": "Synthetic public fixture",
                                "code": "202610101234",
                                "title": "<em>Title</em>",
                                "content": "Content\u3000fixture",
                                "image": "",
                            }
                        ]
                    }
                }
            )
            + ")"
        )
        return SimpleNamespace(
            status_code=200,
            text=text,
            content=text.encode(),
            headers={"Content-Type": "application/javascript", "Content-Encoding": "gzip"},
        )

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["stage"] == "available"
    assert result["request_count"] == 1
    assert result["body_saved"]
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["stage"] == "available"
    assert replay["artifact_sha256"] == result["artifact_sha256"]
    assert len(calls) == 1
    saved = (tmp_path / "public-response.txt").read_text(encoding="utf-8")
    assert "202610101234" in saved
    assert "cookie" not in saved


@pytest.mark.parametrize(
    "text,stage",
    [
        ("<html>not news</html>", "jsonp"),
        ("jQuery35101792940631092459_1764599530165(not json)", "json"),
        (
            'jQuery35101792940631092459_1764599530165({"result":{"cmsArticleWebOld":[]}})',
            "sdk_dataframe",
        ),
    ],
)
def test_news_diagnostic_names_failure_stage_without_claiming_recovery(tmp_path, text, stage):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path,
        get=lambda *a, **k: SimpleNamespace(
            status_code=200, text=text, content=text.encode(), headers={}
        ),
        start="2026-10-05",
        end="2026-10-12",
    )
    assert result["stage"] == stage
    assert result["request_count"] == 1
    assert result["body_saved"] == (stage == "sdk_dataframe")


def _news_cli(monkeypatch, output, *, replay=None, mode=None):
    from scripts import diagnose_public_news as cli

    args = ["diagnose_public_news.py", "--output", str(output),
            "--start", "2026-10-05", "--end", "2026-10-12"]
    args += ["--replay", str(replay)] if replay else ["--authorize-one-public-get-and-save"]
    if mode is not None:
        args += ["--acceptance-mode", mode]
    # Acceptance logic is a unit seam: control only this module's view of the
    # runtime. Real interpreter flags/guard are tested in a separate subprocess.
    with monkeypatch.context() as patch:
        patch.setattr(cli, "sys", SimpleNamespace(
            flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True,
        ))
        patch.setattr(cli, "install", lambda *a, **k: ({}, []))
        patch.setattr(sys, "argv", args)
        code = cli.main()
    return code, json.loads((output / "result.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", [None, "original-text", "news-content"], ids=["legacy", "original", "content"])
@pytest.mark.parametrize("projection", [False, True], ids=["exact", "projection"])
@pytest.mark.parametrize("phase", ["capture", "replay"])
def test_cli_acceptance_modes(tmp_path, monkeypatch, mode, projection, phase):
    from curl_cffi import requests

    from tradingagents import news_diagnostic as diagnostic

    get = synthetic_get(extra={"message": "synthetic metadata"} if projection else None)
    monkeypatch.setattr(requests, "get", get)
    source = None
    if phase == "replay":
        source = tmp_path / "input"
        source.mkdir()
        diagnostic.capture(source, get=get, start="2026-10-05", end="2026-10-12")
        before = (source / "capture.json").read_bytes()  # legacy receipt has no mode fields
    code, result = _news_cli(monkeypatch, tmp_path / "output", replay=source, mode=mode)
    expected = not projection or mode == "news-content"
    assert code == (0 if expected else 1)
    assert result["acceptance"] == {
        "mode": mode or "original-text", "passed": expected,
        "goals": {"original-text": not projection, "news-content": True},
        "content_source_passed": True,
    }
    reservation = json.loads((tmp_path / "output/reservation.json").read_text())
    assert reservation["acceptance_mode"] == (mode or "original-text")
    assert result["capture_passed" if phase == "capture" else "acceptance_passed"] is not projection
    if projection and phase == "capture":
        assert not result["body_saved"]
        assert result["checks"]["retention"]["reason"] == "unknown_envelope_fields"
    if phase == "replay":
        assert result["same_sdk_frame"] and result["same_artifact"]
        assert (source / "capture.json").read_bytes() == before


@pytest.mark.parametrize("mode", ["original-text", "news-content"])
@pytest.mark.parametrize("failure", [
    "transport", "http", "sdk-empty", "no-data", "sensitive", "body-write", "projection-write", "receipt-write",
])
def test_cli_modes_keep_capture_failures(tmp_path, monkeypatch, mode, failure):
    from curl_cffi import requests

    def unavailable(*a, **k):
        raise TimeoutError("SYNTHETIC-PRIVATE")

    get = synthetic_get()
    if failure == "transport":
        get = unavailable
    elif failure == "http":
        def get(*a, **k):
            return SimpleNamespace(status_code=500)
    elif failure in {"sdk-empty", "no-data"}:
        get = synthetic_get(dates=[] if failure == "sdk-empty" else ["2026-09-01"])
    elif failure == "sensitive":
        get = synthetic_get(mutate=lambda v: v["result"]["cmsArticleWebOld"][0].update(content="token=SYNTHETIC-PRIVATE"))
    elif failure in {"projection-write", "receipt-write"}:
        get = synthetic_get(extra={"message": "synthetic metadata"})
    monkeypatch.setattr(requests, "get", get)
    output = tmp_path / "output"
    denied = {"body-write": "public-response.txt", "projection-write": "public-projection.txt", "receipt-write": "capture.json"}.get(failure)
    if denied:
        original_open = Path.open

        def fail_write(path, mode="r", *args, **kwargs):
            if path == output / denied and mode == "x":
                raise PermissionError("SYNTHETIC-PRIVATE")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_write)
    code, result = _news_cli(monkeypatch, output, mode=mode)
    assert code == 1
    assert not result["acceptance"]["passed"]
    assert result["acceptance"]["goals"] == {"original-text": False, "news-content": False}
    assert any(check["status"] == "failed" for check in result["checks"].values())
    assert "SYNTHETIC-PRIVATE" not in (output / "result.json").read_text()


@pytest.mark.parametrize("mode", ["original-text", "news-content"])
@pytest.mark.parametrize("projection", [False, True], ids=["exact", "projection"])
@pytest.mark.parametrize("mutation", ["body", "sdk_frame_sha256", "artifact_sha256", "window"])
def test_cli_modes_reject_replay_tampering(tmp_path, monkeypatch, mode, projection, mutation):
    from tradingagents import news_diagnostic as diagnostic

    source = tmp_path / "input"
    source.mkdir()
    diagnostic.capture(
        source, get=synthetic_get(extra={"message": "synthetic metadata"} if projection else None),
        start="2026-10-05", end="2026-10-12",
    )
    if mutation == "body":
        with (source / ("public-projection.txt" if projection else "public-response.txt")).open("a") as handle:
            handle.write(" ")
    else:
        path = source / "capture.json"
        receipt = json.loads(path.read_text())
        receipt[mutation] = {} if mutation == "window" else "0" * 64
        path.write_text(json.dumps(receipt))
    code, result = _news_cli(monkeypatch, tmp_path / "output", replay=source, mode=mode)
    assert code == 1
    assert result["acceptance"]["goals"] == {"original-text": False, "news-content": False}
    assert result["checks"]["replay"]["status"] == "failed"


@pytest.mark.parametrize("mode", [None, "original-text", "news-content"])
@pytest.mark.parametrize("phase", ["capture", "replay"])
def test_cli_legacy_exact_success_without_full_frame_cannot_pass_content_goal(tmp_path, monkeypatch, mode, phase):
    from curl_cffi import requests

    from tradingagents import news_diagnostic as diagnostic

    def no_digest(frame):
        raise ValueError("synthetic missing frame evidence")

    monkeypatch.setattr(requests, "get", synthetic_get())
    source = None
    with monkeypatch.context() as patch:
        patch.setattr(diagnostic, "_sdk_frame_digest", no_digest)
        if phase == "replay":
            source = tmp_path / "input"
            source.mkdir()
            diagnostic.capture(source, get=synthetic_get(), start="2026-10-05", end="2026-10-12")
        else:
            code, result = _news_cli(monkeypatch, tmp_path / "output", mode=mode)
    if phase == "replay":
        code, result = _news_cli(monkeypatch, tmp_path / "output", replay=source, mode=mode)
    assert code == (1 if mode == "news-content" else 0)
    assert result["acceptance"]["goals"] == {"original-text": True, "news-content": False}


def test_cli_invalid_acceptance_mode_fails_before_output_or_transport(tmp_path, monkeypatch):
    output = tmp_path / "output"
    with pytest.raises(SystemExit) as exc:
        _news_cli(monkeypatch, output, mode="all-success")
    assert exc.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("projection", [False, True], ids=["exact", "projection"])
@pytest.mark.parametrize("change", ["failed", "missing"])
@pytest.mark.parametrize("stage", ["network", "http", "decode", "sdk", "production", "receipt", "retained-representation"])
def test_cli_content_replay_rejects_failed_or_missing_source_stage(tmp_path, monkeypatch, projection, change, stage):
    from tradingagents import news_diagnostic as diagnostic

    source = tmp_path / "input"
    source.mkdir()
    diagnostic.capture(
        source, get=synthetic_get(extra={"message": "synthetic metadata"} if projection else None),
        start="2026-10-05", end="2026-10-12",
    )
    path = source / "capture.json"
    receipt = json.loads(path.read_text())
    if stage == "retained-representation":
        stage = "projection" if projection else "retention"
    if change == "missing":
        del receipt["checks"][stage]
    else:
        receipt["checks"][stage] = {"status": "failed", "reason": "synthetic_source_failure"}
    path.write_text(json.dumps(receipt))
    code, result = _news_cli(monkeypatch, tmp_path / "output", replay=source, mode="news-content")
    assert code == 1
    assert not result["acceptance"]["passed"]
    assert not result["acceptance"]["goals"]["news-content"]
    assert not result["acceptance"]["content_source_passed"]
