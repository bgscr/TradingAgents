"""User-facing acceptance and CURRENT data semantics, with synthetic HTTPS only."""

import csv
import io
import json

import pytest

from tests.test_current_tushare_statements import statement_rows


def sample_rows(endpoint, symbol):
    """Synthetic provider schema, never market data or bank-name-based production logic."""
    if symbol == "600895.SH":
        return statement_rows(endpoint)
    import pandas as pd

    fields = {
        "balancesheet": {
            "cash_reser_cb": 80,
            "depos": 600,
            "cost_fin_assets": 100,
            "fair_value_fin_assets": 50,
            "depos_in_oth_bfi": 30,
            "depos_oth_bfi": 40,
            "loans": 500,
            "total_assets": 1000,
            "total_hldr_eqy_inc_min_int": 200,
            "total_liab": 800,
        },
        "income": {
            "int_exp": 30,
            "int_income": 80,
            "comm_income": 20,
            "n_commis_income": 15,
            "income_tax": 10,
            "invest_income": 5,
            "n_income_attr_p": 40,
            "oper_exp": 20,
            "operate_profit": 50,
            "total_profit": 50,
        },
        "cashflow": {
            "n_cash_flows_fnc_act": -10,
            "n_cashflow_inv_act": -30,
            "n_cashflow_act": 160,
            "c_inf_fr_operate_a": 400,
            "c_pay_dist_dpcp_int_exp": 20,
            "c_paid_for_taxes": 10,
            "c_cash_equ_end_period": 200,
            "eff_fx_flu_cash": 0,
            "n_incr_cash_cash_equ": 120,
            "c_cash_equ_beg_period": 80,
        },
    }[endpoint]
    return pd.DataFrame(
        [
            dict(
                ts_code=symbol,
                ann_date=ann,
                f_ann_date=ann,
                end_date=end,
                report_type="1",
                comp_type="2",
                update_flag="1",
                **fields,
            )
            for end, ann in (("20251231", "20260420"), ("20260630", "20260820"))
        ]
    )


def fake_https(monkeypatch, make_frame, *, response=None, status=200):
    from tradingagents import tushare_financial_https as transport

    calls = []

    class Connection:
        def __init__(self, host, **kwargs):
            self.host, self.port = host, 443

        def request(self, method, path, *, body, headers):
            self.payload = json.loads(body)
            calls.append(self.payload)

        def getresponse(self):
            return self

        def read(self, limit):
            if response is not None:
                return json.dumps(response).encode()
            frame = make_frame(self.payload["api_name"], self.payload["params"]["ts_code"])
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "fields": list(frame.columns),
                        "items": frame.values.tolist(),
                    },
                }
            ).encode()

        def close(self):
            pass

    Connection.status = status
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    return calls


def test_explicit_security_is_authoritative_and_sh_ss_reuse(tmp_path, monkeypatch):
    from tradingagents import statement_acceptance as acceptance

    calls = fake_https(monkeypatch, lambda endpoint, symbol: statement_rows(endpoint))
    request = {"destination": "https://api.tushare.pro", "account": "synthetic", "as_of": "2026-10-16"}
    first = acceptance.acquire(tmp_path, ticker="600895.SH", token="synthetic-only", **request)
    second = acceptance.reuse(tmp_path, ticker="600895.SS", **request)
    assert first["status"] == "acquired"
    assert first["canonical_symbol"] == second["canonical_symbol"] == "600895.SS"
    assert second["status"] == "reused"
    assert first["views"] == second["views"]
    assert len(calls) == 3
    for changed in (
        {"account": "different-account"},
        {"destination": "https://api.waditu.com/dataapi"},
        {"as_of": "2026-10-17"},
    ):
        isolated = acceptance.reuse(tmp_path, ticker="600895.SH", **{**request, **changed})
        assert isolated["status"] == "stopped"
        assert isolated["client_attempts"] == 1
    assert len(calls) == 3


