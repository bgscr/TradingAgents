from __future__ import annotations

import re
from datetime import date, datetime

from tradingagents.picker.errors import PITConfigurationError

_YYYYMMDD_RE = re.compile(r"[0-9]{8}")


def parse_yyyymmdd(value: str, label: str) -> date:
    """Parse one strict eight-digit calendar date."""
    if not isinstance(value, str) or _YYYYMMDD_RE.fullmatch(value) is None:
        raise PITConfigurationError(
            f"{label} must be an eight-digit date in YYYYMMDD format"
        )
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise PITConfigurationError(
            f"{label} must be a valid date in YYYYMMDD format"
        ) from exc


def validate_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    start = parse_yyyymmdd(start_date, "start_date")
    end = parse_yyyymmdd(end_date, "end_date")
    if start > end:
        raise PITConfigurationError("start_date must be on or before end_date")
    return start, end
