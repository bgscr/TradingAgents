import traceback
from dataclasses import FrozenInstanceError
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.picker.errors import (
    PITConfigurationError,
    PITError,
    PITSchemaError,
    TushareNotConfiguredError,
    TushareRateLimitError,
)
from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.pit_models import Dataset, PartitionKey
from tradingagents.picker.tushare_provider import ENDPOINTS, EndpointSpec, TushareProvider


class FakePro:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def query(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def config(tmp_path, token="token"):
    return PITConfig(token, tmp_path, 40, {}, 8)


def assert_credential_safe(error):
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert "secret-token" not in str(error)
    assert "secret-token" not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def test_missing_token_fails_without_importing_sdk(tmp_path, monkeypatch):
    imported = False

    def unexpected_import(_name):
        nonlocal imported
        imported = True

    monkeypatch.setattr("importlib.import_module", unexpected_import)

    with pytest.raises(TushareNotConfiguredError, match="TUSHARE_TOKEN"):
        TushareProvider.create(config(tmp_path, token=None), client=None)

    assert imported is False


def test_injected_client_does_not_import_optional_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "importlib.import_module",
        lambda _name: pytest.fail("optional SDK imported with an injected client"),
    )

    client = FakePro([])
    provider = TushareProvider.create(config(tmp_path), client=client)

    assert provider.client is client


def test_create_imports_tushare_lazily_and_passes_token(tmp_path, monkeypatch):
    client = FakePro([])
    calls = []

    class FakeModule:
        @staticmethod
        def pro_api(token):
            calls.append(token)
            return client

    monkeypatch.setattr(
        "importlib.import_module",
        lambda name: calls.append(name) or FakeModule(),
    )

    provider = TushareProvider.create(config(tmp_path, token="secret-token"))

    assert provider.client is client
    assert calls == ["tushare", "secret-token"]


def test_missing_sdk_error_has_install_hint_without_token(tmp_path, monkeypatch):
    def missing_sdk(_name):
        raise ImportError("secret-token")

    monkeypatch.setattr("importlib.import_module", missing_sdk)
    pit_config = config(tmp_path, token="secret-token")

    with pytest.raises(TushareNotConfiguredError) as error:
        TushareProvider.create(pit_config)

    assert 'pip install ".[pit]"' in str(error.value)
    assert_credential_safe(error.value)


def test_sdk_client_initialization_failure_is_sanitized(tmp_path, monkeypatch):
    class BrokenModule:
        @staticmethod
        def pro_api(_token):
            raise RuntimeError("secret-token")

    monkeypatch.setattr("importlib.import_module", lambda _name: BrokenModule())
    pit_config = config(tmp_path, token="secret-token")

    with pytest.raises(PITConfigurationError) as error:
        TushareProvider.create(pit_config)

    assert str(error.value) == "Tushare client initialization failed"
    assert_credential_safe(error.value)


def test_endpoint_catalog_has_exact_phase_one_contract():
    assert {
        Dataset.TRADE_CAL: EndpointSpec(
            "trade_cal", None, "exchange,cal_date,is_open,pretrade_date"
        ),
        Dataset.STOCK_BASIC: EndpointSpec(
            "stock_basic",
            None,
            "ts_code,symbol,name,area,industry,market,list_status,list_date,delist_date,is_hs",
        ),
        Dataset.DAILY: EndpointSpec(
            "daily",
            "trade_date",
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        ),
        Dataset.DAILY_BASIC: EndpointSpec(
            "daily_basic",
            "trade_date",
            "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,"
            "total_share,float_share,free_share,total_mv,circ_mv",
        ),
        Dataset.NAMECHANGE: EndpointSpec(
            "namechange",
            None,
            "ts_code,name,start_date,end_date,ann_date,change_reason",
        ),
        Dataset.SUSPEND_D: EndpointSpec(
            "suspend_d",
            "suspend_date",
            "ts_code,suspend_date,resume_date,ann_date,suspend_reason,reason_type",
        ),
    } == ENDPOINTS

    with pytest.raises(FrozenInstanceError):
        ENDPOINTS[Dataset.DAILY].api_name = "changed"


