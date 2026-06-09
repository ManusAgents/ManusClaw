# ManusClaw Mobile Node Client

Reference implementation of a WebSocket-based mobile node client for ManusClaw.

This client acts as a remote sensor/input device that connects to the ManusClaw
server and can:
- Register as a compute or sensor node
- Receive Canvas UI updates and display them
- Forward voice commands and screen capture data to the server
- Operate with automatic reconnection on network changes

## Supported Platforms

This is a **reference Python implementation** for desktop/laptop testing. For
actual mobile deployment, use one of:

| Platform | Recommended Approach                                 |
|----------|------------------------------------------------------|
| iOS      | Use `http://pypi.org/project/websockets/` with a Swift wrapper or build with Kivy |
| Android  | Use `http://pypi.org/project/websockets/` with Kivy, BeeWare (Toga), or Chaquopy |

## Prerequisites

```bash
pip install websockets
```

## Usage

```bash
python node_client.py [--server ws://localhost:8765] [--device-id my-phone]
```

## Configuration

| Variable                  | Default                  | Description                      |
|---------------------------|--------------------------|----------------------------------|
| `MANUSCLAW_SERVER_URL`   | `ws://localhost:8765`    | WebSocket URL of ManusClaw server |
| `MANUSCLAW_API_KEY`       |                          | API key for authentication         |
| `MANUSCLAW_DEVICE_ID`     | `auto-generated`         | Unique device identifier          |
| `MANUSCLAW_DEVICE_TYPE`   | `mobile`                 | Device type classification         |
| `MANUSCLAW_CAPABILITIES`  | `voice,screen`           | Comma-separated capabilities      |

## Architecture

```
Mobile Device                          ManusClaw Server
    │                                        │
    │  ─── WebSocket /ws/nodes ──→          │
    │  register(device_id, type, caps)       │
    │                                        │
    │  ←── canvas_update ────                │
    │  (A2UI component updates)               │
    │                                        │
    │  ─── voice_data ──────→               │
    │  (audio chunks for processing)         │
    │                                        │
    │  ─── screen_capture ───→              │
    │  (frame data for analysis)            │
    │                                        │
    │  ←── command ──────────               │
    │  (server-pushed actions)               │
    └────────────────────────────────────────┘
```

## Reconnection Logic

The client implements exponential-backoff reconnection with jitter:
- Base delay: 1s
- Max delay: 60s
- Jitter: ±30% of current delay
- Max attempts: unlimited (runs until stopped)
