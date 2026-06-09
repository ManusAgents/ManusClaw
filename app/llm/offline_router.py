"""
Offline / Local LLM Router
Supports:
  - Ollama        (http://localhost:11434)
  - LMStudio      (http://localhost:1234/v1  — OpenAI-compatible)
  - text-gen-webui (http://localhost:5000/v1 — OpenAI-compatible)
  - HuggingFace Inference API / Spaces
  - Direct GGUF   (llama-cpp-python, fully offline, no internet)
"""
from __future__ import annotations
import asyncio
import json
from typing import Iterator, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# GGUF (llama-cpp-python)
# ─────────────────────────────────────────────────────────────────────────────

def _load_gguf(model_path: str, n_ctx: int = 4096, n_gpu_layers: int = 0):
    try:
        from llama_cpp import Llama
    except ImportError:
        raise ImportError(
            "llama-cpp-python not installed.\n"
            "Install: pip install llama-cpp-python\n"
            "GPU:     CMAKE_ARGS='-DLLAMA_CUBLAS=on' pip install llama-cpp-python"
        )
    return Llama(model_path=model_path, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                 verbose=False)


class GGUFRouter:
    """Run a local .gguf file with zero internet dependency."""

    def __init__(self, model_path: str, n_ctx: int = 4096,
                 n_gpu_layers: int = 0):
        self.llm = _load_gguf(model_path, n_ctx, n_gpu_layers)

    def _parse_tool_calls_from_text(self, text: str) -> list:
        """Extract tool calls from text when native tool calling fails."""
        import re
        tool_calls = []
        # Pattern: {"name": "tool_name", "arguments": {...}}
        pattern = r'\{"name":\s*"(\w+)",\s*"arguments":\s*(\{[^}]*\})\}'
        for match in re.finditer(pattern, text):
            name = match.group(1)
            try:
                args = json.loads(match.group(2))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                "id": f"gguf-{name}-{int(asyncio.get_event_loop().time())}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            })
        return tool_calls

    async def chat(self, messages: List[dict], max_tokens: int = 2048,
                    temperature: float = 0.7, tools=None, **_) -> dict:
        """Async wrapper that returns OpenAI-format dict."""
        kwargs = dict(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = tools
        resp = await asyncio.to_thread(
            self.llm.create_chat_completion,
            **kwargs,
        )
        # If the model returned content instead of tool_calls, try to parse tool calls from text
        msg = resp.get("choices", [{}])[0].get("message", {})
        content = msg.get("content") or ""
        if tools and not msg.get("tool_calls") and content:
            parsed = self._parse_tool_calls_from_text(content)
            if parsed:
                resp["choices"][0]["message"]["tool_calls"] = parsed
                resp["choices"][0]["message"]["content"] = None
        return resp  # already OpenAI-format dict

    def stream(self, messages: List[dict], max_tokens: int = 2048,
               temperature: float = 0.7, **_) -> Iterator[str]:
        for chunk in self.llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        ):
            delta = chunk["choices"][0]["delta"]
            if "content" in delta:
                yield delta["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Ollama
# ─────────────────────────────────────────────────────────────────────────────

class OllamaRouter:
    """
    Direct Ollama integration using the official `ollama` SDK.
    Supports both local Ollama and Ollama Cloud.
    """

    def __init__(self, model: str = "llama3.2:3b",
                 base_url: str = "http://localhost:11434"):
        self.model = model
        self.base = base_url.rstrip("/")
        try:
            from ollama import AsyncClient
            import os
            self._has_sdk = True
            
            headers = None
            api_key = os.environ.get('OLLAMA_API_KEY')
            if api_key:
                headers = {'Authorization': f'Bearer {api_key}'}
            
            self.client = AsyncClient(host=self.base, headers=headers)
        except ImportError:
            self._has_sdk = False
            self.client = None

    async def chat(self, messages: List[dict], tools: Optional[list] = None, max_tokens: int = 2048,
                    temperature: float = 0.7, **_) -> dict:
        """Async Ollama chat returning OpenAI-format dict."""
        if not self._has_sdk:
            raise ImportError("The 'ollama' SDK is required. Run: pip install ollama")

        options = {"num_predict": max_tokens, "temperature": temperature}
        kwargs = {
            "model": self.model,
            "messages": messages,
            "options": options,
            "stream": False
        }
        if tools:
            kwargs["tools"] = tools

        resp = await self.client.chat(**kwargs)
        
        msg = resp.get("message", {})
        
        tool_calls = []
        if "tool_calls" in msg and msg["tool_calls"]:
            import time, json
            for i, tc in enumerate(msg["tool_calls"]):
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                tool_calls.append({
                    "id": f"ollama-{fn.get('name', 'fn')}-{int(time.time())}-{i}",
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": json.dumps(args) if isinstance(args, dict) else args
                    }
                })
        
        return {
            "choices": [{
                "message": {
                    "role": msg.get("role", "assistant"),
                    "content": msg.get("content") or None,
                    "tool_calls": tool_calls or None,
                }
            }]
        }


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible (LMStudio, text-gen-webui, Groq, Together, etc.)
# ─────────────────────────────────────────────────────────────────────────────

