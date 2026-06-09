from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from app.config import Config
from app.logger import logger
from app.schema import ToolResult
from app.tool.base import BaseTool


class DataVisualization(BaseTool):
    name = "data_viz"
    description = (
        "Generate charts from data. Uses matplotlib (Python) to create PNG or HTML charts. "
        "Saves output to workspace/."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["bar", "line", "scatter", "pie", "histogram"],
                "description": "Type of chart to generate.",
            },
            "data": {
                "type": "object",
                "description": "Chart data: {labels: [...], values: [...], title: '...'}",
            },
            "output_name": {
                "type": "string",
                "description": "Output filename (without extension). Saved to workspace/.",
                "default": "chart",
            },
            "format": {
                "type": "string",
                "enum": ["png", "html"],
                "default": "png",
                "description": "Output format.",
            },
        },
        "required": ["chart_type", "data"],
    }

    async def execute(
        self,
        chart_type: str,
        data: dict[str, Any],
        output_name: str = "chart",
        format: str = "png",
        **_: Any,
    ) -> ToolResult:
        # Fix: validate chart_type against allowed values to prevent code injection
        allowed_chart_types = {"bar", "line", "scatter", "pie", "histogram"}
        if chart_type not in allowed_chart_types:
            return ToolResult(error=f"Invalid chart_type '{chart_type}'. Must be one of: {allowed_chart_types}")

        # Fix: sanitize output_name to prevent path traversal
        import re
        if not re.match(r'^[a-zA-Z0-9_\-]+$', output_name):
            return ToolResult(error=f"Invalid output_name '{output_name}'. Only alphanumeric, underscores, and hyphens allowed.")

        workspace = Path(Config.get().workspace_dir)
        workspace.mkdir(exist_ok=True)
        out_path = workspace / f"{output_name}.{format}"

        # Fix: verify the resolved path is still within workspace
        try:
            out_path.resolve().relative_to(workspace.resolve())
        except ValueError:
            return ToolResult(error="Output path escapes workspace directory.")

        labels = data.get("labels", [])
        values = data.get("values", [])
        title = data.get("title", chart_type.capitalize() + " Chart")

        code = self._build_matplotlib_code(chart_type, labels, values, title, str(out_path), format)
        try:
            # Fix: use asyncio subprocess to avoid blocking the event loop
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                return ToolResult(error=f"Chart generation failed: {stderr.decode()}")
            return ToolResult(output=f"Chart saved to {out_path}")
        except asyncio.TimeoutError:
            return ToolResult(error="Chart generation timed out after 30 seconds.")
        except Exception as e:
            return ToolResult(error=str(e))

    def _build_matplotlib_code(
        self,
        chart_type: str,
        labels: list,
        values: list,
        title: str,
        out_path: str,
        fmt: str,
    ) -> str:
        labels_json = json.dumps(labels)
        values_json = json.dumps(values)
        return f"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

labels = {labels_json}
values = {values_json}
title = {json.dumps(title)}
out_path = {json.dumps(out_path)}
fmt = {json.dumps(fmt)}

fig, ax = plt.subplots(figsize=(10, 6))
if "{chart_type}" == "bar":
    ax.bar(labels, values)
elif "{chart_type}" == "line":
    ax.plot(labels, values, marker='o')
elif "{chart_type}" == "scatter":
    ax.scatter(labels, values)
elif "{chart_type}" == "pie":
    ax.pie(values, labels=labels, autopct='%1.1f%%')
elif "{chart_type}" == "histogram":
    ax.hist(values, bins=20)

ax.set_title(title)
plt.tight_layout()
if fmt == "html":
    import mpld3
    html = mpld3.fig_to_html(fig)
    with open(out_path, 'w') as f:
        f.write(html)
else:
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
plt.close()
print(f"Saved to {{out_path}}")
"""
