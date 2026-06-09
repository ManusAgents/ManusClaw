# Architecture

## What is ManusClaw

ManusClaw is a production-grade, uncensored, autonomous AI agent ecosystem written entirely in Python. It provides a self-reasoning, self-correcting, multi-agent AI pipeline that can:

- Execute real shell commands (bash, PowerShell, Termux) with a persistent session that holds state across calls
- Run isolated Python code in its own subprocess with 2 GB memory protection, full imports, and full output
- Browse the web via a real Playwright-driven browser (clicks, screenshots, form submission)
- Search the internet via DuckDuckGo + Bing with automatic fallback chain and retry backoff
- Edit any file using a surgical `str_replace_editor` (view, create, overwrite, patch)
- Control external platforms — GitHub, Vercel, WordPress, HuggingFace, Netlify, Discord, Telegram, and any REST API
- Remember across sessions via SQLite-backed audit log and FTS5-indexed long-term RAG memory
- Deploy multi-agent pipelines — ProductManager, Architect, Engineer, QA — DAG-orchestrated with Kahn's topological sort
- Route to any LLM: OpenAI, Anthropic, Google, Ollama, LMStudio, OpenRouter, Groq, Together, GGUF files, HuggingFace

It runs on Linux, macOS, Windows, Docker, and Termux (Android). It ships with a desktop GUI (Flet), a FastAPI WebSocket server, and a CLI command.

---

## Multi-Agent Brain

ManusClaw implements a full DAG-orchestrated multi-agent pipeline modelled on the MetaGPT philosophy. Four specialist roles execute in strict dependency order, each building on the verified output of the last.

### The Four Roles

```
+-------------------+     PRD      +---------------+   Design Plan  +----------------+  Verified Code  +--------------+
|  ProductManager   | -----------> |   Architect   | ------------> |   Engineer     | -------------> |     QA       |
+-------------------+              +---------------+               +----------------+                +--------------+
```

### Role Details

#### ProductManagerRole (`app/agent/roles/product_manager.py`)

The first agent to receive your goal. It produces a structured PRD (Product Requirements Document) with six mandatory sections:

| Section | Content |
|---|---|
| OBJECTIVE | One crisp sentence defining the deliverable |
| IN SCOPE | Bullet list of every feature being built |
| OUT OF SCOPE | Hard boundaries — what will NOT be built |
| ACCEPTANCE CRITERIA | Numbered, measurable conditions for success |
| TECHNICAL CONSTRAINTS | Language, framework, runtime requirements |
| PRIORITY ORDER | Features ranked P0 (critical) -> P1 -> P2 |

Once the PRD is complete, it publishes the document to the async `RoleMessageBus`, which the Architect subscribes to.

#### ArchitectRole (`app/agent/roles/architect.py`)

Receives the PRD from the bus and produces a concrete, actionable system design with six sections:

| Section | Content |
|---|---|
| SYSTEM OVERVIEW | High-level architecture in prose |
| FILE STRUCTURE | Exact directory tree with file purposes |
| COMPONENT MAP | Each component's responsibility and interface |
| DATA FLOW | How data moves between components, step by step |
| TECHNOLOGY STACK | Exact libraries and versions to use |
| IMPLEMENTATION PLAN | DAG task list in `[TASK-N] <action> \| File: <path> \| Deps: [TASK-X, ...]` format |

The implementation plan is a real directed acyclic graph — every task declares its dependencies, and the Engineer executes them in the correct topological order.

#### EngineerRole (`app/agent/roles/engineer.py`)

The Engineer does not just write code. It delegates to a full Manus agent instance with access to the complete tool arsenal: `python_execute`, `bash`, `str_replace_editor`, `browser_use`, `web_search`, and more. Its loop for every task:

```
1. Read the task description from the Architect's plan
2. Choose the correct tool (str_replace_editor -> write, python_execute/bash -> run)
3. Execute the code
4. Verify: does the output match the acceptance criterion?
5. If FAIL: debug, fix, re-run (up to 3 self-correction attempts)
6. Mark COMPLETE only when verified, save artifact to workspace/
```

