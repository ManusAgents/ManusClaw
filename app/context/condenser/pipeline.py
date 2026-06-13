from __future__ import annotations

"""
PipelineCondenser — Chain multiple condensers in sequence.

A PipelineCondenser applies a sequence of condensers to a View,
passing the output of each condenser as the input to the next.

If any condenser in the pipeline produces a CondensationAction,
the action is applied before passing the view to the next condenser.
This allows condensers to build on each other's work:

  Example pipeline:
    1. RollingCondenser(max_events=100)  — trim to 100 events
    2. LLMSummarizingCondenser(max_events=50) — summarize and trim to 50

The pipeline short-circuits if a condenser returns None (no action),
passing the view through unchanged.

Thread Safety:
  The pipeline itself is thread-safe.  Each condenser in the chain is
  responsible for its own thread safety.
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


class PipelineCondenser(CondenserBase):
    """
    Chain multiple condensers and apply them in sequence.

    Args:
        condensers: Ordered list of condensers to apply.
        stop_on_first: If True, stop after the first condenser that
                       returns an action.  If False, apply all condensers.
        name:       Optional name for this pipeline.

    The pipeline accumulates forgotten_event_ids across all condensers
    and produces a single merged CondensationAction.
    """

    def __init__(
        self,
        condensers: Optional[list[CondenserBase]] = None,
        stop_on_first: bool = False,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(name=name or "PipelineCondenser")
        self._condensers: list[CondenserBase] = list(condensers) if condensers else []
        self._stop_on_first = stop_on_first

    # ── Pipeline management ──────────────────────────────────────────────

    @property
    def condensers(self) -> list[CondenserBase]:
        return list(self._condensers)

    def add(self, condenser: CondenserBase) -> None:
        """Append a condenser to the pipeline."""
        self._condensers.append(condenser)
        logger.debug(f"[{self._name}] Added condenser: {condenser.name}")

    def insert(self, index: int, condenser: CondenserBase) -> None:
        """Insert a condenser at a specific position."""
        self._condensers.insert(index, condenser)
        logger.debug(
            f"[{self._name}] Inserted condenser at {index}: {condenser.name}"
        )

    def remove(self, condenser: CondenserBase) -> None:
        """Remove a condenser from the pipeline."""
        self._condensers.remove(condenser)
        logger.debug(f"[{self._name}] Removed condenser: {condenser.name}")

    def clear(self) -> None:
        """Remove all condensers from the pipeline."""
        self._condensers.clear()
        logger.debug(f"[{self._name}] Cleared all condensers")

    # ── should_condense ──────────────────────────────────────────────────

    def should_condense(self, view: View) -> bool:
        """Return True if any condenser in the pipeline thinks condensation is needed."""
        return any(c.should_condense(view) for c in self._condensers)

    # ── condense ─────────────────────────────────────────────────────────

    def condense(
        self,
        view: View,
        reason: CondensationReason = CondensationReason.EVENTS,
    ) -> Optional[CondensationAction]:
        """
        Run all condensers in sequence, accumulating their actions.

        Each condenser receives the view after previous condensers'
        actions have been applied.  The final result is a merged
        CondensationAction containing all forgotten IDs and the
        last summary (if any).
        """
        start = time.time()

        if not self._condensers:
            logger.trace(f"[{self._name}] Empty pipeline, no condensation")
            return None

        current_view = view
        all_forgotten_ids: set[str] = set()
        latest_summary: Optional[str] = None
        latest_reason = reason
        condenser_results: list[dict] = []
        actions_taken = 0

        for i, condenser in enumerate(self._condensers):
            condenser_start = time.time()
            try:
                # Check if this condenser thinks it should condense
                if not condenser.should_condense(current_view):
                    condenser_results.append({
                        "condenser": condenser.name,
                        "action": "skipped",
                        "duration_s": round(time.time() - condenser_start, 4),
                    })
                    continue

                # Run the condenser
                action = condenser.condense(current_view, reason)

                if action is not None:
                    # Apply the action to get a new view for the next condenser
                    current_view = condenser.apply(current_view, action)
                    all_forgotten_ids.update(action.forgotten_event_ids)

                    if action.summary:
                        latest_summary = action.summary
                    latest_reason = action.reason

                    actions_taken += 1
                    condenser_results.append({
                        "condenser": condenser.name,
                        "action": "condensed",
                        "forgotten": len(action.forgotten_event_ids),
                        "has_summary": action.has_summary,
                        "duration_s": round(time.time() - condenser_start, 4),
                    })

                    if self._stop_on_first:
                        logger.debug(
                            f"[{self._name}] Stop-on-first: stopping after "
                            f"{condenser.name}"
                        )
                        break
                else:
                    condenser_results.append({
                        "condenser": condenser.name,
                        "action": "none",
                        "duration_s": round(time.time() - condenser_start, 4),
                    })

            except Exception as e:
                condenser_results.append({
                    "condenser": condenser.name,
                    "action": "error",
                    "error": str(e),
                    "duration_s": round(time.time() - condenser_start, 4),
                })
                logger.error(
                    f"[{self._name}] Condenser {condenser.name} failed: {e}",
                    exc_info=True,
                )
                # Continue with remaining condensers — crash-proof

        # Build merged action
        if not all_forgotten_ids:
            duration = time.time() - start
            self._metrics.record_call(duration_s=duration)
            return None

        duration = time.time() - start
        tokens_saved = view.token_estimate() - current_view.token_estimate()

        self._metrics.record_call(
            duration_s=duration,
            events_removed=len(all_forgotten_ids),
            tokens_saved=max(0, tokens_saved),
            was_condensation=True,
        )

        logger.info(
            f"[{self._name}] Pipeline complete: "
            f"{actions_taken}/{len(self._condensers)} condensers acted, "
            f"removed={len(all_forgotten_ids)} events, "
            f"tokens_saved={tokens_saved}, "
            f"duration={duration:.3f}s"
        )

        return CondensationAction(
            forgotten_event_ids=all_forgotten_ids,
            summary=latest_summary,
            reason=latest_reason,
            metrics={
                "duration_s": round(duration, 4),
                "total_events_before": len(view),
                "total_events_after": len(current_view),
                "removed_events": len(all_forgotten_ids),
                "tokens_saved": max(0, tokens_saved),
                "actions_taken": actions_taken,
                "condensers_run": len(self._condensers),
                "condenser_results": condenser_results,
            },
        )

    # ── Apply override ───────────────────────────────────────────────────

    def apply(self, view: View, action: CondensationAction) -> View:
        """
        Apply the merged action to the view.

        Since the pipeline already applies actions incrementally during
        condensation, this method applies the final merged action to
        the original view.
        """
        if not action.forgotten_event_ids:
            return view

        new_view = view.remove_event_ids(action.forgotten_event_ids)

        # Insert summary if provided
        if action.summary:
            from app.context.view_properties import Event
            summary_event = Event.user(
                content=action.summary,
                metadata={
                    "condensation": True,
                    "reason": action.reason.value,
                    "pipeline": True,
                },
            )
            new_view.add(summary_event)

        new_view.enforce_properties()
        return new_view

    # ── Reset ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset all condensers in the pipeline."""
        super().reset()
        for condenser in self._condensers:
            condenser.reset()

    # ── Metrics aggregation ──────────────────────────────────────────────

    def aggregate_metrics(self) -> dict[str, dict]:
        """Return metrics from all condensers in the pipeline."""
        return {
            condenser.name: condenser.metrics.model_dump()
            for condenser in self._condensers
        }

    # ── Repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        condenser_names = [c.name for c in self._condensers]
        return (
            f"PipelineCondenser(name={self._name}, "
            f"condensers={condenser_names}, "
            f"stop_on_first={self._stop_on_first})"
        )
