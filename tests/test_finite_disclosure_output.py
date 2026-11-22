"""Closed financial output and explicit filing-bound sign semantics."""

from types import SimpleNamespace

import pytest

from tradingagents.dataflows import (
    finite_disclosure_admission as a,
    finite_disclosure_output as output,
)

pytest_plugins = ["test_finite_disclosure_admission"]


def test_exact_financial_output_only(installation):
    config, view, _ = installation
    report = output.render_report(view)
    output.validate_submission(SimpleNamespace(report=report, claims=[]), config)
    with pytest.raises(a.Rejected):
        output.validate_submission(
            SimpleNamespace(report=report + "Undocumented valuation.", claims=[]), config
        )
    assert len(output.report_lines(view)) == 90
    assert "no_TTM" in report and "no_latest_report" in report


def test_explicit_five_sign_rules_preserve_raw(installation):
    _, view, _ = installation
    flipped = [r for r in view["rows"] if r["conversion"] == "explicit_negation_of_positive_amount"]
    assert len(flipped) == 10
    assert {(r["table"], r["field"]) for r in flipped} == a.NEGATE
    for row in flipped:
        assert a.amount(row["raw_api_value"]) > 0
        assert a.amount(row["derived_disclosure_contribution"]) == -a.amount(row["raw_api_value"])


def test_claims_cannot_expand_scope(installation):
    config, view, _ = installation
    line = output.report_lines(view)[0]
    output.validate_submission(
        SimpleNamespace(
            report=output.render_report(view),
            claims=[SimpleNamespace(statement=line, source_quote=line)],
        ),
        config,
    )
    with pytest.raises(a.Rejected):
        output.validate_submission(
            SimpleNamespace(
                report=output.render_report(view),
                claims=[SimpleNamespace(statement="latest report", source_quote=line)],
            ),
            config,
        )


def test_terminal_scope_and_marker(installation):
    config, view, _ = installation
    state = {
        "company_of_interest": "601328.SH",
        "trade_date": "2026-10-16",
        "fundamentals_report": output.render_report(view),
    }
    # A pasted report is not sufficient without the new runtime/cache evidence binding.
    with pytest.raises(a.Rejected, match="finite_context_missing_or_shape"):
        output.validate_terminal_output(state, config)
    state["finite_disclosure_context"] = {}
    with pytest.raises(a.Rejected):
        output.validate_terminal_output(state, {"finite_disclosure_profile": None})


def test_disabled_output_unchanged():
    state = {"fundamentals_report": "ordinary legacy report"}
    output.validate_terminal_output(state, {})
    assert output.render_limits(state) == ""


def test_downstream_unvalidated_financial_conclusion_rejected(installation):
    config, view, _ = installation
    state = {
        "company_of_interest": "601328.SH",
        "trade_date": "2026-10-16",
        "fundamentals_report": output.render_report(view),
        "trader_investment_plan": "TTM valuation proves this bank is cheap.",
    }
    with pytest.raises(a.Rejected):
        output.validate_terminal_output(state, config)
