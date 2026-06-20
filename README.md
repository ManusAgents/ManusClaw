<div align="center">

<img src="https://img.shields.io/badge/Version-5.1.1-ff69b4?style=for-the-badge&logo=github&logoColor=white" alt="Version">
<img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
<img src="https://img.shields.io/badge/License-MIT-FFD700?style=for-the-badge&logo=opensourceinitiative&logoColor=black" alt="License">
<img src="https://img.shields.io/badge/Status-Bug--Fix%20Patch-00C853?style=for-the-badge&logo=bugsnag&logoColor=white" alt="Status">

<br><br>

# 🐾 M A N U S C L A W

### **v5.1.1 — Enterprise-Grade Autonomous AI Agent Framework (Bug-Fix Patch)**

**A production-ready, self-reasoning AI agent framework with PAORR loop, DAG-based multi-agent orchestration, defense-in-depth security, 100+ LLM providers (cloud + offline/GGUF/HuggingFace), 13+ messaging channels, voice interaction, live canvas, SSH server, cron scheduler, and enterprise observability.**

> **v5.1.1** is a maintenance release that resolves 32 bugs discovered in v5.1.0 across the agent core, security layer, FastAPI server, cron scheduler, secrets redaction, and multi-agent role pipeline. See [CHANGELOG.md](CHANGELOG.md) for the full audit trail.

<p>
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Docker-informational?style=flat-square" alt="Platforms">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/LLM-100%2B%20Providers-FF6F00?style=flat-square&logo=brain&logoColor=white" alt="LLM Providers">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Offline-GGUF%20%7C%20HF%20%7C%20Ollama-9C27B0?style=flat-square" alt="Offline">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Channels-13%2B-00B4D8?style=flat-square&logo=message&logoColor=white" alt="Channels">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Tools-17%2B-00C853?style=flat-square" alt="Tools">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Tests-212%20passed-brightgreen?style=flat-square" alt="Tests">
</p>

</div>

---

## Table of Contents