def test_three_securities_do_not_share_cache_but_each_alias_reuses(tmp_path, monkeypatch):
    from tradingagents import statement_acceptance as acceptance

    calls = fake_https(monkeypatch, sample_rows)
    request = {"destination": "https://api.tushare.pro", "account": "synthetic", "as_of": "2026-10-16"}
    receipts = {}
    for ticker in ("600895.SH", "601328.SH", "601658.SH"):
        receipts[ticker] = acceptance.acquire(
            tmp_path, ticker=ticker, token="synthetic-only", **request
        )
        assert receipts[ticker]["status"] == "acquired"
    assert len(calls) == 9
    assert {call["params"]["ts_code"] for call in calls} == set(receipts)
    for ticker, receipt in receipts.items():
        replay = acceptance.reuse(tmp_path, ticker=ticker.replace(".SH", ".SS"), **request)
        assert replay["status"] == "reused"
        assert replay["views"] == receipt["views"]
    assert len(calls) == 9


def dispatch_sample(
    tmp_path, monkeypatch, *, ticker="601328.SH", endpoint="cashflow", transform=lambda frame: frame
):
    from tests.test_current_tushare_statements import request
    from tradingagents import statement_acceptance as acceptance
    from tradingagents.dataflows.financial_dispatch import FinancialToolDispatcher

    fake_https(monkeypatch, lambda api, symbol: transform(sample_rows(api, symbol)))
    config, asset = acceptance.validate_request(
        tmp_path,
        ticker=ticker,
        destination="https://api.tushare.pro",
        account="synthetic",
        as_of="2026-10-16",
    )
    tool = {
        "cashflow": "get_cashflow",
        "income": "get_income_statement",
        "balancesheet": "get_balance_sheet",
    }[endpoint]
    from tradingagents import tushare_financial_https as transport
    from tradingagents.dataflows import current_tushare_statements as current

    monkeypatch.setattr(
        current,
        "create_financial_statement_client",
        lambda _: transport._Client("https://api.tushare.pro", "synthetic-only"),
    )
    route = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
    )
    return route.dispatch(request(tool))


@pytest.mark.parametrize("ticker", ["601328.SH", "601658.SH"])
def test_bank_output_distinguishes_zero_missing_and_inapplicable(tmp_path, monkeypatch, ticker):
    result = dispatch_sample(
        tmp_path,
        monkeypatch,
        ticker=ticker,
        transform=lambda frame: frame.drop(columns=["c_paid_for_taxes"]),
    )
    assert result.artifact is not None
    rows = list(
        csv.DictReader(
            io.StringIO(
                "\n".join(line for line in result.value.splitlines() if not line.startswith("#"))
            )
        )
    )
    fields = {row["field"]: row for row in rows}
    assert fields["effect_of_exchange_rates"]["value"] == "0"
    assert fields["effect_of_exchange_rates"]["value_status"] == "reported"
    assert fields["cash_paid_for_taxes"]["value"] == ""
    assert fields["cash_paid_for_taxes"]["value_status"] == "missing"
    assert (
        "not_applicable_metrics=gross_margin,current_ratio,quick_ratio,inventory_turnover"
        in result.value
    )
    assert (
        "unavailable_metrics=net_interest_margin,nonperforming_loan_ratio,capital_adequacy_ratio,provision_coverage_ratio"
        in result.value
    )


def test_nonbank_missing_field_is_not_reported_zero(tmp_path, monkeypatch):
    result = dispatch_sample(
        tmp_path,
        monkeypatch,
        ticker="600895.SH",
        transform=lambda frame: frame.drop(columns=["c_paid_for_taxes"]),
    )
    assert result.artifact is not None
    assert ",cash_paid_for_taxes,,," in result.value
    assert ",missing\n" in result.value
    assert ",effect_of_exchange_rates,0,CNY_BASE_UNIT," in result.value
    assert "bank_not_applicable_metrics" not in result.value


