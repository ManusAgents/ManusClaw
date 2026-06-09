# Provider Templates

This directory contains configuration templates for various AI providers supported by **ManusClaw**.

## How to Use Provider Templates

### Option 1 — Direct copy to `config.toml`

Copy a template directly to the project root:

```bash
cp providers/ollama.toml config.toml
```

### Option 2 — Use profiles

Move the template to your profiles directory and switch via an environment variable:

```bash
mkdir -p ~/.manusclaw/profiles/ollama
cp providers/ollama.toml ~/.manusclaw/profiles/ollama/config.toml
MANUSCLAW_PROFILE=ollama manusclaw "Your task"
```

---

## Provider Templates

### Ollama (Free / Local)

Runs models locally via [Ollama](https://ollama.com). Ensure `ollama serve` is running on port 11434.

```toml
[llm]
provider = "ollama"
base_url = "http://localhost:11434/v1"
api_key = "none"
model = "llama3.2:3b"
max_tokens = 4096
temperature = 0.0
```

- **Provider value:** `"ollama"` — uses the native `OllamaRouter` (offline router).
- **Model:** Any model you have pulled locally (e.g. `llama3.2:3b`, `mistral`, `qwen2.5`).
- **API key:** Not required; set to `"none"`.

---

### Ollama Cloud (API)

Paid cloud API from [ollama.com](https://ollama.com). Uses an OpenAI-compatible endpoint.

```toml
[llm]
provider = "ollama"
base_url = "https://ollama.com/v1"
api_key = "YOUR-OLLAMA-API-KEY"
model = "gpt-oss:120b"
max_tokens = 4096
temperature = 0.0
```

- **Provider value:** `"ollama"` — treated as the native Ollama router (uses `OllamaRouter`).
- **API key:** Obtain from your [ollama.com](https://ollama.com) account.
- **Model:** Cloud-hosted models like `gpt-oss:120b`.

---

### OpenRouter (Paid / API)

Access 200+ models (Claude, GPT-4, Gemini, etc.) through a single API key at [openrouter.ai](https://openrouter.ai).

```toml
[llm]
provider = "universal"
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-or-v1-YOUR-OPENROUTER-KEY"
model = "anthropic/claude-3.5-sonnet"
max_tokens = 8192
temperature = 0.0

[llm.extra_headers]
"HTTP-Referer" = "https://github.com/The-JDdev/ManusClaw"
"X-Title" = "ManusClaw"
```

- **Provider value:** `"universal"` — uses `UniversalClient` (OpenAI-compatible SDK call).
- **Extra headers:** `HTTP-Referer` and `X-Title` are required by OpenRouter for ranking.
- **Model:** Use OpenRouter model slugs (e.g. `anthropic/claude-3.5-sonnet`, `openai/gpt-4o`).

---

### 7LLM (Paid / API)

Standard OpenAI-compatible API from [7llm.com](https://7llm.com).

```toml
[llm]
provider = "universal"
base_url = "https://api.7llm.com/v1"
api_key = "YOUR-7LLM-API-KEY"
model = "gpt-4o"
max_tokens = 4096
temperature = 0.0
```

- **Provider value:** `"universal"` — routed through `UniversalClient`.
- **API key:** Obtain from your 7LLM account.

---

### Pollinations (Free / API)

Free OpenAI-compatible text generation API at [pollinations.ai](https://pollinations.ai). No API key required.

```toml
[llm]
provider = "universal"
base_url = "https://text.pollinations.ai/openai"
api_key = "none"
model = "openai"
max_tokens = 4096
temperature = 0.0
```

- **Provider value:** `"universal"` — routed through `UniversalClient`.
- **API key:** Not required; set to `"none"`.
- **Model:** Can be `openai`, `mistral`, `llama`, etc. depending on Pollinations endpoints.

---

### opencode (Native Provider / Free)

OpenAI-compatible endpoint powered by [opencode.ai](https://opencode.ai). Uses a public API key — no sign-up needed.

```toml
[llm]
provider = "openai"
base_url = "https://opencode.ai/zen/v1"
api_key = "public"
model = "deepseek-v4-flash-free"
max_tokens = 4096
temperature = 0.0
```

- **Provider value:** `"openai"` — uses the native `OpenAIClient` SDK wrapper.
- **API key:** `"public"` — no personal key required.
- **Model:** `deepseek-v4-flash-free` (server-configured; other models may be available).

---

## How the LLM Router Selects Providers

The `LLM._build_backend()` method in `app/llm/llm.py:539` routes requests based on the `[llm] provider` field:

| `provider` value | Class used | Notes |
|---|---|---|
| `"mock"` | `MockLLM` | No-op for testing. |
| `"gguf"` | `GGUFRouter` | Local GGUF model file. |
| `"ollama"` | `OllamaRouter` | Local or cloud Ollama instance. |
| `"huggingface"`, `"hf"` | `HuggingFaceRouter` | Hugging Face inference. |
| `"universal"`, `"openrouter"`, `"lmstudio"`, `"openai-compat"`, `"groq"`, `"together"`, `"perplexity"` | `UniversalClient` | **Universal endpoint** — sends OpenAI-compatible HTTP requests to `base_url`. |
| `"openai"` | `OpenAIClient` | **Official SDK** — uses the `openai` Python package. |
| `"anthropic"` | `AnthropicClient` | **Official SDK** — uses the `anthropic` Python package. |
| `"google"`, `"gemini"` | `GoogleClient` | **Official SDK** — uses the `google-generativeai` package. |
| `"mistral"` | `MistralClient` | **Official SDK** — uses the `mistralai` package. |
| `"bedrock"` | `BedrockClient` | AWS Bedrock via `boto3`. |
| *(any other with `base_url` set)* | `UniversalClient` | Fallback universal endpoint. |

### Official SDK vs Universal Endpoint

- **Official SDK** providers (`openai`, `anthropic`, `google`/`gemini`, `mistral`, `bedrock`) use the provider's native Python package. They handle authentication, retries, and streaming via the vendor's official client library.
- **Universal endpoint** (`provider = "universal"`) sends raw HTTP requests to any OpenAI-compatible `base_url`. This works with any provider that exposes an OpenAI-compatible REST API (OpenRouter, 7LLM, Pollinations, LM Studio, Groq, Together, Perplexity, etc.).
- If a `base_url` is set but the provider is empty or unrecognized and has no dedicated SDK handler, the router also falls back to `UniversalClient`.
