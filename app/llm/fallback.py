from __future__ import annotations

"""
FallbackStrategy — Configurable Model Fallback Chains
======================================================

Provides intelligent model fallback when the primary model is unavailable
or returns specific error types. Features include:

- Configurable fallback chains: [primary_model, fallback1, fallback2, ...]
- Per-model cooldown periods after failures
- Automatic fallback on specific error types (rate limit, context window, service unavailable)
- Fallback metrics tracking
- Thread-safe fallback state management
- Fallback event callbacks for monitoring

Typical usage::

    strategy = FallbackStrategy(
        chain=["claude-3-5-sonnet", "gpt-4o", "gemini-1.5-pro"],
        cooldown_s=60.0,
    )
    result = await strategy.execute(my_llm_call_func)
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.exceptions import (
    ManusClawError,
    RateLimitError,
    RetryableError,
    NonRetryableError,
    TokenLimitExceeded,
)
from app.logger import logger
from app.llm.retry import ErrorCategory, classify_error, is_retryable


# ──────────────────────────────────────────────────────────────────────────────
# Fallback Triggers
# ──────────────────────────────────────────────────────────────────────────────

class FallbackTrigger(str, Enum):
    """Error categories that trigger a fallback to the next model."""

    RATE_LIMIT = "rate_limit"
    SERVICE_UNAVAILABLE = "service_unavailable"
    CONTEXT_WINDOW = "context_window"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    QUOTA = "quota"
    ALL_ERRORS = "all_errors"  # Fallback on any error


# Default: fallback on these error categories
DEFAULT_FALLBACK_TRIGGERS: set[FallbackTrigger] = {
    FallbackTrigger.RATE_LIMIT,
    FallbackTrigger.SERVICE_UNAVAILABLE,
    FallbackTrigger.CONTEXT_WINDOW,
    FallbackTrigger.QUOTA,
}

# Mapping from ErrorCategory to FallbackTrigger
_ERROR_TO_FALLBACK: dict[ErrorCategory, FallbackTrigger] = {
    ErrorCategory.RATE_LIMIT: FallbackTrigger.RATE_LIMIT,
    ErrorCategory.SERVICE_UNAVAILABLE: FallbackTrigger.SERVICE_UNAVAILABLE,
    ErrorCategory.CONTEXT_WINDOW: FallbackTrigger.CONTEXT_WINDOW,
    ErrorCategory.SERVER_ERROR: FallbackTrigger.SERVER_ERROR,
    ErrorCategory.TIMEOUT: FallbackTrigger.TIMEOUT,
    ErrorCategory.CONNECTION: FallbackTrigger.CONNECTION,
    ErrorCategory.QUOTA: FallbackTrigger.QUOTA,
}


# ──────────────────────────────────────────────────────────────────────────────
# Model State
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ModelState:
    """Tracks the state of a single model in the fallback chain."""

    model: str
    cooldown_until: float = 0.0
    failure_count: int = 0
    success_count: int = 0
    last_error: Optional[str] = None
    last_error_time: float = 0.0

    @property
    def is_available(self) -> bool:
        """Check if the model is not in cooldown."""
        return time.monotonic() >= self.cooldown_until

    def mark_failed(self, error: str, cooldown_s: float) -> None:
        """Mark this model as failed and start cooldown."""
        self.failure_count += 1
        self.last_error = error
        self.last_error_time = time.monotonic()
        self.cooldown_until = time.monotonic() + cooldown_s

    def mark_success(self) -> None:
        """Mark this model as having succeeded."""
        self.success_count += 1
        self.cooldown_until = 0.0  # Clear cooldown on success


# ──────────────────────────────────────────────────────────────────────────────
# Fallback Metrics
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FallbackMetrics:
    """Tracks metrics for fallback strategy execution."""

    total_calls: int = 0
    primary_successes: int = 0
    fallback_successes: int = 0
    all_failed: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_primary_success(self, model: str) -> None:
        with self._lock:
            self.total_calls += 1
            self.primary_successes += 1
            self._inc_model(model, "success")

    def record_fallback_success(self, primary: str, fallback: str) -> None:
        with self._lock:
            self.total_calls += 1
            self.fallback_successes += 1
            self._inc_model(fallback, "success")
            self._inc_model(primary, "fallback_from")

    def record_all_failed(self, models: list[str]) -> None:
        with self._lock:
            self.total_calls += 1
            self.all_failed += 1
            for m in models:
                self._inc_model(m, "failure")

    def record_fallback_event(
        self,
        from_model: str,
        to_model: str,
        trigger: str,
        error: str,
    ) -> None:
        with self._lock:
            self.fallback_events.append({
                "timestamp": time.time(),
                "from_model": from_model,
                "to_model": to_model,
                "trigger": trigger,
                "error": error[:200],  # Truncate long errors
            })
            # Keep only last 100 events to prevent unbounded memory growth
            if len(self.fallback_events) > 100:
                self.fallback_events = self.fallback_events[-100:]

    def _inc_model(self, model: str, key: str) -> None:
        if model not in self.by_model:
            self.by_model[model] = {"success": 0, "failure": 0, "fallback_from": 0}
        self.by_model[model][key] = self.by_model[model].get(key, 0) + 1

    @property
    def primary_success_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.primary_successes / self.total_calls

    @property
    def fallback_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.fallback_successes / self.total_calls

    def summary(self) -> str:
        return (
            f"FallbackMetrics: calls={self.total_calls} "
            f"primary_rate={self.primary_success_rate:.1%} "
            f"fallback_rate={self.fallback_rate:.1%} "
            f"all_failed={self.all_failed}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "primary_successes": self.primary_successes,
            "fallback_successes": self.fallback_successes,
            "all_failed": self.all_failed,
            "primary_success_rate": round(self.primary_success_rate, 4),
            "fallback_rate": round(self.fallback_rate, 4),
            "by_model": dict(self.by_model),
            "recent_events": self.fallback_events[-10:],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Fallback Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FallbackConfig:
    """Configuration for the fallback strategy.

    Attributes:
        chain: Ordered list of model names, first is primary.
        cooldown_s: Default cooldown period in seconds after a model fails.
        cooldown_multiplier: Multiply cooldown by this factor for repeated failures.
        max_cooldown_s: Maximum cooldown period in seconds.
        triggers: Set of FallbackTrigger values that trigger fallback.
        per_model_cooldown: Override cooldown per model name.
    """

    chain: list[str] = field(default_factory=lambda: ["gpt-4o", "claude-3-5-sonnet"])
    cooldown_s: float = 60.0
    cooldown_multiplier: float = 2.0
    max_cooldown_s: float = 600.0
    triggers: set[FallbackTrigger] = field(default_factory=lambda: DEFAULT_FALLBACK_TRIGGERS)
    per_model_cooldown: dict[str, float] = field(default_factory=dict)

    def get_cooldown(self, model: str, failure_count: int) -> float:
        """Get the cooldown period for a model after a failure.

        Applies exponential backoff on repeated failures.
        """
        base = self.per_model_cooldown.get(model, self.cooldown_s)
        cooldown = base * (self.cooldown_multiplier ** (failure_count))
        return min(cooldown, self.max_cooldown_s)


# ──────────────────────────────────────────────────────────────────────────────
# Fallback Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FallbackResult:
    """Result from a fallback strategy execution."""

    value: Any
    model_used: str
    was_fallback: bool
    primary_model: str
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.value is not None


# ──────────────────────────────────────────────────────────────────────────────
# FallbackStrategy
# ──────────────────────────────────────────────────────────────────────────────

class FallbackStrategy:
    """Intelligent model fallback with configurable chains and cooldown.

    Thread-safe implementation for use in async multi-agent environments.

    Usage::

        strategy = FallbackStrategy(
            config=FallbackConfig(
                chain=["claude-3-5-sonnet", "gpt-4o", "gemini-1.5-pro"],
            ),
        )

        async def call_model(model: str) -> dict:
            # Your LLM call implementation
            ...

        result = await strategy.execute(call_model)
        print(f"Used model: {result.model_used}")
    """

    def __init__(
        self,
        config: Optional[FallbackConfig] = None,
        on_fallback: Optional[Callable[[str, str, str, str], None]] = None,
    ) -> None:
        self._config = config or FallbackConfig()
        self._metrics = FallbackMetrics()
        self._on_fallback = on_fallback
        self._lock = threading.Lock()

        # Initialize model states
        self._model_states: dict[str, ModelState] = {}
        for model in self._config.chain:
            self._model_states[model] = ModelState(model=model)

    @property
    def metrics(self) -> FallbackMetrics:
        """Access the fallback metrics."""
        return self._metrics

    @property
    def config(self) -> FallbackConfig:
        """Access the fallback configuration."""
        return self._config

    @property
    def chain(self) -> list[str]:
        """Return the current fallback chain."""
        return list(self._config.chain)

    def get_available_models(self) -> list[str]:
        """Return models in the chain that are currently available (not in cooldown)."""
        return [m for m in self._config.chain if self._model_states.get(m, ModelState(model=m)).is_available]

    def get_model_state(self, model: str) -> Optional[ModelState]:
        """Get the state of a specific model."""
        return self._model_states.get(model)

    def reset_model(self, model: str) -> None:
        """Reset a model's state, clearing cooldown and failure counts."""
        with self._lock:
            if model in self._model_states:
                self._model_states[model] = ModelState(model=model)

    def reset_all(self) -> None:
        """Reset all model states."""
        with self._lock:
            for model in self._config.chain:
                self._model_states[model] = ModelState(model=model)

    def _should_fallback(self, error: Exception) -> bool:
        """Determine if the error should trigger a fallback."""
        if FallbackTrigger.ALL_ERRORS in self._config.triggers:
            return True

        category = classify_error(error)
        trigger = _ERROR_TO_FALLBACK.get(category)
        if trigger and trigger in self._config.triggers:
            return True

        return False

    async def execute(
        self,
        func: Callable[[str], Any],
        model_kwargs: Optional[dict[str, Any]] = None,
    ) -> FallbackResult:
        """Execute a function with fallback across the model chain.

        The function receives the model name as its first argument and
        optionally additional keyword arguments.

        Args:
            func: Async callable that takes a model name and returns a result.
            model_kwargs: Optional additional keyword arguments per model name.

        Returns:
            FallbackResult with the value and metadata about which model was used.

        Raises:
            The last exception if all models in the chain fail.
        """
        import asyncio

        chain = self._config.chain
        if not chain:
            raise NonRetryableError("FallbackStrategy: empty model chain")

        primary_model = chain[0]
        attempts: list[dict[str, Any]] = []
        last_error: Optional[Exception] = None

        for i, model in enumerate(chain):
            state = self._model_states.get(model)
            if state and not state.is_available:
                remaining = state.cooldown_until - time.monotonic()
                logger.debug(
                    f"[FallbackStrategy] Skipping {model} (cooldown {remaining:.0f}s remaining)"
                )
                attempts.append({
                    "model": model,
                    "status": "skipped",
                    "reason": f"cooldown ({remaining:.0f}s remaining)",
                })
                continue

            try:
                kwargs = {}
                if model_kwargs and model in model_kwargs:
                    kwargs = model_kwargs[model]

                result = await func(model, **kwargs)

                # Success
                with self._lock:
                    if state:
                        state.mark_success()

                if i == 0:
                    self._metrics.record_primary_success(model)
                else:
                    self._metrics.record_fallback_success(primary_model, model)

                attempts.append({
                    "model": model,
                    "status": "success",
                    "attempt_index": i,
                })

                return FallbackResult(
                    value=result,
                    model_used=model,
                    was_fallback=(i > 0),
                    primary_model=primary_model,
                    attempts=attempts,
                )

            except Exception as e:
                last_error = e
                error_category = classify_error(e).value

                with self._lock:
                    if state:
                        cooldown = self._config.get_cooldown(model, state.failure_count)
                        state.mark_failed(str(e)[:200], cooldown)

                attempts.append({
                    "model": model,
                    "status": "failed",
                    "error": str(e)[:200],
                    "error_category": error_category,
                    "attempt_index": i,
                })

                should_fallback = self._should_fallback(e)

                logger.warning(
                    f"[FallbackStrategy] Model {model} failed "
                    f"(category={error_category}, fallback={should_fallback}): {e}"
                )

                if not should_fallback:
                    # Non-fallback error — propagate immediately
                    raise e

                # Record fallback event
                next_model = chain[i + 1] if i + 1 < len(chain) else "(none)"
                self._metrics.record_fallback_event(
                    from_model=model,
                    to_model=next_model,
                    trigger=error_category,
                    error=str(e)[:200],
                )

                if self._on_fallback:
                    try:
                        self._on_fallback(model, next_model, error_category, str(e)[:200])
                    except Exception as callback_err:
                        logger.warning(f"[FallbackStrategy] on_fallback callback error: {callback_err}")

        # All models failed
        self._metrics.record_all_failed(chain)

        if last_error:
            raise last_error
        raise NonRetryableError(
            f"FallbackStrategy: all {len(chain)} models failed or are in cooldown"
        )

    def execute_sync(
        self,
        func: Callable[[str], Any],
        model_kwargs: Optional[dict[str, Any]] = None,
    ) -> FallbackResult:
        """Synchronous version of execute for non-async contexts."""
        chain = self._config.chain
        if not chain:
            raise NonRetryableError("FallbackStrategy: empty model chain")

        primary_model = chain[0]
        attempts: list[dict[str, Any]] = []
        last_error: Optional[Exception] = None

        for i, model in enumerate(chain):
            state = self._model_states.get(model)
            if state and not state.is_available:
                remaining = state.cooldown_until - time.monotonic()
                attempts.append({
                    "model": model,
                    "status": "skipped",
                    "reason": f"cooldown ({remaining:.0f}s remaining)",
                })
                continue

            try:
                kwargs = {}
                if model_kwargs and model in model_kwargs:
                    kwargs = model_kwargs[model]

                result = func(model, **kwargs)

                with self._lock:
                    if state:
                        state.mark_success()

                if i == 0:
                    self._metrics.record_primary_success(model)
                else:
                    self._metrics.record_fallback_success(primary_model, model)

                attempts.append({
                    "model": model,
                    "status": "success",
                    "attempt_index": i,
                })

                return FallbackResult(
                    value=result,
                    model_used=model,
                    was_fallback=(i > 0),
                    primary_model=primary_model,
                    attempts=attempts,
                )

            except Exception as e:
                last_error = e
                error_category = classify_error(e).value

                with self._lock:
                    if state:
                        cooldown = self._config.get_cooldown(model, state.failure_count)
                        state.mark_failed(str(e)[:200], cooldown)

                attempts.append({
                    "model": model,
                    "status": "failed",
                    "error": str(e)[:200],
                    "error_category": error_category,
                    "attempt_index": i,
                })

                should_fallback = self._should_fallback(e)
                if not should_fallback:
                    raise e

                next_model = chain[i + 1] if i + 1 < len(chain) else "(none)"
                self._metrics.record_fallback_event(
                    from_model=model,
                    to_model=next_model,
                    trigger=error_category,
                    error=str(e)[:200],
                )

        self._metrics.record_all_failed(chain)

        if last_error:
            raise last_error
        raise NonRetryableError(
            f"FallbackStrategy: all {len(chain)} models failed or are in cooldown"
        )
