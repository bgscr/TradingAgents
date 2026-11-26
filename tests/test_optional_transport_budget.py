"""Optional data uses the mandatory budget and cannot renew it."""
import sqlite3
from threading import Lock

import pytest

from tradingagents import analysis_budget as budgets


@pytest.mark.parametrize("durable", [False, True])
def test_full_budget_skips_optional_but_stops_mandatory(durable, tmp_path, monkeypatch):
    budget = budgets.AnalysisBudget(1, 60)
    with sqlite3.connect(tmp_path / "fixture.sqlite") as connection:
        if durable:
            budget.bind_checkpoint(connection, Lock(), "fixture", {}, has_checkpoint=False)
        monkeypatch.setattr(budgets, "_ACTIVE", budget)
        with budgets.admitted_transport("data") as allowance:
            assert allowance > 0
        with budgets.admitted_transport("data", optional=True) as allowance:
            assert allowance is None
            assert not budgets._IN_TRANSPORT.get()
        assert budget.receipt()["data_attempts"] == 1
        with pytest.raises(budgets.AnalysisBudgetStopped, match="call_limit_exceeded"):
            budget.admit("model")


def test_checkpoint_binding_distinguishes_financial_observation_policies():
    binding = budgets.checkpoint_budget_binding
    default = binding({}, "fixture")
    assert default == binding({"bounded_moneyflow_enabled": False, "bounded_margin_date": None}, "fixture")
    changed = [
        {"bounded_moneyflow_enabled": True},
        {"bounded_observation_cache_mode": "retrospective"},
        {"bounded_fundflow_observation_sha256": "a" * 64},
        {"bounded_margin_date": "2026-11-25"},
        {"bounded_margin_date": "2026-11-24"},
    ]
    assert len({default, *(binding(policy, "fixture") for policy in changed)}) == len(changed) + 1
