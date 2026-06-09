from __future__ import annotations
"""Matrix adapter — functional if MATRIX_HOMESERVER + MATRIX_ACCESS_TOKEN are set, stub otherwise.

When the matrix-nio library is available it provides full async Matrix support;
otherwise this adapter falls back to a basic REST-only implementation.
"""
import os
import asyncio
import aiohttp
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class MatrixAdapter(BaseMessagingAdapter):
    """Matrix protocol adapter via Homeserver REST API.

    Environment variables:
        MATRIX_HOMESERVER   — e.g. ``https://matrix.org``
        MATRIX_ACCESS_TOKEN — bearer token obtained after login
        MATRIX_USER_ID      — full MXID, e.g. ``@bot:matrix.org``
    """

    platform_name = "matrix"
    _POLL_INTERVAL = 3  # seconds

    def __init__(self) -> None:
        self._homeserver = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
        user_id = os.getenv("MATRIX_USER_ID", "")
        token = os.getenv("MATRIX_ACCESS_TOKEN", "")
        super().__init__(token=token)
        self._user_id = user_id
        self._next_batch: str = ""

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Matrix] Not configured (MATRIX_HOMESERVER / MATRIX_ACCESS_TOKEN not set)")
            return
        logger.info(f"[Matrix] Connecting to {self._homeserver} as {self._user_id}...")
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self.token}"}
                async with session.get(
                    f"{self._homeserver}/_matrix/client/v3/sync",
                    headers=headers,
                    params={"timeout": "0", "filter": '{"room":{"timeline":{"limit":0}}}'},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._next_batch = data.get("next_batch", "")
                        logger.info("[Matrix] Connected — sync token acquired")
                    else:
                        logger.warning(f"[Matrix] Initial sync failed: {resp.status}")
        except Exception as e:
            logger.warning(f"[Matrix] Connection error: {e}")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Matrix] Stub mode — not configured")
            return
        logger.info("[Matrix] Starting long-poll sync loop")
        self._running = True

        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {self.token}"}
            while self._running:
                try:
                    params = {"timeout": "30000"}
                    if self._next_batch:
                        params["since"] = self._next_batch

                    async with session.get(
                        f"{self._homeserver}/_matrix/client/v3/sync",
                        headers=headers,
                        params=params,
                    ) as resp:
                        if resp.status != 200:
                            logger.error(f"[Matrix] Sync error {resp.status}")
                            await asyncio.sleep(self._POLL_INTERVAL)
                            continue

                        data = await resp.json()
                        self._next_batch = data.get("next_batch", "")

                        for room_id, room_data in data.get("rooms", {}).get("join", {}).items():
                            for event in room_data.get("timeline", {}).get("events", []):
                                if (
                                    event.get("type") == "m.room.message"
                                    and event.get("sender") != self._user_id
                                ):
                                    msg_body = event.get("content", {}).get("body", "")
                                    sender = event.get("sender", "")
                                    msg = IncomingMessage(
                                        platform="matrix",
                                        user_id=sender,
                                        channel_id=room_id,
                                        text=msg_body,
                                        message_id=event.get("event_id"),
                                    )
                                    await on_message(msg)
                except Exception as e:
                    logger.error(f"[Matrix] Sync error: {e}")
                    await asyncio.sleep(self._POLL_INTERVAL)

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Matrix:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            url = f"{self._homeserver}/_matrix/client/v3/rooms/{channel_id}/send/m.room.message"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "msgtype": "m.text",
                "body": text[:4000],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Matrix] Send error {resp.status}: {body[:200]}")
                    else:
                        logger.debug("[Matrix] Message sent successfully")
        except Exception as e:
            logger.warning(f"[Matrix] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Matrix] Disconnected")
