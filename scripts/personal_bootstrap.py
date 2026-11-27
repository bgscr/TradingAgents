"""Personal CURRENT cache preparation. Only the personal CLI uses this route."""

import hashlib
import http.client
import json
import os
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts.personal_full import local_path

BASE = "https://api.waditu.com/dataapi"
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "daily": "ts_code,trade_date,open,high,low,close,vol",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_type,suspend_timing",
}
FINANCE = ("balancesheet", "income", "cashflow")
SUPPLEMENTAL_FINANCE = ("margin_detail", "moneyflow_dc")


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


class PreparationError(ValueError):
    """Only local diagnostic codes may reach the CLI."""


def now():
    return datetime.now(timezone.utc)


def profile(config, root, keys):
    """Scope all mutable stores to this personal account, separately from installed data."""
    tools = ("get_balance_sheet", "get_income_statement", "get_cashflow")

    account = digest_bytes(("personal-tushare-v1\0" + keys.get("TUSHARE_TOKEN", "")).encode())
    base = local_path(root / "reports" / "personal" / ".data" / account)
    rates = [
        config[name]
        for name in ("tushare_calls_per_minute", "tushare_operator_safety_ceiling_calls_per_minute")
    ]
    if any(type(n) is not int or not 1 <= n <= 40 for n in rates):
        raise PreparationError("invalid_personal_rate")
    return dict(
        config,
        personal_store=str(base),
        llm_provider="deepseek",
        backend_url="https://api.deepseek.com/v1",
        quick_think_llm=os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash"),
        deep_think_llm=os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-pro"),
        market_data_vendors={"cn_a": {"core_stock_apis": "tushare"}},
        tool_vendors=dict.fromkeys(tools, "tushare"),
        market_history_mode="current",
        mainland_capability_routing_mode="legacy",
        data_usage_mode="personal_research",
        market_history_database_path=str(base / "history.sqlite3"),
        market_history_payload_root=str(base / "payloads"),
        market_history_backup_root=str(base / "backups"),
        data_cache_dir=str(base / "cache"),
        instrument_identity_registry_path=None,
        instrument_identity_registry_sha256=None,
        tushare_financial_https_endpoint=BASE,
        tushare_account_scope_label="personal-" + account,
        financial_cache_only=True,
        tushare_calls_per_minute=min(20, *rates),
        tushare_operator_safety_ceiling_calls_per_minute=min(20, *rates),
    )


@contextmanager
def exclusive(config):
    base = local_path(config["personal_store"])
    for suffix in ("", "-journal", "-shm", "-wal"):
        local_path(base / ("run-lock.sqlite3" + suffix))
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / "run-lock.sqlite3", timeout=0)
    try:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise PreparationError("personal_store_busy") from None
        yield
    finally:
        db.close()


def write_local(path, raw):
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


