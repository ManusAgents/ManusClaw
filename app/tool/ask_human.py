from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.schema import ToolResult
from app.tool.base import BaseTool


class AskHuman(BaseTool):
    name = "ask_human"
    description = "Ask the user a question and wait for their response. Works in interactive (stdin) mode only."
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Question to ask the user."},
            "timeout": {"type": "integer", "description": "Seconds to wait for response (default 120, 0=no timeout)", "default": 120},
        },
        "required": ["question"],
    }

    async def execute(self, question: str, timeout: int = 120, **_: Any) -> ToolResult:
        # FIX: Check if stdin is available (not a pipe/redirect/server mode)
        if not sys.stdin.isatty():
            return ToolResult(
                error="Cannot ask user: running in non-interactive mode (no stdin). "
                      "Re-run in interactive terminal mode or provide all needed info in the initial prompt."
            )
        
        print(f"\n[ManusClaw asks]: {question}")
        try:
            loop = asyncio.get_event_loop()
            if timeout and timeout > 0:
                answer = await asyncio.wait_for(
                    loop.run_in_executor(None, input, "Your answer: "),
                    timeout=timeout,
                )
            else:
                answer = await loop.run_in_executor(None, input, "Your answer: ")
        except asyncio.TimeoutError:
            return ToolResult(
                error=f"No response received within {timeout}s. Proceeding without user input."
            )
        except (EOFError, KeyboardInterrupt):
            return ToolResult(error="User cancelled input.")
        
        return ToolResult(output=f"User said: {answer}")
