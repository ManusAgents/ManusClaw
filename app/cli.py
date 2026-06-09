from __future__ import annotations

"""
ManusClaw CLI — Persistent AI Operating Shell

Activation:
  Just type: manusclaw
  The AI environment activates and enters a persistent interactive shell.
  After activation, type tasks naturally — no need to prefix with 'manusclaw' again.

  Example:
    $ manusclaw
    ManusClaw v4.0.0 — AI Operating Environment Active
    You> Create a Python web scraper for me
    You> Build a website
    You> Analyze this data

Features:
  - Persistent AI shell (activation via single 'manusclaw' command)
  - Task queue with background execution and resume capability
  - Natural language prompts after activation (no repeated command prefix)
  - Slash commands for session/task management
  - Skin engine (YAML-based, 4 built-in skins: default/ares/mono/slate)
  - Spinner during API calls
  - Auto-resume of interrupted tasks on startup

Slash commands:
  /model      — show/switch LLM model
  /skills     — list loaded skills
  /tools      — list available tools
  /memory     — show MEMORY.md content
  /compress   — compress current session context
  /new        — start new session
  /resume     — resume a past session
  /branch     — branch current session
  /tasks      — show task queue status
  /bg <task>  — submit task to background queue
  /help       — show commands
  /exit       — quit (running tasks continue in background)
"""

import asyncio
import signal
import sys
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Version
# ──────────────────────────────────────────────────────────────────────────────

VERSION = "4.0.0"

# ──────────────────────────────────────────────────────────────────────────────
# Skin definitions (data-driven, YAML-loadable)
# ──────────────────────────────────────────────────────────────────────────────

SKINS = {
    "default": {"border": "gold1",          "accent": "yellow",  "user": "cyan",     "agent": "green",  "tool": "magenta", "error": "red"},
    "ares":    {"border": "red1",            "accent": "red",     "user": "white",    "agent": "red",    "tool": "orange1", "error": "bright_red"},
    "mono":    {"border": "white",           "accent": "white",   "user": "white",    "agent": "bright_white", "tool": "grey70", "error": "white"},
    "slate":   {"border": "steel_blue1",     "accent": "blue",    "user": "sky_blue1","agent": "pale_green1","tool": "orchid","error": "red"},
}

ROLE_EMOJI = {"user": "👤", "assistant": "🤖", "tool": "🔧", "system": "⚙"}

SLASH_COMMANDS = [
    "/model", "/skills", "/tools", "/memory", "/compress",
    "/new", "/resume", "/branch", "/tasks", "/bg", "/sessions",
    "/help", "/exit",
]


def _get_skin(name: str = "default") -> dict:
    """Load skin: check ~/.manusclaw/skins/<name>.yaml first, then built-ins."""
    from pathlib import Path
    import os
    skin_file = Path(os.getenv("MANUSCLAW_HOME", Path.home() / ".manusclaw")) / "skins" / f"{name}.yaml"
    if skin_file.exists():
        try:
            import yaml
            data = yaml.safe_load(skin_file.read_text()) or {}
            if data:
                return {**SKINS.get("default", {}), **data}
        except Exception:
            pass
    return SKINS.get(name, SKINS["default"])


# ──────────────────────────────────────────────────────────────────────────────
# Rich-based output helpers
# ──────────────────────────────────────────────────────────────────────────────

