from __future__ import annotations

"""
LLM Metrics — Cost Tracking, Token Counting, and Latency Monitoring
====================================================================

Provides comprehensive metrics for LLM operations:

- Per-conversation cost tracking with provider-specific pricing
- Token counting (prompt + completion + cache + reasoning)
- Latency tracking with percentile calculations (p50, p95, p99)
- Provider-specific cost calculation
- Budget tracking with alerts
- Metrics export in Prometheus format
- Thread-safe metrics collection

Typical usage::

    metrics = LLMMetrics()

    # Record a call
    metrics.record_call(
        conversation_id="conv-123",
        model="gpt-4o",
        provider="openai",
        prompt_tokens=1000,
        completion_tokens=500,
        latency_s=2.5,
    )

    # Check budget
    metrics.check_budget("conv-123", budget_usd=10.0)

    # Export
    print(metrics.to_prometheus())
"""

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Provider Pricing Tables (USD per 1M tokens)
# ──────────────────────────────────────────────────────────────────────────────

# Prices as of 2025. Update as providers change their pricing.
PRICING_TABLE: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00, "cache_read": 5.00},
    "gpt-4": {"input": 30.00, "output": 60.00, "cache_read": 15.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50, "cache_read": 0.25},
    "o1": {"input": 15.00, "output": 60.00, "cache_read": 7.50},
    "o1-mini": {"input": 3.00, "output": 12.00, "cache_read": 1.50},
    "o3-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.55},

    # Anthropic
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
    "claude-3-opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-3-sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-haiku": {"input": 0.25, "output": 1.25, "cache_read": 0.03, "cache_write": 0.30},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},

    # Google
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00, "cache_read": 0.315},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cache_read": 0.0188},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "cache_read": 0.025},

    # Mistral
    "mistral-large": {"input": 2.00, "output": 6.00},
    "mistral-medium": {"input": 0.70, "output": 2.10},
    "mistral-small": {"input": 0.20, "output": 0.60},

    # DeepSeek
    "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_read": 0.014},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_read": 0.14},
}

# Default pricing for unknown models
DEFAULT_PRICING: dict[str, float] = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write": 3.75,
}


def get_model_pricing(model: str) -> dict[str, float]:
    """Get pricing for a model, matching by prefix if exact match not found.

    Args:
        model: The model name (e.g., "gpt-4o-2024-08-06").

    Returns:
        Dict with pricing per 1M tokens for input, output, cache_read, cache_write.
    """
    # Exact match
    if model in PRICING_TABLE:
        return PRICING_TABLE[model]

    # Prefix match (e.g., "gpt-4o-2024-08-06" matches "gpt-4o")
    model_lower = model.lower()
    for key, pricing in PRICING_TABLE.items():
        if model_lower.startswith(key.lower()):
            return pricing

    # Date suffix removal (e.g., "claude-3-5-sonnet-20241022" -> "claude-3-5-sonnet")
    parts = model.rsplit("-", 1)
    if len(parts) > 1 and parts[-1].isdigit():
        prefix = parts[0]
        if prefix in PRICING_TABLE:
            return PRICING_TABLE[prefix]

    return DEFAULT_PRICING


def calculate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    """Calculate the cost in USD for a single LLM call.

    Args:
        model: The model name.
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.
        cache_read_tokens: Number of cache-read tokens.
        cache_write_tokens: Number of cache-write tokens.
        reasoning_tokens: Number of reasoning tokens (charged at output rate).

    Returns:
        Cost in USD.
    """
    pricing = get_model_pricing(model)
    cost = 0.0
    cost += (input_tokens / 1_000_000) * pricing.get("input", 0)
    cost += (output_tokens / 1_000_000) * pricing.get("output", 0)
    cost += (cache_read_tokens / 1_000_000) * pricing.get("cache_read", 0)
    cost += (cache_write_tokens / 1_000_000) * pricing.get("cache_write", 0)
    # Reasoning tokens are typically charged at output rate
    cost += (reasoning_tokens / 1_000_000) * pricing.get("output", 0)
    return cost


# ──────────────────────────────────────────────────────────────────────────────
# Latency Histogram
# ──────────────────────────────────────────────────────────────────────────────

