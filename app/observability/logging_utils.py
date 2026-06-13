"""
Structured Logging Utilities
==============================

Provides structured JSON logging with automatic correlation ID injection,
per-module log level configuration, and integration with the manusclaw
security subsystem for sensitive data redaction.

Features:
    - JSON-formatted log output for machine consumption
    - Correlation ID injection from :mod:`app.observability.correlation`
    - Per-module log level configuration
    - Sensitive data redaction via :mod:`app.security.base`
    - Drop-in replacement for standard ``logging.Logger``
    - Compatible with the existing manusclaw logger

Usage::

    from app.observability.logging_utils import get_structured_logger

    log = get_structured_logger("my_module")
    log.info("Request processed",
             extra={"user_id": "abc123", "action": "query"})

Configuration::

    # Enable JSON logging via config
    [logging]
    json_format = true
    level = INFO

    # Or via environment variable
    MANUSCLAW_LOG_FORMAT=json
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

# ---------------------------------------------------------------------------
# Sensitive data redaction integration
# ---------------------------------------------------------------------------

# Whether the sanitise_message function is available for use
_HAS_SANITISE = False

try:
    from app.security.base import sanitise_message
    _HAS_SANITISE = True
except ImportError:
    def sanitise_message(msg: str) -> str:  # type: ignore[misc]
        return msg

# Whether redaction is enabled by default (controlled by config or env var)
_REDACTION_ENABLED = False

try:
    from app.config import Config
    try:
        _cfg = Config.get()
        _REDACTION_ENABLED = _cfg.redact_secrets
    except Exception:
        pass
except ImportError:
    pass

# Environment variable override
if os.getenv("MANUSCLAW_REDACT", "").lower() in ("1", "true", "yes"):
    _REDACTION_ENABLED = True


# ---------------------------------------------------------------------------
# Correlation ID integration
# ---------------------------------------------------------------------------

try:
    from app.observability.correlation import (
        get_correlation_id,
        get_request_id,
        get_error_id,
    )
except ImportError:
    def get_correlation_id() -> Optional[str]:  # type: ignore[misc]
        return None

    def get_request_id() -> Optional[str]:  # type: ignore[misc]
        return None

    def get_error_id() -> Optional[str]:  # type: ignore[misc]
        return None


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------

class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Each record includes:
        - ``timestamp``: ISO 8601 UTC timestamp
        - ``level``: Log level name
        - ``logger``: Logger name
        - ``message``: The log message
        - ``correlation_id``: From the correlation module
        - ``request_id``: From the correlation module
        - ``error_id``: From the correlation module (if set)
        - ``module``, ``function``, ``line``: Source location
        - ``extra``: Any extra fields passed via ``extra={}``
        - ``exception``: Exception info (if present)
    """

    def __init__(self, redact: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._sanitize(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Correlation IDs
        cid = get_correlation_id()
        if cid:
            log_entry["correlation_id"] = cid

        rid = get_request_id()
        if rid:
            log_entry["request_id"] = rid

        eid = get_error_id()
        if eid:
            log_entry["error_id"] = eid

        # Trace ID from manusclaw's existing logger context
        trace_id = getattr(record, "trace_id", None)
        if trace_id and trace_id != "————":
            log_entry["trace_id"] = trace_id

        agent = getattr(record, "agent", None)
        if agent and agent != "system":
            log_entry["agent"] = agent

        step = getattr(record, "step", None)
        if step and step != 0:
            log_entry["step"] = step

        task_id = getattr(record, "task_id", None)
        if task_id:
            log_entry["task_id"] = task_id

        # Extra fields from the caller
        extra_fields = self._extract_extra(record)
        if extra_fields:
            log_entry["extra"] = self._sanitize_dict(extra_fields)

        # Exception info
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Unknown"
            exc_message = str(record.exc_info[1]) if record.exc_info[1] else ""
            log_entry["exception"] = {
                "type": exc_type,
                "message": self._sanitize(exc_message),
                "traceback": self.formatException(record.exc_info),
            }

        try:
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fallback: serialize what we can
            return json.dumps(
                {k: str(v) for k, v in log_entry.items()},
                ensure_ascii=False,
            )

    def _sanitize(self, text: str) -> str:
        """Redact sensitive data if redaction is enabled."""
        if self.redact and _HAS_SANITISE:
            return sanitise_message(text)
        return text

    def _sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact sensitive values in a dict."""
        if not (self.redact and _HAS_SANITISE):
            return data
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self._sanitize(value)
            elif isinstance(value, dict):
                result[key] = self._sanitize_dict(value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _extract_extra(record: logging.LogRecord) -> Dict[str, Any]:
        """Extract extra fields that were added to the log record.

        Filters out standard LogRecord attributes to only include
        user-provided extra fields.
        """
        standard_attrs = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "pathname", "filename", "module", "levelno", "levelname",
            "thread", "threadName", "process", "processName", "message",
            "msecs", "task", "taskName",
            # manusclaw context fields
            "trace_id", "agent", "step", "task_id",
        }
        return {
            key: value
            for key, value in record.__dict__.items()
            if key not in standard_attrs and not key.startswith("_")
        }


# ---------------------------------------------------------------------------
# Human-readable structured formatter (for development)
# ---------------------------------------------------------------------------

class StructuredTextFormatter(logging.Formatter):
    """Human-readable formatter with correlation ID prefix.

    Format: ``[CORRELATION_ID] LEVEL logger: message``
    """

    def __init__(self, redact: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        cid = get_correlation_id() or "————"
        rid = get_request_id() or ""

        parts: list[str] = []

        # Timestamp
        ts = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).strftime("%H:%M:%S.%f")[:-3]
        parts.append(ts)

        # Correlation ID
        parts.append(f"[{cid}]")

        # Request ID (if present)
        if rid:
            parts.append(f"(req:{rid})")

        # Level
        parts.append(f"{record.levelname:<8}")

        # Logger name (shortened)
        logger_name = record.name
        if logger_name.startswith("manusclaw."):
            logger_name = logger_name[len("manusclaw."):]
        parts.append(f"{logger_name}:")

        # Message
        message = record.getMessage()
        if self.redact and _HAS_SANITISE:
            message = sanitise_message(message)
        parts.append(message)

        # Agent context
        agent = getattr(record, "agent", None)
        step = getattr(record, "step", None)
        if agent and agent != "system":
            parts.append(f"[{agent}@{step or 0}]")

        result = " ".join(parts)

        # Exception
        if record.exc_info and record.exc_info[0] is not None:
            result += "\n" + self.formatException(record.exc_info)

        return result


# ---------------------------------------------------------------------------
# Per-module log level configuration
# ---------------------------------------------------------------------------

_module_levels: Dict[str, int] = {}
_levels_lock = _levels_lock = __import__("threading").Lock()


def set_module_level(module_name: str, level: Union[str, int]) -> None:
    """Set the log level for a specific module.

    Args:
        module_name: The logger name / module path (e.g. ``"app.llm"``).
        level: Log level as string (``"DEBUG"``, ``"INFO"``, etc.) or int.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    with _levels_lock:
        _module_levels[module_name] = level
    logger_obj = logging.getLogger(module_name)
    logger_obj.setLevel(level)


def get_module_levels() -> Dict[str, int]:
    """Return a copy of the current per-module log level overrides."""
    with _levels_lock:
        return dict(_module_levels)


def apply_module_levels(config: Optional[Dict[str, str]] = None) -> None:
    """Apply per-module log levels from a config dict or env var.

    Environment variable: ``MANUSCLAW_LOG_LEVELS``
    Format: ``"module1=DEBUG,module2=WARNING"``

    Args:
        config: Dict mapping module names to level strings.
                If ``None``, reads from the environment variable.
    """
    if config is None:
        env_str = os.getenv("MANUSCLAW_LOG_LEVELS", "")
        if not env_str:
            return
        config = {}
        for pair in env_str.split(","):
            pair = pair.strip()
            if "=" in pair:
                mod, lvl = pair.split("=", 1)
                config[mod.strip()] = lvl.strip()

    for module_name, level_str in config.items():
        set_module_level(module_name, level_str)


# ---------------------------------------------------------------------------
# Structured logger factory
# ---------------------------------------------------------------------------

_json_format_enabled: bool = False


def _is_json_format_enabled() -> bool:
    """Check if JSON log format is enabled via config or env."""
    if os.getenv("MANUSCLAW_LOG_FORMAT", "").lower() == "json":
        return True
    try:
        from app.config import Config
        return Config.get().logging.json_format
    except Exception:
        pass
    return False


def get_structured_logger(
    name: str,
    level: Optional[Union[str, int]] = None,
    json_format: Optional[bool] = None,
    redact: Optional[bool] = None,
) -> logging.Logger:
    """Get a logger with structured formatting.

    If JSON format is enabled (via config, env var, or the ``json_format``
    parameter), the logger outputs single-line JSON.  Otherwise, it uses
    a human-readable structured text format.

    Args:
        name: Logger name (typically ``__name__``).
        level: Optional log level override.
        json_format: Force JSON (``True``) or text (``False``) format.
            If ``None``, reads from config / env.
        redact: Enable sensitive data redaction. If ``None``, reads from
            config (default ``True``).

    Returns:
        A configured ``logging.Logger`` instance.
    """
    logger_obj = logging.getLogger(name)

    # Set level
    if level is not None:
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)
        logger_obj.setLevel(level)
    elif name in _module_levels:
        logger_obj.setLevel(_module_levels[name])

    # Determine format
    use_json = json_format if json_format is not None else _is_json_format_enabled()
    # Default redaction: if explicitly set, use that; otherwise enable if
    # sanitise_message is available (safe default for enterprise use)
    if redact is not None:
        should_redact = redact
    elif _REDACTION_ENABLED:
        should_redact = True
    else:
        should_redact = _HAS_SANITISE

    # Remove any existing handlers from this factory to avoid duplicates
    # (but only if we've already configured them)
    if not getattr(logger_obj, "_structured_configured", False):
        handler = logging.StreamHandler(sys.stderr)
        if use_json:
            handler.setFormatter(StructuredJsonFormatter(redact=should_redact))
        else:
            handler.setFormatter(StructuredTextFormatter(redact=should_redact))
        logger_obj.addHandler(handler)
        # Prevent propagation to the manusclaw root logger whose handlers
        # require ContextFilter attributes (agent, trace_id, etc.) that
        # child loggers may not have set on their records.
        logger_obj.propagate = False
        logger_obj._structured_configured = True  # type: ignore[attr-defined]

    return logger_obj


# ---------------------------------------------------------------------------
# Convenience: configure the root manusclaw logger for structured output
# ---------------------------------------------------------------------------

def configure_structured_logging(
    json_format: Optional[bool] = None,
    redact: Optional[bool] = None,
    level: Optional[Union[str, int]] = None,
) -> None:
    """Configure the manusclaw root logger for structured output.

    This replaces the default handlers on the ``"manusclaw"`` logger
    with structured formatters.  Call this once at application startup.

    Args:
        json_format: Force JSON output. If ``None``, reads from config.
        redact: Enable sensitive data redaction. If ``None``, reads from config.
        level: Root log level. If ``None``, reads from config.
    """
    use_json = json_format if json_format is not None else _is_json_format_enabled()
    if redact is not None:
        should_redact = redact
    elif _REDACTION_ENABLED:
        should_redact = True
    else:
        should_redact = _HAS_SANITISE

    root_logger = logging.getLogger("manusclaw")

    # Set level
    if level is not None:
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)
        root_logger.setLevel(level)
    else:
        try:
            from app.config import Config
            cfg_level = Config.get().logging.level.upper()
            root_logger.setLevel(getattr(logging, cfg_level, logging.DEBUG))
        except Exception:
            pass

    # Replace existing handlers
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    if use_json:
        handler.setFormatter(StructuredJsonFormatter(redact=should_redact))
    else:
        handler.setFormatter(StructuredTextFormatter(redact=should_redact))
    root_logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Integration with manusclaw's existing context filter
# ---------------------------------------------------------------------------

try:
    from app.logger import ContextFilter
    _HAS_CONTEXT_FILTER = True
except ImportError:
    _HAS_CONTEXT_FILTER = False


def bind_logger_context(
    correlation_id: Optional[str] = None,
    request_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    step: Optional[int] = None,
    task_id: Optional[str] = None,
) -> None:
    """Bind context variables to the existing manusclaw logger.

    This integrates structured logging with the existing
    ``app.logger.set_log_context`` system.

    Args:
        correlation_id: Correlation ID for distributed tracing.
        request_id: Request-scoped ID.
        agent_name: Agent name for context.
        step: Step number.
        task_id: Task UUID.
    """
    try:
        from app.logger import set_log_context
        from app.observability.correlation import (
            set_correlation_id as _set_cid,
            set_request_id as _set_rid,
        )

        if correlation_id:
            _set_cid(correlation_id)
        if request_id:
            _set_rid(request_id)

        set_log_context(
            trace_id=correlation_id,
            agent_name=agent_name,
            step_id=step,
            task_id=task_id,
        )
    except ImportError:
        pass
