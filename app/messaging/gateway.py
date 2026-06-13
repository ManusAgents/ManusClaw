from __future__ import annotations
"""MessagingGateway — manages all platform adapters and agent instance cache.

Updated to use AgentRouter for per-channel multi-agent routing.
Backward compatible — falls back to direct Manus creation when
AgentRouter is not configured.
"""
import asyncio
from collections import OrderedDict
from typing import Callable, Optional
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.messaging.telegram import TelegramAdapter
from app.messaging.discord import DiscordAdapter
from app.messaging.slack import SlackAdapter
from app.messaging.whatsapp import WhatsAppAdapter
from app.messaging.signal import SignalAdapter
from app.messaging.teams import TeamsAdapter
from app.messaging.matrix import MatrixAdapter
from app.messaging.irc import IRCAdapter
from app.messaging.google_chat import GoogleChatAdapter
from app.messaging.webchat import WebChatAdapter
from app.messaging.email import EmailAdapter
from app.messaging.twitch import TwitchAdapter
from app.logger import logger

_CACHE_SIZE = 128
_IDLE_TTL = 300  # seconds


def _safe_create_task(coro):
    """Safely schedule an async cleanup coroutine.

    Uses asyncio.get_running_loop() instead of the deprecated
    asyncio.get_event_loop().create_task(). If no loop is running
    (e.g., called from a sync context during shutdown), logs a warning
    instead of crashing.
    """
    try:
        loop = asyncio.get_running_loop()
        return loop.create_task(coro)
    except RuntimeError:
        # No running loop — can't schedule; best-effort log
        logger.warning("No event loop for async cleanup — coroutine dropped")
        return None


class MessagingGateway:
    """
    Central gateway that routes messages from any platform to an agent instance.
    Caches up to 128 active agent instances with idle TTL eviction.
    """

    def __init__(self, use_router: bool = True) -> None:
        self._adapters: list[BaseMessagingAdapter] = [
            TelegramAdapter(),
            DiscordAdapter(),
            SlackAdapter(),
            WhatsAppAdapter(),
            SignalAdapter(),
            TeamsAdapter(),
            MatrixAdapter(),
            IRCAdapter(),
            GoogleChatAdapter(),
            WebChatAdapter(),
            EmailAdapter(),
            TwitchAdapter(),
        ]
        self._agent_cache: OrderedDict = OrderedDict()
        self._last_active: dict[str, float] = {}
        self._approval_pending: dict[str, dict] = {}
        # AgentRouter for per-channel multi-agent routing
        self._router: Optional["AgentRouter"] = None
        self._use_router = use_router
        if use_router:
            try:
                from app.agent.router import AgentRouter
                self._router = AgentRouter()
                logger.info("[Gateway] AgentRouter initialized for multi-agent routing")
            except Exception as e:
                logger.warning(f"[Gateway] AgentRouter init failed, using legacy mode: {e}")
                self._use_router = False

    async def start_all(self, on_message: Optional[Callable] = None) -> None:
        handler = on_message or self._default_handler
        tasks = []
        for adapter in self._adapters:
            await adapter.connect()
            tasks.append(asyncio.create_task(adapter.start(handler)))
        logger.info(f"[Gateway] Started {len(tasks)} platform adapters")

    async def send(self, platform: str, channel_id: str, text: str) -> None:
        for adapter in self._adapters:
            if adapter.platform_name == platform:
                await adapter.send(channel_id, text)
                return
        logger.warning(f"[Gateway] No adapter for platform: {platform}")

    async def _default_handler(self, msg: IncomingMessage) -> None:
        logger.info(f"[Gateway] Message from {msg.platform}/{msg.user_id}: {msg.text[:80]}")
        # Use AgentRouter if available, otherwise fall back to legacy cache
        if self._use_router and self._router:
            agent = self._router.get_agent(msg.platform, msg.user_id, msg.channel_id)
        else:
            agent = self._get_or_create_agent(msg.session_key)
        try:
            result = await agent.run(msg.text)
            # FIX: Raise truncation limit from 4000 to 8000 chars.
            # Many agent outputs exceed 4000 chars, causing information loss.
            # 8000 is a better balance between completeness and messaging limits.
            await self.send(msg.platform, msg.channel_id, result[:8000])
        except Exception as e:
            logger.error(f"[Gateway] Agent error: {e}")
            await self.send(msg.platform, msg.channel_id, f"Error: {e}")

    def _get_or_create_agent(self, session_key: str):
        import time
        from app.agent.manus import Manus
        now = time.monotonic()

        # Evict idle agents — FIX: call cleanup on evicted agents
        evict = [k for k, t in self._last_active.items() if now - t > _IDLE_TTL]
        for k in evict:
            agent = self._agent_cache.pop(k, None)
            self._last_active.pop(k, None)
            # FIX: Call cleanup on evicted agents to release resources
            if agent is not None and hasattr(agent, "cleanup"):
                try:
                    import asyncio as _asyncio
                    coro = agent.cleanup()
                    if _asyncio.iscoroutine(coro):
                        _safe_create_task(coro)
                except Exception as e:
                    logger.warning(f"[Gateway] Cleanup error for evicted agent {k}: {e}")

        # LRU eviction if over cache size — FIX: call cleanup on LRU-evicted agents
        while len(self._agent_cache) >= _CACHE_SIZE:
            k, agent = self._agent_cache.popitem(last=False)
            self._last_active.pop(k, None)
            if agent is not None and hasattr(agent, "cleanup"):
                try:
                    import asyncio as _asyncio
                    coro = agent.cleanup()
                    if _asyncio.iscoroutine(coro):
                        _safe_create_task(coro)
                except Exception as e:
                    logger.warning(f"[Gateway] Cleanup error for LRU-evicted agent {k}: {e}")

        if session_key not in self._agent_cache:
            self._agent_cache[session_key] = Manus()
        self._last_active[session_key] = now
        return self._agent_cache[session_key]

    async def stop_all(self) -> None:
        for adapter in self._adapters:
            await adapter.disconnect()
        if self._router:
            await self._router.shutdown()