- [What's New in v5.1](#-whats-new-in-v51)
- [What's Fixed in v5.1.1](#-whats-fixed-in-v511)
- [Overview](#-overview)
- [Architecture](#-architecture)
- [Features](#-features)
  - [Agent System](#-agent-system--paorr-loop--multi-agent--roles)
  - [Event System](#-event-system--discriminated-unions--eventlog)
  - [Security Defense-in-Depth](#-security-defense-in-depth)
  - [Hooks System](#-hooks-system)
  - [Context Management](#-context-management--view--condenser)
  - [Conversation System](#-conversation-system)
  - [Parallel Tool Execution](#-parallel-tool-execution)
  - [LLM Integration](#-llm-integration--100-providers--offline--streaming)
  - [Secrets Management](#-secrets-management--fernet-encryption)
  - [File Storage](#-file-storage-backends)
  - [Git Provider Integrations](#-git-provider-integrations)
  - [Issue & PR Resolution](#-issue--pr-resolution)
  - [Project Management](#-project-management--jira--linear--slack)
  - [Observability](#-observability--opentelemetry--prometheus)
  - [Messaging Channels](#-13-messaging-channels)
  - [Voice System](#-voice-system--wake-word--stt--tts)
  - [Canvas UI](#-canvas-ui--a2ui-protocol)
  - [Tools](#-17-tools--intelligent-selector)
  - [SSH Server](#-ssh-server)
  - [Cron Scheduler](#-cron-scheduler)
  - [Skills Engine](#-skills-engine)
  - [MCP Protocol](#-mcp-protocol--client--server)
  - [Desktop Apps](#-desktop-apps)
  - [Session & Memory](#-session--memory-system)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [Docker Deployment](#-docker-deployment)
- [Entry Points](#-entry-points)
- [Contributing](#-contributing)
- [Changelog](CHANGELOG.md)
- [License](#-license)

---

## 🆕 What's New in v5.1

ManusClaw v5.1 introduces enterprise-grade capabilities that transform it from a powerful agent framework into a production-ready AI operations platform.

| Category | Highlights |
|---|---|
| **Event System** | 17 discriminated union event types, `LLMConvertibleEvent`, file-backed `EventLog` with O(1) length, crash-proof atomic writes |
| **Security** | Defense-in-depth: Pattern + Policy Rails + LLM + Ensemble analyzers with max-severity fusion |
| **Hooks** | 6 lifecycle event types, DENY/MODIFY/ALLOW decisions, audit trail, per-hook timeout |
| **Context** | View system with manipulation indices, `LLMSummarizingCondenser`, property enforcement |
| **Conversation** | Local & Remote modes, `StuckDetector` (5 patterns), `CancellationToken`, FIFOLock |
| **Parallel Execution** | `ResourceLockManager` with readers-writer locking, deadlock prevention |
| **LLM** | 100+ providers via litellm, streaming deltas, model failover, non-native tool calling |
| **Secrets** | Fernet-encrypted store, `SecretRegistry` with lazy resolution, audit logging |
| **File Storage** | Pluggable backends: Local, S3, GCS, In-Memory with factory auto-detection |
| **Git Providers** | GitHub, GitLab, Azure DevOps, Bitbucket, Forgejo — unified provider interface |
| **Issue Resolution** | LLM-powered resolver for issues, PRs, merge conflicts with webhook triggers |
| **Project Mgmt** | Jira, Linear, Slack integrations for task tracking and notifications |
| **Observability** | OpenTelemetry tracing, Prometheus metrics, K8s liveness + readiness probes |
| **Migrations** | Alembic database migrations with 7 core tables |

---

## 🔧 What's Fixed in v5.1.1

v5.1.1 is a maintenance release that closes 32 bugs across the agent core, security layer, FastAPI server, cron scheduler, secrets redaction, multi-agent role pipeline, and observability subsystem. All fixes are covered by the existing test suite plus two new regression tests; the full suite runs **212 passed, 2 skipped, 0 failed**.

### Critical Runtime Fixes

| # | Component | Bug | Fix |
|---|---|---|---|
| 1 | `app/agent/router.py` | `AgentRegistry._evict_idle` was declared `async` but called from sync `get()`/`put()` without `await` — eviction never ran, so idle-TTL test expected `None` but got the cached agent. | Converted `_evict_idle` to a sync method; agent `cleanup()` coroutines are scheduled fire-and-forget via `_safe_create_task`. Also fixed LRU `put()` path that moved-to-end but did not overwrite the stored agent when re-inserting an existing key. |
| 2 | `app/cli.py` | `logger` was referenced in two functions without being imported → `NameError` on the Spinner long-operation exit path and the background-task checkpoint-restore path. | Local `from app.logger import logger as _logger` imports at the use-site. |
| 3 | `app/conversation/stuck_detector.py` | `_action_fingerprint` referenced undefined `tool_call` instead of the local `tool_name` → `NameError` on every action without a `.function` attribute, breaking stuck detection. | Use `tool_name` consistently. |
| 4 | `app/integrations/slack.py` | `re` was used in `@self._bolt_app.action(re.compile(...))` but never imported → `NameError` on Slack Bolt action-handler registration. | Added `import re`. |
| 5 | `app/server/webhook_router.py` | **Route ordering bug**: `@router.post("/{hook_id}")` was declared before `@router.post("/create")`, so FastAPI matched the parameterised path first and `POST /webhooks/create` returned 404 ("Webhook 'create' not found"). Create / list / delete all broken via HTTP. | Reordered the router so literal sub-paths (`/create`, `/sign/{hook_id}`) come before the parameterised catch-all. Added regression tests using the real FastAPI TestClient. |
| 6 | `app/observability/health.py` | `LLMHealthChecker._test_api_call` was `def` (sync) but called `llm.ask(...)` which is async → returned a coroutine object that was silently discarded (F841 `result`). The health check would always report success regardless of the LLM's actual state. | Bridge the sync/async boundary via a worker thread running `asyncio.run`. Also fixed the call signature: `LLM.ask` takes a list of `Message` objects, not a string. |
| 7 | `app/llm/profile_rotation.py` | `ModelProfile.default()` exception fallback called `cls(name="default")` but `__init__` does not accept `name` → `TypeError` masked the original error. | Construct the profile first, then set `.name`. |
| 8 | `app/llm/credential_pool.py` | Forward-reference `"ModelProfile"` triggered F821 (undefined name) under strict type checking. | Use `TYPE_CHECKING` import so the symbol is resolvable for type checkers without creating a runtime circular import. |
| 9 | `app/observability/metrics.py` | `Union` was used in three module-level type hints but never imported → F821 on module import under strict checkers. | Added `Union` to the existing `typing` import. |
| 10 | `app/voice/talk.py` | `Any` was used in two instance-variable annotations but never imported → F821. | Added `Any` to the existing `typing` import. |

### Logic & Correctness Fixes

| # | Component | Bug | Fix |
|---|---|---|---|
| 11 | `app/cron.py` | `_JOBS_FILE = Path(os.getenv(...))` was evaluated ONCE at module import. Runtime changes to `MANUSCLAW_CRON_FILE` (tests, profile switching, CLI overrides) were silently ignored. | Replaced with `_get_jobs_file()` lazy resolver called inside `_load_jobs` / `_save_jobs`. |
| 12 | `app/cron.py` | `manusclaw-cron --trigger JOB` did not `return` after triggering → fell through to `asyncio.run(scheduler.run_forever())` and blocked the terminal forever. | Added `return`. |
| 13 | `app/cron.py` | `--list` output overwrote `output` on every loop iteration (`output = f"{t}"`) instead of appending, so only the LAST output_target was ever shown. | Use `output += f" {t}"` and strip. |
| 14 | `app/skills/skill_engine.py` | Same module-level-eval bug as cron.py: `_SKILLS_DIR` was set at import time and ignored subsequent `MANUSCLAW_SKILLS_DIR` changes. | Replaced with `_get_skills_dir()` lazy resolver; updated `_load_user()` and `create()` to call it. |
| 15 | `app/tool/memory_tool.py` | Same bug: `_WORKSPACE` / `MEMORY_FILE` / `USER_FILE` frozen at import. The `tmp_workspace` pytest fixture set `MANUSCLAW_WORKSPACE` at runtime, but `MemoryTool.execute()` still wrote to the import-time path — tests passed only because they manually monkey-patched `mt.MEMORY_FILE`. | Added `_get_workspace()` / `_memory_file()` / `_user_file()` lazy resolvers; rewrote `execute()` to use them. |
| 16 | `app/task_queue.py` | Same bug: `_WORKSPACE` / `_DB_PATH` evaluated at import. | Added `_get_db_path()` lazy resolver; `TaskQueue.__init__` calls it when no explicit path is provided. |
| 17 | `app/llm/secret_redaction.py` | AWS-secret pattern used a non-capturing prefix group `(?:secret_key...|aws_secret...)` so `redact()` replaced the entire match including the prefix — `secret_key=ABC...` became `***REDACTED***` (prefix lost). | Converted to a capturing group and use the `\1` backreference pattern, matching the other redaction rules. |
| 18 | `app/integrations/resolver.py` | `clear_results(older_than_hours=24)` computed `cutoff` but never used it — every terminal-status result was removed regardless of age, breaking the documented "older than N hours" contract. | Now uses `started_at` to filter by age, with a safe default (keep results we can't prove are old enough). |

### Resource Leak Fixes

| # | Component | Bug | Fix |
|---|---|---|---|
| 19 | `app/agent/roles/engineer.py` | `Manus()` instances were created for the main pass and the retry pass but `cleanup()` was never called → leaked Bash subprocesses (and any other tool resources) for the lifetime of the process. | Wrapped each Manus run in `try/finally` with a `_cleanup_agent` helper. |
| 20 | `app/agent/roles/qa.py` | Same leak as engineer.py: the QA Manus agent was never cleaned up. | Added `try/finally` with cleanup call. |

### Test Pollution Fix

| # | Component | Bug | Fix |
|---|---|---|---|
| 21 | `tests/test_voice.py` | `test_get_tts_provider_returns_nulltts_stub` did `tts_mod._create_provider = lambda name: ...` — a permanent module-level monkeypatch that leaked into every subsequent test in the file, causing `test_get_tts_provider_preferred_openai` to receive `NullTTS` instead of `OpenAITTS`. | Use the `monkeypatch` fixture so the override is automatically restored at test teardown. |

### Dead-Code / F841 Cleanup

| # | Component | Issue | Resolution |
|---|---|---|---|
| 22 | `app/canvas/tool.py` | `_add_chart` captured `state = await self._server.update(...)` but never used it. | Now reports the resulting component count for consistency with the other canvas method. |
| 23 | `app/file_store/s3.py` | `write_stream` computed `key = self._make_key(path)` but never used it. | Removed the assignment but kept the call for its path-traversal-validation side-effect. |
| 24 | `app/conversation/local_conversation.py` | `_do_fork` computed `fork_log_path` but never used it. | Now logged at DEBUG level so the path is visible in diagnostics. |
| 25 | `app/integrations/webhook_handler.py` | `handler_result = await handler(event)` discarded result. | Replaced with bare `await handler(event)` + explanatory comment. |
| 26 | `app/parallel_executor/executor.py` | `call_lookup = {c.call_id: c for c in calls}` built but never used. | Removed with explanatory comment (results are correlated via `zip()`). |
| 27 | `app/llm/litellm_client.py` | `except Exception as e: ... raise` — `e` unused. | Dropped the `as e` binding. |
| 28 | `app/observability/health.py` | Two `except Exception as e: ... raise` blocks — `e` unused. | Dropped the `as e` bindings. |
| 29 | `app/voice/wake.py` | `sample_width = 2` assigned but never used. | Converted to a comment so the int16 / 2-byte intent is preserved for future readers. |
| 30 | `app/integrations/resolver.py` | `service` and `content` bindings unused in the `apply_changes` path. | Removed bindings with explanatory comments noting why the calls are still made. |

### New Regression Tests

| # | Test | Purpose |
|---|---|---|
| 31 | `tests/test_webhooks.py::test_webhook_router_create_endpoint_not_swallowed_by_catchall` | POSTs to `/webhooks/create` via the real FastAPI `TestClient` and asserts `200` (was `404` before the route-order fix). |
| 32 | `tests/test_webhooks.py::test_webhook_router_trigger_still_works_after_reorder` | Ensures the parameterised `POST /webhooks/{hook_id}` route still triggers webhooks with non-literal IDs after the reorder. |

### Verification

- **Test suite:** `pytest` → 212 passed, 2 skipped, 0 failed (was 210 passed, 2 failed in v5.1.0).
- **Static analysis:** `ruff check app/ --select F821,F841` → 0 errors (was 23 in v5.1.0).
- **HTTP smoke test:** FastAPI `TestClient` hits against `/healthz`, `/`, `/tools`, `/sessions`, `/webhooks` (create / list / trigger-with-HMAC / delete) all pass.
- **Path-traversal audit:** `LocalFileStore._resolve()` rejects `../../../etc/passwd`, `/etc/passwd`, `a/../../b`, `../outside`, `subdir/../../../etc/passwd` — all blocked with `FileStorePermissionError`.
- **Module import audit:** all 133 main modules import cleanly under Python 3.12.

---

## 🌟 Overview

ManusClaw is an enterprise-grade autonomous AI agent framework that empowers Large Language Models to **plan**, **execute code**, **browse the web**, **manage files**, **resolve issues**, and **complete complex multi-step tasks** — all autonomously.

At its core is the **PAORR reasoning loop** (Plan → Act → Observe → Reflect → Retry), a self-correcting execution model. Combined with **DAG-based multi-agent orchestration**, **defense-in-depth security**, **offline LLM support (GGUF/HuggingFace/Ollama)**, and **enterprise observability**, ManusClaw runs anywhere — cloud, local, or fully air-gapped.

**Why ManusClaw?**

| Challenge | ManusClaw Solution |
|---|---|
| Vendor lock-in | 100+ cloud providers + offline GGUF/HuggingFace/Ollama with credential rotation and model failover |
| No internet access | Fully offline: GGUF via llama-cpp-python, HuggingFace local, Ollama local — zero cloud dependency |
| No persistence | SQLite-backed sessions, event logs, task queues — all survive restarts |
| Security blind spots | Defense-in-depth: Pattern → Rails → LLM → Ensemble with audit trails |
| Single-agent limit | DAG-based Multi-Agent Orchestrator with per-channel/per-account routing |
| Context overflow | View system with LLM Summarizing Condenser and property enforcement |
| Tool chaos | Heuristic + LLM ToolSelector scores 17+ tools with failure penalties |
| Platform fragmentation | 13+ messaging adapters, voice, canvas, SSH, webhooks, cron |
| No observability | OpenTelemetry, Prometheus metrics, K8s health probes, correlation IDs |
| Secret management | Fernet encryption, SecretRegistry with lazy resolution |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PRESENTATION LAYER                                 │
│  CLI · WebChat · Canvas (A2UI) · SSH Server · 13+ Messaging · Voice        │
│  Desktop: macOS Menubar · Windows Hub · Mobile Node Client                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                           AGENT LAYER                                       │
│  ┌──────────────┐  ┌───────────────┐  ┌─────────────────────────────────┐  │
│  │  PAORR Loop  │  │  Multi-Agent  │  │  Role Pipeline                  │  │
│  │ Plan → Act → │  │  Orchestrator │  │  PM → Architect → Engineer → QA │  │
│  │ Observe →    │  │  (DAG-based)  │  │  with RoleMessageBus            │  │
│  │ Reflect →    │  └───────────────┘  └─────────────────────────────────┘  │
│  │ Retry        │                                                         │
│  └──────────────┘                                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                         MIDDLEWARE LAYER                                    │
│  Hooks · Security Ensemble · Context View + Condenser · Conversation       │
│  Parallel Executor (ResourceLock) · Event System (Discriminated Unions)    │
├─────────────────────────────────────────────────────────────────────────────┤
│                        INTEGRATION LAYER                                    │
│  LLM (100+ cloud + GGUF/HF/Ollama offline) · Git Providers (5 platforms)  │
│  Issue Resolver · Project Mgmt (Jira/Linear/Slack) · Secrets (Fernet)     │
│  File Storage (S3/GCS/Local) · MCP Protocol · Observability (OTEL/Prom)   │
├─────────────────────────────────────────────────────────────────────────────┤
│                          TOOL LAYER (17+)                                   │
│  Bash · Python · Node.js · Browser · WebSearch · Crawl4AI · ImageGen      │
│  StrReplace · Memory · Delegate · Planning · DataViz · AskHuman           │
│  PlatformCtrl · SkillManager · CrossSessionSearch · Terminate · Selector   │
├─────────────────────────────────────────────────────────────────────────────┤
│                       INFRASTRUCTURE LAYER                                  │
│  SQLite WAL · SessionDB (FTS5) · Cron Scheduler · TaskQueue · Sandbox     │
│  (Docker/SSH/OpenShell) · Alembic Migrations · LongTermMemory             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

### 🤖 Agent System — PAORR Loop + Multi-Agent + Roles

The PAORR loop is the heart of ManusClaw — a self-correcting reasoning cycle that plans, acts, observes, reflects, and retries until the task is complete.

| Feature | Description |
|---|---|
| **PAORR Loop** | Plan → Act → Observe → Reflect → Retry — autonomous self-correction at every step |
| **Self-Check** | Manus agent performs self-check every 3 steps to verify progress |
| **Multi-Agent Orchestrator** | DAG-based pipeline with topological sorting (Kahn's algorithm), event hooks, global timeout |
| **Role Pipeline** | ProductManager → Architect → Engineer → QA with typed `RoleResult` and `RoleMessageBus` |
| **Agent Router** | Per-channel and per-account routing with LRU cache (64 entries, 300s idle TTL) |
| **Agent Registry** | Dynamic agent class import (sandboxed to `app.` namespace) with idle eviction + cleanup |
| **Identity Guard** | 30+ anti-jailbreak patterns in 9 languages (English, Chinese, Spanish, French, German, Portuguese, Japanese, Korean, Russian) |
| **Permission Gate** | 3-tier access control: AUTONOMOUS / CONFIRM / RESTRICTED with catastrophic pattern blocking |
| **Skill Engine** | Auto-injection of domain expertise from Markdown skill files based on relevance matching |
| **PlanningFlow** | Step-by-step task decomposition with scoring, replanning, and agent caching |

**Agent Types:**

| Agent | Description |
|---|---|
| **Manus** | Full-featured autonomous agent with all 14+ tools, PAORR loop, self-check every 3 steps |
| **ReAct** | Think → Act → Observe → Reflect → Retry loop with max 3 reflect retries per step |
| **ToolCall** | Structured function-calling agent with ToolSelector scoring and permission gate |
| **Browser** | Browser-focused agent (browser, search, crawl, terminate) |
| **DataAnalysis** | Manus + DataVisualization tool for data exploration workflows |
| **MCP** | Connects to MCP servers (stdio/SSE) and proxies their tools |

**Agent Inheritance:** `BaseAgent → ReActAgent → ToolCallAgent → Manus → DataAnalysisAgent`

### 📡 Event System — Discriminated Unions + EventLog

| Feature | Description |
|---|---|
| **17 Event Types** | SystemPrompt, Message, Action, Observation, Condensation, CondensationRequest, AgentError, Token, Interrupt, Pause, ConversationError, StateUpdate, LLMCompletionLog, HookExecution, StreamingDelta, UserRejectObservation, ResumeTranscript |
| **Discriminated Unions** | Type-safe polymorphism via `kind` literal discriminators for pattern matching |
| **LLMConvertibleEvent** | Protocol for events that convert to LLM message format with parallel tool-call batching |
| **File-Backed EventLog** | NDJSON append-only log with O(1) length queries, lazy loading, atomic writes |
| **Crash Safety** | Temp-file-then-rename strategy; count file updated post-write; `reindex()` recovery |
| **JSON Serialization** | Full serialize/deserialize with `serialize_batch()` / `deserialize_batch()` for NDJSON |

### 🛡️ Security Defense-in-Depth

Multi-layer security analysis combining pattern matching, policy rails, LLM-based analysis, and ensemble fusion.

| Layer | Class | Description |
|---|---|---|
| **Pattern Analyzer** | `PatternSecurityAnalyzer` | 8 regex patterns across 2 corpora (executable + all-field): `rm_rf`, `sudo_rm`, `eval_call`, `subprocess`, `curl_pipe_exec`, `inject_override`, `inject_mode_switch`, `inject_identity` |
| **Policy Rails** | `PolicyRailSecurityAnalyzer` | 3 structural rails: fetch-to-exec, raw-disk-op, catastrophic-delete. Per-segment evaluation prevents cross-field false positives |
| **LLM Analyzer** | `LLMSecurityAnalyzer` | AI-powered semantic analysis for subtle threats, configurable call budget |
| **Ensemble Analyzer** | `EnsembleSecurityAnalyzer` | Combines all analyzers with max-severity fusion, crash isolation, full audit trail |
| **Confirmation Policy** | `NeverConfirm` / `ConfirmRisky` | Human-in-the-loop confirmation for HIGH/UNKNOWN risk operations |
| **Cipher Module** | `Cipher` | Fernet-based encryption for data at rest with key rotation support |
| **Secret Redaction** | `redact()` | Context-aware detection and masking of API keys, tokens, AWS secrets with prefix preservation |

### 🪝 Hooks System

| Event Type | When | Can Block? |
|---|---|---|
| `SESSION_START` | Agent session begins | No |
| `USER_PROMPT_SUBMIT` | Before user prompt enters loop | Yes (DENY/MODIFY) |
| `PRE_TOOL_USE` | Before tool execution | Yes (DENY) |
| `POST_TOOL_USE` | After tool returns | No |
| `STOP` | Agent about to stop | Yes (DENY) |
| `SESSION_END` | Session terminates | No |

**Built-in Hooks:** `LoggingHook` (logs all events), `SecurityHook` (integrates with security analyzers and blocks dangerous actions), `AuditHook` (JSONL audit trail with secret sanitization).

**Hook Loading:** YAML config with `class_path` imports, Python module auto-discovery from manusclaw home directory.

### 🧠 Context Management — View + Condenser

| Feature | Description |
|---|---|
| **View System** | Linear event projection with `manipulation_indices` — safe points for condensation |
| **View Properties** | `BatchAtomicity`, `ObservationUniqueness`, `ToolCallMatching`, `ToolLoopAtomicity` — structural integrity guarantees |
| **LLM Summarizing Condenser** | Dedicated condenser LLM generates summaries of removed events; 3 trigger reasons: REQUEST (hard), TOKENS (soft), EVENTS (soft) |
| **Rolling Window Condenser** | Keeps the N most recent events, drops the rest |
| **Pipeline Condenser** | Chains multiple condensers with `stop_on_first` short-circuit and aggregated metrics |
| **Progressive Truncation** | If condenser LLM fails, progressively truncates with retry scaling (5 retries, 0.8x per retry) |
| **No-op Condenser** | Pass-through for when condensation is disabled |

### 💬 Conversation System

| Feature | Description |
|---|---|
| **Local Conversation** | In-process conversation with event log, async support, fork/branch, confirmation mode |
| **Remote Conversation** | WebSocket-backed conversation for distributed deployments with reconnection + event buffering |
| **StuckDetector** | 5 detection patterns: repeating action-observation, action-error loops, agent monologue, alternating patterns, context window overflow |
| **CancellationToken** | Thread-safe cancellation with `raise_if_cancelled()`, timeout support, context manager |
| **FIFOLock** | Fair, starvation-free lock (sync + async variants) guaranteeing FIFO ordering |
| **Conversation Factory** | Auto-creates Local or Remote based on configuration |

### ⚡ Parallel Tool Execution

| Feature | Description |
|---|---|
| **ResourceLockManager** | Readers-writer locking per resource with deadlock prevention via global acquisition ordering |
| **Declared Resources** | Each tool declares `READ`/`WRITE` dependencies: `file_resource()`, `terminal_resource()`, `network_resource()` |
| **ParallelToolExecutor** | Thread-pool concurrent execution with resource conflict serialization |
| **AsyncParallelToolExecutor** | `asyncio.gather` variant with semaphore for concurrency capping |
| **Metrics** | Execution time, concurrency level, resource conflicts, per-tool timeout |

### 🧠 LLM Integration — 100+ Providers + Offline + Streaming

#### Cloud Providers

| Provider | Class | Auth |
|---|---|---|
| **OpenAI** | Native SDK | `OPENAI_API_KEY` (supports `_2`, `_3` for pool) |
| **Anthropic** | Native SDK | `ANTHROPIC_API_KEY` |
| **Google/Gemini** | `google-generativeai` | `GOOGLE_API_KEY` |
| **Mistral** | `MistralClient` | `MISTRAL_API_KEY` |
| **AWS Bedrock** | `BedrockClient` (Converse API) | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` |
| **100+ via litellm** | `LiteLLMClient` | Varies per provider |

#### Offline / Local / Air-Gapped Providers

| Provider | Class | Details |
|---|---|---|
| **GGUF** | `GGUFRouter` | llama-cpp-python, fully offline, GPU support (`n_gpu_layers`), tool-call parsing from text |
| **Ollama** | `OllamaRouter` | Official SDK, local + Ollama Cloud with API key |
| **LMStudio** | `OpenAICompatRouter` | OpenAI-compatible endpoint at `localhost:1234` |
| **text-gen-webui** | `OpenAICompatRouter` | OpenAI-compatible endpoint at `localhost:5000` |
| **HuggingFace** | `HuggingFaceRouter` | Inference API + Spaces + Dedicated Endpoints |
| **Any OpenAI-compat** | `OpenAICompatRouter` | Generic REST endpoint (Groq, Together, etc.) |
| **Mock** | `MockLLM` | No API key needed, safe for immediate testing |

#### Pre-Configured Provider TOMLs

| Config File | Service | Type |
|---|---|---|
| `ollama.toml` | Ollama (local) | Free / Local |
| `ollama-cloud.toml` | Ollama Cloud | Paid / API |
| `openrouter.toml` | OpenRouter (200+ models) | Paid / API |
| `pollinations.toml` | Pollinations AI | **Free / No key needed** |
| `7llm.toml` | 7LLM | Paid / API |
| `opencode.toml` | OpenCode (deepseek-v4-flash-free) | **Free / No key needed** |

#### LLM Infrastructure

| Feature | Description |
|---|---|
| **Credential Pool** | Per-provider multi-key rotation with priority ordering, cooldown, and health tracking. Supports `OPENAI_API_KEY_2`, `_3`, etc. |
| **Cross-Provider Rotator** | `CrossProviderRotator` — unified credential interface across all providers |
| **Profile Rotation** | `ProfileRotator` — ordered model entries with priority, fallback weights, per-session profile selection, bounded cache |
| **Fallback Strategy** | Configurable model chains with per-model cooldown, 8 fallback triggers (rate_limit, context_window, timeout, etc.) |
| **Streaming** | Token-level streaming with `StreamingDeltaEvent`, SSE for web clients, backpressure handling, multi-provider chunk parsing |
| **Enhanced Retry** | 5 backoff strategies (fixed, linear, exponential, exponential_jitter, decorrelated_jitter), retry budgets, provider-specific error mapping |
| **Token Budget** | Per-session token tracking with grace call support and cost estimation |
| **LLM Metrics** | Per-conversation cost tracking, latency percentiles (p50/p95/p99), budget alerts, Prometheus export |
| **Non-Native Tool Calling** | `NonNativeToolCallingMixin` — emulates function calling for models without native support via prompt engineering + JSON extraction |
| **Secret Redaction** | Context-aware masking of API keys, Bearer tokens, AWS secrets in prompts and logs |

### 🔐 Secrets Management — Fernet Encryption

| Feature | Description |
|---|---|
| **Fernet Encryption** | Symmetric encryption for secrets at rest with `FERNET_TOKEN_PREFIX` identifier |
| **SecretRegistry** | Named registry with lazy resolution from backing store, namespaces, caching |
| **File Secrets Store** | Encrypted file-based storage with atomic writes and `0600` permissions |
| **Secret Sources** | `STATIC` (provided value), `LOOKUP` (fetched from URL), `ENV` (from environment variable) |
| **API Router** | FastAPI `/secrets` endpoints — never exposes raw values (masked with `***`) |
| **Key Rotation** | `Cipher` supports key rotation with `add_key()` for seamless rotation |

### 📦 File Storage Backends

| Backend | Use Case | Key Feature |
|---|---|---|
| **Local** | Filesystem storage (default) | Atomic writes, sidecar `.meta.json`, streaming |
| **S3** | AWS S3 / MinIO | Presigned URLs, retry with backoff, async executor |
| **GCS** | Google Cloud Storage | Signed URLs (v4), retry with backoff, async executor |
| **In-Memory** | Testing and ephemeral | Full metadata tracking, size limits |

**Factory auto-detection** from `MANUSCLAW_FILE_STORE_BACKEND` env var, config, or explicit parameter.

### 🔀 Git Provider Integrations

| Provider | OAuth | PRs/MRs | Issues | Branches | Files | Suggested Tasks | Webhooks |
|---|---|---|---|---|---|---|---|
| **GitHub** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **GitLab** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Azure DevOps** | ✅ | ✅ | ✅ (Work Items) | ✅ | ✅ | ✅ | ✅ |
| **Bitbucket** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Forgejo** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

All providers implement a unified `GitProviderService` interface with both sync and async methods, thread-safety, rate limiting, and exponential backoff.

**Suggested Task Types:** `OPEN_ISSUE`, `FAILING_CHECKS`, `MERGE_CONFLICT`, `UNRESOLVED_COMMENTS`

**Git Provider Router** — URL-based provider detection (10+ URL patterns including self-hosted instances) with service caching.

### 🎯 Issue & PR Resolution

LLM-powered automated resolution for issues, PRs, and merge conflicts.

| Resolution Type | Description |
|---|---|
| **Issue Resolution** | Analyze reported issue → generate fix → apply changes → post summary comment |
| **PR Update** | Process review feedback → update code → respond to comments |
| **Merge Conflict Resolution** | Detect conflicts → resolve with LLM assistance → push resolution |
| **Comment Response** | Reply to issue/PR comments with context-aware responses |

**Features:** Thread-safe with per-resolution locking, timeout protection, full audit trail, concurrency semaphore (max 5), Jinja2 prompt templates per provider.

**Webhook Handler:** HMAC-SHA256 signature verification, event deduplication, background processing with retry, provider-specific payload normalization.

### 📋 Project Management — Jira + Linear + Slack

| Integration | Capabilities |
|---|---|
| **Jira Cloud + DC** | OAuth/PAT auth, create/update/search issues, JQL search, comments, transitions, ADF to text, webhooks |
| **Linear** | GraphQL API, OAuth 2.0, teams, issues, comments, suggested tasks, webhooks |
| **Slack** | Socket Mode + API, slash commands (`/manusclaw`, `/resolve`, `/review`), Block Kit, thread conversations, interactive buttons, file uploads |

### 📊 Observability — OpenTelemetry + Prometheus

| Component | Description |
|---|---|
| **OpenTelemetry Tracing** | `@observe` decorator for sync/async functions, context propagation, custom span attributes (model, tokens, tool_name) |
| **Prometheus Metrics** | Counters, histograms, gauges for LLM calls, tool execution, conversations, tokens, errors |
| **Health Probes** | Kubernetes-style liveness (`/healthz`) and readiness (`/ready`) with component checkers |
| **Component Checkers** | `DatabaseHealthChecker`, `LLMHealthChecker`, `SandboxHealthChecker` |
| **Correlation IDs** | Request-scoped correlation ID propagation across service boundaries, `error_id` for 500 error lookup |
| **Structured Logging** | JSON-structured logs with context enrichment and sensitive data redaction |
| **Metrics Export** | Prometheus exposition format, JSON dict format |

**Built-in Metrics:** `llm_calls_total`, `llm_call_duration_seconds`, `tool_calls_total`, `tool_call_duration_seconds`, `conversation_duration_seconds`, `active_conversations`, `token_usage_total`, `error_count_total`

### 📨 13+ Messaging Channels

| Channel | Type | Key Feature |
|---|---|---|
| **Telegram** | Bot API | Inline keyboards, file handling, media messages |
| **Discord** | Bot | Slash commands, embeds, thread support |
| **Slack** | Bolt SDK | Blocks, modals, events, interactive components |
| **WhatsApp** | Business API | Media messages, templates, read receipts |
| **Signal** | CLI | End-to-end encrypted messaging |
| **Microsoft Teams** | Bot Framework | Adaptive cards, tabs, channel messages |
| **Matrix** | Protocol | Federation, E2E encryption, rooms |
| **IRC** | Client | Multi-network, channels, CTCP |
| **Twitch** | Chat | Stream integration, commands |
| **Google Chat** | Webhook | Spaces, threads, cards |
| **WebChat** | Built-in | WebSocket, real-time, canvas integration |
| **Email** | SMTP/IMAP | Send/receive, Gmail Pub/Sub auto-reply |
| **Messaging Gateway** | Unified | Multi-adapter routing, agent caching, eviction with cleanup |

### 🎤 Voice System — Wake Word + STT + TTS

| Feature | Description |
|---|---|
| **Wake Word Detection** | Pvporcupine or STT-based wake word ("Hey ManusClaw") |
| **Talk Mode** | Continuous mic → STT → agent → TTS conversation loop |
| **Text-to-Speech** | 3 backends: OpenAI TTS, ElevenLabs, System TTS (espeak/piper) |
| **Speech-to-Text** | 3 engines: OpenAI Whisper, Google STT, Vosk (fully offline) |

### 🎨 Canvas UI — A2UI Protocol

| Feature | Description |
|---|---|
| **A2UI Protocol** | Real-time WebSocket updates from agent to browser with typed component model |
| **Canvas Server** | Built-in WebSocket server for live rendering |
| **Canvas Tool** | Agent tool for rendering charts, tables, buttons, text, images, containers |
| **Chart Types** | Bar, line, scatter, pie, histogram, area, radar |
| **Component Types** | Text, Image, Button, Table, Chart, Container — composable UI |
| **Mobile Nodes** | Connect mobile devices as canvas nodes |
| **Static HTML** | Standalone `canvas.html` for quick deployment |

### 🛠️ 17+ Tools — Intelligent Selector

| # | Tool | Category | Description |
|---|---|---|---|
| 1 | **Bash** | Execution | Persistent async shell (Linux/macOS/Windows/Termux). No artificial timeout. Only OS-destroying commands blocked. Atexit cleanup for orphaned processes. |
| 2 | **PythonExecute** | Execution | Isolated subprocess (2GB memory rlimit on Linux). No default timeout. Full stdout/stderr capture. |
| 3 | **NodeExecute** | Execution | Execute Node.js/JavaScript in isolated subprocess |
| 4 | **StrReplaceEditor** | Files | View, create, str_replace, insert, undo_edit — precise file operations |
| 5 | **BrowserUse** | Web | Playwright browser: navigate, click, type, screenshot, get_text, execute_js, tabs |
| 6 | **WebSearch** | Web | Multi-engine search: DuckDuckGo → Bing fallback with retry |
| 7 | **Crawl4AI** | Web | Extract clean content from URLs (crawl4ai or aiohttp fallback) |
| 8 | **ImageGenerate** | Creative | Generate images via FAL.ai (or mock). Saves to `workspace/images/` |
| 9 | **DataVisualization** | Analysis | Generate charts (bar, line, scatter, pie, histogram). PNG or HTML with mpld3. |
| 10 | **Memory** | Knowledge | CRUD for `MEMORY.md` and `USER.md` — persistent context files across sessions |
| 11 | **Planning** | Planning | Create/update/mark_step/get multi-step execution plans |
| 12 | **Delegate** | Multi-Agent | Spawn isolated subagent (Manus instance) for independent subtasks |
| 13 | **AskHuman** | Interaction | Request clarification from user (interactive mode only) |
| 14 | **PlatformControl** | System | Authenticate and control external platforms: GitHub, Vercel, WordPress, HuggingFace, Netlify, Discord, Telegram, generic REST |
| 15 | **SkillManager** | Skills | Create/patch/delete/list/get skill files |
| 16 | **CrossSessionSearch** | Knowledge | FTS5 full-text search across all past sessions |
| 17 | **Terminate** | Control | Signal task completion |

**Tool Selector** — Confidence-based tool scoring (0.0–1.0) with heuristic keyword matching, optional LLM scoring, failure penalty, recency diversification, and public stats API.

### 📡 SSH Server

| Feature | Description |
|---|---|
| **Remote Access** | Full SSH server for remote agent control and task execution |
| **Shell Integration** | Interactive shell with agent commands via SSH |
| **Configurable** | Port, host, and authentication via `config.toml` or env vars |

### ⏰ Cron Scheduler

| Feature | Description |
|---|---|
| **YAML Persistence** | Cron jobs persisted to YAML with auto-reload |
| **Webhook Delivery** | Job results delivered via webhooks |
| **Multi-Platform Output** | Results posted to messaging channels |
| **Auto-Cleanup** | Agent cleanup after each job execution (no resource leaks) |
| **Secret Protection** | Webhook secrets redacted in storage, loaded from env var |

### 🧩 Skills Engine

Built-in skills auto-injected based on relevance to the current task:

| Skill | File | Domain |
|---|---|---|
| **Coding** | `coding.md` | Software development, debugging, code review |
| **DevOps** | `devops.md` | CI/CD, Docker, Kubernetes, deployment |
| **Data Analysis** | `data_analysis.md` | Pandas, statistics, data exploration |
| **Research** | `research.md` | Web research, summarization, fact-checking |
| **MLOps** | `mlops.md` | Machine learning pipelines, model training |
| **GitHub** | `github.md` | Repository management, PR/Issue workflows |

### 🔌 MCP Protocol — Client + Server

| Feature | Description |
|---|---|
| **MCP Client** | Connect to MCP servers via stdio or SSE, proxy their tools into the agent |
| **MCP Server** | Expose manusclaw tools as MCP-compatible server |
| **Tool Proxying** | MCP tools become native manusclaw tools with full schema conversion |
| **Auto-Discovery** | Configure MCP servers in `config.toml` with auto-connect |

### 🖥️ Desktop Apps

| App | Platform | Description |
|---|---|---|
| **macOS Menubar** | macOS | System tray menubar app via `rumps` |
| **Windows Hub** | Windows | Desktop hub for managing manusclaw instances |
| **Mobile Node** | iOS/Android | Connect mobile devices as canvas nodes |

### 💾 Session & Memory System

| Feature | Description |
|---|---|
| **SessionDB** | SQLite WAL with FTS5 full-text search, session branching, compression |
| **Session Resume** | Resume interrupted sessions with `/resume` command |
| **Session Branching** | Fork sessions with `/branch` for parallel exploration |
| **Long-Term Memory** | RAG-like persistent memory with FTS5 + LIKE fallback, SQLite WAL |
| **Short-Term Memory** | Conversation buffer with refresh, snapshot, and restore |
| **Task Queue** | Persistent SQLite task queue with priority ordering, checkpoint/resume, worker pool, deduplication |
| **Alembic Migrations** | 7 core tables: conversations, events, sessions, tasks, credentials, secrets, audit_log |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- At least one LLM API key (or use **free** Pollinations/OpenCode providers — no key needed!)
- Or run fully **offline** with GGUF/Ollama/HuggingFace — no internet needed!

### Installation

```bash
# Clone the repository
git clone https://github.com/manusagents/manusclaw.git
cd manusclaw

# Install dependencies
pip install -e .

# Or install with all enterprise features
pip install -e ".[all-plus]"

# Configure your API key
cp config.toml config.toml.local
# Edit config.toml with your API keys, or set env vars:
export OPENAI_API_KEY=sk-...

# Run your first task
python main.py "Create a Python script that generates Fibonacci numbers"
```

### Free / No API Key Required

```bash
# Use Pollinations (free, no key)
# Set in config.toml: provider = "pollinations"
# Or use OpenCode (free deepseek-v4-flash)
# Set in config.toml: provider = "opencode"
```

### Fully Offline (Air-Gapped)

```bash
# GGUF — download any .gguf model and run with zero internet
# Set in config.toml:
#   provider = "gguf"
#   model_path = "/path/to/model.gguf"
#   n_gpu_layers = 0  # set >0 for GPU acceleration

# Ollama — run ollama serve, then:
#   provider = "ollama"
#   model = "llama3"

# HuggingFace — use Inference API, Spaces, or local models
#   provider = "huggingface"
#   model = "meta-llama/Llama-3-8B"
```

### One-Line Install (Linux/macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/manusagents/manusclaw/main/install.sh | bash
```

### Windows

```powershell
.\install.ps1
```

---

## ⚙️ Configuration

ManusClaw uses `config.toml` for all configuration. Key sections:

```toml
[llm]
model = "gpt-4o"
provider = "openai"                   # openai | anthropic | google | mistral | bedrock | ollama | gguf | huggingface | litellm | openrouter | pollinations | opencode | 7llm | mock
api_key = ""                          # Or set OPENAI_API_KEY env var
max_tokens = 4096
temperature = 0.7

[llm.streaming]
enabled = true
buffer_size = 4096

[llm.fallback]
enabled = false
chain = ["gpt-4o", "claude-3-5-sonnet-20241022", "gemini-2.0-flash"]

[agent]
max_iterations = 50
mode = "confirm"                      # autonomous | confirm | restricted

[security]
enabled = true
analyzers = ["pattern", "rails"]      # pattern | rails | llm | ensemble
confirmation_threshold = "medium"      # low | medium | high

[hooks]
enabled = true
auto_load = true
timeout_s = 30

[context]
max_events = 200
max_tokens = 128000
condenser_type = "rolling"            # rolling | llm_summarizing | noop

[conversation]
max_iterations = 30
confirmation_mode = "confirm_risky"   # never_confirm | confirm_risky
stuck_detection = true

[observability]
tracing = false
metrics = true
health_probes = true

[secrets]
backend = "file"                      # file | env
encryption_enabled = true

[file_store]
backend = "local"                     # local | s3 | gcs | memory

[git_providers]
default_provider = "github"           # github | gitlab | azure_devops | bitbucket | forgejo

[integrations]
webhooks_enabled = true
jinja_templates_dir = ""

[parallel_executor]
max_workers = 4
timeout_s = 300

[migrations]
enabled = true
auto_run = false

[sandbox]
backend = "docker"                    # docker | ssh | openshell

[ssh]
host = "0.0.0.0"
port = 2222

[voice]
wake_word = "hey manusclaw"
stt_engine = "openai"                 # openai | google | vosk (offline)
tts_engine = "openai"                 # openai | elevenlabs | system
```

See `config.toml` for the full configuration reference with all options and defaults.

---

## 🐳 Docker Deployment

### Build

```bash
docker build -t manusclaw:latest .
```

### Run CLI Agent

```bash
docker compose up

# One-shot task
docker compose run --rm manusclaw "Your task here"
```

### Run Server Mode

```bash
docker compose --profile server up -d
```

### Run Multi-Agent Pipeline

```bash
docker compose --profile multi up
```

### Docker Compose Services

| Service | Profile | Description |
|---|---|---|
| `manusclaw` | default | Interactive CLI agent |
| `server` | server | FastAPI + WebSocket server on port 8765 |
| `multi-agent` | multi | Multi-agent pipeline runner |

### Environment Variables

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AI...
MISTRAL_API_KEY=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
MANUSCLAW_ALLOWED_ORIGINS=https://yourdomain.com
```

### Health Checks

```bash
curl http://localhost:8765/healthz   # Liveness probe
curl http://localhost:8765/ready     # Readiness probe (checks DB, LLM, sandbox)
```

---

## 📟 Entry Points

ManusClaw installs several CLI commands:

| Command | Description |
|---|---|
| `manusclaw` | Interactive CLI agent with slash commands |
| `manusclaw-server` | FastAPI + WebSocket server |
| `manusclaw-cron` | Cron scheduler daemon |
| `manusclaw-multi` | Multi-agent pipeline runner |
| `manusclaw-sessions` | Session management tool |

### CLI Slash Commands

| Command | Description |
|---|---|
| `/model <name>` | Switch LLM model |
| `/skills` | List available skills |
| `/tools` | List available tools |
| `/memory` | Show memory contents |
| `/compress` | Compress conversation context |
| `/new` | Start new session |
| `/resume <id>` | Resume interrupted session |
| `/branch` | Fork current session |
| `/tasks` | Show background task queue |
| `/bg <task>` | Run task in background |

---

## 🤝 Contributing

We welcome contributions! Here's how to get started:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Write** your code with tests
4. **Run** the test suite (`pytest tests/`)
5. **Submit** a pull request

### Development Setup

```bash
# Install with dev dependencies
pip install -e ".[all]"

# Run tests
pytest tests/ -v

# Run linting
ruff check app/
```

### Code Style

- Python 3.11+ with type hints throughout
- Pydantic v2 models for all data structures (`frozen=True` where appropriate)
- Thread-safe by default (locks on all shared state, `RLock` for reentrant access)
- Docstrings on every public class and function
- Crash-proof: atomic writes, atexit cleanup, proper resource management

---

## 📜 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**ManusClaw v5.1.1** — Built by [The-JDdev (SHS Lab)](https://github.com/The-JDdev)

</div>
