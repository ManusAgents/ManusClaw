from __future__ import annotations

"""
Enhanced Message Module — Multi-Modal Content & Extended Thinking Support
=========================================================================

Provides rich message types for communicating with LLMs that support
multi-modal inputs (text + images), extended thinking (Claude-style),
reasoning (OpenAI Responses API / DeepSeek-style), and tool calls.

All models are frozen Pydantic BaseModel instances for immutability and
hashability, ensuring safe use in async contexts and caching layers.
"""

import base64
import json
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Content Block Types
# ──────────────────────────────────────────────────────────────────────────────

class TextContent(BaseModel):
    """A text content block within a message."""

    type: Literal["text"] = "text"
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


class ImageContent(BaseModel):
    """An image content block within a message.

    Supports both URL-based and base64-encoded images.
    When ``base64_data`` is provided, the image is sent as an inline
    data URL; otherwise ``url`` is used directly.
    """

    type: Literal["image"] = "image"
    url: Optional[str] = None
    base64_data: Optional[str] = None
    media_type: str = "image/png"
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def _validate_source(self) -> "ImageContent":
        if not self.url and not self.base64_data:
            raise ValueError("ImageContent requires either 'url' or 'base64_data'")
        return self

    @property
    def data_url(self) -> str:
        """Return a data URL for base64-encoded images."""
        if self.base64_data:
            return f"data:{self.media_type};base64,{self.base64_data}"
        return self.url or ""

    def to_openai_dict(self) -> dict[str, Any]:
        """Convert to OpenAI image content block format."""
        if self.url:
            return {
                "type": "image_url",
                "image_url": {"url": self.url, "detail": self.detail},
            }
        return {
            "type": "image_url",
            "image_url": {"url": self.data_url, "detail": self.detail},
        }

    def to_anthropic_dict(self) -> dict[str, Any]:
        """Convert to Anthropic image content block format."""
        if self.base64_data:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": self.media_type,
                    "data": self.base64_data,
                },
            }
        return {
            "type": "image",
            "source": {"type": "url", "url": self.url},
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_openai_dict()


class ThinkingBlock(BaseModel):
    """A thinking content block (Claude-style extended thinking).

    Represents the model's internal reasoning when extended thinking
    is enabled for Anthropic Claude models.
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "thinking", "thinking": self.thinking}
        if self.signature:
            d["signature"] = self.signature
        return d


class RedactedThinkingBlock(BaseModel):
    """A redacted thinking block (Claude-style).

    Represents thinking content that the model has chosen to redact
    from the output for safety or policy reasons.
    """

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "redacted_thinking", "data": self.data}


class ReasoningItem(BaseModel):
    """A reasoning item (OpenAI Responses API style).

    Captures the model's chain-of-thought reasoning when using
    the OpenAI Responses API with reasoning enabled (o1, o3, etc.).
    """

    type: Literal["reasoning"] = "reasoning"
    id: Optional[str] = None
    summary: list[str] = Field(default_factory=list)
    content: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "reasoning"}
        if self.id:
            d["id"] = self.id
        if self.summary:
            d["summary"] = self.summary
        if self.content:
            d["content"] = self.content
        return d


# Union of all content block types
ContentBlock = Union[TextContent, ImageContent, ThinkingBlock, RedactedThinkingBlock, ReasoningItem]


# ──────────────────────────────────────────────────────────────────────────────
# Tool Call Types
# ──────────────────────────────────────────────────────────────────────────────

class FunctionCall(BaseModel):
    """A function call within a tool call."""

    name: str
    arguments: str  # JSON-encoded string

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


class EnhancedToolCall(BaseModel):
    """An enhanced tool call with proper typing."""

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.to_dict(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Enhanced Message
# ──────────────────────────────────────────────────────────────────────────────

class EnhancedMessage(BaseModel):
    """Enhanced message with multi-modal content, thinking, and reasoning support.

    This is a frozen Pydantic model that supports:
    - Simple text content (``content`` as string)
    - Multi-modal content (``content_blocks`` with TextContent, ImageContent, etc.)
    - Thinking blocks (Claude-style extended thinking)
    - Reasoning items (OpenAI Responses API style)
    - Reasoning content (DeepSeek-style reasoning)
    - Tool calls with proper typing
    - Tool results

    Frozen to ensure immutability in async contexts.
    """

    model_config = {"frozen": True}

    role: str  # system | user | assistant | tool
    content: Optional[str] = None
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = Field(default_factory=list)
    redacted_thinking_blocks: list[RedactedThinkingBlock] = Field(default_factory=list)
    reasoning_items: list[ReasoningItem] = Field(default_factory=list)
    reasoning_content: Optional[str] = None  # DeepSeek-style reasoning
    tool_calls: Optional[list[EnhancedToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    cache_control: Optional[dict[str, Any]] = None  # Anthropic prompt caching

    # ──────────────────────────────────────────────────────────────────────
    # Factory Methods
    # ──────────────────────────────────────────────────────────────────────

    @classmethod
    def system(cls, content: str, **kwargs: Any) -> "EnhancedMessage":
        """Create a system message."""
        return cls(role="system", content=content, **kwargs)

    @classmethod
    def user(
        cls,
        content: Optional[str] = None,
        content_blocks: Optional[list[ContentBlock]] = None,
        **kwargs: Any,
    ) -> "EnhancedMessage":
        """Create a user message, optionally with multi-modal content."""
        return cls(
            role="user",
            content=content,
            content_blocks=content_blocks or [],
            **kwargs,
        )

    @classmethod
    def user_text(cls, text: str, **kwargs: Any) -> "EnhancedMessage":
        """Create a simple text user message."""
        return cls(role="user", content=text, **kwargs)

    @classmethod
    def user_image_url(cls, text: str, image_url: str, detail: str = "auto", **kwargs: Any) -> "EnhancedMessage":
        """Create a user message with text and an image URL."""
        return cls(
            role="user",
            content=text,
            content_blocks=[
                TextContent(text=text),
                ImageContent(url=image_url, detail=detail),
            ],
            **kwargs,
        )

    @classmethod
    def user_image_base64(
        cls,
        text: str,
        base64_data: str,
        media_type: str = "image/png",
        detail: str = "auto",
        **kwargs: Any,
    ) -> "EnhancedMessage":
        """Create a user message with text and a base64-encoded image."""
        return cls(
            role="user",
            content=text,
            content_blocks=[
                TextContent(text=text),
                ImageContent(base64_data=base64_data, media_type=media_type, detail=detail),
            ],
            **kwargs,
        )

    @classmethod
    def assistant(
        cls,
        content: Optional[str] = None,
        tool_calls: Optional[list[EnhancedToolCall]] = None,
        thinking_blocks: Optional[list[ThinkingBlock]] = None,
        reasoning_content: Optional[str] = None,
        **kwargs: Any,
    ) -> "EnhancedMessage":
        """Create an assistant message."""
        return cls(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            thinking_blocks=thinking_blocks or [],
            reasoning_content=reasoning_content,
            **kwargs,
        )

    @classmethod
    def tool_result(cls, content: str, tool_call_id: str, name: str, **kwargs: Any) -> "EnhancedMessage":
        """Create a tool result message."""
        return cls(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            **kwargs,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Conversion Methods
    # ──────────────────────────────────────────────────────────────────────

    def to_openai_dict(self) -> dict[str, Any]:
        """Convert to OpenAI Chat Completions API message format.

        Handles multi-modal content by building the ``content`` field as
        a list of content blocks when images are present.
        """
        d: dict[str, Any] = {"role": self.role}

        # Build content
        if self.content_blocks:
            parts: list[dict[str, Any]] = []
            # Add text content first
            text_parts = [b for b in self.content_blocks if isinstance(b, TextContent)]
            image_parts = [b for b in self.content_blocks if isinstance(b, ImageContent)]

            if text_parts:
                # Use first text block as primary, or merge
                combined_text = " ".join(t.text for t in text_parts)
                if not image_parts:
                    d["content"] = combined_text
                else:
                    parts.append({"type": "text", "text": combined_text})

            for img in image_parts:
                parts.append(img.to_openai_dict())

            if parts:
                d["content"] = parts
            elif self.content is not None:
                d["content"] = self.content
            else:
                d["content"] = None
        elif self.content is not None:
            d["content"] = self.content
        else:
            d["content"] = None

        # Tool calls
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]

        # Tool result fields
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name

        return d

    def to_anthropic_dict(self) -> dict[str, Any]:
        """Convert to Anthropic Messages API format.

        System messages are returned with role "system" so the caller
        can extract them separately (Anthropic passes system as a top-level param).
        """
        d: dict[str, Any] = {"role": self.role}

        if self.role == "system":
            d["content"] = self.content or ""
            return d

        # Build content blocks
        blocks: list[dict[str, Any]] = []

        # Add thinking blocks
        for tb in self.thinking_blocks:
            blocks.append(tb.to_dict())
        for rtb in self.redacted_thinking_blocks:
            blocks.append(rtb.to_dict())

        # Add text content
        if self.content:
            blocks.append({"type": "text", "text": self.content})

        # Add content blocks (images, additional text)
        for block in self.content_blocks:
            if isinstance(block, TextContent) and not self.content:
                blocks.append(block.to_dict())
            elif isinstance(block, ImageContent):
                blocks.append(block.to_anthropic_dict())
            elif isinstance(block, ThinkingBlock):
                blocks.append(block.to_dict())
            elif isinstance(block, RedactedThinkingBlock):
                blocks.append(block.to_dict())

        # Add tool_use blocks for assistant messages
        if self.tool_calls:
            for tc in self.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                })

        # Tool result
        if self.role == "tool" and self.tool_call_id:
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": self.tool_call_id,
                    "content": self.content or "",
                }],
            }

        d["content"] = blocks if blocks else (self.content or "")
        return d

    def to_dict(self) -> dict[str, Any]:
        """Convert to standard OpenAI-compatible dict format.

        This is the default serialization used by the LLM module.
        """
        return self.to_openai_dict()

    # ──────────────────────────────────────────────────────────────────────
    # Properties
    # ──────────────────────────────────────────────────────────────────────

    @property
    def has_images(self) -> bool:
        """Check if this message contains image content."""
        return any(isinstance(b, ImageContent) for b in self.content_blocks)

    @property
    def has_thinking(self) -> bool:
        """Check if this message contains thinking blocks."""
        return bool(self.thinking_blocks) or bool(self.redacted_thinking_blocks)

    @property
    def has_reasoning(self) -> bool:
        """Check if this message contains reasoning content."""
        return bool(self.reasoning_items) or self.reasoning_content is not None

    @property
    def text_content(self) -> str:
        """Extract all text content from this message."""
        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        for block in self.content_blocks:
            if isinstance(block, TextContent):
                parts.append(block.text)
        return " ".join(parts)

    @property
    def thinking_text(self) -> str:
        """Extract all thinking text from this message."""
        parts = [tb.thinking for tb in self.thinking_blocks]
        return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Conversion helpers
# ──────────────────────────────────────────────────────────────────────────────

def from_openai_message(msg_dict: dict[str, Any]) -> EnhancedMessage:
    """Convert an OpenAI-format message dict to an EnhancedMessage.

    Handles:
    - Simple string content
    - List of content blocks (text + image_url)
    - Tool calls
    - Tool results
    """
    role = msg_dict.get("role", "user")
    content = msg_dict.get("content")
    content_blocks: list[ContentBlock] = []

    # Parse multi-modal content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    url_data = block.get("image_url", {})
                    url = url_data.get("url", "")
                    detail = url_data.get("detail", "auto")
                    if url.startswith("data:"):
                        # Data URL — extract base64
                        try:
                            _, data_part = url.split(",", 1)
                            media_part = url.split(";")[0].split(":")[1] if ":" in url else "image/png"
                            content_blocks.append(
                                ImageContent(base64_data=data_part, media_type=media_part, detail=detail)
                            )
                        except (ValueError, IndexError):
                            content_blocks.append(ImageContent(url=url, detail=detail))
                    else:
                        content_blocks.append(ImageContent(url=url, detail=detail))
        content = " ".join(text_parts) if text_parts else None

    # Parse tool calls
    tool_calls: Optional[list[EnhancedToolCall]] = None
    raw_tcs = msg_dict.get("tool_calls") or []
    if raw_tcs:
        tool_calls = [
            EnhancedToolCall(
                id=tc.get("id", ""),
                type=tc.get("type", "function"),
                function=FunctionCall(
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", "{}"),
                ),
            )
            for tc in raw_tcs
        ]

    return EnhancedMessage(
        role=role,
        content=content,
        content_blocks=content_blocks,
        tool_calls=tool_calls,
        tool_call_id=msg_dict.get("tool_call_id"),
        name=msg_dict.get("name"),
    )


def from_anthropic_message(msg_dict: dict[str, Any]) -> EnhancedMessage:
    """Convert an Anthropic-format message dict to an EnhancedMessage.

    Handles content blocks including text, tool_use, thinking, and redacted_thinking.
    """
    role = msg_dict.get("role", "user")
    raw_content = msg_dict.get("content", "")

    text_parts: list[str] = []
    content_blocks: list[ContentBlock] = []
    thinking_blocks: list[ThinkingBlock] = []
    redacted_thinking_blocks: list[RedactedThinkingBlock] = []
    reasoning_items: list[ReasoningItem] = []
    tool_calls: list[EnhancedToolCall] = []

    if isinstance(raw_content, str):
        text_parts.append(raw_content)
    elif isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "text":
                text_parts.append(block.get("text", ""))
                content_blocks.append(TextContent(text=block.get("text", "")))
            elif block_type == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    content_blocks.append(
                        ImageContent(
                            base64_data=source.get("data", ""),
                            media_type=source.get("media_type", "image/png"),
                        )
                    )
                elif source.get("type") == "url":
                    content_blocks.append(ImageContent(url=source.get("url", "")))
            elif block_type == "thinking":
                thinking_blocks.append(
                    ThinkingBlock(
                        thinking=block.get("thinking", ""),
                        signature=block.get("signature"),
                    )
                )
            elif block_type == "redacted_thinking":
                redacted_thinking_blocks.append(
                    RedactedThinkingBlock(data=block.get("data", ""))
                )
            elif block_type == "tool_use":
                args_json = json.dumps(block.get("input", {}))
                tool_calls.append(
                    EnhancedToolCall(
                        id=block.get("id", ""),
                        type="function",
                        function=FunctionCall(
                            name=block.get("name", ""),
                            arguments=args_json,
                        ),
                    )
                )

    return EnhancedMessage(
        role=role,
        content=" ".join(text_parts) if text_parts else None,
        content_blocks=content_blocks,
        thinking_blocks=thinking_blocks,
        redacted_thinking_blocks=redacted_thinking_blocks,
        reasoning_items=reasoning_items,
        tool_calls=tool_calls or None,
    )


def from_schema_message(msg: Any) -> EnhancedMessage:
    """Convert a legacy app.schema.Message to an EnhancedMessage."""
    tool_calls: Optional[list[EnhancedToolCall]] = None
    if msg.tool_calls:
        tool_calls = [
            EnhancedToolCall(
                id=tc.id,
                type=tc.type,
                function=FunctionCall(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ),
            )
            for tc in msg.tool_calls
        ]
    return EnhancedMessage(
        role=msg.role.value if hasattr(msg.role, "value") else str(msg.role),
        content=msg.content,
        tool_calls=tool_calls,
        tool_call_id=msg.tool_call_id,
        name=msg.name,
    )


def encode_image_file(file_path: str, media_type: Optional[str] = None) -> ImageContent:
    """Read an image file and return an ImageContent with base64 encoding.

    Automatically detects media type from file extension if not provided.
    """
    import mimetypes

    if media_type is None:
        guessed, _ = mimetypes.guess_type(file_path)
        media_type = guessed or "image/png"

    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")

    return ImageContent(base64_data=data, media_type=media_type)
