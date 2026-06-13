"""
ManusClaw Hooks System
=======================
OpenHands-style hook system adapted for manusclaw's PAORR agent loop.

Quick start::

    from app.hooks import HookManager, HookEventType, HookContext, HookResult
    from app.hooks import LoggingHook, SecurityHook, AuditHook

    manager = HookManager()
    manager.register(LoggingHook())
    manager.register(SecurityHook())
    manager.register(AuditHook())
    await manager.initialize()

    # In the agent loop:
    result = await manager.emit_pre_tool_use(
        tool_name="bash",
        tool_args={"command": "ls"},
        conversation_id="abc123",
    )
    if result.is_denied:
        # Tool execution blocked by a hook
        ...

Modules:
    types   — HookEventType, HookDecision, HookContext, HookResult, HookError
    base    — HookBase ABC (abstract base class for all hooks)
    manager — HookManager (registry + async executor + metrics)
    builtin — LoggingHook, SecurityHook, AuditHook
    loader  — HookLoader (YAML / Python module loading)
"""

from __future__ import annotations

# ── Core types ────────────────────────────────────────────────────────────────
from app.hooks.types import (
    HookContext,
    HookDecision,
    HookError,
    HookEventType,
    HookResult,
    HookTimeoutError,
)

# ── Abstract base ─────────────────────────────────────────────────────────────
from app.hooks.base import HookBase

# ── Manager ───────────────────────────────────────────────────────────────────
from app.hooks.manager import (
    AggregateHookResult,
    HookManager,
    HookMetrics,
)

# ── Built-in hooks ────────────────────────────────────────────────────────────
from app.hooks.builtin import AuditHook, LoggingHook, SecurityHook

# ── Loader ────────────────────────────────────────────────────────────────────
from app.hooks.loader import BUILTIN_HOOKS, HookLoader

__all__ = [
    # Types
    "HookEventType",
    "HookDecision",
    "HookContext",
    "HookResult",
    "HookError",
    "HookTimeoutError",
    # Base
    "HookBase",
    # Manager
    "HookManager",
    "AggregateHookResult",
    "HookMetrics",
    # Built-in hooks
    "LoggingHook",
    "SecurityHook",
    "AuditHook",
    # Loader
    "HookLoader",
    "BUILTIN_HOOKS",
]
