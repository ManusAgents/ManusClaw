# Canvas Nodes (Mobile/Desktop)

**Status:** ✅ Implemented

## Description
WebSocket protocol for connecting mobile and desktop devices as Live Canvas viewers with interaction support.

## Components

### Node Protocol (`app/nodes/protocol.py`)
Message types: `register`, `registered`, `heartbeat`, `heartbeat_ack`, `event`, `canvas_push`, `command`, `pong`, `error`, `disconnect`.

Event types: `touch`, `tap`, `swipe`, `voice`, `text_input`, `button_press`, `scroll`, `pinch`, `shake`, `screenshot`.

### DeviceManager (`app/nodes/manager.py`)
LRU-cached device registry (128 devices, 120s heartbeat timeout). Capability tracking, broadcast, event/command dispatch.

## Serialization
All messages use `serialize_message()` / `parse_message()` with JSON encoding.

## Usage
```python
from app.nodes.manager import DeviceManager

dm = DeviceManager()
node = await dm.register(registration)
await dm.send_canvas_update("sess-1", components=[...])
await dm.broadcast(push)
stale = await dm.check_heartbeats()
```
