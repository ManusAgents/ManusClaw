"""Canvas tool — allows the agent to render UI components on a live canvas.

Extends :class:`BaseTool` from ``app/tool/base.py`` and follows the same
pattern as other tools (e.g. ``image_gen.py``, ``data_viz.py``).

Tool methods:
    - ``canvas_render(components)``  — render a list of A2UI components
    - ``canvas_add_chart(data)``     — convenience: add a Chart.js chart
    - ``canvas_clear()``             — clear the entire canvas

The tool sends updates through the global :class:`CanvasServer` instance.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.canvas.a2ui import (
    CanvasComponent,
    ChartData,
    chart_component,
    image_component,
    markdown_component,
    table_component,
    text_component,
)
from app.canvas.server import CanvasServer
from app.logger import logger
from app.schema import ToolResult
from app.tool.base import BaseTool

# Global canvas server instance — shared with the web server
_global_canvas_server: Optional[CanvasServer] = None


def get_canvas_server() -> CanvasServer:
    """Return the global CanvasServer singleton, creating it if needed."""
    global _global_canvas_server
    if _global_canvas_server is None:
        _global_canvas_server = CanvasServer()
        logger.info("[CanvasTool] Created global CanvasServer instance")
    return _global_canvas_server


def set_canvas_server(server: CanvasServer) -> None:
    """Set the global CanvasServer instance (for injection from main.py)."""
    global _global_canvas_server
    _global_canvas_server = server


class CanvasTool(BaseTool):
    """Agent-callable tool for rendering components on a live canvas.

    The canvas is a real-time UI surface that web clients can view via
    the ``/ws/canvas/{session_id}`` WebSocket endpoint.

    Example agent usage::

        # Render a dashboard
        await canvas_render(
            session_id="abc123",
            components=[
                {"id": "title", "component_type": "text",
                 "props": {"text": {"content": "Sales Report"}}},
                {"id": "chart1", "component_type": "chart",
                 "props": {"chart": {"chart_type": "bar",
                    "labels": ["Q1", "Q2", "Q3", "Q4"],
                    "datasets": [{"label": "Revenue", "data": [100, 200, 150, 300]}]}}},
            ]
        )
    """

    name = "canvas"
    description = (
        "Render UI components (text, charts, tables, images, buttons) on a live "
        "canvas that users can view in real-time via the web interface. "
        "Use canvas_render to display results, canvas_add_chart for quick charts, "
        "and canvas_clear to reset the canvas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["render", "add_chart", "clear"],
                "description": "Action to perform on the canvas.",
            },
            "session_id": {
                "type": "string",
                "description": "Canvas session ID (auto-generated if empty).",
            },
            "components": {
                "type": "array",
                "description": "List of A2UI component dicts (for 'render' action).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "component_type": {
                            "type": "string",
                            "enum": ["text", "chart", "image", "button", "table", "container", "markdown"],
                        },
                        "props": {"type": "object"},
                    },
                    "required": ["id", "component_type"],
                },
            },
            "chart_data": {
                "type": "object",
                "description": "Chart data dict (for 'add_chart' action).",
                "properties": {
                    "chart_type": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "datasets": {"type": "array"},
                    "title": {"type": "string"},
                },
                "required": ["chart_type", "labels", "datasets"],
            },
            "clear_first": {
                "type": "boolean",
                "description": "Clear canvas before rendering new components.",
                "default": False,
            },
        },
        "required": ["action"],
    }

    def __init__(self, session_id: Optional[str] = None) -> None:
        """Initialise the canvas tool.

        Parameters
        ----------
        session_id:
            Default session ID.  Can be overridden per-call.
        """
        self._default_session_id = session_id or ""
        self._server = get_canvas_server()

    async def execute(
        self,
        action: str = "render",
        session_id: str = "",
        components: Optional[list[dict[str, Any]]] = None,
        chart_data: Optional[dict[str, Any]] = None,
        clear_first: bool = False,
        **_: Any,
    ) -> ToolResult:
        """Execute a canvas action.

        Parameters
        ----------
        action:
            One of "render", "add_chart", "clear".
        session_id:
            Canvas session identifier.  Falls back to default.
        components:
            A2UI component dicts (for render action).
        chart_data:
            Chart definition dict (for add_chart action).
        clear_first:
            Whether to clear before rendering.
        """
        sid = session_id or self._default_session_id
        if not sid:
            import uuid
            sid = str(uuid.uuid4())[:12]

        try:
            if action == "render":
                return await self._render(sid, components or [], clear_first)
            elif action == "add_chart":
                return await self._add_chart(sid, chart_data or {})
            elif action == "clear":
                return await self._clear(sid)
            else:
                return ToolResult(error=f"Unknown canvas action: {action}")
        except Exception as exc:
            logger.error("[CanvasTool] Action '%s' failed: %s", action, exc)
            return ToolResult(error=f"Canvas action failed: {exc}")

    async def _render(
        self,
        session_id: str,
        components: list[dict[str, Any]],
        clear_first: bool,
    ) -> ToolResult:
        """Render a list of components to the canvas."""
        if not components:
            return ToolResult(error="No components provided to render")

        # Validate components
        validated: list[dict[str, Any]] = []
        for comp in components:
            if not isinstance(comp, dict):
                return ToolResult(error=f"Invalid component (not a dict): {comp}")
            if "component_type" not in comp:
                return ToolResult(error=f"Component missing 'component_type': {comp}")
            if "id" not in comp:
                import uuid
                comp["id"] = str(uuid.uuid4())[:8]
            validated.append(comp)

        state = await self._server.update(
            session_id=session_id,
            components=validated,
            clear_first=clear_first,
        )

        return ToolResult(
            output=(
                f"Canvas updated: {len(validated)} components rendered "
                f"on session '{session_id}' "
                f"(total: {len(state.get('components', []))} components). "
                f"View at /canvas?session={session_id}"
            )
        )

    async def _add_chart(
        self,
        session_id: str,
        chart_data: dict[str, Any],
    ) -> ToolResult:
        """Add a chart component to the canvas."""
        required_keys = {"chart_type", "labels", "datasets"}
        missing = required_keys - set(chart_data.keys())
        if missing:
            return ToolResult(error=f"Chart data missing keys: {missing}")

        comp = chart_component(
            id=chart_data.get("id", "chart_auto"),
            chart_type=chart_data["chart_type"],
            labels=chart_data["labels"],
            datasets=chart_data["datasets"],
            title=chart_data.get("title", ""),
        )

        state = await self._server.update(
            session_id=session_id,
            components=[comp],
            clear_first=False,
        )

        return ToolResult(
            output=(
                f"Chart added to canvas session '{session_id}': "
                f"{chart_data['chart_type']} chart with {len(chart_data['labels'])} labels. "
                f"View at /canvas?session={session_id}"
            )
        )

    async def _clear(self, session_id: str) -> ToolResult:
        """Clear all components from the canvas."""
        await self._server.clear(session_id)
        return ToolResult(
            output=f"Canvas session '{session_id}' cleared."
        )

    async def cleanup(self) -> None:
        """No persistent resources to clean up for the tool itself."""
        pass
