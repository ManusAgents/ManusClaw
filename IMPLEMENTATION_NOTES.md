# ManusClaw v5.0 — Implementation Notes

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
