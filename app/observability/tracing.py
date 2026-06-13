"""
OpenTelemetry Tracing Integration
===================================

Provides the ``@observe`` decorator that wraps functions with
OpenTelemetry spans.  When ``opentelemetry`` is not installed, all
operations degrade gracefully to no-ops.

Features:
    - ``@observe`` decorator for both sync and async functions
    - Trace context propagation via ``contextvars``
    - Custom span attributes for agent-specific data (model, tokens,
      tool_name)
    - Automatic error recording on exceptions
    - Configurable service name and tracer provider

Usage::

    from app.observability.tracing import observe, set_span_attribute

    @observe("llm_call")
    async def call_llm(prompt: str, model: str = "gpt-4o"):
        set_span_attribute("llm.model", model)
        ...

    @observe("tool_execution", attributes={"tool.category": "file"})
    def execute_tool(name: str, args: dict):
        ...
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import time
from contextvars import ContextVar
from typing import Any, Callable, Optional, Union

# ---------------------------------------------------------------------------
# Try importing OpenTelemetry — graceful degradation if missing
# ---------------------------------------------------------------------------

_HAS_OTEL = False
_tracer = None

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Span, SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource

    _HAS_OTEL = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

# Use a separate logger namespace to avoid inheriting the manusclaw root
# logger's handlers (which require ContextFilter attributes like 'agent'
# and 'trace_id' that child loggers won't have set on their records).
_logger = logging.getLogger("manusclaw_observability.tracing")

# ---------------------------------------------------------------------------
# Current span context variable
# ---------------------------------------------------------------------------

_current_span: ContextVar[Optional[Any]] = ContextVar(
    "manusclaw_current_span", default=None
)

# ---------------------------------------------------------------------------
# Tracer initialization
# ---------------------------------------------------------------------------

_SERVICE_NAME = os.getenv("MANUSCLAW_SERVICE_NAME", "manusclaw")
_tracer_name = "manusclaw.tracer"


def init_tracing(
    service_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    exporter_type: str = "otlp",
) -> Optional[Any]:
    """Initialize the OpenTelemetry tracer provider.

    Args:
        service_name: Service name for traces. Defaults to
            ``MANUSCLAW_SERVICE_NAME`` env var or ``"manusclaw"``.
        endpoint: OTLP exporter endpoint. If ``None``, uses the
            ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var. If neither is set,
            tracing outputs to the console.
        exporter_type: Exporter type — ``"otlp"`` or ``"console"``.

    Returns:
        The ``TracerProvider`` if OpenTelemetry is available, else ``None``.
    """
    global _tracer, _HAS_OTEL

    if not _HAS_OTEL:
        _logger.debug("OpenTelemetry not installed; tracing disabled")
        return None

    name = service_name or _SERVICE_NAME
    resource = Resource.create({"service.name": name})
    provider = TracerProvider(resource=resource)

    if exporter_type == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            otlp_endpoint = endpoint or os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
            )
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except ImportError:
            _logger.warning(
                "OTLP exporter not available; falling back to console exporter"
            )
            exporter_type = "console"

    if exporter_type == "console":
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        except ImportError:
            _logger.warning("Console exporter not available either; tracing is no-op")

    _otel_trace.set_tracer_provider(provider)
    _tracer = _otel_trace.get_tracer(_tracer_name)
    _logger.info("Tracing initialized: service=%s exporter=%s", name, exporter_type)
    return provider


def get_tracer() -> Optional[Any]:
    """Return the current OpenTelemetry tracer, or ``None`` if unavailable."""
    global _tracer
    if _tracer is not None:
        return _tracer
    if _HAS_OTEL:
        try:
            _tracer = _otel_trace.get_tracer(_tracer_name)
            return _tracer
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Span attribute helpers
# ---------------------------------------------------------------------------

def set_span_attribute(key: str, value: Any) -> None:
    """Set an attribute on the current active span.

    If no span is active or OpenTelemetry is not installed, this is a no-op.

    Args:
        key: Attribute key string.
        value: Attribute value — must be a string, bool, int, or float.
    """
    span = _current_span.get()
    if span is None:
        return
    if _HAS_OTEL and isinstance(span, Span):
        try:
            # Coerce to a type that OTel accepts
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
            else:
                span.set_attribute(key, str(value))
        except Exception:
            pass


def set_span_attributes(attributes: dict[str, Any]) -> None:
    """Set multiple attributes on the current active span.

    Args:
        attributes: Dict of key-value pairs to set as span attributes.
    """
    for key, value in attributes.items():
        set_span_attribute(key, value)


def get_current_span() -> Optional[Any]:
    """Return the current active span from the context variable."""
    return _current_span.get()


# ---------------------------------------------------------------------------
# Trace context propagation
# ---------------------------------------------------------------------------

def inject_trace_context() -> dict[str, str]:
    """Inject the current trace context into a dict for propagation.

    Returns:
        A dict with W3C Trace Context headers, or an empty dict if
        tracing is not available.
    """
    if not _HAS_OTEL:
        return {}
    carrier: dict[str, str] = {}
    try:
        TraceContextTextMapPropagator().inject(carrier)
    except Exception:
        pass
    return carrier


def extract_trace_context(carrier: dict[str, str]) -> Optional[Any]:
    """Extract a trace context from a carrier dict.

    Args:
        carrier: Dict with W3C Trace Context headers.

    Returns:
        The extracted context, or ``None`` if unavailable.
    """
    if not _HAS_OTEL:
        return None
    try:
        return TraceContextTextMapPropagator().extract(carrier)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# @observe decorator
# ---------------------------------------------------------------------------

def observe(
    name: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
    kind: Optional[str] = None,
    record_exception: bool = True,
) -> Callable:
    """Decorator that wraps a function with an OpenTelemetry span.

    Works with both sync and async functions.  When OpenTelemetry is
    not installed, it falls back to simple timing + logging.

    Args:
        name: Span name. Defaults to ``"{module}.{function_name}"``.
        attributes: Dict of static span attributes to set on start.
        kind: Span kind — ``"internal"`` (default), ``"server"``,
            ``"client"``, ``"producer"``, ``"consumer"``.
        record_exception: Whether to record exceptions as span events.

    Returns:
        The decorated function.

    Example::

        @observe("tool_execution", attributes={"tool.category": "file"})
        async def run_tool(name: str, args: dict):
            ...
    """
    static_attrs = attributes or {}

    def _resolve_span_kind() -> Any:
        if not _HAS_OTEL or kind is None:
            return None
        kind_map = {
            "internal": SpanKind.INTERNAL,
            "server": SpanKind.SERVER,
            "client": SpanKind.CLIENT,
            "producer": SpanKind.PRODUCER,
            "consumer": SpanKind.CONSUMER,
        }
        return kind_map.get(kind.lower(), SpanKind.INTERNAL)

    def decorator(func: Callable) -> Callable:
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        span_kind = _resolve_span_kind()
        is_async = inspect.iscoroutinefunction(func)

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _trace_call_async(
                    func, args, kwargs, span_name, static_attrs,
                    span_kind, record_exception,
                )
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                return _trace_call_sync(
                    func, args, kwargs, span_name, static_attrs,
                    span_kind, record_exception,
                )
            return sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# Internal trace call implementations
# ---------------------------------------------------------------------------

async def _trace_call_async(
    func: Callable,
    args: tuple,
    kwargs: dict,
    span_name: str,
    static_attrs: dict[str, Any],
    span_kind: Any,
    record_exception: bool,
) -> Any:
    """Execute an async function within an OpenTelemetry span."""
    tracer = get_tracer()

    if tracer is None or not _HAS_OTEL:
        # Graceful degradation — no OTEL, just time and log
        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as exc:
            duration = time.monotonic() - start
            _logger.debug(
                "observe(%s) failed after %.3fs: %s",
                span_name, duration, exc,
            )
            raise

    start = time.monotonic()
    kind = span_kind or SpanKind.INTERNAL

    with tracer.start_as_current_span(span_name, kind=kind) as span:
        token = _current_span.set(span)

        # Set static attributes
        for key, value in static_attrs.items():
            set_span_attribute(key, value)

        # Auto-set agent-specific attributes from kwargs
        _auto_set_agent_attrs(span, kwargs)

        try:
            result = await func(*args, **kwargs)
            span.set_status(Status(StatusCode.OK))
            return result
        except Exception as exc:
            duration = time.monotonic() - start
            if record_exception:
                span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)[:512]))
            _logger.debug(
                "observe(%s) failed after %.3fs: %s",
                span_name, duration, exc,
            )
            raise
        finally:
            duration = time.monotonic() - start
            set_span_attribute("duration_ms", round(duration * 1000, 2))
            _current_span.reset(token)


def _trace_call_sync(
    func: Callable,
    args: tuple,
    kwargs: dict,
    span_name: str,
    static_attrs: dict[str, Any],
    span_kind: Any,
    record_exception: bool,
) -> Any:
    """Execute a sync function within an OpenTelemetry span."""
    tracer = get_tracer()

    if tracer is None or not _HAS_OTEL:
        start = time.monotonic()
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            duration = time.monotonic() - start
            _logger.debug(
                "observe(%s) failed after %.3fs: %s",
                span_name, duration, exc,
            )
            raise

    start = time.monotonic()
    kind = span_kind or SpanKind.INTERNAL

    with tracer.start_as_current_span(span_name, kind=kind) as span:
        token = _current_span.set(span)

        for key, value in static_attrs.items():
            set_span_attribute(key, value)

        _auto_set_agent_attrs(span, kwargs)

        try:
            result = func(*args, **kwargs)
            span.set_status(Status(StatusCode.OK))
            return result
        except Exception as exc:
            duration = time.monotonic() - start
            if record_exception:
                span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)[:512]))
            _logger.debug(
                "observe(%s) failed after %.3fs: %s",
                span_name, duration, exc,
            )
            raise
        finally:
            duration = time.monotonic() - start
            set_span_attribute("duration_ms", round(duration * 1000, 2))
            _current_span.reset(token)


def _auto_set_agent_attrs(span: Any, kwargs: dict) -> None:
    """Automatically set agent-specific span attributes from kwargs.

    Detects common agent framework kwargs like ``model``, ``tokens``,
    ``tool_name``, ``session_id`` and sets them as span attributes.
    """
    _agent_attr_map = {
        "model": "llm.model",
        "model_name": "llm.model",
        "tokens": "llm.tokens",
        "token_count": "llm.tokens",
        "tool_name": "tool.name",
        "session_id": "session.id",
        "agent_name": "agent.name",
        "step": "agent.step",
        "step_number": "agent.step",
    }
    for kwarg_key, span_key in _agent_attr_map.items():
        if kwarg_key in kwargs:
            set_span_attribute(span_key, kwargs[kwarg_key])


# ---------------------------------------------------------------------------
# Convenience: check if tracing is available
# ---------------------------------------------------------------------------

def is_tracing_available() -> bool:
    """Return ``True`` if OpenTelemetry is installed and a tracer is configured."""
    return _HAS_OTEL and get_tracer() is not None
