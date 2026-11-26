from types import SimpleNamespace

import pytest

from tradingagents.research_submission_validation import rejection_reason

ROLES = ("bull", "bear", "manager", "trader", "aggressive", "neutral", "conservative")


@pytest.mark.parametrize("change,reason", [
    ({}, None), ({"stages": list(ROLES) + ["bull"]}, "duplicate_stage"),
    ({"stages": ["untrusted"]}, "unknown_stage"), ({"stages": ["bull"]}, "missing_role"),
    ({"support": "untrusted"}, "opposing_reference_missing"),
    ({"support": "trader"}, "opposing_roles_invalid"),
    ({"market_fact": "untrusted"}, "unknown_market_fact"),
    ({"financial_fact": "untrusted"}, "unknown_financial_fact"),
])
def test_references(change, reason):
    stages = {role: {"role": role} for role in ROLES}
    values = dict(stages=list(stages), support="bull", opposition="bear", market_fact="m", financial_fact="f")
    assert rejection_reason(SimpleNamespace(**(values | change)), stages, {"m": 1}, {"f": 1}, ROLES) == reason
