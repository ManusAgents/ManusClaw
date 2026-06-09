from __future__ import annotations

"""
Node Protocol — Data definitions for Live Canvas node communication.

Defines the wire protocol for communication between the ManusClaw server
and mobile/desktop nodes (iOS, Android, desktop) via WebSocket.

Message types:
  - NodeRegister: Device registration
  - NodeHeartbeat: Keep-alive ping
  - NodeEvent: User interactions (touch, voice, etc.)
  - CanvasPush: Server pushes canvas updates to nodes
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MessageType(str, Enum):
    """Protocol message types."""
    REGISTER = "register"
    REGISTERED = "registered"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    EVENT = "event"
    CANVAS_PUSH = "canvas_push"
    COMMAND = "command"
    PONG = "pong"
    ERROR = "error"
    DISCONNECT = "disconnect"


class EventType(str, Enum):
    """Types of user interaction events from nodes."""
    TOUCH = "touch"
    TAP = "tap"
    SWIPE = "swipe"
    VOICE = "voice"
    TEXT_INPUT = "text_input"
    BUTTON_PRESS = "button_press"
    SCROLL = "scroll"
    PINCH = "pinch"
    SHAKE = "shake"
    SCREENSHOT = "screenshot"


class DeviceType(str, Enum):
    """Supported device types."""
    IOS = "ios"
    ANDROID = "android"
    DESKTOP = "desktop"
    WEB = "web"
    UNKNOWN = "unknown"


@dataclass
class NodeRegister:
    """Device registration message sent from node to server.

    Attributes:
        device_id: Unique device identifier.
        device_type: Device platform (ios, android, desktop, web).
        capabilities: List of device capabilities (voice, screen, touch, etc.).
        os_name: Operating system name.
        os_version: OS version string.
        app_version: Node client application version.
        api_key: Optional API key for authentication.
        screen_width: Screen width in pixels.
        screen_height: Screen height in pixels.
    """
    device_id: str = ""
    device_type: str = "unknown"
    capabilities: list[str] = field(default_factory=list)
    os_name: str = "unknown"
    os_version: str = ""
    app_version: str = "1.0.0"
    api_key: Optional[str] = None
    screen_width: int = 0
    screen_height: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.REGISTER.value,
            "device_id": self.device_id,
            "device_type": self.device_type,
            "capabilities": self.capabilities,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "app_version": self.app_version,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "timestamp": time.time(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeRegister":
        device_data = data.get("device", data)
        return cls(
            device_id=device_data.get("device_id", ""),
            device_type=device_data.get("device_type", "unknown"),
            capabilities=device_data.get("capabilities", []),
            os_name=device_data.get("os_name", "unknown"),
            os_version=device_data.get("os_version", ""),
            app_version=device_data.get("app_version", "1.0.0"),
            screen_width=device_data.get("screen_width", 0),
            screen_height=device_data.get("screen_height", 0),
        )


@dataclass
class NodeRegistered:
    """Server acknowledgment of device registration."""
    device_id: str = ""
    session_id: str = ""
    heartbeat_interval: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.REGISTERED.value,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "heartbeat_interval": self.heartbeat_interval,
            "timestamp": time.time(),
        }


@dataclass
class NodeHeartbeat:
    """Keep-alive ping from node to server."""
    device_id: str = ""
    battery_level: float = 1.0  # 0.0-1.0
    memory_usage: float = 0.0  # 0.0-1.0
    network_type: str = "unknown"  # wifi, cellular, none

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.HEARTBEAT.value,
            "device_id": self.device_id,
            "battery_level": self.battery_level,
            "memory_usage": self.memory_usage,
            "network_type": self.network_type,
            "timestamp": time.time(),
        }


@dataclass
class NodeEvent:
    """User interaction event sent from node to server.

    Attributes:
        device_id: Source device identifier.
        event_type: Type of interaction (touch, voice, text_input, etc.).
        x: X coordinate for touch/pointer events.
        y: Y coordinate for touch/pointer events.
        data: Event-specific data (text for text_input, audio for voice, etc.).
        component_id: ID of the canvas component that triggered the event.
        timestamp: Event timestamp.
    """
    device_id: str = ""
    event_type: str = "tap"
    x: float = 0.0
    y: float = 0.0
    data: str = ""
    component_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.EVENT.value,
            "device_id": self.device_id,
            "event_type": self.event_type,
            "x": self.x,
            "y": self.y,
            "data": self.data,
            "component_id": self.component_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeEvent":
        return cls(
            device_id=data.get("device_id", ""),
            event_type=data.get("event_type", "tap"),
            x=data.get("x", 0.0),
            y=data.get("y", 0.0),
            data=data.get("data", ""),
            component_id=data.get("component_id", ""),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class CanvasPush:
    """Server pushes canvas updates to a node.

    Attributes:
        session_id: The session this canvas belongs to.
        components: List of canvas component dicts to render.
        clear_first: Whether to clear existing components before push.
        full_state: If True, this is a full state sync (not incremental).
    """
    session_id: str = ""
    components: list[dict[str, Any]] = field(default_factory=list)
    clear_first: bool = False
    full_state: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.CANVAS_PUSH.value,
            "message_type": "canvas_update",
            "session_id": self.session_id,
            "components": self.components,
            "clear_first": self.clear_first,
            "full_state": self.full_state,
            "timestamp": time.time(),
        }


@dataclass
class NodeCommand:
    """Server-pushed command to a node.

    Used for server-initiated actions like taking screenshots,
    recording voice, etc.
    """
    command: str = ""  # ping, screenshot, voice_start, voice_stop
    device_id: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": MessageType.COMMAND.value,
            "command": self.command,
            "device_id": self.device_id,
            "params": self.params,
            "timestamp": time.time(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeCommand":
        return cls(
            command=data.get("command", ""),
            device_id=data.get("device_id", ""),
            params=data.get("params", {}),
        )


@dataclass
class NodeInfo:
    """Server-side representation of a connected node device."""
    device_id: str
    device_type: str = "unknown"
    capabilities: list[str] = field(default_factory=list)
    os_name: str = "unknown"
    os_version: str = ""
    app_version: str = "1.0.0"
    screen_width: int = 0
    screen_height: int = 0
    connected: bool = False
    last_heartbeat: float = 0.0
    battery_level: float = 1.0
    network_type: str = "unknown"
    ws: Any = None  # WebSocket connection reference

    @property
    def has_capability(self) -> "CapabilityChecker":
        """Return a helper for checking device capabilities."""
        return CapabilityChecker(self.capabilities)


class CapabilityChecker:
    """Helper for checking device capabilities."""

    def __init__(self, capabilities: list[str]) -> None:
        self._caps = set(c.lower() for c in capabilities)

    def __getattr__(self, name: str) -> bool:
        return name.lower() in self._caps

    def has(self, capability: str) -> bool:
        """Check if a specific capability is available."""
        return capability.lower() in self._caps

    def all(self, *capabilities: str) -> bool:
        """Check if ALL given capabilities are available."""
        return all(c.lower() in self._caps for c in capabilities)

    def any(self, *capabilities: str) -> bool:
        """Check if ANY of the given capabilities are available."""
        return any(c.lower() in self._caps for c in capabilities)


# ──────────────────────────────────────────────────────────────────────────────
# Serialization / Deserialization
# ──────────────────────────────────────────────────────────────────────────────

def serialize_message(msg: Any) -> str:
    """Serialize a protocol message to JSON string.

    Accepts any protocol dataclass with a ``to_dict()`` method.
    """
    if hasattr(msg, "to_dict"):
        return json.dumps(msg.to_dict(), default=str)
    return json.dumps(msg, default=str)


def parse_message(raw: str) -> dict[str, Any]:
    """Parse a raw JSON message string into a dict.

    Returns an empty dict on parse failure.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_node_register(raw: str) -> Optional[NodeRegister]:
    """Parse a registration message from a node."""
    data = parse_message(raw)
    if data.get("type") == MessageType.REGISTER.value or "device" in data:
        return NodeRegister.from_dict(data)
    return None


def parse_node_event(raw: str) -> Optional[NodeEvent]:
    """Parse an event message from a node."""
    data = parse_message(raw)
    if data.get("type") == MessageType.EVENT.value:
        return NodeEvent.from_dict(data)
    return None
