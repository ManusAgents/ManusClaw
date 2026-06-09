"""Live Canvas server — WebSocket endpoint for A2UI protocol.

Provides a :class:`CanvasServer` that manages per-session canvas state and
broadcasts component updates to connected viewers via WebSocket.

Usage in ``app/server/main.py``::

    canvas_server = CanvasServer()
    app.include_router(canvas_server.router)

The server exposes:
    - ``GET /ws/canvas/{session_id}`` — WebSocket for real-time canvas updates
    - State management via :meth:`update`, :meth:`clear`, :meth:`get_state`
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable, Coroutine, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.canvas.a2ui import (
    CanvasComponent,
    CanvasEvent,
    CanvasUpdate,
    ComponentType,
    MessageType,
    component_from_dict,
    event_from_dict,
    serialize_message,
    parse_message,
)
from app.logger import logger

# Type alias for event handler callbacks
EventHandler = Callable[[CanvasEvent], Coroutine[Any, Any, None]]


class CanvasServer:
    """WebSocket-backed canvas server for the A2UI protocol.

    Manages canvas state per session and broadcasts updates to connected
    viewers.  Supports multiple concurrent viewers per session.

    Parameters
    ----------
    max_sessions:
        Maximum number of concurrent canvas sessions.
    """

    def __init__(self, max_sessions: int = 100) -> None:
        self._max_sessions = max_sessions
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._event_handlers: list[EventHandler] = []

    # ------------------------------------------------------------------
    # FastAPI router (for mounting in main.py)
    # ------------------------------------------------------------------

    @property
    def router(self) -> APIRouter:
        """Return a FastAPI :class:`APIRouter` with canvas endpoints."""
        r = APIRouter(prefix="/ws/canvas", tags=["canvas"])
        r.add_websocket_route(
            "/{session_id}",
            self._websocket_handler,
        )
        return r

    async def _websocket_handler(self, websocket: WebSocket, session_id: str) -> None:
        """Handle incoming WebSocket connections for a canvas session."""
        await websocket.accept()
        await self._connect(websocket, session_id)

        # Send initial state sync
        state = await self.get_state(session_id)
        try:
            await websocket.send_text(json.dumps({
                "message_type": MessageType.SYNC.value,
                "session_id": session_id,
                "components": state.get("components", []),
            }))
        except Exception as exc:
            logger.error("[Canvas] Failed to send initial sync: %s", exc)

        logger.info("[Canvas] Viewer connected to session %s", session_id)

        try:
            while True:
                raw = await websocket.receive_text()
                msg = parse_message(raw)

                # Handle incoming events from the viewer
                if msg.get("message_type") == MessageType.EVENT.value:
                    event = event_from_dict(msg)
                    event.session_id = session_id  # type: ignore[attr-defined]
                    await self._dispatch_event(event)

                elif msg.get("message_type") == MessageType.SYNC.value:
                    # Re-send current state
                    state = await self.get_state(session_id)
                    await websocket.send_text(json.dumps({
                        "message_type": MessageType.SYNC.value,
                        "session_id": session_id,
                        "components": state.get("components", []),
                    }))

                elif msg.get("message_type") == "ping":
                    await websocket.send_text(json.dumps({"message_type": "pong"}))

        except WebSocketDisconnect:
            logger.info("[Canvas] Viewer disconnected from session %s", session_id)
        except Exception as exc:
            logger.error("[Canvas] WebSocket error for session %s: %s", session_id, exc)
        finally:
            await self._disconnect(websocket, session_id)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _connect(self, ws: WebSocket, session_id: str) -> None:
        """Register a new WebSocket connection for a session."""
        async with self._lock:
            if session_id not in self._connections:
                self._connections[session_id] = []
                # Initialize session state
                if session_id not in self._sessions:
                    self._sessions[session_id] = {
                        "components": [],
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    }
            self._connections[session_id].append(ws)

    async def _disconnect(self, ws: WebSocket, session_id: str) -> None:
        """Unregister a WebSocket connection."""
        async with self._lock:
            conns = self._connections.get(session_id, [])
            if ws in conns:
                conns.remove(ws)
            if not conns:
                self._connections.pop(session_id, None)

    async def _broadcast(self, session_id: str, data: dict[str, Any]) -> None:
        """Send a message to all viewers of a session."""
        conns = self._connections.get(session_id, [])
        if not conns:
            return

        message = json.dumps(data, default=str)
        disconnected: list[WebSocket] = []

        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

        # Clean up dead connections
        for ws in disconnected:
            await self._disconnect(ws, session_id)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def on_event(self, handler: EventHandler) -> None:
        """Register an event handler for viewer interactions.

        Handlers are called whenever a user clicks a button, submits a form,
        or interacts with any component that emits events.
        """
        self._event_handlers.append(handler)

    async def _dispatch_event(self, event: CanvasEvent) -> None:
        """Dispatch a canvas event to all registered handlers."""
        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as exc:
                logger.error("[Canvas] Event handler error: %s", exc)

    # ------------------------------------------------------------------
    # State management (public API)
    # ------------------------------------------------------------------

    async def update(
        self,
        session_id: str,
        components: list[dict[str, Any]],
        clear_first: bool = False,
    ) -> dict[str, Any]:
        """Update the canvas for a session with new components.

        Parameters
        ----------
        session_id:
            Unique canvas session identifier.
        components:
            List of component dicts to render.  Each must have at least
            ``id`` and ``component_type`` fields.
        clear_first:
            If True, remove all existing components before adding new ones.

        Returns
        -------
        dict
            Updated canvas state.
        """
        async with self._lock:
            # Ensure session exists
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "components": [],
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }

            state = self._sessions[session_id]

            if clear_first:
                state["components"] = []

            # Merge or replace components
            for comp_dict in components:
                comp_id = comp_dict.get("id", str(uuid.uuid4())[:8])
                if not comp_dict.get("id"):
                    comp_dict["id"] = comp_id

                # Update existing or append new
                existing_idx = next(
                    (i for i, c in enumerate(state["components"])
                     if c.get("id") == comp_id),
                    None,
                )
                if existing_idx is not None:
                    state["components"][existing_idx] = comp_dict
                else:
                    state["components"].append(comp_dict)

            state["updated_at"] = time.time()

        # Broadcast update to all connected viewers
        update_msg = CanvasUpdate(
            session_id=session_id,
            message_type=MessageType.UPDATE.value,
            components=components,
            clear_first=clear_first,
        )
        await self._broadcast(session_id, asdict(update_msg))

        logger.debug(
            "[Canvas] Updated session %s: %d components (clear=%s)",
            session_id,
            len(state["components"]),
            clear_first,
        )
        return state

    async def clear(self, session_id: str) -> dict[str, Any]:
        """Clear all components from a canvas session.

        Returns the cleared (empty) state.
        """
        async with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["components"] = []
                self._sessions[session_id]["updated_at"] = time.time()

        state = self._sessions.get(session_id, {"components": []})

        # Broadcast clear
        await self._broadcast(session_id, {
            "message_type": MessageType.CLEAR.value,
            "session_id": session_id,
        })

        logger.info("[Canvas] Cleared session %s", session_id)
        return state

    async def get_state(self, session_id: str) -> dict[str, Any]:
        """Return the current canvas state for a session.

        Returns an empty state dict if the session doesn't exist.
        """
        return self._sessions.get(session_id, {
            "components": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        })

    async def remove_component(self, session_id: str, component_id: str) -> None:
        """Remove a single component from the canvas."""
        async with self._lock:
            if session_id in self._sessions:
                state = self._sessions[session_id]
                state["components"] = [
                    c for c in state["components"]
                    if c.get("id") != component_id
                ]
                state["updated_at"] = time.time()

        await self._broadcast(session_id, {
            "message_type": MessageType.UPDATE.value,
            "session_id": session_id,
            "removed": [component_id],
        })

    async def session_count(self) -> int:
        """Return the number of active canvas sessions."""
        return len(self._sessions)

    async def cleanup_session(self, session_id: str) -> None:
        """Remove a session and all its state."""
        async with self._lock:
            self._sessions.pop(session_id, None)
            # Close all connections for this session
            conns = self._connections.pop(session_id, [])
        for ws in conns:
            try:
                await ws.close(code=1000, reason="Session cleaned up")
            except Exception:
                pass
        logger.info("[Canvas] Cleaned up session %s", session_id)
