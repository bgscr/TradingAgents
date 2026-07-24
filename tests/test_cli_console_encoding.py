from __future__ import annotations

from io import BytesIO, TextIOWrapper

import pytest
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from cli.console_encoding import configure_utf8_stdio


@pytest.mark.unit
def test_configure_utf8_stdio_preserves_yen_sign_on_strict_gbk_streams() -> None:
    stdout_bytes = BytesIO()
    stderr_bytes = BytesIO()
    stdout = TextIOWrapper(stdout_bytes, encoding="gbk", errors="strict")
    stderr = TextIOWrapper(stderr_bytes, encoding="gbk", errors="strict")

    configure_utf8_stdio(stdout=stdout, stderr=stderr)
    console = Console(file=stdout, force_terminal=False, color_system=None)
    console.print(Panel(Markdown("Price target: ¥8.00"), title="Report"))
    stdout.flush()

    assert stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert stdout_bytes.getvalue().decode("utf-8").count("¥8.00") == 1
