"""A2UI (Agent-to-UI) protocol definitions for Live Canvas.

Provides dataclass definitions for structured messages exchanged between the
agent and the canvas viewer, plus serialization/deserialization helpers.

Message types follow a JSON-RPC-inspired format:
    - ``CanvasUpdate``  — full or partial component state update
    - ``CanvasComponent`` — individual UI element (text, chart, image, …)
    - ``ChartData``     — chart rendering data for Chart.js
    - ``TextComponent`` — rich text block
    - ``ImageComponent`` — embedded image
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional, Union


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComponentType(str, Enum):
    """Supported A2UI component types."""
    TEXT = "text"
    CHART = "chart"
    IMAGE = "image"
    BUTTON = "button"
    TABLE = "table"
    CONTAINER = "container"
    MARKDOWN = "markdown"


class ChartType(str, Enum):
    """Chart.js compatible chart types."""
    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    DOUGHNUT = "doughnut"
    SCATTER = "scatter"
    BUBBLE = "bubble"
    RADAR = "radar"
    POLAR_AREA = "polarArea"


class MessageType(str, Enum):
    """A2UI message types (JSON-RPC inspired)."""
    UPDATE = "update"
    CLEAR = "clear"
    EVENT = "event"
    SYNC = "sync"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Data definitions
# ---------------------------------------------------------------------------

@dataclass
class ChartData:
    """Data for rendering a Chart.js chart."""
    chart_type: str = "bar"
    labels: list[str] = field(default_factory=list)
    datasets: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChartDataset:
    """A single dataset within a ChartData chart."""
    label: str = ""
    data: list[float | int] = field(default_factory=list)
    background_color: str | list[str] = "#4dc9f6"
    border_color: str | list[str] = "#4dc9f6"
    border_width: int = 1
    fill: bool = False


@dataclass
class TextComponent:
    """Rich text block with optional styling."""
    content: str = ""
    style: dict[str, Any] = field(default_factory=dict)  # CSS-like styles
    markdown: bool = False
    html: bool = False


@dataclass
class ImageComponent:
    """Embedded image component."""
    src: str = ""       # URL or base64 data URI
    alt: str = ""
    width: int | None = None
    height: int | None = None
    caption: str = ""


@dataclass
class ButtonComponent:
    """Clickable button that sends events back to the agent."""
    label: str = ""
    action: str = ""    # action identifier sent back via WebSocket
    style: str = "primary"  # primary | secondary | danger
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TableComponent:
    """Simple data table."""
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    title: str = ""
    max_rows: int = 50


@dataclass
class ContainerComponent:
    """Layout container for grouping components."""
    children: list[dict] = field(default_factory=list)
    direction: str = "vertical"  # vertical | horizontal | grid
    gap: int = 8


@dataclass
class CanvasComponent:
    """A single UI component on the canvas.

    Wraps one of the typed component payloads with common metadata.
    """
    id: str = ""
    component_type: str = ComponentType.TEXT.value
    x: int = 0
    y: int = 0
    width: int | None = None   # None = full width
    height: int | None = None
    props: dict[str, Any] = field(default_factory=dict)
    visible: bool = True
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CanvasEvent:
    """User interaction event sent from canvas viewer back to agent."""
    component_id: str
    event_type: str   # click | input | hover | submit
    action: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class CanvasUpdate:
    """Full or partial canvas state update message."""
    session_id: str = ""
    message_type: str = MessageType.UPDATE.value
    components: list[dict[str, Any]] = field(default_factory=list)
    clear_first: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanvasClear:
    """Clear all components from a canvas session."""
    session_id: str = ""
    message_type: str = MessageType.CLEAR.value


@dataclass
class CanvasSyncRequest:
    """Request full state sync from viewer."""
    session_id: str = ""
    message_type: str = MessageType.SYNC.value


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def component_from_dict(data: dict[str, Any]) -> CanvasComponent:
    """Deserialize a CanvasComponent from a plain dict."""
    return CanvasComponent(
        id=data.get("id", ""),
        component_type=data.get("component_type", ComponentType.TEXT.value),
        x=data.get("x", 0),
        y=data.get("y", 0),
        width=data.get("width"),
        height=data.get("height"),
        props=data.get("props", {}),
        visible=data.get("visible", True),
        order=data.get("order", 0),
    )


def update_from_dict(data: dict[str, Any]) -> CanvasUpdate:
    """Deserialize a CanvasUpdate from a plain dict."""
    return CanvasUpdate(
        session_id=data.get("session_id", ""),
        message_type=data.get("message_type", MessageType.UPDATE.value),
        components=data.get("components", []),
        clear_first=data.get("clear_first", False),
        metadata=data.get("metadata", {}),
    )


def event_from_dict(data: dict[str, Any]) -> CanvasEvent:
    """Deserialize a CanvasEvent from a plain dict."""
    return CanvasEvent(
        component_id=data.get("component_id", ""),
        event_type=data.get("event_type", "click"),
        action=data.get("action", ""),
        data=data.get("data", {}),
        timestamp=data.get("timestamp", 0.0),
    )


def serialize_message(msg: Any) -> str:
    """Serialize any A2UI message to a JSON string."""
    if hasattr(msg, "__dataclass_fields__"):
        return json.dumps(asdict(msg), default=str)
    return json.dumps(msg, default=str)


def parse_message(raw: str) -> dict[str, Any]:
    """Parse a raw JSON string into a dict (safe parse with error handling)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "message_type": MessageType.ERROR.value,
            "error": f"Invalid JSON: {exc}",
        }


# ---------------------------------------------------------------------------
# Component builders (convenience)
# ---------------------------------------------------------------------------

def text_component(
    id: str,
    content: str,
    markdown: bool = False,
    **style: Any,
) -> dict[str, Any]:
    """Create a text component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.TEXT.value,
        props={
            "text": TextComponent(content=content, markdown=markdown, style=style),
        },
    ).to_dict()


def chart_component(
    id: str,
    chart_type: str,
    labels: list[str],
    datasets: list[dict[str, Any]],
    title: str = "",
    **options: Any,
) -> dict[str, Any]:
    """Create a chart component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.CHART.value,
        props={
            "chart": ChartData(
                chart_type=chart_type,
                labels=labels,
                datasets=datasets,
                title=title,
                options=options,
            ),
        },
    ).to_dict()


def image_component(
    id: str,
    src: str,
    alt: str = "",
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Create an image component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.IMAGE.value,
        props={
            "image": ImageComponent(src=src, alt=alt, width=width, height=height),
        },
    ).to_dict()


def button_component(
    id: str,
    label: str,
    action: str,
    style: str = "primary",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a button component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.BUTTON.value,
        props={
            "button": ButtonComponent(label=label, action=action, style=style, data=data or {}),
        },
    ).to_dict()


def table_component(
    id: str,
    headers: list[str],
    rows: list[list[Any]],
    title: str = "",
) -> dict[str, Any]:
    """Create a table component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.TABLE.value,
        props={
            "table": TableComponent(headers=headers, rows=rows, title=title),
        },
    ).to_dict()


def markdown_component(
    id: str,
    content: str,
) -> dict[str, Any]:
    """Create a markdown component dict for the canvas."""
    return CanvasComponent(
        id=id,
        component_type=ComponentType.MARKDOWN.value,
        props={
            "text": TextComponent(content=content, markdown=True),
        },
    ).to_dict()
