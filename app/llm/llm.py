from __future__ import annotations

"""
Universal LLM Router — Multi-Provider with Credential Pool & Token Tracking
============================================================================

Providers: openai | anthropic | google | mistral | bedrock | mock | universal
Credential Pool: multiple API keys per provider with priority ordering + auto-rotation
Token Budget: tracks input/output/cache/reasoning tokens per session + grace call
Secret Redaction: optionally scrub keys from log output
"""

import asyncio
import random
import time
from typing import Any, Optional

from app.config import Config
from app.exceptions import RateLimitError, TokenLimitExceeded
from app.logger import logger
from app.schema import Message, Role, ToolCall, Function
from app.llm.token_tracker import TokenBudget, TokenUsage
from app.llm.credential_pool import CredentialPool, build_pool_from_config

MAX_RETRIES     = 8
RETRY_BASE_WAIT = 1.0
RETRY_MAX_WAIT  = 60.0

# ──────────────────────────────────────────────────────────────────────────────
# Long-wait intelligent response handling
# ──────────────────────────────────────────────────────────────────────────────
# Deep-thinking models (DeepSeek R1, o1, o3, etc.) can take 5-20+ minutes to
# generate a response. We must NOT timeout prematurely. The system should wait
# patiently and NOT spam repeated requests.
#
# Strategy:
#   1. Default timeout raised to 30 minutes (1800s) for HTTP-level waits
#   2. Adaptive timeout: detect long-thinking models and extend automatically
#   3. Retry on transient errors ONLY (rate limits, 5xx) — never re-send on timeout
#   4. Progress heartbeat: log periodic status during long waits
# ──────────────────────────────────────────────────────────────────────────────

# Models known to require extended thinking time
LONG_THINKING_MODELS = {
    "deepseek-r1", "deepseek-reasoner", "deepseek-r1-",
    "o1", "o1-mini", "o1-pro", "o1-preview",
    "o3", "o3-mini", "o3-pro",
    "claude-3-7-sonnet", "claude-sonnet-4",
}

DEFAULT_TIMEOUT_LONG    = 1800   # 30 minutes — safe for deep-reasoning models
DEFAULT_TIMEOUT_SHORT   = 600    # 10 minutes — for regular models
PROGRESS_HEARTBEAT_SEC  = 30     # Log a heartbeat every 30s during long waits


def _is_long_thinking_model(model: str) -> bool:
    """Check if the model is known to require extended thinking time."""
    model_lower = (model or "").lower()
    for pattern in LONG_THINKING_MODELS:
        if pattern in model_lower:
            return True
    # Heuristic: any model with 'reason' or 'think' in name likely needs more time
    if any(kw in model_lower for kw in ("reason", "think", "r1")):
        return True
    return False


def _get_adaptive_timeout(model: str, configured_timeout: int) -> int:
    """Return the effective timeout based on model type.

    For long-thinking models, we use a minimum of 30 minutes regardless of
    the configured timeout (unless the user explicitly set an even higher value).
    For regular models, we respect the configured value with a floor of 10 minutes.
    """
    if _is_long_thinking_model(model):
        return max(configured_timeout, DEFAULT_TIMEOUT_LONG)
    return max(configured_timeout, 300)  # At least 5 minutes even for regular models


def _msg_from_openai(choice: dict) -> Message:
    msg = choice.get("message", {})
    role = Role(msg.get("role", "assistant"))
    content = msg.get("content")
    raw_tcs = msg.get("tool_calls") or []
    tool_calls = [
        ToolCall(
            id=tc["id"],
            type=tc.get("type", "function"),
            function=Function(
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments", "{}"),
            ),
        )
        for tc in raw_tcs
    ] or None
    return Message(role=role, content=content, tool_calls=tool_calls)


