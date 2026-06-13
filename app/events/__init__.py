"""
ManusClaw Event System
=======================

A comprehensive, OpenHands-inspired event system adapted for manusclaw's
architecture.  Provides type-safe, immutable events with discriminated-union
dispatch, file-backed persistence, and LLM message conversion.

Quick start::

    from app.events import (
        Event, LLMConvertibleEvent, SourceType,
        SystemPromptEvent, MessageEvent, ActionEvent, ObservationEvent,
        EventLog, serialize, deserialize, events_to_messages,
    )

    # Create events
    prompt = SystemPromptEvent(content="You are a helpful assistant.")
    user_msg = MessageEvent(content="Hello!", role="user")
    action = ActionEvent(tool_call=ToolCallInfo(
        function=FunctionCall(name="bash", arguments='{"command":"ls"}')
    ))

    # Persist events
    log = EventLog("/tmp/my_session_events")
    log.append(prompt)
    log.append(user_msg)
    log.append(action)

    # Convert to LLM messages
    messages = events_to_messages([prompt, user_msg, action])

    # Serialize / deserialize
    json_str = serialize(prompt)
    restored = deserialize(json_str)
"""

from app.events.base import (
    Event,
    LLMConvertibleEvent,
    SourceType,
    _new_id,
    _utc_now,
)

from app.events.types import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    CondensationRequestEvent,
    ConversationErrorEvent,
    ConversationStateUpdateEvent,
    EventUnion,
    FunctionCall,
    HookExecutionEvent,
    InterruptEvent,
    KIND_TO_EVENT,
    LLMCompletionLogEvent,
    MessageEvent,
    ObservationEvent,
    PauseEvent,
    ResumeTranscriptEvent,
    StreamingDeltaEvent,
    SystemPromptEvent,
    TokenEvent,
    ToolCallInfo,
    UserRejectObservation,
)

from app.events.serialization import (
    DeserializationError,
    SerializationError,
    adapter_validate,
    deserialize,
    deserialize_batch,
    serialize,
    serialize_batch,
    serialize_to_dict,
)

from app.events.event_log import (
    EventLog,
    EventLogError,
    EventLogMetrics,
)

# Convenience re-export of the static method as a module-level function
events_to_messages = LLMConvertibleEvent.events_to_messages


__all__ = [
    # Base
    "Event",
    "LLMConvertibleEvent",
    "SourceType",
    "_new_id",
    "_utc_now",
    # Types
    "ActionEvent",
    "AgentErrorEvent",
    "CondensationEvent",
    "CondensationRequestEvent",
    "ConversationErrorEvent",
    "ConversationStateUpdateEvent",
    "EventUnion",
    "FunctionCall",
    "HookExecutionEvent",
    "InterruptEvent",
    "KIND_TO_EVENT",
    "LLMCompletionLogEvent",
    "MessageEvent",
    "ObservationEvent",
    "PauseEvent",
    "ResumeTranscriptEvent",
    "StreamingDeltaEvent",
    "SystemPromptEvent",
    "TokenEvent",
    "ToolCallInfo",
    "UserRejectObservation",
    # Serialization
    "DeserializationError",
    "SerializationError",
    "adapter_validate",
    "deserialize",
    "deserialize_batch",
    "serialize",
    "serialize_batch",
    "serialize_to_dict",
    # Event log
    "EventLog",
    "EventLogError",
    "EventLogMetrics",
    # Convenience
    "events_to_messages",
]
