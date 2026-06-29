from pathlib import Path

import pytest


@pytest.mark.unit
def test_china_data_dependencies_are_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"akshare' in pyproject
    assert '"baostock' in pyproject
