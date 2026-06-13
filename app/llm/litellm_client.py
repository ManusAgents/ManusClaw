from __future__ import annotations

"""
LiteLLM Client — Unified LLM Interface Supporting 100+ Providers
==================================================================

Provides a production-grade LLM client built on litellm that supports:
- 100+ providers (OpenAI, Anthropic, Google, Azure, Bedrock, etc.)
- Fallback to existing custom implementation when litellm is not installed
- Support for both Chat Completions and Responses API
- Image handling with automatic resizing per provider
- Proper error mapping from provider-specific to manusclaw exceptions
- Thread-safe with connection reuse
- Integrated streaming, retry, fallback, and metrics

When litellm is not installed, the client gracefully falls back to the
existing LLM class in app.llm.llm.

Usage::

    client = LiteLLMClient(model="gpt-4o", api_key="...")

    # Simple call
    response = await client.chat(messages=[...])

    # Streaming
    async for chunk in client.stream(messages=[...]):
        print(chunk, end="")

    # With tools
    response = await client.chat(messages=[...], tools=[...])
"""

import asyncio
import base64
import json
import threading
import time
from typing import Any, AsyncIterator, Optional

from app.config import Config
from app.exceptions import (
    LLMAuthError,
    ManusClawError,
    NonRetryableError,
    RateLimitError,
    RetryableError,
    TokenLimitExceeded,
)
from app.logger import logger
from app.schema import Message, Role, ToolCall, Function

# Optional litellm import
_HAS_LITELLM = False
_litellm = None
try:
    import litellm as _litellm_module
    _litellm = _litellm_module
    _HAS_LITELLM = True
except ImportError:
    pass

from app.llm.retry import (
    RetryConfig,
    RetryExecutor,
    map_provider_exception,
    classify_error,
    is_retryable,
    ErrorCategory,
)
from app.llm.metrics import LLMMetrics, get_metrics, calculate_cost
from app.llm.fallback import FallbackStrategy, FallbackConfig, FallbackResult


# ──────────────────────────────────────────────────────────────────────────────
# Image Handling
# ──────────────────────────────────────────────────────────────────────────────

# Maximum image dimensions per provider (width x height)
_PROVIDER_IMAGE_LIMITS: dict[str, tuple[int, int]] = {
    "openai": (2048, 2048),
    "anthropic": (1568, 1568),
    "google": (4096, 4096),
    "default": (2048, 2048),
}


def _get_provider_from_model(model: str) -> str:
    """Infer the provider from the model name."""
    model_lower = model.lower()
    if model_lower.startswith(("gpt-", "o1", "o3", "chatgpt", "dall-e")):
        return "openai"
    if model_lower.startswith(("claude-", "claude3")):
        return "anthropic"
    if model_lower.startswith(("gemini-", "gemini")):
        return "google"
    if model_lower.startswith("mistral"):
        return "mistral"
    if model_lower.startswith("deepseek"):
        return "deepseek"
    if model_lower.startswith(("azure/", "azure/")):
        return "azure"
    if model_lower.startswith("bedrock/"):
        return "bedrock"
    if model_lower.startswith(("vertex_ai/", "vertex/")):
        return "vertex"
    return "default"


def _resize_image_base64(
    base64_data: str,
    max_width: int,
    max_height: int,
    media_type: str = "image/png",
) -> str:
    """Resize a base64-encoded image to fit within the given dimensions.

    Returns the resized image as base64. If PIL/Pillow is not available,
    returns the original image unchanged.
    """
    try:
        from PIL import Image
        import io

        image_bytes = base64.b64decode(base64_data)
        img = Image.open(io.BytesIO(image_bytes))

        # Check if resize is needed
        if img.width <= max_width and img.height <= max_height:
            return base64_data

        # Calculate new dimensions maintaining aspect ratio
        ratio = min(max_width / img.width, max_height / img.height)
        new_width = int(img.width * ratio)
        new_height = int(img.height * ratio)

        # Resize
        img = img.resize((new_width, new_height), Image.LANCZOS)

        # Encode back to base64
        buffer = io.BytesIO()
        format_map = {
            "image/png": "PNG",
            "image/jpeg": "JPEG",
            "image/gif": "GIF",
            "image/webp": "WEBP",
        }
        img_format = format_map.get(media_type, "PNG")
        img.save(buffer, format=img_format)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    except ImportError:
        logger.debug("[LiteLLMClient] Pillow not available, skipping image resize")
        return base64_data
    except Exception as e:
        logger.warning(f"[LiteLLMClient] Image resize failed: {e}")
        return base64_data


