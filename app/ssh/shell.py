"""ManusClaw Restricted Shell.

Parses, validates, and maps incoming SSH commands to ManusClaw internal APIs.
Only a whitelist of safe management commands is allowed.

Supported Commands:
    status              — Show server status (uptime, version, active sessions)
    restart             — Restart the ManusClaw server (placeholder)
    logs [lines]        — Show recent log lines (default: 50)
    agent --message MSG — Run a one-shot agent prompt
    channels list       — List connected messaging channels
    cron list           — List scheduled cron jobs
    help                — Show available commands
    exit / quit         — Close the SSH session

Usage is internal to the SSH server; this module is not a standalone entry point.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.logger import logger


@dataclass
class ShellResult:
    """Result of a shell command execution."""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def to_output(self) -> str:
        """Format the result for display over SSH."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"ERROR: {self.stderr}")
        return "\n".join(parts) if parts else "(no output)"


# ─── Command Registry ──────────────────────────────────────────────────────────

# Whitelist of allowed commands and their handler names
_ALLOWED_COMMANDS: dict[str, str] = {
    "status": "status",
    "restart": "restart",
    "logs": "logs",
    "agent": "agent",
    "channels": "channels",
    "cron": "cron",
    "help": "help",
    "exit": "exit",
    "quit": "exit",
    "clear": "clear",
}


@dataclass
class _CommandInfo:
    """Parsed command information."""
    command: str
    args: list[str] = field(default_factory=list)
    raw: str = ""