def post(token, endpoint, params, fields, remaining, admit):
    """One POST, fixed TLS origin, bounded streaming read. Never include upstream text in errors."""
    from tradingagents.dataflows.bank_identity_snapshot import decode_json
    from tradingagents.market_history.coordinator import (
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
    )

    if endpoint not in {*FIELDS, *FINANCE, *SUPPLEMENTAL_FINANCE, "stock_basic"}:
        raise PreparationError("unsupported_personal_endpoint")
    if endpoint == "moneyflow_dc":
        from scripts.personal_dc_ratio import FIELDS as ratio_fields

        if fields != ratio_fields or set(params) != {"ts_code", "trade_date"}:
            raise PreparationError("personal_request_scope_mismatch")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise PreparationError("certificate_verification_required")
    body = json.dumps(
        {
            "api_name": endpoint,
            "token": token,
            "params": dict(params, ts_type_name=BASE),
            "fields": fields,
        },
        allow_nan=False,
    ).encode()
    conn = http.client.HTTPSConnection(
        "api.waditu.com", timeout=min(30, remaining()), context=context
    )
    try:
        admit()
        conn.request(
            "POST",
            "/dataapi/" + endpoint,
            body=body,
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        )
        sock = conn.sock
        sock.settimeout(min(30, remaining()))
        response = conn.getresponse()
        if response.status == 429:
            from tradingagents.tushare_financial_https import _retry_after_seconds

            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.RATE_LIMITED,
                retryable=False,
                status_code=429,
                retry_after_seconds=_retry_after_seconds(response.getheader("Retry-After", "")),
                error_code="personal_rate_limited",
            )
        if endpoint in SUPPLEMENTAL_FINANCE and response.status in (401, 403):
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.AUTHENTICATION
                if response.status == 401
                else PhysicalAttemptOutcome.PERMISSION_DENIED,
                retryable=False,
                status_code=response.status,
                error_code="dc_ratio_access_rejected"
                if endpoint == "moneyflow_dc"
                else "margin_access_rejected",
            )
        if response.status != 200:
            raise PreparationError("provider_http_rejected")
        raw = bytearray()
        # A server may close after headers, detaching its socket from HTTPConnection.
        # HTTPResponse keeps the same open socket through its buffered file.
        while True:
            sock.settimeout(min(30, remaining()))
            limit = 16384 if endpoint == "moneyflow_dc" else 16 * 1024 * 1024
            block = response.read1(min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
            if len(raw) > limit:
                raise PreparationError("provider_response_too_large")
        remaining()
        if endpoint in SUPPLEMENTAL_FINANCE:
            return bytes(raw)  # Preserve numeric tokens for the source-specific decimal decoders.
        result = decode_json(bytes(raw))
        if type(result) is not dict or type(result.get("code")) is not int or result["code"] != 0:
            raise PreparationError("provider_business_rejected")
        return result
    finally:
        conn.close()


class Session:
    def __init__(self, config, started):
        from tradingagents.market_history.config import MarketHistoryConfig
        from tradingagents.market_history.coordinator import upstream_service_identity_for_provider

        self.account_scope = config.get("tushare_account_scope_label")
        self.fetch_dc_ratio = config.get("personal_fetch_dc_ratio") is True
        self.maximum = config["analysis_max_calls"]
        duration = config["analysis_timeout_seconds"]
        if (
            type(self.maximum) is not int
            or self.maximum < 1
            or type(duration) is not int
            or duration < 1
        ):
            raise PreparationError("invalid_personal_budget")
        self.maximum = min(60, self.maximum)
        self.deadline = started[1] + min(1200, duration)
        self.wall_deadline = started[0] + min(1200, duration)
        self.counts = {}
        self.history = MarketHistoryConfig.from_mapping(config)
        self.service, self.name = upstream_service_identity_for_provider("tushare")
        self.interval = timedelta(seconds=60 / config["tushare_calls_per_minute"])
        self.stage = "preparation"
        self.symbol = None
        self.day = None
        self.factor_start = None

    def validate(self, endpoint, params, fields):
        from tradingagents.dataflows.bank_identity_snapshot import exact_query

        if not self.symbol:
            raise PreparationError("unbound_personal_request")
        code = self.symbol.replace(".SS", ".SH")
        exchange = "SSE" if self.symbol.endswith(".SS") else "SZSE"
        if endpoint == "stock_basic":
            fixed = exact_query(code)
            valid = params == fixed["params"] and fields == fixed["fields"]
        elif endpoint == "trade_cal":
            from dateutil.relativedelta import relativedelta

            first = self.today - relativedelta(years=5) - timedelta(days=32)
            valid = fields == FIELDS[endpoint] and params == {
                "exchange": exchange,
                "start_date": first.strftime("%Y%m%d"),
                "end_date": self.today.strftime("%Y%m%d"),
            }
        elif self.day is not None and endpoint in SUPPLEMENTAL_FINANCE:
            from scripts.personal_dc_ratio import FIELDS as ratio_fields
            from tradingagents.margin_balance import FIELDS as margin_fields

            valid = (
                self.day < now().astimezone(ZoneInfo("Asia/Shanghai")).date()
                and (endpoint != "moneyflow_dc" or self.fetch_dc_ratio)
                and fields
                == (ratio_fields if endpoint == "moneyflow_dc" else ",".join(margin_fields))
                and params
                == {
                    "ts_code": code,
                    "trade_date": self.day.strftime("%Y%m%d"),
                }
            )
        elif self.day is not None and endpoint in {"daily", "adj_factor", "suspend_d"}:
            start = self.factor_start if endpoint == "adj_factor" else self.day
            valid = fields == FIELDS[endpoint] and params == {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": self.day.strftime("%Y%m%d"),
            }
        elif (
            self.day is not None
            and endpoint in FINANCE
            and set(params) == {"ts_code", "report_type", "start_date", "end_date"}
        ):
            first, last = (
                datetime.strptime(params[key], "%Y%m%d").date()
                for key in ("start_date", "end_date")
            )
            valid = (
                fields == ""
                and params["ts_code"] == code
                and params["report_type"] == "1"
                and first.strftime("%Y%m%d") == params["start_date"]
                and last.strftime("%Y%m%d") == params["end_date"]
                and date(self.day.year - 6, 1, 1) <= first <= last <= self.day <= self.today
            )
        else:
            valid = False
        if not valid:
            raise PreparationError("personal_request_scope_mismatch")

    def remaining(self):
        seconds = min(self.deadline - time.monotonic(), self.wall_deadline - time.time())
        if seconds <= 0:
            raise PreparationError("personal_deadline_reached")
        return seconds

    def wait(self, seconds):
        if seconds >= self.remaining():
            raise PreparationError("personal_deadline_reached")
        time.sleep(seconds)

    def financial_attempts(self):
        return sum(self.counts.get(k, 0) for k in (*FINANCE, *SUPPLEMENTAL_FINANCE))

    def query(self, token, endpoint, params, fields, *, observation_binding=None):
        from tradingagents.market_history.coordinator import (
            ProviderRequestCoordinator,
            RequestPriority,
        )
        from tradingagents.market_history.store import MarketHistoryStore

        request_key = "personal:" + uuid4().hex
        if endpoint in SUPPLEMENTAL_FINANCE:
            import re

            from scripts import personal_dc_ratio
            from scripts.personal_observations import margin_key

            if (
                type(observation_binding) is not dict
                or not self.account_scope
                or self.day is None
                or set(observation_binding)
                != {"symbol", "trade_date", "as_of", "identity_sha256", "snapshot_id"}
                or observation_binding["symbol"] != self.symbol.replace(".SS", ".SH")
                or observation_binding["trade_date"] != self.day.isoformat()
                or observation_binding["as_of"] != self.day.isoformat()
                or not re.fullmatch(r"[a-f0-9]{64}", observation_binding["identity_sha256"])
                or not re.fullmatch(r"snapshot:v2:[a-f0-9]{64}", observation_binding["snapshot_id"])
            ):
                raise PreparationError("unbound_personal_request")
            if (
                not isinstance(token, str)
                or "personal-" + digest_bytes(("personal-tushare-v1\0" + token).encode())
                != self.account_scope
            ):
                raise PreparationError("personal_account_scope_mismatch")
            make_key = personal_dc_ratio.request_key if endpoint == "moneyflow_dc" else margin_key
            request_key = make_key(observation_binding, self.account_scope)
        self.validate(endpoint, params, fields)

        def ensure_capacity():
            self.remaining()
            if (
                sum(self.counts.values()) >= self.maximum
                or self.counts.get(endpoint, 0)
                or endpoint in (*FINANCE, *SUPPLEMENTAL_FINANCE)
                and self.financial_attempts() >= 3
            ):
                raise PreparationError("personal_attempt_limit")

        ensure_capacity()
        self.stage = endpoint

        def admit():
            from tradingagents.market_history.coordinator import record_active_physical_attempt_io

            ensure_capacity()
            record_active_physical_attempt_io()
            self.counts[endpoint] = 1

        def send():
            raw = post(token, endpoint, params, fields, self.remaining, admit)
            if endpoint == "moneyflow_dc":
                return personal_dc_ratio.project(raw, observation_binding)
            if endpoint == "margin_detail":
                from scripts.personal_observations import project_margin
                from tradingagents.market_history.coordinator import (
                    PhysicalAttemptFailure,
                    PhysicalAttemptOutcome,
                )
                from tradingagents.moneyflow import MoneyflowUnavailable

                try:
                    return project_margin(raw, observation_binding)
                except MoneyflowUnavailable as exc:
                    denied = str(exc) == "permission_denied"
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.PERMISSION_DENIED
                        if denied
                        else PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                        retryable=False,
                        error_code="margin_date_mismatch"
                        if exc.rejection == "date_window"
                        else "margin_response_rejected",
                    ) from None
            return raw

        with MarketHistoryStore.open_provider_request_authority(self.history) as store:
            coord = ProviderRequestCoordinator(store)
            coord.register_upstream_service(self.service, self.name)
            ceiling = coord.configured_operator_ceiling(self.service)
            if ceiling is None:
                coord.configure_operator_ceiling(
                    upstream_service_id=self.service,
                    minimum_interval=self.interval,
                    allow_prewarming=False,
                    configured_at=now(),
                )
            elif ceiling.allow_prewarming or ceiling.minimum_interval < self.interval:
                raise PreparationError("personal_rate_policy_mismatch")
            result = coord.execute_physical_workflow(
                request_key=request_key,
                upstream_service_id=self.service,
                owner_id="personal:" + uuid4().hex,
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=now,
                sleep=self.wait,
                lease_duration=timedelta(minutes=2),
                max_physical_operations=1,
                wait_for_initial_pacing=True,
                cooldown_scope="market-snapshot",
                workflow=lambda execute: execute(endpoint, send),
            )
            return result.value


