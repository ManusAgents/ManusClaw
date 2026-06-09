# Logger API Reference

## Overview

`app.logger` provides a context-aware logging system built on top of Python's standard `logging` module. Every log record is automatically tagged with four context fields:

| Field | Type | Description |
|-------|------|-------------|
| `trace_id` | `str` | Unique ID for the agent run (12-char UUID) |
| `agent` | `str` | Agent name (e.g. `manus`, `orchestrator`) |
| `step` | `int` | Current PAORR step number |
| `task_id` | `str` | Short task UUID |

Context propagates correctly across `async/await` boundaries via `contextvars`.

---

## `logger` — Singleton Logger Instance

```python
from app.logger import logger
```

`logger` is a standard `logging.Logger` instance named `"manusclaw"`. All standard methods are available:

| Method | Signature | Level |
|--------|-----------|-------|
| `logger.trace()` | `trace(msg, *args, **kwargs)` | `TRACE` (5) |
| `logger.debug()` | `debug(msg, *args, **kwargs)` | `DEBUG` (10) |
| `logger.info()` | `info(msg, *args, **kwargs)` | `INFO` (20) |
| `logger.warning()` | `warning(msg, *args, **kwargs)` | `WARNING` (30) |
| `logger.error()` | `error(msg, *args, **kwargs)` | `ERROR` (40) |
| `logger.critical()` | `critical(msg, *args, **kwargs)` | `CRITICAL` (50) |
| `logger.exception()` | `exception(msg, *args, **kwargs)` | `ERROR` (40) + stack trace |
| `logger.log()` | `log(level, msg, *args, **kwargs)` | Custom level |
| `logger.setLevel()` | `setLevel(level)` | — |

```python
logger.info("Hello %s", "world")
logger.warning("Disk usage at %d%%", pct)
logger.error("Failed to connect: %s", err)
logger.exception("Unhandled exception")   # includes traceback
logger.trace("Fine-grained debug data")   # custom level
```

> **Note:** `trace()` is not a standard `logging` method; it is attached dynamically at `logger.py:133`. It calls `logger.log(logging.TRACE, ...)` where `TRACE = DEBUG - 5`.

---

## Context Functions

### `new_trace_id()`

```python
from app.logger import new_trace_id

tid = new_trace_id()  # e.g. "a1b2c3d4e5f6"
```

Returns the first 12 characters of `uuid.uuid4()` as a short trace identifier.

### `set_log_context()`

```python
from app.logger import set_log_context

tokens = set_log_context(
    trace_id="abc123def456",
    agent_name="manus",
    step_id=3,
    task_id="task_xyz",
)
```

Sets one or more context variables for the current execution context (task/thread). Returns a dict of `ContextVar.Token` objects keyed by field name. These tokens **must** be saved and later passed to `reset_log_context()` to restore previous values.

All parameters are optional. Only non-`None` values are updated.

### `reset_log_context()`

```python
from app.logger import reset_log_context

reset_log_context(tokens)
```

Restores context variables to their values before the corresponding `set_log_context()` call. Accepts the tokens dict returned by `set_log_context()`.

### `get_current_trace_id()`

```python
from app.logger import get_current_trace_id

tid = get_current_trace_id()  # str
```

Returns the current trace context value. Default: `"————"`.

### `get_current_agent()`

```python
from app.logger import get_current_agent

agent = get_current_agent()  # str
```

Returns the current agent context value. Default: `"system"`.

---

## Typical Usage Pattern

```python
from app.logger import logger, new_trace_id, set_log_context, reset_log_context

def run_agent():
    trace_id = new_trace_id()
    ctx = set_log_context(trace_id=trace_id, agent_name="manus")

    logger.info("Agent started")   # tagged with trace_id + agent
    do_work()

    reset_log_context(ctx)
```

For async code, call `set_log_context()` / `reset_log_context()` at the top of each async entry point:

```python
async def handle_request():
    ctx = set_log_context(trace_id=new_trace_id(), agent_name="orchestrator")
    try:
        ...
    finally:
        reset_log_context(ctx)
```

---

## How Context Propagation Works

The system uses `contextvars.ContextVar` (introduced in Python 3.7) for thread-safe and async-safe context storage.

