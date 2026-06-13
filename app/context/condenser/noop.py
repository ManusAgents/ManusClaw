from __future__ import annotations

"""
NoOpCondenser — Passthrough condenser that never condenses.

Useful as a default, for testing, and as a terminal element in a
PipelineCondenser chain.
"""

from typing import Optional

from app.logger import logger
from app.context.view import View
from app.context.condenser.base import (
    CondenserBase,
    CondensationAction,
    CondensationReason,
)


class NoOpCondenser(CondenserBase):
    """
    A condenser that never condenses — always returns None.

    Use cases:
      - Default condenser when no condensation is desired.
      - Testing: verify that the pipeline works without condensation.
      - Pipeline terminal: a no-op at the end of a condenser chain.
    """

    def __init__(self, name: Optional[str] = None) -> None:
        super().__init__(name=name or "NoOpCondenser")

    def condense(
        self,
        view: View,
        reason: CondensationReason = CondensationReason.EVENTS,
    ) -> Optional[CondensationAction]:
        """
        Always returns None — no condensation is performed.
        """
        logger.trace(f"[{self._name}] NoOp: skipping condensation "
                      f"(view has {len(view)} events, reason={reason.value})")
        return None

    def should_condense(self, view: View) -> bool:
        """NoOp never needs to condense."""
        return False
