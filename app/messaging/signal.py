from __future__ import annotations
"""Signal adapter — functional if SIGNAL_CLI_REST_URL is reachable, stub otherwise.

Uses signal-cli's REST API for sending/receiving messages.  signal-cli must be
running separately with ``signal-cli --config /path/to/config rest``.
"""
import os
import asyncio
import aiohttp
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class SignalAdapter(BaseMessagingAdapter):
    """signal-cli REST API adapter.

    Environment variables:
        SIGNAL_CLI_REST_URL — base URL of the signal-cli REST service
                              (default: ``http://localhost:8080``)
        SIGNAL_CLI_NUMBER   — phone number registered with signal-cli
    """

    platform_name = "signal"
    _DEFAULT_URL = "http://localhost:8080"
    _POLL_INTERVAL = 5  # seconds

    def __init__(self) -> None:
        self._rest_url = os.getenv("SIGNAL_CLI_REST_URL", self._DEFAULT_URL).rstrip("/")
        self._number = os.getenv("SIGNAL_CLI_NUMBER", "")
        super().__init__(token=self._number)

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Signal] Not configured (SIGNAL_CLI_NUMBER not set)")
            return
        logger.info(f"[Signal] Connecting to signal-cli at {self._rest_url}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self._rest_url}/v1/about") as resp:
                    if resp.status == 200:
                        logger.info("[Signal] signal-cli REST API is reachable")
                    else:
                        logger.warning(f"[Signal] signal-cli returned {resp.status}")
        except Exception as e:
            logger.warning(f"[Signal] Cannot reach signal-cli: {e}")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Signal] Stub mode — not configured")
            return
        logger.info("[Signal] Starting polling loop")
        self._running = True

        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.get(f"{self._rest_url}/v1/receive") as resp:
                        if resp.status != 200:
                            logger.error(f"[Signal] Receive error {resp.status}")
                            await asyncio.sleep(self._POLL_INTERVAL)
                            continue

                        data = await resp.json()
                        for envelope in data if isinstance(data, list) else []:
                            source = envelope.get("source", "")
                            envelope_type = envelope.get("type", "")
                            if envelope_type == "UNDELIVERABLE":
                                continue
                            msg_data = envelope.get("dataMessage", {})
                            text = msg_data.get("message", "")
                            if not text:
                                continue
                            group_info = msg_data.get("groupInfo")
                            channel_id = (
                                group_info.get("groupId", source)
                                if group_info
                                else source
                            )
                            msg = IncomingMessage(
                                platform="signal",
                                user_id=source,
                                channel_id=channel_id,
                                text=text,
                                message_id=envelope.get("sourceNumber", ""),
                            )
                            await on_message(msg)
                except Exception as e:
                    logger.error(f"[Signal] Polling error: {e}")
                    await asyncio.sleep(self._POLL_INTERVAL)

                await asyncio.sleep(self._POLL_INTERVAL)

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Signal:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            url = f"{self._rest_url}/v1/send"
            payload = {
                "number": self.token,
                "recipients": [channel_id],
                "message": text[:2000],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 201:
                        body = await resp.text()
                        logger.warning(f"[Signal] Send error {resp.status}: {body[:200]}")
                    else:
                        logger.debug("[Signal] Message sent successfully")
        except Exception as e:
            logger.warning(f"[Signal] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Signal] Disconnected")
