"""Run one explicitly authorized live-validation case without interactive prompts."""

# The harness must load .env before importing CLI modules because configuration
# is resolved during import. Keep this intentional ordering explicit to Ruff.
# ruff: noqa: E402, I001

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

import cli.main as cli_main
from cli.models import AnalystType


ANALYSIS_DATE = "2026-07-23"
EQUITY_SYMBOL = "600895.SS"
FUND_SYMBOL = "510500.SS"


def _selections(case: str) -> dict[str, object]:
    if case == "fund":
        ticker = FUND_SYMBOL
        analysts = [
            AnalystType.MARKET,
            AnalystType.SOCIAL,
            AnalystType.NEWS,
        ]
    elif case in {"equity", "degraded"}:
        ticker = EQUITY_SYMBOL
        analysts = [
            AnalystType.MARKET,
            AnalystType.SOCIAL,
            AnalystType.NEWS,
            AnalystType.FUNDAMENTALS,
        ]
    else:
        raise ValueError(f"unknown validation case: {case}")

    return {
        "ticker": ticker,
        "asset_type": "stock",
        "analysis_date": ANALYSIS_DATE,
        "analysts": analysts,
        "research_depth": 1,
        "llm_provider": "deepseek",
        "backend_url": "https://api.deepseek.com",
        "shallow_thinker": "deepseek-v4-flash",
        "deep_thinker": "deepseek-v4-pro",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
        "china_a_enhancement_preset": "basic",
    }


def main() -> None:
    case = os.environ.get("TRADINGAGENTS_VALIDATION_CASE", "").strip().lower()
    selections = _selections(case)

    if case == "degraded":
        cli_main.DEFAULT_CONFIG["tool_vendors"] = {
            **cli_main.DEFAULT_CONFIG["tool_vendors"],
            "get_news": "validation_unavailable",
        }

    original_prompt = cli_main.typer.prompt

    def unattended_prompt(text: str, *args: object, **kwargs: object) -> str:
        if str(text).strip() in {
            "Save report?",
            "Display full report on screen?",
        }:
            return "N"
        return original_prompt(text, *args, **kwargs)

    cli_main.typer.prompt = unattended_prompt
    cli_main.get_user_selections = lambda: selections
    cli_main.run_analysis(checkpoint=False)


if __name__ == "__main__":
    main()