def identity(config, ticker, instant, session=None, token=None):
    from tradingagents.dataflows import bank_identity_snapshot as ids

    path = local_path(Path(config["personal_store"]) / "identity" / (ticker + ".json"))
    query = ids.exact_query(ticker)

    def project(response, stamp):
        obs = ids.parse_exact_response(response, query, stamp, equity=True)
        row = ids._rows(obs["partition"])[0]
        source = digest_bytes(ids.json_bytes(obs))
        registry = {
            "schema_version": "1.0",
            "registry_id": "personal-equity:" + source,
            "rows": [
                {
                    "canonical_symbol": ticker,
                    "venue": "XSHG" if row["exchange"] == "SSE" else "XSHE",
                    "instrument_kind": "equity",
                    "currency": "CNY",
                    "provenance": {
                        "provider": "Tushare CURRENT stock_basic",
                        "source_ref": "stock-basic:" + source,
                        "retrieved_at": ids.utc_text(stamp),
                    },
                }
            ],
        }
        return ids.json_bytes(registry), datetime.strptime(row["list_date"], "%Y%m%d").date()

    document = None
    if path.exists():
        record = ids.decode_json(ids.read_bound(path, maximum=ids.MAX_RESPONSE_BYTES))
        stamp = ids.instant(record["observed"])
        if stamp > instant or digest_bytes(ids.json_bytes(record["response"])) != record["digest"]:
            raise PreparationError("personal_identity_unverified")
        if instant - stamp < timedelta(days=1):
            document, listed = project(record["response"], stamp)
    if document is None:
        if session is None:
            return None
        response = session.query(token, "stock_basic", query["params"], query["fields"])
        stamp = now()
        document, listed = project(response, stamp)
        # Store only verified fields, never server messages, echoed tokens or extra metadata.
        safe = {
            "code": 0,
            "data": {
                "fields": response["data"]["fields"],
                "items": response["data"]["items"],
                "has_more": False,
            },
        }
        write_local(
            path,
            ids.json_bytes(
                {
                    "response": safe,
                    "digest": digest_bytes(ids.json_bytes(safe)),
                    "observed": ids.utc_text(stamp),
                }
            ),
        )
    digest = digest_bytes(document)
    pin = local_path(path.with_name(digest + ".registry.json"))
    if not pin.exists() and session is not None:
        write_local(pin, document)
    if not pin.is_file() or ids.read_bound(pin, digest, maximum=16384) != document:
        raise PreparationError("personal_identity_pin_missing")
    config.update(
        instrument_identity_registry_path=str(pin), instrument_identity_registry_sha256=digest
    )
    return listed