class MockLLM:
    def __init__(self) -> None:
        self._call_count = 0
        self.token_budget = TokenBudget(max_tokens=0)

    async def ask(self, messages: list, **_: Any) -> Message:
        self._call_count += 1
        if self._call_count <= 1:
            return Message(
                role=Role.ASSISTANT,
                content="[MockLLM] Running Python hello-world.",
                tool_calls=[
                    ToolCall(
                        id="mock-tc-1",
                        type="function",
                        function=Function(
                            name="python_execute",
                            arguments='{"code": "print(\\"Hello from ManusClaw!\\")"}',
                        ),
                    )
                ],
            )
        return Message(
            role=Role.ASSISTANT,
            content="Task complete.",
            tool_calls=[
                ToolCall(
                    id="mock-tc-2",
                    type="function",
                    function=Function(name="terminate", arguments='{"reason": "Completed by MockLLM."}'),
                )
            ],
        )

    async def ask_tool(self, messages: list, tools: list, **_: Any) -> Message:
        return await self.ask(messages)

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   **_: Any) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count <= 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "[MockLLM] Running Python hello-world.",
                        "tool_calls": [{
                            "id": "mock-tc-1",
                            "type": "function",
                            "function": {
                                "name": "python_execute",
                                "arguments": '{"code": "print(\\"Hello from ManusClaw!\\")"}'
                            }
                        }]
                    }
                }]
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Task complete.",
                    "tool_calls": [{
                        "id": "mock-tc-2",
                        "type": "function",
                        "function": {
                            "name": "terminate",
                            "arguments": '{"reason": "Completed by MockLLM."}'
                        }
                    }]
                }
            }]
        }


class UniversalClient:
    def __init__(self, base_url: str, api_key: str, model: str, **kwargs: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = kwargs.get("max_tokens", 8192)
        self.temperature = kwargs.get("temperature", 0.0)
        self._extra_headers: dict[str, str] = kwargs.get("extra_headers", {})
        # FIX: Use adaptive timeout — long-thinking models need much more time
        configured_timeout = kwargs.get("timeout", 300)
        self._timeout_seconds = _get_adaptive_timeout(model, configured_timeout)
        logger.info(
            f"[UniversalClient] model={model} timeout={self._timeout_seconds}s "
            f"(long_thinking={_is_long_thinking_model(model)})"
        )

    async def _post(self, payload: dict[str, Any], api_key: Optional[str] = None) -> dict[str, Any]:
        import aiohttp
        key = api_key or self.api_key
        headers: dict[str, str] = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        url = f"{self.base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # FIX: Long-wait progress monitoring — log heartbeats during long waits
            logger.info(f"[UniversalClient] Sending request to {url} (timeout={self._timeout_seconds}s)")
            start_time = asyncio.get_event_loop().time()
            async with session.post(url, json=payload, headers=headers) as resp:
                elapsed = asyncio.get_event_loop().time() - start_time
                logger.info(f"[UniversalClient] Response received after {elapsed:.1f}s (status={resp.status})")
                if resp.status == 429:
                    raise RateLimitError("Rate limited")
                if resp.status == 400:
                    body = await resp.text()
                    if "context" in body.lower() or "token" in body.lower():
                        raise TokenLimitExceeded(body)
                    raise ValueError(f"Bad request: {body}")
                resp.raise_for_status()
                return await resp.json()

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   api_key: Optional[str] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return await self._post(payload, api_key=api_key)


class OpenAIClient:
    def __init__(self, cfg: Any) -> None:
        from openai import AsyncOpenAI
        # FIX: Use adaptive timeout — long-thinking models need much more time
        configured_timeout = getattr(cfg, 'timeout', 300) or 300
        timeout_val = _get_adaptive_timeout(cfg.model, configured_timeout)
        self._c = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=timeout_val)
        self.model: str = cfg.model
        self.max_tokens: int = cfg.max_tokens
        self.temperature: float = cfg.temperature
        logger.info(
            f"[OpenAIClient] model={cfg.model} timeout={timeout_val}s "
            f"(long_thinking={_is_long_thinking_model(cfg.model)})"
        )

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   **_: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=self.model, messages=messages,
            max_tokens=self.max_tokens, temperature=self.temperature,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._c.chat.completions.create(**kwargs)
        return resp.model_dump()


