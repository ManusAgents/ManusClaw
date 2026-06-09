from __future__ import annotations
"""Mistral AI client — official SDK wrapper."""
from typing import Any, Optional


class MistralClient:
    def __init__(self, cfg) -> None:
        try:
            from mistralai import Mistral as _Mistral
        except ImportError:
            raise ImportError(
                "mistralai package not installed. Run: pip install mistralai"
            )
        self._c = _Mistral(api_key=cfg.api_key)
        self.model = cfg.model or "mistral-large-latest"
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        import asyncio
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await asyncio.to_thread(
            lambda: self._c.chat.complete(**kwargs)
        )
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = None
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            import json
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments if isinstance(tc.function.arguments, str)
                                     else json.dumps(tc.function.arguments),
                    },
                }
                for tc in msg.tool_calls
            ]
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": getattr(msg, "content", None),
                    "tool_calls": tool_calls,
                }
            }],
            "usage": {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            } if hasattr(resp, "usage") and resp.usage else {},
        }
