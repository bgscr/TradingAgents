import pytest

from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.tushare_provider import TushareProvider


@pytest.mark.integration
@pytest.mark.live(
    opt_in="RUN_LIVE_TUSHARE_TESTS",
    credentials=("TUSHARE_TOKEN",),
)
def test_tushare_trade_calendar_probe(tmp_path):
    provider = TushareProvider.create(PITConfig.from_env(cache_dir=tmp_path))
    provider.probe()
