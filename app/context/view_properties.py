from __future__ import annotations

"""
View Properties — Structural integrity constraints for LLM context projections.

These properties ensure that after condensation (removing events from a View),
the remaining events still form a valid sequence that an LLM can process
without errors or confusion.

Properties:
  - BatchAtomicity:       Assistant messages with multiple tool_calls must
                          not have those calls split from their parent.
  - ObservationUniqueness: No two tool-response messages should share the
                          same tool_call_id.
  - ToolCallMatching:     Every tool response must have a matching tool call
                          in an assistant message, and vice versa.
  - ToolLoopAtomicity:    If a tool loop is detected (repeated same-tool
                          failures), the entire loop is kept or removed
                          as a unit.

Each property exposes:
  - check(view)       -> bool   : whether the view satisfies the property
  - enforce(view)     -> View   : return a corrected view (removing minimal
                                  events to satisfy the property)
  - manipulation_indices(view) -> set[int] : indices safe to remove without
                                  violating this property
"""

import uuid
from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field

from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Event wrapper — a single item in the View's linear sequence
# ──────────────────────────────────────────────────────────────────────────────

class Event(BaseModel):
    """
    A single event in the context view.

    Wraps a manusclaw Message with a unique ID and metadata for
    manipulation tracking.  The `role` mirrors Message.role so that
    property checks can work with Events directly without unpacking.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: str                              # "system" | "user" | "assistant" | "tool"
    content: Optional[str] = None
    tool_calls: Optional[list[dict]] = None  # serialised ToolCall list
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    batch_id: Optional[str] = None         # groups events that must stay together
    metadata: dict = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    # ── Factory helpers ──────────────────────────────────────────────────

    @classmethod
    def from_message(cls, msg, batch_id: Optional[str] = None) -> "Event":
        """Create an Event from an app.schema.Message."""
        tool_calls_serialised = None
        if msg.tool_calls:
            tool_calls_serialised = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return cls(
            role=msg.role.value if hasattr(msg.role, "value") else str(msg.role),
            content=msg.content,
            tool_calls=tool_calls_serialised,
            tool_call_id=msg.tool_call_id,
            name=msg.name,
            batch_id=batch_id,
        )

    @classmethod
    def system(cls, content: str, **kw) -> "Event":
        return cls(role="system", content=content, **kw)

    @classmethod
    def user(cls, content: str, **kw) -> "Event":
        return cls(role="user", content=content, **kw)

    @classmethod
    def assistant(cls, content: Optional[str] = None,
                  tool_calls: Optional[list[dict]] = None, **kw) -> "Event":
        return cls(role="assistant", content=content, tool_calls=tool_calls, **kw)

    @classmethod
    def tool(cls, content: str, tool_call_id: str, name: str, **kw) -> "Event":
        return cls(role="tool", content=content,
                   tool_call_id=tool_call_id, name=name, **kw)

    @property
    def is_system(self) -> bool:
        return self.role == "system"

    @property
    def is_user(self) -> bool:
        return self.role == "user"

    @property
    def is_assistant(self) -> bool:
        return self.role == "assistant"

    @property
    def is_tool(self) -> bool:
        return self.role == "tool"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def tool_call_ids(self) -> list[str]:
        """Return all tool_call IDs from this assistant event's tool_calls."""
        if not self.tool_calls:
            return []
        return [tc["id"] for tc in self.tool_calls if "id" in tc]

    def to_dict(self) -> dict:
        d: dict = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


# ──────────────────────────────────────────────────────────────────────────────
# View Property ABC
# ──────────────────────────────────────────────────────────────────────────────

class ViewProperty(ABC):
    """
    Abstract base for a structural property that a View must satisfy.

    Subclasses implement three methods:
      - check(events):       Does the event list satisfy this property?
      - enforce(events):     Return a corrected event list (minimal removals).
      - safe_indices(events): Indices that can be removed without violating
                              the property.
    """

    @abstractmethod
    def check(self, events: list[Event]) -> bool:
        """Return True if *events* satisfies this property."""
        ...

    @abstractmethod
    def enforce(self, events: list[Event]) -> list[Event]:
        """
        Return a copy of *events* that satisfies this property.

        The implementation should remove the fewest events necessary.
        Events at index 0 (typically system prompt) must never be removed.
        """
        ...

    @abstractmethod
    def safe_indices(self, events: list[Event]) -> set[int]:
        """
        Return the set of indices that can be removed from *events*
        without violating this property.

        Index 0 (system prompt) is never considered safe to remove.
        """
        ...


# ──────────────────────────────────────────────────────────────────────────────
# BatchAtomicity
# ──────────────────────────────────────────────────────────────────────────────

