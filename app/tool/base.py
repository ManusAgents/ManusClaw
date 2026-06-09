from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.schema import ToolResult


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> ToolResult:
        try:
            return await self.execute(**kwargs)
        except Exception as e:
            return ToolResult(error=str(e))

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool and return a ToolResult."""

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def cleanup(self) -> None:
        """Release any held resources."""


class ToolCollection:
    def __init__(self, *tools: BaseTool) -> None:
        self._tools: dict[str, BaseTool] = {t.name: t for t in tools}

    def add(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    async def execute(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"Tool '{name}' not found. Available: {list(self._tools)}")
        return await tool(**kwargs)

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    async def cleanup_all(self) -> None:
        for tool in self._tools.values():
            try:
                await tool.cleanup()
            except Exception:
                pass

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self):
        return len(self._tools)
