from __future__ import annotations
"""AWS Bedrock client — Converse API wrapper (Claude, Titan, Llama via Bedrock)."""
import json
from typing import Any, Optional


class BedrockClient:
    def __init__(self, cfg) -> None:
        import os
        try:
            import boto3
        except ImportError:
            raise ImportError("boto3 not installed. Run: pip install boto3")
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        self.model = cfg.model or "anthropic.claude-3-5-sonnet-20241022-v2:0"
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        import asyncio
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
        conv = [m for m in messages if m["role"] != "system"]

        bedrock_msgs = []
        for m in conv:
            content = m.get("content") or ""
            bedrock_msgs.append({"role": m["role"], "content": [{"text": content}]})

        kwargs: dict[str, Any] = dict(
            modelId=self.model,
            messages=bedrock_msgs,
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
        )
        if system_msg:
            kwargs["system"] = [{"text": system_msg}]
        if tools:
            kwargs["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["function"]["name"],
                            "description": t["function"].get("description", ""),
                            "inputSchema": {"json": t["function"].get("parameters", {})},
                        }
                    }
                    for t in tools
                ]
            }

        resp = await asyncio.to_thread(lambda: self._client.converse(**kwargs))
        out = resp.get("output", {}).get("message", {})
        content_parts = []
        tool_calls = []
        for block in out.get("content", []):
            if "text" in block:
                content_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append({
                    "id": tu["toolUseId"],
                    "type": "function",
                    "function": {
                        "name": tu["name"],
                        "arguments": json.dumps(tu.get("input", {})),
                    },
                })
        usage = resp.get("usage", {})
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "\n".join(content_parts) or None,
                    "tool_calls": tool_calls or None,
                }
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
            },
        }
