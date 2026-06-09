"""ManusClaw Windows System Tray Hub.

Provides a system tray icon with menu actions for interacting with the ManusClaw
server. Connects via WebSocket and shows desktop notifications for new messages.

Requirements:
    pip install pystray Pillow websockets
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Callable, Optional

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "Missing dependencies. Run:\n"
        "  pip install pystray Pillow\n"
        "Then relaunch this script."
    )

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
_DEVICE_ID: str = os.getenv("MANUSCLAW_DEVICE_ID", "") or f"win-{uuid.uuid4().hex[:8]}"
_CHAT_URL: str = f"http://localhost:8765/chat"
_CONFIG_DIR: Path = Path.home() / ".manusclaw"

_RECONNECT_BASE_DELAY: float = 1.0
_RECONNECT_MAX_DELAY: float = 60.0
_MAX_RECONNECT_ATTEMPTS: int = 50

# ─── State ─────────────────────────────────────────────────────────────────────

_connected: bool = False
_ws: Optional[websockets.WebSocketClientProtocol] = None  # type: ignore[assignment]
_stop_event = threading.Event()
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None


# ─── Tray Icon ─────────────────────────────────────────────────────────────────

def _create_icon_image() -> Image.Image:
    """Generate a simple 64x64 green claw icon programmatically."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw a stylized "M" shape in green
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(0, 180, 100, 255))
    draw.line([(16, 48), (24, 16), (32, 36), (40, 16), (48, 48)],
              fill=(255, 255, 255, 255), width=4)

    return img


def _notification(text: str, title: str = "ManusClaw") -> None:
    """Show a Windows desktop notification via the tray icon."""
    if _tray_icon:
        _tray_icon.notify(text, title)


# ─── WebSocket Client ──────────────────────────────────────────────────────────

async def _ws_loop(on_message: Callable[[dict], None]) -> None:
    """Persistent WebSocket loop with exponential-backoff reconnection."""
    global _connected, _ws

    delay = _RECONNECT_BASE_DELAY
    attempt = 0

    while not _stop_event.is_set():
        ws_url = f"{_SERVER_URL}/ws/chat/{_DEVICE_ID}"
        if _API_KEY:
            ws_url += f"?api_key={_API_KEY}"

        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                _ws = ws
                _connected = True
                delay = _RECONNECT_BASE_DELAY
                attempt = 0
                _tray_icon.icon = _create_icon_image()  # force refresh
                _tray_icon.title = "ManusClaw — Connected"

                _notification("Connected to ManusClaw server")

                async for raw in ws:
                    if _stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                        _handle_ws_message(msg, on_message)
                    except json.JSONDecodeError:
                        pass

        except (
            websockets.ConnectionClosed,
            websockets.InvalidURI,
            OSError,
            ConnectionRefusedError,
        ) as exc:
            _connected = False
            _tray_icon.title = "ManusClaw — Disconnected"
            if attempt < _MAX_RECONNECT_ATTEMPTS:
                _notification(f"Disconnected: {exc}. Reconnecting in {delay:.0f}s…",
                             "ManusClaw")
            else:
                _notification("Disconnected. Max retries reached.", "ManusClaw")
                break

            _stop_event.wait(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)
            attempt += 1


def _handle_ws_message(msg: dict, on_message: Callable[[dict], None]) -> None:
    """Route incoming WebSocket messages."""
    msg_type = msg.get("type", "")

    if msg_type == "connected":
        print(f"[Hub] Server connected: {msg.get('session_id', '?')}")

    elif msg_type == "agent_done":
        output = msg.get("output", "")[:200]
        _notification(output, "ManusClaw — Agent Done")

    elif msg_type == "agent_error":
        error = msg.get("error", "Unknown error")
        _notification(error, "ManusClaw — Error")

    elif msg_type == "agent_start":
        prompt = msg.get("prompt", "")[:60]
        _notification(f"Processing: {prompt}", "ManusClaw — Working")

    # Forward all messages to the callback
    on_message(msg)


async def _send_prompt(prompt: str) -> None:
    """Send a chat prompt to the server over WebSocket."""
    global _ws
    if _ws and _connected:
        await _ws.send(json.dumps({"type": "prompt", "prompt": prompt, "mode": "build"}))


# ─── Menu Actions ──────────────────────────────────────────────────────────────

def _open_chat(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Open the ManusClaw web chat in the default browser."""
    webbrowser.open(_CHAT_URL)


def _start_node(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Start a compute node registration (placeholder action)."""
    _notification("Node registration initiated — feature coming soon", "ManusClaw")
    print("[Hub] Start Node clicked (not yet implemented)")


def _open_settings(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Open the ManusClaw configuration directory in Explorer."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(_CONFIG_DIR))  # type: ignore[attr-defined]
    else:
        webbrowser.open(f"file://{_CONFIG_DIR}")


def _exit_app(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Stop the WebSocket client and exit the tray application."""
    global _connected
    _stop_event.set()
    _connected = False
    icon.stop()


# ─── Tray Setup ────────────────────────────────────────────────────────────────

_tray_icon: Optional[pystray.Icon] = None


def _build_menu() -> pystray.Menu:
    """Construct the system tray context menu."""
    return pystray.Menu(
        pystray.MenuItem("Open Chat", _open_chat, default=True),
        pystray.MenuItem("Start Node", _start_node),
        pystray.MenuItem("Settings", _open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", _exit_app),
    )


def _on_message(msg: dict) -> None:
    """Default message handler — prints to stdout."""
    print(f"[Hub] Message: {json.dumps(msg, default=str)[:200]}")


# ─── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: start the WebSocket loop in a background thread, then run the tray icon."""
    global _tray_icon

    # Start async WebSocket loop in a daemon thread
    def _run_loop():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_ws_loop(_on_message))
        except Exception as exc:
            print(f"[Hub] WebSocket loop exited: {exc}")
        finally:
            _loop.close()

    _loop_thread = threading.Thread(target=_run_loop, daemon=True)
    _loop_thread.start()

    # Create and run the tray icon (blocking call)
    _tray_icon = pystray.Icon(
        name="ManusClaw Hub",
        icon=_create_icon_image(),
        menu=_build_menu(),
        title="ManusClaw — Starting…",
    )

    print(f"[Hub] Device ID: {_DEVICE_ID}")
    print(f"[Hub] Server URL: {_SERVER_URL}")
    print("[Hub] System tray running. Right-click for options.")

    try:
        _tray_icon.run()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        print("[Hub] Shutting down.")


if __name__ == "__main__":
    main()