class AnthropicClient:
    def __init__(self, cfg: Any) -> None:
        from anthropic import AsyncAnthropic
        # FIX: Use adaptive timeout for long-thinking Claude models
        configured_timeout = getattr(cfg, 'timeout', 300) or 300
        timeout_val = _get_adaptive_timeout(cfg.model, configured_timeout)
        self._c = AsyncAnthropic(api_key=cfg.api_key, timeout=timeout_val)
        self.model: str = cfg.model
        self.max_tokens: int = cfg.max_tokens
        self.temperature: float = cfg.temperature
        logger.info(
            f"[AnthropicClient] model={cfg.model} timeout={timeout_val}s "
            f"(long_thinking={_is_long_thinking_model(cfg.model)})"
        )

    @staticmethod
    def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
        """
        FIX: Convert OpenAI-format messages to Anthropic API format.
        - OpenAI role='tool' → Anthropic role='user' with tool_result content block
        - OpenAI assistant with tool_calls → Anthropic assistant with tool_use blocks
        - Consecutive same-role messages are merged (Anthropic requires alternating)
        """
        import json as _json
        converted: list[dict] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")
            if role == "system":
                continue
            if role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": str(content)})
                    for tc in tool_calls:
                        try:
                            inp = _json.loads(tc["function"].get("arguments") or "{}")
                        except Exception:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": tc["function"].get("name", ""),
                            "input": inp,
                        })
                    converted.append({"role": "assistant", "content": blocks})
                else:
                    converted.append({"role": "assistant", "content": content or ""})
            elif role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": str(content or ""),
                    }],
                })
            else:
                converted.append({"role": "user", "content": content or ""})
        # Merge consecutive same-role messages (Anthropic requires alternating)
        merged: list[dict] = []
        for msg in converted:
            if not merged:
                merged.append({"role": msg["role"], "content": msg["content"]})
                continue
            prev = merged[-1]
            if prev["role"] == msg["role"]:
                pc, nc = prev["content"], msg["content"]
                if isinstance(pc, list) and isinstance(nc, list):
                    pc.extend(nc)
                elif isinstance(pc, list):
                    pc.append({"type": "text", "text": str(nc)})
                elif isinstance(nc, list):
                    merged[-1] = {"role": msg["role"], "content": [{"type": "text", "text": str(pc)}, *nc]}
                else:
                    prev["content"] = str(pc) + "\n" + str(nc)
            else:
                merged.append({"role": msg["role"], "content": msg["content"]})
        return merged

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   api_key: Optional[str] = None) -> dict[str, Any]:
        import json
        from anthropic import AsyncAnthropic
        # FIX: Support credential pool key rotation
        client = AsyncAnthropic(api_key=api_key) if api_key else self._c
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        conv = [m for m in messages if m["role"] != "system"]
        # FIX: Convert OpenAI-format messages to Anthropic format
        anthropic_messages = self._to_anthropic_messages(conv)
        # FIX: Extended thinking models require temperature=1 (not arbitrary values)
        is_extended_thinking = "claude-3-7" in self.model or "claude-sonnet-4" in self.model
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=self.max_tokens,
            messages=anthropic_messages,
        )
        if not is_extended_thinking:
            kwargs["temperature"] = self.temperature
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {}),
                }
                for t in tools
            ]
        resp = await client.messages.create(**kwargs)
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                })
        usage: dict[str, Any] = {}
        if hasattr(resp, "usage") and resp.usage:
            usage = {
                "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                "completion_tokens": getattr(resp.usage, "output_tokens", 0),
                "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "\n".join(content_parts) or None,
                    "tool_calls": tool_calls or None,
                }
            }],
            "usage": usage,
        }


