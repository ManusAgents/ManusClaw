"""ManusClaw Mobile Node Client.

Reference Python WebSocket client that connects to the ManusClaw server as a
mobile node. Handles device registration, Canvas updates, voice forwarding,
and screen capture transmission.

Usage:
    python node_client.py [--server ws://localhost:8765] [--device-id my-device]

Requirements:
    pip install websockets
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import websockets
except ImportError:
    sys.exit(
        "websockets not installed. Run:\n"
        "  pip install websockets\n"
    )

# ─── Configuration ────────────────────────────────────────────────────────────

_SERVER_URL: str = os.getenv("MANUSCLAW_SERVER_URL", "ws://localhost:8765")
_API_KEY: str = os.getenv("MANUSCLAW_API_KEY", "")
_DEVICE_ID: str = os.getenv("MANUSCLAW_DEVICE_ID", "") or f"mobile-{uuid.uuid4().hex[:8]}"
_DEVICE_TYPE: str = os.getenv("MANUSCLAW_DEVICE_TYPE", "mobile")
_CAPABILITIES: list[str] = os.getenv(
    "MANUSCLAW_CAPABILITIES", "voice,screen"
).split(",")

_RECONNECT_BASE_DELAY: float = 1.0
_RECONNECT_MAX_DELAY: float = 60.0
_JITTER_FACTOR: float = 0.3
_PING_INTERVAL: float = 15.0


# ─── Data Models ────────────────────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Device registration information sent to the ManusClaw server."""
    device_id: str = field(default_factory=lambda: _DEVICE_ID)
    device_type: str = _DEVICE_TYPE
    capabilities: list[str] = field(default_factory=lambda: list(_CAPABILITIES))
    os_name: str = "unknown"
    os_version: str = ""
    app_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "capabilities": self.capabilities,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "app_version": self.app_version,
        }


@dataclass
class CanvasUpdate:
    """Incoming Canvas A2UI update from the server."""
    message_type: str = ""
    session_id: str = ""
    components: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanvasUpdate:
        return cls(
            message_type=data.get("message_type", ""),
            session_id=data.get("session_id", ""),
            components=data.get("components", []),
            timestamp=data.get("timestamp", time.time()),
        )


# ─── Detection Helpers ──────────────────────────────────────────────────────────

def _detect_os() -> tuple[str, str]:
    """Detect the operating system name and version."""
    import platform

    system = platform.system().lower()
    version = platform.version() or ""

    if system == "darwin":
        release = platform.mac_ver()[0] or ""
        return "ios" if _is_ios_device() else "macos", release
    elif system == "linux":
        return "android" if _is_android_device() else "linux", version
    elif system == "windows":
        return "windows", platform.win32_ver()[1] or version

    return system, version


def _is_ios_device() -> bool:
    """Check if running on iOS via Pythonista or similar."""
    return "iOS" in os.getenv("PLATFORM", "") or hasattr(sys, "_IOS")


def _is_android_device() -> bool:
    """Check if running on Android via Termux, QPython, or similar."""
    return "ANDROID" in os.getenv("PLATFORM", "") or Path("/system/build.prop").exists()


# ─── Voice Forwarder ────────────────────────────────────────────────────────────

