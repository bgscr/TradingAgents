"""Interactive cancellation works without the site-provided exit helper."""
import builtins
from unittest.mock import Mock

import pytest

from cli import utils


@pytest.mark.parametrize(("entry", "args", "message"), [
    ("get_analysis_date", (), "No date provided."),
    ("select_analysts", (), "No analysts selected."),
    ("select_research_depth", (), "No research depth selected."),
    ("_require_text", ("Prompt", "Hint"), "Cancelled."),
    ("select_openrouter_model", ("quick",), "No model selected."),
    ("_select_model", ("openai", "deep"), "No deep thinking llm engine selected."),
    ("prompt_openai_compatible_url", (), "No endpoint URL provided."),
    ("select_llm_provider", (), "No LLM provider selected."),
])
def test_cancel_without_site_exit(monkeypatch, capsys, entry, args, message):
    monkeypatch.delattr(builtins, "exit", raising=False)
    prompt = Mock()
    prompt.return_value.ask.return_value = None
    for kind in ("text", "select", "checkbox"):
        monkeypatch.setattr(utils.questionary, kind, prompt)
    monkeypatch.setattr(utils, "_fetch_openrouter_models", lambda: [])

    with pytest.raises(SystemExit) as stopped:
        getattr(utils, entry)(*args)

    assert stopped.value.code == 1
    assert capsys.readouterr().out.strip() == message + " Exiting..."
    prompt.return_value.ask.assert_called_once_with()
