"""Bounded personal Full evidence. No installed release state or report replay inputs."""

import http.client
import ssl
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from scripts import personal_bootstrap as cold

SOURCES = ("news", "margin_balance", "eastmoney_fundflow_ratio")
SHANGHAI = ZoneInfo("Asia/Shanghai")


MONEYFLOW_DC_MINIMUM_POINTS = 5000  # Tushare API document 349.


def dc_account_access(meets_minimum):
    """Describe a local declaration without querying the account or granting access."""
    status = {True: "declared_at_or_above_minimum", False: "below_minimum", None: "undeclared"}
    return {
        "endpoint": "moneyflow_dc",
        "minimum_points": MONEYFLOW_DC_MINIMUM_POINTS,
        "points_status": status[meets_minimum],
        "entitlement_checked": False,
    }


def scope(config):
    selected = config.get("personal_sources", SOURCES)
    return {
        "evidence_preset": config.get("personal_mode", "full"),
        "full_data_acceptance": "unverified",
        "news_evidence_scope": "publication_index_only",
        "news_content_verified": False,
        "moneyflow_dc_access": dc_account_access(config.get("personal_dc_meets_minimum")),
        "ratio_policy": "cache_or_one_account_query"
        if config.get("personal_fetch_dc_ratio") is True
        else "cache_only",
        "enhancements": {key: "not_run" if key in selected else "not_selected" for key in SOURCES},
    }


