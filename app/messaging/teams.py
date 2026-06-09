from __future__ import annotations
"""Microsoft Teams adapter — stub implementation.

The Bot Framework requires complex OAuth + channel registration that is
better handled through the official Bot Builder SDK.  This adapter provides
the interface contract and logs actions when credentials are present.
"""
import os
import aiohttp
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class TeamsAdapter(BaseMessagingAdapter):
    """Microsoft Bot Framework / Teams adapter (stub).

    Environment variables:
        MICROSOFT_APP_ID       — Azure AD app registration ID
        MICROSOFT_APP_PASSWORD — client secret for the app registration
    """

    platform_name = "teams"

    def __init__(self) -> None:
        app_id = os.getenv("MICROSOFT_APP_ID", "")
        app_password = os.getenv("MICROSOFT_APP_PASSWORD", "")
        # Use app_id as the sentinel token; both must be present for full config
        super().__init__(token=app_id if (app_id and app_password) else "")
        self._app_password = app_password

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Teams] Not configured (MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD not set)")
            return
        logger.info("[Teams] Bot Framework credentials found — stub mode (OAuth flow not implemented)")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Teams] Stub mode — not configured")
            return
        logger.info("[Teams] Stub mode — Bot Framework OAuth + WebSocket not yet implemented")

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[Teams:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            # Bot Framework v3/v4 send endpoint
            service_url = "https://smba.trafficmanager.net/amer/"
            url = f"{service_url}v3/conversations/{channel_id}/activities"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "type": "message",
                "text": text[:4000],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Teams] Send error {resp.status}: {body[:200]}")
                    else:
                        logger.debug("[Teams] Message sent successfully")
        except Exception as e:
            logger.warning(f"[Teams] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Teams] Disconnected")
