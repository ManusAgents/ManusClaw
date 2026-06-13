"""
ManusClaw Conversation System — RemoteConversation
=====================================================

A conversation that connects to a remote agent server via WebSocket.

Key features:
  - WebSocket-based communication with an agent server.
  - Subscribes to conversation events in real-time.
  - Sends messages and control commands (pause, resume, interrupt).
  - Reconnection with exponential backoff.
  - Event buffering during disconnection.
  - Thread-safe event accumulation.

The RemoteConversation does **not** run the agent locally — it delegates
all execution to the remote server and acts as a thin client that
mirrors the conversation state locally.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

from app.conversation.base import BaseConversation
from app.conversation.cancellation_token import CancellationToken, CancelledError
from app.conversation.state import ConversationState, ExecutionStatus
from app.conversation.stuck_detector import StuckDetector, StuckReport
from app.events.base import Event
from app.events.serialization import deserialize, serialize
from app.events.types import (
    ConversationStateUpdateEvent,
    InterruptEvent,
    MessageEvent,
    PauseEvent,
    ResumeTranscriptEvent,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Connection states
# ──────────────────────────────────────────────────────────────────────────────

class ConnectionState(str):
    """WebSocket connection state."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


# ──────────────────────────────────────────────────────────────────────────────
# RemoteConversation
# ──────────────────────────────────────────────────────────────────────────────

