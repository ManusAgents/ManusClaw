# Configuration

All configuration lives in `config.toml`. Environment variables override config values for secrets.

---

## Named Config Profiles

```bash
MANUSCLAW_PROFILE=work python -m app.cli
# Loads: ~/.manusclaw/profiles/work/config.yaml + .env
```

---

## LLM Provider Configuration

ManusClaw implements a dual-mode LLM router that works with every LLM provider — cloud or local, paid or free, online or completely air-gapped.

### Official Provider SDKs

Set `provider` in `config.toml` to use the official SDK for that provider.

| Provider | `provider` value | SDK | Key env var |
|---|---|---|---|
| OpenAI (GPT-4o, o1, etc.) | `openai` | `openai` Python SDK | `OPENAI_API_KEY` |
| Anthropic (Claude 3.5, 3 Opus) | `anthropic` | `anthropic` Python SDK | `ANTHROPIC_API_KEY` |
| Google (Gemini 1.5 Pro, Flash) | `google` or `gemini` | `google-generativeai` SDK | `GOOGLE_API_KEY` |
| No provider (zero-credential test) | `mock` | Built-in MockLLM | _(none required)_ |

```toml
# config.toml — Official OpenAI
[llm]
provider    = "openai"
model       = "gpt-4o"
max_tokens  = 4096
temperature = 0.0
# api_key set via OPENAI_API_KEY env var
```

```toml
# config.toml — Anthropic Claude
[llm]
provider    = "anthropic"
model       = "claude-3-5-sonnet-20241022"
max_tokens  = 8192
temperature = 0.0
```

### Universal / Hacker Mode (OpenRouter, Groq, Together, any proxy)

If you set `base_url`, ManusClaw switches to Universal/Agnostic mode — it sends standard OpenAI-compatible HTTP requests and works with any endpoint that speaks the OpenAI chat completions protocol.

```toml
# config.toml — OpenRouter (200+ models, one API key)
[llm]
provider   = "openrouter"
base_url   = "https://openrouter.ai/api/v1"
api_key    = "sk-or-v1-..."
model      = "anthropic/claude-3.5-sonnet"
max_tokens = 8192

[llm.extra_headers]
"HTTP-Referer" = "https://github.com/The-JDdev/ManusClaw"
"X-Title"      = "ManusClaw"
```

```toml
# config.toml — Groq (ultra-fast inference, free tier)
[llm]
base_url  = "https://api.groq.com/openai/v1"
api_key   = "gsk_..."
model     = "llama-3.3-70b-versatile"
```

```toml
# config.toml — Together AI
[llm]
base_url  = "https://api.together.xyz/v1"
api_key   = "..."
model     = "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"
```

The `extra_headers` field is critical for OpenRouter compliance — it passes the `HTTP-Referer` and `X-Title` headers that OpenRouter requires for rate limit tracking.

### Fully Offline Local LLM

Run ManusClaw with zero internet dependency using locally hosted models.

#### Ollama (Recommended for offline)

```toml
# config.toml — Ollama
[llm]
provider  = "ollama"
base_url  = "http://localhost:11434/v1"
api_key   = "none"
model     = "llama3.2:3b"
```

```bash
# Pull a model first
ollama pull llama3.2:3b        # 3 GB — fast, works on 8 GB RAM
ollama pull llama3.1:8b        # 6 GB — better reasoning
ollama pull deepseek-r1:7b     # 7 GB — strong at code
ollama pull qwen2.5-coder:7b   # 7 GB — best for coding tasks
```

#### LM Studio (GUI-friendly offline)

```toml
# config.toml — LM Studio
[llm]
base_url  = "http://localhost:1234/v1"
api_key   = "none"
model     = "local-model"
```

#### Direct GGUF — Fully Air-Gapped (llama-cpp-python)

```bash
pip install llama-cpp-python
# GPU acceleration (NVIDIA):
CMAKE_ARGS="-DLLAMA_CUBLAS=on" pip install llama-cpp-python --force-reinstall
```

```python
from app.llm.offline_router import GGUFRouter

router = GGUFRouter(
    model_path="/path/to/llama-3.2-3b-instruct.Q4_K_M.gguf",
    n_ctx=4096,
    n_gpu_layers=35,  # 0 = CPU only, 35+ = GPU offload
)
response = router.chat([{"role": "user", "content": "Hello"}])
```

