from __future__ import annotations

"""
AgentRouter — Multi-agent routing per channel/account.

Manages multiple agent instances with per-channel routing rules.
Supports loading agent configurations from config.yaml under [agents.routes]
and [agents.defaults]. Falls back to the default Manus agent when no route matches.

Usage in MessagingGateway:
    router = AgentRouter()
    agent = router.get_agent(msg.platform, msg.user_id, msg.channel_id)
"""

import asyncio
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.logger import logger

_CACHE_SIZE = 64
_IDLE_TTL = 300  # seconds


@dataclass
class AgentConfig:
    """Configuration for a single agent instance.

    Attributes:
        name: Unique agent identifier (e.g. ``manus``, ``coder``, ``analyst``).
        system_prompt: Custom system prompt override. ``None`` uses the agent default.
        tools: List of tool names to enable. Empty means all default tools.
        sandbox_mode: Whether to run code in a sandboxed environment.
        workspace_dir: Per-agent workspace directory.
        class_path: Dotted import path to the agent class. Defaults to
                    ``app.agent.manus.Manus``.
        extra_config: Arbitrary extra configuration passed to the agent constructor.
    """
    name: str = "manus"
    system_prompt: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    sandbox_mode: bool = False
    workspace_dir: str = "workspace"
    class_path: str = "app.agent.manus.Manus"
    extra_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteRule:
    """Pattern-based routing rule that maps incoming messages to an agent.

    Match criteria are evaluated in order of specificity:
      1. Exact channel match (``channel`` + ``chat_id``)
      2. User match (``user_id`` pattern)
      3. Platform match (``channel`` pattern)
      4. Glob/chat_id pattern

    Attributes:
        channel: Platform name or glob (e.g. ``telegram``, ``discord``).
        user_id: User ID pattern (exact or regex).
        chat_id: Chat/channel ID pattern (exact or regex).
        agent_name: The agent configuration name to route to.
        priority: Higher priority rules are evaluated first. Default 0.
    """
    channel: str = ""
    user_id: str = ""
    chat_id: str = ""
    agent_name: str = "manus"
    priority: int = 0

    def matches(self, platform: str, user_id: str, channel_id: str) -> bool:
        """Check if this rule matches the given message attributes."""
        # Exact channel match
        if self.channel and not _match_pattern(self.channel, platform):
            return False
        # User ID match
        if self.user_id and not _match_pattern(self.user_id, user_id):
            return False
        # Chat ID match
        if self.chat_id and not _match_pattern(self.chat_id, channel_id):
            return False
        return True


def _match_pattern(pattern: str, value: str) -> bool:
    """Match a value against a pattern (exact string or regex)."""
    if not pattern:
        return True
    if pattern == value:
        return True
    try:
        return bool(re.search(pattern, value))
    except re.error:
        return pattern == value


class AgentRegistry:
    """LRU cache of agent instances, keyed by a compound session key.

    Follows the same pattern as MessagingGateway's agent cache:
    - LRU eviction when exceeding ``_CACHE_SIZE``
    - Idle TTL eviction for stale agents
    - One agent per unique routing key
    """

    def __init__(self, cache_size: int = _CACHE_SIZE, idle_ttl: int = _IDLE_TTL) -> None:
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._last_active: dict[str, float] = {}
        self._cache_size = cache_size
        self._idle_ttl = idle_ttl

    def get(self, key: str) -> Optional[Any]:
        """Get an agent from cache, or None if not found."""
        now = time.monotonic()
        self._evict_idle(now)
        self._evict_lru()

        if key in self._cache:
            self._cache.move_to_end(key)
            self._last_active[key] = now
            return self._cache[key]
        return None

    def put(self, key: str, agent: Any) -> None:
        """Store an agent instance in the cache."""
        now = time.monotonic()
        self._evict_idle(now)
        self._evict_lru()

        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            self._cache[key] = agent
        self._last_active[key] = now

    def remove(self, key: str) -> Optional[Any]:
        """Remove and return an agent from the cache."""
        agent = self._cache.pop(key, None)
        self._last_active.pop(key, None)
        return agent

    async def cleanup_all(self) -> None:
        """Clean up all cached agent instances (call cleanup on each)."""
        for key, agent in list(self._cache.items()):
            try:
                if hasattr(agent, "cleanup"):
                    await agent.cleanup()
            except Exception as e:
                logger.warning(f"[AgentRegistry] Cleanup error for {key}: {e}")
        self._cache.clear()
        self._last_active.clear()

    @property
    def size(self) -> int:
        return len(self._cache)

    def _evict_idle(self, now: float) -> None:
        """Remove agents that have been idle beyond TTL."""
        evict = [
            k for k, t in self._last_active.items()
            if now - t > self._idle_ttl
        ]
        for k in evict:
            self._cache.pop(k, None)
            self._last_active.pop(k, None)
            logger.debug(f"[AgentRegistry] Evicted idle agent: {k}")

    def _evict_lru(self) -> None:
        """Evict least-recently-used agents when exceeding cache size."""
        while len(self._cache) >= self._cache_size:
            k, agent = self._cache.popitem(last=False)
            self._last_active.pop(k, None)
            logger.debug(f"[AgentRegistry] LRU evicted agent: {k}")


