# ManusClaw Windows Hub

System tray companion app for ManusClaw on Windows.

## Prerequisites

- Python 3.11+
- `pystray` — system tray icon library
- `Pillow` — image handling for tray icons
- `websockets` — WebSocket client for server connection

```bash
pip install pystray Pillow websockets
```

## Usage

```bash
python hub.py
```

### Tray Menu Actions

| Menu Item          | Description                                      |
|--------------------|--------------------------------------------------|
| **Open Chat**      | Opens the ManusClaw web chat in your browser      |
| **Start Node**     | Registers this machine as a compute node         |
| **Settings**       | Opens the ManusClaw configuration folder          |
| **Exit**           | Closes the tray app and all background tasks      |

## Configuration

The hub reads its connection settings from the environment or a local `.env` file:

| Variable               | Default        | Description                          |
|------------------------|----------------|--------------------------------------|
| `MANUSCLAW_SERVER_URL` | `ws://localhost:8765` | WebSocket URL of ManusClaw server |
| `MANUSCLAW_API_KEY`    |                | API key for authentication            |
| `MANUSCLAW_DEVICE_ID`  | `auto`         | Unique device identifier              |

## Building an Executable

Use PyInstaller to create a standalone `.exe`:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.ico --name ManusClawHub hub.py
```

The resulting executable will be in `dist/ManusClawHub.exe`.

## How It Works

1. The hub creates a system tray icon using `pystray`.
2. A background WebSocket client connects to the ManusClaw server at `/ws/chat/{device_id}`.
3. Incoming messages trigger tray notifications.
4. Menu items open browser tabs or run local actions.
5. If the WebSocket disconnects, the client automatically reconnects with exponential backoff.
