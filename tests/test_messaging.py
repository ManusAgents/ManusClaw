"""Tests for messaging adapters and gateway."""
import pytest
import os
os.environ["APP_ENV"] = "test"

from app.config import Config
Config.reset()


def test_telegram_adapter_stub_mode():
    """TelegramAdapter should be in stub mode when token not set."""
    from app.messaging.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    # Should not raise — graceful stub
    assert adapter is not None
    assert adapter.platform_name == "telegram"


def test_discord_adapter_stub_mode():
    from app.messaging.discord import DiscordAdapter
    adapter = DiscordAdapter()
    assert adapter is not None
    assert adapter.platform_name == "discord"


def test_slack_adapter_stub_mode():
    from app.messaging.slack import SlackAdapter
    adapter = SlackAdapter()
    assert adapter is not None
    assert adapter.platform_name == "slack"


@pytest.mark.asyncio
async def test_gateway_send_unknown_platform():
    """Gateway.send() should log warning for unknown platforms, not raise."""
    from app.messaging.gateway import MessagingGateway
    gw = MessagingGateway()
    # Should not raise — just logs a warning
    await gw.send("nonexistent_platform", "channel_123", "Hello")


@pytest.mark.asyncio
async def test_gateway_agent_cache_creates_agent():
    """Gateway should create and cache an agent for a session_key."""
    from app.messaging.gateway import MessagingGateway
    gw = MessagingGateway()
    agent = gw._get_or_create_agent("user_test_001")
    assert agent is not None
    # Same key returns cached instance
    agent2 = gw._get_or_create_agent("user_test_001")
    assert agent is agent2