class RemoteConversation(BaseConversation):
    """
    A conversation that connects to a remote agent server via WebSocket.

    The RemoteConversation subscribes to a WebSocket endpoint on the
    agent server, receives real-time events, and sends user messages
    and control commands.  If the connection drops, it automatically
    reconnects with exponential backoff and buffers events that arrive
    during disconnection.

    Args:
        conversation_id:    Optional unique ID (auto-generated if not provided).
        server_url:         WebSocket URL of the agent server
                            (e.g. ``ws://localhost:3000/ws``).
        confirmation_policy: Security confirmation policy.
        security_analyzer:   Security analyzer instance.
        stuck_detector:      Stuck pattern detector.
        hook_manager:        Hook manager for lifecycle events.
        max_reconnect_attempts: Maximum reconnection attempts before giving up.
        base_reconnect_delay_s:  Base delay (seconds) for exponential backoff.
        max_reconnect_delay_s:   Maximum delay (seconds) between reconnection attempts.
        event_buffer_size:  Maximum number of events to buffer during
                            disconnection (oldest events are dropped).
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        server_url: str = "ws://localhost:3000/ws",
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
        stuck_detector: Optional[StuckDetector] = None,
        hook_manager: Optional[Any] = None,
        max_reconnect_attempts: int = 10,
        base_reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
        event_buffer_size: int = 1000,
    ) -> None:
        super().__init__(
            conversation_id=conversation_id,
            confirmation_policy=confirmation_policy,
            security_analyzer=security_analyzer,
            stuck_detector=stuck_detector,
            hook_manager=hook_manager,
        )

        self._server_url: str = server_url
        self._max_reconnect_attempts: int = max_reconnect_attempts
        self._base_reconnect_delay_s: float = base_reconnect_delay_s
        self._max_reconnect_delay_s: float = max_reconnect_delay_s
        self._event_buffer_size: int = event_buffer_size

        # WebSocket connection
        self._ws: Optional[Any] = None
        self._connection_state: str = ConnectionState.DISCONNECTED
        self._reconnect_attempts: int = 0

        # Local event buffer — events received from the server
        self._event_buffer: Deque[Event] = deque(maxlen=event_buffer_size)

        # Event listeners — callbacks invoked when events arrive
        self._listeners: List[Callable[[Event], None]] = []

        # Background tasks
        self._listener_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

        # Run state
        self._run_future: Optional[asyncio.Future] = None

    # ── Connection management ─────────────────────────────────────────────────

    @property
    def server_url(self) -> str:
        """The WebSocket URL of the remote agent server."""
        return self._server_url

    @property
    def connection_state(self) -> str:
        """Current WebSocket connection state."""
        return self._connection_state

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._connection_state == ConnectionState.CONNECTED

    async def connect(self) -> None:
        """
        Establish the WebSocket connection to the agent server.

        If already connected, this is a no-op.  On failure, the
        connection state is set to DISCONNECTED and the method
        returns normally (no exception).

        Requires the ``websockets`` library (optional dependency).
        """
        if self._connection_state == ConnectionState.CONNECTED:
            return

        self._connection_state = ConnectionState.CONNECTING

        try:
            import websockets
        except ImportError:
            logger.error(
                "[RemoteConversation] 'websockets' package not installed. "
                "Install with: pip install websockets"
            )
            self._connection_state = ConnectionState.DISCONNECTED
            return

        try:
            # Build the URL with conversation_id as a query parameter
            url = f"{self._server_url}?conversation_id={self._id}"

            self._ws = await asyncio.wait_for(
                websockets.connect(url),
                timeout=30.0,
            )
            self._connection_state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0

            logger.info(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Connected to {self._server_url}"
            )

            # Start the listener task
            self._start_listener()

        except Exception as e:
            logger.error(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Connection failed: {e}"
            )
            self._connection_state = ConnectionState.DISCONNECTED
            self._ws = None
            # Start reconnection loop
            self._start_reconnect()

    async def disconnect(self) -> None:
        """
        Close the WebSocket connection gracefully.
        """
        self._connection_state = ConnectionState.DISCONNECTED

        # Cancel background tasks
        if self._listener_task is not None:
            self._listener_task.cancel()
            self._listener_task = None

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info(
            f"[RemoteConversation:{self._id[:8]}] Disconnected"
        )

    def _start_listener(self) -> None:
        """Start the background task that listens for incoming events."""
        if self._listener_task is not None and not self._listener_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
            self._listener_task = loop.create_task(self._listen_loop())
        except RuntimeError:
            pass

    def _start_reconnect(self) -> None:
        """Start the background reconnection task."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
            self._reconnect_task = loop.create_task(self._reconnect_loop())
        except RuntimeError:
            pass

    async def _listen_loop(self) -> None:
        """
        Background loop that reads messages from the WebSocket and
        dispatches them as events.
        """
        if self._ws is None:
            return

        try:
            async for raw_message in self._ws:
                try:
                    event = self._parse_message(raw_message)
                    if event is not None:
                        self._on_event_received(event)
                except Exception as e:
                    logger.warning(
                        f"[RemoteConversation:{self._id[:8]}] "
                        f"Failed to parse message: {e}"
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Listener error: {e}"
            )
            # Connection lost — trigger reconnection
            if self._connection_state == ConnectionState.CONNECTED:
                self._connection_state = ConnectionState.DISCONNECTED
                self._ws = None
                self._start_reconnect()

    async def _reconnect_loop(self) -> None:
        """
        Background loop that attempts to reconnect with exponential
        backoff.
        """
        while self._reconnect_attempts < self._max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = min(
                self._base_reconnect_delay_s * (2 ** (self._reconnect_attempts - 1)),
                self._max_reconnect_delay_s,
            )

            logger.info(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Reconnecting in {delay:.1f}s "
                f"(attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})"
            )

            await asyncio.sleep(delay)

            try:
                await self.connect()
                if self.is_connected:
                    return  # Reconnected successfully
            except Exception as e:
                logger.warning(
                    f"[RemoteConversation:{self._id[:8]}] "
                    f"Reconnect attempt failed: {e}"
                )

        logger.error(
            f"[RemoteConversation:{self._id[:8]}] "
            f"Max reconnection attempts ({self._max_reconnect_attempts}) reached"
        )

    # ── Message parsing ───────────────────────────────────────────────────────

    def _parse_message(self, raw: Any) -> Optional[Event]:
        """
        Parse a raw WebSocket message into an Event.

        Handles both JSON strings and binary payloads.
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        if isinstance(raw, str):
            try:
                return deserialize(raw)
            except Exception:
                # Try parsing as a generic control message
                try:
                    data = json.loads(raw)
                    return self._parse_control_message(data)
                except Exception:
                    return None

        return None

    def _parse_control_message(self, data: Dict[str, Any]) -> Optional[Event]:
        """
        Parse a control message (not an event) from the server.

        Control messages include state updates, connection confirmations,
        etc.
        """
        msg_type = data.get("type", "")

        if msg_type == "state_update":
            new_state = data.get("new_state", "")
            try:
                return ConversationStateUpdateEvent(
                    old_state=data.get("old_state", ""),
                    new_state=new_state,
                )
            except Exception:
                return None

        if msg_type == "interrupt":
            return InterruptEvent(reason=data.get("reason", ""))

        if msg_type == "pause":
            return PauseEvent(reason=data.get("reason", ""))

        if msg_type == "resume":
            return ResumeTranscriptEvent(reason=data.get("reason", ""))

        return None

    # ── Event handling ────────────────────────────────────────────────────────

    def _on_event_received(self, event: Event) -> None:
        """
        Handle an event received from the remote server.

        Buffers the event and notifies listeners.
        """
        self._event_buffer.append(event)

        # Update local state based on the event
        if isinstance(event, ConversationStateUpdateEvent):
            try:
                new_status = ExecutionStatus(event.new_state)
                self._state.try_set_status(new_status)
            except (ValueError, KeyError):
                pass

        # Notify listeners
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                logger.warning(
                    f"[RemoteConversation:{self._id[:8]}] "
                    f"Listener error: {e}"
                )

    def add_listener(self, listener: Callable[[Event], None]) -> None:
        """
        Register a callback to be invoked when events arrive from
        the server.

        Args:
            listener: A callable that takes a single Event argument.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Event], None]) -> None:
        """Remove a previously registered listener."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ── Sending commands ──────────────────────────────────────────────────────

    async def _send_command(self, command: Dict[str, Any]) -> bool:
        """
        Send a JSON command to the remote server via WebSocket.

        Args:
            command: Dict to serialize as JSON and send.

        Returns:
            ``True`` if the command was sent, ``False`` if not connected.
        """
        if not self.is_connected or self._ws is None:
            logger.warning(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Cannot send command: not connected"
            )
            return False

        try:
            payload = json.dumps(command)
            await self._ws.send(payload)
            return True
        except Exception as e:
            logger.error(
                f"[RemoteConversation:{self._id[:8]}] "
                f"Failed to send command: {e}"
            )
            return False

    # ── Abstract method implementations ───────────────────────────────────────

    def _do_send_message(self, message: str, **kwargs: Any) -> Any:
        """
        Send a user message to the remote agent (synchronous wrapper).

        Creates a MessageEvent locally and sends a ``send_message``
        command to the server.
        """
        # Record locally
        event = MessageEvent(
            content=message,
            role="user",
            source="user",
        )
        self._event_buffer.append(event)

        # Send to server (fire and forget for sync variant)
        command = {
            "type": "send_message",
            "conversation_id": self._id,
            "message": message,
            "timestamp": time.time(),
        }

        # Try to send; if not connected, the message is buffered
        # locally and will be sent upon reconnection
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_command(command))
        except RuntimeError:
            # No running loop — try sync approach
            try:
                asyncio.run(self._send_command(command))
            except Exception:
                pass

        return {"event_id": event.id, "status": "sent", "buffered": not self.is_connected}

    async def _do_asend_message(self, message: str, **kwargs: Any) -> Any:
        """
        Send a user message to the remote agent (async).
        """
        event = MessageEvent(
            content=message,
            role="user",
            source="user",
        )
        self._event_buffer.append(event)

        command = {
            "type": "send_message",
            "conversation_id": self._id,
            "message": message,
            "timestamp": time.time(),
        }

        sent = await self._send_command(command)

        return {"event_id": event.id, "status": "sent" if sent else "buffered"}

    def _do_run(self, prompt: str, **kwargs: Any) -> Any:
        """
        Start the agent run on the remote server (synchronous).

        Sends a ``run`` command and waits for completion.
        """
        return asyncio.run(self._do_arun(prompt, **kwargs))

    async def _do_arun(self, prompt: str, **kwargs: Any) -> Any:
        """
        Start the agent run on the remote server (async).

        Sends a ``run`` command and waits for the conversation to reach
        a terminal state.
        """
        # Connect if not already connected
        if not self.is_connected:
            await self.connect()

        # Record locally
        prompt_event = MessageEvent(
            content=prompt,
            role="user",
            source="user",
        )
        self._event_buffer.append(prompt_event)

        # Send run command
        command = {
            "type": "run",
            "conversation_id": self._id,
            "prompt": prompt,
            "max_steps": kwargs.get("max_steps", 30),
            "timestamp": time.time(),
        }

        sent = await self._send_command(command)

        if not sent:
            return {
                "conversation_id": self._id,
                "status": "disconnected",
                "error": "Could not send run command to server",
            }

        # Wait for the conversation to reach a terminal state
        # or for the cancellation token to be triggered
        start_time = time.monotonic()
        timeout = kwargs.get("timeout", 3600)

        while not self._state.is_terminal:
            self._cancellation_token.raise_if_cancelled()

            elapsed = time.monotonic() - start_time
            if elapsed > timeout:
                self._state.error_message = "Run timed out waiting for remote completion"
                try:
                    self._state.set_status(ExecutionStatus.ERROR)
                except ValueError:
                    pass
                break

            await asyncio.sleep(0.5)

        return {
            "conversation_id": self._id,
            "status": self._state.execution_status.value,
            "duration_s": round(time.monotonic() - start_time, 2),
            "events_received": len(self._event_buffer),
        }

    def _do_fork(self, new_id: str) -> "RemoteConversation":
        """
        Create a fork of this conversation on the remote server.

        Sends a ``fork`` command and creates a new RemoteConversation
        for the forked instance.
        """
        forked = RemoteConversation(
            conversation_id=new_id,
            server_url=self._server_url,
            confirmation_policy=self._state.confirmation_policy,
            security_analyzer=self._state.security_analyzer,
            stuck_detector=StuckDetector(
                window_size=self._stuck_detector.window_size,
                repeat_threshold=self._stuck_detector.repeat_threshold,
                monologue_threshold=self._stuck_detector.monologue_threshold,
            ),
            hook_manager=self._hook_manager,
            max_reconnect_attempts=self._max_reconnect_attempts,
            base_reconnect_delay_s=self._base_reconnect_delay_s,
            max_reconnect_delay_s=self._max_reconnect_delay_s,
            event_buffer_size=self._event_buffer_size,
        )

        # Copy buffered events to the fork
        for event in self._event_buffer:
            forked._event_buffer.append(event)

        # Send fork command to the server
        command = {
            "type": "fork",
            "conversation_id": self._id,
            "new_conversation_id": new_id,
            "timestamp": time.time(),
        }

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_command(command))
        except RuntimeError:
            try:
                asyncio.run(self._send_command(command))
            except Exception:
                pass

        forked._title = self._title

        return forked

    def _do_interrupt(self, reason: str) -> None:
        """
        Send an interrupt command to the remote server.
        """
        command = {
            "type": "interrupt",
            "conversation_id": self._id,
            "reason": reason,
            "timestamp": time.time(),
        }

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_command(command))
        except RuntimeError:
            try:
                asyncio.run(self._send_command(command))
            except Exception:
                pass

        # Record locally
        event = InterruptEvent(reason=reason)
        self._event_buffer.append(event)

    def _do_resume(self, **kwargs: Any) -> None:
        """
        Send a resume command to the remote server.
        """
        command = {
            "type": "resume",
            "conversation_id": self._id,
            "timestamp": time.time(),
        }

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_command(command))
        except RuntimeError:
            try:
                asyncio.run(self._send_command(command))
            except Exception:
                pass

        # Record locally
        event = ResumeTranscriptEvent(reason="Resumed by user")
        self._event_buffer.append(event)

    # ── Override: get events ──────────────────────────────────────────────────

    def get_events(self) -> List[Event]:
        """Return all buffered events from the remote server."""
        return list(self._event_buffer)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the connection and clean up resources."""
        super().close()

        # Cancel background tasks
        if self._listener_task is not None:
            self._listener_task.cancel()
            self._listener_task = None

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        # Close WebSocket synchronously (best effort)
        if self._ws is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._ws.close())
            except RuntimeError:
                try:
                    asyncio.run(self._ws.close())
                except Exception:
                    pass
            self._ws = None

        self._connection_state = ConnectionState.DISCONNECTED

    def __repr__(self) -> str:
        return (
            f"<RemoteConversation id={self._id[:8]} "
            f"status={self._state.execution_status.value} "
            f"connection={self._connection_state} "
            f"events={len(self._event_buffer)}>"
        )