class GoogleClient:
    def __init__(self, cfg: Any) -> None:
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("pip install google-generativeai")
        genai.configure(api_key=cfg.api_key)
        self._genai = genai  # FIX: Store module reference for use in chat()
        self._model_name: str = cfg.model or "gemini-1.5-pro"
        self.max_tokens: int = cfg.max_tokens
        self.temperature: float = cfg.temperature

    @staticmethod
    def _to_google_history(messages: list[dict]) -> tuple:
        """FIX: Convert OpenAI-format messages to Google genai chat history format.
        Now includes tool messages by merging them into user role parts."""
        system_txt = None
        history = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if role == "system":
                system_txt = content
            elif role == "user":
                history.append({"role": "user", "parts": [content]})
            elif role == "assistant":
                history.append({"role": "model", "parts": [content]})
            elif role == "tool":
                # FIX: Include tool results by appending as user message
                tool_name = msg.get("name", "unknown")
                tool_content = f"[Tool Result ({tool_name})]: {content}"
                if history and history[-1]["role"] == "user":
                    history[-1]["parts"].append(tool_content)
                else:
                    history.append({"role": "user", "parts": [tool_content]})
        return system_txt, history

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   **_: Any) -> dict[str, Any]:
        import time as _time
        import json as _json
        genai = self._genai
        system_txt, history = self._to_google_history(messages)
        model_kwargs: dict[str, Any] = {"model_name": self._model_name}
        if system_txt:
            model_kwargs["system_instruction"] = system_txt
        # FIX: Convert OpenAI tools to Gemini function declarations
        gemini_tools = None
        if tools:
            try:
                func_decls = []
                for t in tools:
                    fn = t.get("function", {})
                    params = fn.get("parameters", {})
                    props = {}
                    for k, v in params.get("properties", {}).items():
                        props[k] = genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description=v.get("description", "") if isinstance(v, dict) else "",
                        )
                    func_decls.append(genai.protos.FunctionDeclaration(
                        name=fn.get("name", ""),
                        description=fn.get("description", ""),
                        parameters=genai.protos.Schema(
                            type=genai.protos.Type.OBJECT,
                            properties=props,
                            required=params.get("required", []),
                        ),
                    ))
                gemini_tools = [genai.protos.Tool(function_declarations=func_decls)]
            except Exception as e:
                logger.warning(f"[GoogleClient] Tool conversion failed: {e}. Proceeding without tools.")
                gemini_tools = None
        if gemini_tools:
            model_kwargs["tools"] = gemini_tools
        model = genai.GenerativeModel(**model_kwargs)
        gen_config = genai.types.GenerationConfig(
            max_output_tokens=self.max_tokens, temperature=self.temperature
        )
        tool_calls_result: list[dict[str, Any]] = []
        if len(history) > 1:
            chat = model.start_chat(history=history[:-1])
            last_part = history[-1]["parts"][0] if history else ""
            resp = await asyncio.to_thread(chat.send_message, last_part, generation_config=gen_config)
        else:
            prompt = history[0]["parts"][0] if history else ""
            resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)
        content = ""
        try:
            content = resp.text or ""
        except Exception:
            pass
        # FIX: Extract function calls
        try:
            for part in resp.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    tool_calls_result.append({
                        "id": f"gemini-{fc.name}-{int(_time.time())}",
                        "type": "function",
                        "function": {"name": fc.name, "arguments": _json.dumps(dict(fc.args))},
                    })
        except Exception:
            pass
        return {
            "choices": [{"message": {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls_result or None,
            }}]
        }


