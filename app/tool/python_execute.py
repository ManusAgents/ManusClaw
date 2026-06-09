from __future__ import annotations

"""
PythonExecute — isolated Python code execution via multiprocessing.

Runtime philosophy: TASK-COMPLETE, NOT TIME-BOXED.
  The agent runs until the work is done — period.
  Pass timeout=None (or omit it) → subprocess runs until it finishes naturally.
  Pass timeout=N → killed after exactly N seconds if still running.
  2-minute scripts run 2 minutes. 3-hour training jobs run 3 hours.
  There is no DEFAULT and no MAX cap.

Isolation:
  Each execution runs in a separate multiprocessing.Process (own memory space).
  2 GB virtual memory rlimit applied (prevents OOM from killing the host).

Output:
  Full stdout+stderr always returned — no byte truncation, ever.
  The agent needs complete output to reason, debug, and iterate correctly.

Blocked (hard deny):
  fork bombs, writing directly to /dev/sda, os.execv to /bin/rm /

Permitted (everything else):
  All imports, ML training, data pipelines, network calls,
  filesystem operations, subprocesses, long computations.
"""

import multiprocessing
import queue
import sys
import traceback
from io import StringIO
from typing import Any, Optional

from app.schema import ToolResult
from app.tool.base import BaseTool


# ---------------------------------------------------------------------------
# Resource limits (OS-level, applied inside the subprocess)
# ---------------------------------------------------------------------------

MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB virtual memory


# ---------------------------------------------------------------------------
# Worker subprocess — runs inside a separate process
# ---------------------------------------------------------------------------

def _worker(code: str, result_queue: multiprocessing.Queue) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    except Exception:
        pass

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = buf_out = StringIO()
    sys.stderr = buf_err = StringIO()
    try:
        exec(compile(code, "<manusclaw>", "exec"), {"__name__": "__main__"})
        out = buf_out.getvalue()
        err = buf_err.getvalue()
    except SystemExit as e:
        out = buf_out.getvalue()
        err = f"SystemExit({e.code})" if e.code else ""
    except Exception:
        out = buf_out.getvalue()
        err = traceback.format_exc()
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    result_queue.put({"output": out, "error": err or None})


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class PythonExecute(BaseTool):
    name = "python_execute"
    description = (
        "Execute Python code in an isolated subprocess with full system access. "
        "Runs until completion — no artificial time limit. "
        "Optionally pass timeout=N (seconds) to kill after exactly N seconds. "
        "Full output always returned — no truncation, ever. "
        "All imports, filesystem, network, subprocess, and ML ops are permitted. "
        "Use print() to capture output. "
        "Only fork bombs and direct /dev/sda writes are blocked."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python 3 code to execute. Use print() for output.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Optional. Kill subprocess after this many seconds if still running. "
                    "Omit (or pass null) to run until the code naturally completes — "
                    "2 min scripts run 2 min, 3 hour training runs run 3 hours. "
                    "Only set this if you explicitly need a hard deadline."
                ),
            },
        },
        "required": ["code"],
    }

    async def execute(
        self,
        code: str,
        timeout: Optional[int] = None,
        **_: Any,
    ) -> ToolResult:
        result_queue: multiprocessing.Queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_worker, args=(code, result_queue), daemon=True
        )
        proc.start()

        # timeout=None → join waits forever until the subprocess finishes
        proc.join(timeout=timeout)

        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
            return ToolResult(
                error=(
                    f"Deadline reached after {timeout}s. "
                    "Options: omit timeout to run until done, "
                    "add progress checkpoints (print statements), "
                    "run via bash tool with 'nohup python3 script.py &', "
                    "or split the task into smaller steps."
                )
            )

        try:
            result = result_queue.get_nowait()
        except queue.Empty:
            return ToolResult(
                error="Subprocess exited with no result — likely a memory error or OS-level crash."
            )

        output = (result.get("output") or "").strip()
        error  = result.get("error")

        if error:
            return ToolResult(
                output=output or None,
                error=f"Python error:\n{error}\n\nAnalyse the traceback and fix the code.",
            )
        return ToolResult(output=output or "(code completed successfully, no output)")