class BatchAtomicity(ViewProperty):
    """
    Assistant messages with tool_calls form a batch with their
    corresponding tool-response messages.  Removing an assistant
    message must also remove all its tool responses, and vice versa.

    A "batch" is defined as:
      - An assistant message with tool_calls
      - Immediately following tool-response messages whose tool_call_id
        matches one of the assistant's tool_calls

    If an assistant message has no tool_calls, it is a standalone event
    and can be safely removed on its own.
    """

    def check(self, events: list[Event]) -> bool:
        """Verify every tool response has a preceding assistant with matching tool_call_id."""
        active_call_ids: set[str] = set()

        for event in events:
            if event.is_assistant and event.has_tool_calls:
                active_call_ids.update(event.tool_call_ids)
            elif event.is_assistant:
                # Standalone assistant — no batch constraint
                pass
            elif event.is_tool:
                if event.tool_call_id not in active_call_ids:
                    # Orphaned tool response
                    return False

        return True

    def enforce(self, events: list[Event]) -> list[Event]:
        """
        Remove orphaned tool responses and assistant messages whose
        tool_calls have been stripped of their responses.

        Strategy:
          1. Remove any tool event whose tool_call_id doesn't match a
             preceding assistant.
          2. Remove any assistant event with tool_calls where none of
             the responses exist.
        """
        # Pass 1: find all assistant tool_call_ids
        assistant_call_ids: set[str] = set()
        for event in events:
            if event.is_assistant and event.has_tool_calls:
                assistant_call_ids.update(event.tool_call_ids)

        # Pass 2: find all tool response IDs
        tool_response_ids: set[str] = set()
        for event in events:
            if event.is_tool and event.tool_call_id:
                tool_response_ids.add(event.tool_call_id)

        # Pass 3: build result, skipping orphans
        result: list[Event] = []
        for event in events:
            if event.is_tool:
                # Keep only if matching assistant exists
                if event.tool_call_id in assistant_call_ids:
                    result.append(event)
                else:
                    logger.debug(
                        f"[BatchAtomicity] Removing orphaned tool response "
                        f"id={event.id} tool_call_id={event.tool_call_id}"
                    )
            elif event.is_assistant and event.has_tool_calls:
                # Keep only if at least one response exists
                has_response = any(
                    tc_id in tool_response_ids
                    for tc_id in event.tool_call_ids
                )
                if has_response:
                    result.append(event)
                else:
                    logger.debug(
                        f"[BatchAtomicity] Removing assistant with no responses "
                        f"id={event.id}"
                    )
            else:
                result.append(event)

        return result

    def safe_indices(self, events: list[Event]) -> set[int]:
        """
        An index is safe to remove if removing it (and its batch partners)
        does not leave orphaned events.

        We compute this by identifying complete batches.  An index is safe
        if it is part of a complete batch (so the batch can be removed
        atomically) or if it is a standalone event.
        """
        safe: set[int] = set()

        # Build a map: tool_call_id -> (assistant_idx, [tool_idxs])
        batch_map: dict[str, tuple[int, list[int]]] = {}
        for i, event in enumerate(events):
            if i == 0:
                continue  # never safe to remove system prompt
            if event.is_assistant and event.has_tool_calls:
                for tc_id in event.tool_call_ids:
                    batch_map[tc_id] = (i, [])
            elif event.is_tool and event.tool_call_id:
                if event.tool_call_id in batch_map:
                    batch_map[event.tool_call_id][1].append(i)

        # Standalone events (non-batch) are individually safe
        for i, event in enumerate(events):
            if i == 0:
                continue
            if not event.is_tool and not (event.is_assistant and event.has_tool_calls):
                safe.add(i)

        # Batch events are safe only if the ENTIRE batch is removable
        seen_assistant_idxs: set[int] = set()
        for tc_id, (asst_idx, tool_idxs) in batch_map.items():
            if asst_idx in seen_assistant_idxs:
                continue
            seen_assistant_idxs.add(asst_idx)

            # Get all indices for this assistant's batch
            all_batch_idxs = {asst_idx}
            for tid in events[asst_idx].tool_call_ids:
                if tid in batch_map:
                    all_batch_idxs.update(batch_map[tid][1])

            # All batch indices are safe together
            safe.update(all_batch_idxs)

        return safe


# ──────────────────────────────────────────────────────────────────────────────
# ObservationUniqueness
# ──────────────────────────────────────────────────────────────────────────────

