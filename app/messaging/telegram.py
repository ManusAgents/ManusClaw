from __future__ import annotations
"""Telegram adapter — functional if TELEGRAM_BOT_TOKEN is set, stub otherwise."""
import os
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class TelegramAdapter(BaseMessagingAdapter):
    platform_name = "telegram"

    def __init__(self) -> None:
        super().__init__(token=os.getenv("TELEGRAM_BOT_TOKEN", ""))

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Telegram] Not configured (TELEGRAM_BOT_TOKEN not set)")
            return
        logger.info("[Telegram] Connecting...")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Telegram] Stub mode — not configured")
            return
        logger.info("[Telegram] Starting polling loop")
        import asyncio
        import aiohttp
        
        self._running = True
        offset = 0
        
        async with aiohttp.ClientSession() as session:
            while self._running:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates?timeout=30&offset={offset}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.error(f"[Telegram] Error {resp.status}")
                            await asyncio.sleep(5)
                            continue
                        
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            if "message" in update and "text" in update["message"]:
                                msg = IncomingMessage(
                                    platform="telegram",
                                    channel_id=str(update["message"]["chat"]["id"]),
                                    user_id=str(update["message"]["from"]["id"]),
                                    text=update["message"]["text"],
                                    session_key=f"tg_{update['message']['chat']['id']}"
                                )
                                await on_message(msg)
                except Exception as e:
                    logger.error(f"[Telegram] Polling error: {e}")
                    await asyncio.sleep(5)

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Telegram:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"chat_id": channel_id, "text": text[:4096]})
        except Exception as e:
            logger.warning(f"[Telegram] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Telegram] Disconnected")
