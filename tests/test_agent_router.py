"""Tests for multi-agent routing (AgentRouter, RouteRule, AgentRegistry)."""

import time
import pytest
from app.agent.router import RouteRule, AgentConfig, AgentRegistry


# ── RouteRule matching ─────────────────────────────────────────────────────

def test_route_rule_exact_channel():
    rule = RouteRule(channel="telegram", agent_name="manus")
    assert rule.matches("telegram", "user1", "chat1") is True
    assert rule.matches("discord", "user1", "chat1") is False


def test_route_rule_user_pattern():
    rule = RouteRule(user_id=r"admin_.*", agent_name="manus")
    assert rule.matches("telegram", "admin_001", "chat1") is True
    assert rule.matches("telegram", "user_001", "chat1") is False


def test_route_rule_chat_id_pattern():
    rule = RouteRule(chat_id=r"general", agent_name="manus")
    assert rule.matches("telegram", "user1", "general") is True
    assert rule.matches("telegram", "user1", "random") is False


def test_route_rule_empty_matches_all():
    rule = RouteRule(agent_name="default")
    assert rule.matches("any_platform", "any_user", "any_channel") is True


def test_route_rule_priority():
    rule1 = RouteRule(channel="telegram", agent_name="a", priority=10)
    rule2 = RouteRule(channel="telegram", user_id="admin", agent_name="b", priority=20)
    rules = sorted([rule1, rule2], key=lambda r: r.priority, reverse=True)
    assert rules[0].agent_name == "b"
    assert rules[1].agent_name == "a"


# ── AgentConfig ───────────────────────────────────────────────────────────

def test_agent_config_defaults():
    config = AgentConfig()
    assert config.name == "manus"
    assert config.class_path == "app.agent.manus.Manus"
    assert config.tools == []
    assert config.sandbox_mode is False


def test_agent_config_custom():
    config = AgentConfig(
        name="coder",
        system_prompt="You are a coder",
        tools=["python_execute", "bash"],
        class_path="app.agent.manus.Manus",
    )
    assert config.name == "coder"
    assert len(config.tools) == 2


# ── AgentRegistry LRU eviction ────────────────────────────────────────────

def test_registry_put_and_get():
    registry = AgentRegistry(cache_size=10, idle_ttl=300)
    registry.put("key-1", "agent-instance-1")
    assert registry.get("key-1") == "agent-instance-1"
    assert registry.get("nonexistent") is None
    assert registry.size == 1


def test_registry_lru_eviction():
    registry = AgentRegistry(cache_size=3, idle_ttl=9999)
    registry.put("a", 1)
    registry.put("b", 2)
    registry.put("c", 3)
    assert registry.size == 3
    # Adding a 4th should evict the oldest
    registry.put("d", 4)
    assert registry.size == 3
    assert registry.get("a") is None  # Evicted
    assert registry.get("d") == 4


def test_registry_remove():
    registry = AgentRegistry()
    registry.put("key-1", "value")
    removed = registry.remove("key-1")
    assert removed == "value"
    assert registry.size == 0
    assert registry.remove("key-1") is None


@pytest.mark.asyncio
async def test_registry_cleanup_all():
    registry = AgentRegistry()
    agent = type("Agent", (), {"cleanup": lambda self: None})()
    registry.put("key-1", agent)
    await registry.cleanup_all()
    assert registry.size == 0


def test_registry_idle_ttl_eviction():
    """Agents idle beyond TTL should be evicted on next access."""
    registry = AgentRegistry(cache_size=10, idle_ttl=0)
    registry.put("key-1", "value")
    # With TTL=0, immediate eviction on next get
    result = registry.get("key-1")
    # The agent should be evicted since TTL is 0
    # (time.monotonic() now > last_active + 0)
    assert result is None


# ── AgentRouter (uses defaults when no config file exists) ────────────────

def test_agent_router_default_config():
    from app.agent.router import AgentRouter
    router = AgentRouter()
    agents = router.list_agents()
    assert "manus" in agents
    assert len(router.list_routes()) == 0  # No routes by default


def test_agent_router_resolve_route_default():
    from app.agent.router import AgentRouter
    router = AgentRouter()
    # No routes → falls back to "manus"
    route = router._resolve_route("telegram", "user1", "chat1")
    assert route == "manus"


def test_agent_router_resolve_route_with_custom_rules():
    from app.agent.router import AgentRouter, RouteRule
    router = AgentRouter()
    router._configs["coder"] = AgentConfig(name="coder")
    router._rules = [
        RouteRule(channel="discord", agent_name="coder", priority=10),
    ]
    route = router._resolve_route("discord", "user1", "chat1")
    assert route == "coder"