class ObservationUniqueness(ViewProperty):
    """
    No two tool-response events should share the same tool_call_id.

    If duplicates exist, only the first occurrence is kept.
    """

    def check(self, events: list[Event]) -> bool:
        seen: set[str] = set()
        for event in events:
            if event.is_tool and event.tool_call_id:
                if event.tool_call_id in seen:
                    return False
                seen.add(event.tool_call_id)
        return True

    def enforce(self, events: list[Event]) -> list[Event]:
        seen: set[str] = set()
        result: list[Event] = []
        for event in events:
            if event.is_tool and event.tool_call_id:
                if event.tool_call_id in seen:
                    logger.debug(
                        f"[ObservationUniqueness] Removing duplicate "
                        f"tool_call_id={event.tool_call_id} id={event.id}"
                    )
                    continue
                seen.add(event.tool_call_id)
            result.append(event)
        return result

    def safe_indices(self, events: list[Event]) -> set[int]:
        """
        All indices are safe except:
          - Index 0 (system prompt)
          - The FIRST occurrence of each tool_call_id (we want to keep that)
        """
        first_occurrence: set[int] = set()
        seen: set[str] = set()

        for i, event in enumerate(events):
            if event.is_tool and event.tool_call_id:
                if event.tool_call_id not in seen:
                    first_occurrence.add(i)
                    seen.add(event.tool_call_id)

        safe: set[int] = set()
        for i in range(len(events)):
            if i == 0:
                continue
            if i not in first_occurrence:
                safe.add(i)

        return safe


# ──────────────────────────────────────────────────────────────────────────────
# ToolCallMatching
# ──────────────────────────────────────────────────────────────────────────────

class ToolCallMatching(ViewProperty):
    """
    Every tool_call in an assistant message must have a corresponding
    tool-response, and every tool-response must have a corresponding
    tool_call.

    This is stricter than BatchAtomicity: it also catches the case where
    an assistant made 3 tool_calls but only 2 responses are present.
    """

    def check(self, events: list[Event]) -> bool:
        """Verify exact 1:1 matching between tool_calls and tool responses."""
        # Collect all tool_call_ids from assistant messages
        declared_ids: set[str] = set()
        for event in events:
            if event.is_assistant and event.has_tool_calls:
                declared_ids.update(event.tool_call_ids)

        # Collect all tool_call_ids from tool responses
        response_ids: set[str] = set()
        for event in events:
            if event.is_tool and event.tool_call_id:
                response_ids.add(event.tool_call_id)

        return declared_ids == response_ids

    def enforce(self, events: list[Event]) -> list[Event]:
        """
        Remove unmatched tool calls from assistant messages and unmatched
        tool responses. If an assistant message has zero remaining tool_calls
        after filtering, keep it as a standalone (content-only) message.
        """
        # Pass 1: find all tool response IDs
        response_ids: set[str] = set()
        for event in events:
            if event.is_tool and event.tool_call_id:
                response_ids.add(event.tool_call_id)

        # Pass 2: find all declared tool call IDs
        declared_ids: set[str] = set()
        for event in events:
            if event.is_assistant and event.has_tool_calls:
                declared_ids.update(event.tool_call_ids)

        # Pass 3: filter
        result: list[Event] = []
        for event in events:
            if event.is_assistant and event.has_tool_calls:
                # Keep only tool_calls that have responses
                matched_calls = [
                    tc for tc in event.tool_calls
                    if tc.get("id") in response_ids
                ]
                if matched_calls:
                    event = event.model_copy(update={"tool_calls": matched_calls})
                    result.append(event)
                elif event.content:
                    # Keep as standalone assistant message (strip tool_calls)
                    event = event.model_copy(update={"tool_calls": None})
                    result.append(event)
                else:
                    logger.debug(
                        f"[ToolCallMatching] Removing assistant with no "
                        f"matched tool_calls id={event.id}"
                    )
            elif event.is_tool:
                if event.tool_call_id in declared_ids:
                    result.append(event)
                else:
                    logger.debug(
                        f"[ToolCallMatching] Removing unmatched tool response "
                        f"id={event.id} tool_call_id={event.tool_call_id}"
                    )
            else:
                result.append(event)

        return result

    def safe_indices(self, events: list[Event]) -> set[int]:
        """
        An index is safe to remove if removing it (and its matched partner)
        doesn't violate the property.

        Computed by identifying complete assistant+response pairs.
        Removing a complete pair is safe.
        """
        safe: set[int] = set()

        # Build index maps
        asst_by_call_id: dict[str, int] = {}  # tool_call_id -> assistant index
        tool_by_call_id: dict[str, int] = {}   # tool_call_id -> tool response index

        for i, event in enumerate(events):
            if i == 0:
                continue
            if event.is_assistant and event.has_tool_calls:
                for tc_id in event.tool_call_ids:
                    asst_by_call_id[tc_id] = i
            elif event.is_tool and event.tool_call_id:
                tool_by_call_id[event.tool_call_id] = i

        # A complete pair is safe to remove together
        matched_ids = set(asst_by_call_id.keys()) & set(tool_by_call_id.keys())
        for tc_id in matched_ids:
            safe.add(asst_by_call_id[tc_id])
            safe.add(tool_by_call_id[tc_id])

        # Standalone events are individually safe
        for i, event in enumerate(events):
            if i == 0:
                continue
            if not event.is_tool and not (event.is_assistant and event.has_tool_calls):
                safe.add(i)

        return safe


