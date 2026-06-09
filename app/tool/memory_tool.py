from __future__ import annotations
"""Memory CRUD tool — reads and writes MEMORY.md and USER.md for persistent context."""
import os
import threading
from pathlib import Path
from app.tool.base import BaseTool
from app.schema import ToolResult

_WORKSPACE = Path(os.getenv("MANUSCLAW_WORKSPACE", "workspace"))
MEMORY_FILE = _WORKSPACE / "MEMORY.md"
USER_FILE   = _WORKSPACE / "USER.md"

# FIX: Thread lock to prevent race conditions on concurrent append operations
_memory_lock = threading.Lock()


class MemoryTool(BaseTool):
    name = "memory"
    description = (
        "Read or write persistent memory files. MEMORY.md stores facts/knowledge, "
        "USER.md stores user preferences. "
        "Actions: read_memory, write_memory, append_memory, read_user, write_user"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read_memory", "write_memory", "append_memory", "read_user", "write_user"],
            },
            "content": {"type": "string", "description": "Content to write or append"},
        },
        "required": ["action"],
    }

    async def execute(self, action: str, content: str = "") -> ToolResult:
        _WORKSPACE.mkdir(parents=True, exist_ok=True)
        try:
            if action == "read_memory":
                if not MEMORY_FILE.exists():
                    return ToolResult(output="MEMORY.md is empty.")
                return ToolResult(output=MEMORY_FILE.read_text(encoding="utf-8"))
            elif action == "write_memory":
                MEMORY_FILE.write_text(content, encoding="utf-8")
                return ToolResult(output=f"MEMORY.md written ({len(content)} chars).")
            elif action == "append_memory":
                # FIX: Use lock to prevent race condition on concurrent appends
                with _memory_lock:
                    existing = MEMORY_FILE.read_text("utf-8") if MEMORY_FILE.exists() else ""
                    new_content = existing.rstrip() + "\n\n" + content if existing else content
                    MEMORY_FILE.write_text(new_content, encoding="utf-8")
                return ToolResult(output=f"Appended {len(content)} chars to MEMORY.md.")
            elif action == "read_user":
                if not USER_FILE.exists():
                    return ToolResult(output="USER.md is empty.")
                return ToolResult(output=USER_FILE.read_text(encoding="utf-8"))
            elif action == "write_user":
                USER_FILE.write_text(content, encoding="utf-8")
                return ToolResult(output=f"USER.md written ({len(content)} chars).")
            else:
                return ToolResult(error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(error=str(e))
