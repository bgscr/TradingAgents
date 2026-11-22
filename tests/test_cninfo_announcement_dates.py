"""Synthetic announcement dates only; no retained supplier data or live requests."""

import hashlib
import importlib
import json
from datetime import date, datetime

import pandas as pd
import pytest

from tradingagents.dataflows import china_a_enhancements as enh
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol

pytestmark = pytest.mark.unit
SOURCE = "stock_zh_a_disclosure_report_cninfo"
AS_OF = "2026-06-30"


@pytest.fixture(autouse=True)
def block_http(monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic announcement tests forbid HTTP sends")

    monkeypatch.setattr(requests.sessions.Session, "send", forbidden)
    monkeypatch.setattr(enh.ak, "stock_individual_notice_report", lambda **kw: pd.DataFrame())


def collect(monkeypatch, frame, *, as_of=AS_OF, symbol="600895.SS"):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return frame

    monkeypatch.setattr(enh.ak, SOURCE, provider)
    results = enh._collect_announcements(resolve_china_a_symbol(symbol), as_of)
    return next(result for result in results if result.source == SOURCE), calls


@pytest.mark.parametrize("symbol,code", [("600895.SS", "600895"), ("000333.SZ", "000333")])
def test_query_dates_and_stock_are_bound(monkeypatch, symbol, code):
    frame = pd.DataFrame({"公告标题": ["synthetic disclosure"], "公告时间": ["2026-06-20 08:30:00"]})
    result, calls = collect(monkeypatch, frame, symbol=symbol)
    assert calls == [{"symbol": code, "market": "沪深京", "keyword": "", "category": "",
                      "start_date": "20260401", "end_date": "20260630"}]
    assert result.status == "ok"
    assert "date=2026-06-20" in result.records[0]


def test_installed_sdk_request_and_return_columns(monkeypatch):
    """Exercise the actual installed formatter/query function with a fake HTTP boundary."""
    sdk = importlib.import_module(enh.ak.stock_zh_a_disclosure_report_cninfo.__module__)
    monkeypatch.setattr(sdk, "__get_stock_json", lambda market: {"600895": "synthetic-org"})
    monkeypatch.setattr(sdk, "get_tqdm", lambda: lambda items, **kwargs: items)
    sent = []
    timestamp = int(pd.Timestamp("2026-06-20 08:30:00", tz="Asia/Shanghai").timestamp() * 1000)

    class Response:
        def json(self):
            return {"totalAnnouncement": 1, "announcements": [{
                "secCode": "600895", "secName": "Synthetic issuer",
                "announcementTitle": "synthetic disclosure", "announcementTime": timestamp,
                "announcementId": "synthetic-id", "orgId": "synthetic-org",
            }]}

    def post(url, *, data):
        sent.append((url, dict(data)))
        return Response()

    monkeypatch.setattr(sdk.requests, "post", post)
    results = enh._collect_announcements(resolve_china_a_symbol("600895.SS"), AS_OF)
    result = next(item for item in results if item.source == SOURCE)
    assert len(sent) == 2  # Existing SDK probes then fetches page 1; not new pagination.
    assert all(url == "http://www.cninfo.com.cn/new/hisAnnouncement/query" for url, _ in sent)
    assert all(data["seDate"] == "2026-04-01~2026-06-30" for _, data in sent)
    assert all(data["stock"] == "600895,synthetic-org" for _, data in sent)
    assert result.status == "ok"
    assert result.records == ("date=2026-06-20 08:30:00; title=synthetic disclosure",)


@pytest.mark.parametrize("column", ["公告时间", "date", "公告日期", "披露日期", "鍏憡鏃ユ湡", "鎶湶鏃ユ湡"])
@pytest.mark.parametrize("value", ["2026-06-20", "2026-06-20 23:59:59"])
def test_supported_date_columns(monkeypatch, column, value):
    result, _ = collect(monkeypatch, pd.DataFrame({"title": ["synthetic disclosure"], column: [value]}))
    assert result.status == "ok"
    assert result.records == (f"date={value}; title=synthetic disclosure",)


@pytest.mark.parametrize("value", [date(2026, 6, 20), datetime(2026, 6, 20, 12, 30), pd.Timestamp("2026-06-20 12:30:00")])
def test_compatible_typed_dates(monkeypatch, value):
    result, _ = collect(monkeypatch, pd.DataFrame({"title": ["synthetic disclosure"], "date": [value]}))
    assert result.status == "ok"
    assert "date=2026-06-20" in result.records[0]


@pytest.mark.parametrize("value", [
    None, "", pd.NA, pd.NaT, float("nan"), 20260620, True, [],
    "2026-02-30", "2026-6-20", "20260620", "2026/06/20", "June 20, 2026",
    "2026-06-20 24:00:00", "2026-06-20 10:20:60", "2026-06-20T10:00:00Z",
    "2026-06-20 10:00:00+08:00", "2026-06-20\r\n", " 2026-06-20", "2026-06-20 injected",
    pd.Timestamp("2026-06-20", tz="UTC"),
])
def test_invalid_or_missing_dates_are_unavailable(monkeypatch, value):
    result, _ = collect(monkeypatch, pd.DataFrame({"title": ["synthetic rejected"], "公告时间": [value]}))
    assert result.status == "unavailable"
    assert result.records == ()
    assert result.as_of is None
    assert result.error == "no valid dated disclosures in the requested window"


@pytest.mark.parametrize("value,accepted", [
    ("2026-03-31 23:59:59", False), ("2026-04-01", True),
    ("2026-06-30 23:59:59", True), ("2026-07-01", False),
])
def test_inclusive_calendar_window(monkeypatch, value, accepted):
    result, _ = collect(monkeypatch, pd.DataFrame({"title": ["synthetic boundary"], "公告时间": [value]}))
    assert (result.status == "ok") is accepted
    assert bool(result.records) is accepted


@pytest.mark.parametrize("as_of", ["2026-6-30", "20260630", "2026-02-30", "2026-06-30\n", None])
def test_invalid_analysis_date_stops_before_sources(monkeypatch, as_of):
    def forbidden(**kwargs):
        pytest.fail("invalid date reached a provider")

    monkeypatch.setattr(enh.ak, "stock_individual_notice_report", forbidden)
    monkeypatch.setattr(enh.ak, SOURCE, forbidden)
    with pytest.raises(ValueError, match="announcement analysis date"):
        enh._collect_announcements(resolve_china_a_symbol("600895.SS"), as_of)


def test_conflicting_dates_cannot_fall_back_to_an_allowed_column(monkeypatch):
    frame = pd.DataFrame({"title": ["synthetic conflict", "synthetic invalid"],
                          "公告时间": ["2026-07-01", "invalid"], "date": ["2026-06-20"] * 2})
    result, _ = collect(monkeypatch, frame)
    assert result.status == "unavailable"
    assert result.records == ()


def test_mixed_dates_render_only_eligible_announcements(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(enh.ak, SOURCE, lambda **kw: pd.DataFrame({
        "公告标题": ["synthetic valid", "synthetic undated", "synthetic future"],
        "公告时间": ["2026-06-20 08:30:00", None, "2026-07-01"],
    }))
    text = enh.get_china_a_enhancements("600895.SS", AS_OF, "announcements")
    assert "Announcements and disclosures" in text
    assert "synthetic valid" in text
    assert "synthetic undated" not in text and "synthetic future" not in text
    assert f"Source: {SOURCE}; as_of: {AS_OF}" in text


def test_basic_still_performs_no_announcement_work(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(enh, "_collect_announcements", lambda *a: pytest.fail("Basic enabled announcements"))
    monkeypatch.setattr(enh, "_cache_path", lambda *a: pytest.fail("Basic touched cache"))
    assert enh.get_china_a_enhancements("600895.SS", AS_OF, "basic") == ""


def test_old_announcement_cache_is_preserved_but_not_reused(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    legacy = {"ticker": "600895.SS", "curr_date": AS_OF, "preset": "announcements",
              "categories": ["announcements"]}
    digest = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()[:16]
    old = tmp_path / "china_a_enhancements" / f"{digest}.md"
    old.parent.mkdir()
    old.write_text("synthetic unchecked legacy cache", encoding="utf-8")
    monkeypatch.setattr(enh.ak, SOURCE, lambda **kw: pd.DataFrame({
        "title": ["synthetic checked"], "公告时间": ["2026-06-20"],
    }))
    text = enh.get_china_a_enhancements("600895.SS", AS_OF, "announcements")
    assert "synthetic checked" in text
    assert "unchecked legacy" not in text
    assert old.read_text(encoding="utf-8") == "synthetic unchecked legacy cache"


def test_announcement_input_checked_before_cache(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(enh, "_read_cache", lambda *a: pytest.fail("invalid date reached cache"))
    with pytest.raises(ValueError, match="announcement analysis date"):
        enh.get_china_a_enhancements("600895.SS", "2026-6-30", "announcements")


def test_missing_date_column_is_not_qualified(monkeypatch):
    result, _ = collect(monkeypatch, pd.DataFrame({"title": ["synthetic undated"]}))
    assert result.status == "unavailable" and result.records == ()


def test_leap_day_window_and_agreeing_date_aliases(monkeypatch):
    frame = pd.DataFrame({"title": ["synthetic leap"], "公告时间": ["2024-02-29 12:00:00"],
                          "date": ["2024-02-29"]})
    result, calls = collect(monkeypatch, frame, as_of="2024-02-29")
    assert result.status == "ok"
    assert calls[0]["start_date"] == "20231201" and calls[0]["end_date"] == "20240229"


def test_corrected_announcement_cache_reuses_without_extra_calls(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({"title": ["synthetic disclosure"], "公告时间": ["2026-06-20"]})

    monkeypatch.setattr(enh.ak, SOURCE, provider)
    first = enh.get_china_a_enhancements("600895.SS", AS_OF, "announcements")
    assert enh.get_china_a_enhancements("600895.SS", AS_OF, "announcements") == first
    assert len(calls) == 1


def test_other_category_cache_identity_unchanged(tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    identity = {"ticker": "600895.SS", "curr_date": AS_OF, "preset": "flow_sentiment",
                "categories": ["flow_sentiment"]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    assert enh._cache_path("600895.SS", AS_OF, "flow_sentiment", {"flow_sentiment"}).name == f"{digest}.md"


@pytest.mark.parametrize("valid_column,missing_column", [("公告时间", "date"), ("date", "公告时间")])
def test_scalar_nan_alias_does_not_hide_a_valid_date(monkeypatch, valid_column, missing_column):
    frame = pd.DataFrame({"title": ["synthetic valid"], valid_column: ["2026-06-20"],
                          missing_column: [float("nan")]})
    result, _ = collect(monkeypatch, frame)
    assert result.status == "ok" and result.records == ("date=2026-06-20; title=synthetic valid",)
