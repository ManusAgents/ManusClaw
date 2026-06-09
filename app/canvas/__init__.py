"""Live Canvas module — Agent-to-UI (A2UI) protocol for real-time rendering.

Provides:
    - CanvasServer   — WebSocket-backed canvas state manager
    - CanvasTool     — Agent-callable tool for rendering components
    - A2UI dataclass definitions and helpers
"""

from app.canvas.server import CanvasServer
from app.canvas.tool import CanvasTool, get_canvas_server, set_canvas_server
from app.canvas.a2ui import (
    CanvasComponent,
    CanvasUpdate,
    CanvasEvent,
    CanvasClear,
    ChartData,
    TextComponent,
    ImageComponent,
    ButtonComponent,
    TableComponent,
    ContainerComponent,
    ComponentType,
    ChartType,
    MessageType,
    # Serialization helpers
    component_from_dict,
    update_from_dict,
    event_from_dict,
    serialize_message,
    parse_message,
    # Component builders
    text_component,
    chart_component,
    image_component,
    button_component,
    table_component,
    markdown_component,
)

__all__ = [
    "CanvasServer",
    "CanvasTool",
    "get_canvas_server",
    "set_canvas_server",
    "CanvasComponent",
    "CanvasUpdate",
    "CanvasEvent",
    "CanvasClear",
    "ChartData",
    "TextComponent",
    "ImageComponent",
    "ButtonComponent",
    "TableComponent",
    "ContainerComponent",
    "ComponentType",
    "ChartType",
    "MessageType",
    "component_from_dict",
    "update_from_dict",
    "event_from_dict",
    "serialize_message",
    "parse_message",
    "text_component",
    "chart_component",
    "image_component",
    "button_component",
    "table_component",
    "markdown_component",
]