class AgentRouter:
    """Routes incoming messages to the correct agent instance.

    Loads routing configuration from ``config.yaml`` under the ``agents`` section:

    .. code-block:: yaml

        agents:
          defaults:
            sandbox_mode: false
            workspace_dir: workspace
          definitions:
            manus:
              class_path: app.agent.manus.Manus
            coder:
              class_path: app.agent.manus.Manus
              system_prompt: "You are a coding specialist..."
              tools: [python_execute, str_replace_editor, bash]
          routes:
            - channel: discord
              agent_name: coder
            - channel: telegram
              user_id: "admin_.*"
              agent_name: manus
            - chat_id: "general"
              agent_name: manus

    Falls back to the default ``Manus`` agent when no route matches.
    """

    def __init__(self) -> None:
        self._configs: dict[str, AgentConfig] = {}
        self._rules: list[RouteRule] = []
        self._registry = AgentRegistry()
        self._load_config()

    def _load_config(self) -> None:
        """Load agent routing configuration from config.yaml."""
        try:
            from app.config import Config
            cfg = Config.get()
            # Access raw config data for agents section
            agents_data = getattr(cfg._data, "__dict__", {}).get("agents", None)
            if agents_data is None:
                # Try loading from YAML directly
                agents_data = self._load_agents_from_yaml()
            if not agents_data:
                self._register_defaults()
                return
            self._parse_config(agents_data)
        except Exception as e:
            logger.warning(f"[AgentRouter] Config load failed, using defaults: {e}")
            self._register_defaults()

    def _load_agents_from_yaml(self) -> Optional[dict[str, Any]]:
        """Try loading agents config from the config.yaml file."""
        try:
            from pathlib import Path as P
            import os
            home = P(os.getenv("MANUSCLAW_HOME", P.home() / ".manusclaw"))
            for candidate in [home / "config.yaml", P("config.yaml")]:
                if candidate.exists():
                    try:
                        import yaml
                        data = yaml.safe_load(candidate.read_text()) or {}
                        return data.get("agents", None)
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _register_defaults(self) -> None:
        """Register the default Manus agent configuration."""
        self._configs["manus"] = AgentConfig(
            name="manus",
            class_path="app.agent.manus.Manus",
        )
        logger.info("[AgentRouter] Using default Manus agent configuration")

    def _parse_config(self, agents_data: dict[str, Any]) -> None:
        """Parse the agents section from config data."""
        # Parse agent definitions
        definitions = agents_data.get("definitions", {})
        defaults = agents_data.get("defaults", {})

        # Always register manus as the base default
        self._configs["manus"] = AgentConfig(
            name="manus",
            class_path="app.agent.manus.Manus",
            sandbox_mode=defaults.get("sandbox_mode", False),
            workspace_dir=defaults.get("workspace_dir", "workspace"),
        )

        for name, defn in definitions.items():
            if name == "manus":
                # Override manus defaults
                self._configs["manus"] = AgentConfig(
                    name=name,
                    system_prompt=defn.get("system_prompt"),
                    tools=defn.get("tools", []),
                    sandbox_mode=defn.get("sandbox_mode", defaults.get("sandbox_mode", False)),
                    workspace_dir=defn.get("workspace_dir", defaults.get("workspace_dir", "workspace")),
                    class_path=defn.get("class_path", "app.agent.manus.Manus"),
                    extra_config=defn.get("extra_config", {}),
                )
            else:
                self._configs[name] = AgentConfig(
                    name=name,
                    system_prompt=defn.get("system_prompt"),
                    tools=defn.get("tools", []),
                    sandbox_mode=defn.get("sandbox_mode", defaults.get("sandbox_mode", False)),
                    workspace_dir=defn.get("workspace_dir", defaults.get("workspace_dir", "workspace")),
                    class_path=defn.get("class_path", "app.agent.manus.Manus"),
                    extra_config=defn.get("extra_config", {}),
                )
            logger.debug(f"[AgentRouter] Registered agent config: {name}")

        # Parse routing rules
        routes = agents_data.get("routes", [])
        for route in routes:
            rule = RouteRule(
                channel=route.get("channel", ""),
                user_id=route.get("user_id", ""),
                chat_id=route.get("chat_id", ""),
                agent_name=route.get("agent_name", "manus"),
                priority=route.get("priority", 0),
            )
            self._rules.append(rule)
            logger.debug(
                f"[AgentRouter] Route rule: {rule.channel}/{rule.user_id} → {rule.agent_name}"
            )

        # Sort by priority (higher first)
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        logger.info(
            f"[AgentRouter] Loaded {len(self._configs)} agent configs, "
            f"{len(self._rules)} route rules"
        )

    def get_agent(self, platform: str, user_id: str, channel_id: str):
        """Get or create the appropriate agent for a given message context.

        Evaluates routing rules against the platform/user_id/channel_id
        combination and returns a cached or new agent instance.

        Args:
            platform: Messaging platform name (e.g. ``telegram``, ``discord``).
            user_id: User identifier from the messaging platform.
            channel_id: Channel/chat identifier from the messaging platform.

        Returns:
            An agent instance (typically a subclass of BaseAgent).
        """
        # Build a session key for caching
        route_key = self._resolve_route(platform, user_id, channel_id)
        session_key = f"{route_key}:{platform}:{user_id}:{channel_id}"

        # Check cache first
        cached = self._registry.get(session_key)
        if cached is not None:
            return cached

        # Create new agent instance
        agent = self._create_agent(route_key)
        self._registry.put(session_key, agent)
        return agent

    def _resolve_route(self, platform: str, user_id: str, channel_id: str) -> str:
        """Evaluate routing rules and return the matched agent name.

        Falls back to ``manus`` if no rule matches.
        """
        for rule in self._rules:
            if rule.matches(platform, user_id, channel_id):
                if rule.agent_name in self._configs:
                    logger.debug(
                        f"[AgentRouter] Routed {platform}/{user_id} → {rule.agent_name}"
                    )
                    return rule.agent_name
                else:
                    logger.warning(
                        f"[AgentRouter] Route matched {rule.agent_name} but config not found, "
                        f"falling back to manus"
                    )
                    return "manus"
        return "manus"

    def _create_agent(self, agent_name: str):
        """Instantiate an agent from its configuration."""
        config = self._configs.get(agent_name)
        if not config:
            logger.warning(
                f"[AgentRouter] No config for '{agent_name}', creating default Manus"
            )
            from app.agent.manus import Manus
            return Manus()

        try:
            cls = self._import_class(config.class_path)
            logger.info(
                f"[AgentRouter] Creating agent '{config.name}' via {config.class_path}"
            )
            return cls()
        except Exception as e:
            logger.error(
                f"[AgentRouter] Failed to create agent '{agent_name}': {e}, "
                f"falling back to Manus"
            )
            from app.agent.manus import Manus
            return Manus()

    @staticmethod
    def _import_class(dotted_path: str):
        """Import a class from a dotted module path.

        Example: ``app.agent.manus.Manus`` → ``<class Manus>``
        """
        parts = dotted_path.rsplit(".", 1)
        if len(parts) != 2:
            raise ImportError(f"Invalid class path: {dotted_path}")
        module_path, class_name = parts
        import importlib
        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    def list_agents(self) -> dict[str, AgentConfig]:
        """Return all registered agent configurations."""
        return dict(self._configs)

    def list_routes(self) -> list[RouteRule]:
        """Return all routing rules."""
        return list(self._rules)

    async def shutdown(self) -> None:
        """Clean up all cached agent instances."""
        await self._registry.cleanup_all()
        logger.info("[AgentRouter] Shutdown complete")
