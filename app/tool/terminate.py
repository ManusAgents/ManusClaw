from __future__ import annotations

from typing import Any

from app.schema import ToolResult
from app.tool.base import BaseTool


class Terminate(BaseTool):
    name = "terminate"
    description = (
        "Signal that the task is complete and the agent should stop. "
        "Call this when the goal has been achieved or cannot be achieved."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Explanation of why the agent is terminating.",
            },
        },
        "required": ["reason"],
    }

    async def execute(self, reason: str, **_: Any) -> ToolResult:
        return ToolResult(output=f"Agent terminated: {reason}", system="terminate")
