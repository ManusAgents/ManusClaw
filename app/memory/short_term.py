from __future__ import annotations

"""
Short-Term Memory — context-window-aware conversation buffer.
Extends base Memory with sliding-window trimming, role pinning, and
a clean API for injecting summaries / context refreshes.
"""

from typing import Optional

from app.schema import Memory, Message, Role


class ShortTermMemory(Memory):
    """
    Wraps the base Memory with extra utilities.

    - System messages are always pinned (never trimmed)
    - Trimming removes oldest non-system messages first
    - Supports snapshot/restore for Plan Mode dry-runs
    """

    def add_context_refresh(self, summary: str) -> None:
        """Inject a task-history summary as a user message."""
        self.add(Message.user(f"[CONTEXT REFRESH]\n{summary}\n[END CONTEXT REFRESH]"))

    def snapshot(self) -> list[Message]:
        """Return a deep copy of current messages for Plan Mode dry-runs."""
        return [m.model_copy() for m in self.messages]

    def restore(self, snapshot: list[Message]) -> None:
        self.messages = snapshot

    def last_assistant(self) -> Optional[str]:
        for m in reversed(self.messages):
            if m.role == Role.ASSISTANT and m.content:
                return m.content
        return None

    def inject_system_update(self, content: str) -> None:
        """Append or update the first system message with extra content."""
        for i, m in enumerate(self.messages):
            if m.role == Role.SYSTEM:
                self.messages[i] = Message.system(m.content + "\n\n" + content)
                return
        self.messages.insert(0, Message.system(content))

    def token_estimate(self) -> int:
        total = sum(len(m.content or "") for m in self.messages)
        return total // 4

    def recent_tool_calls(self, n: int = 5) -> list[str]:
        """Return last N tool names called (for ToolSelector recency tracking)."""
        names: list[str] = []
        for m in self.messages:
            if m.tool_calls:
                for tc in m.tool_calls:
                    names.append(tc.function.name)
        return names[-n:]
