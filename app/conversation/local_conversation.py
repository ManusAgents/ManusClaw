"""
ManusClaw Conversation System — LocalConversation
====================================================

A conversation that runs the agent locally with direct tool execution,
file-backed event persistence, and full lifecycle management.

Key features:
  - Agent runs in-process with direct tool dispatch.
  - Events persisted to a file-backed :class:`EventLog`.
  - Async support with true async LLM calls.
  - Cancellation via :class:`CancellationToken`.
  - Fork support for branching conversations.
  - Confirmation mode for human-in-the-loop approval.
  - Hook integration for pre/post processing.
  - Auto-generated conversation titles.
  - Tool preloading for faster startup.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.conversation.base import BaseConversation
from app.conversation.cancellation_token import CancellationToken, CancelledError
from app.conversation.state import ConversationState, ExecutionStatus
from app.conversation.stuck_detector import StuckDetector, StuckReport
from app.events.base import Event
from app.events.event_log import EventLog
from app.events.types import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStateUpdateEvent,
    FunctionCall,
    HookExecutionEvent,
    MessageEvent,
    ObservationEvent,
    PauseEvent,
    ResumeTranscriptEvent,
    InterruptEvent,
    SystemPromptEvent,
    ToolCallInfo,
    TokenEvent,
    UserRejectObservation,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Title Generation
# ──────────────────────────────────────────────────────────────────────────────

def _generate_title(prompt: str, max_length: int = 60) -> str:
    """
    Generate a short conversation title from the user's initial prompt.

    Uses a simple heuristic: take the first line, truncate to
    *max_length*, and add an ellipsis if truncated.
    """
    first_line = prompt.strip().split("\n")[0].strip()
    if len(first_line) <= max_length:
        return first_line
    return first_line[:max_length - 3] + "..."


# ──────────────────────────────────────────────────────────────────────────────
# LocalConversation
# ──────────────────────────────────────────────────────────────────────────────

class LocalConversation(BaseConversation):
    """
    A conversation that runs the agent locally with direct tool execution.

    The agent runs in the same process, and events are persisted to a
    file-backed :class:`EventLog`.  This is the default conversation
    type for single-machine deployments.

    Args:
        conversation_id:  Optional unique ID (auto-generated if not provided).
        event_log_dir:    Directory for event log files.  Uses a temp dir
                          if not specified.
        agent:            An optional pre-configured agent instance.
        confirmation_policy: Security confirmation policy.
        security_analyzer:   Security analyzer instance.
        stuck_detector:    Stuck pattern detector.
        hook_manager:      Hook manager for lifecycle events.
        auto_title:        Whether to auto-generate a title from the prompt.
        preload_tools:     List of tool names to preload before the first run.
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        event_log_dir: Optional[str] = None,
        agent: Optional[Any] = None,
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
        stuck_detector: Optional[StuckDetector] = None,
        hook_manager: Optional[Any] = None,
        auto_title: bool = True,
        preload_tools: Optional[List[str]] = None,
    ) -> None:
        super().__init__(
            conversation_id=conversation_id,
            confirmation_policy=confirmation_policy,
            security_analyzer=security_analyzer,
            stuck_detector=stuck_detector,
            hook_manager=hook_manager,
        )

        self._agent: Optional[Any] = agent
        self._auto_title: bool = auto_title
        self._preload_tools: List[str] = preload_tools or []
        self._preloaded: bool = False

        # Set up the event log
        log_dir = event_log_dir or tempfile.mkdtemp(prefix="manusclaw_conv_")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, self._id)
        self._event_log: EventLog = EventLog(log_path)
        self._state.events = self._event_log

        # Set up the view (deferred — set when agent is configured)
        self._state.view = None

        # Track run metadata
        self._run_start_time: Optional[float] = None
        self._run_prompt: Optional[str] = None

    # ── Event log access ──────────────────────────────────────────────────────

    @property
    def event_log(self) -> EventLog:
        """Direct access to the file-backed event log."""
        return self._event_log

    # ── Agent management ──────────────────────────────────────────────────────

    @property
    def agent(self) -> Optional[Any]:
        """The agent instance for this conversation (if configured)."""
        return self._agent

    @agent.setter
    def agent(self, value: Any) -> None:
        """Set the agent instance and wire up the view if available."""
        self._agent = value

    # ── Tool preloading ───────────────────────────────────────────────────────

    def _preload_tools_if_needed(self) -> None:
        """
        Preload tools by running a warm-up cycle with the agent.

        This ensures that tool definitions are loaded and cached before
        the first real user interaction, reducing latency.
        """
        if self._preloaded or not self._preload_tools or not self._agent:
            return

        try:
            if hasattr(self._agent, "preload_tools"):
                self._agent.preload_tools(self._preload_tools)
                logger.debug(
                    f"[LocalConversation:{self._id[:8]}] "
                    f"Preloaded {len(self._preload_tools)} tools"
                )
        except Exception as e:
            logger.warning(
                f"[LocalConversation:{self._id[:8]}] "
                f"Tool preloading failed: {e}"
            )
        finally:
            self._preloaded = True

    # ── Hook integration ──────────────────────────────────────────────────────

    async def _emit_hook(
        self,
        event_type: Any,
        **context_fields: Any,
    ) -> Any:
        """
        Emit a hook event through the hook manager (if configured).

        Returns an AggregateHookResult or None if no hook manager.
        """
        if self._hook_manager is None:
            return None

        from app.hooks.types import HookContext
        ctx = HookContext(
            event_type=event_type,
            conversation_id=self._id,
            **context_fields,
        )
        return await self._hook_manager.execute_hooks(event_type, ctx)

    # ── Security check ────────────────────────────────────────────────────────

    def _check_security(self, action: str, context: Optional[Dict[str, Any]] = None) -> Any:
        """
        Run the security analyzer on an action (if configured).

        Returns a RiskAssessment or None.
        """
        if self._state.security_analyzer is None:
            return None
        return self._state.security_analyzer.safe_analyze(action, context)

    def _requires_confirmation(self, assessment: Any) -> bool:
        """
        Check if a risk assessment requires user confirmation.

        Returns True if the confirmation policy says confirmation is needed.
        """
        if self._state.confirmation_policy is None:
            return False
        if assessment is None:
            return False
        decision = self._state.confirmation_policy.requires_confirmation(assessment)
        return decision.needs_confirmation

    # ── Title generation ──────────────────────────────────────────────────────

    def _maybe_generate_title(self, prompt: str) -> None:
        """Auto-generate a conversation title if not already set."""
        if self._auto_title and not self._title:
            self._title = _generate_title(prompt)

    # ── Abstract method implementations ───────────────────────────────────────

    def _do_send_message(self, message: str, **kwargs: Any) -> Any:
        """
        Deliver a user message to the agent (synchronous).

        Creates a MessageEvent and appends it to the event log.
        If an agent is configured, runs the agent loop.
        """
        event = MessageEvent(
            content=message,
            role="user",
            source="user",
        )
        self._event_log.append(event)

        # If the agent is idle, we can run it
        if self._agent is not None and self._state.is_idle:
            return self.run(message, **kwargs)

        return {"event_id": event.id, "status": "recorded"}

    async def _do_asend_message(self, message: str, **kwargs: Any) -> Any:
        """
        Deliver a user message asynchronously.

        Creates a MessageEvent, appends it to the event log, and
        runs the agent loop asynchronously if the agent is idle.
        """
        event = MessageEvent(
            content=message,
            role="user",
            source="user",
        )
        self._event_log.append(event)

        if self._agent is not None and self._state.is_idle:
            return await self.arun(message, **kwargs)

        return {"event_id": event.id, "status": "recorded"}

    def _do_run(self, prompt: str, **kwargs: Any) -> Any:
        """
        Execute the agent loop synchronously.

        If no agent is configured, records the prompt as a MessageEvent
        and returns a basic result.
        """
        self._run_start_time = time.monotonic()
        self._run_prompt = prompt
        self._maybe_generate_title(prompt)

        # Record the prompt as a message event
        prompt_event = MessageEvent(
            content=prompt,
            role="user",
            source="user",
        )
        self._event_log.append(prompt_event)

        # Record state change event
        state_event = ConversationStateUpdateEvent(
            old_state="IDLE",
            new_state="RUNNING",
        )
        self._event_log.append(state_event)

        if self._agent is None:
            logger.warning(
                f"[LocalConversation:{self._id[:8]}] "
                f"No agent configured; recording prompt only"
            )
            return {
                "conversation_id": self._id,
                "status": "no_agent",
                "event_id": prompt_event.id,
            }

        # Preload tools
        self._preload_tools_if_needed()

        # Run the agent synchronously via asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We're already inside an event loop — schedule the agent
            # as a task and block-wait for it
            future = asyncio.ensure_future(
                self._run_agent_async(prompt, **kwargs)
            )
            # This will work if there's a running loop that we can
            # piggyback on; otherwise we need a new loop.
            try:
                return loop.run_until_complete(future)
            except RuntimeError:
                # Can't nest event loops — use a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(
                        asyncio.run,
                        self._run_agent_async(prompt, **kwargs)
                    ).result()
                return result
        else:
            # No running loop — create one
            return asyncio.run(self._run_agent_async(prompt, **kwargs))

    async def _do_arun(self, prompt: str, **kwargs: Any) -> Any:
        """
        Execute the agent loop asynchronously.

        This is the primary execution path — ``_do_run`` delegates here.
        """
        self._run_start_time = time.monotonic()
        self._run_prompt = prompt
        self._maybe_generate_title(prompt)

        # Record the prompt
        prompt_event = MessageEvent(
            content=prompt,
            role="user",
            source="user",
        )
        self._event_log.append(prompt_event)

        # Record state change
        state_event = ConversationStateUpdateEvent(
            old_state="IDLE",
            new_state="RUNNING",
        )
        self._event_log.append(state_event)

        if self._agent is None:
            return {
                "conversation_id": self._id,
                "status": "no_agent",
                "event_id": prompt_event.id,
            }

        self._preload_tools_if_needed()
        return await self._run_agent_async(prompt, **kwargs)

    async def _run_agent_async(self, prompt: str, **kwargs: Any) -> Any:
        """
        Core async agent execution with cancellation, stuck detection,
        and hook integration.
        """
        result_text: str = ""
        step_count: int = 0
        max_steps: int = kwargs.get("max_steps", 30)

        try:
            # Emit session_start hook
            if self._hook_manager is not None:
                from app.hooks.types import HookEventType
                await self._emit_hook(
                    HookEventType.SESSION_START,
                    agent_name=getattr(self._agent, "name", "unknown"),
                )

            while self._state.is_running and step_count < max_steps:
                # Check cancellation
                self._cancellation_token.raise_if_cancelled()

                # Check stuck patterns
                stuck_report = self.check_stuck()
                if stuck_report.is_stuck:
                    logger.warning(
                        f"[LocalConversation:{self._id[:8]}] "
                        f"Stuck detected: {stuck_report.pattern} — "
                        f"{stuck_report.suggestion}"
                    )
                    # Inject a nudge message
                    nudge_event = MessageEvent(
                        content=(
                            f"[System] Stuck detection alert: {stuck_report.detail}. "
                            f"Suggestion: {stuck_report.suggestion}"
                        ),
                        role="user",
                        source="agent",
                    )
                    self._event_log.append(nudge_event)

                step_count += 1

                # Run a single agent step
                if hasattr(self._agent, "step"):
                    step_result = await self._agent.step()
                    if step_result:
                        result_text += (step_result + "\n") if step_result else ""
                elif hasattr(self._agent, "run"):
                    # Full run (not step-by-step)
                    try:
                        full_result = await self._agent.run(prompt)
                        result_text = full_result if isinstance(full_result, str) else str(full_result)
                    except Exception as e:
                        error_event = AgentErrorEvent(
                            error=str(e),
                            error_type=type(e).__name__,
                        )
                        self._event_log.append(error_event)
                        raise
                    break  # Full run completed
                else:
                    logger.warning(
                        f"[LocalConversation:{self._id[:8]}] "
                        f"Agent has no step() or run() method"
                    )
                    break

            # Record final state
            if self._state.is_running:
                final_event = ConversationStateUpdateEvent(
                    old_state="RUNNING",
                    new_state="FINISHED",
                )
                self._event_log.append(final_event)

            # Emit session_end hook
            if self._hook_manager is not None:
                from app.hooks.types import HookEventType
                await self._emit_hook(
                    HookEventType.SESSION_END,
                    agent_name=getattr(self._agent, "name", "unknown"),
                )

        except CancelledError:
            interrupt_event = InterruptEvent(
                reason=self._cancellation_token.reason,
            )
            self._event_log.append(interrupt_event)
            raise

        except Exception as e:
            error_event = AgentErrorEvent(
                error=str(e),
                error_type=type(e).__name__,
            )
            self._event_log.append(error_event)
            raise

        finally:
            duration = (
                time.monotonic() - self._run_start_time
                if self._run_start_time else 0.0
            )
            self._event_log.flush()

            logger.info(
                f"[LocalConversation:{self._id[:8]}] "
                f"Run completed: steps={step_count} duration={duration:.1f}s"
            )

        return {
            "conversation_id": self._id,
            "status": self._state.execution_status.value,
            "result": result_text.strip(),
            "steps": step_count,
            "duration_s": round(duration, 2) if self._run_start_time else 0.0,
        }

    def _do_fork(self, new_id: str) -> "LocalConversation":
        """
        Create a deep copy of this conversation with *new_id*.

        The fork gets its own event log, state, and cancellation token.
        Events from the original are copied to the fork's log.
        """
        # Create a new event log for the fork
        log_dir = os.path.dirname(self._event_log._path) if hasattr(self._event_log, '_path') else tempfile.mkdtemp(prefix="manusclaw_fork_")
        os.makedirs(log_dir, exist_ok=True)
        # Compose the expected log path for diagnostics (the LocalConversation
        # constructor uses ``event_log_dir`` + conversation_id to name the file).
        fork_log_path = os.path.join(log_dir, new_id)
        logger.debug(f"[LocalConversation:{self._id[:8]}] Forking to {fork_log_path}")

        forked = LocalConversation(
            conversation_id=new_id,
            event_log_dir=log_dir,
            agent=None,  # Don't share the agent instance
            confirmation_policy=self._state.confirmation_policy,
            security_analyzer=self._state.security_analyzer,
            stuck_detector=StuckDetector(
                window_size=self._stuck_detector.window_size,
                repeat_threshold=self._stuck_detector.repeat_threshold,
                monologue_threshold=self._stuck_detector.monologue_threshold,
            ),
            hook_manager=self._hook_manager,
            auto_title=self._auto_title,
            preload_tools=list(self._preload_tools),
        )

        # Copy events from the original
        try:
            original_events = list(self._event_log)
            for event in original_events:
                forked._event_log.append(event)
        except Exception as e:
            logger.warning(
                f"[LocalConversation:{self._id[:8]}] "
                f"Failed to copy events to fork: {e}"
            )

        # Copy title
        forked._title = self._title

        return forked

    def _do_interrupt(self, reason: str) -> None:
        """
        Handle interruption by recording an InterruptEvent.
        """
        event = InterruptEvent(reason=reason)
        self._event_log.append(event)

    def _do_resume(self, **kwargs: Any) -> None:
        """
        Handle resumption by recording a ResumeTranscriptEvent.
        """
        event = ResumeTranscriptEvent(reason="Resumed by user")
        self._event_log.append(event)

    # ── Confirmation mode ─────────────────────────────────────────────────────

    def confirm_action(self, action_event: ActionEvent, approved: bool, reason: str = "") -> None:
        """
        Confirm or reject a pending action that required human approval.

        Args:
            action_event: The action that was pending confirmation.
            approved:     Whether the user approved the action.
            reason:       Optional reason for rejection.
        """
        if approved:
            # The action can proceed — record the approval
            logger.info(
                f"[LocalConversation:{self._id[:8]}] "
                f"Action approved: {action_event.tool_call.function.name}"
            )
        else:
            # Record user rejection
            reject_event = UserRejectObservation(
                tool_call_id=action_event.tool_call.id,
                tool_name=action_event.tool_call.function.name,
                reason=reason or "User rejected the action",
            )
            self._event_log.append(reject_event)
            logger.info(
                f"[LocalConversation:{self._id[:8]}] "
                f"Action rejected: {action_event.tool_call.function.name}"
            )

    # ── Override: get events ──────────────────────────────────────────────────

    def get_events(self) -> List[Event]:
        """Return all events from the file-backed event log."""
        try:
            return list(self._event_log)
        except Exception:
            return []

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the conversation and flush the event log."""
        super().close()
        try:
            self._event_log.flush()
        except Exception:
            pass

    def __repr__(self) -> str:
        has_agent = self._agent is not None
        event_count = 0
        try:
            event_count = len(self._event_log)
        except Exception:
            pass
        return (
            f"<LocalConversation id={self._id[:8]} "
            f"status={self._state.execution_status.value} "
            f"agent={'yes' if has_agent else 'no'} "
            f"events={event_count}>"
        )
