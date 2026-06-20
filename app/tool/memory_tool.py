from __future__ import annotations
"""Memory CRUD tool — reads and writes MEMORY.md and USER.md for persistent context."""
import os
import threading
from pathlib import Path
from app.tool.base import BaseTool
from app.schema import ToolResult


def _get_workspace() -> Path:
    """Resolve the workspace directory lazily.

    Reads ``MANUSCLAW_WORKSPACE`` each call so runtime env changes
    (tests via ``monkeypatch.setenv``, profile switching, CLI overrides)
    are honoured. Previously this was a module-level constant evaluated
    once at import, which silently ignored later env changes.
    """
    return Path(os.getenv("MANUSCLAW_WORKSPACE", "workspace"))


def _memory_file() -> Path:
    return _get_workspace() / "MEMORY.md"


def _user_file() -> Path:
    return _get_workspace() / "USER.md"


# Backward-compatible module-level names.
# NOTE: These are kept for compatibility with code/tests that patch
#       ``mt.MEMORY_FILE`` / ``mt._WORKSPACE`` directly. The MemoryTool
#       implementation itself calls the lazy resolvers above so runtime
#       env-var changes take effect.
_WORKSPACE = _get_workspace()
MEMORY_FILE = _WORKSPACE / "MEMORY.md"
USER_FILE = _WORKSPACE / "USER.md"

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
        workspace = _get_workspace()
        memory_file = _memory_file()
        user_file = _user_file()
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            if action == "read_memory":
                if not memory_file.exists():
                    return ToolResult(output="MEMORY.md is empty.")
                return ToolResult(output=memory_file.read_text(encoding="utf-8"))
            elif action == "write_memory":
                memory_file.write_text(content, encoding="utf-8")
                return ToolResult(output=f"MEMORY.md written ({len(content)} chars).")
            elif action == "append_memory":
                # FIX: Use lock to prevent race condition on concurrent appends
                with _memory_lock:
                    existing = memory_file.read_text("utf-8") if memory_file.exists() else ""
                    new_content = existing.rstrip() + "\n\n" + content if existing else content
                    memory_file.write_text(new_content, encoding="utf-8")
                return ToolResult(output=f"Appended {len(content)} chars to MEMORY.md.")
            elif action == "read_user":
                if not user_file.exists():
                    return ToolResult(output="USER.md is empty.")
                return ToolResult(output=user_file.read_text(encoding="utf-8"))
            elif action == "write_user":
                user_file.write_text(content, encoding="utf-8")
                return ToolResult(output=f"USER.md written ({len(content)} chars).")
            else:
                return ToolResult(error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(error=str(e))
