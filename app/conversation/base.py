"""
ManusClaw Conversation System — BaseConversation ABC
======================================================

The abstract base class that defines the public contract for all
conversation implementations (local, remote, etc.).

A Conversation is the top-level orchestrator for an agent session.  It
manages:

  - The conversation lifecycle (start, run, pause, resume, stop).
  - The event log (persistence and retrieval).
  - The LLM context view.
  - Cancellation tokens for graceful shutdown.
  - Forking (branching) a conversation into a new independent instance.
  - Hook integration for pre/post processing.
  - Security analysis and confirmation policies.
  - Stuck detection and recovery.

Subclasses must implement:
  - :meth:`_do_send_message` — actually deliver a user message to the agent.
  - :meth:`_do_run` — actually execute the agent loop synchronously.
  - :meth:`_do_arun` — actually execute the agent loop asynchronously.
  - :meth:`_do_fork` — create a deep copy with a new ID.
  - :meth:`_do_interrupt` — cancel the running agent loop.
  - :meth:`_do_resume` — resume from a paused state.
"""

from __future__ import annotations

import copy
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from app.conversation.cancellation_token import CancellationToken, CancelledError
from app.conversation.state import ConversationState, ExecutionStatus
from app.conversation.stuck_detector import StuckDetector, StuckReport
from app.events.base import Event
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# BaseConversation
# ──────────────────────────────────────────────────────────────────────────────