def frame(response, fields, *, finance=False):
    import pandas as pd

    from tradingagents.financial_response_completeness import (
        inspect_response_completeness,
        inspect_response_values,
    )

    completeness = inspect_response_completeness(response)
    data = response["data"]
    if fields and set(data["fields"]) != set(fields.split(",")):
        raise PreparationError("provider_fields_mismatch")
    values = inspect_response_values(data["fields"], data["items"])
    value = pd.DataFrame(data["items"], columns=data["fields"], dtype=object)
    if finance:
        value.attrs.update(response_completeness=completeness, response_values=values)
    return value


def prepare_market(config, ticker, token, session, listed):
    from dateutil.relativedelta import relativedelta

    from scripts.personal_full import cached_close, selected_session
    from tradingagents.dataflows import tushare_market_history as market
    from tradingagents.market_history.current_cache import (
        bind_prepared_publication,
        read_prepared_exchange_calendar,
    )
    from tradingagents.market_history.current_incremental import load_current_history
    from tradingagents.market_history.store import MarketHistoryStore

    today = session.today
    exchange = "SSE" if ticker.endswith(".SS") else "SZSE"
    code = ticker.replace(".SS", ".SH")
    first = today - relativedelta(years=5) - timedelta(days=32)
    with MarketHistoryStore.open(session.history) as store:
        from tradingagents.market_history.current_cache import prepared_exchange_calendar_provider

        # Authenticate the latest stored revision even when a later session extends its range.
        provider = prepared_exchange_calendar_provider(exchange)
        bounds = store._connection.execute(
            "SELECT MIN(session_date), MAX(session_date) FROM market_sessions WHERE calendar_revision_id=("
            "SELECT calendar_revision_id FROM market_session_calendars WHERE provider_dataset_id=? "
            "ORDER BY observed_at DESC,calendar_revision_id DESC LIMIT 1)",
            (provider.provider_dataset_id,),
        ).fetchone()
        cal = None
        if bounds[0] is not None:
            prior = read_prepared_exchange_calendar(
                store,
                ticker,
                date.fromisoformat(bounds[0]),
                date.fromisoformat(bounds[1]),
                config=config,
            )
            if prior.observed_at > now():
                raise PreparationError("future_calendar_observation")
            if (
                bounds[0] <= (today - relativedelta(years=5)).isoformat()
                and bounds[1] >= today.isoformat()
            ):
                cal = prior
        if cal is None or not timedelta(0) <= now() - cal.observed_at < timedelta(days=1):
            params = {
                "exchange": exchange,
                "start_date": first.strftime("%Y%m%d"),
                "end_date": today.strftime("%Y%m%d"),
            }
            result = frame(
                session.query(token, "trade_cal", params, FIELDS["trade_cal"]), FIELDS["trade_cal"]
            )

            class CalendarPage:
                def fetch_range(self, endpoint, *, request_executor, **kwargs):
                    if endpoint != "trade_cal" or kwargs != params:
                        raise PreparationError("calendar_scope_mismatch")
                    return request_executor(endpoint, lambda: result)

            publication = market.load_calendar(
                ticker, first.isoformat(), today.isoformat(), provider=CalendarPage()
            )
            from dataclasses import replace

            store.publish_session_calendar(
                bind_prepared_publication(replace(publication, observed_at=now()), exchange)
            )
            cal = read_prepared_exchange_calendar(store, ticker, first, today, config=config)
        day = selected_session(cal, now())
        if listed > day:
            raise PreparationError("stock_not_listed_on_selected_date")
        session.day = day
        if not cached_close(
            store,
            ticker,
            cal,
            day,
            now(),
            config.get("market_history_current_cache_max_age_seconds", 86400),
        ):
            encoded = day.strftime("%Y%m%d")
            day_params = {"ts_code": code, "start_date": encoded, "end_date": encoded}
            binding, instrument = market.history_binding(ticker)
            retained = store.load_current_history(binding, instrument)
            factor_start = min({day, *retained.raw}) if retained else day
            session.factor_start = factor_start
            tables = []
            for endpoint in ("daily", "adj_factor", "suspend_d"):
                params = dict(day_params)
                if endpoint == "adj_factor":
                    params.update(start_date=factor_start.strftime("%Y%m%d"))
                value = frame(
                    session.query(token, endpoint, params, FIELDS[endpoint]), FIELDS[endpoint]
                )
                if endpoint in {"daily", "adj_factor"} and value.empty:
                    raise PreparationError("stock_close_unavailable")
                for row in value.to_dict("records"):
                    if (
                        row["ts_code"] != code
                        or not params["start_date"] <= row["trade_date"] <= encoded
                    ):
                        raise PreparationError("market_response_scope_mismatch")
                tables.append(value)
            import pandas as pd

            calendar_row = pd.DataFrame(
                [
                    row
                    for row in json.loads(cal.raw_payload)["calendar"]
                    if row["cal_date"] == encoded
                ]
            )
            # Membership is read back from the verified upstream calendar, never inferred from a weekday.
            if not any(
                row.session_date == day and row.status.value == "open" for row in cal.sessions
            ):
                raise PreparationError("selected_session_not_open")
            candidate = market._candidate(
                *tables,
                calendar_row,
                binding=binding,
                instrument=instrument,
                ts_code=code,
                exchange=exchange,
                start=day,
                end=day,
                observed_at=now(),
            )

            def acquire(begin, end, anchor):
                if (begin, end, anchor) != (str(day), str(day), str(day)):
                    raise PreparationError("unexpected_market_range")
                return candidate

            def day_calendar(begin, end):
                if begin != end or begin != str(day):
                    raise PreparationError("unexpected_calendar_range")
                return candidate.calendar

            load_current_history(
                store,
                ticker,
                str(day),
                str(day),
                acquire=acquire,
                acquire_calendar=day_calendar,
                force_refresh=True,
                now=now(),
            )
            if not cached_close(store, ticker, cal, day, now(), 86400):
                raise PreparationError("stock_close_unverified")
        return day


