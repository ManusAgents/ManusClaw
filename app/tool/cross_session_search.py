from __future__ import annotations
"""Cross-session search — FTS5 full-text search across all session messages."""
from app.tool.base import BaseTool
from app.schema import ToolResult


class CrossSessionSearch(BaseTool):
    name = "cross_session_search"
    description = (
        "Search across all past session messages and tool calls using full-text search. "
        "Use to find previous work, recall past solutions, or avoid repeating mistakes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "default": 10},
            "search_in": {"type": "string", "enum": ["messages", "tool_calls", "both"], "default": "both"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, limit: int = 10, search_in: str = "both") -> ToolResult:
        from app.db.session import SessionDB
        db = SessionDB()
        try:
            results = await db.fts_search(query=query, limit=limit, search_in=search_in)
            if not results:
                return ToolResult(output=f"No results for: {query!r}")
            lines = [f"Found {len(results)} result(s) for {query!r}:", ""]
            for r in results:
                lines.append(f"  [{r.get('type','msg')}] session={r.get('session_id','?')} role={r.get('role','?')}")
                lines.append(f"    {r.get('snippet', r.get('content',''))[:200]}")
                lines.append("")
            return ToolResult(output="\n".join(lines))
        except Exception as e:
            return ToolResult(error=str(e))
        finally:
            db.close()