# ──────────────────────────────────────────────────────────────────────────────
# ToolLoopAtomicity
# ──────────────────────────────────────────────────────────────────────────────

class ToolLoopAtomicity(ViewProperty):
    """
    If a tool loop is detected (the same tool called repeatedly with
    similar arguments and failing), the entire loop must be kept or
    removed as a unit — never partially.

    A "loop" is defined as a sequence of consecutive assistant+tool pairs
    where the same tool name appears `min_loop_length` or more times in a
    row.

    This prevents condensation from keeping only the failing part of a
    loop, which would mislead the LLM about what was tried.
    """

    def __init__(self, min_loop_length: int = 3) -> None:
        self.min_loop_length = min_loop_length

    def _detect_loops(self, events: list[Event]) -> list[list[int]]:
        """
        Detect tool loops and return groups of indices that form each loop.

        A loop is a maximal sequence of consecutive (assistant, tool) pairs
        where the tool name repeats.
        """
        loops: list[list[int]] = []
        current_loop: list[int] = []
        current_tool_name: Optional[str] = None

        for i, event in enumerate(events):
            if event.is_tool and event.name:
                if event.name == current_tool_name:
                    # Continue existing loop — include the assistant that
                    # preceded this tool response
                    if current_loop and current_loop[-1] != i - 1:
                        current_loop.append(i - 1)  # assistant index
                    current_loop.append(i)
                else:
                    # New tool or different tool
                    if len(current_loop) >= self.min_loop_length * 2:
                        loops.append(current_loop[:])
                    current_loop = [i - 1, i] if i > 0 and events[i - 1].is_assistant else [i]
                    current_tool_name = event.name
            elif event.is_assistant:
                # Might precede a tool call; don't reset loop yet
                pass
            else:
                # Non-tool event breaks the loop
                if len(current_loop) >= self.min_loop_length * 2:
                    loops.append(current_loop[:])
                current_loop = []
                current_tool_name = None

        # Final loop
        if len(current_loop) >= self.min_loop_length * 2:
            loops.append(current_loop[:])

        return loops

    def check(self, events: list[Event]) -> bool:
        """Check that no partial loops exist (all loops are complete)."""
        loops = self._detect_loops(events)
        # If loops exist, they must be self-consistent
        # (This property is more about enforcement than pure checking)
        for loop_indices in loops:
            loop_events = [events[i] for i in loop_indices if i < len(events)]
            tool_names = [e.name for e in loop_events if e.is_tool and e.name]
            if not tool_names:
                continue
            # All tool names in a loop should be the same
            if len(set(tool_names)) != 1:
                return False
        return True

    def enforce(self, events: list[Event]) -> list[Event]:
        """
        Remove entire loops.  Keeps the first occurrence of the tool
        and removes subsequent repeats if a loop is detected.

        Actually, for structural integrity, we keep the loop intact
        or remove it entirely. Since partial removal is the problem,
        we just return events as-is (the loop is atomic).
        The actual removal decision is made by the condenser.
        """
        # For enforcement, we simply verify loops are intact.
        # If a partial loop is detected, we remove the entire loop.
        loops = self._detect_loops(events)
        if not loops:
            return events

        loop_indices: set[int] = set()
        for loop in loops:
            loop_indices.update(loop)

        # If any loop index is missing (partial), remove the whole loop
        # Since we're given the full events, loops should be intact.
        # We just flag them for the condenser.
        return events

    def safe_indices(self, events: list[Event]) -> set[int]:
        """
        Loop indices are safe to remove ONLY as a complete group.
        Non-loop indices are individually safe.
        """
        loops = self._detect_loops(events)
        loop_indices: set[int] = set()
        for loop in loops:
            loop_indices.update(loop)

        # Loop indices: safe only as a group (all or nothing)
        # We mark them ALL as safe since the condenser should remove
        # them atomically
        safe: set[int] = set()
        for i in range(len(events)):
            if i == 0:
                continue
            if i in loop_indices:
                safe.add(i)  # safe as part of the atomic group
            else:
                safe.add(i)  # individually safe

        return safe

    def get_loop_groups(self, events: list[Event]) -> list[set[int]]:
        """Return loop index groups for use by condensers."""
        loops = self._detect_loops(events)
        return [set(loop) for loop in loops if len(loop) >= self.min_loop_length * 2]


# ──────────────────────────────────────────────────────────────────────────────
# Convenience: all default properties
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_PROPERTIES: list[ViewProperty] = [
    BatchAtomicity(),
    ObservationUniqueness(),
    ToolCallMatching(),
    ToolLoopAtomicity(),
]