class RestrictedShell:
    """Restricted shell command handler for ManusClaw SSH access.

    Parses incoming SSH commands, validates them against the whitelist,
    and maps them to internal ManusClaw API calls. Unknown or dangerous
    commands are rejected.

    Usage::

        shell = RestrictedShell()
        result = await shell.execute("status")
        print(result.to_output())
    """

    def __init__(self) -> None:
        self._start_time: float = time.time()
        self._motd: str = (
            "═══════════════════════════════════════════════\n"
            "  ManusClaw SSH Gateway — Restricted Shell\n"
            "  Type 'help' for available commands.\n"
            "═══════════════════════════════════════════════\n"
        )

    @property
    def motd(self) -> str:
        """Message of the day, displayed on SSH connection."""
        return self._motd

    @property
    def prompt(self) -> str:
        """Shell prompt string."""
        return "manusclaw> "

    # ─── Parse ─────────────────────────────────────────────────────────────

    def _parse(self, raw_input: str) -> Optional[_CommandInfo]:
        """Parse a raw input string into a command and arguments.

        Args:
            raw_input: The raw command string from the SSH client.

        Returns:
            _CommandInfo if parsing succeeded, None for empty/whitespace-only input.
        """
        stripped = raw_input.strip()
        if not stripped:
            return None

        # Handle shell-style pipes and redirects — reject them
        for char in "|&;`$(){}[]<>!":
            if char in stripped:
                return _CommandInfo(
                    command="",
                    args=[],
                    raw=raw_input,
                )

        parts = stripped.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []

        return _CommandInfo(command=command, args=args, raw=raw_input)

    # ─── Validate ──────────────────────────────────────────────────────────

    def _validate(self, cmd: _CommandInfo) -> Optional[str]:
        """Validate a parsed command against the whitelist.

        Args:
            cmd: The parsed command info.

        Returns:
            Error string if invalid, None if valid.
        """
        if not cmd.command:
            return "Empty command"

        if cmd.command not in _ALLOWED_COMMANDS:
            return (
                f"Unknown command: '{cmd.command}'. "
                f"Allowed: {', '.join(sorted(_ALLOWED_COMMANDS.keys()))}"
            )

        # Extra validation for specific commands
        if cmd.command == "agent":
            if "--message" not in cmd.args:
                return "Usage: agent --message <prompt>"

        if cmd.command == "channels":
            if not cmd.args or cmd.args[0] != "list":
                return "Usage: channels list"

        if cmd.command == "cron":
            if not cmd.args or cmd.args[0] != "list":
                return "Usage: cron list"

        if cmd.command == "logs":
            if cmd.args:
                try:
                    n_lines = int(cmd.args[0])
                    if n_lines < 1 or n_lines > 1000:
                        return "logs: line count must be between 1 and 1000"
                except ValueError:
                    return f"logs: invalid number '{cmd.args[0]}'"

        return None

    # ─── Execute ───────────────────────────────────────────────────────────

    async def execute(self, raw_input: str) -> ShellResult:
        """Parse, validate, and execute a shell command.

        Args:
            raw_input: The raw command string from the SSH client.

        Returns:
            ShellResult with exit_code, stdout, and stderr.
        """
        cmd = self._parse(raw_input)

        if cmd is None:
            return ShellResult(exit_code=0, stdout="")

        # Reject if empty command (means parse found dangerous characters)
        if not cmd.command:
            return ShellResult(
                exit_code=1,
                stderr="Command rejected: contains disallowed characters",
            )

        # Validate
        error = self._validate(cmd)
        if error:
            return ShellResult(exit_code=1, stderr=error)

        # Dispatch
        handler_name = _ALLOWED_COMMANDS[cmd.command]
        handler = getattr(self, f"_cmd_{handler_name}", None)
        if handler is None:
            return ShellResult(exit_code=1, stderr=f"No handler for: {cmd.command}")

        try:
            return await handler(cmd.args)
        except Exception as exc:
            logger.error(f"[SSH:Shell] Command error: {cmd.command}: {exc}")
            return ShellResult(exit_code=1, stderr=f"Internal error: {exc}")

    # ─── Command Handlers ──────────────────────────────────────────────────

    async def _cmd_status(self, args: list[str]) -> ShellResult:
        """Show ManusClaw server status."""
        uptime_s = time.time() - self._start_time
        hours, remainder = divmod(int(uptime_s), 3600)
        minutes, seconds = divmod(remainder, 60)

        # Try to get active session count
        n_sessions = "?"
        try:
            from app.db.session import SessionDB
            db = SessionDB()
            sessions = await db.get_sessions(limit=1)
            n_sessions = str(len(sessions))
        except Exception:
            pass

        lines = [
            f"ManusClaw Server v4.0.0",
            f"Status:     Running",
            f"Uptime:     {hours}h {minutes}m {seconds}s",
            f"Sessions:   {n_sessions}",
            f"SSH:        Connected",
            f"Python:     {__import__('sys').version.split()[0]}",
        ]

        return ShellResult(stdout="\n".join(lines))

    async def _cmd_restart(self, args: list[str]) -> ShellResult:
        """Restart the ManusClaw server (placeholder)."""
        logger.warning("[SSH:Shell] Restart requested via SSH")
        return ShellResult(
            stdout="Restart requested. This is a placeholder — "
                   "use systemd, supervisor, or your process manager to restart.",
        )

    async def _cmd_logs(self, args: list[str]) -> ShellResult:
        """Show recent log lines."""
        n_lines = int(args[0]) if args else 50
        n_lines = max(1, min(n_lines, 1000))

        log_dir = Path("logs")
        if not log_dir.exists():
            return ShellResult(
                stderr="No log directory found. Is the server running?",
            )

        # Find the most recent log file
        log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            return ShellResult(stderr="No log files found.")

        latest_log = log_files[0]

        try:
            lines = latest_log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-n_lines:]
            return ShellResult(stdout="\n".join(tail))
        except Exception as exc:
            return ShellResult(stderr=f"Failed to read log: {exc}")

    async def _cmd_agent(self, args: list[str]) -> ShellResult:
        """Run a one-shot agent prompt."""
        # Parse --message <prompt>
        try:
            idx = args.index("--message")
            prompt = " ".join(args[idx + 1:]) if idx + 1 < len(args) else ""
        except ValueError:
            return ShellResult(stderr="Usage: agent --message <prompt>")

        if not prompt:
            return ShellResult(stderr="No prompt provided")

        logger.info(f"[SSH:Shell] Agent prompt: {prompt[:80]}")

        try:
            from app.agent.manus import Manus

            agent = Manus()
            output = await agent.run(prompt)
            return ShellResult(stdout=output[:4000])
        except Exception as exc:
            logger.error(f"[SSH:Shell] Agent error: {exc}")
            return ShellResult(stderr=f"Agent error: {exc}")

    async def _cmd_channels(self, args: list[str]) -> ShellResult:
        """List connected messaging channels."""
        try:
            from app.messaging.gateway import MessagingGateway
            gw = MessagingGateway()
            platform_names = [a.platform_name for a in gw._adapters if a.is_configured()]

            if not platform_names:
                return ShellResult(
                    stdout="No messaging channels configured.\n"
                           "Set TELEGRAM_TOKEN, DISCORD_TOKEN, etc. to enable."
                )

            lines = ["Configured messaging channels:"]
            for name in platform_names:
                status = "●" if True else "○"  # All configured = active
                lines.append(f"  {status} {name}")

            return ShellResult(stdout="\n".join(lines))

        except Exception as exc:
            return ShellResult(stderr=f"Failed to list channels: {exc}")

    async def _cmd_cron(self, args: list[str]) -> ShellResult:
        """List scheduled cron jobs."""
        try:
            from app.cron import CronScheduler

            scheduler = CronScheduler()
            jobs = scheduler.list_jobs()

            if not jobs:
                return ShellResult(stdout="No cron jobs scheduled.")

            lines = [f"{'ID':<20} {'NAME':<24} {'CRON':<16} {'RUNS':>5}  {'ENABLED'}"]
            lines.append("-" * 72)
            for j in jobs:
                status = "yes" if j.enabled else "no"
                lines.append(
                    f"{j.job_id:<20} {j.name:<24} {j.cron_expr:<16} "
                    f"{j.run_count:>5}  {status}"
                )

            return ShellResult(stdout="\n".join(lines))

        except Exception as exc:
            return ShellResult(stderr=f"Failed to list cron jobs: {exc}")

    async def _cmd_help(self, args: list[str]) -> ShellResult:
        """Show available commands."""
        help_text = """Available commands:

  status                 Show server status
  restart                Request server restart
  logs [lines]           Show recent log lines (default: 50)
  agent --message MSG    Run agent with a one-shot prompt
  channels list          List messaging channels
  cron list              List scheduled cron jobs
  clear                  Clear the screen
  help                   Show this help message
  exit / quit            Close SSH session
"""
        return ShellResult(stdout=help_text)

    async def _cmd_exit(self, args: list[str]) -> ShellResult:
        """Signal that the client wants to disconnect."""
        return ShellResult(exit_code=0, stdout="")

    async def _cmd_clear(self, args: list[str]) -> ShellResult:
        """Send ANSI clear sequence."""
        return ShellResult(stdout="\033[2J\033[H")