class VoiceForwarder:
    """Handles recording and forwarding voice data to the server.

    This is a reference implementation. In production, integrate with
    platform-specific audio APIs (AVFoundation on iOS, AudioRecord on Android).
    """

    def __init__(self) -> None:
        self._recording: bool = False
        self._chunk_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

    def is_available(self) -> bool:
        """Check if voice recording is available on this platform."""
        try:
            import sounddevice  # noqa: F401 — available on desktop
            return True
        except ImportError:
            return False

    async def start_recording(self, sample_rate: int = 16000) -> None:
        """Start recording audio and queuing chunks for forwarding."""
        self._recording = True
        print("[Voice] Recording started (stub — no audio hardware)")

        # In production: use platform-specific audio API to read chunks
        # and put them into self._chunk_queue

    def stop_recording(self) -> None:
        """Stop recording audio."""
        self._recording = False
        print("[Voice] Recording stopped")

    async def get_next_chunk(self, timeout: float = 1.0) -> Optional[bytes]:
        """Get the next audio chunk from the queue."""
        try:
            return await asyncio.wait_for(self._chunk_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


# ─── Screen Capture ─────────────────────────────────────────────────────────────

class ScreenCapture:
    """Handles capturing screen data and forwarding to the server.

    Reference implementation — in production, use platform-specific APIs.
    """

    def __init__(self) -> None:
        self._capturing: bool = False
        self._frame_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=30)

    def is_available(self) -> bool:
        """Check if screen capture is available."""
        try:
            from PIL import ImageGrab  # noqa: F401 — desktop only
            return True
        except ImportError:
            return False

    async def capture_frame(self) -> Optional[dict[str, Any]]:
        """Capture a single screen frame and return as base64."""
        # Stub: in production, capture using platform APIs
        return None

    def stop(self) -> None:
        """Stop screen capture."""
        self._capturing = False


# ─── Node Client ───────────────────────────────────────────────────────────────

class ManusClawNodeClient:
    """WebSocket client that registers as a mobile node with the ManusClaw server.

    Handles:
    - Device registration and heartbeat
    - Canvas update reception
    - Voice data forwarding
    - Screen capture forwarding
    - Automatic reconnection with exponential backoff
    """

    def __init__(
        self,
        server_url: str = _SERVER_URL,
        device_id: str = _DEVICE_ID,
        api_key: str = _API_KEY,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.device_id = device_id
        self.api_key = api_key

        os_name, os_version = _detect_os()
        self.device_info = DeviceInfo(
            device_id=device_id,
            device_type=_DEVICE_TYPE,
            capabilities=list(_CAPABILITIES),
            os_name=os_name,
            os_version=os_version,
        )

        self._ws: Optional[websockets.WebSocketClientProtocol] = None  # type: ignore[assignment]
        self._connected: bool = False
        self._running: bool = False
        self._reconnect_delay: float = _RECONNECT_BASE_DELAY

        self.voice = VoiceForwarder()
        self.screen = ScreenCapture()
        self._on_canvas_update: Optional[list] = []
        self._on_command_callbacks: list = []

    @property
    def connected(self) -> bool:
        return self._connected

    # ─── Callbacks ─────────────────────────────────────────────────────────

    def on_canvas_update(self, callback) -> None:
        """Register a callback for Canvas A2UI updates.

        Callback signature: ``callback(update: CanvasUpdate) -> None``
        """
        self._on_canvas_update.append(callback)

    def on_command(self, callback) -> None:
        """Register a callback for server-pushed commands.

        Callback signature: ``callback(command: dict) -> None``
        """
        self._on_command_callbacks.append(callback)

    # ─── Connection ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket connection loop with reconnection."""
        self._running = True

        while self._running:
            ws_url = f"{self.server_url}/ws/nodes/{self.device_id}"
            if self.api_key:
                ws_url += f"?api_key={self.api_key}"

            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_delay = _RECONNECT_BASE_DELAY

                    await self._register()
                    print(f"[Node] Connected as {self.device_info.device_type} "
                          f"({', '.join(self.device_info.capabilities)})")

                    await self._message_loop()

            except (
                websockets.ConnectionClosed,
                websockets.InvalidURI,
                OSError,
                ConnectionRefusedError,
            ) as exc:
                self._connected = False
                self._ws = None
                print(f"[Node] Disconnected: {exc}")

            if not self._running:
                break

            # Exponential backoff with jitter
            jitter = self._reconnect_delay * _JITTER_FACTOR * (random.random() * 2 - 1)
            wait = max(0.1, self._reconnect_delay + jitter)
            print(f"[Node] Reconnecting in {wait:.1f}s…")
            await asyncio.sleep(wait)
            self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX_DELAY)

    async def disconnect(self) -> None:
        """Stop the connection loop and close the WebSocket."""
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        print("[Node] Disconnected")

    # ─── Registration ──────────────────────────────────────────────────────

    async def _register(self) -> None:
        """Send device registration message to the server."""
        if not self._ws:
            return

        await self._ws.send(json.dumps({
            "type": "register",
            "device": self.device_info.to_dict(),
        }))

    # ─── Message Loop ─────────────────────────────────────────────────────

    async def _message_loop(self) -> None:
        """Process incoming WebSocket messages."""
        if not self._ws:
            return

        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "connected":
                print(f"[Node] Server acknowledged: {msg.get('session_id', '?')}")

            elif msg_type == "canvas_update":
                update = CanvasUpdate.from_dict(msg)
                for cb in self._on_canvas_update:
                    try:
                        cb(update)
                    except Exception as exc:
                        print(f"[Node] Canvas callback error: {exc}")

            elif msg_type == "command":
                for cb in self._on_command_callbacks:
                    try:
                        cb(msg)
                    except Exception as exc:
                        print(f"[Node] Command callback error: {exc}")

            elif msg_type == "ping":
                await self._send({"type": "pong"})

    async def _send(self, data: dict[str, Any]) -> None:
        """Send a JSON message to the server."""
        if self._ws and self._connected:
            await self._ws.send(json.dumps(data))

    # ─── Voice ─────────────────────────────────────────────────────────────

    async def send_voice_data(self, audio_chunk: bytes, sample_rate: int = 16000) -> None:
        """Send a voice audio chunk to the server for processing."""
        import base64

        encoded = base64.b64encode(audio_chunk).decode("ascii")
        await self._send({
            "type": "voice_data",
            "device_id": self.device_id,
            "audio": encoded,
            "sample_rate": sample_rate,
            "timestamp": time.time(),
        })

    # ─── Screen Capture ────────────────────────────────────────────────────

    async def send_screen_capture(self, frame_b64: str, width: int = 0, height: int = 0) -> None:
        """Send a screen capture frame to the server."""
        await self._send({
            "type": "screen_capture",
            "device_id": self.device_id,
            "frame": frame_b64,
            "width": width,
            "height": height,
            "timestamp": time.time(),
        })

    # ─── Chat ──────────────────────────────────────────────────────────────

    async def send_chat(self, message: str, mode: str = "build") -> None:
        """Send a chat message through this node's session."""
        await self._send({
            "type": "prompt",
            "prompt": message,
            "mode": mode,
        })