class LatencyHistogram:
    """Simple latency histogram for tracking response times.

    Uses fixed bucket boundaries and supports percentile calculations.
    Thread-safe.
    """

    # Bucket boundaries in seconds
    BUCKETS = [
        0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0,
        7.5, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0,
        120.0, 180.0, 300.0, 600.0, float("inf"),
    ]

    def __init__(self) -> None:
        self._counts: list[int] = [0] * len(self.BUCKETS)
        self._total: float = 0.0
        self._count: int = 0
        self._min: float = float("inf")
        self._max: float = 0.0
        self._sum_sq: float = 0.0
        self._lock = threading.Lock()

    def record(self, value: float) -> None:
        """Record a latency value."""
        with self._lock:
            self._count += 1
            self._total += value
            self._sum_sq += value * value
            if value < self._min:
                self._min = value
            if value > self._max:
                self._max = value

            for i, boundary in enumerate(self.BUCKETS):
                if value <= boundary:
                    self._counts[i] += 1
                    break

    def percentile(self, p: float) -> float:
        """Calculate the given percentile (0-100).

        Uses linear interpolation between bucket boundaries.
        Returns 0.0 if no values have been recorded.
        """
        with self._lock:
            if self._count == 0:
                return 0.0

            target = (p / 100.0) * self._count
            cumulative = 0

            for i, boundary in enumerate(self.BUCKETS):
                cumulative += self._counts[i]
                if cumulative >= target:
                    # Linear interpolation within the bucket
                    prev_cumulative = cumulative - self._counts[i]
                    prev_boundary = self.BUCKETS[i - 1] if i > 0 else 0.0
                    if self._counts[i] == 0:
                        return prev_boundary

                    fraction = (target - prev_cumulative) / self._counts[i]
                    return prev_boundary + fraction * (boundary - prev_boundary)

            return self.BUCKETS[-2]  # Last finite bucket

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def mean(self) -> float:
        with self._lock:
            return self._total / self._count if self._count > 0 else 0.0

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "count": self._count,
                "min": round(self._min, 3) if self._count > 0 else 0,
                "max": round(self._max, 3) if self._count > 0 else 0,
                "mean": round(self.mean, 3),
                "p50": round(self.p50, 3),
                "p95": round(self.p95, 3),
                "p99": round(self.p99, 3),
            }


# ──────────────────────────────────────────────────────────────────────────────
# Conversation Metrics
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ConversationMetrics:
    """Tracks metrics for a single conversation."""

    conversation_id: str
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0
    models_used: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def avg_latency_s(self) -> float:
        return self.total_latency_s / self.total_calls if self.total_calls > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_latency_s": round(self.avg_latency_s, 3),
            "models_used": dict(self.models_used),
            "errors": self.errors,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Budget Alert
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BudgetAlert:
    """A budget alert triggered when spending exceeds a threshold."""

    conversation_id: str
    budget_usd: float
    spent_usd: float
    threshold_pct: float
    timestamp: float = field(default_factory=time.time)

    @property
    def percentage(self) -> float:
        return (self.spent_usd / self.budget_usd * 100) if self.budget_usd > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "budget_usd": round(self.budget_usd, 4),
            "spent_usd": round(self.spent_usd, 4),
            "threshold_pct": round(self.threshold_pct, 1),
            "actual_pct": round(self.percentage, 1),
            "timestamp": self.timestamp,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Global LLM Metrics
# ──────────────────────────────────────────────────────────────────────────────