@pytest.mark.parametrize("company_type", ["3", "4", "7", "unknown"])
def test_unsupported_company_type_is_explicit_and_stops(tmp_path, monkeypatch, company_type):
    from tradingagents import statement_acceptance as acceptance

    calls = fake_https(
        monkeypatch, lambda api, symbol: sample_rows(api, symbol).assign(comp_type=company_type)
    )
    result = acceptance.acquire(
        tmp_path,
        ticker="601328.SH",
        token="synthetic-only",
        destination="https://api.tushare.pro",
        account="synthetic",
        as_of="2026-10-16",
    )
    assert result["status"] == "stopped"
    assert len(calls) == 1
    assert result["failure"]["error_code"] == "current_financial_unsupported_company_type"


def test_cli_failure_writes_terminal_receipt_and_does_not_launch_reuse(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts import accept_financial_statements as cli

    output = tmp_path / "reservation"
    calls = []

    def failed_child(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(cli.subprocess, "run", failed_child)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(cli.__file__),
            "--output",
            str(output),
            "--ticker",
            "601328.SH",
            "--destination",
            "https://api.tushare.pro",
            "--account",
            "synthetic",
            "--as-of",
            "2026-10-16",
        ],
    )
    assert cli.main() == 1
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "stopped"
    assert result["failed_phase"] == "synthetic"
    assert result["failure"]["error_code"] == "acceptance_worker_exit"
    assert result["physical_requests"] == [None, None]
    assert len(calls) == 1


def test_network_gate_blocks_unplanned_hosts_and_restores_closed_state():
    from scripts.financial_network_guard import FinancialNetworkGuard

    class Connection:
        host = "api.tushare.pro"
        port = 443
        _tunnel_host = None

    connection = Connection()
    guard = FinancialNetworkGuard("https://api.tushare.pro")
    assert not guard.allows("socket.getaddrinfo", ("api.tushare.pro", 443))
    with guard.exchange(connection, "income"):
        assert guard.allows("http.client.connect", (connection, "api.tushare.pro", 443))
        assert not guard.allows("http.client.connect", (Connection(), "api.tushare.pro", 443))
        assert not guard.allows("socket.getaddrinfo", ("unplanned.invalid", 443))
        assert not guard.allows("socket.bind", (object(), ("127.0.0.1", 0)))
    assert not guard.allows("http.client.send", (connection, b"not a payload"))
    with pytest.raises(RuntimeError), guard.exchange(connection, "income"):
        pytest.fail("duplicate table must not open the gate")


def test_interruption_preserves_consumed_budget_and_completed_progress(tmp_path, monkeypatch):
    from tradingagents import statement_acceptance as acceptance

    progress = {}

    def interrupted_response(endpoint, symbol):
        # At the external response boundary the attempt must already be visible
        # to the owning worker, even if acceptance never returns normally.
        assert progress["physical_requests"] in {1, 2}
        if endpoint == "income":
            raise KeyboardInterrupt
        return sample_rows(endpoint, symbol)

    calls = fake_https(monkeypatch, interrupted_response)
    result = acceptance.acquire(
        tmp_path, ticker="601328.SH", destination="https://api.tushare.pro",
        account="synthetic", as_of="2026-10-16", token="synthetic-only", progress=progress,
    )
    assert len(calls) == result["physical_requests"] == progress["physical_requests"] == 2
    assert result["failure"]["error_code"] == "financial_https_interrupted"
    assert progress["completed_tools"] == ["get_balance_sheet"]
    assert len(progress["views"]) == 2
    assert progress["current_view"] == "get_income_statement:quarterly"


