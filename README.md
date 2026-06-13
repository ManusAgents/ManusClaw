<div align="center">

<img src="https://img.shields.io/badge/Version-5.1.0-ff69b4?style=for-the-badge&logo=github&logoColor=white" alt="Version">
<img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
<img src="https://img.shields.io/badge/License-MIT-FFD700?style=for-the-badge&logo=opensourceinitiative&logoColor=black" alt="License">

<br><br>

# 🐾 M A N U S C L A W

### **v5.1.0 — Enterprise-Grade Autonomous AI Agent Framework**

**A production-ready, self-reasoning AI agent framework with DAG-based multi-agent orchestration, defense-in-depth security, 100+ LLM providers, 13+ messaging channels, voice interaction, live canvas, and enterprise observability — built for teams that ship.**

<p>
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Docker-informational?style=flat-square" alt="Platforms">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/LLM-100%2B%20Providers-FF6F00?style=flat-square&logo=brain&logoColor=white" alt="LLM Providers">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Channels-13%2B%20Platforms-00B4D8?style=flat-square&logo=message&logoColor=white" alt="Channels">
  &nbsp;•&nbsp;
  <img src="https://img.shields.io/badge/Tools-18%2B-00C853?style=flat-square" alt="Tools">
</p>

</div>

---

## Table of Contents

