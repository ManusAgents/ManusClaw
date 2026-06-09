from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.schema import ToolResult
from app.tool.base import BaseTool


class StrReplaceEditor(BaseTool):
    name = "str_replace_editor"
    description = (
        "Edit files on disk. Supports commands: view, create, str_replace, insert, undo_edit."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "Operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "File or directory path.",
            },
            "file_text": {
                "type": "string",
                "description": "Content for create command.",
            },
            "old_str": {
                "type": "string",
                "description": "String to replace (str_replace command).",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string (str_replace command).",
            },
            "insert_line": {
                "type": "integer",
                "description": "Line number to insert after (insert command).",
            },
            "new_line": {
                "type": "string",
                "description": "Line content to insert.",
            },
        },
        "required": ["command", "path"],
    }

    def __init__(self) -> None:
        self._history: dict[str, list[str]] = {}

    async def execute(
        self,
        command: str,
        path: str,
        file_text: Optional[str] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
        new_line: Optional[str] = None,
        **_: Any,
    ) -> ToolResult:
        p = Path(path)

        if command == "view":
            if p.is_dir():
                entries = sorted(p.iterdir())
                lines = [f"Directory: {p}", "---"]
                for e in entries:
                    marker = "/" if e.is_dir() else ""
                    lines.append(f"  {e.name}{marker}")
                return ToolResult(output="\n".join(lines))
            if not p.exists():
                return ToolResult(error=f"File not found: {path}")
            content = p.read_text(encoding="utf-8")
            numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(content.splitlines()))
            return ToolResult(output=f"File: {path}\n---\n{numbered}")

        if command == "create":
            if file_text is None:
                return ToolResult(error="file_text is required for create.")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(file_text, encoding="utf-8")
            return ToolResult(output=f"Created {path} ({len(file_text)} bytes).")

        if command == "str_replace":
            if old_str is None or new_str is None:
                return ToolResult(error="old_str and new_str required for str_replace.")
            if not p.exists():
                return ToolResult(error=f"File not found: {path}")
            content = p.read_text(encoding="utf-8")
            if old_str not in content:
                return ToolResult(error=f"old_str not found in {path}.")
            self._history.setdefault(path, []).append(content)
            new_content = content.replace(old_str, new_str, 1)
            p.write_text(new_content, encoding="utf-8")
            return ToolResult(output=f"Replaced in {path}.")

        if command == "insert":
            if insert_line is None or new_line is None:
                return ToolResult(error="insert_line and new_line required for insert.")
            if not p.exists():
                return ToolResult(error=f"File not found: {path}")
            content = p.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            self._history.setdefault(path, []).append(content)
            idx = max(0, min(insert_line, len(lines)))
            lines.insert(idx, new_line + "\n")
            p.write_text("".join(lines), encoding="utf-8")
            return ToolResult(output=f"Inserted line at position {idx} in {path}.")

        if command == "undo_edit":
            history = self._history.get(path, [])
            if not history:
                return ToolResult(error=f"No edit history for {path}.")
            prev = history.pop()
            p.write_text(prev, encoding="utf-8")
            return ToolResult(output=f"Reverted last edit on {path}.")

        return ToolResult(error=f"Unknown command: {command}")
