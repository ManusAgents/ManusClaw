from __future__ import annotations

"""
Streaming Response Support — SSE, Token-Level Callbacks, and Backpressure
=========================================================================

Provides comprehensive streaming support for LLM responses:

- Token-level streaming with callbacks
- SSE (Server-Sent Events) for web clients
- StreamingDelta event emission for progressive content
- Proper cleanup on client disconnect
- Backpressure handling with configurable buffer sizes
- Support for both sync and async streaming

Typical usage::

    # Async streaming with callback
    async for delta in stream_async(llm_stream):
        print(delta.content, end="", flush=True)

    # SSE for web clients
    async def sse_endpoint(request):
        async for event in stream_sse(llm_stream):
            yield event
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Iterator, Optional, Union

from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Streaming Event Types
# ──────────────────────────────────────────────────────────────────────────────

class StreamEventType(str, Enum):
    """Types of streaming events."""

    START = "start"
    DELTA = "delta"              # Text content delta
    THINKING_DELTA = "thinking_delta"  # Thinking content delta (Claude extended thinking)
    TOOL_CALL_DELTA = "tool_call_delta"  # Partial tool call
    USAGE = "usage"              # Token usage info
    DONE = "done"                # Stream complete
    ERROR = "error"              # Error occurred
    METADATA = "metadata"        # Metadata event


# ──────────────────────────────────────────────────────────────────────────────
# Streaming Data Classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StreamingDelta:
    """A single streaming content delta.

    Represents a chunk of content received from the LLM during streaming.
    """

    content: str = ""
    thinking_content: str = ""
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    tool_call_arguments: str = ""
    event_type: StreamEventType = StreamEventType.DELTA
    model: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
        }
        if self.content:
            d["content"] = self.content
        if self.thinking_content:
            d["thinking_content"] = self.thinking_content
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_call_name:
            d["tool_call_name"] = self.tool_call_name
        if self.tool_call_arguments:
            d["tool_call_arguments"] = self.tool_call_arguments
        if self.model:
            d["model"] = self.model
        return d


@dataclass
class StreamingResult:
    """Aggregated result from a completed stream.

    Accumulates all deltas into the final response.
    """

    content: str = ""
    thinking_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: Optional[dict[str, Any]] = None
    duration_s: float = 0.0
    total_deltas: int = 0

    def apply_delta(self, delta: StreamingDelta) -> None:
        """Apply a streaming delta to this result."""
        self.total_deltas += 1

        if delta.content:
            self.content += delta.content

        if delta.thinking_content:
            self.thinking_content += delta.thinking_content

        if delta.tool_call_id:
            # Find or create the tool call accumulator
            tc = None
            for existing in self.tool_calls:
                if existing.get("id") == delta.tool_call_id:
                    tc = existing
                    break
            if tc is None:
                tc = {
                    "id": delta.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": delta.tool_call_name or "",
                        "arguments": "",
                    },
                }
                self.tool_calls.append(tc)
            if delta.tool_call_name:
                tc["function"]["name"] = delta.tool_call_name
            if delta.tool_call_arguments:
                tc["function"]["arguments"] += delta.tool_call_arguments

    def to_openai_response(self) -> dict[str, Any]:
        """Convert to OpenAI Chat Completion response format."""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content or None,
        }
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls

        return {
            "choices": [{"message": message}],
            "usage": self.usage or {},
            "model": self.model,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Callback Types
# ──────────────────────────────────────────────────────────────────────────────

OnDeltaCallback = Callable[[StreamingDelta], None]
OnDoneCallback = Callable[[StreamingResult], None]
OnErrorCallback = Callable[[Exception], None]


# ──────────────────────────────────────────────────────────────────────────────
# Backpressure Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BackpressureConfig:
    """Configuration for stream backpressure handling.

    When the consumer (callback or iterator) cannot keep up with the
    producer (LLM stream), backpressure is applied to prevent memory
    exhaustion.

    Attributes:
        max_buffer_size: Maximum number of deltas to buffer.
        high_watermark: Start applying backpressure at this buffer level.
        low_watermark: Resume normal flow at this buffer level.
        drop_oldest_on_overflow: If True, drop oldest deltas on overflow;
                                 if False, block the producer.
    """

    max_buffer_size: int = 1000
    high_watermark: int = 800
    low_watermark: int = 400
    drop_oldest_on_overflow: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Stream Metrics
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StreamMetrics:
    """Metrics for a single streaming session."""

    stream_id: str = ""
    model: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    total_deltas: int = 0
    total_content_chars: int = 0
    total_thinking_chars: int = 0
    tool_calls_count: int = 0
    backpressure_events: int = 0
    dropped_deltas: int = 0
    error: Optional[str] = None

    @property
    def duration_s(self) -> float:
        if self.end_time <= 0:
            return time.time() - self.start_time
        return self.end_time - self.start_time

    @property
    def tokens_per_second(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        # Rough estimate: ~4 chars per token
        estimated_tokens = self.total_content_chars / 4
        return estimated_tokens / self.duration_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "model": self.model,
            "duration_s": round(self.duration_s, 3),
            "total_deltas": self.total_deltas,
            "total_content_chars": self.total_content_chars,
            "total_thinking_chars": self.total_thinking_chars,
            "tool_calls_count": self.tool_calls_count,
            "tokens_per_second": round(self.tokens_per_second, 1),
            "backpressure_events": self.backpressure_events,
            "dropped_deltas": self.dropped_deltas,
            "error": self.error,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Async Stream Processor
# ──────────────────────────────────────────────────────────────────────────────

class AsyncStreamProcessor:
    """Process an async LLM stream with callbacks, backpressure, and SSE output.

    This is the main workhorse for async streaming. It wraps an async
    iterator of raw stream chunks and provides:
    - Delta extraction from provider-specific chunk formats
    - Callback invocation on each delta
    - Backpressure management
    - SSE event generation
    - Automatic cleanup on disconnect or error

    Usage::

        processor = AsyncStreamProcessor(
            stream=my_llm_stream,
            on_delta=my_callback,
        )
        async for delta in processor:
            handle(delta)
    """

    def __init__(
        self,
        stream: AsyncIterator[Any],
        on_delta: Optional[OnDeltaCallback] = None,
        on_done: Optional[OnDoneCallback] = None,
        on_error: Optional[OnErrorCallback] = None,
        backpressure: Optional[BackpressureConfig] = None,
        model: str = "",
        stream_id: Optional[str] = None,
        disconnect_check: Optional[Callable[[], bool]] = None,
        buffer_size: int = 100,
    ) -> None:
        self._stream = stream
        self._on_delta = on_delta
        self._on_done = on_done
        self._on_error = on_error
        self._backpressure = backpressure or BackpressureConfig()
        self._model = model
        self._stream_id = stream_id or str(uuid.uuid4())[:12]
        self._disconnect_check = disconnect_check
        self._buffer_size = buffer_size

        self._result = StreamingResult(model=model)
        self._metrics = StreamMetrics(
            stream_id=self._stream_id,
            model=model,
            start_time=time.time(),
        )
        self._buffer: asyncio.Queue[Optional[StreamingDelta]] = asyncio.Queue(
            maxsize=buffer_size
        )
        self._done = False
        self._started = False
        self._error: Optional[Exception] = None

    @property
    def metrics(self) -> StreamMetrics:
        return self._metrics

    @property
    def result(self) -> StreamingResult:
        return self._result

    @property
    def is_done(self) -> bool:
        return self._done

    def _check_disconnect(self) -> bool:
        """Check if the consumer has disconnected."""
        if self._disconnect_check:
            try:
                return self._disconnect_check()
            except Exception:
                return True
        return False

    async def _process_stream(self) -> None:
        """Consume the raw stream and push deltas into the buffer."""
        try:
            # Emit start event
            start_delta = StreamingDelta(
                event_type=StreamEventType.START,
                model=self._model,
            )
            await self._buffer.put(start_delta)
            self._started = True

            async for chunk in self._stream:
                # Check for client disconnect
                if self._check_disconnect():
                    logger.info(f"[Stream:{self._stream_id}] Client disconnected, stopping stream")
                    break

                # Extract deltas from the chunk
                deltas = self._extract_deltas(chunk)

                for delta in deltas:
                    # Apply backpressure
                    if self._buffer.qsize() >= self._backpressure.high_watermark:
                        self._metrics.backpressure_events += 1
                        if self._backpressure.drop_oldest_on_overflow:
                            try:
                                self._buffer.get_nowait()
                                self._metrics.dropped_deltas += 1
                            except asyncio.QueueEmpty:
                                pass
                        else:
                            # Wait for buffer to drain
                            while self._buffer.qsize() > self._backpressure.low_watermark:
                                await asyncio.sleep(0.01)

                    await self._buffer.put(delta)

                    # Update metrics
                    self._metrics.total_deltas += 1
                    self._metrics.total_content_chars += len(delta.content)
                    self._metrics.total_thinking_chars += len(delta.thinking_content)
                    if delta.tool_call_id:
                        self._metrics.tool_calls_count += 1

                    # Apply to result
                    self._result.apply_delta(delta)

                    # Callback
                    if self._on_delta:
                        try:
                            self._on_delta(delta)
                        except Exception as cb_err:
                            logger.warning(f"[Stream:{self._stream_id}] Delta callback error: {cb_err}")

        except Exception as e:
            self._error = e
            self._metrics.error = str(e)[:200]
            logger.error(f"[Stream:{self._stream_id}] Stream error: {e}")

            error_delta = StreamingDelta(
                event_type=StreamEventType.ERROR,
                content=str(e)[:500],
            )
            await self._buffer.put(error_delta)

            if self._on_error:
                try:
                    self._on_error(e)
                except Exception as cb_err:
                    logger.warning(f"[Stream:{self._stream_id}] Error callback error: {cb_err}")

        finally:
            # Emit done event
            done_delta = StreamingDelta(event_type=StreamEventType.DONE)
            await self._buffer.put(done_delta)
            self._done = True
            self._metrics.end_time = time.time()
            self._result.duration_s = self._metrics.duration_s

            if self._on_done:
                try:
                    self._on_done(self._result)
                except Exception as cb_err:
                    logger.warning(f"[Stream:{self._stream_id}] Done callback error: {cb_err}")

            # Signal end of iteration
            await self._buffer.put(None)

    def _extract_deltas(self, chunk: Any) -> list[StreamingDelta]:
        """Extract StreamingDelta objects from a raw stream chunk.

        Handles OpenAI, Anthropic, and generic chunk formats.
        """
        deltas: list[StreamingDelta] = []

        # Handle dict-based chunks (OpenAI, Anthropic, litellm)
        if isinstance(chunk, dict):
            deltas.extend(self._extract_from_dict(chunk))
        elif hasattr(chunk, "model_dump"):
            # Pydantic model
            try:
                data = chunk.model_dump()
                deltas.extend(self._extract_from_dict(data))
            except Exception:
                pass
        elif hasattr(chunk, "choices"):
            # OpenAI SDK object
            try:
                for choice in chunk.choices:
                    delta_obj = getattr(choice, "delta", None)
                    if delta_obj:
                        content = getattr(delta_obj, "content", None) or ""
                        tool_calls = getattr(delta_obj, "tool_calls", None)

                        if content:
                            deltas.append(StreamingDelta(
                                content=content,
                                event_type=StreamEventType.DELTA,
                                model=self._model,
                            ))

                        if tool_calls:
                            for tc in tool_calls:
                                tc_id = getattr(tc, "id", None)
                                func = getattr(tc, "function", None)
                                tc_name = getattr(func, "name", None) if func else None
                                tc_args = getattr(func, "arguments", None) if func else ""

                                deltas.append(StreamingDelta(
                                    tool_call_id=tc_id or "",
                                    tool_call_name=tc_name or "",
                                    tool_call_arguments=tc_args or "",
                                    event_type=StreamEventType.TOOL_CALL_DELTA,
                                    model=self._model,
                                ))
            except Exception as e:
                logger.debug(f"[Stream:{self._stream_id}] Failed to extract from SDK object: {e}")

        elif hasattr(chunk, "delta"):
            # Anthropic SDK stream event
            delta_attr = chunk.delta
            if hasattr(delta_attr, "text"):
                text = delta_attr.text or ""
                if text:
                    deltas.append(StreamingDelta(
                        content=text,
                        event_type=StreamEventType.DELTA,
                        model=self._model,
                    ))
            if hasattr(delta_attr, "thinking"):
                thinking = delta_attr.thinking or ""
                if thinking:
                    deltas.append(StreamingDelta(
                        thinking_content=thinking,
                        event_type=StreamEventType.THINKING_DELTA,
                        model=self._model,
                    ))
            if hasattr(delta_attr, "partial_json"):
                partial_json = delta_attr.partial_json or ""
                if partial_json:
                    # Get tool call context from the event
                    tc_id = getattr(chunk, "id", "")
                    tc_name = ""
                    if hasattr(chunk, "name"):
                        tc_name = chunk.name or ""
                    deltas.append(StreamingDelta(
                        tool_call_id=tc_id,
                        tool_call_name=tc_name,
                        tool_call_arguments=partial_json,
                        event_type=StreamEventType.TOOL_CALL_DELTA,
                        model=self._model,
                    ))

        elif isinstance(chunk, str):
            # Raw text chunk
            deltas.append(StreamingDelta(
                content=chunk,
                event_type=StreamEventType.DELTA,
                model=self._model,
            ))

        return deltas

    def _extract_from_dict(self, data: dict[str, Any]) -> list[StreamingDelta]:
        """Extract deltas from a dictionary chunk."""
        deltas: list[StreamingDelta] = []

        # OpenAI format
        choices = data.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            content = delta.get("content")
            if content:
                deltas.append(StreamingDelta(
                    content=content,
                    event_type=StreamEventType.DELTA,
                    model=self._model,
                ))

            # Tool calls
            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                deltas.append(StreamingDelta(
                    tool_call_id=tc.get("id", ""),
                    tool_call_name=func.get("name", ""),
                    tool_call_arguments=func.get("arguments", ""),
                    event_type=StreamEventType.TOOL_CALL_DELTA,
                    model=self._model,
                ))

        # Anthropic format
        event_type = data.get("type", "")
        if event_type == "content_block_delta":
            delta_data = data.get("delta", {})
            if delta_data.get("type") == "text_delta":
                text = delta_data.get("text", "")
                if text:
                    deltas.append(StreamingDelta(
                        content=text,
                        event_type=StreamEventType.DELTA,
                        model=self._model,
                    ))
            elif delta_data.get("type") == "thinking_delta":
                thinking = delta_data.get("thinking", "")
                if thinking:
                    deltas.append(StreamingDelta(
                        thinking_content=thinking,
                        event_type=StreamEventType.THINKING_DELTA,
                        model=self._model,
                    ))
            elif delta_data.get("type") == "input_json_delta":
                partial_json = delta_data.get("partial_json", "")
                if partial_json:
                    tc_id = data.get("id", "")
                    deltas.append(StreamingDelta(
                        tool_call_id=tc_id,
                        tool_call_arguments=partial_json,
                        event_type=StreamEventType.TOOL_CALL_DELTA,
                        model=self._model,
                    ))

        # Usage data
        usage = data.get("usage")
        if usage:
            self._result.usage = usage

        return deltas

    async def __aiter__(self) -> AsyncIterator[StreamingDelta]:
        """Iterate over streaming deltas."""
        task = asyncio.create_task(self._process_stream())
        try:
            while True:
                delta = await self._buffer.get()
                if delta is None:
                    break
                yield delta
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ──────────────────────────────────────────────────────────────────────────────
# SSE Formatting
# ──────────────────────────────────────────────────────────────────────────────

def format_sse_event(data: Any, event: Optional[str] = None) -> str:
    """Format data as a Server-Sent Event string.

    Args:
        data: The data to send (will be JSON-encoded if not a string).
        event: Optional SSE event type.

    Returns:
        Formatted SSE string.
    """
    if not isinstance(data, str):
        data = json.dumps(data)

    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


async def stream_sse(
    stream: AsyncIterator[Any],
    model: str = "",
    on_delta: Optional[OnDeltaCallback] = None,
    on_done: Optional[OnDoneCallback] = None,
    on_error: Optional[OnErrorCallback] = None,
    disconnect_check: Optional[Callable[[], bool]] = None,
) -> AsyncIterator[str]:
    """Convert an LLM stream into SSE-formatted strings.

    This is the main entry point for web clients. It produces
    SSE-formatted strings that can be yielded directly from
    FastAPI/Starlette streaming endpoints.

    Usage with FastAPI::

        @app.get("/chat/stream")
        async def chat_stream():
            async def generate():
                stream = await llm.astream(messages)
                async for sse_event in stream_sse(stream):
                    yield sse_event
            return StreamingResponse(generate(), media_type="text/event-stream")

    Args:
        stream: Async iterator of raw LLM stream chunks.
        model: Model name for metadata.
        on_delta: Optional callback for each delta.
        on_done: Optional callback when stream completes.
        on_error: Optional callback on stream error.
        disconnect_check: Optional callable to check if client disconnected.

    Yields:
        SSE-formatted strings.
    """
    processor = AsyncStreamProcessor(
        stream=stream,
        on_delta=on_delta,
        on_done=on_done,
        on_error=on_error,
        model=model,
        disconnect_check=disconnect_check,
    )

    async for delta in processor:
        if delta.event_type == StreamEventType.START:
            yield format_sse_event(
                {"type": "start", "model": model, "stream_id": processor._stream_id},
                event="start",
            )
        elif delta.event_type == StreamEventType.DELTA:
            yield format_sse_event(delta.to_dict(), event="delta")
        elif delta.event_type == StreamEventType.THINKING_DELTA:
            yield format_sse_event(delta.to_dict(), event="thinking")
        elif delta.event_type == StreamEventType.TOOL_CALL_DELTA:
            yield format_sse_event(delta.to_dict(), event="tool_call")
        elif delta.event_type == StreamEventType.USAGE:
            yield format_sse_event(delta.to_dict(), event="usage")
        elif delta.event_type == StreamEventType.DONE:
            result_dict = processor.result.to_openai_response()
            yield format_sse_event(
                {"type": "done", "result": result_dict},
                event="done",
            )
        elif delta.event_type == StreamEventType.ERROR:
            yield format_sse_event(
                {"type": "error", "error": delta.content},
                event="error",
            )


# ──────────────────────────────────────────────────────────────────────────────
# Sync Stream Processor
# ──────────────────────────────────────────────────────────────────────────────

class SyncStreamProcessor:
    """Synchronous stream processor for non-async contexts.

    Usage::

        processor = SyncStreamProcessor(stream=my_sync_stream)
        for delta in processor:
            print(delta.content, end="", flush=True)
    """

    def __init__(
        self,
        stream: Iterator[Any],
        on_delta: Optional[OnDeltaCallback] = None,
        on_done: Optional[OnDoneCallback] = None,
        on_error: Optional[OnErrorCallback] = None,
        model: str = "",
    ) -> None:
        self._stream = stream
        self._on_delta = on_delta
        self._on_done = on_done
        self._on_error = on_error
        self._model = model
        self._stream_id = str(uuid.uuid4())[:12]
        self._result = StreamingResult(model=model)
        self._metrics = StreamMetrics(
            stream_id=self._stream_id,
            model=model,
            start_time=time.time(),
        )
        self._done = False
        self._started = False

    @property
    def metrics(self) -> StreamMetrics:
        return self._metrics

    @property
    def result(self) -> StreamingResult:
        return self._result

    def __iter__(self) -> Iterator[StreamingDelta]:
        try:
            # Emit start event
            yield StreamingDelta(
                event_type=StreamEventType.START,
                model=self._model,
            )
            self._started = True

            for chunk in self._stream:
                deltas = self._extract_deltas(chunk)
                for delta in deltas:
                    self._metrics.total_deltas += 1
                    self._metrics.total_content_chars += len(delta.content)
                    self._metrics.total_thinking_chars += len(delta.thinking_content)
                    if delta.tool_call_id:
                        self._metrics.tool_calls_count += 1

                    self._result.apply_delta(delta)

                    if self._on_delta:
                        try:
                            self._on_delta(delta)
                        except Exception:
                            pass

                    yield delta

        except Exception as e:
            self._metrics.error = str(e)[:200]
            yield StreamingDelta(
                event_type=StreamEventType.ERROR,
                content=str(e)[:500],
            )
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception:
                    pass

        finally:
            yield StreamingDelta(event_type=StreamEventType.DONE)
            self._done = True
            self._metrics.end_time = time.time()
            self._result.duration_s = self._metrics.duration_s
            if self._on_done:
                try:
                    self._on_done(self._result)
                except Exception:
                    pass

    def _extract_deltas(self, chunk: Any) -> list[StreamingDelta]:
        """Extract deltas from a sync chunk. Same logic as async version."""
        deltas: list[StreamingDelta] = []
        if isinstance(chunk, dict):
            choices = chunk.get("choices", [])
            for choice in choices:
                delta = choice.get("delta", {})
                content = delta.get("content")
                if content:
                    deltas.append(StreamingDelta(
                        content=content,
                        event_type=StreamEventType.DELTA,
                        model=self._model,
                    ))
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    func = tc.get("function", {})
                    deltas.append(StreamingDelta(
                        tool_call_id=tc.get("id", ""),
                        tool_call_name=func.get("name", ""),
                        tool_call_arguments=func.get("arguments", ""),
                        event_type=StreamEventType.TOOL_CALL_DELTA,
                        model=self._model,
                    ))
        elif isinstance(chunk, str):
            deltas.append(StreamingDelta(
                content=chunk,
                event_type=StreamEventType.DELTA,
                model=self._model,
            ))
        return deltas


# ──────────────────────────────────────────────────────────────────────────────
# Convenience Functions
# ──────────────────────────────────────────────────────────────────────────────

async def stream_async(
    stream: AsyncIterator[Any],
    on_delta: Optional[OnDeltaCallback] = None,
    model: str = "",
) -> AsyncIterator[StreamingDelta]:
    """Convenience function for async streaming.

    Simplifies the most common streaming pattern.
    """
    processor = AsyncStreamProcessor(
        stream=stream,
        on_delta=on_delta,
        model=model,
    )
    async for delta in processor:
        yield delta


def stream_sync(
    stream: Iterator[Any],
    on_delta: Optional[OnDeltaCallback] = None,
    model: str = "",
) -> Iterator[StreamingDelta]:
    """Convenience function for sync streaming."""
    processor = SyncStreamProcessor(
        stream=stream,
        on_delta=on_delta,
        model=model,
    )
    for delta in processor:
        yield delta