#### QARole (`app/agent/roles/qa.py`)

Like the Engineer, QA delegates to a full Manus agent so it can actually run tests, not just describe them. Its report format:

```
QA REPORT
---------------------------------
[1] Criterion: <text from PRD>
    Test run: python_execute -> test_feature_x.py
    Result: PASS -- output: "all assertions passed"

[2] Criterion: <text from PRD>
    Test run: bash -> curl http://localhost:8080/health
    Result: FAIL -- HTTP 500, defect in app/server.py:line 42

---------------------------------
Summary: 4 PASS | 1 FAIL | 0 PARTIAL
Verdict: REWORK REQUIRED
Defects: app/server.py:42 -- null pointer in request handler
```

If all P0 criteria pass, the verdict is `APPROVED` and the pipeline terminates cleanly.

### Topological Execution Engine (`app/agent/orchestrator.py`)

The `MultiAgentOrchestrator` uses Kahn's Algorithm to execute roles in valid topological order based on their declared dependency graph:

```python
_DEFAULT_DEPS = {
    "product_manager": [],
    "architect":       ["product_manager"],
    "engineer":        ["architect"],
    "qa":              ["engineer"],
}
```

Custom pipelines can be injected with different roles and dependencies:

```python
from app.agent.orchestrator import MultiAgentOrchestrator

orchestrator = MultiAgentOrchestrator(
    pipeline=["product_manager", "architect", "engineer", "qa"],
    deps={
        "product_manager": [],
        "architect":       ["product_manager"],
        "engineer":        ["architect"],
        "qa":              ["engineer"],
    },
    timeout=7200,
)
result = await orchestrator.run("Build a REST API for a task management system")
```

Every role's output is logged to the same SQLite session, creating a complete, auditable record of the entire pipeline execution. The orchestrator also handles role failures gracefully — if a single role crashes, the error is recorded and the pipeline continues with the next role rather than dying entirely.

### Async RoleMessageBus

Roles communicate via a lightweight publish/subscribe message bus (`app/agent/roles/base_role.py`). Each `RoleMessage` carries:

- `from_role` — the sender
- `to_role` — the recipient (or `"*"` for broadcast)
- `content` — a human-readable summary
- `artefact` — the full output text (PRD, design doc, implementation summary, QA report)

This architecture means roles are fully decoupled. New roles can be added, existing ones replaced, or they can run in parallel (for roles with no dependency edges) without touching any other component.

---

## Tool Arsenal

ManusClaw's Manus agent is loaded with 8 core tools at startup. Every tool is a `BaseTool` subclass with a name, description, JSON Schema parameters, and an async `execute()` method.

### `bash` — Persistent Shell

**File:** `app/tool/bash.py`

A persistent, stateful shell session that survives across multiple calls within an agent run. The same environment, working directory, and shell variables are maintained between calls.

| Feature | Detail |
|---|---|
| Platform | Linux/macOS -> `bash --norc --noprofile` / Windows -> `PowerShell -NoProfile` / Termux -> `bash` |
| Timeout | None by default. Pass `timeout=N` for a hard deadline. |
| Output | Full stdout + stderr, never truncated. Exit code included. |
| Blocked | Only OS-destroying patterns (rm -rf /, fork bombs, dd to block devices) |
| Persistence | Shell state survives across tool calls within a session |

```python
result = await bash.execute(
    command="cd /app && pip install requests && python -c 'import requests; print(requests.__version__)'",
)
result2 = await bash.execute(command="ls -la")  # still in /app
```

### `python_execute` — Isolated Python Subprocess

**File:** `app/tool/python_execute.py`

Runs Python code in a fresh `multiprocessing.Process` — completely isolated from the host Python environment, with a 2 GB virtual memory rlimit applied inside the subprocess.

