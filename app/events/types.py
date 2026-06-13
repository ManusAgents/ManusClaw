"""
ManusClaw Event System — Concrete Event Types
===============================================

All event types used by the manusclaw agent runtime.  Each type sets a unique
``kind`` literal that serves as the discriminator in a discriminated union,
enabling type-safe pattern matching and serialization/deserialization.

Event taxonomy:

  LLM-convertible (feed into the LLM conversation context):
    SystemPromptEvent, MessageEvent, ActionEvent, ObservationEvent,
    UserRejectObservation

  Control-flow / lifecycle:
    PauseEvent, InterruptEvent, ResumeTranscriptEvent

  Context-management / condensation:
    CondensationEvent, CondensationRequestEvent

  Error / monitoring:
    AgentErrorEvent, ConversationErrorEvent, TokenEvent,
    LLMCompletionLogEvent, HookExecutionEvent

  Real-time streaming:
    StreamingDeltaEvent

  State sync:
    ConversationStateUpdateEvent
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.events.base import (
    Event,
    LLMConvertibleEvent,
    SourceType,
    _new_id,
    _utc_now,
)


# ──────────────────────────────────────────────────────────────────────────────
# Tool-call helpers (shared between ActionEvent and ObservationEvent)
# ──────────────────────────────────────────────────────────────────────────────

class FunctionCall(BaseModel, frozen=True):
    """Represents a single function call within a tool invocation."""
    name: str
    arguments: str  # JSON-encoded string


class ToolCallInfo(BaseModel, frozen=True):
    """Mirrors the OpenAI tool_call structure for LLM message conversion."""
    id: str = Field(default_factory=lambda: f"call_{_new_id()[:8]}")
    type: Literal["function"] = "function"
    function: FunctionCall


# ──────────────────────────────────────────────────────────────────────────────
# SystemPromptEvent
# ──────────────────────────────────────────────────────────────────────────────

class SystemPromptEvent(LLMConvertibleEvent, frozen=True):
    """Carries the system prompt injected at the start of a conversation.

    Maps to ``role=system`` in the LLM message format.
    """

    kind: Literal["system_prompt"] = "system_prompt"
    content: str
    source: SourceType = "agent"

    def to_llm_message(self) -> dict[str, Any]:
        return {"role": "system", "content": self.content}


# ──────────────────────────────────────────────────────────────────────────────
# MessageEvent
# ──────────────────────────────────────────────────────────────────────────────

class MessageEvent(LLMConvertibleEvent, frozen=True):
    """A text message from the user or agent.

    Maps to ``role=user`` (source=user) or ``role=assistant`` (source=agent)
    in the LLM message format.
    """

    kind: Literal["message"] = "message"
    content: str
    role: Literal["user", "assistant"] = "user"
    source: SourceType = "user"

    def to_llm_message(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


# ──────────────────────────────────────────────────────────────────────────────
# ActionEvent
# ──────────────────────────────────────────────────────────────────────────────

class ActionEvent(LLMConvertibleEvent, frozen=True):
    """Represents an agent action — typically a tool call.

    When multiple tool calls are issued in parallel (single LLM response),
    they share the same ``llm_response_id`` so that
    :meth:`LLMConvertibleEvent.events_to_messages` can batch them into one
    assistant message with a ``tool_calls`` list.

    Maps to ``role=assistant`` with ``tool_calls`` in the LLM message format.
    """

    kind: Literal["action"] = "action"
    tool_call: ToolCallInfo
    source: SourceType = "agent"
    # Text content the assistant emitted alongside the tool call (if any)
    content: Optional[str] = None

    def to_llm_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": self.tool_call.id,
                    "type": self.tool_call.type,
                    "function": {
                        "name": self.tool_call.function.name,
                        "arguments": self.tool_call.function.arguments,
                    },
                }
            ],
        }
        return msg


# ──────────────────────────────────────────────────────────────────────────────
# ObservationEvent
# ──────────────────────────────────────────────────────────────────────────────

class ObservationEvent(LLMConvertibleEvent, frozen=True):
    """The result of executing a tool / action.

    Maps to ``role=tool`` in the LLM message format, linking back to the
    originating ``ActionEvent`` via ``tool_call_id``.
    """

    kind: Literal["observation"] = "observation"
    tool_call_id: str
    tool_name: str
    content: str
    source: SourceType = "environment"
    # Whether the tool execution succeeded
    success: bool = True
    # Execution duration in milliseconds
    duration_ms: int = 0

    def to_llm_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
            "name": self.tool_name,
        }


# ──────────────────────────────────────────────────────────────────────────────
# UserRejectObservation
# ──────────────────────────────────────────────────────────────────────────────

class UserRejectObservation(LLMConvertibleEvent, frozen=True):
    """The user explicitly rejected the result of a tool execution.

    This is the human-in-the-loop mechanism: when a tool result requires
    approval and the user declines, this event is emitted instead of a
    standard ObservationEvent.

    Maps to ``role=tool`` with a rejection notice.
    """

    kind: Literal["user_reject_observation"] = "user_reject_observation"
    tool_call_id: str
    tool_name: str
    reason: str = "User rejected the tool result."
    source: SourceType = "user"

    def to_llm_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": f"REJECTED: {self.reason}",
            "name": self.tool_name,
        }


# ──────────────────────────────────────────────────────────────────────────────
# CondensationEvent
# ──────────────────────────────────────────────────────────────────────────────

class CondensationEvent(Event, frozen=True):
    """A condensation (context-window compression) has occurred.

    When the conversation grows too long for the model's context window,
    a condensation strategy summarizes earlier events.  This event records
    which events were forgotten and what summary replaced them.

    This is **not** LLM-convertible — it is metadata for the event log
    and UI to understand context-window management.
    """

    kind: Literal["condensation"] = "condensation"
    # IDs of events that were removed/condensed
    forgotten_event_ids: list[str] = Field(default_factory=list)
    # Summary that replaces the forgotten events
    summary: str = ""
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# CondensationRequestEvent
# ──────────────────────────────────────────────────────────────────────────────

class CondensationRequestEvent(Event, frozen=True):
    """Request from the runtime to condense the conversation.

    Emitted when the token tracker detects that the context window is
    approaching its limit and the condensation policy should run.
    """

    kind: Literal["condensation_request"] = "condensation_request"
    current_token_count: int = 0
    max_token_count: int = 0
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# AgentErrorEvent
# ──────────────────────────────────────────────────────────────────────────────

class AgentErrorEvent(Event, frozen=True):
    """An error occurred within the agent loop.

    Captures the error message and optional exception details.  This is
    **not** LLM-convertible — it is for observability and error handling.
    """

    kind: Literal["agent_error"] = "agent_error"
    error: str
    error_type: str = "RuntimeError"
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# TokenEvent
# ──────────────────────────────────────────────────────────────────────────────

class TokenEvent(Event, frozen=True):
    """Token usage tracking event.

    Emitted after each LLM call to record prompt/completion token counts
    for budget enforcement and observability.
    """

    kind: Literal["token"] = "token"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# InterruptEvent
# ──────────────────────────────────────────────────────────────────────────────

class InterruptEvent(Event, frozen=True):
    """The agent run was interrupted (e.g. by a signal or user request).

    Unlike PauseEvent, an interrupt may not be resumable.
    """

    kind: Literal["interrupt"] = "interrupt"
    reason: str = "Interrupted by user."
    source: SourceType = "user"


# ──────────────────────────────────────────────────────────────────────────────
# PauseEvent
# ──────────────────────────────────────────────────────────────────────────────

class PauseEvent(Event, frozen=True):
    """The agent run was paused and can be resumed later.

    Emitted when the user or a hook requests a pause.  A subsequent
    ResumeTranscriptEvent will signal continuation.
    """

    kind: Literal["pause"] = "pause"
    reason: str = "Paused by user."
    source: SourceType = "user"


# ──────────────────────────────────────────────────────────────────────────────
# ConversationErrorEvent
# ──────────────────────────────────────────────────────────────────────────────

class ConversationErrorEvent(Event, frozen=True):
    """An error at the conversation/protocol level.

    Distinct from AgentErrorEvent: this represents errors in the
    conversation protocol itself (e.g. malformed messages, invalid
    state transitions) rather than errors in the agent logic.
    """

    kind: Literal["conversation_error"] = "conversation_error"
    error: str
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# ConversationStateUpdateEvent
# ──────────────────────────────────────────────────────────────────────────────

class ConversationStateUpdateEvent(Event, frozen=True):
    """The conversation's runtime state has changed.

    Used to synchronize UI and external consumers with the current
    conversation state (e.g. IDLE → RUNNING → FINISHED).
    """

    kind: Literal["conversation_state_update"] = "conversation_state_update"
    old_state: str = "IDLE"
    new_state: str = "RUNNING"
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# LLMCompletionLogEvent
# ──────────────────────────────────────────────────────────────────────────────

class LLMCompletionLogEvent(Event, frozen=True):
    """Detailed log of an LLM API completion call.

    Captures the raw request/response for audit, debugging, and replay.
    This is **not** LLM-convertible — it is purely for observability.
    """

    kind: Literal["llm_completion_log"] = "llm_completion_log"
    model: str = ""
    prompt_messages: int = 0
    completion_id: str = ""
    finish_reason: str = ""
    latency_ms: int = 0
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# HookExecutionEvent
# ──────────────────────────────────────────────────────────────────────────────

class HookExecutionEvent(Event, frozen=True):
    """A lifecycle hook was executed.

    Records which hook ran, its trigger point, and the outcome.  Useful
    for debugging hook pipelines and measuring hook latency.
    """

    kind: Literal["hook_execution"] = "hook_execution"
    hook_name: str
    trigger: str  # e.g. "pre_action", "post_observation", "on_error"
    success: bool = True
    duration_ms: int = 0
    source: SourceType = "hook"


# ──────────────────────────────────────────────────────────────────────────────
# StreamingDeltaEvent
# ──────────────────────────────────────────────────────────────────────────────

class StreamingDeltaEvent(Event, frozen=True):
    """A streaming delta from the LLM response.

    Emitted during streaming to push partial content to the UI in
    real-time.  Not persisted in the event log by default.
    """

    kind: Literal["streaming_delta"] = "streaming_delta"
    delta: str
    completion_id: str = ""
    source: SourceType = "agent"


# ──────────────────────────────────────────────────────────────────────────────
# ResumeTranscriptEvent
# ──────────────────────────────────────────────────────────────────────────────

class ResumeTranscriptEvent(Event, frozen=True):
    """A paused conversation has been resumed.

    Contains the ID of the PauseEvent that is being resumed, enabling
    consumers to pair pause/resume events.
    """

    kind: Literal["resume_transcript"] = "resume_transcript"
    pause_event_id: str = ""
    reason: str = "Resumed by user."
    source: SourceType = "user"


# ──────────────────────────────────────────────────────────────────────────────
# Discriminated union type — the canonical union of all event types
# ──────────────────────────────────────────────────────────────────────────────

EventUnion = Union[
    SystemPromptEvent,
    MessageEvent,
    ActionEvent,
    ObservationEvent,
    UserRejectObservation,
    CondensationEvent,
    CondensationRequestEvent,
    AgentErrorEvent,
    TokenEvent,
    InterruptEvent,
    PauseEvent,
    ConversationErrorEvent,
    ConversationStateUpdateEvent,
    LLMCompletionLogEvent,
    HookExecutionEvent,
    StreamingDeltaEvent,
    ResumeTranscriptEvent,
]
"""Discriminated union of all event types.

Usage with Pydantic's discriminated union::

    from pydantic import TypeAdapter
    from app.events.types import EventUnion

    adapter = TypeAdapter(Annotated[EventUnion, Field(discriminator="kind")])
    event = adapter.validate_json(payload)
"""

# Mapping from kind literal → concrete event class (for deserialization)
KIND_TO_EVENT: dict[str, type[Event]] = {
    "system_prompt": SystemPromptEvent,
    "message": MessageEvent,
    "action": ActionEvent,
    "observation": ObservationEvent,
    "user_reject_observation": UserRejectObservation,
    "condensation": CondensationEvent,
    "condensation_request": CondensationRequestEvent,
    "agent_error": AgentErrorEvent,
    "token": TokenEvent,
    "interrupt": InterruptEvent,
    "pause": PauseEvent,
    "conversation_error": ConversationErrorEvent,
    "conversation_state_update": ConversationStateUpdateEvent,
    "llm_completion_log": LLMCompletionLogEvent,
    "hook_execution": HookExecutionEvent,
    "streaming_delta": StreamingDeltaEvent,
    "resume_transcript": ResumeTranscriptEvent,
}
