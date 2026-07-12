import pytest

from tradingagents.picker.errors import PITConfigurationError
from tradingagents.picker.pit_config import PITConfig


def test_config_uses_explicit_values_before_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", "99")
    config = PITConfig.from_env(cache_dir=tmp_path, calls_per_minute=12)
    assert config.token == "env-token"
    assert config.cache_dir == tmp_path.resolve()
    assert config.calls_per_minute == 12


def test_config_defaults_under_tradingagents_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("TUSHARE_CALLS_PER_MINUTE", raising=False)
    config = PITConfig.from_env()
    assert config.cache_dir == (tmp_path / "pit" / "tushare").resolve()
    assert config.calls_per_minute == 40
    assert config.max_attempts == 8


def test_endpoint_rate_overrides_global(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", "40")
    monkeypatch.setenv("TUSHARE_DAILY_BASIC_CALLS_PER_MINUTE", "20")
    config = PITConfig.from_env(cache_dir=tmp_path)
    assert config.rate_for("daily_basic") == 20
    assert config.rate_for("daily") == 40


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_rate_is_rejected(monkeypatch, tmp_path, value):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", value)
    with pytest.raises(PITConfigurationError, match="positive integer"):
        PITConfig.from_env(cache_dir=tmp_path)