# ─── Demo Callbacks ────────────────────────────────────────────────────────────

def _on_canvas(update: CanvasUpdate) -> None:
    """Handle incoming Canvas updates."""
    n_components = len(update.components)
    print(f"[Node] Canvas update: {n_components} component(s) "
          f"in session {update.session_id}")


def _on_command(command: dict) -> None:
    """Handle server-pushed commands."""
    cmd_name = command.get("command", "")
    print(f"[Node] Command received: {cmd_name}")
    if cmd_name == "ping":
        print("[Node] Responding to server ping")


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI arguments and run the node client."""
    parser = argparse.ArgumentParser(
        description="ManusClaw Mobile Node Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default=_SERVER_URL,
                        help="WebSocket URL of ManusClaw server")
    parser.add_argument("--device-id", default=_DEVICE_ID,
                        help="Unique device identifier")
    parser.add_argument("--api-key", default=_API_KEY,
                        help="API key for authentication")

    args = parser.parse_args()

    client = ManusClawNodeClient(
        server_url=args.server,
        device_id=args.device_id,
        api_key=args.api_key,
    )
    client.on_canvas_update(_on_canvas)
    client.on_command(_on_command)

    print(f"[Node] Starting ManusClaw node client")
    print(f"[Node] Device ID:   {client.device_id}")
    print(f"[Node] Device Type: {client.device_info.device_type}")
    print(f"[Node] Server:      {client.server_url}")
    print(f"[Node] Capabilities: {', '.join(client.device_info.capabilities)}")
    print("[Node] Press Ctrl+C to stop")

    try:
        asyncio.run(client.connect())
    except KeyboardInterrupt:
        print("\n[Node] Stopping…")
        asyncio.run(client.disconnect())


if __name__ == "__main__":
    main()