def manual_worker_context(tmp_path, monkeypatch, hidden_prompt):
    import getpass
    from types import SimpleNamespace

    from scripts import accept_financial_statements as cli

    monkeypatch.setattr(getpass, "getpass", hidden_prompt)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # The exact-file runner already installed an irrevocable offline audit guard.
    # Do not install another permanent, narrower output-root guard inside pytest.
    monkeypatch.setattr(cli, "install", lambda *a, **k: (
        dict.fromkeys(("network", "credentials", "subprocess", "outside_write"), 0), []
    ))
    contract = {
        "sources": cli.identities(), "live": True,
        "request": {"ticker": "601328.SH", "destination": "https://api.waditu.com/dataapi",
                    "account": "synthetic", "as_of": "2026-10-16"},
    }
    (tmp_path / "contract.json").write_text(json.dumps(contract))
    return cli, SimpleNamespace(output=tmp_path, worker="live")


@pytest.mark.parametrize(
    "token,accepted",
    [
        ("offline-paste-sentinel", True),
        ("x", True),  # No guessed credential-length rule.
        ("", False),
        ("   ", False),
        (" offline-paste-sentinel", False),
        ("offline-paste-sentinel ", False),
        ("offline\tpaste", False),
        ("offline\npaste", False),
        ("offline\x00paste", False),
        ("offline\u200bpaste", False),
        ('"offline-paste-sentinel"', False),
        ("'offline-paste-sentinel'", False),
        ("`offline-paste-sentinel`", False),
        ("Bearer offline-paste-sentinel", False),
        ("TUSHARE_TOKEN=offline-paste-sentinel", False),
        ("token:offline-paste-sentinel", False),
        ('{"token":"offline-paste-sentinel"}', False),
        ("$env:TUSHARE_TOKEN=offline-paste-sentinel", False),
        ("api_key=offline-paste-sentinel", False),
        ("“offline-paste-sentinel”", False),
        ("https://offline.invalid/?token=offline-paste-sentinel", False),
        ("offline\u034fpaste", False),
        ("ts.set_token('offline-paste-sentinel')", False),
        ("ts.pro_api('offline-paste-sentinel')", False),
        ("pro=ts.pro_api('offline-paste-sentinel')", False),
        ("offline\u115fpaste", False),
        ("offline\u17b4paste", False),
        ("offline\u180bpaste", False),
        ("offline\u3164paste", False),
        ("offline\ufe0fpaste", False),
        ("offline\uffa0paste", False),
        ("offline\ufff0paste", False),
        ("offline\U000e0100paste", False),
        ("offline-é-paste", True),
        ("offline-a\u0301-paste", True),
    ],
    ids=["valid", "short", "empty", "blank", "leading", "trailing", "tab", "newline",
         "nul", "invisible", "double-quoted", "single-quoted", "backticks", "bearer",
         "assignment", "label", "json", "powershell", "api-key", "curly-quotes", "url",
         "grapheme-joiner", "sdk-set-token", "sdk-pro-api", "sdk-assignment",
         "hangul-filler", "khmer-invisible", "mongolian-selector", "hangul-compat-filler",
         "variation-selector", "halfwidth-filler", "reserved-invisible", "supplementary-selector",
         "visible-unicode", "visible-combining"],
)
def test_hidden_prompt_to_worker_request_and_safe_receipt(tmp_path, monkeypatch, token, accepted):
    prompts = []

    def hidden_prompt(message):
        prompts.append(message)
        return token

    cli, args = manual_worker_context(tmp_path, monkeypatch, hidden_prompt)
    calls = fake_https(monkeypatch, sample_rows)
    code = cli.worker(args)
    receipt_text = (tmp_path / "live.json").read_text()
    receipt = json.loads(receipt_text)
    assert len(prompts) == 1
    if token:
        assert json.dumps(token) not in receipt_text
    if accepted:
        assert code == 0 and receipt["status"] == "acquired"
        assert [call["api_name"] for call in calls] == ["balancesheet", "income", "cashflow"]
        assert all(call["token"] == token for call in calls)
    else:
        assert code == 1 and receipt["status"] == "stopped"
        assert receipt["failure"]["error_code"] == "acceptance_token_input_invalid"
        assert receipt["physical_requests"] == 0 and calls == []
    assert "Tushare" in prompts[0]


