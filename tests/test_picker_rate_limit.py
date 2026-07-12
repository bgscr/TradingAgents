from dataclasses import FrozenInstanceError

import pytest

from tradingagents.picker.rate_limit import RetryPolicy, TokenBucketLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_limiter_paces_calls_without_real_sleep():
    clock = FakeClock()
    limiter = TokenBucketLimiter(60, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_penalize_halves_effective_rate():
    clock = FakeClock()
    limiter = TokenBucketLimiter(40, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.penalize()
    assert limiter.calls_per_minute == 20


def test_penalize_recalculates_the_pending_interval():
    clock = FakeClock()
    limiter = TokenBucketLimiter(40, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.acquire()
    limiter.penalize()
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(3.0)]


def test_penalize_before_first_call_preserves_immediate_token():
    clock = FakeClock()
    limiter = TokenBucketLimiter(1, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.penalize()
    limiter.acquire()
    assert clock.sleeps == []


@pytest.mark.parametrize("calls_per_minute", [0, -1])
def test_limiter_rejects_non_positive_rates(calls_per_minute):
    with pytest.raises(ValueError, match="positive"):
        TokenBucketLimiter(calls_per_minute)


def test_retry_policy_honors_provider_delay():
    policy = RetryPolicy(max_attempts=8, jitter=lambda low, high: 0.0)
    assert policy.delay(attempt=3, retry_after=12.0) == 12.0


def test_retry_policy_caps_exponential_delay():
    policy = RetryPolicy(base_delay=1.0, max_delay=60.0, jitter=lambda low, high: 0.0)
    assert policy.delay(attempt=20, retry_after=None) == 60.0


def test_retry_policy_adds_jitter_with_declared_bounds():
    bounds = []

    def jitter(low, high):
        bounds.append((low, high))
        return 0.2

    policy = RetryPolicy(jitter=jitter)
    assert policy.delay(attempt=3, retry_after=None) == pytest.approx(4.2)
    assert bounds == [(0.0, 0.25)]


def test_retry_policy_is_frozen():
    policy = RetryPolicy(jitter=lambda low, high: 0.0)
    with pytest.raises(FrozenInstanceError):
        policy.max_attempts = 9
