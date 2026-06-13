from __future__ import annotations

"""
Enhanced Retry Logic — Exponential Backoff with Provider-Specific Error Mapping
================================================================================

Provides production-grade retry logic for LLM API calls with:
- Exponential backoff with jitter (full or decorrelated)
- Provider-specific error classification and mapping
- Retry budget (total elapsed time limit)
- Per-provider retryable error detection
- Comprehensive retry metrics
- map_provider_exception() for normalising provider-specific errors

Thread-safe and safe for async contexts.
"""

import random
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Type

from app.exceptions import (
    ManusClawError,
    RateLimitError,
    RetryableError,
    NonRetryableError,
    TokenLimitExceeded,
    LLMAuthError,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Error Classification
# ──────────────────────────────────────────────────────────────────────────────

class ErrorCategory(str, Enum):
    """Classification of LLM API errors for retry decisions."""

    RATE_LIMIT = "rate_limit"           # 429 — retryable with backoff
    CONTEXT_WINDOW = "context_window"   # Token limit exceeded — not retryable
    AUTH = "auth"                       # 401/403 — not retryable
    SERVICE_UNAVAILABLE = "service_unavailable"  # 503 — retryable
    SERVER_ERROR = "server_error"       # 500/502/504 — retryable
    TIMEOUT = "timeout"                 # Request timeout — retryable
    CONNECTION = "connection"           # Network/connection error — retryable
    BAD_REQUEST = "bad_request"         # 400 — not retryable
    VALIDATION = "validation"           # Invalid input — not retryable
    QUOTA = "quota"                     # Account quota exceeded — not retryable
    CONTENT_FILTER = "content_filter"   # Content policy violation — not retryable
    UNKNOWN = "unknown"                 # Unclassified — treat as non-retryable


# Mapping from HTTP status codes to error categories
_STATUS_CODE_MAP: dict[int, ErrorCategory] = {
    400: ErrorCategory.BAD_REQUEST,
    401: ErrorCategory.AUTH,
    403: ErrorCategory.AUTH,
    404: ErrorCategory.BAD_REQUEST,
    408: ErrorCategory.TIMEOUT,
    429: ErrorCategory.RATE_LIMIT,
    500: ErrorCategory.SERVER_ERROR,
    502: ErrorCategory.SERVER_ERROR,
    503: ErrorCategory.SERVICE_UNAVAILABLE,
    504: ErrorCategory.SERVER_ERROR,
    529: ErrorCategory.RATE_LIMIT,  # Overloaded (Anthropic-specific)
}

# Retryable categories
_RETRYABLE_CATEGORIES: set[ErrorCategory] = {
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVICE_UNAVAILABLE,
    ErrorCategory.SERVER_ERROR,
    ErrorCategory.TIMEOUT,
    ErrorCategory.CONNECTION,
}


def classify_error(error: Exception, provider: str = "") -> ErrorCategory:
    """Classify an exception into an ErrorCategory.

    Inspects the exception type, message content, and any HTTP status
    code attributes to determine the appropriate category.

    Args:
        error: The exception to classify.
        provider: Optional provider name for provider-specific heuristics.

    Returns:
        The ErrorCategory for this error.
    """
    # Check for manusclaw-specific exceptions first
    if isinstance(error, TokenLimitExceeded):
        return ErrorCategory.CONTEXT_WINDOW
    if isinstance(error, RateLimitError):
        return ErrorCategory.RATE_LIMIT
    if isinstance(error, LLMAuthError):
        return ErrorCategory.AUTH
    if isinstance(error, NonRetryableError):
        return ErrorCategory.UNKNOWN
    if isinstance(error, RetryableError):
        return ErrorCategory.SERVER_ERROR

    # Check for HTTP status code on the exception
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "http_status", None)
    if status_code is None:
        # Some SDKs nest the status code
        resp = getattr(error, "response", None)
        if resp is not None:
            status_code = getattr(resp, "status_code", None)

    if status_code and isinstance(status_code, int):
        category = _STATUS_CODE_MAP.get(status_code)
        if category:
            return category

    # Fallback: inspect error message for keywords
    msg = str(error).lower()
    if any(kw in msg for kw in ("rate limit", "rate_limit", "too many requests", "429")):
        return ErrorCategory.RATE_LIMIT
    if any(kw in msg for kw in ("context window", "token limit", "max_tokens", "context_length")):
        return ErrorCategory.CONTEXT_WINDOW
    if any(kw in msg for kw in ("unauthorized", "invalid api key", "authentication", "forbidden")):
        return ErrorCategory.AUTH
    if any(kw in msg for kw in ("timeout", "timed out", "deadline exceeded")):
        return ErrorCategory.TIMEOUT
    if any(kw in msg for kw in ("connection", "network", "reset", "broken pipe", "refused")):
        return ErrorCategory.CONNECTION
    if any(kw in msg for kw in ("service unavailable", "overloaded", "capacity")):
        return ErrorCategory.SERVICE_UNAVAILABLE
    if any(kw in msg for kw in ("bad request", "invalid", "validation")):
        return ErrorCategory.BAD_REQUEST
    if any(kw in msg for kw in ("quota", "billing", "limit exceeded")):
        return ErrorCategory.QUOTA
    if any(kw in msg for kw in ("content_filter", "content policy", "safety")):
        return ErrorCategory.CONTENT_FILTER

    # Provider-specific heuristics
    provider_lower = provider.lower()
    if provider_lower == "anthropic":
        if "overloaded" in msg:
            return ErrorCategory.SERVICE_UNAVAILABLE
    elif provider_lower == "openai":
        if "insufficient_quota" in msg:
            return ErrorCategory.QUOTA
    elif provider_lower == "google":
        if "resource_exhausted" in msg:
            return ErrorCategory.RATE_LIMIT

    return ErrorCategory.UNKNOWN


