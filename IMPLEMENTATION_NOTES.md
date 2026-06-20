# ManusClaw v5.1.1 — Implementation Notes

> **v5.1.1** is a maintenance release that closes 32 bugs discovered in
> v5.1.0. See [CHANGELOG.md](CHANGELOG.md) for the full audit trail and
> the `## 🔧 What's Fixed in v5.1.1` section in [README.md](README.md)
> for a tabular breakdown.

## Recent Bug Fixes (v5.1.1)

The v5.1.1 patch release closed 32 bugs across the codebase. The full
audit trail is in [CHANGELOG.md](CHANGELOG.md); the key architectural
lessons are summarised here so future contributors don't reintroduce
the same patterns.

### Recurring Pattern: Module-Level Env-Var-Backed Path Constants

**Affected modules:** `app/cron.py`, `app/skills/skill_engine.py`,
`app/tool/memory_tool.py`, `app/task_queue.py`.

**The bug:** A module-level constant was assigned from an environment
variable at import time, e.g.:

```python
# ANTI-PATTERN — do not do this
_JOBS_FILE = Path(os.getenv("MANUSCLAW_CRON_FILE", _DEFAULT_CRON_FILE))
```

This silently ignored any runtime changes to the env var. Tests that
used `monkeypatch.setenv` to point at a tmp directory passed only
because they also manually patched the module-level constant. In
production, profile switching or CLI overrides had no effect.

**The fix:** Replace the module-level constant with a lazy resolver
function called at the use-site:

```python
# CORRECT — env var read each call
def _get_jobs_file() -> Path:
    return Path(os.getenv("MANUSCLAW_CRON_FILE", _DEFAULT_CRON_FILE))
```

Keep the old module-level name as a backward-compat alias for external
code that imports it directly, but the implementation always calls the
resolver.

### Recurring Pattern: Async Method Called From Sync Context Without Await

**Affected modules:** `app/agent/router.py`,
`app/observability/health.py`.

**The bug:** A method was declared `async def` but called from a sync
caller without `await`, so the call returned a coroutine object that
was silently discarded. In `AgentRegistry`, this meant eviction never
ran; in `LLMHealthChecker`, the health check always reported success
regardless of the LLM's actual state.

**The fix:** Either make the caller async (preferred) or convert the
callee to sync and bridge the async boundary explicitly (e.g. via
`asyncio.run` in a worker thread — used in `health.py` because the
caller is a sync FastAPI-agnostic checker).

### FastAPI Route Ordering

**Affected module:** `app/server/webhook_router.py`.

**The bug:** `@router.post("/{hook_id}")` was declared before
`@router.post("/create")`, so FastAPI matched the parameterised path
first and `POST /webhooks/create` was treated as
`trigger_webhook(hook_id="create")` → 404.

**The fix:** Declare literal sub-paths (`/create`, `/sign/{hook_id}`)
BEFORE the parameterised catch-all (`/{hook_id}`). Documented in the
router module docstring and locked in with two regression tests using
the real FastAPI `TestClient`.

**Rule of thumb:** In FastAPI, route order matters. Always put literal
paths before parameterised paths sharing the same HTTP verb.

### Resource Cleanup in Multi-Agent Roles

**Affected modules:** `app/agent/roles/engineer.py`,
`app/agent/roles/qa.py`.

**The bug:** `Manus()` instances were created inside `_think_act_publish`
but `cleanup()` was never called, leaking the persistent Bash
subprocess (and any other tool resources) for the lifetime of the
process. Each retry created a fresh `Manus()` and leaked another.

**The fix:** Wrap each `Manus()` use in `try/finally` with a
`_cleanup_agent` helper that calls `agent.cleanup()` and awaits it if
the return value is awaitable.

**Rule of thumb:** Anything that creates a `Manus()` (or any agent
with persistent resources) MUST clean it up in a `finally` block. The
`PlanningFlow` already had this pattern (`_cleanup_agents`); roles
just hadn't adopted it.

### Test Pollution via Module-Level Monkeypatching

**Affected file:** `tests/test_voice.py`.

**The bug:** A test did `tts_mod._create_provider = lambda name: ...`
directly on the module — a permanent mutation that leaked into every
subsequent test in the same module, causing the next test to receive
`NullTTS` instead of `OpenAITTS`.

**The fix:** Use the `monkeypatch` pytest fixture, which automatically
restores the original attribute at test teardown.

