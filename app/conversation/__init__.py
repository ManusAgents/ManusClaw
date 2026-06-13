"""
ManusClaw Conversation System
================================

A comprehensive, OpenHands-inspired conversation system adapted for
manusclaw's agent architecture.  Provides local and remote conversation
implementations with full lifecycle management, cancellation, forking,
stuck detection, and security integration.

Quick start::

    from app.conversation import ConversationFactory, LocalConversation

    # Create a local conversation
    conv = ConversationFactory.create(mode="local")

    # Or directly
    conv = LocalConversation()

    # Run an agent
    result = await conv.arun("Build a web scraper")

    # Check for stuck patterns
    report = conv.check_stuck()
    if report.is_stuck:
        print(f"Stuck: {report.pattern}")

    # Fork a conversation
    forked = conv.fork()

Modules:
    cancellation_token  — CancellationToken for graceful shutdown
    fifo_lock           — FIFOLock and AsyncFIFOLock for fair concurrent access
    state               — ConversationState (execution status, events, security)
    stuck_detector      — 5-pattern StuckDetector
    base                — BaseConversation ABC
    local_conversation  — LocalConversation with full lifecycle
    remote_conversation — RemoteConversation via WebSocket
    factory             — ConversationFactory (local vs remote based on config)
"""

from __future__ import annotations

# ── Cancellation ──────────────────────────────────────────────────────────────

from app.conversation.cancellation_token import (
    CancelledError,
    CancellationToken,
)

# ── FIFO Locks ───────────────────────────────────────────────────────────────

from app.conversation.fifo_lock import (
    AsyncFIFOLock,
    FIFOLock,
)

# ── State ─────────────────────────────────────────────────────────────────────

from app.conversation.state import (
    ConversationState,
    ExecutionStatus,
    SecretRegistry,
)

# ── Stuck Detection ──────────────────────────────────────────────────────────

from app.conversation.stuck_detector import (
    StuckDetector,
    StuckPattern,
    StuckReport,
)

# ── Base Conversation ────────────────────────────────────────────────────────

from app.conversation.base import BaseConversation

# ── Concrete Conversations ───────────────────────────────────────────────────

from app.conversation.local_conversation import LocalConversation
from app.conversation.remote_conversation import (
    ConnectionState,
    RemoteConversation,
)

# ── Factory ───────────────────────────────────────────────────────────────────

from app.conversation.factory import (
    ConversationConfig,
    ConversationFactory,
)

__all__ = [
    # Cancellation
    "CancelledError",
    "CancellationToken",
    # FIFO Locks
    "AsyncFIFOLock",
    "FIFOLock",
    # State
    "ConversationState",
    "ExecutionStatus",
    "SecretRegistry",
    # Stuck Detection
    "StuckDetector",
    "StuckPattern",
    "StuckReport",
    # Base
    "BaseConversation",
    # Concrete
    "LocalConversation",
    "RemoteConversation",
    "ConnectionState",
    # Factory
    "ConversationConfig",
    "ConversationFactory",
]