def is_retryable(error: Exception, provider: str = "") -> bool:
    """Determine if an error is retryable.

    Args:
        error: The exception to check.
        provider: Optional provider name for provider-specific heuristics.

    Returns:
        True if the error is retryable, False otherwise.
    """
    category = classify_error(error, provider)
    return category in _RETRYABLE_CATEGORIES


# ──────────────────────────────────────────────────────────────────────────────
# Provider-Specific Exception Mapping
# ──────────────────────────────────────────────────────────────────────────────

def map_provider_exception(
    error: Exception,
    provider: str = "",
    model: str = "",
) -> ManusClawError:
    """Map a provider-specific exception to a manusclaw exception.

    This function inspects the raw exception from a provider SDK and
    converts it to the appropriate manusclaw exception type, preserving
    the original error as the cause.

    Args:
        error: The raw exception from the provider.
        provider: The provider name (e.g., "openai", "anthropic").
        model: The model name (for context in error messages).

    Returns:
        A ManusClawError subclass instance.
    """
    category = classify_error(error, provider)
    msg = str(error)
    if model:
        msg = f"[{model}] {msg}"

    if category == ErrorCategory.RATE_LIMIT:
        # Extract retry-after if available
        retry_after = _extract_retry_after(error)
        return RateLimitError(message=msg, wait_s=retry_after)

    if category == ErrorCategory.CONTEXT_WINDOW:
        return TokenLimitExceeded(msg)

    if category == ErrorCategory.AUTH:
        return LLMAuthError(msg)

    if category in _RETRYABLE_CATEGORIES:
        return RetryableError(message=msg, wait_s=2.0)

    return NonRetryableError(msg)