def prepare_statements(config, ticker, day, token, session):
    from tradingagents.budget_readiness import _financial_identity, _financial_payloads
    from tradingagents.dataflows._tushare_statement_artifacts import encode_provider_artifact
    from tradingagents.dataflows.current_statement_cache import (
        CurrentStatementCache,
        StatementCacheChunk,
        StatementCacheScope,
        _gaps,
    )
    from tradingagents.dataflows.current_tushare_statements import NORMALIZER_VERSION
    from tradingagents.dataflows.financial_contracts import FinancialStatementType
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity

    ident = _financial_identity(config, ticker, None)
    revision = ProviderSubrequestInstrumentIdentity.from_authoritative(ident).identity_revision
    cache = CurrentStatementCache(
        Path(config["data_cache_dir"]) / "current-financial-statements.sqlite3",
        max_age=timedelta(seconds=config["tushare_financial_cache_max_age_seconds"]),
    )
    code = ticker.replace(".SS", ".SH")
    for endpoint, kind in zip(
        FINANCE,
        (
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.INCOME_STATEMENT,
            FinancialStatementType.CASH_FLOW,
        ),
        strict=True,
    ):
        scope = StatementCacheScope(
            config["tushare_account_scope_label"],
            revision,
            endpoint,
            digest_bytes(BASE.encode()),
            NORMALIZER_VERSION,
        )

        def acquire(first, last, *, endpoint=endpoint, kind=kind):
            params = {
                "ts_code": code,
                "start_date": first.strftime("%Y%m%d"),
                "end_date": last.strftime("%Y%m%d"),
                "report_type": "1",
            }
            value = frame(session.query(token, endpoint, params, ""), "", finance=True)
            if not {"ts_code", "ann_date", "end_date"} <= set(value.columns):
                raise PreparationError("statement_columns_missing")
            for row in value.to_dict("records"):
                if (
                    row["ts_code"] != code
                    or not params["start_date"] <= row["ann_date"] <= params["end_date"]
                    or str(row.get("report_type", "1")) != "1"
                ):
                    raise PreparationError("statement_scope_mismatch")
            stamp = now()
            payload = encode_provider_artifact(
                endpoint_id=endpoint,
                frame=value,
                retrieved_at=stamp,
                observed_at=stamp,
                adapter_metadata=value.attrs,
            )
            _financial_payloads(
                (StatementCacheChunk(first, last, payload, stamp, digest_bytes(payload)),),
                endpoint,
                ident,
                kind,
                stamp,
            )
            return payload

        start = date(day.year - 6, 1, 1)
        stamp = now()
        chunks, failure = cache._load(scope.key, start, day)
        if failure or any(chunk.checked_at > stamp for chunk in chunks):
            raise PreparationError("statement_cache_unverified")
        fresh = [
            (c.range_start, c.range_end) for c in chunks if stamp - c.checked_at < cache.max_age
        ]
        if len(_gaps(start, day, fresh)) > 1:
            # Two independent ranges do not authorize a second call or replacing a fresh interior.
            raise PreparationError("disjoint_statement_gaps")
        result = cache.read_through(scope, start, day, stamp, acquire)
        if result.failure or result.cache_failure or result.missing_ranges or result.stale_ranges:
            raise PreparationError("financial_coverage_unavailable")


def hydrate(config, ticker, token, session):
    from scripts.personal_full import check_store_paths
    from scripts.personal_graph import local_payloads
    from tradingagents.analysis_budget import active_budget
    from tradingagents.full_session_ledger import owner
    from tradingagents.market_history.current_cache import compatibility_diagnostic

    if owner() is not None or active_budget() is not None:
        raise PreparationError("personal_requires_independent_session")
    check_store_paths(config)
    if compatibility_diagnostic() is not None:
        raise PreparationError("market_sources_unverified")
    from tradingagents.dataflows.tushare_market_history import _instrument

    if session.symbol is not None or _instrument(ticker).yahoo_symbol != ticker:
        raise PreparationError("personal_session_identity_mismatch")
    session.symbol = ticker
    session.today = now().astimezone(ZoneInfo("Asia/Shanghai")).date()
    with local_payloads():
        listed = identity(config, ticker, now(), session, token)
        day = prepare_market(config, ticker, token, session, listed)
        prepare_statements(config, ticker, day, token, session)
    session.remaining()
    return day
