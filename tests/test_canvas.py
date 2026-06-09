"""Tests for canvas system (CanvasServer, A2UI protocol, CanvasTool)."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.canvas.a2ui import (
    CanvasComponent,
    CanvasEvent,
    CanvasUpdate,
    ComponentType,
    MessageType,
    serialize_message,
    parse_message,
    component_from_dict,
    event_from_dict,
    text_component,
    chart_component,
    image_component,
    button_component,
    table_component,
    markdown_component,
)


# ── A2UI Serialization / Deserialization ──────────────────────────────────

def test_serialize_canvas_update():
    update = CanvasUpdate(
        session_id="sess-1",
        message_type=MessageType.UPDATE.value,
        components=[{"id": "c1", "component_type": "text"}],
        clear_first=False,
    )
    raw = serialize_message(update)
    data = json.loads(raw)
    assert data["session_id"] == "sess-1"
    assert data["message_type"] == "update"


def test_parse_message_valid():
    raw = '{"message_type": "update", "session_id": "s1"}'
    data = parse_message(raw)
    assert data["session_id"] == "s1"


def test_parse_message_invalid_json():
    result = parse_message("not json {{{")
    assert result["message_type"] == MessageType.ERROR.value
    assert "Invalid JSON" in result["error"]


def test_component_from_dict():
    data = {
        "id": "comp-1",
        "component_type": "chart",
        "x": 10,
        "y": 20,
        "width": 500,
        "props": {"chart": {"chart_type": "bar"}},
    }
    comp = component_from_dict(data)
    assert comp.id == "comp-1"
    assert comp.component_type == "chart"
    assert comp.x == 10


def test_event_from_dict():
    data = {
        "component_id": "btn-1",
        "event_type": "click",
        "action": "submit",
        "data": {"value": "hello"},
    }
    event = event_from_dict(data)
    assert event.component_id == "btn-1"
    assert event.event_type == "click"


# ── Component builders ─────────────────────────────────────────────────────

def test_text_component_builder():
    comp = text_component("t1", "Hello World", markdown=True)
    assert comp["id"] == "t1"
    assert comp["component_type"] == "text"
    props = comp["props"]["text"]
    assert props["content"] == "Hello World"
    assert props["markdown"] is True


def test_chart_component_builder():
    comp = chart_component("ch1", "bar", ["A", "B"], [{"label": "Sales", "data": [1, 2]}])
    assert comp["component_type"] == "chart"
    chart = comp["props"]["chart"]
    assert chart["chart_type"] == "bar"
    assert chart["labels"] == ["A", "B"]


def test_button_component_builder():
    comp = button_component("b1", "Submit", "do_submit", style="primary")
    assert comp["component_type"] == "button"
    btn = comp["props"]["button"]
    assert btn["label"] == "Submit"
    assert btn["action"] == "do_submit"


def test_image_component_builder():
    comp = image_component("img1", "https://example.com/pic.png", alt="Photo")
    assert comp["component_type"] == "image"
    img = comp["props"]["image"]
    assert img["src"] == "https://example.com/pic.png"


def test_table_component_builder():
    comp = table_component("tbl1", ["Name", "Age"], [["Alice", 30]])
    assert comp["component_type"] == "table"
    tbl = comp["props"]["table"]
    assert tbl["headers"] == ["Name", "Age"]


def test_markdown_component_builder():
    comp = markdown_component("md1", "# Title\n\nBody text")
    assert comp["component_type"] == "markdown"


# ── CanvasServer update / clear / get_state ─────────────────────────────────

@pytest.mark.asyncio
async def test_canvas_server_update_creates_session():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    state = await server.update("sess-1", [
        {"id": "c1", "component_type": "text", "props": {"text": "Hello"}}
    ])
    assert "sess-1" in server._sessions
    assert len(state["components"]) == 1
    assert state["components"][0]["id"] == "c1"


@pytest.mark.asyncio
async def test_canvas_server_clear():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    await server.update("sess-2", [{"id": "c1", "component_type": "text"}])
    state = await server.clear("sess-2")
    assert state["components"] == []


@pytest.mark.asyncio
async def test_canvas_server_get_state_empty():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    state = await server.get_state("nonexistent")
    assert state["components"] == []


@pytest.mark.asyncio
async def test_canvas_server_get_state_existing():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    await server.update("sess-3", [{"id": "c1", "component_type": "text"}])
    state = await server.get_state("sess-3")
    assert len(state["components"]) == 1


@pytest.mark.asyncio
async def test_canvas_server_update_clear_first():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    await server.update("sess-4", [{"id": "c1", "component_type": "text"}])
    await server.update("sess-4", [{"id": "c2", "component_type": "chart"}], clear_first=True)
    state = await server.get_state("sess-4")
    assert len(state["components"]) == 1
    assert state["components"][0]["id"] == "c2"


@pytest.mark.asyncio
async def test_canvas_server_session_count():
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    assert await server.session_count() == 0
    await server.update("s1", [{"id": "c1", "component_type": "text"}])
    assert await server.session_count() == 1


# ── CanvasTool actions ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_canvas_tool_render():
    from app.canvas.tool import CanvasTool, set_canvas_server
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    set_canvas_server(server)
    tool = CanvasTool(session_id="test-sess")
    result = await tool.execute(
        action="render",
        components=[{"id": "c1", "component_type": "text", "props": {"text": {"content": "Hi"}}}],
    )
    assert result.output is not None
    assert "1 components rendered" in result.output


@pytest.mark.asyncio
async def test_canvas_tool_clear():
    from app.canvas.tool import CanvasTool, set_canvas_server
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    set_canvas_server(server)
    tool = CanvasTool(session_id="test-sess")
    result = await tool.execute(action="clear")
    assert "cleared" in result.output.lower()


@pytest.mark.asyncio
async def test_canvas_tool_add_chart():
    from app.canvas.tool import CanvasTool, set_canvas_server
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    set_canvas_server(server)
    tool = CanvasTool(session_id="test-sess")
    result = await tool.execute(
        action="add_chart",
        chart_data={
            "chart_type": "bar",
            "labels": ["A", "B"],
            "datasets": [{"label": "Sales", "data": [10, 20]}],
        },
    )
    assert result.output is not None
    assert "bar chart" in result.output.lower()


@pytest.mark.asyncio
async def test_canvas_tool_unknown_action():
    from app.canvas.tool import CanvasTool, set_canvas_server
    from app.canvas.server import CanvasServer
    server = CanvasServer()
    set_canvas_server(server)
    tool = CanvasTool(session_id="test-sess")
    result = await tool.execute(action="explode")
    assert "Unknown canvas action" in result.error