def _extract_retry_after(error: Exception) -> float:
    """Try to extract Retry-After value from an exception.

    Checks common attributes where SDKs store the retry-after header.
    """
    # Check direct attribute
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass

    # Check headers
    resp = getattr(error, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", {})
        if headers:
            ra = headers.get("retry-after") or headers.get("Retry-After")
            if ra:
                try:
                    return float(ra)
                except (TypeError, ValueError):
                    pass

    # Check error message for common patterns
    msg = str(error).lower()
    import re
    match = re.search(r"retry.?after[:\s]+(\d+(?:\.\d+)?)", msg)
    if match:
        return float(match.group(1))

    match = re.search(r"wait[:\s]+(\d+(?:\.\d+)?)\s*s", msg)
    if match:
        return float(match.group(1))

    return 5.0  # Default backoff for rate limits


# ──────────────────────────────────────────────────────────────────────────────
# Backoff Strategies
# ──────────────────────────────────────────────────────────────────────────────

class BackoffStrategy(str, Enum):
    """Backoff strategy for retry waits."""

    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    EXPONENTIAL_JITTER = "exponential_jitter"
    DECORRELATED_JITTER = "decorrelated_jitter"


# ──────────────────────────────────────────────────────────────────────────────
# Retry Metrics
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryMetrics:
    """Tracks retry-related metrics for monitoring and debugging."""

    total_attempts: int = 0
    total_retries: int = 0
    total_retry_time_s: float = 0.0
    successes_on_first_try: int = 0
    successes_after_retry: int = 0
    exhausted_retries: int = 0
    budget_exhausted: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_provider: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_attempt(self, succeeded: bool, retries: int = 0,
                       retry_time_s: float = 0.0, category: str = "",
                       provider: str = "") -> None:
        """Record the outcome of a retry sequence."""
        with self._lock:
            self.total_attempts += 1
            self.total_retries += retries
            self.total_retry_time_s += retry_time_s

            if succeeded and retries == 0:
                self.successes_on_first_try += 1
            elif succeeded:
                self.successes_after_retry += 1
            else:
                self.exhausted_retries += 1

            if category:
                self.by_category[category] = self.by_category.get(category, 0) + 1
            if provider:
                self.by_provider[provider] = self.by_provider.get(provider, 0) + 1

    def record_budget_exhausted(self) -> None:
        """Record that the retry budget was exhausted."""
        with self._lock:
            self.budget_exhausted += 1

    @property
    def first_try_success_rate(self) -> float:
        """Percentage of calls that succeed on the first try."""
        if self.total_attempts == 0:
            return 0.0
        return self.successes_on_first_try / self.total_attempts

    @property
    def overall_success_rate(self) -> float:
        """Percentage of calls that eventually succeed."""
        if self.total_attempts == 0:
            return 0.0
        return (self.successes_on_first_try + self.successes_after_retry) / self.total_attempts

    def summary(self) -> str:
        """Return a human-readable summary of retry metrics."""
        return (
            f"RetryMetrics: attempts={self.total_attempts} "
            f"first_try_rate={self.first_try_success_rate:.1%} "
            f"overall_rate={self.overall_success_rate:.1%} "
            f"total_retries={self.total_retries} "
            f"retry_time={self.total_retry_time_s:.1f}s "
            f"budget_exhausted={self.budget_exhausted}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return metrics as a dictionary."""
        return {
            "total_attempts": self.total_attempts,
            "total_retries": self.total_retries,
            "total_retry_time_s": round(self.total_retry_time_s, 3),
            "successes_on_first_try": self.successes_on_first_try,
            "successes_after_retry": self.successes_after_retry,
            "exhausted_retries": self.exhausted_retries,
            "budget_exhausted": self.budget_exhausted,
            "first_try_success_rate": round(self.first_try_success_rate, 4),
            "overall_success_rate": round(self.overall_success_rate, 4),
            "by_category": dict(self.by_category),
            "by_provider": dict(self.by_provider),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Retry Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    """Configuration for retry behaviour.

    Attributes:
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_wait_s: Base wait time in seconds for backoff calculation.
        max_wait_s: Maximum wait time in seconds between retries.
        strategy: Backoff strategy to use.
        retry_budget_s: Total time budget for all retries (0 = no budget).
        provider: Provider name for error classification.
    """

    max_retries: int = 8
    base_wait_s: float = 1.0
    max_wait_s: float = 60.0
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL_JITTER
    retry_budget_s: float = 300.0  # 5-minute default budget
    provider: str = ""

    def compute_wait(self, attempt: int, last_wait: float = 0.0) -> float:
        """Compute the wait time for the given attempt number.

        Args:
            attempt: The current attempt number (1-indexed).
            last_wait: The previous wait time (used for decorrelated jitter).

        Returns:
            Wait time in seconds.
        """
        if self.strategy == BackoffStrategy.FIXED:
            return self.base_wait_s

        if self.strategy == BackoffStrategy.LINEAR:
            wait = self.base_wait_s * attempt
            return min(wait, self.max_wait_s)

        if self.strategy == BackoffStrategy.EXPONENTIAL:
            wait = self.base_wait_s * (2 ** (attempt - 1))
            return min(wait, self.max_wait_s)

        if self.strategy == BackoffStrategy.EXPONENTIAL_JITTER:
            wait = self.base_wait_s * (2 ** (attempt - 1))
            jitter = random.uniform(0, wait * 0.5)
            return min(wait + jitter, self.max_wait_s)

        if self.strategy == BackoffStrategy.DECORRELATED_JITTER:
            # AWS-style: sleep = min(cap, random_between(base, sleep * 3))
            if last_wait == 0.0:
                last_wait = self.base_wait_s
            wait = random.uniform(self.base_wait_s, last_wait * 3)
            return min(wait, self.max_wait_s)

        # Default: exponential with jitter
        wait = self.base_wait_s * (2 ** (attempt - 1))
        return min(wait, self.max_wait_s)


# ──────────────────────────────────────────────────────────────────────────────
# Retry Executor
# ──────────────────────────────────────────────────────────────────────────────

class RetryExecutor:
    """Executes async functions with configurable retry logic.

    Thread-safe and designed for async contexts. Tracks comprehensive
    metrics for monitoring and debugging.

    Usage::

        executor = RetryExecutor(RetryConfig(max_retries=5))
        result = await executor.execute(my_async_func, arg1, arg2)
    """

    def __init__(self, config: Optional[RetryConfig] = None) -> None:
        self._config = config or RetryConfig()
        self._metrics = RetryMetrics()

    @property
    def metrics(self) -> RetryMetrics:
        """Access the retry metrics."""
        return self._metrics

    @property
    def config(self) -> RetryConfig:
        """Access the retry configuration."""
        return self._config

    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        retry_on: Optional[set[ErrorCategory]] = None,
        on_retry: Optional[Callable[[int, Exception, float], None]] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute an async function with retry logic.

        Args:
            func: The async function to execute.
            *args: Positional arguments to pass to the function.
            retry_on: Set of ErrorCategory values to retry on.
                     Defaults to standard retryable categories.
            on_retry: Optional callback called before each retry with
                     (attempt, error, wait_seconds).
            **kwargs: Keyword arguments to pass to the function.

        Returns:
            The return value of the function.

        Raises:
            The last exception if all retries are exhausted.
            NonRetryableError if the error is classified as non-retryable.
        """
        import asyncio

        retryable_categories = retry_on or _RETRYABLE_CATEGORIES
        max_retries = self._config.max_retries
        budget_s = self._config.retry_budget_s

        last_wait = 0.0
        retry_start = time.monotonic()
        retries = 0
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            # Check retry budget
            if budget_s > 0 and retries > 0:
                elapsed = time.monotonic() - retry_start
                if elapsed >= budget_s:
                    logger.warning(
                        f"[RetryExecutor] Retry budget exhausted ({elapsed:.1f}s >= {budget_s:.1f}s) "
                        f"after {retries} retries"
                    )
                    self._metrics.record_budget_exhausted()
                    if last_error:
                        raise map_provider_exception(
                            last_error, self._config.provider
                        ) from last_error
                    raise NonRetryableError("Retry budget exhausted")

            try:
                result = await func(*args, **kwargs)
                # Success
                retry_time = time.monotonic() - retry_start if retries > 0 else 0.0
                self._metrics.record_attempt(
                    succeeded=True,
                    retries=retries,
                    retry_time_s=retry_time,
                    provider=self._config.provider,
                )
                return result

            except Exception as e:
                last_error = e
                category = classify_error(e, self._config.provider)

                # Check if this error is retryable
                if category not in retryable_categories:
                    logger.debug(
                        f"[RetryExecutor] Non-retryable error (category={category.value}): {e}"
                    )
                    self._metrics.record_attempt(
                        succeeded=False,
                        retries=retries,
                        category=category.value,
                        provider=self._config.provider,
                    )
                    raise map_provider_exception(
                        e, self._config.provider
                    ) from e

                # Check if we have retries left
                if attempt >= max_retries:
                    logger.warning(
                        f"[RetryExecutor] Exhausted {max_retries} retries "
                        f"(category={category.value}): {e}"
                    )
                    retry_time = time.monotonic() - retry_start
                    self._metrics.record_attempt(
                        succeeded=False,
                        retries=retries,
                        retry_time_s=retry_time,
                        category=category.value,
                        provider=self._config.provider,
                    )
                    raise map_provider_exception(
                        e, self._config.provider
                    ) from e

                # Compute wait time
                wait = self._config.compute_wait(attempt + 1, last_wait)
                last_wait = wait
                retries += 1

                # Extract retry-after from rate limit errors
                if category == ErrorCategory.RATE_LIMIT:
                    retry_after = _extract_retry_after(e)
                    wait = max(wait, retry_after)

                logger.info(
                    f"[RetryExecutor] Retry {retries}/{max_retries} after "
                    f"{category.value} error. Waiting {wait:.1f}s: {e}"
                )

                if on_retry:
                    on_retry(retries, e, wait)

                await asyncio.sleep(wait)

        # Should not reach here, but just in case
        if last_error:
            raise map_provider_exception(last_error, self._config.provider) from last_error
        raise NonRetryableError("Retry executor reached unreachable state")


class SyncRetryExecutor:
    """Synchronous version of RetryExecutor for non-async contexts.

    Usage::

        executor = SyncRetryExecutor(RetryConfig(max_retries=3))
        result = executor.execute(my_sync_func, arg1, arg2)
    """

    def __init__(self, config: Optional[RetryConfig] = None) -> None:
        self._config = config or RetryConfig()
        self._metrics = RetryMetrics()

    @property
    def metrics(self) -> RetryMetrics:
        return self._metrics

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        retry_on: Optional[set[ErrorCategory]] = None,
        on_retry: Optional[Callable[[int, Exception, float], None]] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute a synchronous function with retry logic."""
        retryable_categories = retry_on or _RETRYABLE_CATEGORIES
        max_retries = self._config.max_retries
        budget_s = self._config.retry_budget_s

        last_wait = 0.0
        retry_start = time.monotonic()
        retries = 0
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            if budget_s > 0 and retries > 0:
                elapsed = time.monotonic() - retry_start
                if elapsed >= budget_s:
                    self._metrics.record_budget_exhausted()
                    if last_error:
                        raise map_provider_exception(
                            last_error, self._config.provider
                        ) from last_error
                    raise NonRetryableError("Retry budget exhausted")

            try:
                result = func(*args, **kwargs)
                retry_time = time.monotonic() - retry_start if retries > 0 else 0.0
                self._metrics.record_attempt(
                    succeeded=True,
                    retries=retries,
                    retry_time_s=retry_time,
                    provider=self._config.provider,
                )
                return result

            except Exception as e:
                last_error = e
                category = classify_error(e, self._config.provider)

                if category not in retryable_categories:
                    self._metrics.record_attempt(
                        succeeded=False,
                        retries=retries,
                        category=category.value,
                        provider=self._config.provider,
                    )
                    raise map_provider_exception(
                        e, self._config.provider
                    ) from e

                if attempt >= max_retries:
                    retry_time = time.monotonic() - retry_start
                    self._metrics.record_attempt(
                        succeeded=False,
                        retries=retries,
                        retry_time_s=retry_time,
                        category=category.value,
                        provider=self._config.provider,
                    )
                    raise map_provider_exception(
                        e, self._config.provider
                    ) from e

                wait = self._config.compute_wait(attempt + 1, last_wait)
                last_wait = wait
                retries += 1

                if category == ErrorCategory.RATE_LIMIT:
                    retry_after = _extract_retry_after(e)
                    wait = max(wait, retry_after)

                if on_retry:
                    on_retry(retries, e, wait)

                time.sleep(wait)

        if last_error:
            raise map_provider_exception(last_error, self._config.provider) from last_error
        raise NonRetryableError("Retry executor reached unreachable state")
