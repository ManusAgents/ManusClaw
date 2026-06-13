"""
ManusClaw Event System — Base Classes
======================================

Provides the foundational event abstractions for the manusclaw agent framework,
inspired by OpenHands's event system and adapted for manusclaw's architecture.

Key design decisions:
  - Pydantic v2 with ``frozen=True`` for immutable, hashable events.
  - Discriminated union pattern via the ``kind`` field for type-safe polymorphism.
  - ``LLMConvertibleEvent`` base class for events that map to LLM message roles.
  - ``events_to_messages()`` static method supports batching parallel tool calls
    (multiple ActionEvents sharing the same ``llm_response_id``).

Thread safety: Event objects are immutable (frozen Pydantic models), so they are
safe to share across threads without additional synchronisation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# Source type — who/what produced the event
# ──────────────────────────────────────────────────────────────────────────────

SourceType = Literal["agent", "user", "environment", "hook"]
"""Identifies the origin of an event.

- ``agent``       : Produced by the LLM agent (e.g. a tool-call decision).
- ``user``        : Produced by the human operator (e.g. a chat message).
- ``environment`` : Produced by the runtime/sandbox (e.g. tool execution result).
- ``hook``        : Produced by a lifecycle hook (pre/post processing).
"""


# ──────────────────────────────────────────────────────────────────────────────
# Base Event
# ──────────────────────────────────────────────────────────────────────────────

class Event(BaseModel, frozen=True):
    """Root base class for all manusclaw events.

    Every event carries:
      - ``id``         : Unique identifier (auto-generated UUID4 if not supplied).
      - ``timestamp``  : UTC datetime when the event was created.
      - ``source``     : Who/what produced the event.
      - ``kind``       : Discriminator for the discriminated-union pattern.
                         Each concrete subclass **must** set its own literal value.

    The model is **frozen** (immutable) to guarantee safe sharing across threads
    and to prevent accidental mutation after creation.
    """

    model_config: ClassVar[dict[str, Any]] = {
        "frozen": True,
        "populate_by_name": True,
    }

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    source: SourceType = "agent"
    kind: str = Field(init=False)

    # Subclasses override this with a literal value that becomes the discriminator.
    # We set it here as a default; concrete types override via Field(default="...").
    # Pydantic will use the literal value for discriminated-union dispatch.


# ──────────────────────────────────────────────────────────────────────────────
# LLM-convertible Event
# ──────────────────────────────────────────────────────────────────────────────

class LLMConvertibleEvent(Event, frozen=True):
    """Base for events that can be converted into LLM-compatible message dicts.

    Subclasses must implement :meth:`to_llm_message` which returns a dict
    compatible with the OpenAI Chat Completion message schema:

    .. code-block:: python

        {
            "role": "system" | "user" | "assistant" | "tool",
            "content": "...",
            # optional keys: tool_calls, tool_call_id, name
        }

    The :meth:`events_to_messages` static method on this class handles the
    full conversion pipeline, including batching of parallel tool calls.
    """

    llm_response_id: Optional[str] = Field(
        default=None,
        description=(
            "Groups parallel tool calls from the same LLM response. "
            "All ActionEvents produced by a single LLM completion share "
            "the same llm_response_id so that events_to_messages() can "
            "batch them into one assistant message with multiple tool_calls."
        ),
    )

    def to_llm_message(self) -> dict[str, Any]:
        """Convert this event into an LLM-compatible message dict.

        Must be overridden by concrete subclasses.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement to_llm_message()"
        )

    # ──────────────────────────────────────────────────────────────────
    # Batch conversion
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def events_to_messages(
        events: list["LLMConvertibleEvent"],
    ) -> list[dict[str, Any]]:
        """Convert a sequence of events into LLM-compatible message dicts.

        This method handles the important case of **parallel tool calls**:
        when the LLM returns multiple tool calls in a single response, they
        are represented as separate ``ActionEvent`` instances sharing the same
        ``llm_response_id``.  This method batches them into a single assistant
        message with a ``tool_calls`` list, matching the OpenAI API format.

        Algorithm:
          1. Group consecutive ActionEvents with the same ``llm_response_id``
             into a single assistant message with ``tool_calls``.
          2. Each ObservationEvent becomes a ``role=tool`` message with the
             corresponding ``tool_call_id``.
          3. SystemPromptEvent → ``role=system``, MessageEvent → ``role=user``.

        Args:
            events: Ordered list of LLM-convertible events.

        Returns:
            List of dicts compatible with the OpenAI Chat Completion API.
        """
        messages: list[dict[str, Any]] = []
        i = 0

        while i < len(events):
            event = events[i]

            # ── Batched ActionEvents (parallel tool calls) ────────────
            if event.kind == "action" and event.llm_response_id is not None:
                batch: list[LLMConvertibleEvent] = []
                rid = event.llm_response_id
                # Collect all consecutive ActionEvents with the same response id
                while (
                    i < len(events)
                    and events[i].kind == "action"
                    and events[i].llm_response_id == rid
                ):
                    batch.append(events[i])
                    i += 1

                # Merge into a single assistant message with tool_calls
                tool_calls_list: list[dict[str, Any]] = []
                assistant_content: str | None = None

                for action in batch:
                    action_msg = action.to_llm_message()
                    if "tool_calls" in action_msg:
                        tool_calls_list.extend(action_msg["tool_calls"])
                    # Preserve any text content from the first action
                    if assistant_content is None and action_msg.get("content"):
                        assistant_content = action_msg["content"]

                merged: dict[str, Any] = {"role": "assistant"}
                if assistant_content is not None:
                    merged["content"] = assistant_content
                else:
                    merged["content"] = None
                if tool_calls_list:
                    merged["tool_calls"] = tool_calls_list
                messages.append(merged)
                continue

            # ── Single ActionEvent (no batching) ─────────────────────
            if event.kind == "action":
                messages.append(event.to_llm_message())
                i += 1
                continue

            # ── All other event types ────────────────────────────────
            messages.append(event.to_llm_message())
            i += 1

        return messages


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    """Return the current UTC datetime with timezone info."""
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())
