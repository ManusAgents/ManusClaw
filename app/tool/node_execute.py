from __future__ import annotations
"""Node.js code execution tool — runs JS code in an isolated subprocess."""
import asyncio
import os
import tempfile
from app.tool.base import BaseTool
from app.schema import ToolResult


class NodeExecute(BaseTool):
    name = "node_execute"
    description = "Execute Node.js / JavaScript code in an isolated subprocess."
    parameters = {
        "type": "object",
        "properties": {
            "code":    {"type": "string", "description": "JavaScript code to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["code"],
    }

    async def execute(self, code: str, timeout: int = 30) -> ToolResult:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
                f.write(code)
                tmp = f.name
            try:
                proc = await asyncio.create_subprocess_exec(
                    "node", tmp,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                out = stdout.decode()
                err = stderr.decode()
                if proc.returncode != 0:
                    return ToolResult(output=out, error=err or f"Exit {proc.returncode}")
                return ToolResult(output=out or "(no output)")
            finally:
                os.unlink(tmp)
        except asyncio.TimeoutError:
            return ToolResult(error=f"Timed out after {timeout}s")
        except FileNotFoundError:
            return ToolResult(error="Node.js not found. Install Node.js to use this tool.")
        except Exception as e:
            return ToolResult(error=str(e))
