# Dependency Refactoring

## 1. Dependency Audit

The following dependencies were removed from `pyproject.toml`:

| Package    | Reason                                      |
|------------|---------------------------------------------|
| `tiktoken` | Dead dependency — zero imports in codebase. |
| `loguru`   | Replaced with stdlib `logging` module.      |

## 2. Logger Rewrite — `app/logger.py`

The entire logging layer was rewritten to eliminate the `loguru` dependency while preserving full API compatibility across all 40 consumer files.

### Context Propagation

- **`ContextVar`-based context**: Four `contextvars.ContextVar` instances carry `trace_id`, `agent`, `step`, and `task_id` across `async/await` boundaries without manual threading.
- **`ContextFilter`**: A `logging.Filter` that injects the four context fields into every `LogRecord` before formatting.
- **`set_log_context` / `reset_log_context`**: Context managers using `ContextVar.set()`/`reset()` tokens for scoped overrides.
- **`new_trace_id()`**: Generates a short 12-char UUID.

### Formatting

- **`ColorfulFormatter`**: Custom `logging.Formatter` with auto TTY detection (`sys.stderr.isatty()`) and `NO_COLOR` environment variable support. Applies distinct ANSI colours per log level in stderr output and falls back to plain text when colours are disabled or output is piped.

### File Rotation

- **`CompressedRotatingFileHandler`**: Extends `logging.handlers.RotatingFileHandler`. On rollover it gzip-compresses the rotated file (`.log` → `.log.1.gz`). Logs are written to `logs/<timestamp>.log` with a 50 MB limit and 7 backup generations.

### API Compatibility

- A `TRACE` level (`DEBUG - 5`) is registered globally, and `logger.trace()` is attached as a convenience method on the root logger, matching the `loguru` API surface that consumers expected.

## 3. Provider Template — `providers/opencode.toml`

New provider template pointing to the opencode.ai OpenAI-compatible endpoint:

```toml
[llm]
provider = "openai"
base_url = "https://opencode.ai/zen/v1"
api_key = "public"
model = "deepseek-v4-flash-free"
max_tokens = 4096
temperature = 0.0
```
