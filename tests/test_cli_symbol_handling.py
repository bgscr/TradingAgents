"""CLI symbol validation/classification must agree with the data path.

Regressions for #980 (validation rejected GC=F), #981 (BTCUSD misclassified as
stock), #982 (BTC-USDT accepted but unpriceable on Yahoo).
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import tradingagents.dataflows.config as config_module
from cli import main as cli_main
from cli.models import AssetType
from cli.utils import detect_asset_type, is_valid_ticker_input, normalize_ticker_symbol
from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryUnavailable,
    RegistryFailureReason,
    resolve_authoritative_instrument_identity,
)
from tradingagents.dataflows.symbol_utils import normalize_symbol
from tradingagents.evidence import AcquisitionUnavailableReason, acquire_run_evidence
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup


class _QuietCliDisplay:
    def start(self) -> None:
        return None

    def refresh(self, _spinner_text=None) -> None:
        return None

    def publish_event(self, _event) -> None:
        return None

    def report_ready(self, _section_name, _content, _path) -> None:
        return None

    def close(self) -> None:
        return None


# Quote currency is identity-defining: only separator-free USD aliases normalize.
@pytest.mark.parametrize("raw,expected", [
    ("BTCUSD", "BTC-USD"),
    ("BTCUSDT", "BTCUSDT"),
    ("BTC-USDT", "BTC-USDT"),
    ("BTC-USDC", "BTC-USDC"),
    ("ethusdt", "ETHUSDT"),
    # non-crypto must be untouched
    ("AAPL", "AAPL"),
    ("GC=F", "GC=F"),
    ("600519.SS", "600519.SS"),
    ("EURUSD", "EURUSD=X"),
])
def test_normalize_symbol_crypto_and_passthrough(raw, expected):
    assert normalize_symbol(raw) == expected


# --- #980: validation accepts Yahoo futures/forex symbols ---
@pytest.mark.parametrize("value,ok", [
    ("GC=F", True),
    ("EURUSD=X", True),
    ("AAPL", True),
    ("0700.HK", True),
    ("^GSPC", True),
    ("", True),                 # empty -> defaults to SPY downstream
    ("bad symbol!", False),     # space + '!' rejected
    ("A" * 40, False),          # too long
])
def test_ticker_input_validation(value, ok):
    assert is_valid_ticker_input(value) is ok


# --- #981/#982: asset-type classified on the canonical symbol ---
@pytest.mark.parametrize("raw,expected", [
    ("BTCUSD", AssetType.CRYPTO),
    ("BTC-USDT", AssetType.CRYPTO),
    ("BTC-USD", AssetType.CRYPTO),
    ("ETHUSD", AssetType.CRYPTO),
    ("AAPL", AssetType.STOCK),
    ("GC=F", AssetType.STOCK),
    ("600519.SS", AssetType.STOCK),
])
def test_detect_asset_type(raw, expected):
    assert detect_asset_type(raw) == expected


def test_cli_normalize_delegates_to_data_layer():
    # CLI must produce the same canonical symbol the data path will price.
    for raw in ("XAUUSD", "BTCUSD", "btc-usdt", "AAPL"):
        assert normalize_ticker_symbol(raw) == normalize_symbol(raw)


def test_cli_unsupported_crypto_symbol_uses_typed_crypto_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_registries = {
        "instrument_identity_registry_path": "must-not-read-mainland.json",
        "instrument_identity_registry_sha256": "0" * 64,
        "crypto_identity_registry_path": "must-not-read-crypto.json",
        "crypto_identity_registry_sha256": "1" * 64,
    }
    configured_snapshot = dict(configured_registries)
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: configured_registries,
    )

    symbol = normalize_ticker_symbol("UNI-USD")
    result = resolve_authoritative_instrument_identity(symbol)

    assert detect_asset_type(symbol) is AssetType.CRYPTO
    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_symbol_not_supported"

    evidence = acquire_run_evidence(symbol, "2026-07-25")
    assert evidence.acquisition_outcomes[0].reason is (
        AcquisitionUnavailableReason.REGISTRY_NOT_CONFIGURED
    )

    model = MagicMock()

    def forbidden_tool_node(_state):
        raise AssertionError("unsupported crypto must stop before analyst tools")

    workflow = GraphSetup(
        model,
        model,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            forbidden_tool_node,
        ),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        evidence_gate_mode="enforce",
    ).setup_graph(["market"])

    class UnsupportedCryptoCliGraph:
        def __init__(self) -> None:
            self.graph = workflow.compile()
            self.propagator = Propagator()

        def resolve_evidence_state(self, ticker: str, trade_date: str):
            assert (ticker, trade_date) == (symbol, "2026-07-25")
            return evidence

        def create_initial_state(
            self,
            ticker: str,
            trade_date: str,
            *,
            asset_type: str,
            evidence_state,
        ):
            return self.propagator.create_initial_state(
                ticker,
                trade_date,
                asset_type=asset_type,
                evidence_state=evidence_state,
            )

    cli_graph = UnsupportedCryptoCliGraph()
    model.reset_mock()
    selections = {
        "ticker": symbol,
        "analysis_date": "2026-07-25",
        "asset_type": "crypto",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "fixture-quick",
        "deep_thinker": "fixture-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    captured_terminal: dict = {}
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts):
        captured_terminal.update(final_state)
        original_mark_completed(final_state, artifacts)

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(
            cli_main.DEFAULT_CONFIG,
            results_dir=str(tmp_path / "results"),
            data_cache_dir=str(tmp_path / "cache"),
            evidence_gate_mode="enforce",
            checkpoint_enabled=False,
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "TradingAgentsGraph",
        lambda *_args, **_kwargs: cli_graph,
    )
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: _QuietCliDisplay(),
    )
    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    cli_result = CliRunner().invoke(
        cli_main.app,
        ["analyze", "--no-checkpoint"],
    )

    assert cli_result.exit_code == 0, cli_result.output
    terminal = captured_terminal

    assert terminal["evidence_preflight"]["passed"] is False
    assert terminal["analysis_outcome_contract"] is not None
    assert terminal["terminal_outcome_kind"] == "analysis_outcome"
    assert terminal["evidence_state"]["acquisition_outcomes"][0]["reason"] == (
        "registry_not_configured"
    )
    assert terminal.get("trading_decision") is None
    assert terminal["final_trade_decision"] is None
    assert model.mock_calls == []
    assert config_module.get_config() == configured_snapshot
