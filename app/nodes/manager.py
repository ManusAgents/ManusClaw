from __future__ import annotations

"""
DeviceManager — Manages connected mobile/desktop Live Canvas nodes.

Tracks connected devices (iOS, Android, desktop), handles registration,
Canvas updates, and user interaction events via WebSocket.

Uses LRU cache pattern (similar to MessagingGateway) for node connections.
"""

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Callable, Coroutine, Optional

from app.logger import logger
from app.nodes.protocol import (
    CanvasPush,
    NodeCommand,
    NodeEvent,
    NodeHeartbeat,
    NodeInfo,
    NodeRegistered,
    NodeRegister,
    CapabilityChecker,
    MessageType,
    serialize_message,
    parse_message,
    parse_node_event,
)

# Type aliases
EventHandler = Callable[[NodeEvent], Coroutine[Any, Any, None]]
CommandHandler = Callable[[NodeCommand], Coroutine[Any, Any, None]]

_CACHE_SIZE = 128
_HEARTBEAT_TIMEOUT = 120  # seconds before a node is considered stale


class DeviceManager:
    """Manages connected mobile/desktop Live Canvas nodes.

    Features:
    - Device registration and deregistration
    - Per-node capability tracking
    - Canvas update delivery via WebSocket
    - User interaction event handling
    - Heartbeat monitoring
    - LRU cache for node connections
    - Broadcast to all connected nodes

    Usage::

        dm = DeviceManager()
        dm.register(device_info, websocket)
        await dm.send_to_node("device-id", canvas_push)
        await dm.broadcast(canvas_push)
    """

    def __init__(self, max_nodes: int = _CACHE_SIZE,
                 heartbeat_timeout: int = _HEARTBEAT_TIMEOUT) -> None:
        self._nodes: OrderedDict[str, NodeInfo] = OrderedDict()
        self._max_nodes = max_nodes
        self._heartbeat_timeout = heartbeat_timeout
        self._event_handlers: list[EventHandler] = []
        self._command_handlers: list[CommandHandler] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Node Registration
    # ------------------------------------------------------------------

    async def register(self, registration: NodeRegister, ws: Any = None) -> NodeInfo:
        """Register a new device node or update an existing one.

        Args:
            registration: NodeRegister message with device information.
            ws: WebSocket connection reference (optional).

        Returns:
            NodeInfo for the registered device.
        """
        async with self._lock:
            device_id = registration.device_id
            now = time.monotonic()

            # Check if already registered (update)
            if device_id in self._nodes:
                node = self._nodes[device_id]
                node.device_type = registration.device_type
                node.capabilities = registration.capabilities
                node.os_name = registration.os_name
                node.os_version = registration.os_version
                node.app_version = registration.app_version
                node.screen_width = registration.screen_width
                node.screen_height = registration.screen_height
                node.connected = True
                node.last_heartbeat = now
                if ws:
                    node.ws = ws
                self._nodes.move_to_end(device_id)
                logger.info(
                    f"[DeviceManager] Updated node: {device_id} "
                    f"({registration.device_type}, "
                    f"{', '.join(registration.capabilities)})"
                )
            else:
                # Evict if at capacity
                self._evict_stale(now)
                while len(self._nodes) >= self._max_nodes:
                    self._nodes.popitem(last=False)

                node = NodeInfo(
                    device_id=device_id,
                    device_type=registration.device_type,
                    capabilities=registration.capabilities,
                    os_name=registration.os_name,
                    os_version=registration.os_version,
                    app_version=registration.app_version,
                    screen_width=registration.screen_width,
                    screen_height=registration.screen_height,
                    connected=True,
                    last_heartbeat=now,
                    ws=ws,
                )
                self._nodes[device_id] = node
                logger.info(
                    f"[DeviceManager] Registered new node: {device_id} "
                    f"({registration.device_type}, "
                    f"{', '.join(registration.capabilities)})"
                )

            return node

    async def unregister(self, device_id: str) -> bool:
        """Unregister a device node.

        Args:
            device_id: Device identifier to unregister.

        Returns:
            True if the device was found and removed.
        """
        async with self._lock:
            if device_id in self._nodes:
                self._nodes[device_id].connected = False
                self._nodes[device_id].ws = None
                del self._nodes[device_id]
                logger.info(f"[DeviceManager] Unregistered node: {device_id}")
                return True
            return False

    # ------------------------------------------------------------------
    # Message Delivery
    # ------------------------------------------------------------------

    async def send_to_node(self, device_id: str, data: Any) -> bool:
        """Send a message to a specific node via its WebSocket connection.

        Args:
            device_id: Target device identifier.
            data: Message to send (protocol dataclass or dict).

        Returns:
            True if the message was sent successfully.
        """
        node = self._nodes.get(device_id)
        if not node or not node.connected or not node.ws:
            logger.warning(f"[DeviceManager] Cannot send to {device_id}: not connected")
            return False

        try:
            if hasattr(data, "to_dict"):
                msg_str = serialize_message(data)
            elif isinstance(data, dict):
                msg_str = json.dumps(data, default=str)
            else:
                msg_str = str(data)

            await node.ws.send_text(msg_str)
            return True
        except Exception as e:
            logger.warning(f"[DeviceManager] Send to {device_id} failed: {e}")
            # Mark as disconnected
            node.connected = False
            return False

    async def broadcast(self, data: Any, filter_fn: Optional[Callable] = None) -> int:
        """Broadcast a message to all connected nodes.

        Args:
            data: Message to send (protocol dataclass or dict).
            filter_fn: Optional filter function. Called with NodeInfo,
                       returns True if the node should receive the message.

        Returns:
            Number of nodes the message was sent to.
        """
        sent = 0
        disconnected: list[str] = []

        for device_id, node in self._nodes.items():
            if not node.connected or not node.ws:
                continue
            if filter_fn and not filter_fn(node):
                continue

            success = await self.send_to_node(device_id, data)
            if success:
                sent += 1
            else:
                disconnected.append(device_id)

        # Clean up disconnected nodes
        for did in disconnected:
            await self.unregister(did)

        if sent > 0:
            logger.debug(f"[DeviceManager] Broadcast to {sent} node(s)")
        return sent

    async def send_canvas_update(self, session_id: str, components: list[dict[str, Any]],
                                clear_first: bool = False,
                                device_id: Optional[str] = None) -> int:
        """Send a Canvas update to a specific node or all connected nodes.

        Args:
            session_id: Session this canvas belongs to.
            components: Canvas component dicts to render.
            clear_first: Whether to clear existing components first.
            device_id: If set, send only to this device. Otherwise broadcast.

        Returns:
            Number of nodes the update was sent to.
        """
        push = CanvasPush(
            session_id=session_id,
            components=components,
            clear_first=clear_first,
        )

        if device_id:
            return 1 if await self.send_to_node(device_id, push) else 0
        return await self.broadcast(push)

    # ------------------------------------------------------------------
    # Heartbeat Management
    # ------------------------------------------------------------------

    async def update_heartbeat(self, device_id: str, heartbeat: Optional[NodeHeartbeat] = None) -> None:
        """Update the last heartbeat time for a node.

        Args:
            device_id: Device identifier.
            heartbeat: Optional heartbeat message with additional info.
        """
        node = self._nodes.get(device_id)
        if node:
            node.last_heartbeat = time.monotonic()
            node.connected = True
            if heartbeat:
                node.battery_level = heartbeat.battery_level
                node.network_type = heartbeat.network_type

    async def check_heartbeats(self) -> list[str]:
        """Check all nodes for stale heartbeats.

        Returns:
            List of device IDs that have timed out.
        """
        now = time.monotonic()
        stale = []
        for device_id, node in list(self._nodes.items()):
            if node.connected and (now - node.last_heartbeat) > self._heartbeat_timeout:
                stale.append(device_id)
                node.connected = False
                logger.warning(
                    f"[DeviceManager] Node {device_id} heartbeat timeout "
                    f"({now - node.last_heartbeat:.0f}s since last heartbeat)"
                )
        return stale

    # ------------------------------------------------------------------
    # Event Handling
    # ------------------------------------------------------------------

    def on_event(self, handler: EventHandler) -> None:
        """Register a handler for node interaction events.

        Callback signature: ``async handler(event: NodeEvent) -> None``
        """
        self._event_handlers.append(handler)

    async def handle_event(self, event: NodeEvent) -> None:
        """Dispatch a node event to all registered handlers."""
        logger.debug(
            f"[DeviceManager] Event: {event.event_type} from {event.device_id} "
            f"(component={event.component_id})"
        )
        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"[DeviceManager] Event handler error: {e}")

    def on_command(self, handler: CommandHandler) -> None:
        """Register a handler for commands sent from nodes.

        Callback signature: ``async handler(command: NodeCommand) -> None``
        """
        self._command_handlers.append(handler)

    async def handle_command(self, command: NodeCommand) -> None:
        """Dispatch a command from a node to all registered handlers."""
        logger.debug(
            f"[DeviceManager] Command from {command.device_id}: {command.command}"
        )
        for handler in self._command_handlers:
            try:
                await handler(command)
            except Exception as e:
                logger.error(f"[DeviceManager] Command handler error: {e}")

    # ------------------------------------------------------------------
    # Incoming Message Processing
    # ------------------------------------------------------------------

    async def process_message(self, device_id: str, raw_message: str) -> None:
        """Process an incoming WebSocket message from a node.

        Routes the message to the appropriate handler based on type.
        """
        data = parse_message(raw_message)
        msg_type = data.get("type", "")

        if msg_type == MessageType.HEARTBEAT.value:
            heartbeat = NodeHeartbeat(
                device_id=device_id,
                battery_level=data.get("battery_level", 1.0),
                memory_usage=data.get("memory_usage", 0.0),
                network_type=data.get("network_type", "unknown"),
            )
            await self.update_heartbeat(device_id, heartbeat)

            # Send acknowledgment
            node = self._nodes.get(device_id)
            if node and node.ws:
                ack = {
                    "type": MessageType.HEARTBEAT_ACK.value,
                    "device_id": device_id,
                    "timestamp": time.time(),
                }
                await self.send_to_node(device_id, ack)

        elif msg_type == MessageType.EVENT.value:
            event = parse_node_event(raw_message)
            if event:
                event.device_id = device_id
                await self.handle_event(event)

        elif msg_type == MessageType.COMMAND.value:
            command = NodeCommand.from_dict(data)
            command.device_id = device_id
            await self.handle_command(command)

        elif msg_type == "ping":
            await self.send_to_node(device_id, {"type": "pong"})

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_node(self, device_id: str) -> Optional[NodeInfo]:
        """Get a node by device ID."""
        return self._nodes.get(device_id)

    def list_nodes(self) -> list[NodeInfo]:
        """Return all registered nodes."""
        return list(self._nodes.values())

    def list_connected(self) -> list[NodeInfo]:
        """Return only connected (active) nodes."""
        return [n for n in self._nodes.values() if n.connected]

    def find_by_capability(self, capability: str) -> list[NodeInfo]:
        """Find all nodes that have a specific capability."""
        return [
            n for n in self._nodes.values()
            if n.connected and capability.lower() in [c.lower() for c in n.capabilities]
        ]

    @property
    def connected_count(self) -> int:
        """Return the number of connected nodes."""
        return sum(1 for n in self._nodes.values() if n.connected)

    @property
    def total_count(self) -> int:
        """Return the total number of registered nodes."""
        return len(self._nodes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_stale(self, now: float) -> None:
        """Remove nodes that have been inactive beyond heartbeat timeout."""
        stale = [
            did for did, node in self._nodes.items()
            if not node.connected
            or (now - node.last_heartbeat) > self._heartbeat_timeout * 2
        ]
        for did in stale:
            self._nodes.pop(did, None)
            logger.debug(f"[DeviceManager] Evicted stale node: {did}")

    async def shutdown(self) -> None:
        """Disconnect all nodes and clean up."""
        for device_id in list(self._nodes.keys()):
            await self.unregister(device_id)
        self._event_handlers.clear()
        self._command_handlers.clear()
        logger.info("[DeviceManager] Shutdown complete")


class NodeClient:
    """Client-side helper for connecting to the DeviceManager.

    Provides a simplified interface for nodes to register, send events,
    and receive canvas updates.
    """

    def __init__(self, device_id: str, device_type: str = "unknown",
                 capabilities: Optional[list[str]] = None) -> None:
        self.device_id = device_id
        self.device_type = device_type
        self.capabilities = capabilities or []
        self._ws: Any = None
        self._connected = False
        self._on_canvas_update: Optional[list] = []
        self._on_command_callbacks: list = []

    @property
    def connected(self) -> bool:
        return self._connected

    def on_canvas_update(self, callback: Callable) -> None:
        """Register a callback for canvas updates from the server."""
        self._on_canvas_update.append(callback)

    def on_command(self, callback: Callable) -> None:
        """Register a callback for server commands."""
        self._on_command_callbacks.append(callback)

    async def connect(self, ws_url: str) -> None:
        """Connect to the DeviceManager via WebSocket."""
        try:
            import websockets
        except ImportError:
            raise ImportError("websockets required: pip install websockets")

        async with websockets.connect(ws_url) as ws:
            self._ws = ws
            self._connected = True

            # Send registration
            registration = NodeRegister(
                device_id=self.device_id,
                device_type=self.device_type,
                capabilities=self.capabilities,
            )
            await ws.send(serialize_message(registration))

            # Message loop
            async for raw in ws:
                data = parse_message(raw)
                msg_type = data.get("type", "")

                if msg_type == MessageType.REGISTERED.value:
                    logger.info(f"[NodeClient] Registered as {self.device_id}")

                elif msg_type in (MessageType.CANVAS_PUSH.value, "canvas_update"):
                    for cb in self._on_canvas_update:
                        try:
                            cb(data)
                        except Exception as e:
                            logger.error(f"[NodeClient] Canvas callback error: {e}")

                elif msg_type == MessageType.COMMAND.value:
                    for cb in self._on_command_callbacks:
                        try:
                            cb(data)
                        except Exception as e:
                            logger.error(f"[NodeClient] Command callback error: {e}")

                elif msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

        self._connected = False

    async def send_event(self, event: NodeEvent) -> None:
        """Send an interaction event to the server."""
        if self._ws and self._connected:
            await self._ws.send(serialize_message(event))