@pytest.mark.parametrize(
    ("dataset", "endpoint", "date_arg"),
    [
        (Dataset.DAILY, "daily", "trade_date"),
        (Dataset.DAILY_BASIC, "daily_basic", "trade_date"),
        (Dataset.SUSPEND_D, "suspend_d", "suspend_date"),
    ],
)
def test_date_partition_uses_endpoint_date_argument(
    tmp_path, dataset, endpoint, date_arg
):
    client = FakePro([pd.DataFrame({"ts_code": ["000001.SZ"]})])
    provider = TushareProvider.create(config(tmp_path), client=client)

    provider.fetch(PartitionKey(dataset, "20260710"))

    assert client.calls == [
        (
            endpoint,
            {
                date_arg: "20260710",
                "fields": ENDPOINTS[dataset].fields,
                "limit": 5000,
                "offset": 0,
            },
        )
    ]


def test_full_page_is_followed_by_next_offset(tmp_path):
    first = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(2)]})
    second = pd.DataFrame({"ts_code": ["999999.SZ"]})
    client = FakePro([first, second])
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)

    out = provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert len(out) == 3
    assert [call[1]["offset"] for call in client.calls] == [0, 2]


def test_pagination_stops_only_after_short_page_and_deduplicates(tmp_path):
    client = FakePro(
        [
            pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"]}),
            pd.DataFrame({"ts_code": ["000002.SZ", "000003.SZ"]}),
            pd.DataFrame(columns=["ts_code"]),
        ]
    )
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)

    out = provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert out["ts_code"].tolist() == ["000001.SZ", "000002.SZ", "000003.SZ"]
    assert [call[1]["offset"] for call in client.calls] == [0, 2, 4]


def test_repeated_full_page_fails_closed(tmp_path):
    page = pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"]})
    client = FakePro([page, page.copy()])
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)

    with pytest.raises(PITSchemaError, match="daily.*repeated full page"):
        provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert len(client.calls) == 2


def test_trade_calendar_year_uses_explicit_date_range(tmp_path):
    client = FakePro(
        [
            pd.DataFrame(
                {
                    "cal_date": ["20260709", "20260710", "20260711"],
                    "is_open": [1, 1, 0],
                }
            )
        ]
    )
    provider = TushareProvider.create(config(tmp_path), client=client)

    out = provider.fetch(PartitionKey(Dataset.TRADE_CAL, "2026"))

    assert len(out) == 3
    assert client.calls[0] == (
        "trade_cal",
        {
            "start_date": "20260101",
            "end_date": "20261231",
            "exchange": "SSE",
            "fields": ENDPOINTS[Dataset.TRADE_CAL].fields,
            "limit": 5000,
            "offset": 0,
        },
    )


def test_stock_basic_combines_all_statuses_and_deduplicates_by_all_fields(tmp_path):
    listed = pd.DataFrame(
        {"ts_code": ["000001.SZ"], "name": ["Ping An"], "list_status": ["L"]}
    )
    delisted = pd.DataFrame(
        {"ts_code": ["000001.SZ"], "name": ["Old Name"], "list_status": ["D"]}
    )
    paused = listed.copy()
    client = FakePro([listed, delisted, paused])
    provider = TushareProvider.create(config(tmp_path), client=client)

    out = provider.fetch(PartitionKey(Dataset.STOCK_BASIC, "current"))

    assert out[["ts_code", "name"]].to_dict("records") == [
        {"ts_code": "000001.SZ", "name": "Ping An"},
        {"ts_code": "000001.SZ", "name": "Old Name"},
    ]
    assert [call[1]["list_status"] for call in client.calls] == ["L", "D", "P"]
    assert all(call[0] == "stock_basic" for call in client.calls)
    assert all(call[1]["fields"] == ENDPOINTS[Dataset.STOCK_BASIC].fields for call in client.calls)


def test_stock_basic_paginates_each_status_independently(tmp_path):
    client = FakePro(
        [
            pd.DataFrame({"ts_code": ["L1", "L2"]}),
            pd.DataFrame({"ts_code": ["L3"]}),
            pd.DataFrame({"ts_code": ["D1"]}),
            pd.DataFrame({"ts_code": ["P1"]}),
        ]
    )
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)

    out = provider.fetch(PartitionKey(Dataset.STOCK_BASIC, "current"))

    assert out["ts_code"].tolist() == ["L1", "L2", "L3", "D1", "P1"]
    assert [(call[1]["list_status"], call[1]["offset"]) for call in client.calls] == [
        ("L", 0),
        ("L", 2),
        ("D", 0),
        ("P", 0),
    ]


