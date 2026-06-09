from __future__ import annotations
"""Skill manager tool — create, patch, delete, list, load skills."""
from app.tool.base import BaseTool
from app.schema import ToolResult


class SkillManagerTool(BaseTool):
    name = "skill_manager"
    description = (
        "Manage procedural skills (reusable knowledge stored as Markdown). "
        "Actions: list, get, create, patch, delete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "create", "patch", "delete"]},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "version": {"type": "string"},
        },
        "required": ["action"],
    }

    async def execute(self, action: str, name: str = "", description: str = "",
                      content: str = "", tags=None, version: str = "1.0.0") -> ToolResult:
        from app.skills.skill_engine import get_skill_engine
        engine = get_skill_engine()
        try:
            if action == "list":
                skills = engine.list_skills()
                if not skills:
                    return ToolResult(output="No skills. Use create to add one.")
                lines = [f"Skills ({len(skills)}):"]
                for s in skills:
                    lines.append(f"  {s.name} v{s.version}: {s.description[:60]}")
                return ToolResult(output="\n".join(lines))
            elif action == "get":
                skill = engine.get(name)
                if not skill:
                    return ToolResult(error=f"Skill not found: {name}")
                return ToolResult(output=skill.to_file_content())
            elif action == "create":
                if not name or not content:
                    return ToolResult(error="name and content required")
                skill = engine.create(name, description, content, tags, version)
                return ToolResult(output=f"Skill created: {name} at {skill.path}")
            elif action == "patch":
                skill = engine.patch(name, content or None, description or None, version or None)
                if not skill:
                    return ToolResult(error=f"Not found: {name}")
                return ToolResult(output=f"Patched: {name} v{skill.version}")
            elif action == "delete":
                ok = engine.delete(name)
                return ToolResult(output=f"Deleted: {name}") if ok else ToolResult(error=f"Not found: {name}")
        except Exception as e:
            return ToolResult(error=str(e))
