from __future__ import annotations

"""
ManusClaw Context-Aware Logger
===============================
Every log record is automatically tagged with four context fields:

  trace_id  — unique ID for the agent run (set once per run)
  agent     — agent name (manus, orchestrator, product_manager, …)
  step      — current PAORR step number within the run
  task_id   — short task UUID

Context is propagated via Python's `contextvars` module, which means it
works correctly across `async/await` boundaries without manual threading.
"""

import gzip
import logging
import os
import shutil
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from app.config import Config


_ctx_trace: ContextVar[str] = ContextVar("mc_trace", default="————")
_ctx_agent: ContextVar[str] = ContextVar("mc_agent", default="system")
_ctx_step: ContextVar[int] = ContextVar("mc_step", default=0)
_ctx_task: ContextVar[str] = ContextVar("mc_task", default="")

logging.TRACE = logging.DEBUG - 5
logging.addLevelName(logging.TRACE, "TRACE")

_USE_COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ

_RESET = "\033[0m" if _USE_COLOR else ""
_COLORS = {
    "TRACE": "\033[38;5;245m" if _USE_COLOR else "",
    "DEBUG": "\033[36m" if _USE_COLOR else "",
    "INFO": "\033[32m" if _USE_COLOR else "",
    "WARNING": "\033[33m" if _USE_COLOR else "",
    "ERROR": "\033[31m" if _USE_COLOR else "",
    "CRITICAL": "\033[41;37m" if _USE_COLOR else "",
}
_LEVEL_COLORS = {
    logging.TRACE: "TRACE",
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _ctx_trace.get()
        record.agent = _ctx_agent.get()
        record.step = _ctx_step.get()
        record.task_id = _ctx_task.get()
        return True


class ColorfulFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level_name = _LEVEL_COLORS.get(record.levelno, "INFO")
        color = _COLORS.get(level_name, "")
        time_str = self.formatTime(record, "%H:%M:%S")
        level_padded = f"{level_name:<8}"
        if _USE_COLOR:
            return (
                f"{color}{time_str}{_RESET} | {color}{level_padded}{_RESET} | "
                f"\033[36m{record.agent}\033[0m@\033[36m{record.step}\033[0m "
                f"[\033[2m{record.trace_id}\033[0m] — "
                f"{color}{record.getMessage()}{_RESET}"
            )
        return (
            f"{time_str} | {level_padded} | "
            f"{record.agent}@{record.step} [{record.trace_id}] — "
            f"{record.getMessage()}"
        )


class CompressedRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        for i in range(self.backupCount - 1, 0, -1):
            sfn = f"{self.baseFilename}.{i}.gz"
            dfn = f"{self.baseFilename}.{i + 1}.gz"
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.rename(sfn, dfn)

        dfn = f"{self.baseFilename}.1"
        if os.path.exists(self.baseFilename):
            if os.path.exists(dfn):
                os.remove(dfn)
            os.rename(self.baseFilename, dfn)

        if os.path.exists(dfn):
            gzfn = f"{dfn}.gz"
            with open(dfn, "rb") as f_in, gzip.open(gzfn, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(dfn)

        if not self.delay:
            self.stream = self._open()


_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

_log_level: str = "DEBUG"
try:
    _log_level = Config.get().logging.level.upper().strip() or "DEBUG"
except Exception:
    pass

_logger = logging.getLogger("manusclaw")
_logger.setLevel(getattr(logging, _log_level, logging.DEBUG))

_logger.trace = lambda msg, *args, **kwargs: _logger.log(logging.TRACE, msg, *args, **kwargs)

_logger.addFilter(ContextFilter())

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(ColorfulFormatter())
_logger.addHandler(_console_handler)

_file_handler = CompressedRotatingFileHandler(
    str(_LOG_FILE),
    maxBytes=50 * 1024 * 1024,
    backupCount=7,
    encoding="utf-8",
)
_file_formatter = logging.Formatter(
    "{asctime} | {levelname:<8} | "
    "agent={agent} step={step} "
    "trace={trace_id} task={task_id} | "
    "{name}:{funcName}:{lineno} — {message}",
    style="{",
)
_file_handler.setFormatter(_file_formatter)
_logger.addHandler(_file_handler)

logger = _logger


def new_trace_id() -> str:
    return str(uuid.uuid4())[:12]


def set_log_context(
    trace_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    step_id: Optional[int] = None,
    task_id: Optional[str] = None,
) -> dict:
    tokens: dict = {}
    if trace_id is not None:
        tokens["trace_id"] = _ctx_trace.set(trace_id)
    if agent_name is not None:
        tokens["agent_name"] = _ctx_agent.set(agent_name)
    if step_id is not None:
        tokens["step_id"] = _ctx_step.set(step_id)
    if task_id is not None:
        tokens["task_id"] = _ctx_task.set(task_id)
    return tokens


def reset_log_context(tokens: dict) -> None:
    for key, token in tokens.items():
        if key == "trace_id":
            _ctx_trace.reset(token)
        elif key == "agent_name":
            _ctx_agent.reset(token)
        elif key == "step_id":
            _ctx_step.reset(token)
        elif key == "task_id":
            _ctx_task.reset(token)


def get_current_trace_id() -> str:
    return _ctx_trace.get()


def get_current_agent() -> str:
    return _ctx_agent.get()
