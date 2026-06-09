from __future__ import annotations

"""
ManusClaw Session Tools — Standalone CLI for session management.

Provides commands for listing, inspecting, injecting messages into,
creating, deleting, and exporting sessions from the SessionDB.

Usage from shell::

    manusclaw sessions list
    manusclaw sessions history <session_id>
    manusclaw sessions send <session_id> --message "text"
    manusclaw sessions spawn --prompt "do something"
    manusclaw sessions delete <session_id>
    manusclaw sessions export <session_id> [--output file.json]

Can also be used programmatically:

    from app.session_tools import SessionToolsCLI
    cli = SessionToolsCLI()
    await cli.run(["list"])
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Any, Optional

from app.logger import logger


class SessionToolsCLI:
    """Standalone CLI for session management.

    Commands:
        list     — show all sessions from SessionDB
        history  — print conversation with tool calls for a session
        send     — inject a message into an existing session
        spawn    — create a new session and run a task
        delete   — delete a session and its data
        export   — export session data as JSON
    """

    def __init__(self) -> None:
        self._db = None

    def _get_db(self):
        """Lazy-initialize the SessionDB connection."""
        if self._db is None:
            from app.db.session import SessionDB
            self._db = SessionDB()
        return self._db

    async def run(self, args: Optional[list[str]] = None) -> None:
        """Parse arguments and execute the requested command."""
        parser = self._build_parser()
        parsed = parser.parse_args(args)

        if not hasattr(parsed, "command"):
            parser.print_help()
            return

        try:
            db = self._get_db()
            handler = getattr(self, f"_cmd_{parsed.command}", None)
            if handler is None:
                parser.print_help()
                return
            await handler(db, parsed)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        except Exception as e:
            logger.error(f"[SessionTools] Error: {e}")
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            if self._db:
                self._db.close()

    def _build_parser(self) -> argparse.ArgumentParser:
        """Build the argument parser."""
        parser = argparse.ArgumentParser(
            prog="manusclaw sessions",
            description="ManusClaw Session Management Tools",
        )
        sub = parser.add_subparsers(dest="command", help="Command to execute")

        # list
        sub.add_parser("list", help="Show all sessions")

        # history
        hist = sub.add_parser("history", help="Print session conversation history")
        hist.add_argument("session_id", help="Session ID")
        hist.add_argument("--tool-calls", action="store_true",
                          help="Include tool calls in output")
        hist.add_argument("--limit", type=int, default=50,
                          help="Max messages to show (default: 50)")

        # send
        send = sub.add_parser("send", help="Inject a message into an existing session")
        send.add_argument("session_id", help="Session ID")
        send.add_argument("--message", "-m", required=True,
                           help="Message text to inject")

        # spawn
        spawn = sub.add_parser("spawn", help="Create a new session and run a task")
        spawn.add_argument("--prompt", "-p", required=True,
                           help="Task prompt for the new session")
        spawn.add_argument("--agent", default="manus",
                           help="Agent name (default: manus)")
        spawn.add_argument("--mode", default="build",
                           choices=["build", "plan"],
                           help="Agent mode (default: build)")

        # delete
        delete = sub.add_parser("delete", help="Delete a session")
        delete.add_argument("session_id", help="Session ID")
        delete.add_argument("--force", "-f", action="store_true",
                            help="Skip confirmation prompt")

        # export
        export = sub.add_parser("export", help="Export session data as JSON")
        export.add_argument("session_id", help="Session ID")
        export.add_argument("--output", "-o", default=None,
                            help="Output file path (default: stdout)")
        export.add_argument("--include-tool-calls", action="store_true",
                            help="Include tool calls in export")

        return parser

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    async def _cmd_list(self, db, args) -> None:
        """List all sessions from the SessionDB."""
        sessions = await db.get_sessions(limit=50)
        if not sessions:
            print("No sessions found.")
            return

        print(f"{'ID':<14} {'AGENT':<10} {'STATE':<10} {'STEPS':>5}  {'GOAL'}")
        print("-" * 80)
        for s in sessions:
            goal = (s.get("goal") or "")[:45]
            agent = s.get("agent_name", "manus")
            state = s.get("state", "unknown")
            steps = s.get("step_count", 0)
            print(f"{s['id']:<14} {agent:<10} {state:<10} {steps:>5}  {goal}")
        print(f"\nTotal: {len(sessions)} session(s)")

    async def _cmd_history(self, db, args) -> None:
        """Print conversation history for a session."""
        session_id = args.session_id
        messages = await db.get_session_messages(session_id)

        if not messages:
            print(f"No messages found for session {session_id}")
            return

        print(f"=== Session {session_id} — {len(messages)} messages ===\n")

        for msg in messages[:args.limit]:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "") or ""
            ts = msg.get("ts", 0)
            time_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""

            # Truncate long content
            if len(content) > 500:
                content = content[:500] + "..."

            # Skip empty system messages for brevity
            if role == "SYSTEM" and len(content) > 200:
                content = content[:100] + "..." + content[-50:]

            print(f"[{time_str}] {role}: {content}\n")

        if args.tool_calls:
            tool_calls = await db.get_session_tool_calls(session_id)
            if tool_calls:
                print(f"\n=== Tool Calls ({len(tool_calls)}) ===\n")
                for tc in tool_calls:
                    step = tc.get("step", "?")
                    tool = tc.get("tool_name", "unknown")
                    args_str = (tc.get("args") or "")[:100]
                    output = (tc.get("output") or "")[:200]
                    error = tc.get("error")
                    success = "OK" if tc.get("success") else "FAIL"

                    print(f"  Step {step} | {tool} [{success}]")
                    print(f"    Args: {args_str}")
                    if error:
                        print(f"    Error: {error}")
                    elif output:
                        print(f"    Output: {output}")
                    print()

    async def _cmd_send(self, db, args) -> None:
        """Inject a message into an existing session."""
        session_id = args.session_id
        message = args.message

        # Verify session exists
        sessions = await db.get_sessions(limit=100)
        found = any(s["id"] == session_id for s in sessions)
        if not found:
            print(f"Session {session_id} not found.")
            print("Use 'manusclaw sessions list' to see available sessions.")
            return

        # Log the message
        await db.log_message(session_id, "user", message)
        print(f"Message injected into session {session_id}")
        print(f"  Content: {message[:100]}")

        # Optionally run the agent with this message
        # (This is a simple injection — the agent would need to pick it up
        # on next run. For immediate execution, use spawn instead.)
        print("\nNote: Message logged. Use 'manusclaw sessions spawn' to run a task,")
        print("or reconnect to this session for the agent to process it.")

    async def _cmd_spawn(self, db, args) -> None:
        """Create a new session and run a task."""
        prompt = args.prompt
        agent_name = args.agent
        mode = args.mode

        print(f"Spawning new session...")
        print(f"  Agent: {agent_name}")
        print(f"  Mode:  {mode}")
        print(f"  Prompt: {prompt[:80]}...\n")

        try:
            from app.agent.manus import Manus
            agent = Manus()
            result = await agent.run(prompt)
            session_id = agent._session_id or "unknown"
            print(f"Session {session_id} completed.")
            print(f"\n--- Result ---")
            print(result or "(no output)")
        except Exception as e:
            print(f"Spawn failed: {e}")
            raise

    async def _cmd_delete(self, db, args) -> None:
        """Delete a session."""
        session_id = args.session_id

        if not args.force:
            confirm = input(f"Delete session {session_id}? [y/N] ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return

        # Delete messages and tool calls, then the session
        try:
            def _delete():
                conn = db._ensure()
                conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM tool_calls WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                conn.commit()

            await asyncio.to_thread(_delete)
            print(f"Session {session_id} deleted.")
        except Exception as e:
            print(f"Delete failed: {e}")
            raise

    async def _cmd_export(self, db, args) -> None:
        """Export session data as JSON."""
        session_id = args.session_id

        # Gather session data
        sessions = await db.get_sessions(limit=100)
        session_info = next((s for s in sessions if s["id"] == session_id), None)

        if not session_info:
            print(f"Session {session_id} not found.")
            return

        export_data: dict[str, Any] = {
            "session": session_info,
            "messages": await db.get_session_messages(session_id),
        }

        if args.include_tool_calls:
            export_data["tool_calls"] = await db.get_session_tool_calls(session_id)

        json_str = json.dumps(export_data, indent=2, default=str)

        if args.output:
            from pathlib import Path
            Path(args.output).write_text(json_str)
            print(f"Exported to {args.output}")
        else:
            print(json_str)


def main() -> None:
    """Entry point for ``manusclaw sessions`` command."""
    cli = SessionToolsCLI()
    if len(sys.argv) > 1 and sys.argv[1] != "sessions":
        # If called as `manusclaw sessions list`, strip the "sessions" prefix
        # If called as `python -m app.session_tools list`, pass all args
        asyncio.run(cli.run(sys.argv[1:]))
    else:
        # Called as `manusclaw sessions list` — sys.argv[1] is "sessions"
        asyncio.run(cli.run(sys.argv[2:]))


if __name__ == "__main__":
    main()