**Rule of thumb:** Never mutate module-level state directly in tests.
Always go through `monkeypatch.setattr` / `monkeypatch.setenv` so
teardown is automatic.

## Design Decisions

### Stub-First Pattern
Every feature follows the **stub-first** pattern: when optional dependencies are missing or environment variables are not set, the feature logs a warning and operates in a degraded (but safe) mode. This ensures:
- `pip install manusclaw` works with zero optional dependencies
- Each feature can be tested independently
- Graceful degradation in production environments

### Adapter Pattern for Messaging
All 12+ messaging adapters inherit from `BaseMessagingAdapter` (ABC) with four abstract methods: `connect`, `start`, `send`, `disconnect`. Each adapter checks `is_configured()` before attempting real connections.

### A2UI Protocol
The Agent-to-UI protocol uses JSON dataclasses inspired by JSON-RPC. Components are typed (`text`, `chart`, `image`, `button`, `table`, `container`, `markdown`) with builder functions for ergonomic construction.

### Restricted Shell
The SSH shell uses a whitelist approach: only explicitly allowed commands pass validation. All shell metacharacters (`|`, `&`, `;`, `` ` ``, `$`, `()`, `{}`, `[]`, `<>`, `!`) are rejected at the parse level, before any command dispatch.

### LRU Cache Pattern
Three systems use the same LRU cache pattern:
- `AgentRegistry` (agent router) — cache_size=64, idle_ttl=300s
- `MessagingGateway` — cache_size=128, idle_ttl=300s
- `DeviceManager` — cache_size=128, heartbeat_timeout=120s

### Model Failover Design
`ProfileRotator` separates the profile definition from runtime state. The `ModelProfile` defines ordered entries; `ProfileRotator` manages cooldown timers, success tracking, and per-session overrides.

## Patterns Followed

1. **Async-first**: All I/O is async using `asyncio`. Blocking operations (STT, SSH paramiko) run via `asyncio.to_thread()`.

2. **Environment-driven config**: All feature flags and credentials come from environment variables, consistent with the existing `MANUSCLAW_*` and feature-specific prefixes.

3. **Entry points**: CLI tools use `argparse` with subcommands. Each entry point (`manusclaw-sessions`, `manusclaw-channels`, `manusclaw-webhook`) is a standalone script.

4. **SQLite persistence**: Webhooks and cron jobs persist to SQLite/YAML, ensuring survival across restarts without external databases.

5. **Protocol versioning**: The A2UI and node protocols include message types as string enums, allowing future extension without breaking existing clients.

## Known Limitations

1. **IRC TLS**: The IRC adapter does not natively handle TLS (IRC port 6697). Users who need TLS should use a TLS-terminating proxy.

2. **OpenShell sandbox**: Linux-only. Requires `unshare` binary and kernel support for user namespaces.

3. **SSH Sandbox**: Falls back to Docker when not configured (SSH_SANDBOX_HOST/USER not set).

4. **Wake word detection**: Porcupine requires a paid PICOVOICE_API_KEY. The speech_recognition fallback requires a network connection for Google STT.

5. **Canvas WebSocket**: The CanvasServer depends on FastAPI's WebSocket support. It does not scale horizontally (in-memory state per server instance).

6. **Companion apps**: The desktop companion apps are scaffolded but not fully functional. They provide starting points for platform-specific integration.

7. **Webhook agent trigger**: The webhook system creates a new `Manus()` agent per trigger. There is no session affinity or deduplication.

8. **Model failover**: The `ProfileRotator` does not implement circuit breaker patterns with half-open states. It uses simple cooldown timers.

## Future Work

- **WebSocket multiplexing**: Support horizontal scaling of CanvasServer with Redis-backed session state.
- **MQTT bridge**: Add an MQTT adapter for IoT device communication.
- **OAuth2 flow wizard**: Interactive OAuth2 setup for Gmail and other OAuth-protected services.
- **Webhook deduplication**: Idempotency keys for webhook triggers to prevent duplicate agent runs.
- **Voice wake word training**: Support custom wake word models beyond the default porcupine keywords.
- **Container orchestration**: Add Kubernetes-based sandbox backend for cloud deployments.
- **Real-time collaboration**: Multi-user canvas editing with conflict resolution.
- **Streaming TTS**: Stream audio chunks instead of waiting for complete synthesis.
- **Desktop companion apps**: Full implementation with native installers.
- **Metrics dashboard**: Prometheus/OpenMetrics endpoint for operational monitoring.
