from __future__ import annotations
"""Delegate tool — spawns an isolated subagent in a thread pool."""
import asyncio
from app.tool.base import BaseTool
from app.schema import ToolResult


class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Spawn an isolated subagent to handle an independent subtask. "
        "Use for tasks that can run in parallel or need full isolation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The subtask description"},
            "max_steps": {"type": "integer", "default": 15},
            "timeout": {"type": "integer", "default": 300},
        },
        "required": ["task"],
    }

    async def execute(self, task: str, max_steps: int = 15, timeout: int = 300) -> ToolResult:
        from app.agent.manus import Manus
        from app.permissions.gate import AgentMode

        async def _run() -> str:
            agent = Manus(mode=AgentMode.BUILD)
            agent._max_steps = max_steps
            return await agent.run(task)

        try:
            result = await asyncio.wait_for(_run(), timeout=timeout)
            return ToolResult(output=f"[Delegate completed]\n{result[:3000]}")
        except asyncio.TimeoutError:
            return ToolResult(error=f"Subagent timed out after {timeout}s")
        except Exception as e:
            return ToolResult(error=f"Subagent error: {e}")
