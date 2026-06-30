import datetime

import pytest


@pytest.mark.unit
def test_is_china_a_ticker_accepts_mainland_forms():
    import cli.main as m

    assert m.is_china_a_ticker("600895.SS") is True
    assert m.is_china_a_ticker("600895.SH") is True
    assert m.is_china_a_ticker("000333.SZ") is True
    assert m.is_china_a_ticker("600895") is True


@pytest.mark.unit
def test_is_china_a_ticker_rejects_non_mainland_forms():
    import cli.main as m

    assert m.is_china_a_ticker("AAPL") is False
    assert m.is_china_a_ticker("0700.HK") is False
    assert m.is_china_a_ticker("BTC-USD") is False
    assert m.is_china_a_ticker("900901.SS") is False
    assert m.is_china_a_ticker("900901.SH") is False
    assert m.is_china_a_ticker("200012.SZ") is False
    assert m.is_china_a_ticker("900901") is False
    assert m.is_china_a_ticker("200012") is False


@pytest.mark.unit
def test_build_run_config_carries_china_a_preset():
    import cli.main as m

    selections = {
        "research_depth": 1,
        "shallow_thinker": "gpt-5.4-mini",
        "deep_thinker": "gpt-5.5",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
        "china_a_enhancement_preset": "flow_sentiment",
    }

    config = m._build_run_config(selections, checkpoint=None)

    assert config["china_a_enhancement_preset"] == "flow_sentiment"


@pytest.mark.unit
def test_china_a_preset_prompt_maps_numeric_choice_to_value(monkeypatch):
    import cli.main as m

    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: "2")

    assert m.select_china_a_enhancement_preset() == "flow_sentiment"


@pytest.mark.unit
def test_china_a_preset_prompt_defaults_to_basic_on_enter(monkeypatch):
    import cli.main as m

    def prompt_returns_default(*args, **kwargs):
        return kwargs["default"]

    monkeypatch.setattr(m.typer, "prompt", prompt_returns_default)

    assert m.select_china_a_enhancement_preset() == "basic"


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls(2026, 6, 30, 8, 30, tzinfo=tz)
        return cls(2026, 6, 29, 17, 30)


@pytest.mark.unit
def test_china_a_same_beijing_date_allowed_when_local_date_is_behind(monkeypatch):
    import cli.main as m

    responses = iter(["2026-06-30"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("600895.SS") == "2026-06-30"
    assert any("may be incomplete" in line for line in printed)


@pytest.mark.unit
def test_china_a_after_beijing_today_is_rejected_then_accepts(monkeypatch):
    import cli.main as m

    responses = iter(["2026-07-01", "2026-06-30"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("600895.SS") == "2026-06-30"
    assert any("cannot be in the future" in line for line in printed)


@pytest.mark.unit
def test_non_china_symbol_keeps_local_date_limit(monkeypatch):
    import cli.main as m

    responses = iter(["2026-06-30", "2026-06-29"])
    monkeypatch.setattr(m.datetime, "datetime", FixedDateTime)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: next(responses))

    printed = []
    monkeypatch.setattr(m.console, "print", lambda *args, **kwargs: printed.append(str(args[0])))

    assert m.get_analysis_date("AAPL") == "2026-06-29"
    assert any("cannot be in the future" in line for line in printed)