class OpenAICompatRouter:
    """
    Generic OpenAI-compatible endpoint.
    LMStudio:      base_url="http://localhost:1234/v1",  api_key="none"
    text-gen-webui:base_url="http://localhost:5000/v1",  api_key="none"
    Groq:          base_url="https://api.groq.com/openai/v1", api_key=<key>
    OpenRouter:    base_url="https://openrouter.ai/api/v1",   api_key=<key>
    """

    def __init__(self, model: str, base_url: str,
                 api_key: str = "none", timeout: Optional[int] = None):
        self.model = model
        self.base = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout

    async def chat(self, messages: List[dict], max_tokens: int = 2048,
                   temperature: float = 0.7, **_) -> dict:
        """Async OpenAI-compatible chat returning OpenAI-format dict."""
        payload = {
            "model": self.model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        try:
            import httpx
            r = await asyncio.to_thread(
                httpx.post,
                f"{self.base}/chat/completions",
                headers=self.headers,
                json=payload,
                # FIX: use json= (not data=) so httpx serialises correctly
                timeout=self.timeout,
            )
            return r.json()
        except ImportError:
            import urllib.request, json as _j
            data = _j.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self.base}/chat/completions",
                data=data, headers=self.headers, method="POST"
            )
            resp = await asyncio.to_thread(
                urllib.request.urlopen, req, timeout=self.timeout
            )
            body = resp.read()
            return _j.loads(body)


# ─────────────────────────────────────────────────────────────────────────────
# HuggingFace Inference API / Spaces
# ─────────────────────────────────────────────────────────────────────────────

class HuggingFaceRouter:
    """
    HuggingFace Inference API (serverless) or Inference Endpoints (dedicated).
    model examples:
      "meta-llama/Meta-Llama-3-8B-Instruct"
      "HuggingFaceH4/zephyr-7b-beta"
    Or a Spaces URL: "https://your-space.hf.space"
    """

    def __init__(self, model: str, hf_token: Optional[str] = None,
                 endpoint_url: Optional[str] = None):
        self.token = hf_token or ""
        self.headers = {"Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json"}
        if endpoint_url:
            self.url = endpoint_url.rstrip("/") + "/v1/chat/completions"
        else:
            self.url = (
                f"https://api-inference.huggingface.co/models/{model}"
                "/v1/chat/completions"
            )

    async def chat(self, messages: List[dict], max_tokens: int = 1024,
                   temperature: float = 0.7, **_) -> dict:
        """Async HuggingFace chat returning OpenAI-format dict."""
        payload = {
            "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            import httpx
            r = await asyncio.to_thread(
                httpx.post, self.url, headers=self.headers,
                json=payload, timeout=120,  # FIX: json= not data=
            )
            return r.json()
        except ImportError:
            import urllib.request, json as _j
            data = _j.dumps(payload).encode()
            req = urllib.request.Request(
                self.url, data=data, headers=self.headers, method="POST"
            )
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=120)
            body = resp.read()
            return _j.loads(body)


# ─────────────────────────────────────────────────────────────────────────────
# Universal factory
# ─────────────────────────────────────────────────────────────────────────────

def build_offline_router(config: dict):
    """
    Build the right offline router from config dict.

    GGUF (fully offline):
        {"provider": "gguf", "model_path": "/path/to/model.gguf",
         "n_ctx": 4096, "n_gpu_layers": 0}

    Ollama:
        {"provider": "ollama", "model": "llama3.2:3b",
         "base_url": "http://localhost:11434"}

    LMStudio / text-gen-webui / any OpenAI-compat:
        {"provider": "openai_compat", "model": "local-model",
         "base_url": "http://localhost:1234/v1", "api_key": "none"}

    HuggingFace:
        {"provider": "huggingface",
         "model": "HuggingFaceH4/zephyr-7b-beta",
         "hf_token": "hf_xxx"}
    """
    p = config.get("provider", "").lower()
    if p == "gguf":
        return GGUFRouter(
            model_path=config["model_path"],
            n_ctx=config.get("n_ctx", 4096),
            n_gpu_layers=config.get("n_gpu_layers", 0),
        )
    if p == "ollama":
        return OllamaRouter(
            model=config.get("model", "llama3.2:3b"),
            base_url=config.get("base_url", "http://localhost:11434"),
        )
    if p in ("openai_compat", "lmstudio", "textgen", "groq", "openrouter"):
        return OpenAICompatRouter(
            model=config.get("model", "local-model"),
            base_url=config.get("base_url", "http://localhost:1234/v1"),
            api_key=config.get("api_key", "none"),
            timeout=config.get("timeout"),
        )
    if p in ("huggingface", "hf"):
        return HuggingFaceRouter(
            model=config.get("model", ""),
            hf_token=config.get("hf_token") or config.get("api_key", ""),
            endpoint_url=config.get("endpoint_url"),
        )
    raise ValueError(
        f"Unknown offline provider '{p}'. "
        "Valid: gguf | ollama | openai_compat | huggingface"
    )
