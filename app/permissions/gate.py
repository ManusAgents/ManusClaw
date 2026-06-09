from __future__ import annotations

"""
PermissionGate — 3-Tier Permission System
==========================================

Tier 1 — ALLOW:  Automatic. No confirmation needed.
Tier 2 — ASK:    Requires user confirmation in Plan Mode. Auto-approved in Build Mode.
Tier 3 — DENY:   Blocked unconditionally (catastrophic OS operations only).

Agent Modes:
  Build Mode — ASK actions are auto-approved. Full autonomous execution.
  Plan Mode  — ASK actions pause and prompt the user. Read-only by default.
"""

import re
from enum import Enum
from typing import Optional


class PermissionTier(str, Enum):
    ALLOW = "allow"
    ASK   = "ask"
    DENY  = "deny"


class AgentMode(str, Enum):
    BUILD = "build"   # Full auto — ASK actions are silently approved
    PLAN  = "plan"    # Conservative — ASK actions pause for user input


class PermissionDenied(Exception):
    pass


# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

# Tools that are always ALLOW (read-only, safe)
_ALLOW_TOOLS = {
    "web_search",
    "crawl",
    "str_replace_editor_view",   # view-only variant
    "ask_human",
    "planning",
    "terminate",
    "data_viz",
}

# Tools that require ASK in Plan Mode
_ASK_TOOLS = {
    "bash",
    "python_execute",
    "str_replace_editor",   # write/edit/create
    "browser_use",          # could submit forms, login, etc.
    "platform_control",     # FIX: controls external platforms — must require ASK
    "delegate",             # FIX: spawns subagent — require explicit approval
}

# Tools that are always DENY — never run
_DENY_TOOLS: set[str] = set()   # No tools are permanently denied by name

# ---------------------------------------------------------------------------
# Command-level DENY patterns (catastrophic OS operations only)
# These are the ONLY hard blocks. Everything else is allowed.
# ---------------------------------------------------------------------------

_DENY_COMMAND_PATTERNS = [
    # Wipe root filesystem
    r"rm\s+-[rRf]+\s+/\s*$",
    r"rm\s+-[rRf]+\s+/\*",
    r"rm\s+--no-preserve-root",
    # Fork bomb
    r":\(\)\s*\{.*:\|:.*\}",
    # Zero out block devices
    r"dd\s+if=/dev/zero\s+of=/dev/(s|h|v|xv)d",
    r">\s*/dev/(s|h|v|xv)d[a-z]$",
    # Format disks
    r"mkfs\.",
    r"fdisk.*--wipe",
    r"wipefs",
    # Kill all processes
    r"kill\s+-9\s+-1\b",
    r"killall\s+-9\b",
    # Shred system binaries
    r"shred\s+.*(bin|sbin|lib|usr|etc)/",
]

_DENY_CODE_PATTERNS = [
    # Python fork bomb equivalent
    r"os\.fork\(\).*while True",
    r"multiprocessing.*spawn.*while True",
    # Overwrite /dev/sda from Python
    r"open\(['\"]\/dev\/(s|h)d",
]


class PermissionGate:
    """
    Central permission checker. Instantiate once per agent run.
    """

    def __init__(self, mode: AgentMode = AgentMode.BUILD) -> None:
        self.mode = mode
        self._approved_this_session: set[str] = set()

    # ------------------------------------------------------------------
    # Main check API
    # ------------------------------------------------------------------

    def check_tool(self, tool_name: str, args: dict) -> PermissionTier:
        """
        Returns the effective permission tier for a tool call.
        Raises PermissionDenied if the call must be blocked.
        """
        # Hard deny — tool name
        if tool_name in _DENY_TOOLS:
            raise PermissionDenied(f"Tool '{tool_name}' is permanently blocked.")

        # Check command/code content for catastrophic patterns
        content = self._extract_content(tool_name, args)
        if content:
            self._check_catastrophic(tool_name, content)

        # Tier resolution
        if tool_name in _ALLOW_TOOLS:
            return PermissionTier.ALLOW

        if tool_name in _ASK_TOOLS:
            if self.mode == AgentMode.BUILD:
                return PermissionTier.ALLOW   # Build Mode auto-approves
            # Plan Mode — check if already approved this session
            key = self._approval_key(tool_name, args)
            if key in self._approved_this_session:
                return PermissionTier.ALLOW
            return PermissionTier.ASK

        # Unknown tools default to ASK
        return PermissionTier.ASK

    def approve(self, tool_name: str, args: dict) -> None:
        """Mark an ASK action as user-approved for this session."""
        self._approved_this_session.add(self._approval_key(tool_name, args))

    def is_build_mode(self) -> bool:
        return self.mode == AgentMode.BUILD

    def is_plan_mode(self) -> bool:
        return self.mode == AgentMode.PLAN

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_content(self, tool_name: str, args: dict) -> Optional[str]:
        if tool_name == "bash":
            return args.get("command", "")
        if tool_name == "python_execute":
            return args.get("code", "")
        return None

    def _check_catastrophic(self, tool_name: str, content: str) -> None:
        patterns = (
            _DENY_COMMAND_PATTERNS if tool_name == "bash"
            else _DENY_CODE_PATTERNS if tool_name == "python_execute"
            else _DENY_COMMAND_PATTERNS + _DENY_CODE_PATTERNS
        )
        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE | re.DOTALL):
                raise PermissionDenied(
                    f"🚫 BLOCKED — matched catastrophic pattern: `{pattern}`\n"
                    f"This operation would cause irreversible OS damage and cannot be executed.\n"
                    f"All other system operations, file edits, network access, and package "
                    f"installations are fully permitted."
                )

    def _approval_key(self, tool_name: str, args: dict) -> str:
        content = self._extract_content(tool_name, args) or str(sorted(args.items()))
        return f"{tool_name}:{hash(content[:200])}"

    # ------------------------------------------------------------------
    # Plan Mode interactive prompt
    # ------------------------------------------------------------------

    async def request_approval(self, tool_name: str, args: dict, description: str = "") -> bool:
        """
        In Plan Mode, print the pending action and ask the user for approval.
        Returns True if approved, False if rejected.
        """
        import asyncio
        from app.logger import logger
        preview = description or f"{tool_name}({str(args)[:120]})"
        print(f"\n⏸  [PLAN MODE] Pending action requires approval:")
        print(f"   Tool: {tool_name}")
        print(f"   Preview: {preview}")
        # Fix: use run_in_executor to avoid blocking the event loop
        try:
            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(
                None, lambda: input("   Approve? [y/N]: ").strip().lower()
            )
        except EOFError:
            logger.info(f"[PermissionGate] No interactive terminal — auto-rejecting: {tool_name}")
            return False
        approved = answer in ("y", "yes")
        if approved:
            self.approve(tool_name, args)
            logger.info(f"[PermissionGate] User approved: {tool_name}")
        else:
            logger.info(f"[PermissionGate] User rejected: {tool_name}")
        return approved
