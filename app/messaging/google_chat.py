from __future__ import annotations
"""Google Chat adapter — stub implementation.

Google Chat bots require a service account with domain-wide delegation and
the Google Chat API enabled.  The full implementation needs the google-auth
library for service-account JWT tokens.
"""
import os
from app.messaging.base import BaseMessagingAdapter
from app.logger import logger


class GoogleChatAdapter(BaseMessagingAdapter):
    """Google Chat adapter (stub).

    Environment variables:
        GOOGLE_CHAT_SERVICE_ACCOUNT — path to a JSON service-account key file
    """

    platform_name = "google_chat"

    def __init__(self) -> None:
        sa_path = os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT", "")
        super().__init__(token=sa_path)

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[GoogleChat] Not configured (GOOGLE_CHAT_SERVICE_ACCOUNT not set)")
            return
        logger.info("[GoogleChat] Stub mode — google-auth integration not yet implemented")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[GoogleChat] Stub mode — not configured")
            return
        logger.info("[GoogleChat] Stub mode — webhook listener not yet implemented")

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[GoogleChat:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            import aiohttp
            import json

            # Load service account and obtain access token (placeholder)
            try:
                with open(self.token, "r") as f:
                    sa_data = json.load(f)
                project_id = sa_data.get("project_id", "")
                logger.info(f"[GoogleChat] Service account loaded for project {project_id}")
            except Exception as e:
                logger.warning(f"[GoogleChat] Failed to read service account: {e}")
                return

            # Google Chat API v1 — requires OAuth2 bearer token from service account
            url = "https://chat.googleapis.com/v1/spaces/{space}/messages"
            payload = {"text": text[:4096]}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[GoogleChat] Send error {resp.status}: {body[:200]}")
                    else:
                        logger.debug("[GoogleChat] Message sent successfully")
        except Exception as e:
            logger.warning(f"[GoogleChat] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[GoogleChat] Disconnected")
