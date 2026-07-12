from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .errors import (
    PITConfigurationError,
    PITError,
    PITSchemaError,
    TushareNotConfiguredError,
    TushareRateLimitError,
)
from .pit_config import PITConfig
from .pit_models import Dataset, PartitionKey


@dataclass(frozen=True)
class EndpointSpec:
    api_name: str
    date_arg: str | None
    fields: str


ENDPOINTS = {
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
}


class TushareProvider:
    def __init__(self, client: Any, page_size: int = 5000):
        self.client = client
        self.page_size = page_size

    @classmethod
    def create(
        cls,
        config: PITConfig,
        client: Any | None = None,
        page_size: int = 5000,
    ) -> TushareProvider:
        if not config.token:
            raise TushareNotConfiguredError(
                "Tushare PIT data requires the TUSHARE_TOKEN environment variable"
            )

        resolved_client = client
        if resolved_client is None:
            import_failed = False
            initialization_failed = False
            try:
                module = importlib.import_module("tushare")
                resolved_client = module.pro_api(config.token)
            except ImportError:
                import_failed = True
            except Exception:
                initialization_failed = True

            if import_failed:
                raise TushareNotConfiguredError(
                    'Tushare PIT support is unavailable; run pip install ".[pit]"'
                )
            if initialization_failed:
                raise PITConfigurationError("Tushare client initialization failed")

        return cls(resolved_client, page_size=page_size)

    def probe(self) -> None:
        spec = ENDPOINTS[Dataset.TRADE_CAL]
        year = str(datetime.now(ZoneInfo("Asia/Shanghai")).year)
        kwargs = {
            "start_date": f"{year}0101",
            "end_date": f"{year}1231",
            "exchange": "SSE",
            "fields": spec.fields,
            "limit": 1,
            "offset": 0,
        }
        probe_failed = False
        try:
            result = self._query(spec.api_name, **kwargs)
        except TushareRateLimitError:
            raise
        except PITError:
            probe_failed = True

        if probe_failed:
            raise PITConfigurationError(
                "Tushare endpoint 'trade_cal' probe failed; verify token permissions"
            )

        if not isinstance(result, pd.DataFrame) or result.empty:
            raise PITConfigurationError(
                "Tushare endpoint 'trade_cal' probe returned no rows; verify token permissions"
            )

    def fetch(self, key: PartitionKey) -> pd.DataFrame:
        spec = ENDPOINTS[key.dataset]

        if key.dataset is Dataset.STOCK_BASIC:
            frames = [
                self._fetch_pages(spec, {"list_status": status})
                for status in ("L", "D", "P")
            ]
            return self._concat_unique(frames)

        if key.dataset is Dataset.TRADE_CAL:
            query_args = {
                "start_date": f"{key.partition}0101",
                "end_date": f"{key.partition}1231",
                "exchange": "SSE",
            }
        elif spec.date_arg is not None:
            query_args = {spec.date_arg: key.partition}
        else:
            query_args = {}

        return self._fetch_pages(spec, query_args)

    def _fetch_pages(
        self,
        spec: EndpointSpec,
        query_args: dict[str, object],
    ) -> pd.DataFrame:
        offset = 0
        combined: pd.DataFrame | None = None

        while True:
            page = self._query(
                spec.api_name,
                **query_args,
                fields=spec.fields,
                limit=self.page_size,
                offset=offset,
            )
            if not isinstance(page, pd.DataFrame):
                raise PITSchemaError(
                    f"{spec.api_name} returned {type(page).__name__}, expected DataFrame"
                )

            previous_rows = 0 if combined is None else len(combined)
            combined = self._concat_unique([combined, page] if combined is not None else [page])
            if len(page) == self.page_size and len(combined) == previous_rows:
                raise PITSchemaError(
                    f"{spec.api_name} pagination returned a repeated full page at offset {offset}"
                )
            if len(page) < self.page_size:
                return combined
            offset += self.page_size

    def _query(self, endpoint: str, **kwargs: object) -> Any:
        try:
            return self.client.query(endpoint, **kwargs)
        except Exception as exc:
            throttled = self._is_throttle(exc)
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
                retry_after = None

        if throttled:
            raise TushareRateLimitError(
                f"Tushare endpoint '{endpoint}' rate limit exceeded",
                retry_after=retry_after,
            )
        raise PITError(f"Tushare endpoint '{endpoint}' request failed")

    @staticmethod
    def _concat_unique(frames: list[pd.DataFrame]) -> pd.DataFrame:
        return pd.concat(frames, ignore_index=True).drop_duplicates(ignore_index=True)

    @staticmethod
    def _is_throttle(exc: Exception) -> bool:
        statuses = [getattr(exc, "status", None), getattr(exc, "status_code", None)]
        response = getattr(exc, "response", None)
        if response is not None:
            statuses.append(getattr(response, "status_code", None))
        if any(str(status) == "429" for status in statuses):
            return True

        message = str(exc).casefold()
        return any(
            fragment in message
            for fragment in ("too many requests", "rate limit", "频次", "每分钟")
        )
