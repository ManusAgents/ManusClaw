"""
ManusClaw Event System — Serialization / Deserialization
=========================================================

Handles converting events to/from JSON with type discrimination via the
``kind`` field.  Supports both single-event and batch (newline-delimited
JSON) formats.

Design goals:
  - Deterministic: same event → same JSON (sorted keys, ISO-8601 timestamps).
  - Round-trip safe: ``deserialize(serialize(event)) == event``.
  - Fast: uses Pydantic v2's built-in JSON capabilities where possible.
  - Extensible: adding new event types only requires updating ``KIND_TO_EVENT``
    in ``types.py``; this module picks it up automatically.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

from pydantic import BaseModel, TypeAdapter

from app.events.base import Event, LLMConvertibleEvent
from app.events.types import (
    KIND_TO_EVENT,
    EventUnion,
)

logger = logging.getLogger("manusclaw.events.serialization")


# ──────────────────────────────────────────────────────────────────────────────
# Custom JSON encoder for types that Pydantic doesn't serialize by default
# ──────────────────────────────────────────────────────────────────────────────

class _EventEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime and bytes objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        return super().default(obj)


# ──────────────────────────────────────────────────────────────────────────────
# Serialization
# ──────────────────────────────────────────────────────────────────────────────

def serialize(event: Event) -> str:
    """Serialize a single event to a JSON string.

    The ``kind`` field is always included as the top-level discriminator.

    Args:
        event: Any event instance.

    Returns:
        Compact JSON string with sorted keys for determinism.

    Raises:
        SerializationError: If the event cannot be serialized.
    """
    try:
        data = event.model_dump(mode="json")
        return json.dumps(data, sort_keys=True, cls=_EventEncoder)
    except Exception as exc:
        raise SerializationError(
            f"Failed to serialize event kind={getattr(event, 'kind', '?')}: {exc}"
        ) from exc


def serialize_to_dict(event: Event) -> dict[str, Any]:
    """Serialize a single event to a plain dict (no JSON encoding).

    Useful when embedding events in larger structures or when the
    caller wants to post-process the data before final JSON encoding.

    Args:
        event: Any event instance.

    Returns:
        Dict representation with all types JSON-compatible.

    Raises:
        SerializationError: If the event cannot be serialized.
    """
    try:
        return event.model_dump(mode="json")
    except Exception as exc:
        raise SerializationError(
            f"Failed to serialize event kind={getattr(event, 'kind', '?')} to dict: {exc}"
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Deserialization
# ──────────────────────────────────────────────────────────────────────────────

def deserialize(raw: str | bytes | dict[str, Any]) -> Event:
    """Deserialize a JSON string, bytes, or dict into an Event instance.

    The ``kind`` field is used to dispatch to the correct concrete class.

    Args:
        raw: JSON string, bytes, or dict representing an event.

    Returns:
        Concrete Event subclass instance.

    Raises:
        DeserializationError: If the data is invalid or the kind is unknown.
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, dict):
            data = raw
        else:
            raise DeserializationError(
                f"Expected str, bytes, or dict; got {type(raw).__name__}"
            )
    except json.JSONDecodeError as exc:
        raise DeserializationError(f"Invalid JSON: {exc}") from exc

    kind = data.get("kind")
    if kind is None:
        raise DeserializationError(
            "Missing 'kind' discriminator field in event data"
        )

    event_cls = KIND_TO_EVENT.get(kind)
    if event_cls is None:
        raise DeserializationError(
            f"Unknown event kind '{kind}'. "
            f"Known kinds: {sorted(KIND_TO_EVENT.keys())}"
        )

    try:
        return event_cls.model_validate(data)
    except Exception as exc:
        raise DeserializationError(
            f"Failed to validate {event_cls.__name__} from data: {exc}"
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Batch serialization (newline-delimited JSON — NDJSON)
# ──────────────────────────────────────────────────────────────────────────────

def serialize_batch(events: list[Event]) -> str:
    """Serialize a list of events to newline-delimited JSON (NDJSON).

    Each event is serialized as a single JSON object on its own line.
    This format is ideal for append-only log files because new events
    can be appended without re-writing the entire file.

    Args:
        events: List of event instances.

    Returns:
        NDJSON string (one JSON object per line, trailing newline).

    Raises:
        SerializationError: If any event cannot be serialized.
    """
    lines: list[str] = []
    for event in events:
        lines.append(serialize(event))
    return "\n".join(lines) + "\n" if lines else ""


def deserialize_batch(raw: str | bytes) -> list[Event]:
    """Deserialize a newline-delimited JSON string into a list of events.

    Blank lines are silently skipped.  Individual deserialization errors
    are logged but do **not** abort the batch — valid events are still
    returned.

    Args:
        raw: NDJSON string (one JSON object per line).

    Returns:
        List of successfully deserialized Event instances.

    Raises:
        DeserializationError: If the input is not valid UTF-8.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeserializationError(f"Invalid UTF-8: {exc}") from exc

    events: list[Event] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(deserialize(line))
        except DeserializationError as exc:
            logger.warning(
                "Skipping invalid event at line %d: %s", line_no, exc
            )
    return events


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic TypeAdapter for the discriminated union
# ──────────────────────────────────────────────────────────────────────────────

# This adapter can validate/deserialize any event using Pydantic's native
# discriminated union support.  It is used internally but also exposed for
# callers who prefer the TypeAdapter API.

try:
    from typing import Annotated
    _EVENT_UNION_ADAPTER = TypeAdapter(
        Annotated[EventUnion, ...]
    )
except Exception:
    # Fallback: if the Annotated form doesn't work with the Union,
    # we use our own dispatch via deserialize()
    _EVENT_UNION_ADAPTER = None


def adapter_validate(data: dict[str, Any]) -> Event:
    """Validate an event dict using the Pydantic discriminated-union adapter.

    Falls back to :func:`deserialize` if the adapter is unavailable.
    """
    if _EVENT_UNION_ADAPTER is not None:
        try:
            return _EVENT_UNION_ADAPTER.validate_python(data)
        except Exception:
            pass
    return deserialize(data)


# ──────────────────────────────────────────────────────────────────────────────
# Error types
# ──────────────────────────────────────────────────────────────────────────────

class SerializationError(Exception):
    """Raised when an event cannot be serialized to JSON."""


class DeserializationError(Exception):
    """Raised when JSON data cannot be deserialized into an Event."""
