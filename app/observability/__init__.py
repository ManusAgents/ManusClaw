"""
ManusClaw Observability Subsystem
===================================

Enterprise-grade observability for the manusclaw agent framework,
inspired by OpenHands's observability architecture.

Modules:
    - **correlation** — Request-scoped correlation IDs for distributed tracing
    - **tracing** — OpenTelemetry tracing with ``@observe`` decorator
    - **metrics** — Prometheus-compatible counters, histograms, and gauges
    - **logging_utils** — Structured JSON logging with redaction
    - **health** — Kubernetes-style liveness/readiness health probes

Quick start::

    from app.observability import (
        observe,
        get_correlation_id,
        set_correlation_id,
        CorrelationContext,
        get_metrics,
        get_health_system,
        get_structured_logger,
    )

    # Decorate a function for tracing
    @observe("my_function")
    async def my_function():
        ...

    # Set up correlation for a request
    with CorrelationContext():
        ...

    # Get metrics
    metrics = get_metrics()

    # Check health
    health = get_health_system()
    liveness = health.liveness()
    readiness = health.readiness()
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Correlation — always available (no optional deps)
# ---------------------------------------------------------------------------

from app.observability.correlation import (
    CorrelationContext,
    get_all_context_ids,
    get_correlation_id,
    get_error_id,
    get_parent_span_id,
    get_request_id,
    new_correlation_id,
    new_error_id,
    new_request_id,
    set_correlation_id,
    set_error_id,
    set_parent_span_id,
    set_request_id,
)

# ---------------------------------------------------------------------------
# Tracing — gracefully degrades when opentelemetry is not installed
# ---------------------------------------------------------------------------

from app.observability.tracing import (
    extract_trace_context,
    get_current_span,
    get_tracer,
    init_tracing,
    inject_trace_context,
    is_tracing_available,
    observe,
    set_span_attribute,
    set_span_attributes,
)

# ---------------------------------------------------------------------------
# Metrics — gracefully degrades when prometheus_client is not installed
# ---------------------------------------------------------------------------

from app.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    active_conversations,
    conversation_duration_seconds,
    error_count_total,
    generate_prometheus_output,
    get_metrics,
    inc_counter,
    is_prometheus_available,
    llm_call_duration_seconds,
    llm_calls_total,
    observe_histogram,
    register_counter,
    register_gauge,
    register_histogram,
    set_gauge,
    token_usage_total,
    tool_call_duration_seconds,
    tool_calls_total,
)

# ---------------------------------------------------------------------------
# Logging — structured JSON logging utilities
# ---------------------------------------------------------------------------

from app.observability.logging_utils import (
    StructuredJsonFormatter,
    StructuredTextFormatter,
    apply_module_levels,
    bind_logger_context,
    configure_structured_logging,
    get_module_levels,
    get_structured_logger,
    set_module_level,
)

# ---------------------------------------------------------------------------
# Health — liveness / readiness probes
# ---------------------------------------------------------------------------

from app.observability.health import (
    ComponentHealth,
    DatabaseHealthChecker,
    HealthCheckSystem,
    HealthChecker,
    HealthStatus,
    LLMHealthChecker,
    SandboxHealthChecker,
    get_health_system,
)

__all__ = [
    # Correlation
    "CorrelationContext",
    "get_all_context_ids",
    "get_correlation_id",
    "get_error_id",
    "get_parent_span_id",
    "get_request_id",
    "new_correlation_id",
    "new_error_id",
    "new_request_id",
    "set_correlation_id",
    "set_error_id",
    "set_parent_span_id",
    "set_request_id",
    # Tracing
    "extract_trace_context",
    "get_current_span",
    "get_tracer",
    "init_tracing",
    "inject_trace_context",
    "is_tracing_available",
    "observe",
    "set_span_attribute",
    "set_span_attributes",
    # Metrics
    "Counter",
    "Gauge",
    "Histogram",
    "active_conversations",
    "conversation_duration_seconds",
    "error_count_total",
    "generate_prometheus_output",
    "get_metrics",
    "inc_counter",
    "is_prometheus_available",
    "llm_call_duration_seconds",
    "llm_calls_total",
    "observe_histogram",
    "register_counter",
    "register_gauge",
    "register_histogram",
    "set_gauge",
    "token_usage_total",
    "tool_call_duration_seconds",
    "tool_calls_total",
    # Logging
    "StructuredJsonFormatter",
    "StructuredTextFormatter",
    "apply_module_levels",
    "bind_logger_context",
    "configure_structured_logging",
    "get_module_levels",
    "get_structured_logger",
    "set_module_level",
    # Health
    "ComponentHealth",
    "DatabaseHealthChecker",
    "HealthCheckSystem",
    "HealthChecker",
    "HealthStatus",
    "LLMHealthChecker",
    "SandboxHealthChecker",
    "get_health_system",
]
