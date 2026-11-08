from __future__ import annotations

"""
Circuit breaker for model entries (provider/model pairs).

Closes the documented ProfileRotator gap: previously a failing entry was
retried after its fixed cooldown forever, no matter how many times it
failed. With the breaker, an entry that fails ``failure_threshold``
consecutive times transitions CLOSED → OPEN and is skipped entirely
(fail-fast) until the reset window elapses, then gets exactly ONE
half-open probe call:

- probe succeeds → CLOSED (counters reset)
- probe fails    → OPEN again (new reset window)

Configured via environment:

- ``MANUSCLAW_CB_FAILURE_THRESHOLD`` — consecutive failures before OPEN
  (default 3; ``0`` disables the breaker)
- ``MANUSCLAW_CB_RESET_S`` — seconds before an OPEN breaker allows a
  half-open probe (default 60)

State is kept in memory per process, matching ProfileRotator's stats.
"""

import os
import time
from enum import Enum
from typing import Callable, Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _EntryState:
    __slots__ = ("state", "failures", "opened_at", "probes_in_flight")

    def __init__(self) -> None:
        self.state: BreakerState = BreakerState.CLOSED
        self.failures: int = 0
        self.opened_at: float = 0.0
        self.probes_in_flight: int = 0


class CircuitBreaker:
    """Per-key circuit breaker with a half-open probe phase."""

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(0, int(failure_threshold))
        self._reset_s = max(0.0, float(reset_s))
        self._clock = clock
        self._entries: dict[str, _EntryState] = {}

    @classmethod
    def from_env(cls) -> "CircuitBreaker":
        """Build a breaker from MANUSCLAW_CB_* environment variables."""
        try:
            threshold = int(os.getenv("MANUSCLAW_CB_FAILURE_THRESHOLD", "3"))
        except ValueError:
            threshold = 3
        try:
            reset_s = float(os.getenv("MANUSCLAW_CB_RESET_S", "60"))
        except ValueError:
            reset_s = 60.0
        return cls(failure_threshold=threshold, reset_s=reset_s)

    @property
    def failure_threshold(self) -> int:
        return self._threshold

    @property
    def reset_s(self) -> float:
        return self._reset_s

    def allow(self, key: str) -> bool:
        """Whether a call for ``key`` may proceed right now."""
        if self._threshold <= 0:
            return True
        entry = self._entries.get(key)
        if entry is None or entry.state is BreakerState.CLOSED:
            return True

        now = self._clock()
        if entry.state is BreakerState.OPEN:
            if now - entry.opened_at >= self._reset_s:
                entry.state = BreakerState.HALF_OPEN
                entry.probes_in_flight = 0
            else:
                return False

        if entry.state is BreakerState.HALF_OPEN:
            if entry.probes_in_flight >= 1:
                return False
            entry.probes_in_flight += 1
            return True

        return True

    def record_failure(self, key: str) -> None:
        """Count a failure; trip OPEN at the threshold or on a failed probe."""
        if self._threshold <= 0:
            return
        entry = self._entries.setdefault(key, _EntryState())
        entry.failures += 1
        if entry.probes_in_flight > 0:
            entry.probes_in_flight -= 1
        if entry.state is BreakerState.HALF_OPEN or entry.failures >= self._threshold:
            entry.state = BreakerState.OPEN
            entry.opened_at = self._clock()

    def record_success(self, key: str) -> None:
        """A successful call fully resets the breaker for ``key``."""
        self._entries.pop(key, None)

    def reset_all(self) -> None:
        self._entries.clear()

    def state_of(self, key: str) -> BreakerState:
        """Current breaker state for ``key`` (CLOSED when disabled/unknown)."""
        if self._threshold <= 0:
            return BreakerState.CLOSED
        entry = self._entries.get(key)
        if entry is None:
            return BreakerState.CLOSED
        if entry.state is BreakerState.OPEN and self._reset_s > 0:
            if self._clock() - entry.opened_at >= self._reset_s:
                return BreakerState.HALF_OPEN
        return entry.state

    def open_remaining_s(self, key: str) -> float:
        """Seconds until an OPEN breaker grants its half-open probe."""
        entry = self._entries.get(key)
        if entry is None or entry.state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, entry.opened_at + self._reset_s - self._clock())

    def snapshot(self) -> dict[str, dict[str, Optional[float]]]:
        """Observability snapshot: per-key state and failure counts."""
        return {
            key: {
                "state": self.state_of(key).value,
                "failures": entry.failures,
                "open_remaining_s": self.open_remaining_s(key),
            }
            for key, entry in self._entries.items()
        }