| Feature | Detail |
|---|---|
| Isolation | Separate OS process — crashes don't kill the agent |
| Memory | 2 GB virtual memory rlimit via `resource.setrlimit` |
| Timeout | None by default. Pass `timeout=N` seconds for a hard cap. |
| Output | 100% of stdout + stderr, zero truncation |
| Imports | All imports permitted — numpy, torch, sklearn, requests, anything |
| Blocked | Fork bombs, direct `/dev/sda` writes |

```python
result = await python_execute.execute(code="""
import torch
import numpy as np

x = torch.randn(1000, 1000)
y = torch.mm(x, x.T)
print(f"Matrix shape: {y.shape}")
print(f"Mean: {y.mean():.4f}")
""")
```

### `web_search` — Multi-Engine Search with Fallback

**File:** `app/tool/web_search.py`

Searches the web through a configurable fallback chain. If DuckDuckGo fails (rate limit, network), it automatically falls back to Bing. Each engine has its own 3-attempt retry with exponential backoff.

```
DuckDuckGo (duckduckgo-search library) -> Bing (aiohttp scraper) -> Error
```

```python
result = await web_search.execute(
    query="Python async best practices 2025",
    max_results=8,
)
```

### `str_replace_editor` — Surgical File Editor

**File:** `app/tool/str_replace_editor.py`

Provides precise file operations: view a file or directory, create new files with content, overwrite files, and str_replace — surgically replace an exact string in a file. All file operations are relative to the workspace.

### `browser_use` — Real Playwright Browser

**File:** `app/tool/browser_use_tool.py`

A full headless Chromium browser driven by Playwright. Can navigate URLs, click elements, fill forms, take screenshots, and extract page content. Used for tasks that require actual browser interaction — login forms, JavaScript-rendered pages, file downloads.

### `crawl` — Clean Web Extraction

**File:** `app/tool/crawl4ai.py`

Uses Crawl4AI to extract clean, structured text from any URL. Strips ads, navigation, and boilerplate. Returns the main content as plain text.

### `ask_human` — Human-in-the-Loop

**File:** `app/tool/ask_human.py`

Pauses execution and asks the user a question directly in the terminal. Used when the agent genuinely needs information it cannot determine itself — API keys, passwords, ambiguous requirements, or final approval before a destructive action.

### `terminate` — Explicit Completion Signal

**File:** `app/tool/terminate.py`

The agent must call this tool to signal task completion. It sets `agent.state = AgentState.FINISHED` and logs the completion reason. An agent that doesn't call terminate before hitting `max_steps` is considered to have hit a timeout, not completed the task.

### PlatformControlTool (`app/tool/platform_control.py`)

The most powerful tool for autonomous external platform management. Give the agent a token and it can create repos, trigger deployments, publish posts, send messages, and run inference.

| Platform | Authentication | Capabilities |
|---|---|---|
| GitHub | `token` (PAT) | Create/delete repos, manage issues, push files, trigger workflows, manage releases |
| Vercel | `token` + optional `team_id` | List/create deployments, manage projects, configure domains |
| WordPress | `site_url` + `username` + `app_password` | Create/edit posts, manage media, update settings |
| HuggingFace | `token` | Run inference, manage models/datasets/Spaces, query the Hub API |
| Netlify | `token` | Manage sites, trigger deploys, configure DNS |
| Discord | `bot_token` | Send messages, manage channels, create webhooks |
| Telegram | `bot_token` | Send messages, manage channels, receive updates |
| Generic REST | `token` (any scheme) | Any API that accepts HTTP — AWS, Stripe, Twilio, custom services |

