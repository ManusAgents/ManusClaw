"""
ManusClaw Conversation System — ConversationState
====================================================

The mutable runtime state of a single conversation.  This is the central
bookkeeping object that tracks execution status, the event log, the LLM
view, security configuration, hook results, and secret management.

State machine::

    IDLE ──→ RUNNING ──→ FINISHED
                  │
                  ├──→ PAUSED ──→ RUNNING (resume)
                  │
                  └──→ ERROR

Thread Safety:
    All mutations go through :class:`FIFOLock` to guarantee fair,
    ordered access from concurrent threads and async tasks.
"""

from __future__ import annotations

import threading
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from app.conversation.fifo_lock import AsyncFIFOLock, FIFOLock
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Execution Status
# ──────────────────────────────────────────────────────────────────────────────

class ExecutionStatus(str, Enum):
    """
    The lifecycle status of a conversation's agent run.

    Transitions:
        IDLE     → RUNNING   (run starts)
        RUNNING  → FINISHED  (normal completion)
        RUNNING  → PAUSED    (user or hook pauses)
        RUNNING  → ERROR     (unrecoverable error)
        PAUSED   → RUNNING   (resume)
        RUNNING  → IDLE      (reset)
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    ERROR = "ERROR"

    def is_terminal(self) -> bool:
        """Return True if this state cannot transition further."""
        return self in (ExecutionStatus.FINISHED, ExecutionStatus.ERROR)


# ──────────────────────────────────────────────────────────────────────────────
# Allowed transitions
# ──────────────────────────────────────────────────────────────────────────────

_VALID_TRANSITIONS: Dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.IDLE: {ExecutionStatus.RUNNING},
    ExecutionStatus.RUNNING: {
        ExecutionStatus.FINISHED,
        ExecutionStatus.PAUSED,
        ExecutionStatus.ERROR,
        ExecutionStatus.IDLE,
    },
    ExecutionStatus.PAUSED: {ExecutionStatus.RUNNING, ExecutionStatus.ERROR},
    ExecutionStatus.FINISHED: set(),
    ExecutionStatus.ERROR: set(),
}


# ──────────────────────────────────────────────────────────────────────────────
# Secret Registry
# ──────────────────────────────────────────────────────────────────────────────

class SecretRegistry:
    """
    Thread-safe registry for secrets that should be redacted from
    conversation events before they are persisted or transmitted.

    Secrets are stored as (key, value) pairs.  The ``redact`` method
    replaces any occurrence of a secret value with a placeholder.
    """

    def __init__(self) -> None:
        self._secrets: Dict[str, str] = {}
        self._lock: threading.Lock = threading.Lock()

    def register(self, key: str, value: str) -> None:
        """
        Register a secret value under *key*.

        If *value* is empty, the registration is silently skipped.
        """
        if not value:
            return
        with self._lock:
            self._secrets[key] = value

    def unregister(self, key: str) -> None:
        """Remove a secret by its key."""
        with self._lock:
            self._secrets.pop(key, None)

    def redact(self, text: str) -> str:
        """
        Return *text* with all registered secret values replaced by
        ``<REDACTED:key>`` placeholders.
        """
        with self._lock:
            for key, value in self._secrets.items():
                if value and value in text:
                    text = text.replace(value, f"<REDACTED:{key}>")
        return text

    def clear(self) -> None:
        """Remove all registered secrets."""
        with self._lock:
            self._secrets.clear()

    @property
    def count(self) -> int:
        """Number of registered secrets."""
        with self._lock:
            return len(self._secrets)

    def __repr__(self) -> str:
        return f"<SecretRegistry count={self.count}>"


# ──────────────────────────────────────────────────────────────────────────────
# ConversationState
# ──────────────────────────────────────────────────────────────────────────────

class ConversationState:
    """
    The mutable runtime state of a single conversation.

    This object is the single source of truth for:
      - **execution_status**: the current agent lifecycle phase.
      - **events**: a reference to the :class:`EventLog` for this conversation.
      - **view**: a reference to the :class:`View` (LLM context window).
      - **confirmation_policy**: the security confirmation policy.
      - **security_analyzer**: the active security analyzer (if any).
      - **secret_registry**: redaction registry for secrets.
      - **blocked_actions**: actions blocked by hooks.
      - **blocked_messages**: messages blocked by hooks.

    All mutations are protected by a :class:`FIFOLock` for fair concurrent
    access, and an :class:`AsyncFIFOLock` for async callers.

    Usage::

        state = ConversationState()
        state.set_status(ExecutionStatus.RUNNING)
        # ... agent runs ...
        state.set_status(ExecutionStatus.FINISHED)
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
    ) -> None:
        self._id: str = conversation_id or str(uuid.uuid4())
        self._status: ExecutionStatus = ExecutionStatus.IDLE
        self._lock: FIFOLock = FIFOLock()
        self._async_lock: AsyncFIFOLock = AsyncFIFOLock()

        # Event log reference (set by the conversation that owns this state)
        self._events: Optional[Any] = None  # EventLog
        # View reference (LLM context window)
        self._view: Optional[Any] = None  # View

        # Security
        self._confirmation_policy: Optional[Any] = confirmation_policy
        self._security_analyzer: Optional[Any] = security_analyzer
        self._secret_registry: SecretRegistry = SecretRegistry()

        # Hook results — actions/messages blocked by hooks
        self._blocked_actions: List[Dict[str, Any]] = []
        self._blocked_messages: List[Dict[str, Any]] = []

        # Metadata
        self._created_at: Optional[float] = None
        self._updated_at: Optional[float] = None
        self._error_message: Optional[str] = None
        self._pause_reason: Optional[str] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        """Unique identifier for this conversation."""
        return self._id

    # ── Execution status ──────────────────────────────────────────────────────

    @property
    def execution_status(self) -> ExecutionStatus:
        """Current execution status (thread-safe read)."""
        return self._status

    def set_status(self, new_status: ExecutionStatus) -> ExecutionStatus:
        """
        Transition to *new_status* if the transition is valid.

        Args:
            new_status: The target status.

        Returns:
            The previous status.

        Raises:
            ValueError: If the transition is not allowed.

        Thread Safety:
            Protected by FIFOLock.
        """
        import time

        with self._lock:
            old = self._status
            if new_status not in _VALID_TRANSITIONS.get(old, set()):
                if old == new_status:
                    return old  # Idempotent no-op
                raise ValueError(
                    f"Invalid state transition: {old.value} → {new_status.value}"
                )
            self._status = new_status
            self._updated_at = time.monotonic()

            # Clear transient state on terminal transitions
            if new_status == ExecutionStatus.IDLE:
                self._error_message = None
                self._pause_reason = None
            elif new_status == ExecutionStatus.RUNNING:
                self._pause_reason = None
            elif new_status == ExecutionStatus.ERROR:
                pass  # error_message set separately
            elif new_status == ExecutionStatus.PAUSED:
                pass  # pause_reason set separately

            logger.debug(
                f"[ConversationState:{self._id[:8]}] "
                f"Status: {old.value} → {new_status.value}"
            )
            return old

    def try_set_status(self, new_status: ExecutionStatus) -> bool:
        """
        Attempt to transition to *new_status* without raising on failure.

        Returns:
            ``True`` if the transition succeeded, ``False`` otherwise.
        """
        try:
            self.set_status(new_status)
            return True
        except ValueError:
            return False

    @property
    def is_running(self) -> bool:
        return self._status == ExecutionStatus.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._status == ExecutionStatus.PAUSED

    @property
    def is_finished(self) -> bool:
        return self._status == ExecutionStatus.FINISHED

    @property
    def is_error(self) -> bool:
        return self._status == ExecutionStatus.ERROR

    @property
    def is_idle(self) -> bool:
        return self._status == ExecutionStatus.IDLE

    @property
    def is_terminal(self) -> bool:
        return self._status.is_terminal()

    # ── Events ────────────────────────────────────────────────────────────────

    @property
    def events(self) -> Optional[Any]:
        """Reference to the EventLog for this conversation."""
        return self._events

    @events.setter
    def events(self, value: Optional[Any]) -> None:
        self._events = value

    # ── View ──────────────────────────────────────────────────────────────────

    @property
    def view(self) -> Optional[Any]:
        """Reference to the View (LLM context window) for this conversation."""
        return self._view

    @view.setter
    def view(self, value: Optional[Any]) -> None:
        self._view = value

    # ── Security ──────────────────────────────────────────────────────────────

    @property
    def confirmation_policy(self) -> Optional[Any]:
        """The active confirmation policy (e.g. NeverConfirm, ConfirmRisky)."""
        return self._confirmation_policy

    @confirmation_policy.setter
    def confirmation_policy(self, value: Optional[Any]) -> None:
        self._confirmation_policy = value

    @property
    def security_analyzer(self) -> Optional[Any]:
        """The active security analyzer (e.g. EnsembleSecurityAnalyzer)."""
        return self._security_analyzer

    @security_analyzer.setter
    def security_analyzer(self, value: Optional[Any]) -> None:
        self._security_analyzer = value

    @property
    def secret_registry(self) -> SecretRegistry:
        """The secret redaction registry."""
        return self._secret_registry

    # ── Blocked actions/messages from hooks ────────────────────────────────────

    def add_blocked_action(self, action: Dict[str, Any]) -> None:
        """
        Record an action that was blocked by a hook.

        Args:
            action: Dict with at least ``tool_name``, ``reason``, and
                    optionally ``tool_args``.
        """
        with self._lock:
            self._blocked_actions.append(action)

    def add_blocked_message(self, message: Dict[str, Any]) -> None:
        """
        Record a message that was blocked by a hook.

        Args:
            message: Dict with at least ``reason`` and optionally
                     ``content``.
        """
        with self._lock:
            self._blocked_messages.append(message)

    @property
    def blocked_actions(self) -> List[Dict[str, Any]]:
        """Snapshot of all blocked actions."""
        with self._lock:
            return list(self._blocked_actions)

    @property
    def blocked_messages(self) -> List[Dict[str, Any]]:
        """Snapshot of all blocked messages."""
        with self._lock:
            return list(self._blocked_messages)

    # ── Error / pause info ────────────────────────────────────────────────────

    @property
    def error_message(self) -> Optional[str]:
        return self._error_message

    @error_message.setter
    def error_message(self, value: Optional[str]) -> None:
        self._error_message = value

    @property
    def pause_reason(self) -> Optional[str]:
        return self._pause_reason

    @pause_reason.setter
    def pause_reason(self, value: Optional[str]) -> None:
        self._pause_reason = value

    # ── Async lock access ─────────────────────────────────────────────────────

    @property
    def async_lock(self) -> AsyncFIFOLock:
        """The async FIFO lock for protecting async mutations."""
        return self._async_lock

    @property
    def sync_lock(self) -> FIFOLock:
        """The sync FIFO lock for protecting synchronous mutations."""
        return self._lock

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """
        Return a read-only snapshot of the state for diagnostics / UI.

        The snapshot includes status, blocked counts, and metadata but
        **not** the event log or view (those are large and have their
        own access patterns).
        """
        with self._lock:
            return {
                "id": self._id,
                "status": self._status.value,
                "error_message": self._error_message,
                "pause_reason": self._pause_reason,
                "blocked_actions_count": len(self._blocked_actions),
                "blocked_messages_count": len(self._blocked_messages),
                "secret_count": self._secret_registry.count,
                "has_confirmation_policy": self._confirmation_policy is not None,
                "has_security_analyzer": self._security_analyzer is not None,
                "has_events": self._events is not None,
                "has_view": self._view is not None,
            }

    def __repr__(self) -> str:
        return (
            f"<ConversationState id={self._id[:8]} "
            f"status={self._status.value}>"
        )