1. **Storage**: Four module-level `ContextVar` instances hold `trace_id`, `agent`, `step`, and `task_id`.
2. **Filter**: `ContextFilter` (a `logging.Filter` subclass) runs on every log record and attaches the current `ContextVar` values as `LogRecord` attributes (`record.trace_id`, `record.agent`, `record.step`, `record.task_id`).
3. **Formatter**: `ColorfulFormatter` reads these attributes to produce the log line.
4. **Async safety**: Because `ContextVar` respects `asyncio` task boundaries, context set in one coroutine does not leak into another — even with `asyncio.gather()`.

```
[ContextVar store] → ContextFilter.filter(record) → ColorfulFormatter.format(record) → output
```

---

## Configuration

### Log Level

The log level is read at import time from `Config.get().logging.level` (default: `"DEBUG"`).

In `config.toml`:

```toml
[logging]
level = "INFO"       # TRACE | DEBUG | INFO | WARNING | ERROR | CRITICAL
```

The level string is uppercased and looked up via `getattr(logging, ...)`. Falls back to `DEBUG` on error.

### Environment Variable Override

No direct env var for log level exists yet; set `MANUSCLAW_PROFILE` to load a profile with a different `config.yaml` / `.env` that overrides the logging level.

### `NO_COLOR`

Set the `NO_COLOR` environment variable to disable ANSI color output regardless of terminal capability. Color is also disabled when stderr is not a TTY.

```bash
NO_COLOR=1 python main.py
```

---

## Output Destinations

### Console (stderr)

- **Handler**: `logging.StreamHandler(sys.stderr)`
- **Formatter**: `ColorfulFormatter`
- **Format** (color):
  ```
  14:23:05 | INFO     | manus@3 [abc123def456] — Agent started
  ```
- **Format** (no color):
  ```
  14:23:05 | INFO     | manus@3 [abc123def456] — Agent started
  ```
- TTY-aware: colors enabled only when `sys.stderr.isatty()` and `NO_COLOR` is not set.
- Level colors: `TRACE` grey, `DEBUG` cyan, `INFO` green, `WARNING` yellow, `ERROR` red, `CRITICAL` white-on-red.

### File (rotating, compressed)

- **Handler**: `CompressedRotatingFileHandler` (extends `RotatingFileHandler`)
- **Directory**: `logs/` (created automatically)
- **Filename**: `{YYYYMMDD_HHMMSS}.log`
- **Max size**: 50 MB per file
- **Backup count**: 7
- **Compression**: Rotated files are gzip-compressed (`.1.gz`, `.2.gz`, …)
- **Encoding**: UTF-8
- **Format**:
  ```
  2025-06-07 14:23:05,123 | INFO     | agent=manus step=3 trace=abc123def456 task=task_xyz | app.module:func:42 — message
  ```

---

## Migration Guide: Replacing `loguru`

This logger is **not** a drop-in replacement for loguru, but the common use cases are similar.

| loguru | ManusClaw Logger |
|--------|------------------|
| `logger.info("msg")` | `logger.info("msg")` — identical |
| `logger.info("val {}", x)` | `logger.info("val %s", x)` — printf-style |
| `logger.exception("msg")` | `logger.exception("msg")` — identical |
| `logger.add(sink, ...)` | Not supported; handlers are pre-configured |
| `logger.remove()` | Not supported |
| `logger.bind(trace_id=x)` | Use `set_log_context(trace_id=x)` |
| `logger.opt(depth=1)` | Not supported. Use `logging.Logger` directly |
| `logger.trace(...)` | `logger.trace(...)` — **identical** (custom level 5) |

Key differences:
- Use `%s`-style formatting, not `{}`-style.
- Context is set via `set_log_context()` / `reset_log_context()` per task, not per-call via `bind()`.
- Handlers are fixed at import time; there is no runtime `logger.add()` / `logger.remove()`.
- `trace()` works the same — it's a custom level at `DEBUG - 5`.

### Minimal migration example

```python
# Before (loguru)
from loguru import logger
logger.add("app.log", rotation="50 MB")
logger.info("Hello {}", name)

# After (ManusClaw)
from app.logger import logger
logger.info("Hello %s", name)
# File output is automatic — no setup needed
```
