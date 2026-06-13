"""
Correlation ID System for Distributed Tracing
================================================

Provides request-scoped correlation IDs that propagate across async
boundaries, integrate with logging and tracing, and enable log-based
error lookup for 500 responses.

Architecture:
    - Uses ``contextvars`` for async-safe propagation
    - Generates short, URL-safe correlation IDs (8 chars)
    - error_id: a unique identifier for 500 errors that can be
      returned to the client and looked up in logs
    - Integrates with :mod:`app.observability.logging_utils` and
      :mod:`app.observability.tracing`

Usage::

    from app.observability.correlation import (
        get_correlation_id,
        set_correlation_id,
        new_correlation_id,
        get_error_id,
        CorrelationContext,
    )

    # In a request middleware
    cid = new_correlation_id()
    set_correlation_id(cid)

    # In error handlers
    error_id = get_error_id()
    # Return error_id to the client for support lookup
"""

from __future__ import annotations

import secrets
import threading
from contextvars import ContextVar, Token
from typing import Optional


# ---------------------------------------------------------------------------
# Context variables — async-safe via contextvars
# ---------------------------------------------------------------------------

_correlation_id: ContextVar[Optional[str]] = ContextVar(
    "manusclaw_correlation_id", default=None
)
_request_id: ContextVar[Optional[str]] = ContextVar(
    "manusclaw_request_id", default=None
)
_error_id: ContextVar[Optional[str]] = ContextVar(
    "manusclaw_error_id", default=None
)
_parent_span_id: ContextVar[Optional[str]] = ContextVar(
    "manusclaw_parent_span_id", default=None
)

# Thread-safe fallback for sync code that does not use contextvars
_thread_local = threading.local()


def _generate_short_id(length: int = 8) -> str:
    """Generate a URL-safe, short, cryptographically random ID."""
    return secrets.token_urlsafe(length * 3 // 4 + 1)[:length]


# ---------------------------------------------------------------------------
# Correlation ID
# ---------------------------------------------------------------------------

def new_correlation_id() -> str:
    """Generate a new correlation ID.

    Returns:
        A short, URL-safe random string (8 characters).
    """
    return _generate_short_id(8)


def get_correlation_id() -> Optional[str]:
    """Return the current correlation ID, or ``None`` if unset."""
    cid = _correlation_id.get()
    if cid is None:
        cid = getattr(_thread_local, "correlation_id", None)
    return cid


def set_correlation_id(cid: str) -> Token:
    """Set the correlation ID for the current async context.

    Args:
        cid: The correlation ID string to set.

    Returns:
        A contextvars Token that can be used to reset the value.
    """
    _thread_local.correlation_id = cid
    return _correlation_id.set(cid)


def reset_correlation_id(token: Token) -> None:
    """Reset the correlation ID to its previous value.

    Also clears the thread-local fallback so that ``get_correlation_id()``
    correctly returns ``None`` after the context manager exits.

    Args:
        token: The Token returned by :func:`set_correlation_id`.
    """
    _correlation_id.reset(token)
    # Clear thread-local fallback so it doesn't leak outside the context
    _thread_local.correlation_id = _correlation_id.get()


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------

def new_request_id() -> str:
    """Generate a new request ID (12 characters)."""
    return _generate_short_id(12)


def get_request_id() -> Optional[str]:
    """Return the current request ID, or ``None`` if unset."""
    return _request_id.get()


def set_request_id(rid: str) -> Token:
    """Set the request ID for the current async context.

    Args:
        rid: The request ID string to set.

    Returns:
        A contextvars Token for resetting.
    """
    return _request_id.set(rid)


def reset_request_id(token: Token) -> None:
    """Reset the request ID to its previous value."""
    _request_id.reset(token)


# ---------------------------------------------------------------------------
# Error ID — for 500 errors that need client-visible identifiers
# ---------------------------------------------------------------------------

def new_error_id() -> str:
    """Generate a new error ID (prefixed ``err-`` for easy log searching).

    Returns:
        A string like ``err-a8fK3bN2``.
    """
    return f"err-{_generate_short_id(8)}"


def get_error_id() -> Optional[str]:
    """Return the current error ID, or ``None`` if unset."""
    return _error_id.get()


def set_error_id(eid: str) -> Token:
    """Set the error ID for the current async context."""
    return _error_id.set(eid)


def reset_error_id(token: Token) -> None:
    """Reset the error ID to its previous value."""
    _error_id.reset(token)


# ---------------------------------------------------------------------------
# Parent Span ID — for trace context propagation
# ---------------------------------------------------------------------------

def get_parent_span_id() -> Optional[str]:
    """Return the parent span ID, or ``None`` if unset."""
    return _parent_span_id.get()


def set_parent_span_id(span_id: str) -> Token:
    """Set the parent span ID for the current async context."""
    return _parent_span_id.set(span_id)


def reset_parent_span_id(token: Token) -> None:
    """Reset the parent span ID to its previous value."""
    _parent_span_id.reset(token)


# ---------------------------------------------------------------------------
# Context manager — convenient for request scopes
# ---------------------------------------------------------------------------

class CorrelationContext:
    """Context manager that sets and automatically resets correlation IDs.

    Usage::

        with CorrelationContext(correlation_id="abc123", request_id="req-xyz"):
            # Inside: get_correlation_id() == "abc123"
            ...
        # Outside: get_correlation_id() is None

    Works with ``async with`` as well::

        async with CorrelationContext():
            await handle_request()
    """

    def __init__(
        self,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self._cid = correlation_id or new_correlation_id()
        self._rid = request_id or new_request_id()
        self._tokens: list[tuple[str, Token]] = []

    def __enter__(self) -> "CorrelationContext":
        self._tokens.append(("correlation_id", set_correlation_id(self._cid)))
        self._tokens.append(("request_id", set_request_id(self._rid)))
        return self

    def __exit__(self, *exc: object) -> None:
        self._reset()

    async def __aenter__(self) -> "CorrelationContext":
        self._tokens.append(("correlation_id", set_correlation_id(self._cid)))
        self._tokens.append(("request_id", set_request_id(self._rid)))
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._reset()

    def _reset(self) -> None:
        for name, token in reversed(self._tokens):
            if name == "correlation_id":
                reset_correlation_id(token)
            elif name == "request_id":
                reset_request_id(token)
        self._tokens.clear()

    @property
    def correlation_id(self) -> str:
        return self._cid

    @property
    def request_id(self) -> str:
        return self._rid


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def get_all_context_ids() -> dict[str, Optional[str]]:
    """Return a dict of all current context IDs.

    Useful for injecting into log records or trace attributes.
    """
    return {
        "correlation_id": get_correlation_id(),
        "request_id": get_request_id(),
        "error_id": get_error_id(),
        "parent_span_id": get_parent_span_id(),
    }
