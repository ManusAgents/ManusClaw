from __future__ import annotations

"""
ManusClaw Context Management System
======================================

This module provides OpenHands-style context management for the manusclaw
AI operating environment.  It consists of two main components:

1. **View**: A linearly ordered projection of events for LLM consumption.
   Supports incremental maintenance, manipulation indices (safe removal
   points), and property enforcement.

2. **Condenser**: Pluggable strategies for reducing context size while
   preserving structural integrity.  Includes rolling windows, LLM-based
   summarization, and composable pipelines.

Quick start:

    from app.context import View, Event, RollingCondenser

    # Build a view
    view = View(keep_first=1)  # protect the system prompt
    view.add(Event.user("Hello, world!"))

    # Condense when needed
    condenser = RollingCondenser(max_events=50)
    if condenser.should_condense(view):
        action = condenser.condense(view)
        if action:
            view = condenser.apply(view, action)
"""

# ── View & Events ─────────────────────────────────────────────────────────────

from app.context.view import View
from app.context.view_properties import (
    Event,
    ViewProperty,
    BatchAtomicity,
    ObservationUniqueness,
    ToolCallMatching,
    ToolLoopAtomicity,
    DEFAULT_PROPERTIES,
)

# ── Condensers ────────────────────────────────────────────────────────────────

from app.context.condenser import (
    CondenserBase,
    CondensationAction,
    Condensation,
    CondensationReason,
    CondenserMetrics,
    NoOpCondenser,
    RollingCondenser,
    LLMSummarizingCondenser,
    PipelineCondenser,
)

__all__ = [
    # View & Events
    "View",
    "Event",
    "ViewProperty",
    "BatchAtomicity",
    "ObservationUniqueness",
    "ToolCallMatching",
    "ToolLoopAtomicity",
    "DEFAULT_PROPERTIES",
    # Condensers
    "CondenserBase",
    "CondensationAction",
    "Condensation",
    "CondensationReason",
    "CondenserMetrics",
    "NoOpCondenser",
    "RollingCondenser",
    "LLMSummarizingCondenser",
    "PipelineCondenser",
]
