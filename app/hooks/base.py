from __future__ import annotations

"""
ManusClaw Hooks — Abstract Base Class
=======================================
All hooks must inherit from ``HookBase`` and implement ``on_event``.

Lifecycle contract:
    - ``on_event`` is called for every subscribed event type.
    - The hook MUST return a ``HookResult`` — never raise.
    - If the hook raises, the manager catches the error and returns ALLOW
      (fail-open) so the agent is never blocked by a buggy hook.
    - ``setup`` / ``teardown`` are optional lifecycle methods called by
      the ``HookManager`` when hooks are registered / unregistered.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.hooks.types import HookContext, HookDecision, HookEventType, HookResult


class HookBase(ABC):
    """
    Abstract base class for all manusclaw hooks.

    Subclasses must:
        1. Set ``name`` to a unique identifier.
        2. Set ``subscribed_events`` to the set of events this hook cares about.
        3. Implement ``on_event(context) -> HookResult``.

    Optionally override:
        - ``setup()`` — called once when the hook is registered.
        - ``teardown()`` — called once when the hook is unregistered.
        - ``priority`` — lower numbers run first (default 100).
    """

    # ── Hook identity ────────────────────────────────────────────────────

    name: str = "unnamed_hook"
    description: str = ""
    priority: int = 100  # Lower runs first within the same event type
    enabled: bool = True

    # ── Event subscription ───────────────────────────────────────────────

    subscribed_events: set[HookEventType] = set()

    # ── Configuration ────────────────────────────────────────────────────

    timeout_s: float = 10.0  # Max seconds a hook may run before being cancelled
    fail_open: bool = True   # If True, ALLOW on error; if False, DENY on error

    # ── Abstract interface ───────────────────────────────────────────────

    @abstractmethod
    async def on_event(self, context: HookContext) -> HookResult:
        """
        Process a hook event and return a decision.

        Args:
            context: Full context about the event being processed.

        Returns:
            HookResult with a decision (ALLOW / DENY / MODIFY).

        Implementation notes:
            - This method MUST NOT raise exceptions. Return a HookResult
              instead. The manager wraps calls in error isolation, but
              hooks should handle their own errors gracefully.
            - Long-running operations should be avoided. Respect timeout_s.
            - For PRE_TOOL_USE: return DENY to block tool execution.
            - For POST_TOOL_USE: always return ALLOW (observation only).
            - For USER_PROMPT_SUBMIT: return MODIFY to rewrite the prompt,
              DENY to block it, or ALLOW to pass it through.
            - For STOP: return DENY to prevent the agent from stopping.
        """

    # ── Optional lifecycle ───────────────────────────────────────────────

    async def setup(self) -> None:
        """
        Called once when the hook is registered with the HookManager.
        Use for one-time initialization (opening files, connecting to DB, etc.).
        """

    async def teardown(self) -> None:
        """
        Called once when the hook is unregistered from the HookManager.
        Use for cleanup (closing files, flushing buffers, etc.).
        """

    # ── Helpers ──────────────────────────────────────────────────────────

    def is_subscribed(self, event_type: HookEventType) -> bool:
        """Check if this hook subscribes to a given event type."""
        return self.enabled and event_type in self.subscribed_events

    def configure(self, **kwargs: Any) -> None:
        """
        Update hook configuration from keyword arguments.
        Only updates attributes that already exist on the hook.
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def __repr__(self) -> str:
        events = ", ".join(e.value for e in self.subscribed_events)
        return (
            f"<{self.__class__.__name__} "
            f"name={self.name!r} "
            f"priority={self.priority} "
            f"events=[{events}] "
            f"enabled={self.enabled}>"
        )
