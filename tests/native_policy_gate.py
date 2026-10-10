"""Explicit consent for tests which create real Windows policy objects."""

import os

import pytest


def native_policy_tests_enabled(environment=None, *, platform=None):
    environment = os.environ if environment is None else environment
    platform = os.name if platform is None else platform
    return platform == "nt" and environment.get("TRADINGAGENTS_RUN_NATIVE_POLICY_TESTS") == "1"


native_policy_test = pytest.mark.skipif(
    not native_policy_tests_enabled(),
    reason="requires Windows and explicit TRADINGAGENTS_RUN_NATIVE_POLICY_TESTS=1",
)
