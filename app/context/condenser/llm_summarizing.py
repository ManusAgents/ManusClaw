from __future__ import annotations

"""
LLMSummarizingCondenser — Generate summaries via a dedicated condenser LLM.

This is the most sophisticated condenser.  Instead of simply dropping
events, it uses an LLM to generate a summary of the events being
removed, preserving critical information in a condensed form.

Flow:
  1. Identify events to remove (beyond the rolling window or by token budget).
  2. Call the condenser LLM with the events to be forgotten.
  3. Generate a summary that captures key information.
  4. Return a CondensationAction with:
     - forgotten_event_ids: IDs of removed events
     - summary: The LLM-generated summary
     - A Condensation event inserted into the view

Hard context reset:
  If the LLM call fails (rate limit, timeout, etc.), the condenser
  performs a progressive truncation with up to `max_retries` attempts,
  scaling the context by `retry_scale_factor` each time (0.8x default).

Thread Safety:
  All operations are thread-safe.  The condenser can be called from
  multiple threads concurrently (each call is independent).
"""

import time
import uuid
from typing import Any, Optional

from app.logger import logger
from app.context.view import View
from app.context.view_properties import Event
from app.context.condenser.base import (
    CondenserBase,
    CondensationAction,
    Condensation,
    CondensationReason,
    CondenserMetrics,
)

# ──────────────────────────────────────────────────────────────────────────────
# System prompt for the condenser LLM
# ──────────────────────────────────────────────────────────────────────────────

CONDENSER_SYSTEM_PROMPT = """\
You are a context condensation assistant for the ManusClaw AI operating environment.
Your task is to summarize a sequence of conversation events that are being removed
from the LLM's context window.

Guidelines:
1. Preserve all factual information: names, values, decisions, and outcomes.
2. Preserve the logical flow: what was attempted, what succeeded, what failed.
3. Preserve any error messages or warnings that may be relevant later.
4. Preserve tool call results that contain important data.
5. Be concise — the summary should be significantly shorter than the original events.
6. Use a structured format with clear sections.
7. Do NOT add information that wasn't in the original events.
8. If the events show a tool loop or repeated failures, note that explicitly.

Format your summary as:
## Condensed Context
- **Task**: <what was being done>
- **Actions taken**: <key actions and results>
- **Errors/Issues**: <any problems encountered>
- **Current state**: <where things stand>
- **Key data**: <important values, paths, outputs>
"""

CONDENSER_USER_TEMPLATE = """\
Please summarize the following conversation events that are being removed from context.
These events occurred between the system prompt and the current conversation point.

Events to summarize:
---
{events_text}
---

Provide a concise but complete summary that preserves all critical information."""


# ──────────────────────────────────────────────────────────────────────────────
# LLM Summarizing Condenser
# ──────────────────────────────────────────────────────────────────────────────

