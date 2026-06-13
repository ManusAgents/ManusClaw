from __future__ import annotations

"""
ManusClaw Hooks — Hook Manager
================================
Central orchestrator for hook registration, execution, and lifecycle.

Features:
    - Thread-safe hook registration and removal
    - Async hook execution with per-hook timeout protection
    - Error isolation: hook failures never crash the agent
    - Priority-based execution ordering
    - Aggregate result computation across multiple hooks
    - Built-in metrics for observability
"""

import asyncio
import threading
import time
from collections import defaultdict
from typing import Any, Optional

from app.hooks.base import HookBase
from app.hooks.types import (
    HookContext,
    HookDecision,
    HookError,
    HookEventType,
    HookResult,
    HookTimeoutError,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Aggregate result — combines decisions from multiple hooks
# ──────────────────────────────────────────────────────────────────────────────

class AggregateHookResult:
    """
    Merges HookResult objects from multiple hooks into a single decision.

    Rules:
        1. If ANY hook returns DENY, the aggregate is DENY (first DENY wins).
        2. If no DENY but at least one MODIFY, the aggregate is MODIFY
           (last MODIFY's content wins).
        3. Otherwise, ALLOW.
    """

    def __init__(self) -> None:
        self.decision: HookDecision = HookDecision.ALLOW
        self.modified_content: Optional[str] = None
        self.denial_reason: str = ""
        self.modification_reason: str = ""
        self.results: list[HookResult] = []
        self.errors: list[HookError] = []

    def add(self, result: HookResult, hook_name: str = "") -> None:
        """Merge a single hook result into the aggregate."""
        self.results.append(result)

        if result.decision == HookDecision.DENY:
            if self.decision != HookDecision.DENY:
                # First DENY takes precedence
                self.decision = HookDecision.DENY
                self.denial_reason = result.reason or f"Denied by hook '{hook_name}'"

        elif result.decision == HookDecision.MODIFY:
            if self.decision == HookDecision.ALLOW:
                self.decision = HookDecision.MODIFY
            # Last MODIFY's content wins
            self.modified_content = result.modified_content
            self.modification_reason = result.reason or f"Modified by hook '{hook_name}'"

    def add_error(self, error: HookError) -> None:
        """Record a hook execution error."""
        self.errors.append(error)

    @property
    def is_allowed(self) -> bool:
        return self.decision == HookDecision.ALLOW

    @property
    def is_denied(self) -> bool:
        return self.decision == HookDecision.DENY

    @property
    def is_modified(self) -> bool:
        return self.decision == HookDecision.MODIFY

    def to_hook_result(self) -> HookResult:
        """Convert aggregate to a single HookResult."""
        if self.decision == HookDecision.DENY:
            return HookResult.deny(self.denial_reason)
        if self.decision == HookDecision.MODIFY:
            return HookResult.modify(
                self.modified_content or "",
                reason=self.modification_reason,
            )
        return HookResult.allow()


# ──────────────────────────────────────────────────────────────────────────────
# Hook execution metrics
# ──────────────────────────────────────────────────────────────────────────────

class HookMetrics:
    """Lightweight execution metrics per hook."""

    def __init__(self) -> None:
        self.invocations: int = 0
        self.errors: int = 0
        self.timeouts: int = 0
        self.total_duration_s: float = 0.0
        self.denies: int = 0
        self.modifications: int = 0
        self._lock: threading.Lock = threading.Lock()

    def record(self, duration_s: float, decision: HookDecision,
               is_error: bool = False, is_timeout: bool = False) -> None:
        with self._lock:
            self.invocations += 1
            self.total_duration_s += duration_s
            if is_error:
                self.errors += 1
            if is_timeout:
                self.timeouts += 1
            if decision == HookDecision.DENY:
                self.denies += 1
            elif decision == HookDecision.MODIFY:
                self.modifications += 1

    @property
    def avg_duration_ms(self) -> float:
        if self.invocations == 0:
            return 0.0
        return (self.total_duration_s / self.invocations) * 1000

    def snapshot(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "denies": self.denies,
            "modifications": self.modifications,
            "total_duration_s": round(self.total_duration_s, 3),
            "avg_duration_ms": round(self.avg_duration_ms, 2),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Hook Manager
# ──────────────────────────────────────────────────────────────────────────────

class HookManager:
    """
    Thread-safe hook registry and async executor.

    Usage:
        manager = HookManager()
        manager.register(my_hook)

        # In the agent loop:
        result = await manager.execute_hooks(HookEventType.PRE_TOOL_USE, context)
        if result.is_denied:
            # Skip tool execution
            ...

    Thread Safety:
        - Registration / removal are guarded by a threading.Lock.
        - Execution is fully async and does not hold the lock while running hooks.
    """

    def __init__(self, default_timeout_s: float = 10.0) -> None:
        self._hooks: dict[str, HookBase] = {}
        # event_type -> sorted list of hook names
        self._event_index: dict[HookEventType, list[str]] = defaultdict(list)
        self._lock: threading.Lock = threading.Lock()
        self._metrics: dict[str, HookMetrics] = defaultdict(HookMetrics)
        self._default_timeout_s: float = default_timeout_s
        self._initialized: bool = False

    # ──────────────────────────────────────────────────────────────────────
    # Registration
    # ──────────────────────────────────────────────────────────────────────

    def register(self, hook: HookBase) -> None:
        """
        Register a hook. If a hook with the same name exists, it is replaced.
        Rebuilds the per-event index for O(1) lookup during execution.
        """
        with self._lock:
            self._hooks[hook.name] = hook
            self._rebuild_index()
        logger.debug(f"[HookManager] Registered hook: {hook}")

    def register_many(self, *hooks: HookBase) -> None:
        """Register multiple hooks at once."""
        with self._lock:
            for hook in hooks:
                self._hooks[hook.name] = hook
            self._rebuild_index()
        logger.debug(f"[HookManager] Registered {len(hooks)} hooks")

    def unregister(self, hook_name: str) -> Optional[HookBase]:
        """Remove a hook by name. Returns the removed hook or None."""
        with self._lock:
            hook = self._hooks.pop(hook_name, None)
            if hook is not None:
                self._rebuild_index()
                logger.debug(f"[HookManager] Unregistered hook: {hook_name}")
            return hook

    def get(self, hook_name: str) -> Optional[HookBase]:
        """Look up a hook by name (thread-safe read)."""
        with self._lock:
            return self._hooks.get(hook_name)

    def list_hooks(self) -> list[HookBase]:
        """Return all registered hooks (snapshot copy)."""
        with self._lock:
            return list(self._hooks.values())

    def _rebuild_index(self) -> None:
        """
        Rebuild the event→hooks index. Must be called under self._lock.
        Hooks are sorted by priority (ascending) so lower-priority hooks run first.
        """
        self._event_index.clear()
        for event_type in HookEventType:
            subscribed = [
                h for h in self._hooks.values()
                if h.is_subscribed(event_type)
            ]
            subscribed.sort(key=lambda h: h.priority)
            self._event_index[event_type] = [h.name for h in subscribed]

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Call setup() on all registered hooks. Idempotent."""
        if self._initialized:
            return
        for hook in self.list_hooks():
            try:
                await hook.setup()
                logger.debug(f"[HookManager] Setup complete: {hook.name}")
            except Exception as e:
                logger.error(
                    f"[HookManager] Setup failed for '{hook.name}': {e}",
                    exc_info=True,
                )
        self._initialized = True

    async def shutdown(self) -> None:
        """Call teardown() on all registered hooks and clear the registry."""
        for hook in self.list_hooks():
            try:
                await hook.teardown()
                logger.debug(f"[HookManager] Teardown complete: {hook.name}")
            except Exception as e:
                logger.error(
                    f"[HookManager] Teardown failed for '{hook.name}': {e}",
                    exc_info=True,
                )
        with self._lock:
            self._hooks.clear()
            self._event_index.clear()
        self._initialized = False

    # ──────────────────────────────────────────────────────────────────────
    # Execution
    # ──────────────────────────────────────────────────────────────────────

    async def execute_hooks(
        self,
        event_type: HookEventType,
        context: HookContext,
    ) -> AggregateHookResult:
        """
        Execute all hooks subscribed to *event_type* in priority order.

        Error Isolation:
            Each hook runs inside a try/except. Failures are logged and
            recorded in the aggregate result but never propagate to the caller.
            By default (fail-open), a failed hook produces an ALLOW result.

        Timeout Protection:
            Each hook must complete within ``hook.timeout_s`` seconds.
            If it exceeds the deadline, a HookTimeoutError is recorded and
            the hook is treated as an error (ALLOW by default).

        Returns:
            AggregateHookResult merging all hook decisions.
        """
        aggregate = AggregateHookResult()

        # Snapshot hook names under lock, then execute without the lock
        hook_names: list[str]
        with self._lock:
            hook_names = list(self._event_index.get(event_type, []))

        if not hook_names:
            return aggregate

        logger.trace(  # type: ignore[attr-defined]
            f"[HookManager] Executing {len(hook_names)} hook(s) for {event_type.value}"
        )

        for name in hook_names:
            hook: Optional[HookBase]
            with self._lock:
                hook = self._hooks.get(name)
            if hook is None or not hook.enabled:
                continue

            start = time.monotonic()
            try:
                timeout = hook.timeout_s or self._default_timeout_s
                result = await asyncio.wait_for(
                    hook.on_event(context),
                    timeout=timeout,
                )
                duration = time.monotonic() - start

                # Validate return type
                if not isinstance(result, HookResult):
                    logger.warning(
                        f"[HookManager] Hook '{name}' returned {type(result).__name__} "
                        f"instead of HookResult — treating as ALLOW"
                    )
                    result = HookResult.allow()

                aggregate.add(result, hook_name=name)
                self._metrics[name].record(duration, result.decision)

            except asyncio.TimeoutError:
                duration = time.monotonic() - start
                timeout_val = hook.timeout_s or self._default_timeout_s
                err = HookTimeoutError(name, event_type, timeout_val)
                aggregate.add_error(err)
                self._metrics[name].record(duration, HookDecision.ALLOW, is_timeout=True)

                # Fail-open or fail-closed
                fallback = HookResult.allow() if hook.fail_open else HookResult.deny(
                    f"Hook '{name}' timed out"
                )
                aggregate.add(fallback, hook_name=name)

                logger.warning(
                    f"[HookManager] Hook '{name}' timed out after {timeout_val:.1f}s "
                    f"on {event_type.value}"
                )

            except Exception as e:
                duration = time.monotonic() - start
                err = HookError(name, event_type, str(e))
                aggregate.add_error(err)
                self._metrics[name].record(duration, HookDecision.ALLOW, is_error=True)

                # Fail-open or fail-closed
                fallback = HookResult.allow() if hook.fail_open else HookResult.deny(
                    f"Hook '{name}' raised an error: {e}"
                )
                aggregate.add(fallback, hook_name=name)

                logger.error(
                    f"[HookManager] Hook '{name}' raised {type(e).__name__}: {e}",
                    exc_info=True,
                )

        return aggregate

    # ──────────────────────────────────────────────────────────────────────
    # Convenience shortcuts
    # ──────────────────────────────────────────────────────────────────────

    async def emit_session_start(
        self,
        conversation_id: str,
        agent_name: str = "",
        source: str = "system",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """Emit a SESSION_START event."""
        ctx = HookContext(
            event_type=HookEventType.SESSION_START,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.SESSION_START, ctx)

    async def emit_session_end(
        self,
        conversation_id: str,
        agent_name: str = "",
        source: str = "system",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """Emit a SESSION_END event."""
        ctx = HookContext(
            event_type=HookEventType.SESSION_END,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.SESSION_END, ctx)

    async def emit_pre_tool_use(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        conversation_id: str = "",
        agent_name: str = "",
        source: str = "agent",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """
        Emit a PRE_TOOL_USE event. Returns aggregate result.
        Check ``result.is_denied`` to decide whether to proceed.
        """
        ctx = HookContext(
            event_type=HookEventType.PRE_TOOL_USE,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            tool_name=tool_name,
            tool_args=tool_args,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.PRE_TOOL_USE, ctx)

    async def emit_post_tool_use(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: Any = None,
        conversation_id: str = "",
        agent_name: str = "",
        source: str = "agent",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """Emit a POST_TOOL_USE event (observation only)."""
        ctx = HookContext(
            event_type=HookEventType.POST_TOOL_USE,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.POST_TOOL_USE, ctx)

    async def emit_user_prompt_submit(
        self,
        user_message: str,
        conversation_id: str = "",
        agent_name: str = "",
        source: str = "user",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """
        Emit a USER_PROMPT_SUBMIT event.
        Check ``result.is_denied`` to block, ``result.is_modified`` to use
        ``result.modified_content`` instead of the original message.
        """
        ctx = HookContext(
            event_type=HookEventType.USER_PROMPT_SUBMIT,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            user_message=user_message,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.USER_PROMPT_SUBMIT, ctx)

    async def emit_stop(
        self,
        conversation_id: str = "",
        agent_name: str = "",
        source: str = "agent",
        extra: Optional[dict[str, Any]] = None,
    ) -> AggregateHookResult:
        """
        Emit a STOP event. Check ``result.is_denied`` to prevent the
        agent from stopping.
        """
        ctx = HookContext(
            event_type=HookEventType.STOP,
            conversation_id=conversation_id,
            agent_name=agent_name,
            source=source,
            extra=extra or {},
        )
        return await self.execute_hooks(HookEventType.STOP, ctx)

    # ──────────────────────────────────────────────────────────────────────
    # Metrics
    # ──────────────────────────────────────────────────────────────────────

    def get_metrics(self, hook_name: Optional[str] = None) -> dict[str, Any]:
        """
        Return execution metrics.

        If hook_name is given, returns metrics for that hook only.
        Otherwise, returns metrics for all hooks.
        """
        if hook_name:
            m = self._metrics.get(hook_name)
            return m.snapshot() if m else {}
        return {name: m.snapshot() for name, m in self._metrics.items()}

    def get_stats(self) -> dict[str, Any]:
        """Return a summary of the hook manager's state."""
        with self._lock:
            hook_names = list(self._hooks.keys())
            event_counts = {
                et.value: len(names) for et, names in self._event_index.items()
            }
        return {
            "total_hooks": len(hook_names),
            "hook_names": hook_names,
            "events_with_subscribers": event_counts,
            "initialized": self._initialized,
            "default_timeout_s": self._default_timeout_s,
        }
