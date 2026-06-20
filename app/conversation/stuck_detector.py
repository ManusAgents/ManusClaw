"""
ManusClaw Conversation System — StuckDetector
================================================

Detects when a conversation's agent loop is "stuck" — i.e. making no
meaningful progress despite continued execution.

Five detection scenarios are implemented:

1. **Repeating action-observation cycles** — the agent takes the same
   action and gets the same observation multiple times.

2. **Repeating action-error cycles** — the agent keeps retrying the same
   action that consistently fails with an error.

3. **Agent monologue** — the agent sends many consecutive messages
   without any user input, suggesting it may be talking to itself.

4. **Alternating action-observation patterns** — a specific sub-case
   where the agent alternates between exactly two actions/observations
   in a tight loop.

5. **Context window error loops** — the agent repeatedly triggers
   context-window overflow errors (e.g. TokenLimitExceeded) without
   recovering.

Each detector returns a :class:`StuckReport` indicating whether the
conversation appears stuck, which pattern was detected, and a
human-readable suggestion for recovery.

Usage::

    detector = StuckDetector(window_size=10, repeat_threshold=3)
    report = detector.analyze(events)
    if report.is_stuck:
        logger.warning(f"Agent is stuck: {report.pattern} — {report.suggestion}")
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Stuck Pattern Enum
# ──────────────────────────────────────────────────────────────────────────────

class StuckPattern(str, Enum):
    """Identifies which stuck pattern was detected."""

    REPEATING_ACTION_OBSERVATION = "repeating_action_observation"
    REPEATING_ACTION_ERROR = "repeating_action_error"
    AGENT_MONOLOGUE = "agent_monologue"
    ALTERNATING_ACTION_OBSERVATION = "alternating_action_observation"
    CONTEXT_WINDOW_ERROR_LOOP = "context_window_error_loop"


# ──────────────────────────────────────────────────────────────────────────────
# Stuck Report
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StuckReport:
    """
    The result of analyzing a conversation for stuck patterns.

    Attributes:
        is_stuck:     Whether any stuck pattern was detected.
        pattern:      The specific :class:`StuckPattern` detected
                      (``None`` if not stuck).
        confidence:   0.0–1.0 confidence in the detection.
        detail:       Human-readable description of what was detected.
        suggestion:   Suggested recovery action.
        metadata:     Arbitrary machine-readable data for programmatic handling.
    """

    is_stuck: bool
    pattern: Optional[StuckPattern] = None
    confidence: float = 0.0
    detail: str = ""
    suggestion: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Event fingerprinting helpers
# ──────────────────────────────────────────────────────────────────────────────

def _action_fingerprint(event: Any) -> str:
    """
    Produce a stable fingerprint for an action event.

    The fingerprint is based on the tool name and arguments so that
    semantically identical actions produce the same fingerprint.
    """
    tool_name = getattr(event, "tool_call", None)
    if tool_name is not None:
        func = getattr(tool_name, "function", None)
        if func is not None:
            name = getattr(func, "name", "")
            args = getattr(func, "arguments", "")
            raw = f"{name}:{args}"
        else:
            raw = str(tool_name)
    else:
        raw = str(event)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _observation_fingerprint(event: Any) -> str:
    """
    Produce a stable fingerprint for an observation event.

    Based on the tool name and content hash.
    """
    tool_name = getattr(event, "tool_name", "")
    content = getattr(event, "content", "")
    # Use first 200 chars of content to avoid hashing huge strings
    raw = f"{tool_name}:{content[:200]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _error_fingerprint(event: Any) -> str:
    """
    Produce a stable fingerprint for an error event.

    Based on error type and message.
    """
    error_type = getattr(event, "error_type", type(event).__name__)
    error_msg = getattr(event, "error", "")
    raw = f"{error_type}:{error_msg[:200]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _is_action(event: Any) -> bool:
    """Return True if the event is an action event."""
    return getattr(event, "kind", None) == "action"


def _is_observation(event: Any) -> bool:
    """Return True if the event is an observation event."""
    return getattr(event, "kind", None) == "observation"


def _is_error(event: Any) -> bool:
    """Return True if the event is an error event."""
    kind = getattr(event, "kind", None)
    return kind in ("agent_error", "conversation_error")


def _is_user_message(event: Any) -> bool:
    """Return True if the event is a user-sent message."""
    kind = getattr(event, "kind", None)
    if kind == "message":
        source = getattr(event, "source", "")
        role = getattr(event, "role", "")
        return source == "user" or role == "user"
    return False


def _is_context_window_error(event: Any) -> bool:
    """
    Return True if the event represents a context window overflow.

    Checks for token limit errors or condensation request events.
    """
    kind = getattr(event, "kind", None)
    if kind == "condensation_request":
        return True
    if kind in ("agent_error", "conversation_error"):
        error = getattr(event, "error", "")
        error_type = getattr(event, "error_type", "")
        context_keywords = [
            "token", "context", "window", "maximum",
            "too many", "exceed", "length", "context_length",
        ]
        combined = f"{error_type} {error}".lower()
        return any(kw in combined for kw in context_keywords)
    return False


def _is_failed_observation(event: Any) -> bool:
    """Return True if the event is an observation that indicates failure."""
    if not _is_observation(event):
        return False
    success = getattr(event, "success", True)
    return not success


# ──────────────────────────────────────────────────────────────────────────────
# StuckDetector
# ──────────────────────────────────────────────────────────────────────────────

class StuckDetector:
    """
    Analyzes a sequence of conversation events to detect stuck patterns.

    The detector looks at the most recent *window_size* events and checks
    five independent patterns.  The first pattern that triggers (in
    priority order) is returned.

    Args:
        window_size:        Number of recent events to analyze.
        repeat_threshold:   How many repetitions constitute "stuck"
                            for patterns 1, 2, and 4.
        monologue_threshold: How many consecutive non-user messages
                             constitute a "monologue" (pattern 3).
        context_error_threshold: How many context-window errors in the
                                 window constitute a loop (pattern 5).
    """

    def __init__(
        self,
        window_size: int = 20,
        repeat_threshold: int = 3,
        monologue_threshold: int = 8,
        context_error_threshold: int = 2,
    ) -> None:
        self._window_size = window_size
        self._repeat_threshold = repeat_threshold
        self._monologue_threshold = monologue_threshold
        self._context_error_threshold = context_error_threshold

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze(self, events: Sequence[Any]) -> StuckReport:
        """
        Analyze *events* for stuck patterns.

        Checks patterns in priority order and returns the first match.
        If no pattern is detected, returns a non-stuck report.

        Args:
            events: The full event log (only the last *window_size*
                    events are analyzed).

        Returns:
            A :class:`StuckReport` with the detection result.
        """
        if not events:
            return StuckReport(is_stuck=False)

        window = events[-self._window_size:] if len(events) > self._window_size else list(events)

        # Pattern 1: Repeating action-observation cycles
        report = self._check_repeating_action_observation(window)
        if report.is_stuck:
            return report

        # Pattern 2: Repeating action-error cycles
        report = self._check_repeating_action_error(window)
        if report.is_stuck:
            return report

        # Pattern 3: Agent monologue
        report = self._check_agent_monologue(window)
        if report.is_stuck:
            return report

        # Pattern 4: Alternating action-observation patterns
        report = self._check_alternating_action_observation(window)
        if report.is_stuck:
            return report

        # Pattern 5: Context window error loops
        report = self._check_context_window_error_loop(window)
        if report.is_stuck:
            return report

        return StuckReport(is_stuck=False)

    # ── Pattern 1: Repeating action-observation cycles ────────────────────────

    def _check_repeating_action_observation(self, events: List[Any]) -> StuckReport:
        """
        Detect when the agent takes the same action and gets the same
        observation multiple times in a row.

        Example:
            Action(bash, "ls") → Observation("file1.txt") →
            Action(bash, "ls") → Observation("file1.txt") →
            Action(bash, "ls") → Observation("file1.txt")
        """
        # Build action-observation pairs
        pairs: List[str] = []
        i = 0
        while i < len(events):
            event = events[i]
            if _is_action(event):
                action_fp = _action_fingerprint(event)
                obs_fp = ""
                # Look for the next observation
                for j in range(i + 1, len(events)):
                    if _is_observation(events[j]):
                        obs_fp = _observation_fingerprint(events[j])
                        i = j + 1
                        break
                    if _is_action(events[j]):
                        # Next action without observation — broken pair
                        break
                else:
                    i += 1
                pairs.append(f"{action_fp}:{obs_fp}")
            else:
                i += 1

        if len(pairs) < self._repeat_threshold:
            return StuckReport(is_stuck=False)

        # Check if the last N pairs are identical
        last_n = pairs[-self._repeat_threshold:]
        if len(set(last_n)) == 1 and last_n[0] != ":":
            return StuckReport(
                is_stuck=True,
                pattern=StuckPattern.REPEATING_ACTION_OBSERVATION,
                confidence=0.85,
                detail=(
                    f"The same action-observation pair repeated "
                    f"{self._repeat_threshold}+ times: {last_n[0]}"
                ),
                suggestion=(
                    "The agent is stuck in a loop repeating the same action "
                    "with the same result. Try injecting a user message to "
                    "redirect, or pause and provide new instructions."
                ),
                metadata={"repeat_count": self._repeat_threshold, "pair_fingerprint": last_n[0]},
            )

        return StuckReport(is_stuck=False)

    # ── Pattern 2: Repeating action-error cycles ──────────────────────────────

    def _check_repeating_action_error(self, events: List[Any]) -> StuckReport:
        """
        Detect when the agent keeps retrying the same action that
        consistently fails.

        Example:
            Action(bash, "make") → Observation(error) →
            Action(bash, "make") → Observation(error) →
            Action(bash, "make") → Observation(error)
        """
        # Build action-failed_observation pairs
        pairs: List[str] = []
        i = 0
        while i < len(events):
            event = events[i]
            if _is_action(event):
                action_fp = _action_fingerprint(event)
                obs_fp = ""
                # Look for the next observation
                for j in range(i + 1, len(events)):
                    if _is_observation(events[j]):
                        if _is_failed_observation(events[j]):
                            obs_fp = _observation_fingerprint(events[j])
                            i = j + 1
                            break
                        else:
                            # Successful observation — not an error cycle
                            i = j + 1
                            break
                    if _is_action(events[j]):
                        break
                else:
                    i += 1
                if obs_fp:
                    pairs.append(f"{action_fp}:ERR:{obs_fp}")
            else:
                i += 1

        if len(pairs) < self._repeat_threshold:
            return StuckReport(is_stuck=False)

        last_n = pairs[-self._repeat_threshold:]
        if len(set(last_n)) == 1:
            return StuckReport(
                is_stuck=True,
                pattern=StuckPattern.REPEATING_ACTION_ERROR,
                confidence=0.90,
                detail=(
                    f"The same action-error pair repeated "
                    f"{self._repeat_threshold}+ times: {last_n[0]}"
                ),
                suggestion=(
                    "The agent is repeatedly failing on the same action. "
                    "Consider injecting an error analysis message or "
                    "suggesting an alternative approach."
                ),
                metadata={"repeat_count": self._repeat_threshold, "pair_fingerprint": last_n[0]},
            )

        return StuckReport(is_stuck=False)

    # ── Pattern 3: Agent monologue ────────────────────────────────────────────

    def _check_agent_monologue(self, events: List[Any]) -> StuckReport:
        """
        Detect when the agent sends many consecutive messages without
        any user input, suggesting it may be talking to itself.

        This is not always "stuck" but is a signal that the conversation
        may benefit from user intervention.
        """
        consecutive_non_user = 0
        max_consecutive = 0

        for event in events:
            if _is_user_message(event):
                consecutive_non_user = 0
            else:
                # Only count LLM-convertible events (actions, messages,
                # observations) — not control events like PauseEvent
                kind = getattr(event, "kind", "")
                if kind in ("action", "message", "observation"):
                    consecutive_non_user += 1
                    max_consecutive = max(max_consecutive, consecutive_non_user)

        if max_consecutive >= self._monologue_threshold:
            return StuckReport(
                is_stuck=True,
                pattern=StuckPattern.AGENT_MONOLOGUE,
                confidence=0.60,  # Lower confidence — monologue isn't always stuck
                detail=(
                    f"Agent sent {max_consecutive} consecutive messages "
                    f"without user input (threshold={self._monologue_threshold})"
                ),
                suggestion=(
                    "The agent has been operating autonomously for an "
                    "extended period without user feedback. Consider "
                    "pausing to review progress or providing new direction."
                ),
                metadata={"max_consecutive": max_consecutive},
            )

        return StuckReport(is_stuck=False)

    # ── Pattern 4: Alternating action-observation patterns ────────────────────

    def _check_alternating_action_observation(self, events: List[Any]) -> StuckReport:
        """
        Detect when the agent alternates between exactly two
        action-observation pairs in a tight loop.

        Example:
            Action(A) → Obs(X) → Action(B) → Obs(Y) →
            Action(A) → Obs(X) → Action(B) → Obs(Y)
        """
        # Build sequence of action fingerprints
        action_fps: List[str] = []
        for event in events:
            if _is_action(event):
                action_fps.append(_action_fingerprint(event))

        if len(action_fps) < self._repeat_threshold * 2:
            return StuckReport(is_stuck=False)

        # Check for a 2-element cycle in the last N actions
        last = action_fps[-(self._repeat_threshold * 2):]
        # A 2-cycle looks like: A, B, A, B, A, B
        if len(last) >= 4:
            a, b = last[0], last[1]
            if a != b:
                is_two_cycle = True
                for idx, fp in enumerate(last):
                    expected = a if idx % 2 == 0 else b
                    if fp != expected:
                        is_two_cycle = False
                        break
                if is_two_cycle:
                    return StuckReport(
                        is_stuck=True,
                        pattern=StuckPattern.ALTERNATING_ACTION_OBSERVATION,
                        confidence=0.80,
                        detail=(
                            f"Agent is alternating between two actions "
                            f"in a loop: {a} ↔ {b} "
                            f"({len(last)} consecutive alternations)"
                        ),
                        suggestion=(
                            "The agent is stuck alternating between two "
                            "actions. Break the cycle by injecting a new "
                            "instruction or suggesting a third approach."
                        ),
                        metadata={"action_a": a, "action_b": b, "cycle_length": len(last)},
                    )

        return StuckReport(is_stuck=False)

    # ── Pattern 5: Context window error loops ─────────────────────────────────

    def _check_context_window_error_loop(self, events: List[Any]) -> StuckReport:
        """
        Detect when the agent repeatedly triggers context-window overflow
        errors without recovering.

        This can happen when the condenser fails or the agent keeps
        appending to the context without trimming.
        """
        context_errors = 0
        for event in events:
            if _is_context_window_error(event):
                context_errors += 1

        if context_errors >= self._context_error_threshold:
            return StuckReport(
                is_stuck=True,
                pattern=StuckPattern.CONTEXT_WINDOW_ERROR_LOOP,
                confidence=0.95,
                detail=(
                    f"{context_errors} context-window errors detected "
                    f"in the last {self._window_size} events "
                    f"(threshold={self._context_error_threshold})"
                ),
                suggestion=(
                    "The agent is stuck in a context window overflow loop. "
                    "Force condensation, truncate the event history, or "
                    "restart the conversation with a shorter context."
                ),
                metadata={"error_count": context_errors},
            )

        return StuckReport(is_stuck=False)

    # ── Configuration ─────────────────────────────────────────────────────────

    @property
    def window_size(self) -> int:
        return self._window_size

    @window_size.setter
    def window_size(self, value: int) -> None:
        self._window_size = max(4, value)

    @property
    def repeat_threshold(self) -> int:
        return self._repeat_threshold

    @repeat_threshold.setter
    def repeat_threshold(self, value: int) -> None:
        self._repeat_threshold = max(2, value)

    @property
    def monologue_threshold(self) -> int:
        return self._monologue_threshold

    @monologue_threshold.setter
    def monologue_threshold(self, value: int) -> None:
        self._monologue_threshold = max(3, value)

    def __repr__(self) -> str:
        return (
            f"<StuckDetector window={self._window_size} "
            f"repeat={self._repeat_threshold} "
            f"monologue={self._monologue_threshold}>"
        )