class LLMSummarizingCondenser(CondenserBase):
    """
    Condenser that uses an LLM to generate summaries of forgotten events.

    This condenser:
      1. Determines which events to remove (using a configurable strategy).
      2. Calls a condenser LLM to summarize the events being removed.
      3. Creates a Condensation event with the summary.
      4. On LLM failure, performs progressive truncation with retry.

    Args:
        max_events:        Target maximum number of events after condensation.
        max_tokens:        Target maximum token count after condensation.
                           If set, condensation is triggered by token count
                           instead of event count.
        max_retries:       Maximum number of retry attempts on LLM failure.
        retry_scale_factor: Factor to scale the context by on each retry (0.8 = 80%).
        llm_config:        Optional dict with LLM configuration for the condenser.
                           If None, falls back to the main LLM config.
        name:              Optional name for this condenser instance.
    """

    def __init__(
        self,
        max_events: int = 50,
        max_tokens: int = 0,
        max_retries: int = 5,
        retry_scale_factor: float = 0.8,
        llm_config: Optional[dict[str, Any]] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(name=name or "LLMSummarizingCondenser")
        if max_events < 1:
            raise ValueError(f"max_events must be >= 1, got {max_events}")
        if not 0 < retry_scale_factor <= 1.0:
            raise ValueError(
                f"retry_scale_factor must be in (0, 1], got {retry_scale_factor}"
            )
        self._max_events = max_events
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_scale_factor = retry_scale_factor
        self._llm_config = llm_config or {}
        self._condensation_count = 0

    # ── Public properties ────────────────────────────────────────────────

    @property
    def max_events(self) -> int:
        return self._max_events

    @max_events.setter
    def max_events(self, value: int) -> None:
        if value < 1:
            raise ValueError(f"max_events must be >= 1, got {value}")
        self._max_events = value

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @max_tokens.setter
    def max_tokens(self, value: int) -> None:
        self._max_tokens = max(0, value)

    # ── should_condense ──────────────────────────────────────────────────

    def should_condense(self, view: View) -> bool:
        """Return True if the view exceeds the configured limits."""
        if self._max_tokens > 0:
            return view.token_estimate() > self._max_tokens
        return len(view) > self._max_events + view.keep_first

    # ── condense ─────────────────────────────────────────────────────────

    def condense(
        self,
        view: View,
        reason: CondensationReason = CondensationReason.EVENTS,
    ) -> Optional[CondensationAction]:
        """
        Attempt LLM-based summarizing condensation.

        On failure, performs progressive truncation with retry.
        """
        start = time.time()
        try:
            # Step 1: Identify events to remove
            forgotten_ids = self._select_events_for_removal(view)
            if not forgotten_ids:
                logger.trace(f"[{self._name}] No events to remove")
                return None

            # Step 2: Collect the events being forgotten (for summarization)
            events = view.events
            forgotten_events = [
                e for e in events if e.id in forgotten_ids
            ]

            # Step 3: Generate summary via LLM
            summary = self._generate_summary(forgotten_events, view)

            # Step 4: Build condensation action
            action = CondensationAction(
                forgotten_event_ids=forgotten_ids,
                summary=summary,
                reason=reason,
                metrics={
                    "duration_s": round(time.time() - start, 4),
                    "total_events": len(events),
                    "removed_events": len(forgotten_ids),
                    "max_events": self._max_events,
                    "max_tokens": self._max_tokens,
                    "summary_length": len(summary) if summary else 0,
                    "condensation_number": self._condensation_count,
                },
            )

            self._condensation_count += 1

            logger.info(
                f"[{self._name}] Condensation #{self._condensation_count}: "
                f"removing {len(forgotten_ids)} events, "
                f"summary={len(summary) if summary else 0} chars"
            )

            return action

        except Exception as e:
            duration = time.time() - start
            self._metrics.record_call(duration_s=duration, error=True)
            logger.error(
                f"[{self._name}] Error during condensation: {e}",
                exc_info=True,
            )
            # Fallback: return a hard-reset action without summary
            return self._hard_reset_fallback(view, reason)

    # ── Event selection ──────────────────────────────────────────────────

    def _select_events_for_removal(self, view: View) -> set[str]:
        """
        Select which events to remove from the view.

        Strategy:
          1. Get the manipulation_indices (safe to remove).
          2. Keep the keep_first zone and the most recent max_events.
          3. Remove everything else that's in manipulation_indices.
        """
        events = view.events
        total = len(events)
        keep_first = view.keep_first

        # Determine how many events to keep
        if self._max_tokens > 0:
            # Token-based: remove events until we're under budget
            return self._select_by_tokens(view)
        else:
            # Event-based: keep last max_events
            cutoff = total - self._max_events
            if cutoff <= keep_first:
                return set()

            candidate_indices = set(range(keep_first, cutoff))
            manipulable = view.manipulation_indices
            removable_indices = candidate_indices & manipulable

            forgotten_ids: set[str] = set()
            for idx in removable_indices:
                if idx < len(events):
                    forgotten_ids.add(events[idx].id)

            return forgotten_ids

    def _select_by_tokens(self, view: View) -> set[str]:
        """
        Select events to remove based on token budget.

        Remove oldest removable events until the view is under the
        token limit.
        """
        events = view.events
        current_tokens = view.token_estimate()
        target_tokens = self._max_tokens

        if current_tokens <= target_tokens:
            return set()

        manipulable = view.manipulation_indices
        keep_first = view.keep_first

        forgotten_ids: set[str] = set()
        tokens_saved = 0

        # Remove from oldest to newest (within manipulable range)
        for idx in sorted(manipulable):
            if idx < keep_first:
                continue
            if idx >= len(events):
                continue

            event = events[idx]
            event_tokens = max(1, len(event.content or "") // 4)
            forgotten_ids.add(event.id)
            tokens_saved += event_tokens

            if current_tokens - tokens_saved <= target_tokens:
                break

        return forgotten_ids

    # ── Summary generation ───────────────────────────────────────────────

    def _generate_summary(
        self,
        forgotten_events: list[Event],
        view: View,
    ) -> Optional[str]:
        """
        Generate a summary of the forgotten events using the condenser LLM.

        Implements progressive truncation on failure:
          - Try with full context first.
          - On failure, scale down by retry_scale_factor and retry.
          - After max_retries, return None (hard reset).
        """
        if not forgotten_events:
            return None

        events_text = self._format_events_for_summary(forgotten_events)

        for attempt in range(self._max_retries + 1):
            try:
                summary = self._call_condenser_llm(events_text)

                if summary and len(summary.strip()) > 0:
                    # Create a Condensation record
                    condensation = Condensation(
                        forgotten_event_ids={e.id for e in forgotten_events},
                        summary=summary,
                        reason=CondensationReason.EVENTS,
                    )
                    # Prepend the condensation marker
                    return condensation.to_event_content() + "\n\n" + summary

            except Exception as e:
                logger.warning(
                    f"[{self._name}] Summary generation attempt "
                    f"{attempt + 1}/{self._max_retries + 1} failed: {e}"
                )

                if attempt < self._max_retries:
                    # Progressive truncation: scale down the events text
                    scale = self._retry_scale_factor ** (attempt + 1)
                    truncate_at = int(len(events_text) * scale)
                    events_text = events_text[:truncate_at]
                    logger.info(
                        f"[{self._name}] Retrying with truncated context: "
                        f"{truncate_at} chars (scale={scale:.2f})"
                    )
                else:
                    logger.error(
                        f"[{self._name}] All {self._max_retries + 1} attempts "
                        f"failed. Falling back to hard reset."
                    )

        return None

    def _format_events_for_summary(self, events: list[Event]) -> str:
        """Format events into text suitable for the condenser LLM."""
        parts: list[str] = []
        for i, event in enumerate(events):
            role = event.role.upper()
            content = event.content or ""
            if len(content) > 500:
                content = content[:500] + "...[truncated]"

            if event.is_assistant and event.has_tool_calls:
                tool_names = [
                    tc.get("function", {}).get("name", "unknown")
                    for tc in event.tool_calls
                ]
                parts.append(
                    f"[{i}] {role}: (tool_calls: {tool_names}) {content}"
                )
            elif event.is_tool:
                parts.append(
                    f"[{i}] TOOL ({event.name}): {content}"
                )
            else:
                parts.append(f"[{i}] {role}: {content}")

        return "\n".join(parts)

    # ── LLM call ─────────────────────────────────────────────────────────

    def _call_condenser_llm(self, events_text: str) -> str:
        """
        Call the condenser LLM to generate a summary.

        Tries to use the app's LLM module. Falls back to a simple
        extractive summary if the LLM is unavailable.
        """
        try:
            from app.llm.llm import LLM
            from app.config import Config

            cfg = Config.get()

            # Build condenser-specific LLM config
            condenser_model = self._llm_config.get("model", cfg.llm.model)
            condenser_provider = self._llm_config.get("provider", cfg.llm.provider)

            llm = LLM(
                provider=condenser_provider,
                model=condenser_model,
                max_tokens=self._llm_config.get("max_tokens", 1024),
                temperature=self._llm_config.get("temperature", 0.0),
            )

            messages = [
                {"role": "system", "content": CONDENSER_SYSTEM_PROMPT},
                {"role": "user", "content": CONDENSER_USER_TEMPLATE.format(
                    events_text=events_text
                )},
            ]

            # Use async LLM in sync context
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # We're in an async context — create a task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        llm.chat(messages)
                    )
                    response = future.result(timeout=120)
            else:
                response = asyncio.run(llm.chat(messages))

            if isinstance(response, str):
                return response
            elif isinstance(response, dict):
                return response.get("content", "")
            elif hasattr(response, "content"):
                return response.content or ""
            return str(response)

        except ImportError:
            logger.debug(
                f"[{self._name}] LLM module not available, "
                f"using extractive fallback"
            )
            return self._extractive_summary(events_text)
        except Exception as e:
            logger.warning(
                f"[{self._name}] LLM call failed: {e}, "
                f"using extractive fallback"
            )
            return self._extractive_summary(events_text)

    def _extractive_summary(self, events_text: str) -> str:
        """
        Simple extractive summary fallback when LLM is unavailable.

        Takes the first and last few lines as a crude summary.
        """
        lines = events_text.split("\n")
        if len(lines) <= 10:
            return events_text

        head = lines[:5]
        tail = lines[-5:]
        return (
            "## Extractive Summary (LLM unavailable)\n"
            + "\n".join(head)
            + f"\n... [{len(lines) - 10} lines omitted] ...\n"
            + "\n".join(tail)
        )

    # ── Hard reset fallback ──────────────────────────────────────────────

    def _hard_reset_fallback(
        self,
        view: View,
        reason: CondensationReason,
    ) -> Optional[CondensationAction]:
        """
        When LLM summarization fails completely, perform a hard context
        reset: remove all removable events without a summary.

        This is a last resort to prevent context overflow.
        """
        events = view.events
        manipulable = view.manipulation_indices
        keep_first = view.keep_first

        # Remove all manipulable events beyond keep_first + a small window
        keep_tail = min(self._max_events, len(events) - keep_first)
        cutoff = len(events) - keep_tail

        forgotten_ids: set[str] = set()
        for idx in sorted(manipulable):
            if idx < keep_first:
                continue
            if idx >= cutoff:
                continue
            if idx < len(events):
                forgotten_ids.add(events[idx].id)

        if not forgotten_ids:
            return None

        logger.warning(
            f"[{self._name}] Hard reset: removing {len(forgotten_ids)} events "
            f"without summary (LLM unavailable)"
        )

        return CondensationAction(
            forgotten_event_ids=forgotten_ids,
            summary=None,
            reason=reason,
            metrics={
                "hard_reset": True,
                "total_events": len(events),
                "removed_events": len(forgotten_ids),
            },
        )

    # ── Reset ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset internal state for a new session."""
        super().reset()
        self._condensation_count = 0