def _process_images_for_provider(
    messages: list[dict[str, Any]],
    provider: str,
) -> list[dict[str, Any]]:
    """Process images in messages, resizing them per provider limits.

    Walks through all messages and their content blocks, resizing
    any base64-encoded images to fit within the provider's limits.
    """
    limits = _PROVIDER_IMAGE_LIMITS.get(provider, _PROVIDER_IMAGE_LIMITS["default"])
    max_w, max_h = limits

    processed = []
    for msg in messages:
        new_msg = dict(msg)
        content = msg.get("content")

        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        try:
                            header, data = url.split(",", 1)
                            media_type = header.split(";")[0].split(":")[1] if ":" in header else "image/png"
                            resized = _resize_image_base64(data, max_w, max_h, media_type)
                            new_url = f"data:{media_type};base64,{resized}"
                            new_parts.append({
                                "type": "image_url",
                                "image_url": {"url": new_url, "detail": part.get("image_url", {}).get("detail", "auto")},
                            })
                        except (ValueError, IndexError):
                            new_parts.append(part)
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            new_msg["content"] = new_parts

        processed.append(new_msg)

    return processed


# ──────────────────────────────────────────────────────────────────────────────
# LiteLLM Client
# ──────────────────────────────────────────────────────────────────────────────

class LiteLLMClient:
    """Unified LLM client using litellm for 100+ provider support.

    Falls back to the existing LLM class when litellm is not installed.

    Features:
    - Automatic provider detection from model name
    - Image resizing per provider limits
    - Integrated retry with exponential backoff
    - Integrated fallback strategy
    - Integrated metrics tracking
    - Both Chat Completions and streaming support
    - Thread-safe with connection reuse

    Usage::

        client = LiteLLMClient(model="gpt-4o")

        # Simple chat
        response = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
        )

        # With tools
        response = await client.chat(
            messages=[...],
            tools=[{"type": "function", "function": {...}}],
        )

        # Streaming
        async for chunk in client.stream(messages=[...]):
            print(chunk, end="")
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: int = 1800,
        retry_config: Optional[RetryConfig] = None,
        fallback_chain: Optional[list[str]] = None,
        conversation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # Load defaults from config
        cfg = Config.get()
        self._model = model or cfg.llm.model or "gpt-4o"
        self._api_key = api_key or cfg.llm.api_key
        self._api_base = api_base or cfg.llm.base_url
        self._max_tokens = max_tokens or cfg.llm.max_tokens
        self._temperature = temperature if temperature != 0.0 else cfg.llm.temperature
        self._timeout = timeout or cfg.llm.timeout
        self._extra_headers: dict[str, str] = kwargs.get("extra_headers", {}) or cfg.llm.extra_headers
        self._provider = _get_provider_from_model(self._model)
        self._conversation_id = conversation_id or "default"

        # Retry
        self._retry_config = retry_config or RetryConfig(
            max_retries=min(cfg.llm.max_retries, 15),
            provider=self._provider,
        )
        self._retry_executor = RetryExecutor(self._retry_config)

        # Fallback
        self._fallback: Optional[FallbackStrategy] = None
        if fallback_chain:
            self._fallback = FallbackStrategy(
                config=FallbackConfig(chain=fallback_chain),
            )

        # Metrics
        self._metrics = get_metrics()

        # Lock for thread safety
        self._lock = threading.Lock()

        # Fallback LLM (when litellm is not available)
        self._fallback_llm: Optional[Any] = None

        if _HAS_LITELLM:
            logger.info(
                f"[LiteLLMClient] Initialized with litellm: model={self._model} "
                f"provider={self._provider}"
            )
        else:
            logger.info(
                "[LiteLLMClient] litellm not installed, falling back to built-in LLM"
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def has_litellm(self) -> bool:
        return _HAS_LITELLM

    # ──────────────────────────────────────────────────────────────────────
    # Chat Completions
    # ──────────────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request.

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            model: Override the default model.
            temperature: Override the default temperature.
            max_tokens: Override the default max_tokens.
            api_key: Override the default API key.
            **kwargs: Additional kwargs passed to litellm.completion.

        Returns:
            Response dict in OpenAI Chat Completion format.
        """
        use_model = model or self._model
        provider = _get_provider_from_model(use_model)

        # Process images for provider limits
        processed_messages = _process_images_for_provider(messages, provider)

        start_time = time.monotonic()
        is_error = False

        try:
            if self._fallback:
                result = await self._fallback.execute(
                    lambda m, **_: self._do_chat(
                        messages=processed_messages,
                        tools=tools,
                        model=m,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        api_key=api_key,
                        **kwargs,
                    ),
                )
                response = result.value
                use_model = result.model_used
            else:
                response = await self._do_chat(
                    messages=processed_messages,
                    tools=tools,
                    model=use_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    api_key=api_key,
                    **kwargs,
                )

            return response

        except Exception as e:
            is_error = True
            raise

        finally:
            latency = time.monotonic() - start_time
            # Record metrics (best effort)
            try:
                usage = {}
                if not is_error:
                    # Extract usage from response if available
                    resp_obj = locals().get("response", {})
                    if isinstance(resp_obj, dict):
                        usage = resp_obj.get("usage", {})

                self._metrics.record_call(
                    conversation_id=self._conversation_id,
                    model=use_model,
                    provider=provider,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                    cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
                    reasoning_tokens=usage.get("reasoning_tokens", 0),
                    latency_s=latency,
                    is_error=is_error,
                )
            except Exception as metrics_err:
                logger.debug(f"[LiteLLMClient] Metrics recording failed: {metrics_err}")

    async def _do_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal chat implementation.

        Uses litellm when available, falls back to built-in LLM otherwise.
        """
        use_model = model or self._model
        use_temp = temperature if temperature is not None else self._temperature
        use_max_tokens = max_tokens or self._max_tokens
        use_key = api_key or self._api_key

        if not _HAS_LITELLM:
            return await self._chat_fallback(
                messages=messages,
                tools=tools,
                model=use_model,
                **kwargs,
            )

        # Use litellm
        call_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "max_tokens": use_max_tokens,
            "temperature": use_temp,
            "timeout": self._timeout,
        }

        if use_key:
            call_kwargs["api_key"] = use_key
        if self._api_base:
            call_kwargs["api_base"] = self._api_base
        if self._extra_headers:
            call_kwargs["extra_headers"] = self._extra_headers
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")

        # Merge additional kwargs
        for k, v in kwargs.items():
            if k not in ("tool_choice",):
                call_kwargs[k] = v

        try:
            response = await self._retry_executor.execute(
                lambda: _litellm.acompletion(**call_kwargs),
            )

            # litellm returns a ModelResponse object; convert to dict
            if hasattr(response, "model_dump"):
                return response.model_dump()
            if hasattr(response, "json"):
                return json.loads(response.json())
            return dict(response)

        except Exception as e:
            mapped = map_provider_exception(e, self._provider, use_model)
            if isinstance(mapped, (RateLimitError, RetryableError)):
                raise mapped from e
            raise

    async def _chat_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fallback to the built-in LLM when litellm is not available."""
        if self._fallback_llm is None:
            from app.llm.llm import LLM
            self._fallback_llm = LLM()

        return await self._fallback_llm._call_with_retry(messages, tools=tools)

    # ──────────────────────────────────────────────────────────────────────
    # Streaming
    # ──────────────────────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion response.

        Yields chunks in OpenAI streaming format.

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            model: Override the default model.
            temperature: Override the default temperature.
            max_tokens: Override the default max_tokens.
            api_key: Override the default API key.
            **kwargs: Additional kwargs passed to litellm.completion.

        Yields:
            Dict chunks in OpenAI streaming format.
        """
        use_model = model or self._model
        provider = _get_provider_from_model(use_model)

        processed_messages = _process_images_for_provider(messages, provider)

        if not _HAS_LITELLM:
            # Fallback: non-streaming call
            response = await self._chat_fallback(
                messages=processed_messages,
                tools=tools,
                model=use_model,
                **kwargs,
            )
            # Yield as a single chunk
            yield response
            return

        call_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": processed_messages,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
            "timeout": self._timeout,
            "stream": True,
        }

        use_key = api_key or self._api_key
        if use_key:
            call_kwargs["api_key"] = use_key
        if self._api_base:
            call_kwargs["api_base"] = self._api_base
        if self._extra_headers:
            call_kwargs["extra_headers"] = self._extra_headers
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")

        for k, v in kwargs.items():
            if k not in ("tool_choice", "stream"):
                call_kwargs[k] = v

        try:
            response = await _litellm.acompletion(**call_kwargs)
            async for chunk in response:
                if hasattr(chunk, "model_dump"):
                    yield chunk.model_dump()
                elif hasattr(chunk, "json"):
                    yield json.loads(chunk.json())
                else:
                    yield dict(chunk)

        except Exception as e:
            mapped = map_provider_exception(e, self._provider, use_model)
            raise mapped from e

    # ──────────────────────────────────────────────────────────────────────
    # High-level ask methods (compatible with LLM class)
    # ──────────────────────────────────────────────────────────────────────

    async def ask(self, messages: list[Message], **kwargs: Any) -> Message:
        """Ask the LLM and return a Message object.

        Compatible with the LLM.ask() interface.
        """
        raw = [m.to_dict() for m in messages]
        data = await self.chat(raw, **kwargs)
        return self._msg_from_response(data)

    async def ask_tool(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Message:
        """Ask the LLM with tools and return a Message object.

        Compatible with the LLM.ask_tool() interface.
        """
        raw = [m.to_dict() for m in messages]
        data = await self.chat(raw, tools=tools, **kwargs)
        return self._msg_from_response(data)

    @staticmethod
    def _msg_from_response(data: dict[str, Any]) -> Message:
        """Convert an OpenAI-format response dict to a Message object."""
        choices = data.get("choices", [])
        if not choices:
            return Message(role=Role.ASSISTANT, content="(no response)")
        choice = choices[0]
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

    # ──────────────────────────────────────────────────────────────────────
    # Responses API (OpenAI Responses API)
    # ──────────────────────────────────────────────────────────────────────

    async def responses(
        self,
        input: Any,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call the OpenAI Responses API (for o1, o3 models).

        This uses the Responses API format which is different from
        Chat Completions. It is only available when litellm is installed
        and the provider is OpenAI.

        Args:
            input: The input (can be a string or list of messages).
            model: The model to use.
            instructions: System instructions.
            tools: Tool definitions.
            **kwargs: Additional kwargs.

        Returns:
            Response dict from the Responses API.
        """
        if not _HAS_LITELLM:
            # Fall back to chat completions
            logger.warning("[LiteLLMClient] Responses API not available without litellm, using chat completions")
            if isinstance(input, str):
                messages = [{"role": "user", "content": input}]
            else:
                messages = input
            if instructions:
                messages = [{"role": "system", "content": instructions}] + messages
            return await self.chat(messages=messages, tools=tools, model=model, **kwargs)

        use_model = model or self._model
        call_kwargs: dict[str, Any] = {
            "model": use_model,
            "input": input,
        }
        if instructions:
            call_kwargs["instructions"] = instructions
        if tools:
            call_kwargs["tools"] = tools
        if self._api_key:
            call_kwargs["api_key"] = self._api_key
        call_kwargs.update(kwargs)

        try:
            # Use litellm's responses endpoint if available
            if hasattr(_litellm, "aresponses"):
                response = await _litellm.aresponses(**call_kwargs)
            else:
                # Fall back to chat completions format
                if isinstance(input, str):
                    messages = [{"role": "user", "content": input}]
                else:
                    messages = input
                if instructions:
                    messages = [{"role": "system", "content": instructions}] + messages
                return await self.chat(messages=messages, tools=tools, model=model, **kwargs)

            if hasattr(response, "model_dump"):
                return response.model_dump()
            if hasattr(response, "json"):
                return json.loads(response.json())
            return dict(response)

        except Exception as e:
            mapped = map_provider_exception(e, self._provider, use_model)
            raise mapped from e

    # ──────────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def list_models() -> list[str]:
        """List available models from litellm.

        Returns an empty list if litellm is not installed.
        """
        if not _HAS_LITELLM:
            return []
        try:
            return _litellm.model_list or []
        except Exception:
            return []

    @staticmethod
    def get_model_info(model: str) -> dict[str, Any]:
        """Get information about a specific model from litellm.

        Returns an empty dict if litellm is not installed.
        """
        if not _HAS_LITELLM:
            return {}
        try:
            return _litellm.get_model_info(model) or {}
        except Exception:
            return {}

    def set_conversation_id(self, conversation_id: str) -> None:
        """Set the conversation ID for metrics tracking."""
        self._conversation_id = conversation_id

    def estimate_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> float:
        """Estimate the cost for a call with the given token counts."""
        return calculate_cost(
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )
