"""ManusClaw macOS Menu Bar App.

Provides a native macOS menu bar application for interacting with the ManusClaw
server. Connects via WebSocket, shows connection status, and offers quick access
to chat, canvas, and voice features.

Requirements:
    pip install rumps websockets
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
    import rumps
except ImportError:
    sys.exit(
        "rumps not installed. Run:\n"
        "  pip install rumps\n"
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

_SERVER_URL: str = os.getenv("MANUSCLAW_SERVER_URL", "http://localhost:8765")
_WS_URL: str = os.getenv("MANUSCLAW_WS_URL", "ws://localhost:8765")
_API_KEY: str = os.getenv("MANUSCLAW_API_KEY", "")
_DEVICE_ID: str = os.getenv("MANUSCLAW_DEVICE_ID", "") or f"mac-{uuid.uuid4().hex[:8]}"
_CHAT_URL: str = f"{_SERVER_URL}/chat"
_CANVAS_URL: str = f"{_SERVER_URL}/canvas"
_CONFIG_DIR: Path = Path.home() / ".manusclaw"

_HEALTHCHECK_INTERVAL: float = 30.0
_RECONNECT_BASE_DELAY: float = 1.0
_RECONNECT_MAX_DELAY: float = 60.0

# ─── State ─────────────────────────────────────────────────────────────────────

_connected: bool = False
_stop_event = threading.Event()
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None


# ─── WebSocket Client ──────────────────────────────────────────────────────────

async def _ws_loop(on_message: Callable[[dict], None]) -> None:
    """Persistent WebSocket loop with exponential-backoff reconnection."""
    global _connected

    delay = _RECONNECT_BASE_DELAY

    while not _stop_event.is_set():
        ws_url = f"{_WS_URL}/ws/chat/{_DEVICE_ID}"
        if _API_KEY:
            ws_url += f"?api_key={_API_KEY}"

        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                _connected = True
                delay = _RECONNECT_BASE_DELAY

                # Notify menu bar of connection
                try:
                    _app.title = "✓ ManusClaw"
                    _app.menu["Status"].title = "● Connected"
                except Exception:
                    pass

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
        ):
            _connected = False
            try:
                _app.title = "✗ ManusClaw"
                _app.menu["Status"].title = "○ Disconnected"
            except Exception:
                pass

            _stop_event.wait(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)


def _handle_ws_message(msg: dict, on_message: Callable[[dict], None]) -> None:
    """Route incoming WebSocket messages."""
    msg_type = msg.get("type", "")

    if msg_type == "connected":
        print(f"[MenuBar] Server connected: {msg.get('session_id', '?')}")

    elif msg_type == "agent_done":
        output = msg.get("output", "")[:200]
        try:
            rumps.notification("ManusClaw", "Agent Complete", output)
        except Exception:
            print(f"[MenuBar] Agent done: {output}")

    elif msg_type == "agent_error":
        error = msg.get("error", "Unknown error")
        try:
            rumps.notification("ManusClaw", "Error", error)
        except Exception:
            print(f"[MenuBar] Error: {error}")

    on_message(msg)


async def _send_prompt(prompt: str) -> None:
    """Send a prompt to the WebSocket server."""
    global _ws_ref
    if _ws_ref and _connected:
        await _ws_ref.send(json.dumps({
            "type": "prompt",
            "prompt": prompt,
            "mode": "build",
        }))


# WebSocket reference for sending messages
_ws_ref: Optional[websockets.WebSocketClientProtocol] = None  # type: ignore[assignment]


# Patch the ws loop to store the reference
_original_ws_loop = _ws_loop


async def _ws_loop_with_ref(on_message: Callable[[dict], None]) -> None:
    """WebSocket loop that also stores the reference for sending."""
    global _ws_ref, _connected

    delay = _RECONNECT_BASE_DELAY

    while not _stop_event.is_set():
        ws_url = f"{_WS_URL}/ws/chat/{_DEVICE_ID}"
        if _API_KEY:
            ws_url += f"?api_key={_API_KEY}"

        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                _ws_ref = ws
                _connected = True
                delay = _RECONNECT_BASE_DELAY
                try:
                    _app.title = "✓ ManusClaw"
                    _app.menu["Status"].title = "● Connected"
                except Exception:
                    pass

                async for raw in ws:
                    if _stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                        _handle_ws_message(msg, on_message)
                    except json.JSONDecodeError:
                        pass

        except Exception:
            _connected = False
            _ws_ref = None
            try:
                _app.title = "✗ ManusClaw"
                _app.menu["Status"].title = "○ Disconnected"
            except Exception:
                pass

            _stop_event.wait(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)


# ─── Health Check ───────────────────────────────────────────────────────────────

async def _healthcheck_loop() -> None:
    """Periodically ping /healthz to check server availability."""
    import urllib.request

    while not _stop_event.is_set():
        try:
            url = f"{_SERVER_URL}/healthz"
            req = urllib.request.Request(url, headers={"X-API-Key": _API_KEY} if _API_KEY else {})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    if not _connected:
                        print("[MenuBar] Server is up but WebSocket not connected")
        except Exception:
            pass
        _stop_event.wait(_HEALTHCHECK_INTERVAL)


# ─── Menu Actions ──────────────────────────────────────────────────────────────

def _on_chat(sender: rumps.MenuItem) -> None:
    """Open the web chat in the default browser."""
    webbrowser.open(_CHAT_URL)


def _on_canvas(sender: rumps.MenuItem) -> None:
    """Open the canvas viewer in the default browser."""
    webbrowser.open(_CANVAS_URL)


def _on_voice_toggle(sender: rumps.MenuItem) -> None:
    """Toggle voice input (placeholder)."""
    sender.state = not sender.state
    state_str = "ON" if sender.state else "OFF"
    try:
        rumps.notification("ManusClaw", "Voice", f"Voice input {state_str}")
    except Exception:
        print(f"[MenuBar] Voice toggled: {state_str}")


def _on_preferences(sender: rumps.MenuItem) -> None:
    """Open the config directory in Finder."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.system(f"open {_CONFIG_DIR}")


def _on_quit(sender: rumps.MenuItem) -> None:
    """Stop all loops and quit."""
    global _connected
    _stop_event.set()
    _connected = False
    rumps.quit_application()


def _on_quick_chat(sender: rumps.MenuItem) -> None:
    """Show a text input dialog for quick chat."""
    window = rumps.Window(
        title="Quick Chat",
        message="Send a message to ManusClaw:",
        default_text="",
        ok="Send",
        cancel="Cancel",
    )
    response = window.run()
    if response.clicked:
        prompt = response.text.strip()
        if prompt:
            print(f"[MenuBar] Quick chat: {prompt[:80]}")
            # Schedule the send in the async loop
            if _loop and _loop.is_running():
                _loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(_send_prompt(prompt), loop=_loop)
                )


def _on_message(msg: dict) -> None:
    """Default message handler."""
    msg_type = msg.get("type", "")
    if msg_type not in ("connected", "ping", "pong"):
        print(f"[MenuBar] {msg_type}: {json.dumps(msg, default=str)[:120]}")


# ─── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: build the rumps app and run it."""
    global _app

    _app = rumps.App(
        "ManusClaw",
        title="○ ManusClaw",
        quit_button=None,
    )

    _app.menu = [
        rumps.MenuItem("Status", callback=lambda _: None),
        rumps.MenuItem("Chat", callback=_on_chat),
        rumps.MenuItem("Canvas", callback=_on_canvas),
        rumps.MenuItem("Quick Chat…", callback=_on_quick_chat),
        rumps.MenuItem("Voice", callback=_on_voice_toggle),
        None,  # separator
        rumps.MenuItem("Preferences", callback=_on_preferences),
        rumps.MenuItem("Quit", callback=_on_quit),
    ]

    # Start async loops in background thread
    def _run_async():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(asyncio.gather(
                _ws_loop_with_ref(_on_message),
                _healthcheck_loop(),
            ))
        except Exception as exc:
            print(f"[MenuBar] Async loop exited: {exc}")
        finally:
            _loop.close()

    _loop_thread = threading.Thread(target=_run_async, daemon=True)
    _loop_thread.start()

    print(f"[MenuBar] Device ID: {_DEVICE_ID}")
    print(f"[MenuBar] Server: {_SERVER_URL}")

    _app.run()


if __name__ == "__main__":
    main()
