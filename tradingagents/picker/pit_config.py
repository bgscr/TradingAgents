from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PITConfigurationError


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise PITConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise PITConfigurationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class PITConfig:
    token: str | None
    cache_dir: Path
    calls_per_minute: int
    endpoint_rates: dict[str, int]
    max_attempts: int = 8

    @classmethod
    def from_env(
        cls,
        cache_dir: Path | None = None,
        calls_per_minute: int | None = None,
    ) -> PITConfig:
        root = Path(
            os.environ.get(
                "TRADINGAGENTS_CACHE_DIR",
                Path.home() / ".tradingagents" / "cache",
            )
        )
        resolved_cache = (cache_dir or root / "pit" / "tushare").expanduser().resolve()
        global_rate = (
            _positive_int(str(calls_per_minute), "--calls-per-minute")
            if calls_per_minute is not None
            else _positive_int(
                os.environ.get("TUSHARE_CALLS_PER_MINUTE", "40"),
                "TUSHARE_CALLS_PER_MINUTE",
            )
        )
        endpoint_rates = {}
        prefix = "TUSHARE_"
        suffix = "_CALLS_PER_MINUTE"
        for name, raw in os.environ.items():
            if (
                name.startswith(prefix)
                and name.endswith(suffix)
                and name != "TUSHARE_CALLS_PER_MINUTE"
            ):
                endpoint = name[len(prefix) : -len(suffix)].lower()
                endpoint_rates[endpoint] = _positive_int(raw, name)
        return cls(
            token=os.environ.get("TUSHARE_TOKEN") or None,
            cache_dir=resolved_cache,
            calls_per_minute=global_rate,
            endpoint_rates=endpoint_rates,
        )

    def rate_for(self, endpoint: str) -> int:
        return self.endpoint_rates.get(endpoint.lower(), self.calls_per_minute)