- [What's New in v5.1](#-whats-new-in-v51)
- [Overview](#-overview)
- [Architecture](#-architecture)
- [Features](#-features)
  - [Agent System](#-agent-system)
  - [Event System](#-event-system)
  - [Security Defense-in-Depth](#-security-defense-in-depth)
  - [Hooks System](#-hooks-system)
  - [Context Management](#-context-management)
  - [Conversation System](#-conversation-system)
  - [Parallel Tool Execution](#-parallel-tool-execution)
  - [LLM Integration](#-llm-integration)
  - [Secrets Management](#-secrets-management)
  - [File Storage](#-file-storage)
  - [Git Provider Integrations](#-git-provider-integrations)
  - [Issue & PR Resolution](#-issue--pr-resolution)
  - [Project Management](#-project-management)
  - [Observability](#-observability)
  - [Messaging Channels](#-messaging-channels)
  - [Voice System](#-voice-system)
  - [Canvas UI](#-canvas-ui)
  - [Tools](#-tools)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [Docker Deployment](#-docker-deployment)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🆕 What's New in v5.1

ManusClaw v5.1 introduces enterprise-grade capabilities that transform it from a powerful agent framework into a production-ready AI operations platform.

| Category | Highlights |
|---|---|
| **Event System** | Discriminated union types, `LLMConvertibleEvent` protocol, file-backed `EventLog` with O(1) length queries, crash-proof atomic writes |
| **Security** | Defense-in-depth with Pattern, Policy Rails, LLM, and Ensemble analyzers; max-severity fusion; crash isolation |
| **Hooks** | 6 lifecycle event types, blocking (DENY), modification (MODIFY), audit trail, priority-based execution, per-hook timeout |
| **Context** | View system with manipulation indices, `LLMSummarizingCondenser`, property enforcement (atomicity, uniqueness, tool-call matching) |
| **Conversation** | Local & Remote conversation modes, `StuckDetector` (5 patterns), `CancellationToken`, FIFO fair locks |
| **Parallel Execution** | `ResourceLockManager` with readers-writer locking, `FIFOLock` (sync + async), deadlock prevention with global acquisition ordering |
| **LLM** | 100+ providers via litellm, streaming deltas, credential pool rotation, model failover profiles, token budget enforcement |
| **Secrets** | Fernet-encrypted store, `SecretRegistry` with lazy resolution, namespace support, audit logging |
| **File Storage** | Pluggable backends: Local, S3, GCS, In-Memory; factory auto-detection |
| **Git Providers** | GitHub, GitLab, Azure DevOps, Bitbucket, Forgejo — unified provider interface |
| **Issue Resolution** | LLM-powered resolver for issues, PR updates, merge conflicts; thread-safe with timeout protection |
| **Project Management** | Jira, Linear, Slack integrations for task tracking and notifications |
| **Observability** | OpenTelemetry tracing, Prometheus metrics, Kubernetes health probes (liveness + readiness) |
| **Tools** | 18+ built-in tools including Browser Use, Crawl4AI, Data Viz, Image Gen, Memory, Delegate |
| **Channels** | 13+ messaging platforms including WhatsApp, Signal, Teams, Matrix, IRC, Twitch, Google Chat |

---

## 🌟 Overview

ManusClaw is an enterprise-grade autonomous AI agent framework that empowers Large Language Models to **plan**, **execute code**, **browse the web**, **manage files**, **resolve issues**, and **complete complex multi-step tasks** — all autonomously.

At its core is the **PAORR reasoning loop** (Plan → Act → Observe → Reflect → Retry), a sophisticated execution model that gives agents self-correction capabilities. Combined with **DAG-based multi-agent orchestration**, **defense-in-depth security**, and **enterprise observability**, ManusClaw is designed for teams that need reliable, auditable AI automation at scale.

**Why ManusClaw?**

| Challenge | ManusClaw Solution |
|---|---|
| Vendor lock-in | 100+ LLM providers with credential rotation, model failover profiles, and zero-switch routing |
| No persistence | SQLite-backed sessions, event logs, task queues — all survive restarts |
| Security blind spots | Defense-in-depth: Pattern → Rails → LLM → Ensemble analysis with audit trails |
| Single-agent limit | DAG-based Multi-Agent Orchestrator with per-channel/per-account routing |
| Context overflow | View system with LLM Summarizing Condenser and property enforcement |
| Tool chaos | Heuristic ToolSelector scores 18+ tools with failure penalties and recency diversification |
| Platform fragmentation | 13+ messaging adapters, voice, canvas, SSH, webhooks |
| No observability | OpenTelemetry tracing, Prometheus metrics, K8s health probes |
| Secret management | Fernet encryption, SecretRegistry with lazy resolution and audit logging |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PRESENTATION LAYER                          │
│  CLI · WebChat · Canvas (A2UI) · 13+ Messaging Channels · Voice   │
├─────────────────────────────────────────────────────────────────────┤
│                         AGENT LAYER                                │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │  PAORR Loop │  │ Multi-Agent  │  │  Role Pipeline            │  │
│  │ Plan→Act→   │  │ Orchestrator │  │  PM → Architect → Eng → QA│  │
│  │ Observe→    │  │  (DAG-based) │  │  with RoleMessageBus      │  │
│  │ Reflect→    │  └──────────────┘  └───────────────────────────┘  │
│  │ Retry       │                                                   │
│  └─────────────┘                                                   │
├─────────────────────────────────────────────────────────────────────┤
│                       MIDDLEWARE LAYER                              │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌──────────────────────┐ │
│  │  Hooks   │ │ Security │ │  Context   │ │  Conversation        │ │
│  │ System   │ │ Ensemble │ │  View +    │ │  Local / Remote +    │ │
│  │ 6 events │ │ Defense- │ │  Condenser │ │  StuckDetector +     │ │
│  │ DENY/    │ │ in-Depth │ │  Pipeline  │ │  CancellationToken   │ │
│  │ MODIFY   │ │          │ │            │ │                      │ │
│  └──────────┘ └──────────┘ └───────────┘ └──────────────────────┘ │
│  ┌──────────────────────┐ ┌──────────────────────────────────────┐ │
│  │  Parallel Executor   │ │  Event System                       │ │
│  │  ResourceLockManager │ │  Discriminated Unions · EventLog    │ │
│  │  FIFO Locks          │ │  LLMConvertibleEvent · Streaming    │ │
│  └──────────────────────┘ └──────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────────────┤
│                       INTEGRATION LAYER                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │   LLM    │ │   Git    │ │  Issue   │ │ Project  │ │ Secrets│ │
│  │ 100+     │ │Providers │ │Resolver  │ │  Mgmt    │ │ Fernet │ │
│  │Providers │ │5 platforms│ │LLM-powered│ │Jira·Linear│ │Registry│ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────────────────────┐   │
│  │  File    │ │   MCP    │ │  Observability                   │   │
│  │ Storage  │ │ Protocol │ │  OTEL · Prometheus · Health      │   │
│  │S3·GCS·Lcl│ │ Client+  │ │  Traces · Metrics · Probes       │   │
│  └──────────┘ │  Server  │ └──────────────────────────────────┘   │
│               └──────────┘                                         │
├─────────────────────────────────────────────────────────────────────┤
│                        TOOL LAYER                                  │
│  Bash · Python · Browser · WebSearch · Crawl4AI · ImageGen ·      │
│  StrReplace · Memory · Delegate · Planning · DataViz · AskHuman ·  │
│  PlatformCtrl · NodeExecute · SkillManager · Terminate · Selector  │
├─────────────────────────────────────────────────────────────────────┤
│                      INFRASTRUCTURE LAYER                          │
│  SQLite · SessionDB · Cron · TaskQueue · Sandbox (Docker/SSH/Local)│
└─────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

### 🤖 Agent System

The PAORR loop is the heart of ManusClaw — a self-correcting reasoning cycle that plans, acts, observes, reflects, and retries until the task is complete.

| Feature | Description |
|---|---|
| **PAORR Loop** | Plan → Act → Observe → Reflect → Retry — autonomous self-correction at every step |
| **Multi-Agent Orchestrator** | DAG-based pipeline with topological sorting and parallel stage execution |
| **Role Pipeline** | ProductManager → Architect → Engineer → QA with typed `RoleResult` and `RoleMessageBus` |
| **Agent Router** | Per-channel and per-account routing to specialized agent instances |
| **Identity Guard** | 30+ anti-jailbreak patterns with prompt injection detection |
| **Permission Gate** | AgentMode-based access control (AUTONOMOUS, CONFIRM, RESTRICTED) |
| **Skill Engine** | Auto-injection of domain expertise from Markdown/YAML skill files |
| **PlanningFlow** | Step-by-step task decomposition with dependency tracking |

### 📡 Event System

A type-safe, discriminated-union event system that provides full observability into every agent action.

| Feature | Description |
|---|---|
| **Discriminated Unions** | 17 typed event kinds with `kind` literal discriminators for pattern matching |
| **LLMConvertibleEvent** | Protocol for events that convert to LLM message format (`to_llm_message()`) |
| **File-Backed EventLog** | NDJSON append-only log with O(1) length queries, lazy loading, atomic writes |
| **Crash Safety** | Temp-file-then-rename strategy; count file updated post-write; `reindex()` recovery |
| **StreamingDeltaEvent** | Real-time streaming deltas pushed to UI during LLM generation |
| **TokenEvent** | Per-call token tracking for budget enforcement and cost monitoring |
| **HookExecutionEvent** | Audit trail for every hook invocation with timing and outcome |
| **Condensation Events** | Context window management events with forgotten-event tracking |

### 🛡️ Security Defense-in-Depth

Multi-layer security analysis that combines pattern matching, policy rails, LLM-based analysis, and ensemble fusion.

| Layer | Description |
|---|---|
| **Pattern Analyzer** | Regex-based detection of dangerous commands, path traversal, injection patterns |
| **Policy Rails** | Configurable allow/deny rules for tool arguments and execution contexts |
| **LLM Analyzer** | AI-powered semantic analysis for subtle threats that rules miss |
| **Ensemble Analyzer** | Combines all analyzers with max-severity fusion, crash isolation, and full audit trail |
| **Confirmation Policy** | Human-in-the-loop confirmation for high-risk operations |
| **Secret Redaction** | Automatic detection and masking of secrets in LLM prompts and outputs |
| **Cipher Module** | Fernet-based encryption for sensitive data at rest |

### 🪝 Hooks System

A lifecycle hook system that enables blocking, modification, and observability at every stage of agent execution.

| Event Type | When | Can Block? |
|---|---|---|
| `SESSION_START` | Agent session begins | No |
| `USER_PROMPT_SUBMIT` | Before user prompt enters loop | Yes (DENY/MODIFY) |
| `PRE_TOOL_USE` | Before tool execution | Yes (DENY) |
| `POST_TOOL_USE` | After tool returns | No |
| `STOP` | Agent about to stop | Yes (DENY) |
| `SESSION_END` | Session terminates | No |

**Hook Decisions:**
- `ALLOW` — Proceed without modification
- `DENY` — Block the action with a mandatory reason
- `MODIFY` — Rewrite user prompt content (only for `USER_PROMPT_SUBMIT`)

**Capabilities:** Priority-based execution, per-hook timeout protection, error isolation, aggregate result computation, built-in metrics.

### 🧠 Context Management

Intelligent context window management that prevents overflow while preserving critical information.

| Feature | Description |
|---|---|
| **View System** | Linear event projection with manipulation indices for safe condensation |
| **View Properties** | `BatchAtomicity`, `ObservationUniqueness`, `ToolCallMatching`, `ToolLoopAtomicity` |
| **LLM Summarizing Condenser** | Uses a dedicated condenser LLM to generate summaries of removed events |
| **Rolling Window Condenser** | Keeps the N most recent events, drops the rest |
| **No-op Condenser** | Pass-through for when condensation is disabled |
| **Condensation Pipeline** | Composable pipeline of condensers with metrics and fallback |
| **Progressive Truncation** | If condenser LLM fails, progressively truncates with retry scaling |

### 💬 Conversation System

Robust conversation management with stuck detection, cancellation, and fair concurrency.

| Feature | Description |
|---|---|
| **Local Conversation** | In-process conversation with event log and state machine |
| **Remote Conversation** | Network-backed conversation for distributed deployments |
| **StuckDetector** | 5 detection patterns: repeating actions, action-error loops, monologue, alternating patterns, context overflow |
| **CancellationToken** | Thread-safe cancellation with `raise_if_cancelled()`, timeout support, context manager |
| **FIFOLock** | Fair, starvation-free lock (sync + async variants) guaranteeing FIFO ordering |
| **Conversation State Machine** | IDLE → RUNNING → FINISHED with state-update events |

### ⚡ Parallel Tool Execution

Fine-grained resource locking for safe concurrent tool execution.

| Feature | Description |
|---|---|
| **ResourceLockManager** | Readers-writer locking per resource with deadlock prevention |
| **Declared Resources** | Each tool declares `READ`/`WRITE` resource dependencies |
| **Global Acquisition Ordering** | Prevents deadlock via ordered lock acquisition with timeout |
| **FIFOLock** | Fair lock guaranteeing waiters acquire in request order (sync + async) |
| **Async Executor** | Parallel execution of independent tool calls with resource isolation |

### 🧠 LLM Integration

Comprehensive LLM provider support with enterprise-grade reliability features.

| Feature | Description |
|---|---|
| **100+ Providers** | Via litellm: OpenAI, Anthropic, Google, Mistral, AWS Bedrock, Azure, Groq, Ollama, and more |
| **Streaming** | Real-time delta streaming with `StreamingDeltaEvent` for UI updates |
| **Model Failover** | Configurable failover profiles with automatic provider switching |
| **Credential Pool** | Rotating API key pool with health tracking and automatic demotion |
| **Token Budget** | Per-conversation token tracking with budget enforcement |
| **Secret Redaction** | Automatic masking of API keys and secrets in prompts |
| **Offline Router** | Local model routing for air-gapped deployments |
| **Non-Native Tool Calling** | Emulated tool calling for providers without native support |

### 🔐 Secrets Management

Enterprise secret handling with encryption, lazy resolution, and audit logging.

| Feature | Description |
|---|---|
| **Fernet Encryption** | Symmetric encryption for secrets at rest using cryptography library |
| **SecretRegistry** | Named registry with lazy resolution from backing store |
| **File Secrets Store** | Encrypted file-based storage with namespace support |
| **Namespace Support** | Organize secrets into logical namespaces |
| **Audit Logging** | All secret access is logged with masked values |
| **Thread Safety** | All operations protected by locks for concurrent access |

### 📦 File Storage

Pluggable file storage with multiple backend support.

| Backend | Use Case |
|---|---|
| **Local** | Filesystem storage (default) |
| **S3** | AWS S3 / MinIO compatible |
| **GCS** | Google Cloud Storage |
| **In-Memory** | Testing and ephemeral data |

**Factory auto-detection** from environment variables, config, or explicit parameter.

### 🔀 Git Provider Integrations

Unified interface across five Git hosting platforms.

| Provider | Capabilities |
|---|---|
| **GitHub** | Issues, PRs, comments, branches, file operations, suggested tasks |
| **GitLab** | Issues, MRs, comments, branches, file operations |
| **Azure DevOps** | Work items, PRs, repositories, pipelines |
| **Bitbucket** | Issues, PRs, repositories, pipelines |
| **Forgejo** | Issues, PRs, repositories (self-hosted CodeForge) |

All providers implement a unified `GitProviderService` interface for seamless switching.

### 🎯 Issue & PR Resolution

LLM-powered automated resolution for issues, PRs, and merge conflicts.

| Resolution Type | Description |
|---|---|
| **Issue Resolution** | Analyze reported issue, generate fix, apply changes, post summary |
| **PR Update** | Process review feedback, update code, respond to comments |
| **Merge Conflict Resolution** | Detect conflicts, resolve with LLM assistance, push resolution |

**Features:** Thread-safe execution with per-resolution locking, timeout protection, full audit trail, webhook-triggered resolution, provider-specific prompt templates.

### 📋 Project Management

Integration with popular project management and communication tools.

| Integration | Capabilities |
|---|---|
| **Jira** | Create/update issues, track status, sync with Git providers |
| **Linear** | Create/update issues, project tracking, team workflows |
| **Slack** | Notifications, slash commands, interactive messages |

### 📊 Observability

Production-grade observability with distributed tracing, metrics, and health probes.

| Component | Description |
|---|---|
| **OpenTelemetry Tracing** | `@observe` decorator for sync/async functions, context propagation, custom span attributes |
| **Prometheus Metrics** | Counters, histograms, gauges for LLM calls, tool execution, conversations, tokens, errors |
| **Health Probes** | Kubernetes-style liveness (`/healthz`) and readiness (`/ready`) endpoints |
| **Component Checkers** | Database, LLM provider, and sandbox health monitoring |
| **Correlation IDs** | Distributed correlation ID propagation across service boundaries |
| **Structured Logging** | JSON-structured logs with context enrichment |

### 📨 13+ Messaging Channels

Reach your agents from virtually any platform.

| Channel | Type | Key Feature |
|---|---|---|
| **Telegram** | Bot API | Inline keyboards, file handling |
| **Discord** | Bot | Slash commands, embeds |
| **Slack** | Bolt SDK | Blocks, modals, events |
| **WhatsApp** | Business API | Media messages, templates |
| **Signal** | CLI | End-to-end encrypted |
| **Microsoft Teams** | Bot Framework | Adaptive cards, tabs |
| **Matrix** | Protocol | Federation, E2E encryption |
| **IRC** | Client | Multi-network, channels |
| **Twitch** | Chat | Stream integration |
| **Google Chat** | Webhook | Spaces, threads |
| **WebChat** | Built-in | WebSocket, real-time |
| **Email** | SMTP/IMAP | Send/receive, Gmail Pub/Sub |
| **Gateway** | Unified | Multi-adapter routing |

### 🎤 Voice System

Hands-free interaction with wake word detection and natural conversation.

| Feature | Description |
|---|---|
| **Wake Word Detection** | Pvporcupine or STT-based wake word ("Hey ManusClaw") |
| **Talk Mode** | Continuous mic → STT → agent → TTS conversation loop |
| **Text-to-Speech** | 3 backends: OpenAI TTS, ElevenLabs, System TTS |
| **Speech-to-Text** | 3 engines: OpenAI Whisper, Google STT, Vosk (offline) |

### 🎨 Canvas UI

Live visual output through the Agent-to-UI (A2UI) protocol.

| Feature | Description |
|---|---|
| **A2UI Protocol** | Real-time WebSocket updates from agent to browser |
| **Canvas Server** | Built-in WebSocket server for live rendering |
| **Canvas Tool** | Agent tool for rendering charts, tables, buttons, and custom UI |
| **Mobile Nodes** | Connect mobile devices as canvas nodes |
| **Static HTML** | Standalone canvas.html for quick deployment |

### 🛠️ 18+ Tools

A comprehensive tool arsenal with intelligent selection.

| Tool | Category | Description |
|---|---|---|
| `BashTool` | Execution | Shell command execution with timeout and output capture |
| `PythonExecute` | Execution | Sandboxed Python code execution |
| `BrowserUseTool` | Web | Browser automation with Playwright |
| `WebSearchTool` | Web | Web search with multiple engines |
| `Crawl4AITool` | Web | Intelligent web crawling and extraction |
| `StrReplaceEditor` | File | Precise string replacement in files |
| `ImageGenTool` | Creative | AI image generation |
| `DataVizTool` | Analysis | Data visualization (charts, plots) |
| `MemoryTool` | Knowledge | Cross-session persistent memory |
| `DelegateTool` | Multi-Agent | Delegate subtasks to specialized agents |
| `PlanningTool` | Planning | Task decomposition and step management |
| `AskHumanTool` | Interaction | Request user input during execution |
| `PlatformControl` | System | System-level platform operations |
| `NodeExecute` | Distributed | Execute tasks on remote nodes |
| `SkillManager` | Skills | Load and manage skill files |
| `SelectorTool` | Meta | Heuristic tool selection with scoring |
| `TerminateTool` | Control | Graceful agent termination |
| `CrossSessionSearch` | Knowledge | Search across conversation history |

**Tool Selector** scores each tool per step using failure penalties, recency diversification, and relevance heuristics.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- At least one LLM API key (OpenAI, Anthropic, etc.)

### Installation

```bash
# Clone the repository
git clone https://github.com/The-JDdev/ManusClaw.git
cd ManusClaw

# Install dependencies
pip install -r requirements.txt

# Configure your API key
cp config.toml.example config.toml
# Edit config.toml with your API keys

# Run your first task
python main.py "Create a Python script that generates Fibonacci numbers"
```

### One-Line Install (Linux/macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/The-JDdev/ManusClaw/main/install.sh | bash
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
provider = "openai"
api_key = ""                          # Or set OPENAI_API_KEY env var
max_tokens = 4096
temperature = 0.7
streaming = true

[llm.failover]
enabled = true
profiles = ["gpt-4o", "claude-sonnet-4-20250514", "gemini-2.0-flash"]

[agent]
max_iterations = 50
auto_approve = false                  # Require human confirmation for risky ops
mode = "confirm"                      # autonomous | confirm | restricted

[security]
pattern_analyzer = true
policy_rails = true
llm_analyzer = false                  # Enable for semantic analysis
ensemble = true

[hooks]
enabled = true
timeout_seconds = 30

[context]
condenser = "llm_summarizing"         # llm_summarizing | rolling | noop
max_tokens = 128000

[observability]
tracing = true
metrics = true
health_probes = true

[file_store]
backend = "local"                     # local | s3 | gcs | memory

[secrets]
encryption = true
store = "file"                        # file | env

[git_provider]
default = "github"                    # github | gitlab | azure_devops | bitbucket | forgejo
```

See `config.toml` for the full configuration reference.

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
```

### Health Checks

The server profile includes Kubernetes-style health checks:

```bash
curl http://localhost:8765/healthz   # Liveness probe
curl http://localhost:8765/ready     # Readiness probe
```

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
# Install dev dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ -v

# Run linting
ruff check app/
```

### Code Style

- Python 3.11+ with type hints throughout
- Pydantic models for all data structures
- Thread-safe by default (locks on all shared state)
- Docstrings on every public class and function

---

## 📜 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**ManusClaw v5.1.0** — Built by [The-JDdev (SHS Lab)](https://github.com/The-JDdev)

</div>