def test_namechange_all_has_no_date_filter_and_keeps_earlier_intervals(tmp_path):
    old_interval = {
        "ts_code": "000001.SZ",
        "name": "Old Name",
        "start_date": "19910101",
        "end_date": "20260710",
    }
    client = FakePro(
        [
            pd.DataFrame([old_interval, {**old_interval, "name": "Older Name"}]),
            pd.DataFrame([{**old_interval, "name": "Current Name"}]),
        ]
    )
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)

    out = provider.fetch(PartitionKey(Dataset.NAMECHANGE, "all"))

    assert out["start_date"].tolist() == ["19910101", "19910101", "19910101"]
    assert [call[1]["offset"] for call in client.calls] == [0, 2]
    assert all("start_date" not in call[1] and "end_date" not in call[1] for call in client.calls)


class ThrottleError(RuntimeError):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@pytest.mark.parametrize(
    "error",
    [
        ThrottleError("unavailable", status=429),
        RuntimeError("Too Many Requests"),
        RuntimeError("RATE LIMIT exceeded"),
        RuntimeError("接口访问频次受限"),
        RuntimeError("每分钟最多访问该接口"),
    ],
)
def test_throttle_signals_become_typed_error(tmp_path, error):
    client = FakePro([error])
    provider = TushareProvider.create(config(tmp_path), client=client)

    with pytest.raises(TushareRateLimitError) as raised:
        provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert str(raised.value) == "Tushare endpoint 'daily' rate limit exceeded"


def test_throttle_preserves_numeric_retry_after(tmp_path):
    client = FakePro([ThrottleError("rate limit secret-token", retry_after=2.5)])
    provider = TushareProvider.create(config(tmp_path), client=client)

    with pytest.raises(TushareRateLimitError) as error:
        provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert error.value.retry_after == 2.5
    assert_credential_safe(error.value)


def test_generic_sdk_failure_becomes_sanitized_endpoint_error(tmp_path):
    client = FakePro([RuntimeError("request failed for secret-token")])
    provider = TushareProvider.create(config(tmp_path), client=client)

    with pytest.raises(PITError) as error:
        provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))

    assert str(error.value) == "Tushare endpoint 'daily' request failed"
    assert_credential_safe(error.value)


def test_probe_requests_one_current_year_trade_calendar_row(tmp_path):
    year = str(datetime.now(ZoneInfo("Asia/Shanghai")).year)
    client = FakePro([pd.DataFrame({"cal_date": [f"{year}0101"]})])
    provider = TushareProvider.create(config(tmp_path), client=client)

    assert provider.probe() is None

    assert client.calls == [
        (
            "trade_cal",
            {
                "start_date": f"{year}0101",
                "end_date": f"{year}1231",
                "exchange": "SSE",
                "fields": ENDPOINTS[Dataset.TRADE_CAL].fields,
                "limit": 1,
                "offset": 0,
            },
        )
    ]


def test_probe_uses_china_year_at_los_angeles_new_year_boundary(tmp_path, monkeypatch):
    los_angeles_time = datetime(
        2026,
        12,
        31,
        8,
        30,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )

    class FixedDateTime:
        @classmethod
        def now(cls, timezone):
            assert timezone == ZoneInfo("Asia/Shanghai")
            return los_angeles_time.astimezone(timezone)

    class FixedHostDate:
        @classmethod
        def today(cls):
            return los_angeles_time.date()

    monkeypatch.setattr(
        "tradingagents.picker.tushare_provider.datetime",
        FixedDateTime,
        raising=False,
    )
    monkeypatch.setattr(
        "tradingagents.picker.tushare_provider.date",
        FixedHostDate,
        raising=False,
    )
    client = FakePro([pd.DataFrame({"cal_date": ["20270101"]})])
    provider = TushareProvider.create(config(tmp_path), client=client)

    provider.probe()

    assert los_angeles_time.year == 2026
    assert client.calls[0][1]["start_date"] == "20270101"
    assert client.calls[0][1]["end_date"] == "20271231"


@pytest.mark.parametrize("result", [RuntimeError("secret-token"), pd.DataFrame()])
def test_probe_permission_failure_names_endpoint_without_exposing_token(tmp_path, result):
    client = FakePro([result])
    provider = TushareProvider.create(config(tmp_path, token="secret-token"), client=client)

    with pytest.raises(PITConfigurationError, match="trade_cal") as error:
        provider.probe()

    assert_credential_safe(error.value)


def test_probe_preserves_typed_throttle_error(tmp_path):
    client = FakePro([ThrottleError("rate limit secret-token", retry_after=3)])
    provider = TushareProvider.create(config(tmp_path), client=client)

    with pytest.raises(TushareRateLimitError) as error:
        provider.probe()

    assert error.value.retry_after == 3
    assert str(error.value) == "Tushare endpoint 'trade_cal' rate limit exceeded"
    assert_credential_safe(error.value)
