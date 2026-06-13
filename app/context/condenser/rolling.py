from __future__ import annotations

"""
RollingCondenser — Keep the last N events.

A simple condenser that maintains a rolling window of the most recent
N events.  Events beyond the window are forgotten (removed).

Respects:
  - keep_first:  Events in the keep_first zone are always retained.
  - manipulation_indices: Only removes events at safe indices.
  - Properties: After removal, all registered properties are enforced.

This is the simplest non-trivial condenser and serves as a baseline
for more sophisticated approaches.
"""

import time
from typing import Optional

from app.logger import logger
from app.context.view import View
from app.context.condenser.base import (
    CondenserBase,
    CondensationAction,
    CondensationReason,
)


class RollingCondenser(CondenserBase):
    """
    Keep only the last `max_events` events in the view.

    Events in the `keep_first` zone are always retained regardless of
    the window size.  If the view has fewer than `max_events` events
    (after accounting for keep_first), no condensation occurs.

    Args:
        max_events: Maximum number of events to keep (excluding keep_first).
        name:       Optional name for this condenser instance.
    """

    def __init__(
        self,
        max_events: int = 50,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(name=name or "RollingCondenser")
        if max_events < 1:
            raise ValueError(f"max_events must be >= 1, got {max_events}")
        self._max_events = max_events

    @property
    def max_events(self) -> int:
        return self._max_events

    @max_events.setter
    def max_events(self, value: int) -> None:
        if value < 1:
            raise ValueError(f"max_events must be >= 1, got {value}")
        self._max_events = value

    def should_condense(self, view: View) -> bool:
        """Return True if the view exceeds max_events."""
        return len(view) > self._max_events + view.keep_first

    def condense(
        self,
        view: View,
        reason: CondensationReason = CondensationReason.EVENTS,
    ) -> Optional[CondensationAction]:
        """
        Determine which events to remove to fit within max_events.

        Strategy:
          1. Identify events in the keep_first zone (always kept).
          2. From the remaining events, keep the most recent `max_events`.
          3. Only remove events that are in manipulation_indices.
          4. If no events can be safely removed, return None.
        """
        start = time.time()
        try:
            events = view.events
            total = len(events)

            if total <= self._max_events + view.keep_first:
                logger.trace(
                    f"[{self._name}] View within limits "
                    f"({total} <= {self._max_events + view.keep_first})"
                )
                return None

            # Get safe removal indices
            manipulable = view.manipulation_indices

            # Determine which events to keep
            keep_first = view.keep_first

            # Indices we want to remove: everything beyond the window
            # Window = keep_first zone + last max_events
            cutoff_index = total - self._max_events
            if cutoff_index <= keep_first:
                # Can't remove anything in keep_first zone
                logger.debug(
                    f"[{self._name}] Cannot condense: cutoff {cutoff_index} "
                    f"is within keep_first zone {keep_first}"
                )
                return None

            # Candidate indices for removal (between keep_first and cutoff)
            candidate_indices = set(range(keep_first, cutoff_index))

            # Only remove indices that are in manipulation_indices
            removable_indices = candidate_indices & manipulable

            if not removable_indices:
                logger.debug(
                    f"[{self._name}] No safe indices to remove "
                    f"(candidates={len(candidate_indices)}, "
                    f"manipulable={len(manipulable)})"
                )
                return None

            # Collect event IDs to forget
            forgotten_ids: set[str] = set()
            for idx in removable_indices:
                if idx < len(events):
                    forgotten_ids.add(events[idx].id)

            if not forgotten_ids:
                return None

            duration = time.time() - start
            self._metrics.record_call(duration_s=duration)

            logger.info(
                f"[{self._name}] Condensing: removing {len(forgotten_ids)} "
                f"of {total} events (max_events={self._max_events})"
            )

            return CondensationAction(
                forgotten_event_ids=forgotten_ids,
                reason=reason,
                metrics={
                    "duration_s": round(duration, 4),
                    "total_events": total,
                    "removed_events": len(forgotten_ids),
                    "max_events": self._max_events,
                },
            )

        except Exception as e:
            duration = time.time() - start
            self._metrics.record_call(duration_s=duration, error=True)
            logger.error(
                f"[{self._name}] Error during condensation: {e}",
                exc_info=True,
            )
            return None
