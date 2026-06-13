from __future__ import annotations

"""
ManusClaw Context Condensers
==============================

Condensers reduce the size of the LLM context window by removing
or summarizing events while preserving structural integrity.

Available condensers:
  - NoOpCondenser:           Passthrough, never condenses.
  - RollingCondenser:        Keep last N events.
  - LLMSummarizingCondenser: LLM-powered summarization with progressive
                              truncation fallback.
  - PipelineCondenser:       Chain multiple condensers in sequence.

Base classes and types:
  - CondenserBase:           Abstract base class for all condensers.
  - CondensationAction:      Describes what to remove and what summary to insert.
  - Condensation:            Persisted record of a condensation event.
  - CondensationReason:      Why condensation was triggered.
  - CondenserMetrics:        Performance metrics for a condenser.
"""

from app.context.condenser.base import (
    CondenserBase,
    CondensationAction,
    Condensation,
    CondensationReason,
    CondenserMetrics,
)
from app.context.condenser.noop import NoOpCondenser
from app.context.condenser.rolling import RollingCondenser
from app.context.condenser.llm_summarizing import LLMSummarizingCondenser
from app.context.condenser.pipeline import PipelineCondenser

__all__ = [
    # Base
    "CondenserBase",
    "CondensationAction",
    "Condensation",
    "CondensationReason",
    "CondenserMetrics",
    # Implementations
    "NoOpCondenser",
    "RollingCondenser",
    "LLMSummarizingCondenser",
    "PipelineCondenser",
]
