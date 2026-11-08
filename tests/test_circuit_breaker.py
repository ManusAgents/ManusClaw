"""Tests for the circuit breaker and its ProfileRotator integration."""

import pytest

from app.llm.circuit_breaker import BreakerState, CircuitBreaker
from app.llm.profile_rotation import ModelEntry, ModelProfile, ProfileRotator


@pytest.fixture
def clock():
    """Controllable monotonic clock."""
    state = {"now": 100.0}
    return state, lambda: state["now"]


def make_breaker(clock, threshold=3, reset_s=60.0):
    _, clock_fn = clock
    return CircuitBreaker(failure_threshold=threshold, reset_s=reset_s, clock=clock_fn)


# ── State machine ────────────────────────────────────────────────────────────

def test_closed_allows_calls(clock):
    breaker = make_breaker(clock)
    assert breaker.state_of("k") is BreakerState.CLOSED
    assert breaker.allow("k") is True
    assert breaker.allow("k") is True


def test_opens_after_threshold_consecutive_failures(clock):
    breaker = make_breaker(clock, threshold=3)
    breaker.record_failure("k")
    breaker.record_failure("k")
    assert breaker.allow("k") is True  # below threshold → still CLOSED
    breaker.record_failure("k")
    assert breaker.state_of("k") is BreakerState.OPEN
    assert breaker.allow("k") is False


def test_success_resets_failure_count(clock):
    breaker = make_breaker(clock, threshold=3)
    breaker.record_failure("k")
    breaker.record_failure("k")
    breaker.record_success("k")
    breaker.record_failure("k")
    breaker.record_failure("k")
    assert breaker.state_of("k") is BreakerState.CLOSED  # never hit 3 consecutive


def test_half_open_single_probe_after_reset_window(clock):
    breaker = make_breaker(clock, threshold=1, reset_s=60.0)
    breaker.record_failure("k")
    assert breaker.allow("k") is False

    clock[0]["now"] += 61.0
    # One probe granted, further calls denied until the probe resolves
    assert breaker.allow("k") is True
    assert breaker.allow("k") is False
    assert breaker.state_of("k") is BreakerState.HALF_OPEN


def test_probe_success_closes_breaker(clock):
    breaker = make_breaker(clock, threshold=1, reset_s=60.0)
    breaker.record_failure("k")
    clock[0]["now"] += 61.0
    assert breaker.allow("k") is True  # the probe
    breaker.record_success("k")
    assert breaker.state_of("k") is BreakerState.CLOSED
    assert breaker.allow("k") is True


def test_probe_failure_reopens_with_new_window(clock):
    breaker = make_breaker(clock, threshold=1, reset_s=60.0)
    breaker.record_failure("k")
    clock[0]["now"] += 61.0
    assert breaker.allow("k") is True  # the probe
    breaker.record_failure("k")
    assert breaker.state_of("k") is BreakerState.OPEN
    assert breaker.allow("k") is False
    clock[0]["now"] += 61.0
    assert breaker.allow("k") is True  # next probe window


def test_open_remaining_s(clock):
    breaker = make_breaker(clock, threshold=1, reset_s=30.0)
    breaker.record_failure("k")
    assert breaker.open_remaining_s("k") == pytest.approx(30.0)
    clock[0]["now"] += 10.0
    assert breaker.open_remaining_s("k") == pytest.approx(20.0)
    clock[0]["now"] += 100.0
    assert breaker.open_remaining_s("k") == 0.0


def test_disabled_breaker_always_allows(clock):
    breaker = make_breaker(clock, threshold=0)
    for _ in range(10):
        breaker.record_failure("k")
    assert breaker.allow("k") is True
    assert breaker.state_of("k") is BreakerState.CLOSED


def test_per_key_isolation(clock):
    breaker = make_breaker(clock, threshold=1)
    breaker.record_failure("dead")
    assert breaker.allow("dead") is False
    assert breaker.allow("healthy") is True


def test_snapshot(clock):
    breaker = make_breaker(clock, threshold=2)
    breaker.record_failure("k")
    snap = breaker.snapshot()
    assert snap["k"]["state"] == "closed"
    assert snap["k"]["failures"] == 1
    breaker.record_failure("k")
    snap = breaker.snapshot()
    assert snap["k"]["state"] == "open"


def test_from_env(monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("MANUSCLAW_CB_RESET_S", "12.5")
    breaker = CircuitBreaker.from_env()
    assert breaker.failure_threshold == 5
    assert breaker.reset_s == pytest.approx(12.5)

    monkeypatch.setenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "not-a-number")
    breaker = CircuitBreaker.from_env()
    assert breaker.failure_threshold == 3  # fallback to default


# ── ProfileRotator integration ───────────────────────────────────────────────

async def test_rotator_skips_breaker_open_entry(monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("MANUSCLAW_CB_RESET_S", "60")

    primary = ModelEntry(provider="openai", model="m1", priority=0, cooldown_s=0.0)
    backup = ModelEntry(provider="anthropic", model="m2", priority=1, cooldown_s=0.0)
    rotator = ProfileRotator(ModelProfile([primary, backup]))

    # Two consecutive failures → breaker OPEN for primary
    first = await rotator.get_next()
    assert first.key == primary.key
    await rotator.mark_failed(first)

    second = await rotator.get_next()
    assert second.key == primary.key  # still CLOSED after one failure
    await rotator.mark_failed(second)

    # cooldown_s=0 means cooldown alone wouldn't skip it — the breaker does
    third = await rotator.get_next()
    assert third.key == backup.key
    again = await rotator.get_next()
    assert again.key == backup.key

    summary = rotator.status_summary()
    assert "OPEN" in summary


async def test_rotator_success_resets_breaker(monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("MANUSCLAW_CB_RESET_S", "60")

    primary = ModelEntry(provider="openai", model="m1", priority=0, cooldown_s=0.0)
    backup = ModelEntry(provider="anthropic", model="m2", priority=1, cooldown_s=0.0)
    rotator = ProfileRotator(ModelProfile([primary, backup]))

    await rotator.mark_failed(await rotator.get_next())
    await rotator.mark_failed(await rotator.get_next())
    assert (await rotator.get_next()).key == backup.key

    await rotator.mark_success(primary)  # resets the breaker for primary
    assert (await rotator.get_next()).key == primary.key


async def test_rotator_breaker_disabled(monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "0")

    primary = ModelEntry(provider="openai", model="m1", priority=0, cooldown_s=0.0)
    rotator = ProfileRotator(ModelProfile([primary]))

    for _ in range(5):
        await rotator.mark_failed(primary)
        entry = await rotator.get_next()
        assert entry.key == primary.key
