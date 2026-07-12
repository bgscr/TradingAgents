from tradingagents.dataflows.errors import (
    VendorNotConfiguredError,
    VendorRateLimitError,
)


class PITError(Exception):
    """Base error for the point-in-time data foundation."""


class PITConfigurationError(PITError, ValueError):
    """Invalid local PIT configuration."""


class PITSchemaError(PITError):
    """A provider partition violates its normalized schema."""


class PITCoverageError(PITError):
    """A snapshot or fold does not meet the required data coverage."""


class TushareNotConfiguredError(VendorNotConfiguredError, PITError):
    """The PIT extra or TUSHARE_TOKEN is unavailable."""


class TushareRateLimitError(VendorRateLimitError, PITError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
