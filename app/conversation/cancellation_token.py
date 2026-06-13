"""
ManusClaw Conversation System — CancellationToken
====================================================

A thread-safe cancellation primitive for graceful shutdown of conversations
and agent runs.

Design goals:
  - Thread-safe: ``cancel()`` and ``is_cancelled`` can be called from any thread.
  - Composable: works as a context manager for scoped cancellation.
  - Informative: ``raise_if_cancelled()`` raises a clear exception with a reason.
  - Timeout-aware: supports waiting with a deadline.

Usage::

    token = CancellationToken()

    # In the agent loop:
    token.raise_if_cancelled()

    # From another thread (e.g. user presses Ctrl+C):
    token.cancel("User requested stop")

    # As a context manager:
    with CancellationToken() as ct:
        ct.raise_if_cancelled()
        # ... do work ...
    # ct is automatically cancelled when exiting the block
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class CancelledError(Exception):
    """Raised when a CancellationToken has been cancelled."""

    def __init__(self, reason: str = "Operation was cancelled") -> None:
        self.reason = reason
        super().__init__(reason)


class CancellationToken:
    """
    Thread-safe cancellation flag with context-manager and timeout support.

    A CancellationToken can be shared between threads to signal cancellation.
    The token starts in a non-cancelled state and can be cancelled exactly
    once via :meth:`cancel`.  Once cancelled, it cannot be reset.

    Thread Safety:
        All public methods are thread-safe.  Internal state is guarded by
        a ``threading.Lock`` and an ``threading.Event`` for efficient waiting.
    """

    def __init__(self, reason: str = "") -> None:
        self._cancelled: bool = False
        self._reason: str = reason
        self._lock: threading.Lock = threading.Lock()
        self._event: threading.Event = threading.Event()
        self._cancelled_at: Optional[float] = None

    # ── Core API ──────────────────────────────────────────────────────────────

    def cancel(self, reason: str = "Cancelled") -> None:
        """
        Signal cancellation.

        This method is idempotent — calling it multiple times has no
        additional effect after the first call.  The *reason* from the
        first call is preserved.

        Args:
            reason: Human-readable explanation for the cancellation.
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
            self._cancelled_at = time.monotonic()
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Return ``True`` if :meth:`cancel` has been called."""
        return self._cancelled

    @property
    def reason(self) -> str:
        """Return the cancellation reason (empty string if not cancelled)."""
        return self._reason

    @property
    def cancelled_at(self) -> Optional[float]:
        """
        Return the monotonic timestamp when cancellation occurred,
        or ``None`` if not cancelled.
        """
        return self._cancelled_at

    @property
    def elapsed_since_cancel(self) -> Optional[float]:
        """
        Return seconds elapsed since cancellation, or ``None`` if not cancelled.
        """
        if self._cancelled_at is None:
            return None
        return time.monotonic() - self._cancelled_at

    # ── Raising ───────────────────────────────────────────────────────────────

    def raise_if_cancelled(self) -> None:
        """
        Raise :class:`CancelledError` if the token has been cancelled.

        This is the idiomatic way to check cancellation in a hot loop::

            while True:
                token.raise_if_cancelled()
                # ... do work ...

        Raises:
            CancelledError: If the token has been cancelled.
        """
        if self._cancelled:
            raise CancelledError(self._reason)

    def check(self) -> bool:
        """
        Return ``True`` if the token has been cancelled (non-raising check).

        Equivalent to ``token.is_cancelled`` but reads better in conditionals::

            if token.check():
                break
        """
        return self._cancelled

    # ── Waiting ───────────────────────────────────────────────────────────────

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Block until the token is cancelled or *timeout* elapses.

        Args:
            timeout: Maximum seconds to wait.  ``None`` means wait forever.

        Returns:
            ``True`` if the token was cancelled before the timeout,
            ``False`` if the timeout elapsed first.
        """
        return self._event.wait(timeout=timeout)

    def wait_and_raise(self, timeout: Optional[float] = None) -> None:
        """
        Wait for cancellation and then raise :class:`CancelledError`.

        If *timeout* elapses without cancellation, returns normally
        (no exception is raised).

        Args:
            timeout: Maximum seconds to wait.

        Raises:
            CancelledError: If the token was cancelled during the wait.
        """
        cancelled = self._event.wait(timeout=timeout)
        if cancelled:
            raise CancelledError(self._reason)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "CancellationToken":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._cancelled:
            self.cancel("CancellationToken context exited")
        return None

    # ── Repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        status = "CANCELLED" if self._cancelled else "active"
        reason = f" reason={self._reason!r}" if self._cancelled else ""
        return f"<CancellationToken {status}{reason}>"