```python
# GitHub repository management
result = await platform_control.execute(
    platform="github",
    credentials={"token": "ghp_..."},
    method="POST",
    path="/user/repos",
    body={"name": "my-project", "description": "Built by ManusClaw", "private": False},
)

# Deploy to Vercel
result = await platform_control.execute(
    platform="vercel",
    credentials={"token": "vercel_token_...", "team_id": "team_xxx"},
    method="GET",
    path="/v9/projects",
)

# Publish a WordPress post
result = await platform_control.execute(
    platform="wordpress",
    credentials={
        "site_url": "https://myblog.com",
        "username": "admin",
        "app_password": "xxxx xxxx xxxx xxxx",
    },
    method="POST",
    path="/posts",
    body={"title": "ManusClaw: AI That Actually Works", "content": "...", "status": "publish"},
)

# Any REST API (e.g. Stripe)
result = await platform_control.execute(
    platform="generic",
    credentials={
        "base_url": "https://api.stripe.com",
        "token": "sk_live_...",
        "auth_scheme": "Bearer",
    },
    method="GET",
    path="/v1/customers",
    params={"limit": 10},
)
```

The agent can chain these tool calls across a multi-step workflow — for example: write code with `str_replace_editor`, run tests with `python_execute`, push to GitHub with `platform_control`, trigger a Vercel deployment, and send a Telegram notification on completion.

---

## Architecture Deep Dive

### The PAORR Loop

Every single agent step follows the **PAORR** (Plan -> Act -> Observe -> Reflect -> Retry) loop:

```
+------------------------------------------------------------------+
|                         PAORR LOOP                                |
|                                                                   |
|  +-----------+                                                     |
|  |   PLAN    | <- Inject tool intelligence scores into context    |
|  |           |    Score all tools against current sub-goal        |
|  +-----+-----+    Ask LLM: which tool, what args?                |
|        |                                                           |
|  +-----v-----+                                                     |
|  |    ACT    | <- Check permission gate (Tier 1/2/3)              |
|  |           |    Execute tool with retry & backoff               |
|  +-----+-----+    Feed result back into context                   |
|        |                                                           |
|  +-----v-----+                                                     |
|  |  OBSERVE  | <- Record observation in TaskHistory               |
|  |           |    Log tool call to SQLite                         |
|  +-----+-----+    Check for loop patterns                         |
|        |                                                           |
|  +-----v-----+                                                     |
|  |  REFLECT  | <- Did the tool output solve the sub-goal?         |
|  |           |    If yes: move on                                 |
|  +-----+-----+    If no: inject error + ask for self-correction   |
|        |                                                           |
|  +-----v-----+                                                     |
|  |   RETRY   | <- If error: re-score tools (penalize failed)      |
|  |           |    Ask LLM for corrected tool/args                 |
|  +-----+-----+    Exponential backoff (base=1s, max=20s)         |
|        |                                                           |
|        +------------------- next step ----------------------------+
```

### ToolSelector — Adaptive Tool Intelligence

`app/tool/selector.py` implements a heuristic tool scoring engine that runs before every LLM decision:

1. Scores all available tools against the current sub-goal using keyword/semantic matching
2. Penalizes tools that have recently failed in this run
3. Injects the ranked list + rationale as a `TOOL INTELLIGENCE` block into the conversation
4. Tracks `record_use`, `record_success`, `record_failure` to adapt scores across the run

This guides the LLM toward better tool choices without overriding its judgment — the LLM can still deviate from the top-ranked tool but must explain why.

### Loop Detection & Escape

The agent monitors for two types of loops:

**Duplicate Response Loop** — if the last N assistant messages are identical text, an escape prompt is injected forcing a new strategy.

**Tool-Call Loop** — if the `TaskHistory` detects the same tool being called with the same args across 3 recent steps without progress, a hard escape is injected: "Switch to a completely different tool or decomposition strategy."

### Self-Check Every 3 Steps

Every 3 steps, the Manus agent injects a structured self-check prompt:

```
[SELF-CHECK]
1. Which sub-goals are complete? (list them)
2. Which sub-goal are you currently working on?
3. Are you making progress, or repeating the same action?
4. What is your NEXT concrete tool call?
```

This forces the agent to maintain an explicit model of its progress and prevents it from drifting or losing track of the original goal on long-running tasks.