class LLMMetrics:
    """Central metrics collector for all LLM operations.

    Thread-safe singleton-like class that tracks:
    - Per-conversation cost and token usage
    - Per-model latency histograms
    - Global cost totals
    - Budget alerts

    Usage::

        metrics = LLMMetrics()

        metrics.record_call(
            conversation_id="conv-123",
            model="gpt-4o",
            provider="openai",
            prompt_tokens=1000,
            completion_tokens=500,
            latency_s=2.5,
        )

        # Get conversation metrics
        conv_metrics = metrics.get_conversation("conv-123")

        # Export Prometheus format
        prom_text = metrics.to_prometheus()
    """

    def __init__(self) -> None:
        self._conversations: dict[str, ConversationMetrics] = {}
        self._global_latency = LatencyHistogram()
        self._model_latencies: dict[str, LatencyHistogram] = {}
        self._model_token_counts: dict[str, dict[str, int]] = {}
        self._global_cost_usd: float = 0.0
        self._global_calls: int = 0
        self._global_errors: int = 0
        self._alerts: list[BudgetAlert] = []
        self._budget_callbacks: list[Callable[[BudgetAlert], None]] = []
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────
    # Recording
    # ──────────────────────────────────────────────────────────────────────

    def record_call(
        self,
        conversation_id: str,
        model: str,
        provider: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        latency_s: float = 0.0,
        is_error: bool = False,
    ) -> float:
        """Record a single LLM API call.

        Args:
            conversation_id: Unique identifier for the conversation.
            model: The model name used.
            provider: The provider name.
            prompt_tokens: Number of input/prompt tokens.
            completion_tokens: Number of output/completion tokens.
            cache_read_tokens: Number of cache-read tokens.
            cache_write_tokens: Number of cache-write tokens.
            reasoning_tokens: Number of reasoning tokens.
            latency_s: The request latency in seconds.
            is_error: Whether the call resulted in an error.

        Returns:
            The calculated cost in USD for this call.
        """
        cost = calculate_cost(
            model=model,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )

        with self._lock:
            # Update conversation metrics
            if conversation_id not in self._conversations:
                self._conversations[conversation_id] = ConversationMetrics(
                    conversation_id=conversation_id
                )
            conv = self._conversations[conversation_id]
            conv.total_calls += 1
            conv.total_input_tokens += prompt_tokens
            conv.total_output_tokens += completion_tokens
            conv.total_cache_read_tokens += cache_read_tokens
            conv.total_cache_write_tokens += cache_write_tokens
            conv.total_reasoning_tokens += reasoning_tokens
            conv.total_cost_usd += cost
            conv.total_latency_s += latency_s
            if is_error:
                conv.errors += 1
            conv.models_used[model] = conv.models_used.get(model, 0) + 1

            # Update global metrics
            self._global_calls += 1
            self._global_cost_usd += cost
            if is_error:
                self._global_errors += 1

            # Update latency histograms
            if latency_s > 0:
                self._global_latency.record(latency_s)
                if model not in self._model_latencies:
                    self._model_latencies[model] = LatencyHistogram()
                self._model_latencies[model].record(latency_s)

            # Update per-model token counts
            if model not in self._model_token_counts:
                self._model_token_counts[model] = {
                    "input": 0, "output": 0, "cache_read": 0,
                    "cache_write": 0, "reasoning": 0,
                }
            tc = self._model_token_counts[model]
            tc["input"] += prompt_tokens
            tc["output"] += completion_tokens
            tc["cache_read"] += cache_read_tokens
            tc["cache_write"] += cache_write_tokens
            tc["reasoning"] += reasoning_tokens

        if cost > 0:
            logger.debug(
                f"[LLMMetrics] Call recorded: conv={conversation_id} "
                f"model={model} tokens={prompt_tokens}+{completion_tokens} "
                f"cost=${cost:.6f} latency={latency_s:.2f}s"
            )

        return cost

    def record_usage_dict(
        self,
        conversation_id: str,
        model: str,
        usage: dict[str, int],
        provider: str = "",
        latency_s: float = 0.0,
        is_error: bool = False,
    ) -> float:
        """Record a call from a usage dict (as returned by LLM APIs).

        Args:
            conversation_id: Unique identifier for the conversation.
            model: The model name.
            usage: Dict with token counts (prompt_tokens, completion_tokens, etc.)
            provider: The provider name.
            latency_s: The request latency in seconds.
            is_error: Whether the call resulted in an error.

        Returns:
            The calculated cost in USD.
        """
        return self.record_call(
            conversation_id=conversation_id,
            model=model,
            provider=provider,
            prompt_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            completion_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_s=latency_s,
            is_error=is_error,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Budget Management
    # ──────────────────────────────────────────────────────────────────────

    def check_budget(
        self,
        conversation_id: str,
        budget_usd: float,
        alert_thresholds: Optional[list[float]] = None,
    ) -> bool:
        """Check if a conversation is within budget.

        Args:
            conversation_id: The conversation to check.
            budget_usd: The budget in USD.
            alert_thresholds: List of percentage thresholds (0-100) to trigger alerts.
                             Defaults to [50, 80, 90, 100].

        Returns:
            True if within budget, False if budget exceeded.
        """
        thresholds = alert_thresholds or [50, 80, 90, 100]

        with self._lock:
            conv = self._conversations.get(conversation_id)
            if not conv:
                return True

            spent = conv.total_cost_usd
            pct = (spent / budget_usd * 100) if budget_usd > 0 else 0

            # Check thresholds (only trigger once per threshold)
            for threshold in sorted(thresholds):
                if pct >= threshold:
                    # Check if we already alerted at this threshold
                    already_alerted = any(
                        a.conversation_id == conversation_id
                        and a.budget_usd == budget_usd
                        and a.threshold_pct == threshold
                        for a in self._alerts
                    )
                    if not already_alerted:
                        alert = BudgetAlert(
                            conversation_id=conversation_id,
                            budget_usd=budget_usd,
                            spent_usd=spent,
                            threshold_pct=threshold,
                        )
                        self._alerts.append(alert)
                        logger.warning(
                            f"[LLMMetrics] Budget alert: conv={conversation_id} "
                            f"spent=${spent:.4f}/{budget_usd:.4f} ({pct:.1f}%) "
                            f"threshold={threshold}%"
                        )
                        for callback in self._budget_callbacks:
                            try:
                                callback(alert)
                            except Exception as e:
                                logger.warning(f"[LLMMetrics] Budget callback error: {e}")

            return spent < budget_usd

    def add_budget_callback(self, callback: Callable[[BudgetAlert], None]) -> None:
        """Add a callback to be invoked when a budget alert is triggered."""
        with self._lock:
            self._budget_callbacks.append(callback)

    # ──────────────────────────────────────────────────────────────────────
    # Querying
    # ──────────────────────────────────────────────────────────────────────

    def get_conversation(self, conversation_id: str) -> Optional[ConversationMetrics]:
        """Get metrics for a specific conversation."""
        with self._lock:
            return self._conversations.get(conversation_id)

    def get_model_latency(self, model: str) -> Optional[LatencyHistogram]:
        """Get the latency histogram for a specific model."""
        with self._lock:
            return self._model_latencies.get(model)

    def get_model_tokens(self, model: str) -> Optional[dict[str, int]]:
        """Get token counts for a specific model."""
        with self._lock:
            return self._model_token_counts.get(model)

    @property
    def global_cost_usd(self) -> float:
        """Total cost across all conversations."""
        with self._lock:
            return self._global_cost_usd

    @property
    def global_calls(self) -> int:
        """Total number of calls across all conversations."""
        with self._lock:
            return self._global_calls

    @property
    def global_errors(self) -> int:
        """Total number of errors across all conversations."""
        with self._lock:
            return self._global_errors

    @property
    def global_latency(self) -> LatencyHistogram:
        """Global latency histogram."""
        return self._global_latency

    @property
    def alerts(self) -> list[BudgetAlert]:
        """All budget alerts."""
        with self._lock:
            return list(self._alerts)

    # ──────────────────────────────────────────────────────────────────────
    # Export
    # ──────────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Export all metrics as a dictionary."""
        with self._lock:
            return {
                "global": {
                    "total_calls": self._global_calls,
                    "total_errors": self._global_errors,
                    "total_cost_usd": round(self._global_cost_usd, 4),
                    "latency": self._global_latency.to_dict(),
                },
                "conversations": {
                    cid: cm.to_dict()
                    for cid, cm in self._conversations.items()
                },
                "models": {
                    model: {
                        "latency": hist.to_dict(),
                        "tokens": dict(self._model_token_counts.get(model, {})),
                    }
                    for model, hist in self._model_latencies.items()
                },
                "alerts": [a.to_dict() for a in self._alerts[-20:]],
            }

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus exposition format.

        Returns a string suitable for serving on a /metrics endpoint.
        """
        with self._lock:
            lines: list[str] = []

            # Global metrics
            lines.append("# HELP llm_calls_total Total number of LLM API calls")
            lines.append("# TYPE llm_calls_total counter")
            lines.append(f"llm_calls_total {self._global_calls}")

            lines.append("# HELP llm_errors_total Total number of LLM API errors")
            lines.append("# TYPE llm_errors_total counter")
            lines.append(f"llm_errors_total {self._global_errors}")

            lines.append("# HELP llm_cost_usd_total Total cost in USD")
            lines.append("# TYPE llm_cost_usd_total counter")
            lines.append(f"llm_cost_usd_total {self._global_cost_usd:.6f}")

            # Per-model metrics
            for model, hist in self._model_latencies.items():
                safe_model = model.replace("-", "_").replace(".", "_")
                lines.append(f'# HELP llm_latency_seconds Latency for model "{model}"')
                lines.append("# TYPE llm_latency_seconds summary")
                lines.append(f'llm_latency_seconds{{model="{safe_model}",quantile="0.5"}} {hist.p50:.3f}')
                lines.append(f'llm_latency_seconds{{model="{safe_model}",quantile="0.95"}} {hist.p95:.3f}')
                lines.append(f'llm_latency_seconds{{model="{safe_model}",quantile="0.99"}} {hist.p99:.3f}')
                lines.append(f'llm_latency_seconds_sum{{model="{safe_model}"}} {hist._total:.3f}')
                lines.append(f'llm_latency_seconds_count{{model="{safe_model}"}} {hist.count}')

            for model, tc in self._model_token_counts.items():
                safe_model = model.replace("-", "_").replace(".", "_")
                lines.append("# HELP llm_tokens_total Token usage per model")
                lines.append("# TYPE llm_tokens_total counter")
                lines.append(f'llm_input_tokens_total{{model="{safe_model}"}} {tc.get("input", 0)}')
                lines.append(f'llm_output_tokens_total{{model="{safe_model}"}} {tc.get("output", 0)}')
                lines.append(f'llm_cache_read_tokens_total{{model="{safe_model}"}} {tc.get("cache_read", 0)}')
                lines.append(f'llm_cache_write_tokens_total{{model="{safe_model}"}} {tc.get("cache_write", 0)}')
                lines.append(f'llm_reasoning_tokens_total{{model="{safe_model}"}} {tc.get("reasoning", 0)}')

            # Per-conversation cost
            for cid, conv in self._conversations.items():
                safe_cid = cid.replace("-", "_").replace(" ", "_")
                lines.append("# HELP llm_conversation_cost_usd Cost per conversation")
                lines.append("# TYPE llm_conversation_cost_usd gauge")
                lines.append(f'llm_conversation_cost_usd{{conversation="{safe_cid}"}} {conv.total_cost_usd:.6f}')
                lines.append("# HELP llm_conversation_calls Total calls per conversation")
                lines.append("# TYPE llm_conversation_calls gauge")
                lines.append(f'llm_conversation_calls{{conversation="{safe_cid}"}} {conv.total_calls}')

            return "\n".join(lines) + "\n"

    def summary(self) -> str:
        """Return a human-readable summary."""
        with self._lock:
            return (
                f"LLMMetrics: calls={self._global_calls} "
                f"errors={self._global_errors} "
                f"cost=${self._global_cost_usd:.4f} "
                f"conversations={len(self._conversations)} "
                f"models={len(self._model_latencies)} "
                f"p50={self._global_latency.p50:.2f}s "
                f"p95={self._global_latency.p95:.2f}s "
                f"p99={self._global_latency.p99:.2f}s"
            )

    # ──────────────────────────────────────────────────────────────────────
    # Management
    # ──────────────────────────────────────────────────────────────────────

    def clear_conversation(self, conversation_id: str) -> None:
        """Clear metrics for a specific conversation."""
        with self._lock:
            self._conversations.pop(conversation_id, None)

    def clear_all(self) -> None:
        """Clear all metrics."""
        with self._lock:
            self._conversations.clear()
            self._model_latencies.clear()
            self._model_token_counts.clear()
            self._alerts.clear()
            self._global_cost_usd = 0.0
            self._global_calls = 0
            self._global_errors = 0
            self._global_latency = LatencyHistogram()

    def prune_old_conversations(self, max_age_s: float = 86400) -> int:
        """Remove conversation metrics older than max_age_s.

        Args:
            max_age_s: Maximum age in seconds (default 24 hours).

        Returns:
            Number of pruned conversations.
        """
        cutoff = time.time() - max_age_s
        pruned = 0
        with self._lock:
            to_remove = [
                cid for cid, conv in self._conversations.items()
                if conv.created_at < cutoff
            ]
            for cid in to_remove:
                del self._conversations[cid]
                pruned += 1
        return pruned


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_global_metrics: Optional[LLMMetrics] = None
_metrics_lock = threading.Lock()


def get_metrics() -> LLMMetrics:
    """Get the global LLMMetrics instance (lazy singleton)."""
    global _global_metrics
    with _metrics_lock:
        if _global_metrics is None:
            _global_metrics = LLMMetrics()
        return _global_metrics


def reset_metrics() -> None:
    """Reset the global LLMMetrics instance."""
    global _global_metrics
    with _metrics_lock:
        if _global_metrics is not None:
            _global_metrics.clear_all()
        _global_metrics = None