Recommended GGUF models by size:

| Model | Size | RAM Required | Use Case |
|---|---|---|---|
| `llama-3.2-3b-instruct.Q4_K_M.gguf` | ~2 GB | 4 GB | Fast, mobile-friendly |
| `llama-3.1-8b-instruct.Q4_K_M.gguf` | ~5 GB | 8 GB | Balanced |
| `deepseek-coder-v2-lite.Q4_K_M.gguf` | ~9 GB | 12 GB | Best for code |
| `qwen2.5-72b-instruct.Q4_K_M.gguf` | ~40 GB | 48 GB | Maximum intelligence |

#### HuggingFace Inference API / Spaces

```toml
# config.toml — HuggingFace Inference API
[llm]
provider  = "huggingface"
model     = "meta-llama/Meta-Llama-3-8B-Instruct"
api_key   = "hf_..."
```

```python
from app.llm.offline_router import HuggingFaceRouter

router = HuggingFaceRouter(
    model="HuggingFaceH4/zephyr-7b-beta",
    hf_token="hf_...",
    endpoint_url="https://your-endpoint.endpoints.huggingface.cloud",
)
```

### LLM Retry & Rate-Limit Handling

The LLM layer implements an 8-attempt exponential backoff with jitter:

```
Attempt 1 -> immediate
Attempt 2 -> wait ~1.0s
Attempt 3 -> wait ~2.0s
Attempt 4 -> wait ~4.0s
...
Attempt 8 -> wait up to 60s
```

Rate limit errors (`429`) trigger the backoff. `TokenLimitExceeded` errors propagate immediately (no point retrying with the same context). All other errors retry up to the limit before propagating.

---

## Full Configuration Reference

```toml
# ─────────────────────────────────────────────────────────
# ManusClaw Configuration — config.toml
# ─────────────────────────────────────────────────────────

[llm]
provider     = "mock"         # mock | openai | anthropic | google | openrouter | ollama | universal
model        = "gpt-4o"       # model name for the selected provider
base_url     = ""             # set this to enable Universal/Agnostic mode
api_key      = ""             # prefer env var: OPENAI_API_KEY or ANTHROPIC_API_KEY
max_tokens   = 4096           # max response tokens
temperature  = 0.0            # 0.0 = deterministic, 1.0 = creative
max_retries  = 6              # LLM retry attempts on failure
timeout      = 120            # seconds before LLM request times out

[llm.extra_headers]           # extra HTTP headers (required for OpenRouter)
# "HTTP-Referer" = "https://github.com/The-JDdev/ManusClaw"
# "X-Title"      = "ManusClaw"

[browser]
headless           = true     # run browser without visible window
disable_security   = false    # disable browser sandbox (use in Docker only)
max_content_length = 10000    # max chars extracted from a page

[search]
engines     = ["duckduckgo", "bing"]   # search engine fallback chain
max_results = 10                        # max results per search

[sandbox]
enabled      = false           # enable Docker sandbox for code execution
docker_image = "python:3.11-slim"
memory_limit = "256m"
timeout      = 30

[runflow]
enable_data_analysis = false  # enable DataAnalysisAgent in flow pipelines
timeout              = 3600   # max seconds for a run_flow session

workspace_dir = "workspace"   # where all agent outputs are saved
max_steps     = 30            # max steps per agent run (prevents infinite loops)

# MCP Server definitions (optional)
# [[mcp_servers]]
# name      = "my-server"
# transport = "stdio"
# command   = "node"
# args      = ["path/to/mcp-server.js"]
```

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key (auto-loaded into `llm.api_key`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (auto-loaded into `llm.api_key`) |
| `GOOGLE_API_KEY` | Google Gemini API key |
| `LLM_BASE_URL` | Override `llm.base_url` at runtime |
| `MANUSCLAW_API_KEY` | Enables API Key authentication on the server |
| `MANUSCLAW_ALLOWED_ORIGINS` | CORS allowed origins (comma-separated) |
| `MANUSCLAW_PROFILE` | Named config profile to load |