class BaseConversation(ABC):
    """
    Abstract base class for all conversation implementations.

    Provides the public API and common lifecycle management.  Subclasses
    implement the transport-specific logic (local execution, remote
    WebSocket, etc.) via the ``_do_*`` hook methods.

    Attributes:
        conversation_id:  Unique identifier for this conversation.
        state:            The mutable :class:`ConversationState`.
        cancellation_token: Token for signalling graceful shutdown.
        stuck_detector:   Detector for stuck agent loops.
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
        stuck_detector: Optional[StuckDetector] = None,
        hook_manager: Optional[Any] = None,
    ) -> None:
        self._id: str = conversation_id or str(uuid.uuid4())
        self._state: ConversationState = ConversationState(
            conversation_id=self._id,
            confirmation_policy=confirmation_policy,
            security_analyzer=security_analyzer,
        )
        self._cancellation_token: CancellationToken = CancellationToken()
        self._stuck_detector: StuckDetector = stuck_detector or StuckDetector()
        self._hook_manager: Optional[Any] = hook_manager
        self._title: Optional[str] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def conversation_id(self) -> str:
        """Unique identifier for this conversation."""
        return self._id

    @property
    def title(self) -> Optional[str]:
        """Auto-generated or user-set title for this conversation."""
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def state(self) -> ConversationState:
        """The mutable runtime state of this conversation."""
        return self._state

    @property
    def execution_status(self) -> ExecutionStatus:
        """Shortcut for ``self.state.execution_status``."""
        return self._state.execution_status

    # ── Cancellation ──────────────────────────────────────────────────────────

    @property
    def cancellation_token(self) -> CancellationToken:
        """The cancellation token for this conversation."""
        return self._cancellation_token

    # ── Stuck detector ────────────────────────────────────────────────────────

    @property
    def stuck_detector(self) -> StuckDetector:
        """The stuck pattern detector for this conversation."""
        return self._stuck_detector

    # ── Hook manager ──────────────────────────────────────────────────────────

    @property
    def hook_manager(self) -> Optional[Any]:
        """The hook manager for this conversation (if configured)."""
        return self._hook_manager

    # ── Events ────────────────────────────────────────────────────────────────

    def get_events(self) -> List[Event]:
        """
        Return all events in the conversation's event log.

        Returns:
            List of Event instances (may be empty).
        """
        if self._state.events is not None:
            try:
                return list(self._state.events)
            except Exception:
                pass
        return []

    def get_recent_events(self, n: int = 20) -> List[Event]:
        """
        Return the last *n* events.

        Args:
            n: Number of recent events to return.

        Returns:
            List of up to *n* Event instances.
        """
        events = self.get_events()
        return events[-n:] if events else []

    # ── Stuck detection ───────────────────────────────────────────────────────

    def check_stuck(self) -> StuckReport:
        """
        Analyze the conversation's events for stuck patterns.

        Returns:
            A :class:`StuckReport` with the detection result.
        """
        events = self.get_events()
        return self._stuck_detector.analyze(events)

    # ── Public lifecycle API ──────────────────────────────────────────────────

    def send_message(self, message: str, **kwargs: Any) -> Any:
        """
        Send a user message to the agent.

        This is the primary entry point for user interaction.  The
        message is validated through hooks (if configured) and then
        passed to the subclass-specific delivery method.

        Args:
            message: The user's text message.
            **kwargs: Additional subclass-specific parameters.

        Returns:
            Subclass-specific response (e.g. a result object or None).

        Raises:
            RuntimeError: If the conversation is in a terminal state.
        """
        if self._state.is_terminal:
            raise RuntimeError(
                f"Cannot send message to conversation in "
                f"{self._state.execution_status.value} state"
            )

        # Redact secrets from the message
        redacted = self._state.secret_registry.redact(message)

        return self._do_send_message(redacted, **kwargs)

    async def asend_message(self, message: str, **kwargs: Any) -> Any:
        """
        Async variant of :meth:`send_message`.

        Args:
            message: The user's text message.
            **kwargs: Additional subclass-specific parameters.

        Returns:
            Subclass-specific response.
        """
        if self._state.is_terminal:
            raise RuntimeError(
                f"Cannot send message to conversation in "
                f"{self._state.execution_status.value} state"
            )

        redacted = self._state.secret_registry.redact(message)
        return await self._do_asend_message(redacted, **kwargs)

    def run(self, prompt: str, **kwargs: Any) -> Any:
        """
        Run the agent loop synchronously with the given prompt.

        The conversation must be in IDLE state.  Transitions to RUNNING
        and then to FINISHED/ERROR upon completion.

        Args:
            prompt: The initial user prompt.
            **kwargs: Additional subclass-specific parameters.

        Returns:
            Subclass-specific result.

        Raises:
            RuntimeError: If the conversation is not in IDLE state.
            CancelledError: If the run is interrupted.
        """
        if not self._state.is_idle:
            raise RuntimeError(
                f"Cannot run conversation in "
                f"{self._state.execution_status.value} state; must be IDLE"
            )

        self._cancellation_token = CancellationToken()
        self._state.set_status(ExecutionStatus.RUNNING)

        try:
            result = self._do_run(prompt, **kwargs)
            if self._state.is_running:
                self._state.set_status(ExecutionStatus.FINISHED)
            return result
        except CancelledError:
            if self._state.is_running:
                self._state.set_status(ExecutionStatus.FINISHED)
                self._state.error_message = "Run was cancelled"
            raise
        except Exception as e:
            if self._state.is_running or self._state.is_paused:
                try:
                    self._state.set_status(ExecutionStatus.ERROR)
                except ValueError:
                    pass
                self._state.error_message = str(e)
            raise

    async def arun(self, prompt: str, **kwargs: Any) -> Any:
        """
        Run the agent loop asynchronously with the given prompt.

        The conversation must be in IDLE state.  Transitions to RUNNING
        and then to FINISHED/ERROR upon completion.

        Args:
            prompt: The initial user prompt.
            **kwargs: Additional subclass-specific parameters.

        Returns:
            Subclass-specific result.

        Raises:
            RuntimeError: If the conversation is not in IDLE state.
            CancelledError: If the run is interrupted.
        """
        if not self._state.is_idle:
            raise RuntimeError(
                f"Cannot run conversation in "
                f"{self._state.execution_status.value} state; must be IDLE"
            )

        self._cancellation_token = CancellationToken()
        self._state.set_status(ExecutionStatus.RUNNING)

        try:
            result = await self._do_arun(prompt, **kwargs)
            if self._state.is_running:
                self._state.set_status(ExecutionStatus.FINISHED)
            return result
        except CancelledError:
            if self._state.is_running:
                self._state.set_status(ExecutionStatus.FINISHED)
                self._state.error_message = "Run was cancelled"
            raise
        except Exception as e:
            if self._state.is_running or self._state.is_paused:
                try:
                    self._state.set_status(ExecutionStatus.ERROR)
                except ValueError:
                    pass
                self._state.error_message = str(e)
            raise

    def interrupt(self, reason: str = "Interrupted by user") -> None:
        """
        Gracefully interrupt the running agent loop.

        Sets the cancellation token and transitions the state to PAUSED.
        The agent loop should check the cancellation token periodically
        and exit cleanly when cancelled.

        Args:
            reason: Human-readable reason for the interruption.
        """
        self._cancellation_token.cancel(reason)
        if self._state.is_running:
            try:
                self._state.set_status(ExecutionStatus.PAUSED)
                self._state.pause_reason = reason
            except ValueError:
                pass
        self._do_interrupt(reason)
        logger.info(
            f"[Conversation:{self._id[:8]}] Interrupted: {reason}"
        )

    def resume(self, **kwargs: Any) -> None:
        """
        Resume a paused conversation.

        Creates a fresh cancellation token and transitions the state
        back to RUNNING.

        Args:
            **kwargs: Additional subclass-specific parameters.

        Raises:
            RuntimeError: If the conversation is not in PAUSED state.
        """
        if not self._state.is_paused:
            raise RuntimeError(
                f"Cannot resume conversation in "
                f"{self._state.execution_status.value} state; must be PAUSED"
            )

        self._cancellation_token = CancellationToken()
        self._state.set_status(ExecutionStatus.RUNNING)
        self._do_resume(**kwargs)
        logger.info(
            f"[Conversation:{self._id[:8]}] Resumed"
        )

    def fork(self, new_id: Optional[str] = None) -> "BaseConversation":
        """
        Create a deep copy of this conversation with a new ID.

        The fork inherits all events, state, and configuration but is
        completely independent — mutations to the fork do not affect
        the original and vice versa.

        The original conversation must not be in RUNNING state (forking
        a running conversation could lead to inconsistent state).

        Args:
            new_id: Optional ID for the fork.  Auto-generated if not
                    provided.

        Returns:
            A new :class:`BaseConversation` instance (subclass-specific).

        Raises:
            RuntimeError: If the conversation is currently RUNNING.
        """
        if self._state.is_running:
            raise RuntimeError("Cannot fork a running conversation")

        fork_id = new_id or str(uuid.uuid4())
        forked = self._do_fork(fork_id)

        # Reset the fork's state to IDLE
        forked._state.set_status(ExecutionStatus.IDLE)
        forked._cancellation_token = CancellationToken()

        logger.info(
            f"[Conversation:{self._id[:8]}] Forked to {fork_id[:8]}"
        )
        return forked

    def get_state(self) -> Dict[str, Any]:
        """
        Return a snapshot of the conversation's current state.

        This includes execution status, metadata, and summary info.
        Does **not** include the full event log (use :meth:`get_events`).
        """
        snapshot = self._state.snapshot()
        snapshot["title"] = self._title
        snapshot["type"] = self.__class__.__name__
        return snapshot

    # ── Abstract methods — subclasses MUST implement ──────────────────────────

    @abstractmethod
    def _do_send_message(self, message: str, **kwargs: Any) -> Any:
        """Deliver a user message to the agent (subclass-specific)."""
        ...

    @abstractmethod
    async def _do_asend_message(self, message: str, **kwargs: Any) -> Any:
        """Async variant of message delivery (subclass-specific)."""
        ...

    @abstractmethod
    def _do_run(self, prompt: str, **kwargs: Any) -> Any:
        """Execute the agent loop synchronously (subclass-specific)."""
        ...

    @abstractmethod
    async def _do_arun(self, prompt: str, **kwargs: Any) -> Any:
        """Execute the agent loop asynchronously (subclass-specific)."""
        ...

    @abstractmethod
    def _do_fork(self, new_id: str) -> "BaseConversation":
        """Create a deep copy with *new_id* (subclass-specific)."""
        ...

    @abstractmethod
    def _do_interrupt(self, reason: str) -> None:
        """Handle interruption (subclass-specific)."""
        ...

    @abstractmethod
    def _do_resume(self, **kwargs: Any) -> None:
        """Handle resumption (subclass-specific)."""
        ...

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Clean up resources held by this conversation.

        Subclasses should override to release agent connections, file
        handles, etc.  The base implementation cancels the token and
        flushes the event log.
        """
        if not self._cancellation_token.is_cancelled:
            self._cancellation_token.cancel("Conversation closed")
        if self._state.events is not None:
            try:
                self._state.events.flush()
            except Exception:
                pass

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} id={self._id[:8]} "
            f"status={self._state.execution_status.value}>"
        )