def test_hidden_prompt_failure_cannot_fall_back_to_visible_input(tmp_path, monkeypatch):
    import getpass
    import warnings

    prompts = []

    def unavailable_prompt(message):
        prompts.append(message)
        warnings.warn("synthetic terminal failure", getpass.GetPassWarning, stacklevel=1)
        pytest.fail("visible fallback must not collect input")

    cli, args = manual_worker_context(tmp_path, monkeypatch, unavailable_prompt)
    calls = fake_https(monkeypatch, sample_rows)
    assert cli.worker(args) == 1
    receipt_text = (tmp_path / "live.json").read_text()
    receipt = json.loads(receipt_text)
    assert receipt["physical_requests"] == 0 and calls == [] and len(prompts) == 1
    assert receipt["failure"]["error_code"] == "acceptance_application_failed"
    assert "synthetic terminal failure" not in receipt_text


@pytest.mark.parametrize(
    "response,status,error,provider_code,basis",
    [
        ({"code": -1, "msg": "synthetic token service unavailable"}, 200,
         "upstream_rejection", -1, "message_heuristic_authentication"),
        ({"code": 2002, "msg": "synthetic 权限不足"}, 200,
         "upstream_rejection", 2002, "message_heuristic_permission"),
        ({"code": 2002, "msg": "synthetic unknown rejection"}, 200,
         "upstream_rejection", 2002, "provider_code_unmapped"),
        ({"code": -1, "msg": {"token": "synthetic non-text"}}, 200,
         "upstream_rejection", -1, "provider_code_unmapped"),
        ({"code": -1, "msg": "synthetic 每分钟"}, 200,
         "rate_limited", -1, "message_heuristic_rate_limit"),
        ({"code": 2147483647, "msg": "synthetic unknown"}, 200,
         "upstream_rejection", 2147483647, "provider_code_unmapped"),
        ({"code": -2147483648, "msg": "synthetic unknown"}, 200,
         "upstream_rejection", -2147483648, "provider_code_unmapped"),
        ({"code": 2147483648, "msg": "synthetic token"}, 200,
         "malformed_response", None, "response_schema"),
        ({"code": True, "msg": "synthetic token"}, 200,
         "malformed_response", None, "response_schema"),
        ({"code": 0, "data": {}}, 200, "malformed_response", 0, "response_schema"),
        ({"code": 0}, 401, "authentication", None, "http_status"),
        ({"code": 0}, 403, "permission_denied", None, "http_status"),
    ],
    ids=["ambiguous-auth", "permission-hint", "unmapped", "nontext-message", "rate-limit",
         "upper-bound", "lower-bound", "oversize-code", "boolean-code", "invalid-data",
         "http-auth", "http-permission"],
)
def test_upstream_failure_receipt_preserves_only_safe_classification(
    tmp_path, monkeypatch, response, status, error, provider_code, basis
):
    from tradingagents import statement_acceptance as acceptance

    calls = fake_https(monkeypatch, sample_rows, response=response, status=status)
    result = acceptance.acquire(
        tmp_path, ticker="601328.SH", destination="https://api.waditu.com/dataapi",
        account="synthetic", as_of="2026-10-16", token="offline-receipt-sentinel",
    )
    assert result["status"] == "stopped" and result["physical_requests"] == len(calls) == 1
    assert result["views"] == {} and result["completed_tools"] == []
    failure = result["failure"]
    assert failure["error_code"] == "financial_https_" + error
    assert failure["provider_code"] == provider_code
    assert failure["classification_basis"] == basis
    assert failure["http_status"] == status and not failure["retryable"]
    serialized = json.dumps(result)
    assert "synthetic" not in serialized and "offline-receipt-sentinel" not in serialized
    assert "msg" not in failure
