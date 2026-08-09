"""Fail-closed pytest guard for tests that can contact live services."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

import pytest

_PLACEHOLDER_CREDENTIALS = frozenset({"dummy", "fake", "placeholder", "test"})


def live_test_enabled(
    *,
    opt_in: str,
    credentials: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a live test has both explicit consent and credentials."""

    effective_environment = os.environ if environment is None else environment
    if effective_environment.get(opt_in) != "1":
        return False
    return bool(credentials) and all(
        _credential_available(effective_environment.get(name, ""))
        for name in credentials
    )


def _credential_available(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and normalized.casefold() not in _PLACEHOLDER_CREDENTIALS


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live(opt_in, credentials): test that may contact a live external service",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    del config
    for item in items:
        marker = item.get_closest_marker("live")
        if marker is None:
            continue

        opt_in = marker.kwargs.get("opt_in")
        credentials = marker.kwargs.get("credentials")
        if not isinstance(opt_in, str) or not opt_in:
            raise pytest.UsageError(
                f"{item.nodeid}: live marker requires a non-empty opt_in name"
            )
        if (
            not isinstance(credentials, (tuple, list))
            or not credentials
            or not all(isinstance(name, str) and name for name in credentials)
        ):
            raise pytest.UsageError(
                f"{item.nodeid}: live marker requires credential variable names"
            )

        if not live_test_enabled(opt_in=opt_in, credentials=credentials):
            credential_names = ", ".join(credentials)
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        f"set {opt_in}=1 and {credential_names} for live validation"
                    )
                )
            )