def _print_banner(skin: dict, model_name: str = "") -> None:
    """Print the ManusClaw activation banner."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
        console = Console()
        banner_text = Text()
        banner_text.append("ManusClaw", style=f"bold {skin['accent']}")
        banner_text.append(f" v{VERSION}", style="bold white")
        banner_text.append("\nAI Operating Environment Active", style=skin['accent'])
        if model_name:
            banner_text.append(f"\nModel: {model_name}", style="dim")
        banner_text.append("\n\nType your task naturally. Use /help for commands.", style="dim")
        console.print(Panel(banner_text, border_style=skin["border"], expand=False, padding=(1, 2)))
    except ImportError:
        print(f"\n  ╔══════════════════════════════════════════╗")
        print(f"  ║  ManusClaw v{VERSION}                       ║")
        print(f"  ║  AI Operating Environment Active         ║")
        if model_name:
            print(f"  ║  Model: {model_name:<33}║")
        print(f"  ║  Type tasks naturally. /help for commands ║")
        print(f"  ╚══════════════════════════════════════════╝\n")


def _print_header(skin: dict, model: str, session_id: str, step: int = 0) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        console = Console()
        console.print(Panel(
            f"[bold {skin['accent']}]ManusClaw[/] | model: {model} | session: {session_id[:8]} | step: {step}",
            border_style=skin["border"],
            expand=True,
        ))
    except ImportError:
        print(f"=== ManusClaw | {model} | {session_id[:8]} ===")


def _print_message(role: str, content: str, skin: dict) -> None:
    emoji = ROLE_EMOJI.get(role, "•")
    color = skin.get(role if role in ("user", "tool", "error") else "agent", "white")
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        console = Console()
        label = f"[{color}]{emoji} {role.upper()}[/{color}]"
        if role == "assistant":
            console.print(f"{label}")
            console.print(Markdown(content))
        else:
            console.print(f"{label}: {content[:2000]}")
    except ImportError:
        print(f"{emoji} {role.upper()}: {content[:500]}")


def _print_tool_activity(tool_name: str, args_preview: str, skin: dict) -> None:
    try:
        from rich.console import Console
        console = Console()
        console.print(f"  [{skin['tool']}]🔧 {tool_name}[/] — {args_preview[:80]}")
    except ImportError:
        print(f"  [TOOL] {tool_name}: {args_preview[:80]}")


# ──────────────────────────────────────────────────────────────────────────────
# Spinner context manager
# ──────────────────────────────────────────────────────────────────────────────

class Spinner:
    """Contextual spinner using Rich if available, otherwise plain text.

    FIX: Enhanced for long-wait scenarios. When deep-thinking models (DeepSeek R1,
    o1, etc.) are processing, the spinner shows periodic progress updates so the
    user knows the system is still working and hasn't frozen.
    """

    def __init__(self, verb: str = "thinking", skin: Optional[dict] = None) -> None:
        self.verb = verb
        self.skin = skin or SKINS["default"]
        self._status = None
        self._console = None
        self._start_time = None

    def __enter__(self):
        import time as _time
        self._start_time = _time.monotonic()
        try:
            from rich.console import Console
            self._console = Console()
            self._status = self._console.status(
                f"[{self.skin['accent']}]{self.verb}...[/]", spinner="dots"
            )
            self._status.__enter__()
        except ImportError:
            print(f"  {self.verb}...", end="", flush=True)
        return self

    def update(self, verb: str = None, elapsed: float = None) -> None:
        """Update the spinner with new status — useful during long waits."""
        if verb:
            self.verb = verb
        try:
            if self._status and self._console:
                msg = f"[{self.skin['accent']}]{self.verb}...[/]"
                if elapsed and elapsed > 30:
                    mins, secs = divmod(int(elapsed), 60)
                    msg += f" [{min(secs, 59)}s elapsed]"
                    if elapsed > 120:
                        msg = f"[{self.skin['accent']}]{self.verb}...[/] [{mins}m {secs}s elapsed]"
                self._status.update(msg)
        except Exception:
            pass

    def __exit__(self, *args):
        import time as _time
        if self._start_time:
            elapsed = _time.monotonic() - self._start_time
            if elapsed > 60:
                mins, secs = divmod(int(elapsed), 60)
                logger.info(f"[Spinner] Long operation completed: {mins}m {secs}s")
        if self._status:
            self._status.__exit__(*args)
        else:
            print(" done")


# ──────────────────────────────────────────────────────────────────────────────
# Slash command handlers
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_slash(cmd: str, agent=None, session_id: str = "",
                        task_queue=None) -> Optional[str]:
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        help_text = (
            "ManusClaw Commands:\n"
            "  /model [name]  — show/switch LLM model\n"
            "  /skills        — list loaded skills\n"
            "  /tools         — list available tools\n"
            "  /memory        — show MEMORY.md content\n"
            "  /compress      — compress current session context\n"
            "  /new           — start new session\n"
            "  /resume        — resume a past session\n"
            "  /branch        — branch current session\n"
            "  /tasks         — show background task queue status\n"
            "  /bg <task>     — submit task to background queue\n"
            '  /sessions list — show all sessions\n'
            '  /sessions history <id> — show session conversation\n'
            '  /sessions send <id> -m "text" — inject message\n'
            '  /sessions spawn -p "task" — create and run session\n'
            "  /exit          — quit (background tasks continue)\n"
        )
        return help_text

    if command == "/model":
        from app.config import Config
        cfg = Config.get()
        if arg:
            cfg._data.llm.model = arg
            return f"Model set to: {arg}"
        return f"Current model: {cfg.llm.model} (provider: {cfg.llm.provider})"

    if command == "/skills":
        try:
            from app.skills.skill_engine import get_skill_engine
            skills = get_skill_engine().list_skills()
            if not skills:
                return "No skills loaded."
            return "\n".join(f"  {s.name} v{s.version}: {s.description[:60]}" for s in skills)
        except Exception as e:
            return f"Skills error: {e}"

    if command == "/tools":
        if agent and hasattr(agent, "tools"):
            names = list(agent.tools._tools.keys())
            return "Tools: " + ", ".join(names)
        return "No agent active."

    if command == "/memory":
        from pathlib import Path
        mf = Path("workspace/MEMORY.md")
        if mf.exists():
            return mf.read_text()
        return "MEMORY.md is empty."

    if command == "/compress":
        if session_id and agent:
            from app.db.session import SessionDB
            db = SessionDB()
            summary = agent._task_history.context_summary() if agent._task_history else "No task history."
            await db.compress_session(session_id, summary)
            db.close()
            return f"Session {session_id[:8]} compressed."
        return "No active session."

    if command == "/new":
        return "NEW_SESSION"

    if command == "/branch":
        if session_id:
            from app.db.session import SessionDB
            db = SessionDB()
            new_sid = await db.branch_session(session_id, arg or None)
            db.close()
            return f"Branched session: {new_sid}"
        return "No active session to branch."

    if command == "/resume":
        from app.db.session import SessionDB
        db = SessionDB()
        sessions = await db.get_sessions(limit=10)
        db.close()
        if not sessions:
            return "No sessions found."
        lines = ["Recent sessions:"]
        for s in sessions:
            lines.append(f"  {s['id']} | {s['state']} | {s['goal'][:50]}")
        return "\n".join(lines)

    if command == "/tasks":
        if task_queue:
            summary = await task_queue.status_summary()
            lines = ["Task Queue:"]
            lines.append(f"  Total: {summary.get('total', 0)}")
            lines.append(f"  Queued: {summary.get('queued', 0)}")
            lines.append(f"  Running: {summary.get('running', 0)}")
            lines.append(f"  Completed: {summary.get('completed', 0)}")
            lines.append(f"  Failed: {summary.get('failed', 0)}")
            lines.append(f"  Paused: {summary.get('paused', 0)}")
            # Show recent tasks
            tasks = await task_queue.list_tasks()
            recent = [t for t in tasks if t.status.value in ("queued", "running", "paused")][:5]
            if recent:
                lines.append("\n  Active tasks:")
                for t in recent:
                    lines.append(f"    [{t.status.value}] {t.id} — {t.prompt[:50]}")
            return "\n".join(lines)
        return "Task queue not initialized."

    if command == "/bg":
        if not arg:
            return "Usage: /bg <task description>"
        if task_queue:
            from app.task_queue import TaskPriority
            task = await task_queue.submit(arg, priority=TaskPriority.NORMAL)
            return f"Task submitted to background queue: {task.id}\nUse /tasks to monitor progress."
        return "Task queue not initialized."

    if command == "/sessions":
        return await _handle_sessions(arg, agent, session_id)

    if command == "/exit":
        return "EXIT"

    return f"Unknown command: {command}. Type /help for help."


# ──────────────────────────────────────────────────────────────────────────────
# /sessions subcommand handler
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_sessions(arg: str, agent=None, session_id: str = "") -> str:
    """Handle /sessions subcommands: list, history, send, spawn."""
    parts = arg.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subarg = parts[1] if len(parts) > 1 else ""

    if not subcmd or subcmd == "list":
        from app.db.session import SessionDB
        db = SessionDB()
        sessions = await db.get_sessions(limit=20)
        db.close()
        if not sessions:
            return "No sessions found."
        lines = ["Sessions:", f"{'ID':<14} {'STATE':<10} {'AGENT':<10} {'GOAL'}", "-" * 70]
        for s in sessions:
            goal = (s.get("goal") or "")[:40]
            lines.append(f"  {s['id']:<14} {s.get('state', '?'):<10} {s.get('agent_name', 'manus'):<10} {goal}")
        return "\n".join(lines)

    if subcmd == "history":
        if not subarg:
            return "Usage: /sessions history <session_id>"
        from app.db.session import SessionDB
        db = SessionDB()
        messages = await db.get_session_messages(subarg.strip())
        tool_calls = await db.get_session_tool_calls(subarg.strip())
        db.close()
        if not messages:
            return f"No messages found for session {subarg.strip()}"
        lines = [f"=== Session {subarg.strip()} ==="]
        for msg in messages[-30:]:
            role = msg.get("role", "?").upper()
            content = (msg.get("content") or "")[:200]
            if role != "SYSTEM":
                lines.append(f"  [{role}] {content}")
        if tool_calls:
            lines.append(f"\n  ({len(tool_calls)} tool call(s))")
        return "\n".join(lines)

    if subcmd == "send":
        # Parse: send <session_id> --message "text" or send <session_id> -m "text"
        import shlex
        tokens = list(shlex.split(subarg)) if subarg else []
        if not tokens:
            return "Usage: /sessions send <session_id> --message \"text\""
        sid = tokens[0]
        msg_text = ""
        i = 1
        while i < len(tokens):
            if tokens[i] in ("--message", "-m") and i + 1 < len(tokens):
                msg_text = tokens[i + 1]
                break
            i += 1
        if not msg_text:
            return "Usage: /sessions send <session_id> --message \"text\""
        from app.db.session import SessionDB
        db = SessionDB()
        await db.log_message(sid, "user", msg_text)
        db.close()
        return f"Message injected into session {sid}."

    if subcmd == "spawn":
        # Parse: spawn --prompt "task" or spawn -p "task"
        import shlex
        tokens = list(shlex.split(subarg)) if subarg else []
        prompt_text = ""
        i = 0
        while i < len(tokens):
            if tokens[i] in ("--prompt", "-p") and i + 1 < len(tokens):
                prompt_text = tokens[i + 1]
                break
            i += 1
        if not prompt_text:
            return "Usage: /sessions spawn --prompt \"task description\""
        return f"SPAWN_TASK:{prompt_text}"

    return f"Unknown sessions subcommand: {subcmd}. Use: list, history, send, spawn"


# ──────────────────────────────────────────────────────────────────────────────
# prompt_toolkit input layer
# ──────────────────────────────────────────────────────────────────────────────

def _get_completer():
    try:
        from prompt_toolkit.completion import WordCompleter
        return WordCompleter(SLASH_COMMANDS, pattern=r"^/\w*")
    except ImportError:
        return None


def _get_session():
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from pathlib import Path
        history_file = Path.home() / ".manusclaw" / ".cli_history"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        return PromptSession(history=FileHistory(str(history_file)))
    except ImportError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Background task executor
# ──────────────────────────────────────────────────────────────────────────────

async def _execute_background_task(task_entry) -> str:
    """Execute a task from the background queue."""
    from app.agent.manus import Manus
    agent = Manus()
    try:
        # If task has a checkpoint, restore memory state
        if task_entry.checkpoint and task_entry.checkpoint.memory_snapshot:
            from app.schema import Message
            messages = [Message.from_dict(m) for m in task_entry.checkpoint.memory_snapshot]
            agent.memory.messages = messages
            logger.info(f"[BG Task {task_entry.id}] Restored from checkpoint at step {task_entry.checkpoint.step_count}")

        result = await agent.run(task_entry.prompt)

        # Save checkpoint periodically is handled by the agent loop
        return result or "Task completed (no output)"
    except Exception as e:
        return f"Task failed: {e}"
    finally:
        await agent.cleanup()


# ──────────────────────────────────────────────────────────────────────────────
# Main interactive loop — persistent AI operating shell
# ──────────────────────────────────────────────────────────────────────────────

async def _interactive_loop(skin_name: str = "default") -> None:
    from app.agent.manus import Manus
    from app.config import Config
    from app.task_queue import TaskQueue
    from app.logger import logger

    skin = _get_skin(skin_name)
    cfg = Config.get()
    model_name = cfg.llm.model
    agent = Manus()
    session_id = ""
    pt_session = _get_session()
    completer = _get_completer()

    # Initialize task queue for background execution
    task_queue = TaskQueue(max_workers=1)
    task_queue.set_executor(_execute_background_task)

    # Resume any interrupted tasks from previous sessions
    resumed_count = await task_queue.resume_interrupted()
    if resumed_count > 0:
        _print_message("system", f"Resumed {resumed_count} interrupted task(s) from previous session.", skin)

    # Start background workers
    await task_queue.start_workers()

    # Print activation banner
    _print_banner(skin, model_name)

    # Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        shutdown_event.set()

    try:
        # Register signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler, sig, None)
            except (NotImplementedError, OSError):
                pass  # Windows doesn't support add_signal_handler
    except RuntimeError:
        pass

    while not shutdown_event.is_set():
        try:
            if pt_session and completer:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: pt_session.prompt("You> ", completer=completer, multiline=False)
                )
            else:
                user_input = await asyncio.get_event_loop().run_in_executor(None, input, "You> ")
        except (EOFError, KeyboardInterrupt):
            print("\n")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Handle slash commands
        if user_input.startswith("/"):
            result = await _handle_slash(
                user_input, agent=agent, session_id=session_id,
                task_queue=task_queue,
            )
            if result == "EXIT":
                break
            if result == "NEW_SESSION":
                await agent.cleanup()
                agent = Manus()
                session_id = ""
                _print_message("system", "Started new session.", skin)
                continue
            if result:
                _print_message("system", result, skin)
            continue

        # Regular prompt — run agent (foreground)
        _print_message("user", user_input, skin)

        # FIX: Identity guard — detect jailbreak attempts before processing
        from app.agent.identity_guard import detect_manipulation
        is_manipulation, matched_pattern = detect_manipulation(user_input)
        if is_manipulation:
            logger.warning(
                f"[IdentityGuard] Manipulation attempt in CLI: '{matched_pattern}'"
            )

        verb = "thinking" if "?" not in user_input else "researching"

        try:
            with Spinner(verb=verb, skin=skin):
                result = await agent.run(user_input)
            session_id = agent._session_id or ""
            _print_message("assistant", result or "(no output)", skin)
            _print_header(skin, model_name, session_id, agent._step_count)
        except Exception as e:
            _print_message("error", f"Error: {e}", skin)

        # Reset agent state for next prompt (without losing session context)
        from app.schema import AgentState
        agent.state = AgentState.IDLE
        agent._step_count = 0

    # Graceful shutdown
    _print_message("system", "Shutting down... Background tasks will continue and can be resumed next session.", skin)
    await task_queue.stop_workers()
    await agent.cleanup()
    print("Goodbye. ManusClaw tasks are saved — run 'manusclaw' again to resume.")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="ManusClaw — Autonomous AI Operating Environment",
        prog="manusclaw",
    )
    parser.add_argument("prompt", nargs="*", help="Task prompt (omit for interactive shell)")
    parser.add_argument("--skin", default="default", choices=list(SKINS.keys()), help="UI skin")
    parser.add_argument("--model", help="Override LLM model")
    parser.add_argument("--profile", help="Config profile name")
    parser.add_argument("--no-color", action="store_true", help="Disable colors")
    parser.add_argument("--version", action="version", version=f"ManusClaw v{VERSION}")
    args = parser.parse_args()

    if args.profile:
        import os
        os.environ["MANUSCLAW_PROFILE"] = args.profile

    if args.model:
        import os
        os.environ["LLM_MODEL_OVERRIDE"] = args.model

    if args.prompt:
        # Single-shot mode: manusclaw "do something"
        prompt_text = " ".join(args.prompt)

        async def _run_once():
            from app.agent.manus import Manus
            from app.task_queue import TaskQueue
            skin = _get_skin(args.skin)
            agent = Manus()

            # Also init task queue to resume interrupted tasks
            task_queue = TaskQueue(max_workers=1)
            resumed = await task_queue.resume_interrupted()
            if resumed:
                _print_message("system", f"Resumed {resumed} background task(s).", skin)

            with Spinner(verb="thinking", skin=skin):
                result = await agent.run(prompt_text)
            _print_message("assistant", result or "(no output)", skin)
            await agent.cleanup()

        asyncio.run(_run_once())
    else:
        # Interactive shell mode: manusclaw (persistent AI operating environment)
        try:
            asyncio.run(_interactive_loop(skin_name=args.skin))
        except KeyboardInterrupt:
            print("\nGoodbye. Run 'manusclaw' again to resume.")


if __name__ == "__main__":
    main()