class LLM:
    """
    Multi-provider LLM router with credential pool, token tracking, and grace call.

    Token budget is shared with the calling agent via shared TokenBudget reference.
    To wire: agent.llm.token_budget = agent.token_budget (done in ReActAgent via init hook).
    """

    def __init__(self, token_budget: int = 0) -> None:
        cfg = Config.get()
        # Use config budget if caller did not override
        effective_budget = token_budget if token_budget > 0 else cfg.token_budget
        self.token_budget = TokenBudget(max_tokens=effective_budget)
        self._backend = self._build_backend(cfg)
        self._pool: Optional[CredentialPool] = self._build_pool(cfg)
        self._max_retries = max(1, int(cfg.llm.max_retries or MAX_RETRIES))
        self._redact: bool = getattr(cfg, "redact_secrets", False)

    def _build_pool(self, cfg: Any) -> Optional[CredentialPool]:
        provider = (cfg.llm.provider or "").lower()
        if provider in ("mock",):
            return None
        return build_pool_from_config(provider, cfg.llm.api_key)

    def _build_backend(self, cfg: Any) -> Any:
        provider = (cfg.llm.provider or "").lower().strip()
        if provider == "mock":
            logger.info("Using MockLLM")
            return MockLLM()
        # FIX: Wire in offline/local routers
        if provider == "gguf":
            from app.llm.offline_router import GGUFRouter
            model_path = cfg.llm.model or ""
            if not model_path:
                raise ValueError("GGUF provider requires llm.model = '/path/to/model.gguf'")
            logger.info(f"Using GGUF offline router: {model_path}")
            return GGUFRouter(model_path=model_path, n_ctx=8192)
        if provider == "ollama":
            from app.llm.offline_router import OllamaRouter
            base_url = cfg.llm.base_url or "http://localhost:11434"
            model = cfg.llm.model or "llama3.2:3b"
            logger.info(f"Using Ollama router: {base_url} model={model}")
            return OllamaRouter(model=model, base_url=base_url)
        if provider in ("huggingface", "hf"):
            from app.llm.offline_router import HuggingFaceRouter
            logger.info(f"Using HuggingFace router: {cfg.llm.model}")
            return HuggingFaceRouter(
                model=cfg.llm.model or "",
                hf_token=cfg.llm.api_key or "",
                endpoint_url=cfg.llm.base_url,
            )
        universal_triggers = {"universal", "openrouter", "lmstudio", "openai-compat", "groq", "together", "perplexity"}
        if cfg.llm.base_url and (not provider or provider in universal_triggers):
            logger.info(f"Universal LLM — {cfg.llm.base_url} model={cfg.llm.model}")
            return UniversalClient(
                base_url=cfg.llm.base_url, api_key=cfg.llm.api_key or "none",
                model=cfg.llm.model, max_tokens=cfg.llm.max_tokens,
                temperature=cfg.llm.temperature, extra_headers=cfg.llm.extra_headers or {},
                timeout=cfg.llm.timeout,
            )
        if provider == "openai":
            return OpenAIClient(cfg.llm)
        if provider == "anthropic":
            return AnthropicClient(cfg.llm)
        if provider in ("google", "gemini"):
            return GoogleClient(cfg.llm)
        if provider == "mistral":
            from app.llm.mistral_client import MistralClient
            return MistralClient(cfg.llm)
        if provider == "bedrock":
            from app.llm.bedrock_client import BedrockClient
            return BedrockClient(cfg.llm)
        if cfg.llm.base_url:
            return UniversalClient(
                base_url=cfg.llm.base_url, api_key=cfg.llm.api_key or "none",
                model=cfg.llm.model, max_tokens=cfg.llm.max_tokens,
                temperature=cfg.llm.temperature,
                timeout=cfg.llm.timeout,
            )
        logger.warning(f"No valid LLM config (provider={provider!r}). Using MockLLM.")
        return MockLLM()

    async def ask(self, messages: list[Message], **kwargs: Any) -> Message:
        raw = [m.to_dict() for m in messages]
        data = await self._call_with_retry(raw, tools=None)
        self.token_budget.record(data)
        return _msg_from_openai(data["choices"][0])

    async def ask_tool(self, messages: list[Message], tools: list[dict[str, Any]], **kwargs: Any) -> Message:
        raw = [m.to_dict() for m in messages]
        data = await self._call_with_retry(raw, tools=tools)
        self.token_budget.record(data)
        return _msg_from_openai(data["choices"][0])

    async def _call_with_retry(self, messages: list[dict[str, Any]],
                                tools: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
        wait = RETRY_BASE_WAIT
        last_err: Optional[Exception] = None

        # Grace call: allow one extra call after budget exhausted
        if self.token_budget.is_exhausted:
            if not self.token_budget.use_grace():
                raise TokenLimitExceeded("Token budget exhausted and grace call already used.")

        # FIX: Detect if we're using a long-thinking model — timeouts are expected
        # and should NOT trigger retries. The HTTP client already has an adaptive
        # timeout of 30 minutes for such models. We only retry on transient errors.
        model_name = getattr(self._backend, 'model', '')
        is_deep_thinker = _is_long_thinking_model(model_name)

        for attempt in range(1, self._max_retries + 1):
            cred = await self._pool.get() if self._pool else None
            try:
                api_key: Optional[str] = cred.api_key if cred else None
                chat_kwargs: dict[str, Any] = {}
                # FIX: Pass rotated api_key to ALL backends, not just UniversalClient
                if api_key:
                    chat_kwargs["api_key"] = api_key

                # FIX: Long-wait progress heartbeat — start background monitor
                t_start = time.monotonic()
                result: dict[str, Any] = await self._backend.chat(messages, tools=tools, **chat_kwargs)
                elapsed = time.monotonic() - t_start

                if elapsed > 60:
                    logger.info(
                        f"[LLM] Long response received after {elapsed:.0f}s "
                        f"(model={model_name}). This is normal for deep-thinking models."
                    )

                if cred and self._pool:
                    await self._pool.mark_success(cred)
                return result

            except TokenLimitExceeded:
                raise

            except RateLimitError:
                if cred and self._pool:
                    await self._pool.mark_exhausted(cred)
                logger.warning(f"[LLM] Rate limited (attempt {attempt}). Wait {wait:.1f}s...")
                await asyncio.sleep(wait)
                wait = min(wait * 2 + random.uniform(0, 1), RETRY_MAX_WAIT)

            except (asyncio.TimeoutError, TimeoutError) as e:
                # FIX: For long-thinking models, timeouts after the adaptive period
                # (30 minutes) are genuine failures — do NOT silently retry with
                # the same request (that would spam the API and waste time).
                # For regular models, a timeout is unusual and worth one retry.
                last_err = e
                if is_deep_thinker:
                    logger.error(
                        f"[LLM] Timeout after adaptive period for deep-thinking model "
                        f"'{model_name}'. This model took too long even with extended "
                        f"timeout. NOT retrying to avoid API spam."
                    )
                    raise
                # Regular model timeout — allow retry with backoff
                if attempt < self._max_retries:
                    logger.warning(
                        f"[LLM] Timeout (attempt {attempt}/{self._max_retries}) "
                        f"for model '{model_name}'. Retrying in {wait:.1f}s..."
                    )
                    await asyncio.sleep(wait)
                    wait = min(wait * 2 + random.uniform(0, 1), RETRY_MAX_WAIT)
                else:
                    raise

            except Exception as e:
                last_err = e
                # FIX: Distinguish connection errors (retry) from content errors (don't retry)
                error_str = str(e).lower()
                is_transient = any(kw in error_str for kw in (
                    "connection", "network", "reset", "broken pipe",
                    "temporary", "502", "503", "504", "server error",
                ))
                if not is_transient and attempt >= 2:
                    # Non-transient error (bad request, auth, etc.) — fail fast
                    logger.error(f"[LLM] Non-transient error: {e}. Not retrying.")
                    raise
                if attempt == self._max_retries:
                    raise
                logger.warning(f"[LLM] Error (attempt {attempt}/{self._max_retries}): {e}. Retry in {wait:.1f}s...")
                await asyncio.sleep(wait)
                wait = min(wait * 2 + random.uniform(0, 1), RETRY_MAX_WAIT)

        raise RuntimeError(f"LLM failed after {self._max_retries} retries. Last: {last_err}")
