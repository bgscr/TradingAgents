"""Cold offline child: production _worker and unchanged test-only runtime guard."""

# ruff: noqa: E402 -- startup guards must precede application imports.
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1]
tree = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
assert mode in {"synthetic", "reuse"}
cache_root = tree / "acquire"
guard_root = cache_root if mode == "synthetic" else tree
sys.path.insert(0, str(SOURCE / "scripts"))
# Real cold parent import boundary: no project/dependency sys.path yet.
import accept_financial_statements as cold_parent

receipts_module = cold_parent._receipt_helpers()
assert receipts_module.gap_eligible({}) is False
assert not any(name == "tradingagents" or name.startswith("tradingagents.") for name in sys.modules)
cold_scope = receipts_module.scopes_from_binding(
    {
        "account": "offline-native-gap",
        "instrument_identity_revision": "identity:v1",
        "destination": "https://api.waditu.com/dataapi",
        "normalizer_version": "current-tushare-statements-v1",
    }
)
assert len(cold_scope) == 3
from diagnostic_runtime import Urllib3LoopbackProbe, install

counts, blocked = install(
    guard_root, SOURCE, offline=True, test_loopback_probe=Urllib3LoopbackProbe.POLICY
)
from financial_network_guard import FinancialNetworkGuard

from scripts import accept_financial_statements as entry
from tests import test_financial_acceptance_scope as fixture

original = fixture.sample_rows


def rows(endpoint, symbol):
    frame = original(endpoint, symbol)
    if endpoint == "balancesheet":
        frame = frame.assign(cost_fin_assets=None, fair_value_fin_assets=None)
    return frame


fixture.sample_rows = rows
request = {
    "ticker": "601328.SH",
    "destination": "https://api.waditu.com/dataapi",
    "account": "offline-native-gap",
    "as_of": "2026-10-16",
}
contract = {"request": request, "allow_optional_financial_gaps": True}
guard = FinancialNetworkGuard(request["destination"])
result = {
    "pid": os.getpid(),
    "ppid": os.getppid(),
    "mode": mode,
    "synthetic_transport": True,
    "live_qualification": False,
    "cold_parent_stdlib_only": True,
}
try:
    result.update(
        entry._worker(SimpleNamespace(worker=mode), cache_root, contract, counts, guard, result)
    )
except BaseException as error:
    result.update(
        status="test_failed",
        error_type=type(error).__name__,
        winerror=getattr(error, "winerror", None),
        errno=getattr(error, "errno", None),
    )
result.update(guard_counts=counts, blocked_network=blocked)
target = (cache_root if mode == "synthetic" else tree) / (mode + "-result.json")
with target.open("x", encoding="utf-8") as stream:
    json.dump(result, stream, indent=2)
expected = "acquired_degraded" if mode == "synthetic" else "reused_degraded"
raise SystemExit(0 if result.get("status") == expected else 1)
