from __future__ import annotations

"""
View — Linear projection of events for LLM consumption.

A View is an ordered sequence of Events that represents the current
context window visible to the LLM.  It supports:

  - **Incremental maintenance**: add events one at a time without
    rebuilding structural metadata from scratch.
  - **Manipulation indices**: safe points where events can be removed
    without violating any registered ViewProperty.
  - **Property enforcement**: after condensation, enforce_properties()
    ensures the resulting view is structurally valid.
  - **keep_first**: the first N events (typically the system prompt)
    are never candidates for removal.

The View is the core data structure that condensers operate on.
"""

import threading
import uuid
from typing import Optional

from app.logger import logger
from app.context.view_properties import (
    Event,
    ViewProperty,
    BatchAtomicity,
    ObservationUniqueness,
    ToolCallMatching,
    ToolLoopAtomicity,
    DEFAULT_PROPERTIES,
)


class View:
    """
    A linearly ordered projection of events for LLM consumption.

    The view maintains:
      - A list of Event objects in order.
      - A set of manipulation_indices (positions safe for removal).
      - A keep_first count (events 0..keep_first-1 are never removable).
      - Registered ViewProperty instances that govern structural integrity.

    Thread Safety:
      All mutations are protected by an internal lock.  Read-only
      properties snapshot the current state under the lock.
    """

    def __init__(
        self,
        events: Optional[list[Event]] = None,
        properties: Optional[list[ViewProperty]] = None,
        keep_first: int = 1,
    ) -> None:
        self._events: list[Event] = list(events) if events else []
        self._properties: list[ViewProperty] = list(properties) if properties else list(DEFAULT_PROPERTIES)
        self._keep_first: int = keep_first
        self._lock = threading.RLock()
        self._id: str = uuid.uuid4().hex[:8]

        # Cached manipulation indices — invalidated on mutation
        self._manipulation_indices_cache: Optional[set[int]] = None
        self._cache_valid: bool = False

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self._id

    # ── Core accessors ───────────────────────────────────────────────────

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    @property
    def keep_first(self) -> int:
        return self._keep_first

    @keep_first.setter
    def keep_first(self, value: int) -> None:
        with self._lock:
            self._keep_first = max(0, value)
            self._invalidate_cache()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def __getitem__(self, index: int) -> Event:
        with self._lock:
            return self._events[index]

    def __iter__(self):
        with self._lock:
            return iter(list(self._events))

    # ── Manipulation indices ─────────────────────────────────────────────

    @property
    def manipulation_indices(self) -> set[int]:
        """
        Return the set of indices where events can be safely removed.

        This is the intersection of safe indices across all registered
        properties, minus the keep_first zone.
        """
        with self._lock:
            if self._cache_valid and self._manipulation_indices_cache is not None:
                return set(self._manipulation_indices_cache)

            if not self._events:
                self._manipulation_indices_cache = set()
                self._cache_valid = True
                return set()

            # Compute intersection of all properties' safe indices
            safe_sets: list[set[int]] = []
            for prop in self._properties:
                safe_sets.append(prop.safe_indices(self._events))

            if safe_sets:
                combined = safe_sets[0]
                for s in safe_sets[1:]:
                    combined = combined & s
            else:
                combined = set(range(len(self._events)))

            # Remove keep_first zone
            combined = {i for i in combined if i >= self._keep_first}

            self._manipulation_indices_cache = combined
            self._cache_valid = True
            return set(combined)

    # ── Mutation methods ─────────────────────────────────────────────────

    def add(self, event: Event) -> None:
        """Append an event to the view.  Invalidates manipulation cache."""
        with self._lock:
            self._events.append(event)
            self._invalidate_cache()
            logger.trace(f"[View:{self._id}] Added event id={event.id} "
                         f"role={event.role} total={len(self._events)}")

    def add_events(self, events: list[Event]) -> None:
        """Append multiple events to the view."""
        with self._lock:
            self._events.extend(events)
            self._invalidate_cache()
            logger.trace(f"[View:{self._id}] Added {len(events)} events "
                         f"total={len(self._events)}")

    def remove_indices(self, indices: set[int]) -> "View":
        """
        Remove events at the given indices and return a new View
        with the remaining events.

        Does NOT modify this view in-place — returns a new View.
        Also enforces all properties on the result.
        """
        with self._lock:
            remaining = [
                event for i, event in enumerate(self._events)
                if i not in indices
            ]
            new_view = View(
                events=remaining,
                properties=list(self._properties),
                keep_first=self._keep_first,
            )
            # Enforce properties on the new view
            new_view.enforce_properties()
            logger.debug(
                f"[View:{self._id}] Removed {len(indices)} indices, "
                f"remaining={len(remaining)}"
            )
            return new_view

    def remove_event_ids(self, event_ids: set[str]) -> "View":
        """
        Remove events whose IDs are in *event_ids* and return a new View.

        Does NOT modify this view in-place.
        """
        with self._lock:
            remaining = [
                event for event in self._events
                if event.id not in event_ids
            ]
            new_view = View(
                events=remaining,
                properties=list(self._properties),
                keep_first=self._keep_first,
            )
            new_view.enforce_properties()
            logger.debug(
                f"[View:{self._id}] Removed {len(event_ids)} event IDs, "
                f"remaining={len(remaining)}"
            )
            return new_view

    # ── Property enforcement ─────────────────────────────────────────────

    def enforce_properties(self) -> None:
        """
        Run all registered properties' enforce() methods in sequence
        on the current events list.

        Modifies this view in-place.  Typically called after condensation
        to ensure structural integrity.
        """
        with self._lock:
            for prop in self._properties:
                before = len(self._events)
                self._events = prop.enforce(self._events)
                removed = before - len(self._events)
                if removed > 0:
                    logger.info(
                        f"[View:{self._id}] {prop.__class__.__name__} "
                        f"removed {removed} events"
                    )
            self._invalidate_cache()

    def check_properties(self) -> dict[str, bool]:
        """
        Check all registered properties and return a dict of
        property_name -> satisfied (bool).
        """
        with self._lock:
            results: dict[str, bool] = {}
            for prop in self._properties:
                name = prop.__class__.__name__
                results[name] = prop.check(self._events)
            return results

    def is_valid(self) -> bool:
        """Return True if all properties are satisfied."""
        return all(self.check_properties().values())

    # ── Conversion ───────────────────────────────────────────────────────

    def to_messages(self) -> list[dict]:
        """
        Convert events to the message format expected by the LLM.

        Returns a list of dicts with role, content, tool_calls, etc.
        """
        with self._lock:
            return [event.to_dict() for event in self._events]

    def to_event_ids(self) -> list[str]:
        """Return ordered list of event IDs."""
        with self._lock:
            return [e.id for e in self._events]

    # ── Token estimation ─────────────────────────────────────────────────

    def token_estimate(self) -> int:
        """
        Rough token estimate (chars / 4).
        More accurate than nothing, less accurate than tiktoken.
        """
        with self._lock:
            total = sum(len(e.content or "") for e in self._events)
            # Account for tool_calls arguments
            for e in self._events:
                if e.tool_calls:
                    for tc in e.tool_calls:
                        func = tc.get("function", {})
                        total += len(func.get("arguments", ""))
            return max(1, total // 4)

    # ── Snapshot / diff ──────────────────────────────────────────────────

    def snapshot(self) -> list[Event]:
        """Return a deep copy of current events for comparison."""
        with self._lock:
            return [e.model_copy() for e in self._events]

    def diff(self, other: "View") -> tuple[set[str], set[str]]:
        """
        Compare this view with another and return:
          (ids_only_in_self, ids_only_in_other)
        """
        my_ids = {e.id for e in self._events}
        other_ids = {e.id for e in other._events}
        return (my_ids - other_ids, other_ids - my_ids)

    # ── Summary ──────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Return a human-readable summary of the view state."""
        with self._lock:
            role_counts: dict[str, int] = {}
            for e in self._events:
                role_counts[e.role] = role_counts.get(e.role, 0) + 1

            props = self.check_properties()
            props_str = ", ".join(
                f"{k}={'OK' if v else 'FAIL'}" for k, v in props.items()
            )

            return (
                f"View(id={self._id}, events={len(self._events)}, "
                f"roles={role_counts}, "
                f"manipulable={len(self.manipulation_indices)}, "
                f"keep_first={self._keep_first}, "
                f"tokens~={self.token_estimate()}, "
                f"properties=[{props_str}])"
            )

    # ── Internal ─────────────────────────────────────────────────────────

    def _invalidate_cache(self) -> None:
        self._cache_valid = False
        self._manipulation_indices_cache = None

    def __repr__(self) -> str:
        return (
            f"View(id={self._id}, events={len(self._events)}, "
            f"keep_first={self._keep_first})"
        )
