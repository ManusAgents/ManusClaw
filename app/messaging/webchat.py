from __future__ import annotations
"""WebChat adapter — internal WebSocket-based chat for the built-in web UI.

This adapter manages lightweight WebSocket connections originating from the
ManusClaw web interface.  It does not connect to any external service.
"""
import asyncio
import json
from typing import Optional
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class WebChatAdapter(BaseMessagingAdapter):
    """Built-in web chat adapter using WebSocket connections.

    The adapter acts as a registry for active WebSocket connections.
    ``app/server/main.py`` should call ``register_connection`` when a client
    connects and ``unregister_connection`` when it disconnects.
    """

    platform_name = "webchat"

    def __init__(self) -> None:
        # Token not needed for internal adapter — always considered configured
        super().__init__(token="webchat-internal")
        self._connections: dict[str, asyncio.Queue] = {}

    def is_configured(self) -> bool:
        return True

    async def connect(self) -> None:
        logger.info("[WebChat] Internal adapter ready (no external connection needed)")

    async def start(self, on_message) -> None:
        self._running = True
        self._on_message = on_message
        logger.info("[WebChat] Accepting WebSocket connections")
        # Messages are pushed via receive_from_client; no polling needed.

    async def register_connection(self, client_id: str) -> asyncio.Queue:
        """Register a new WebSocket client and return its outgoing queue."""
        queue: asyncio.Queue = asyncio.Queue()
        self._connections[client_id] = queue
        logger.info(f"[WebChat] Client registered: {client_id} ({len(self._connections)} active)")
        return queue

    async def unregister_connection(self, client_id: str) -> None:
        """Remove a disconnected WebSocket client."""
        self._connections.pop(client_id, None)
        logger.info(f"[WebChat] Client disconnected: {client_id} ({len(self._connections)} active)")

    async def receive_from_client(self, client_id: str, text: str) -> None:
        """Called by the web server when a message arrives from a web client."""
        msg = IncomingMessage(
            platform="webchat",
            user_id=client_id,
            channel_id=client_id,
            text=text,
        )
        if hasattr(self, "_on_message"):
            await self._on_message(msg)
        else:
            logger.warning(f"[WebChat] No handler registered; dropped message from {client_id}")

    async def send(self, channel_id: str, text: str) -> None:
        """Send a response back to a specific web client."""
        queue = self._connections.get(channel_id)
        if queue is None:
            logger.warning(f"[WebChat] No active connection for {channel_id}")
            return
        await queue.put({"type": "message", "text": text[:4000]})
        logger.debug(f"[WebChat] Response queued for {channel_id}")

    async def broadcast(self, text: str) -> None:
        """Broadcast a message to all connected web clients."""
        for client_id, queue in self._connections.items():
            await queue.put({"type": "broadcast", "text": text[:4000]})
        if self._connections:
            logger.debug(f"[WebChat] Broadcast to {len(self._connections)} clients")

    async def disconnect(self) -> None:
        self._running = False
        self._connections.clear()
        logger.info("[WebChat] Disconnected")
