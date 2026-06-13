from __future__ import annotations

"""
ManusClaw Hooks — Type Definitions
====================================
Core types for the hook system, inspired by OpenHands's hook architecture
but adapted for manusclaw's PAORR agent loop.

Hook Event Flow:
    SESSION_START → [USER_PROMPT_SUBMIT → PRE_TOOL_USE → tool exec → POST_TOOL_USE]* → STOP → SESSION_END

Decisions:
    ALLOW   — Proceed without modification
    DENY    — Block the action (tool execution, user prompt, or agent stop)
    MODIFY  — Replace or augment the content (user prompts only)
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# Event Types
# ──────────────────────────────────────────────────────────────────────────────

class HookEventType(str, Enum):
    """
    Lifecycle events that hooks can subscribe to.

    Events are emitted in the following order during a typical agent run:

        SESSION_START        — Fired once when a new agent session begins.
        USER_PROMPT_SUBMIT   — Fired before a user prompt enters the agent loop.
        PRE_TOOL_USE         — Fired before a tool is executed. Hooks can DENY.
        POST_TOOL_USE        — Fired after a tool returns. Observation only.
        STOP                 — Fired when the agent is about to stop. Hooks can DENY.
        SESSION_END          — Fired once when the session terminates.
    """

    SESSION_START      = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE       = "pre_tool_use"
    POST_TOOL_USE      = "post_tool_use"
    STOP               = "stop"
    SESSION_END        = "session_end"


# ──────────────────────────────────────────────────────────────────────────────
# Hook Decision
# ──────────────────────────────────────────────────────────────────────────────

class HookDecision(str, Enum):
    """
    Decision returned by a hook after processing an event.

    ALLOW   — No objection; the action proceeds as-is.
    DENY    — The action must be blocked. The caller should abort the pending
              operation and surface the hook's reason to the user / agent.
    MODIFY  — The hook has rewritten the content (only valid for
              USER_PROMPT_SUBMIT events). The caller should use
              HookResult.modified_content instead of the original.
    """

    ALLOW  = "allow"
    DENY   = "deny"
    MODIFY = "modify"


# ──────────────────────────────────────────────────────────────────────────────
# Hook Context
# ──────────────────────────────────────────────────────────────────────────────

class HookContext(BaseModel):
    """
    Rich context object passed to every hook invocation.

    Fields are populated based on the event type:
        - All events:        event_type, conversation_id, agent_name, source
        - PRE_TOOL_USE:      + tool_name, tool_args
        - POST_TOOL_USE:     + tool_name, tool_args, tool_result
        - USER_PROMPT_SUBMIT: + user_message
        - SESSION_START:     (base fields only)
        - SESSION_END:       (base fields only)
        - STOP:              (base fields only)
    """

    # ── Identifiers ──────────────────────────────────────────────────────
    event_type:      HookEventType
    conversation_id: str                       = ""
    agent_name:      str                       = ""
    source:          str                       = "agent"   # "agent" | "user" | "system"

    # ── Tool-specific (PRE_TOOL_USE / POST_TOOL_USE) ─────────────────────
    tool_name:       Optional[str]             = None
    tool_args:       Optional[dict[str, Any]]  = None
    tool_result:     Optional[Any]             = None      # Only in POST_TOOL_USE

    # ── User prompt (USER_PROMPT_SUBMIT) ─────────────────────────────────
    user_message:    Optional[str]             = None

    # ── Extensible metadata ──────────────────────────────────────────────
    extra:           dict[str, Any]            = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


# ──────────────────────────────────────────────────────────────────────────────
# Hook Result
# ──────────────────────────────────────────────────────────────────────────────

class HookResult(BaseModel):
    """
    Value returned by a hook after processing an event.

    decision:          The hook's verdict. Defaults to ALLOW.
    modified_content:  Replacement content when decision is MODIFY.
    reason:            Human-readable explanation, especially for DENY.
    metadata:          Arbitrary key-value data for diagnostics / audit.
    """

    decision:         HookDecision         = HookDecision.ALLOW
    modified_content: Optional[str]        = None
    reason:           str                  = ""
    metadata:         dict[str, Any]       = Field(default_factory=dict)

    # ── Convenience constructors ─────────────────────────────────────────

    @classmethod
    def allow(cls, reason: str = "", **metadata: Any) -> "HookResult":
        """Create an ALLOW result."""
        return cls(decision=HookDecision.ALLOW, reason=reason, metadata=metadata)

    @classmethod
    def deny(cls, reason: str, **metadata: Any) -> "HookResult":
        """Create a DENY result with a mandatory reason."""
        return cls(decision=HookDecision.DENY, reason=reason, metadata=metadata)

    @classmethod
    def modify(cls, modified_content: str, reason: str = "", **metadata: Any) -> "HookResult":
        """Create a MODIFY result with replacement content."""
        return cls(
            decision=HookDecision.MODIFY,
            modified_content=modified_content,
            reason=reason,
            metadata=metadata,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Hook Execution Error
# ──────────────────────────────────────────────────────────────────────────────

class HookError(Exception):
    """Raised when a hook fails during execution (for internal bookkeeping)."""

    def __init__(self, hook_name: str, event_type: HookEventType, message: str) -> None:
        self.hook_name = hook_name
        self.event_type = event_type
        super().__init__(f"Hook '{hook_name}' failed on {event_type.value}: {message}")


class HookTimeoutError(HookError):
    """Raised when a hook exceeds its configured timeout."""

    def __init__(self, hook_name: str, event_type: HookEventType, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(
            hook_name, event_type,
            f"Timed out after {timeout_s:.1f}s",
        )
