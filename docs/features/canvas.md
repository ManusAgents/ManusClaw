# Live Canvas (A2UI)

**Status:** ✅ Implemented

## Description
Real-time UI rendering via WebSocket using the Agent-to-UI protocol. Supports text, charts, tables, images, buttons, markdown, and containers.

## Components

### A2UI Protocol (`app/canvas/a2ui.py`)
- `CanvasComponent` — generic UI element wrapper
- `CanvasUpdate` — state update message
- `CanvasEvent` — user interaction event
- Component builders: `text_component()`, `chart_component()`, `image_component()`, `button_component()`, `table_component()`, `markdown_component()`

### CanvasServer (`app/canvas/server.py`)
WebSocket endpoint at `/ws/canvas/{session_id}`. Manages per-session state, broadcasts updates, dispatches events.

### CanvasTool (`app/canvas/tool.py`)
Agent-callable tool with actions: `render`, `add_chart`, `clear`.

## Configuration
Requires `server` extra (`fastapi` + `uvicorn`).

## Usage
```python
# Agent tool call
await canvas_render(session_id="abc", components=[
    {"id": "title", "component_type": "text", "props": {"text": {"content": "Report"}}},
    {"id": "chart", "component_type": "chart", "props": {"chart": {"chart_type": "bar", "labels": ["Q1"], "datasets": [...]}}},
])
```

## View
Web UI at `/canvas?session={session_id}`.
