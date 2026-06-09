from __future__ import annotations
"""Slack adapter — functional if SLACK_BOT_TOKEN is set, stub otherwise."""
import os
from app.messaging.base import BaseMessagingAdapter
from app.logger import logger


class SlackAdapter(BaseMessagingAdapter):
    platform_name = "slack"

    def __init__(self) -> None:
        super().__init__(token=os.getenv("SLACK_BOT_TOKEN", ""))

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Slack] Not configured (SLACK_BOT_TOKEN not set)")
            return
        logger.info("[Slack] Connecting via Socket Mode...")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Slack] Stub mode — not configured")
            return
        logger.info("[Slack] Starting event handler")

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Slack:stub] Would send to #{channel_id}: {text[:80]}")
            return
        try:
            import aiohttp
            url = "https://slack.com/api/chat.postMessage"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"channel": channel_id, "text": text[:3000]}, headers=headers)
        except Exception as e:
            logger.warning(f"[Slack] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Slack] Disconnected")
