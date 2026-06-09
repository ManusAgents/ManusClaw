from __future__ import annotations

"""
CredentialPool — per-provider API key pool with priority ordering and auto-rotation.

Each provider can have multiple credentials ranked by priority (lower = higher priority).
When a key is exhausted (RateLimitError) it is deprioritised and the next key is tried.
Keys auto-recover after a cooldown window.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from app.logger import logger


@dataclass
class Credential:
    api_key: str
    priority: int = 0
    exhausted_until: float = 0.0
    use_count: int = 0
    fail_count: int = 0

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.exhausted_until

    def mark_exhausted(self, cooldown_s: float = 60.0) -> None:
        self.exhausted_until = time.monotonic() + cooldown_s
        self.fail_count += 1
        logger.warning(f"[CredentialPool] Key ...{self.api_key[-6:]} exhausted, cooling {cooldown_s:.0f}s")

    def mark_success(self) -> None:
        self.use_count += 1
        self.exhausted_until = 0.0


class CredentialPool:
    """
    Thread-safe pool of API keys for a single provider.
    Keys are tried in priority order; exhausted keys are skipped until cooled.
    """

    def __init__(self, credentials: list[dict], cooldown_s: float = 60.0) -> None:
        self._creds: list[Credential] = sorted(
            [Credential(api_key=c["api_key"], priority=c.get("priority", 0)) for c in credentials],
            key=lambda c: c.priority,
        )
        self._cooldown = cooldown_s
        self._lock = asyncio.Lock()
        self._current_idx = 0

    @classmethod
    def from_env(cls, env_keys: list[str], cooldown_s: float = 60.0) -> "CredentialPool":
        """Build a pool from a list of API key strings."""
        return cls(
            [{"api_key": k, "priority": i} for i, k in enumerate(env_keys) if k],
            cooldown_s=cooldown_s,
        )

    async def get(self) -> Optional[Credential]:
        """Return the highest-priority available credential, or None if all exhausted."""
        async with self._lock:
            available = [c for c in self._creds if c.is_available]
            if not available:
                return None
            return available[0]

    async def mark_exhausted(self, cred: Credential) -> None:
        async with self._lock:
            cred.mark_exhausted(self._cooldown)

    async def mark_success(self, cred: Credential) -> None:
        async with self._lock:
            cred.mark_success()

    @property
    def size(self) -> int:
        return len(self._creds)

    @property
    def available_count(self) -> int:
        return sum(1 for c in self._creds if c.is_available)


    @classmethod
    def from_profile(cls, profile: "ModelProfile") -> "CredentialPool":
        """Build a CredentialPool from a ModelProfile's entries.

        Extracts all unique API keys from the profile, ordered by priority.
        Useful for cross-provider rotation where the caller wants a unified
        pool of all configured keys regardless of provider.

        Args:
            profile: A ModelProfile instance.

        Returns:
            A CredentialPool with all keys from the profile.
        """
        credentials = []
        seen_keys: set[str] = set()
        for entry in profile.entries:
            if entry.api_key and entry.api_key not in seen_keys:
                credentials.append({
                    "api_key": entry.api_key,
                    "priority": entry.priority,
                })
                seen_keys.add(entry.api_key)
        return cls(credentials, cooldown_s=60.0) if credentials else cls([])


class CrossProviderRotator:
    """Cross-provider credential rotation.

    Wraps multiple CredentialPool instances (one per provider) and provides
    a unified interface for getting the next available credential across
    all providers. Works in conjunction with ModelProfile for cross-provider
    failover.

    Usage::

        rotator = CrossProviderRotator()
        rotator.add_pool("openai", openai_pool)
        rotator.add_pool("anthropic", anthropic_pool)

        cred, provider = await rotator.get_next("openai")  # Try openai first
        if cred is None:
            cred, provider = await rotator.get_next()  # Try any provider
    """

    def __init__(self) -> None:
        self._pools: dict[str, CredentialPool] = {}
        self._lock = asyncio.Lock()
        self._priority_order: list[str] = []

    def add_pool(self, provider: str, pool: CredentialPool,
                 priority: int = 0) -> None:
        """Register a credential pool for a provider.

        Args:
            provider: Provider name (``openai``, ``anthropic``, etc.).
            pool: CredentialPool instance.
            priority: Lower = higher priority when selecting across providers.
        """
        self._pools[provider] = pool
        # Maintain priority-ordered list
        if provider not in self._priority_order:
            self._priority_order.append(provider)
        self._priority_order.sort(
            key=lambda p: (priority, p)
        )
        logger.debug(
            f"[CrossProviderRotator] Added pool for {provider} "
            f"(size={pool.size}, priority={priority})"
        )

    async def get_next(self, preferred_provider: Optional[str] = None) -> tuple[Optional[Credential], str]:
        """Get the next available credential, trying preferred provider first.

        Args:
            preferred_provider: Provider to try first. If None, tries all
                               in priority order.

        Returns:
            Tuple of (Credential, provider_name). Credential may be None
            if all pools are exhausted.
        """
        async with self._lock:
            # Try preferred provider first
            if preferred_provider and preferred_provider in self._pools:
                cred = await self._pools[preferred_provider].get()
                if cred:
                    return cred, preferred_provider

            # Try all providers in priority order
            for provider in self._priority_order:
                if preferred_provider and provider == preferred_provider:
                    continue  # Already tried
                cred = await self._pools[provider].get()
                if cred:
                    return cred, provider

            return None, ""

    async def mark_exhausted(self, provider: str, cred: Credential) -> None:
        """Mark a credential as exhausted in its provider pool."""
        pool = self._pools.get(provider)
        if pool:
            await pool.mark_exhausted(cred)

    async def mark_success(self, provider: str, cred: Credential) -> None:
        """Mark a credential as successful in its provider pool."""
        pool = self._pools.get(provider)
        if pool:
            await pool.mark_success(cred)

    @property
    def providers(self) -> list[str]:
        """Return the list of registered providers in priority order."""
        return list(self._priority_order)

    @property
    def total_available(self) -> int:
        """Return the total number of available credentials across all pools."""
        return sum(p.available_count for p in self._pools.values())


def build_pool_from_config(provider: str, primary_key: Optional[str] = None) -> Optional[CredentialPool]:
    """
    Build a CredentialPool from environment variables.
    Looks for OPENAI_API_KEY, OPENAI_API_KEY_2, OPENAI_API_KEY_3, etc.
    """
    import os
    prefix_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "bedrock": "AWS_ACCESS_KEY_ID",
        "google": "GOOGLE_API_KEY",
    }
    prefix = prefix_map.get(provider.lower())
    if not prefix:
        if primary_key:
            return CredentialPool.from_env([primary_key])
        return None

    keys: list[str] = []
    if primary_key:
        keys.append(primary_key)
    # Also try OPENAI_API_KEY_2, _3, etc.
    for i in range(2, 10):
        k = os.getenv(f"{prefix}_{i}", "")
        if k and k not in keys:
            keys.append(k)

    if not keys:
        env_k = os.getenv(prefix, "")
        if env_k:
            keys.append(env_k)

    return CredentialPool.from_env(keys) if keys else None
