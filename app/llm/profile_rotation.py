from __future__ import annotations

"""
Model Failover Profiles / Auth Rotation
=========================================

Provides cross-provider model failover with configurable priority ordering,
cooldown for failed models, and per-session profile selection.

A ``ModelProfile`` defines an ordered list of (provider, model, api_key)
entries. The ``ProfileRotator`` tries entries in priority order, skipping
failed models that are in cooldown.

Supports cross-provider failover: e.g., OpenAI → Anthropic → Ollama.

Usage::

    from app.llm.profile_rotation import ModelProfile, ProfileRotator

    profile = ModelProfile.from_config([
        {"provider": "openai", "model": "gpt-4o", "api_key": "sk-...", "priority": 0},
        {"provider": "anthropic", "model": "claude-sonnet-4", "api_key": "sk-ant-...", "priority": 1},
        {"provider": "ollama", "model": "llama3.2:3b", "api_key": "", "priority": 2},
    ])

    rotator = ProfileRotator(profile)
    entry = await rotator.get_next()  # Returns best available model entry
    # ... if it fails ...
    await rotator.mark_failed(entry)
    entry = await rotator.get_next()  # Returns next in priority order
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.logger import logger


@dataclass
class ModelEntry:
    """A single model configuration within a profile.

    Attributes:
        provider: LLM provider name (``openai``, ``anthropic``, ``ollama``, etc.).
        model: Model identifier (``gpt-4o``, ``claude-sonnet-4``, etc.).
        api_key: API key for this provider. Empty for local models.
        base_url: Optional custom base URL (for Ollama, LMStudio, etc.).
        priority: Lower value = higher priority. Entries are sorted by priority.
        fallback_weight: Weight used for probabilistic load balancing at same priority.
        extra_headers: Additional HTTP headers to send with requests.
        max_tokens: Max output tokens for this model.
        temperature: Temperature setting for this model.
        timeout: Request timeout in seconds.
        cooldown_s: Cooldown period after failure (seconds).
    """
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: Optional[str] = None
    priority: int = 0
    fallback_weight: float = 1.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout: int = 300
    cooldown_s: float = 60.0

    @property
    def key(self) -> str:
        """Unique key for this model entry."""
        return f"{self.provider}:{self.model}"

    def __repr__(self) -> str:
        key_suffix = self.api_key[-6:] if self.api_key else "no-key"
        return f"ModelEntry({self.provider}/{self.model} ...{key_suffix} p={self.priority})"


@dataclass
class ModelStats:
    """Tracks success/failure statistics for a single model entry."""
    entry: ModelEntry
    total_calls: int = 0
    success_count: int = 0
    fail_count: int = 0
    last_failure: float = 0.0  # monotonic time of last failure
    total_latency_s: float = 0.0  # cumulative latency in seconds

    @property
    def success_rate(self) -> float:
        """Return success rate as a float between 0.0 and 1.0."""
        if self.total_calls == 0:
            return 1.0
        return self.success_count / self.total_calls

    @property
    def is_in_cooldown(self) -> bool:
        """Check if this model is in cooldown (recently failed)."""
        if self.fail_count == 0:
            return False
        return time.monotonic() < self.last_failure + self.entry.cooldown_s

    @property
    def remaining_cooldown_s(self) -> float:
        """Seconds remaining in cooldown, or 0 if not in cooldown."""
        if not self.is_in_cooldown:
            return 0.0
        return self.last_failure + self.entry.cooldown_s - time.monotonic()

    @property
    def avg_latency_s(self) -> float:
        """Average latency per call."""
        if self.total_calls == 0:
            return 0.0
        return self.total_latency_s / self.total_calls


class ModelProfile:
    """An ordered list of model entries for failover.

    Entries are sorted by priority (lower = higher priority). When multiple
    entries have the same priority, ``fallback_weight`` determines the
    probability of selection.

    Can be created from a list of dicts (config) or directly from ModelEntry
    objects.
    """

    def __init__(self, entries: Optional[list[ModelEntry]] = None) -> None:
        self.entries: list[ModelEntry] = sorted(
            entries or [], key=lambda e: (e.priority, e.model)
        )
        self.name: str = ""
        self._stats: dict[str, ModelStats] = {}

    @classmethod
    def from_config(cls, config: list[dict[str, Any]], name: str = "") -> "ModelProfile":
        """Create a profile from a list of configuration dicts.

        Args:
            config: List of dicts with keys: provider, model, api_key, priority,
                    fallback_weight, base_url, extra_headers, max_tokens,
                    temperature, timeout, cooldown_s.
            name: Optional profile name for identification.

        Returns:
            A ModelProfile with entries sorted by priority.
        """
        entries = []
        for item in config:
            entry = ModelEntry(
                provider=item.get("provider", "openai"),
                model=item.get("model", "gpt-4o"),
                api_key=item.get("api_key", ""),
                base_url=item.get("base_url"),
                priority=item.get("priority", 0),
                fallback_weight=item.get("fallback_weight", 1.0),
                extra_headers=item.get("extra_headers", {}),
                max_tokens=item.get("max_tokens", 4096),
                temperature=item.get("temperature", 0.0),
                timeout=item.get("timeout", 300),
                cooldown_s=item.get("cooldown_s", 60.0),
            )
            entries.append(entry)
        profile = cls(entries)
        profile.name = name
        return profile

    @classmethod
    def default(cls) -> "ModelProfile":
        """Create a default profile that uses the current Config settings."""
        try:
            from app.config import Config
            cfg = Config.get()
            entry = ModelEntry(
                provider=cfg.llm.provider,
                model=cfg.llm.model,
                api_key=cfg.llm.api_key or "",
                base_url=cfg.llm.base_url,
                max_tokens=cfg.llm.max_tokens,
                temperature=cfg.llm.temperature,
                timeout=cfg.llm.timeout,
            )
            profile = cls([entry])
            profile.name = "default"
            return profile
        except Exception as e:
            logger.warning(f"[ModelProfile] Failed to create default profile: {e}")
            return cls(name="default")

    def get_stats(self, entry: ModelEntry) -> ModelStats:
        """Get or create stats tracking for a model entry."""
        key = entry.key
        if key not in self._stats:
            self._stats[key] = ModelStats(entry=entry)
        return self._stats[key]

    def list_stats(self) -> list[ModelStats]:
        """Return stats for all entries that have been used."""
        return list(self._stats.values())

    def __repr__(self) -> str:
        return f"ModelProfile(name={self.name!r}, entries={len(self.entries)})"


class ProfileRotator:
    """Manages model failover across a profile's entries.

    - Tries entries in priority order
    - Skips models in cooldown (recently failed)
    - Supports probabilistic selection at the same priority level
    - Tracks per-model success/failure statistics
    - Supports per-session profile selection

    Usage::

        rotator = ProfileRotator(profile)
        entry = await rotator.get_next()
        # Use entry for LLM call...
        if failure:
            await rotator.mark_failed(entry)
        else:
            await rotator.mark_success(entry)
    """

    def __init__(self, profile: Optional[ModelProfile] = None) -> None:
        self._profile = profile or ModelProfile.default()
        self._current_idx: int = 0
        self._lock = asyncio.Lock()
        self._session_profiles: dict[str, ModelProfile] = {}

    @property
    def profile(self) -> ModelProfile:
        """Return the active model profile."""
        return self._profile

    async def get_next(self, provider_filter: Optional[str] = None) -> Optional[ModelEntry]:
        """Get the next available model entry for use.

        Skips entries that are in cooldown. Optionally filters by provider.

        Args:
            provider_filter: If set, only return entries from this provider.

        Returns:
            The best available ModelEntry, or None if all are in cooldown.
        """
        async with self._lock:
            entries = self._profile.entries

            if provider_filter:
                entries = [e for e in entries if e.provider == provider_filter]

            # Try entries in priority order
            for entry in entries:
                stats = self._profile.get_stats(entry)
                if not stats.is_in_cooldown:
                    return entry

            # All in cooldown — find the one with least remaining cooldown
            best = None
            best_remaining = float("inf")
            for entry in entries:
                if provider_filter and entry.provider != provider_filter:
                    continue
                stats = self._profile.get_stats(entry)
                remaining = stats.remaining_cooldown_s
                if remaining < best_remaining:
                    best_remaining = remaining
                    best = entry

            if best:
                logger.warning(
                    f"[ProfileRotator] All models in cooldown. "
                    f"Waiting {best_remaining:.1f}s for {best.key}"
                )
                return best

            logger.error("[ProfileRotator] No models available")
            return None

    async def mark_success(self, entry: ModelEntry, latency_s: float = 0.0) -> None:
        """Record a successful call for a model entry.

        Args:
            entry: The model entry that succeeded.
            latency_s: Duration of the successful call in seconds.
        """
        async with self._lock:
            stats = self._profile.get_stats(entry)
            stats.total_calls += 1
            stats.success_count += 1
            stats.total_latency_s += latency_s
            logger.debug(
                f"[ProfileRotator] Success: {entry.key} "
                f"(rate={stats.success_rate:.1%}, latency={latency_s:.1f}s)"
            )

    async def mark_failed(self, entry: ModelEntry, error: Optional[str] = None) -> None:
        """Record a failed call and put the model in cooldown.

        Args:
            entry: The model entry that failed.
            error: Optional error message for logging.
        """
        async with self._lock:
            stats = self._profile.get_stats(entry)
            stats.total_calls += 1
            stats.fail_count += 1
            stats.last_failure = time.monotonic()
            logger.warning(
                f"[ProfileRotator] Failed: {entry.key} "
                f"(cooldown={entry.cooldown_s:.0f}s, error={error or 'unknown'})"
            )

    async def reset_all(self) -> None:
        """Reset all cooldown timers and stats."""
        async with self._lock:
            for stats in self._profile.list_stats():
                stats.fail_count = 0
                stats.last_failure = 0.0
            logger.info("[ProfileRotator] All cooldowns reset")

    def set_session_profile(self, session_id: str, profile: ModelProfile) -> None:
        """Associate a model profile with a specific session.

        Args:
            session_id: The session identifier.
            profile: The model profile to use for this session.
        """
        self._session_profiles[session_id] = profile
        logger.debug(
            f"[ProfileRotator] Set profile '{profile.name}' for session {session_id}"
        )

    def get_session_profile(self, session_id: str) -> Optional[ModelProfile]:
        """Get the model profile associated with a session.

        Returns None if no session-specific profile is set.
        """
        return self._session_profiles.get(session_id)

    def remove_session_profile(self, session_id: str) -> None:
        """Remove the session-specific profile and revert to default."""
        self._session_profiles.pop(session_id, None)

    def status_summary(self) -> str:
        """Return a human-readable status summary of all model entries."""
        lines = [f"Profile: {self._profile.name or 'default'}"]
        lines.append(f"{'Model':<35} {'Priority':>8} {'Rate':>7} {'Cooldown':>10} {'Latency':>10}")
        lines.append("-" * 75)

        for entry in self._profile.entries:
            stats = self._profile.get_stats(entry)
            cooldown_str = ""
            if stats.is_in_cooldown:
                cooldown_str = f"{stats.remaining_cooldown_s:.0f}s"
            else:
                cooldown_str = "ready"

            lines.append(
                f"{entry.provider}/{entry.model:<28} {entry.priority:>8} "
                f"{stats.success_rate:>6.0%} {cooldown_str:>10} "
                f"{stats.avg_latency_s:>9.1f}s"
            )

        return "\n".join(lines)
