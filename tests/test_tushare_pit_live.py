import os

import pytest

from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.tushare_provider import TushareProvider


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("TUSHARE_TOKEN")
    or os.environ.get("RUN_LIVE_TUSHARE_TESTS") != "1",
    reason="set TUSHARE_TOKEN and RUN_LIVE_TUSHARE_TESTS=1 for live validation",
)
def test_tushare_trade_calendar_probe(tmp_path):
    provider = TushareProvider.create(PITConfig.from_env(cache_dir=tmp_path))
    provider.probe()
