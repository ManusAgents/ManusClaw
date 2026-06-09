# ManusClaw macOS Menu Bar App

macOS menu bar companion for ManusClaw, built with `rumps`.

## Prerequisites

- macOS 10.15+
- Python 3.11+
- `rumps` — macOS menu bar library

```bash
pip install rumps websockets
```

## Usage

```bash
python menubar.py
```

### Menu Bar Items

| Menu Item         | Description                                      |
|-------------------|--------------------------------------------------|
| **Status**        | Shows current connection status (Connected/Disconnected) |
| **Chat**          | Opens the ManusClaw web chat interface           |
| **Canvas**        | Opens the live canvas viewer                     |
| **Voice**         | Toggles voice input on/off (placeholder)          |
| **Preferences**   | Opens the ManusClaw config directory             |
| **Quit**          | Exits the menu bar app                           |

## Configuration

| Variable               | Default        | Description                          |
|------------------------|----------------|--------------------------------------|
| `MANUSCLAW_SERVER_URL` | `http://localhost:8765` | HTTP URL of ManusClaw server   |
| `MANUSCLAW_WS_URL`    | `ws://localhost:8765` | WebSocket URL of ManusClaw server |
| `MANUSCLAW_API_KEY`    |                | API key for authentication            |
| `MANUSCLAW_DEVICE_ID`  | `auto`         | Unique device identifier              |

## Bundling as a macOS App

Use `py2app` to create a `.app` bundle:

```bash
pip install py2app
```

Create a `setup.py`:

```python
from setuptools import setup

APP = ["menubar.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icon.icns",
    "plist": {
        "CFBundleShortVersionString": "1.0",
        "CFBundleName": "ManusClaw",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
```

Then build:

```bash
python setup.py py2app
```

## Architecture

- The menu bar app uses `rumps` for native macOS integration.
- A background `threading.Thread` runs an asyncio WebSocket client.
- Health checks ping `/healthz` every 30 seconds.
- Quick chat is available via a text input dialog.
- Voice features are placeholders pending the voice module integration.
