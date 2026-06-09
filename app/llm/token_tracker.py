from __future__ import annotations

"""
TokenTracker — per-session token budget with grace call support.

Tracks input, output, cache, and reasoning tokens separately.
A grace call is allowed after budget exhaustion for cleanup.
"""

from dataclasses import dataclass, field
from typing import Optional

from app.logger import logger


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_estimate_usd(self) -> float:
        """Rough estimate at GPT-4o rates."""
        return (self.input_tokens / 1_000_000 * 2.50) + (self.output_tokens / 1_000_000 * 10.0)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def to_dict(self) -> dict:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
            "reasoning": self.reasoning_tokens,
            "total": self.total,
            "cost_usd": round(self.cost_estimate_usd, 6),
        }


class TokenBudget:
    """
    Tracks token usage and enforces a budget with one grace call.
    
    Usage:
        budget = TokenBudget(max_tokens=100_000)
        budget.record(usage)
        if budget.is_exhausted and not budget.grace_used:
            budget.use_grace()   # one extra call for cleanup
    """

    def __init__(self, max_tokens: int = 0) -> None:
        self.max_tokens = max_tokens
        self._usage = TokenUsage()
        self._grace_used = False
        self._grace_available = max_tokens > 0

    def record(self, response: dict) -> TokenUsage:
        """Extract and record token usage from an LLM response dict."""
        usage_dict = response.get("usage", {})
        if not usage_dict:
            return TokenUsage()

        delta = TokenUsage(
            input_tokens=usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0)),
            output_tokens=usage_dict.get("completion_tokens", usage_dict.get("output_tokens", 0)),
            cache_read_tokens=usage_dict.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage_dict.get("cache_creation_input_tokens", 0),
            reasoning_tokens=usage_dict.get("reasoning_tokens", 0),
        )
        self._usage = self._usage + delta

        if self.max_tokens > 0 and self._usage.total >= self.max_tokens:
            logger.warning(
                f"[TokenBudget] Budget reached: {self._usage.total}/{self.max_tokens} tokens"
            )
        return delta

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    @property
    def is_exhausted(self) -> bool:
        if self.max_tokens <= 0:
            return False
        return self._usage.total >= self.max_tokens

    @property
    def grace_used(self) -> bool:
        return self._grace_used

    def use_grace(self) -> bool:
        """Consume the grace call. Returns True if grace was available."""
        if self._grace_available and not self._grace_used:
            self._grace_used = True
            logger.info("[TokenBudget] Grace call activated for cleanup.")
            return True
        return False

    @property
    def remaining(self) -> int:
        if self.max_tokens <= 0:
            return -1
        return max(0, self.max_tokens - self._usage.total)

    def summary(self) -> str:
        u = self._usage
        parts = [f"in={u.input_tokens} out={u.output_tokens}"]
        if u.cache_read_tokens:
            parts.append(f"cache_read={u.cache_read_tokens}")
        if u.reasoning_tokens:
            parts.append(f"reasoning={u.reasoning_tokens}")
        parts.append(f"total={u.total}")
        if self.max_tokens > 0:
            parts.append(f"budget={self.max_tokens} remaining={self.remaining}")
        return " | ".join(parts)
