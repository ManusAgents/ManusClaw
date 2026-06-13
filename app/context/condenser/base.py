from __future__ import annotations

"""
Condenser Base — Abstract interface and data structures for context condensation.

A Condenser takes a View (the current LLM context) and optionally produces
a CondensationAction that describes which events to forget and what summary
to insert in their place.

Condensation Reasons:
  - REQUEST (hard):  User or system explicitly requested condensation.
  - TOKENS  (soft):  Token budget is approaching its limit.
  - EVENTS  (soft):  Too many events in context; proactive trimming.

Hard reasons bypass throttling and should always be honoured.
Soft reasons may be deferred or rate-limited.
"""

import time
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.logger import logger
from app.context.view import View


# ──────────────────────────────────────────────────────────────────────────────
# Condensation reason classification
# ──────────────────────────────────────────────────────────────────────────────

class CondensationReason(str, Enum):
    """
    Why condensation was triggered.

    - REQUEST: Hard — user or system explicitly asked for condensation.
               Must be honoured immediately.
    - TOKENS:  Soft — token budget is approaching limit. May be deferred.
    - EVENTS:  Soft — too many events. May be deferred.
    """
    REQUEST = "request"
    TOKENS  = "tokens"
    EVENTS  = "events"

    @property
    def is_hard(self) -> bool:
        return self == CondensationReason.REQUEST

    @property
    def is_soft(self) -> bool:
        return not self.is_hard


# ──────────────────────────────────────────────────────────────────────────────
# Condensation action — what to do
# ──────────────────────────────────────────────────────────────────────────────

class CondensationAction(BaseModel):
    """
    Describes the result of a condensation operation.

    Attributes:
        forgotten_event_ids: IDs of events to remove from the view.
        summary:             Optional summary text to insert, replacing
                             the forgotten events.
        summary_event:       Pre-built Event to insert (if summary is set).
        reason:              Why condensation was triggered.
        metrics:             Performance metrics from the condensation.
    """
    forgotten_event_ids: set[str] = Field(default_factory=set)
    summary: Optional[str] = None
    summary_event: Optional[dict] = None  # serialised Event
    reason: CondensationReason = CondensationReason.EVENTS
    metrics: dict = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def has_summary(self) -> bool:
        return self.summary is not None

    @property
    def num_forgotten(self) -> int:
        return len(self.forgotten_event_ids)

    def to_summary_line(self) -> str:
        return (
            f"CondensationAction(forgotten={self.num_forgotten}, "
            f"has_summary={self.has_summary}, reason={self.reason.value})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Condensation event — persisted record of a condensation
# ──────────────────────────────────────────────────────────────────────────────

class Condensation(BaseModel):
    """
    A persisted record of a condensation action.

    This event is inserted into the view in place of the forgotten
    events, preserving a summary of what was removed.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    forgotten_event_ids: set[str] = Field(default_factory=set)
    summary: str = ""
    reason: CondensationReason = CondensationReason.EVENTS
    timestamp: float = Field(default_factory=time.time)

    @property
    def num_forgotten(self) -> int:
        return len(self.forgotten_event_ids)

    def to_event_content(self) -> str:
        """Format as text suitable for insertion into the view."""
        lines = [
            "[CONDENSATION]",
            f"Reason: {self.reason.value}",
            f"Events forgotten: {self.num_forgotten}",
        ]
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        lines.append("[END CONDENSATION]")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Condenser metrics
# ──────────────────────────────────────────────────────────────────────────────

class CondenserMetrics(BaseModel):
    """Performance metrics for a condenser instance."""
    total_calls: int = 0
    total_condensations: int = 0
    total_events_removed: int = 0
    total_tokens_saved: int = 0
    total_time_s: float = 0.0
    last_condensation_time: Optional[float] = None
    errors: int = 0

    def record_call(self, duration_s: float, events_removed: int = 0,
                    tokens_saved: int = 0, was_condensation: bool = False,
                    error: bool = False) -> None:
        self.total_calls += 1
        self.total_time_s += duration_s
        if was_condensation:
            self.total_condensations += 1
            self.total_events_removed += events_removed
            self.total_tokens_saved += tokens_saved
            self.last_condensation_time = time.time()
        if error:
            self.errors += 1

    def summary(self) -> str:
        return (
            f"Calls={self.total_calls} Condensations={self.total_condensations} "
            f"Removed={self.total_events_removed} Saved={self.total_tokens_saved}tok "
            f"Time={self.total_time_s:.2f}s Errors={self.errors}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# CondenserBase — abstract interface
# ──────────────────────────────────────────────────────────────────────────────

class CondenserBase(ABC):
    """
    Abstract base class for all context condensers.

    A condenser examines the current View and optionally returns a
    CondensationAction describing which events to remove and what
    summary to insert.

    Subclasses must implement:
      - condense(view, reason) -> Optional[CondensationAction]

    The condense() method should be:
      - Thread-safe
      - Crash-proof (catch all exceptions internally)
      - Idempotent (calling twice with the same view yields same result)
      - Fast (< 100ms for typical views, except LLM-based condensers)
    """

    def __init__(self, name: Optional[str] = None) -> None:
        self._name = name or self.__class__.__name__
        self._metrics = CondenserMetrics()

    @property
    def name(self) -> str:
        return self._name

    @property
    def metrics(self) -> CondenserMetrics:
        return self._metrics

    @abstractmethod
    def condense(
        self,
        view: View,
        reason: CondensationReason = CondensationReason.EVENTS,
    ) -> Optional[CondensationAction]:
        """
        Examine the view and optionally return a CondensationAction.

        Args:
            view:   The current context view.
            reason: Why condensation is being considered.

        Returns:
            A CondensationAction if condensation is warranted, or None
            if the view should be left unchanged.
        """
        ...

    def should_condense(self, view: View) -> bool:
        """
        Quick check whether condensation might be needed, without
        performing the full condensation computation.

        Default implementation returns True (always check).
        Subclasses can override for efficiency.
        """
        return True

    def apply(self, view: View, action: CondensationAction) -> View:
        """
        Apply a CondensationAction to a View, producing a new View.

        This is a helper that:
          1. Removes forgotten events.
          2. Inserts a summary event (if any).
          3. Enforces all properties on the result.
        """
        start = time.time()
        try:
            if not action.forgotten_event_ids and not action.summary:
                return view

            # Remove forgotten events
            new_view = view.remove_event_ids(action.forgotten_event_ids)

            # Insert summary if provided
            if action.summary:
                from app.context.view_properties import Event
                summary_event = Event.user(
                    content=action.summary,
                    metadata={"condensation": True, "reason": action.reason.value},
                )
                new_view.add(summary_event)

            # Enforce properties
            new_view.enforce_properties()

            duration = time.time() - start
            tokens_saved = view.token_estimate() - new_view.token_estimate()
            self._metrics.record_call(
                duration_s=duration,
                events_removed=action.num_forgotten,
                tokens_saved=max(0, tokens_saved),
                was_condensation=True,
            )
            logger.info(
                f"[{self._name}] Applied condensation: "
                f"removed={action.num_forgotten} "
                f"tokens_saved={tokens_saved} "
                f"duration={duration:.3f}s"
            )
            return new_view

        except Exception as e:
            duration = time.time() - start
            self._metrics.record_call(duration_s=duration, error=True)
            logger.error(
                f"[{self._name}] Error applying condensation: {e}",
                exc_info=True,
            )
            # Return original view on error — crash-proof
            return view

    def reset(self) -> None:
        """Reset internal state. Called when a new session starts."""
        self._metrics = CondenserMetrics()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self._name})"
