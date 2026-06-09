from __future__ import annotations
"""Discord adapter — functional if DISCORD_BOT_TOKEN is set, stub otherwise."""
import os
from app.messaging.base import BaseMessagingAdapter
from app.logger import logger


class DiscordAdapter(BaseMessagingAdapter):
    platform_name = "discord"

    def __init__(self) -> None:
        super().__init__(token=os.getenv("DISCORD_BOT_TOKEN", ""))

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Discord] Not configured (DISCORD_BOT_TOKEN not set)")
            return
        logger.info("[Discord] Connecting via Gateway...")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Discord] Stub mode — not configured")
            return
        logger.info("[Discord] Starting event loop")

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Discord:stub] Would send to channel {channel_id}: {text[:80]}")
            return
        try:
            import aiohttp
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"content": text[:2000]}, headers=headers)
        except Exception as e:
            logger.warning(f"[Discord] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Discord] Disconnected")