def get_page(ticker, day, remaining, admit):
    """One credential-free TLS GET. The project builds the query; no redirects or article GETs."""
    from tradingagents.dataflows.eastmoney_news import MAX_BODY_BYTES
    from tradingagents.powershell_news import make_request

    target = urlsplit(make_request(ticker, day).url)
    host = "search-api-web.eastmoney.com"
    if (target.scheme, target.netloc, target.path, target.fragment) != (
        "https",
        host,
        "/search/jsonp",
        "",
    ):
        raise ValueError("invalid_news_destination")
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    conn = http.client.HTTPSConnection(host, timeout=min(15, remaining()), context=context)
    try:
        admit()
        conn.request(
            "GET", target.path + "?" + target.query, headers={"Accept-Encoding": "identity"}
        )
        sock = conn.sock
        sock.settimeout(min(15, remaining()))
        response = conn.getresponse()
        if response.status in (401, 403):
            raise PermissionError
        if (
            response.status != 200
            or response.getheader("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError("news_response_unavailable")
        raw = bytearray()
        while len(raw) <= MAX_BODY_BYTES:
            sock.settimeout(min(15, remaining()))
            block = response.read1(min(65536, MAX_BODY_BYTES + 1 - len(raw)))
            if not block:
                remaining()
                return bytes(raw)
            raw.extend(block)
        raise ValueError("news_response_too_large")
    finally:
        conn.close()


def news_page(raw, ticker, day, observed):
    from tradingagents.dataflows.eastmoney_news import NewsResponseError, parse_news_page
    from tradingagents.powershell_news import make_request

    request = make_request(ticker, day)
    try:
        page = parse_news_page(
            raw,
            expected_callback=request.callback,
            ticker=ticker,
            start_date=request.start,
            end_date=day,
            project_only=True,
            isolate_invalid_sources=True,
        )
    except NewsResponseError as exc:
        counts = exc.source_selection
        if exc.reason == "no_usable_articles" and counts is not None:
            if counts.returned == 0:
                return {
                    "status": "valid_empty",
                    "coverage": "one_page",
                    "observed_at": observed.isoformat(),
                }
            if counts.outside_window == counts.returned:
                return {"status": "date_mismatch"}
        return {"status": "unverified"}
    articles = []
    for row in page.rows:
        published = datetime.fromisoformat(row["date"]).replace(tzinfo=SHANGHAI)
        if published > observed:
            return {"status": "date_mismatch"}
        # Only verifier-built numeric article references and times leave this boundary.
        articles.append({"source": row["url"], "published_at": published.isoformat()})
    return {
        "status": "verified",
        "coverage": "one_page",
        "observed_at": observed.isoformat(),
        "window_start": request.start,
        "window_end": day,
        "articles": articles,
        "excluded": page.source_selection.as_dict(),
        "historical_visibility_verified": False,
    }


class PersonalNews:
    def __init__(self, ticker, day, session, selected, allowed):
        self.ticker, self.day, self.session = ticker, day, session
        self.lock = Lock()
        self.result = None
        if not selected or not allowed:
            self.result = {
                "status": "not_selected" if not selected else "permission_not_configured"
            }
        self.returned_to_news_tool = False

    def collect(self, admit):
        with self.lock:
            if self.result is None:
                self.result = {"status": "unavailable"}
                try:
                    raw = get_page(self.ticker, self.day, self.session.remaining, admit)
                    self.result = news_page(raw, self.ticker, self.day, cold.now())
                except PermissionError:
                    self.result = {"status": "permission_denied"}
                except cold.PreparationError:
                    self.result = {"status": "budget_unavailable"}
                except (ValueError, OSError, http.client.HTTPException):
                    pass
            return self.result

    def graph_metadata(self, symbol, start, end, *, instrument_identity=None):
        from contextlib import ExitStack

        from tradingagents.analysis_budget import (
            active_budget,
            admitted_transport,
            require_data_vendor,
        )
        from tradingagents.dataflows.errors import NoMarketDataError

        first = (date.fromisoformat(self.day) - timedelta(days=7)).isoformat()
        try:
            dates_valid = all(
                isinstance(x, str) and date.fromisoformat(x).isoformat() == x for x in (start, end)
            )
        except ValueError:
            dates_valid = False
        if (
            not dates_valid
            or not isinstance(symbol, str)
            or symbol.replace(".SH", ".SS") != self.ticker
            or not first <= start <= end <= self.day
            or active_budget() is None
            or instrument_identity is None
            or not instrument_identity.is_authoritative
            or instrument_identity.symbol.replace(".SH", ".SS") != self.ticker
        ):
            raise NoMarketDataError(symbol, self.ticker, "news_request_unverified")
        with ExitStack() as stack:

            def admit():
                require_data_vendor("akshare")
                if stack.enter_context(admitted_transport("data", optional=True)) is None:
                    raise cold.PreparationError("optional_budget_unavailable")

            result = self.collect(admit)
        records = [
            item for item in result.get("articles", []) if start <= item["published_at"][:10] <= end
        ]
        if not records:
            raise NoMarketDataError(symbol, self.ticker, "news_metadata_unavailable")
        # get_news / sentiment_news require substantive article evidence. A verified
        # publication index stays in the run receipt and cannot satisfy either capability.
        from tradingagents.dataflows.acquisition import (
            AcquisitionFailure,
            AcquisitionUnavailableReason,
        )

        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.NO_DATA,
            error_code="personal_news_index_only",
            retryable=False,
        )


def margin_key(binding, account):
    from tradingagents import margin_balance as margin, moneyflow

    return "margin_detail:" + moneyflow._digest(
        {
            "contract": margin.VERSION,
            "binding": binding,
            "fields": margin.FIELDS,
            "destination": moneyflow.HOST + margin.PATH,
            "account_scope": account,
        }
    )


def project_margin(raw, binding):
    from tradingagents import margin_balance as margin, moneyflow

    rows = margin.decode(raw, binding)
    return (
        rows,
        moneyflow._projection_digest("margin_detail", binding, rows),
        cold.now().astimezone(timezone.utc).isoformat(),
    )


def margin_observation(session, binding, token, account, authority):
    from tradingagents import margin_balance as margin, moneyflow
    from tradingagents.market_history.coordinator import PhysicalAttemptBudgetExhausted
    from tradingagents.retrospective_observations import retrospective_observation

    day = date.fromisoformat(binding["trade_date"])
    key = margin_key(binding, account)

    def received(value):
        if type(value) is not tuple or len(value) != 3 or not isinstance(value[2], str):
            return None
        stamp = datetime.fromisoformat(value[2])
        if stamp.utcoffset() != timedelta(0) or not timedelta(0) <= cold.now() - stamp < timedelta(
            days=1
        ):
            return None
        if value[0] == []:
            return (
                value[2]
                if value[1] == moneyflow._projection_digest("margin_detail", binding, [])
                else None
            )
        return margin.cached_receipt_time(value, binding)

    value = retrospective_observation(
        authority,
        request_key=key,
        provider="tushare",
        operation="margin_detail",
        as_of=day,
        receipt_time=received,
    )
    source = "coordinator_cache"
    if value is None:
        source = "account_request"
        if day >= cold.now().astimezone(SHANGHAI).date():
            return {"status": "publication_pending", "rule": "retrospective_window_not_open"}
        if session.financial_attempts() >= 3:
            return {"status": "financial_budget_unavailable"}
        try:
            value = session.query(
                token,
                "margin_detail",
                {"ts_code": binding["symbol"], "trade_date": day.strftime("%Y%m%d")},
                ",".join(margin.FIELDS),
                observation_binding=binding,
            )
        except PhysicalAttemptBudgetExhausted as exc:
            status = exc.failure.outcome.value
            if exc.failure.error_code == "margin_date_mismatch":
                status = "date_mismatch"
            return {
                "status": status
                if status
                in {"date_mismatch", "permission_denied", "authentication", "rate_limited"}
                else "unverified"
            }
    if received(value) is None:
        return {"status": "unverified"}
    rows, _, stamp = value
    return {
        "status": "verified" if rows else "valid_empty",
        "acquisition": source,
        "observed_at": stamp,
        "trade_date": day.isoformat(),
        **({"rzye_yuan": rows[0][1]} if rows else {}),
    }


def ratio_observation(binding, authority):
    from tradingagents import powershell_fundflow as flow
    from tradingagents.evidence import stable_acquisition_source_ref
    from tradingagents.retrospective_observations import retrospective_observation

    day = date.fromisoformat(binding["trade_date"])
    operation = "stock_individual_fund_flow.powershell"
    key = stable_acquisition_source_ref(
        "financial-provider-physical-request",
        "full-pair-v1:" + flow._digest(binding),
        "1",
        operation,
    )
    result = {"status": "cache_missing_or_unverified"}

    def received(value):
        if type(value) is not dict or set(value) != {
            "projection",
            "received_at",
            "normalized_sha256",
        }:
            return None
        projection = value["projection"]
        if type(projection) is not dict or set(projection) != {"rows", "counts"}:
            return None
        obs = dict(
            contract=flow.VERSION,
            symbol=binding["symbol"],
            as_of=day.isoformat(),
            start=(day - timedelta(days=7)).isoformat(),
            identity_sha256=binding["identity_sha256"],
            provider="eastmoney.push2his",
            transport="powershell",
            endpoint=flow.ENDPOINT,
            received_at=value["received_at"],
            provenance="retrospective_backfill",
            amounts_and_prices="excluded_unit_unverified",
            ratio_unit="percent",
            status="available_partial",
            normalized_sha256=value["normalized_sha256"],
            **projection,
        )
        if (
            flow.render_observation(obs, binding["symbol"], day.isoformat())
            == flow._INVALID_OBSERVATION
            or obs["counts"]["duplicate"]
        ):
            return None
        matches = [row for row in obs["rows"] if row[0] == day.isoformat()]
        if len(matches) != 1:
            return None
        result.update(
            status="verified",
            trade_date=day.isoformat(),
            main_net_percent=matches[0][1],
            observed_at=obs["received_at"],
            acquisition="coordinator_cache",
        )
        return obs["received_at"]

    proof = retrospective_observation(
        authority,
        request_key=key,
        provider="akshare",
        operation="financial-fundamentals:" + operation,
        as_of=day,
        receipt_time=received,
        with_receipt=True,
    )
    if proof is None:
        return {
            "status": "publication_pending"
            if day >= cold.now().astimezone(SHANGHAI).date()
            else "cache_missing_or_unverified"
        }
    return result


def snapshot_binding(state, ticker, day):
    from tradingagents import margin_balance

    try:
        binding = margin_balance._binding(state, ticker, day, day)
        margin_balance._require_active_snapshot(state)
        return binding
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def supplement(config, binding, ticker, day, receipt, session, news, token):
    from scripts.personal_full import local_path
    from tradingagents.market_history.config import provider_request_authority_config
    from tradingagents.market_history.coordinator import PhysicalAttemptBudgetExhausted

    statuses = receipt["enhancements"]
    details = receipt.setdefault("source_evidence", {})
    # The graph budget is finalized. One admission account covers all remaining operations.
    session.maximum -= receipt["admitted_attempts"]
    if "news" in config["personal_sources"]:

        def admit_news():
            session.remaining()
            if sum(session.counts.values()) >= session.maximum or session.counts.get("public_news"):
                raise cold.PreparationError("personal_attempt_limit")
            session.counts["public_news"] = 1

        details["news"] = dict(
            news.collect(admit_news), returned_to_news_tool=news.returned_to_news_tool
        )
        statuses["news"] = details["news"]["status"]
    for key, reader in (
        ("margin_balance", margin_observation),
        ("eastmoney_fundflow_ratio", ratio_observation),
    ):
        if key not in config["personal_sources"]:
            continue
        if key == "eastmoney_fundflow_ratio" and not config["personal_public_news_allowed"]:
            statuses[key] = "permission_not_configured"
            details[key] = {"status": statuses[key], "used_for_rating": False}
            continue
        try:
            session.remaining()
            if binding is None:
                raise ValueError("active_snapshot_unverified")
            authority = provider_request_authority_config(session.history)
            local_path(authority.database_path)
            value = (
                reader(session, binding, token, config["tushare_account_scope_label"], authority)
                if key == "margin_balance"
                else reader(binding, authority)
            )
            if key == "eastmoney_fundflow_ratio" and value["status"] == "verified":
                age = cold.now() - datetime.fromisoformat(value["observed_at"])
                if not timedelta(0) <= age < timedelta(days=1):
                    value = {"status": "cache_stale"}
            if key == "eastmoney_fundflow_ratio" and value["status"] in {
                "cache_missing_or_unverified",
                "cache_stale",
            }:
                from scripts import personal_dc_ratio

                account_value = personal_dc_ratio.observe(
                    session, binding, token, authority, fetch=session.fetch_dc_ratio
                )
                if account_value["status"] != "cache_missing":
                    value = account_value
        except PhysicalAttemptBudgetExhausted as exc:
            outcome = exc.failure.outcome.value
            value = {
                "status": outcome
                if outcome in {"permission_denied", "authentication", "rate_limited"}
                else "unavailable"
            }
        except cold.PreparationError as exc:
            value = {
                "status": "permission_denied"
                if str(exc) == "permission_denied"
                else "budget_unavailable"
                if str(exc) in {"personal_attempt_limit", "personal_deadline_reached"}
                else "unavailable"
            }
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            value = {"status": "binding_or_source_unverified"}
        details[key] = dict(value, used_for_rating=False)
        statuses[key] = value["status"]
