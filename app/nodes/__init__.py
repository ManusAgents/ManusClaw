"""Live Canvas Nodes — Mobile/desktop node management for ManusClaw.

Manages connected devices (iOS, Android, desktop) that receive Canvas
updates and send user interaction events via WebSocket.

Usage::

    from app.nodes import DeviceManager, NodeClient

    # Server side:
    dm = DeviceManager()
    await dm.register(registration, websocket)
    await dm.send_canvas_update(session_id, components)

    # Client side:
    client = NodeClient("my-device", "ios", ["voice", "screen"])
    await client.connect("ws://localhost:8765/ws/nodes/my-device")
"""

from app.nodes.manager import DeviceManager, NodeClient
from app.nodes.protocol import (
    CanvasPush,
    CapabilityChecker,
    DeviceType,
    EventType,
    MessageType,
    NodeCommand,
    NodeEvent,
    NodeHeartbeat,
    NodeInfo,
    NodeRegister,
    NodeRegistered,
    parse_message,
    parse_node_event,
    parse_node_register,
    serialize_message,
)

__all__ = [
    "DeviceManager",
    "NodeClient",
    "CanvasPush",
    "CapabilityChecker",
    "DeviceType",
    "EventType",
    "MessageType",
    "NodeCommand",
    "NodeEvent",
    "NodeHeartbeat",
    "NodeInfo",
    "NodeRegister",
    "NodeRegistered",
    "parse_message",
    "parse_node_event",
    "parse_node_register",
    "serialize_message",
]
