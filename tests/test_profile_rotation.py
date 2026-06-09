"""Tests for model failover / profile rotation."""

import time
import pytest
from app.llm.profile_rotation import ModelEntry, ModelProfile, ModelStats, ProfileRotator


# ── ModelEntry ────────────────────────────────────────────────────────────

def test_model_entry_key():
    entry = ModelEntry(provider="openai", model="gpt-4o")
    assert entry.key == "openai:gpt-4o"


def test_model_entry_repr():
    entry = ModelEntry(provider="anthropic", model="claude-sonnet-4", api_key="sk-ant-abc123xyz")
    r = repr(entry)
    assert "anthropic/claude-sonnet-4" in r
    assert "abc123xyz" not in r  # Only last 6 chars shown


# ── ModelProfile creation ─────────────────────────────────────────────────

def test_model_profile_from_config():
    config = [
        {"provider": "openai", "model": "gpt-4o", "api_key": "sk-1", "priority": 0},
        {"provider": "anthropic", "model": "claude-sonnet-4", "api_key": "sk-ant-2", "priority": 1},
        {"provider": "ollama", "model": "llama3.2:3b", "priority": 2},
    ]
    profile = ModelProfile.from_config(config, name="multi")
    assert profile.name == "multi"
    assert len(profile.entries) == 3
    # Sorted by priority
    assert profile.entries[0].provider == "openai"
    assert profile.entries[1].provider == "anthropic"
    assert profile.entries[2].provider == "ollama"


def test_model_profile_empty():
    profile = ModelProfile()
    assert len(profile.entries) == 0
    assert profile.name == ""


def test_model_profile_get_stats():
    entry = ModelEntry(provider="openai", model="gpt-4o")
    profile = ModelProfile(entries=[entry])
    stats = profile.get_stats(entry)
    assert isinstance(stats, ModelStats)
    assert stats.entry.key == "openai:gpt-4o"


def test_model_profile_list_stats():
    profile = ModelProfile(entries=[
        ModelEntry(provider="openai", model="gpt-4o"),
        ModelEntry(provider="anthropic", model="claude-sonnet-4"),
    ])
    profile.get_stats(profile.entries[0])
    all_stats = profile.list_stats()
    assert len(all_stats) == 1  # Only tracked after get_stats is called


# ── ProfileRotator priority ordering ──────────────────────────────────────

@pytest.mark.asyncio
async def test_rotator_get_next_priority_order():
    config = [
        {"provider": "openai", "model": "gpt-4o", "priority": 0},
        {"provider": "anthropic", "model": "claude-sonnet-4", "priority": 1},
        {"provider": "ollama", "model": "llama3.2:3b", "priority": 2},
    ]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)
    entry = await rotator.get_next()
    assert entry is not None
    assert entry.provider == "openai"
    assert entry.model == "gpt-4o"


@pytest.mark.asyncio
async def test_rotator_cross_provider_failover():
    """After marking openai as failed, should return anthropic."""
    config = [
        {"provider": "openai", "model": "gpt-4o", "priority": 0, "cooldown_s": 9999},
        {"provider": "anthropic", "model": "claude-sonnet-4", "priority": 1},
    ]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)

    first = await rotator.get_next()
    assert first.provider == "openai"

    await rotator.mark_failed(first)
    second = await rotator.get_next()
    assert second.provider == "anthropic"


@pytest.mark.asyncio
async def test_rotator_mark_success():
    config = [{"provider": "openai", "model": "gpt-4o"}]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)

    entry = await rotator.get_next()
    await rotator.mark_success(entry, latency_s=1.5)

    stats = profile.get_stats(entry)
    assert stats.total_calls == 1
    assert stats.success_count == 1
    assert stats.success_rate == 1.0
    assert stats.avg_latency_s == 1.5


@pytest.mark.asyncio
async def test_rotator_mark_failed_cooldown():
    config = [{"provider": "openai", "model": "gpt-4o", "cooldown_s": 9999}]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)

    entry = await rotator.get_next()
    await rotator.mark_failed(entry, error="rate limit")

    stats = profile.get_stats(entry)
    assert stats.fail_count == 1
    assert stats.is_in_cooldown


@pytest.mark.asyncio
async def test_rotator_reset_all():
    config = [{"provider": "openai", "model": "gpt-4o", "cooldown_s": 9999}]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)

    entry = await rotator.get_next()
    await rotator.mark_failed(entry)
    assert profile.get_stats(entry).is_in_cooldown

    await rotator.reset_all()
    assert not profile.get_stats(entry).is_in_cooldown


@pytest.mark.asyncio
async def test_rotator_provider_filter():
    config = [
        {"provider": "openai", "model": "gpt-4o", "priority": 0},
        {"provider": "anthropic", "model": "claude-sonnet-4", "priority": 1},
    ]
    profile = ModelProfile.from_config(config)
    rotator = ProfileRotator(profile)

    entry = await rotator.get_next(provider_filter="anthropic")
    assert entry is not None
    assert entry.provider == "anthropic"


@pytest.mark.asyncio
async def test_rotator_empty_profile():
    profile = ModelProfile()
    rotator = ProfileRotator(profile)
    entry = await rotator.get_next()
    assert entry is None


@pytest.mark.asyncio
async def test_rotator_session_profile():
    config = [{"provider": "openai", "model": "gpt-4o"}]
    profile = ModelProfile.from_config(config, name="default")
    rotator = ProfileRotator(profile)

    session_profile = ModelProfile(
        entries=[ModelEntry(provider="anthropic", model="claude-sonnet-4")],
    )
    session_profile.name = "claude-only"

    rotator.set_session_profile("sess-123", session_profile)
    assert rotator.get_session_profile("sess-123") == session_profile

    rotator.remove_session_profile("sess-123")
    assert rotator.get_session_profile("sess-123") is None


@pytest.mark.asyncio
async def test_rotator_status_summary():
    config = [{"provider": "openai", "model": "gpt-4o", "priority": 0}]
    profile = ModelProfile.from_config(config, name="test")
    rotator = ProfileRotator(profile)
    summary = rotator.status_summary()
    assert "test" in summary
    assert "openai" in summary
