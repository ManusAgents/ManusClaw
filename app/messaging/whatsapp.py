from __future__ import annotations
"""WhatsApp adapter — functional if WHATSAPP_ACCESS_TOKEN is set, stub otherwise.

Uses the WhatsApp Business Cloud API (graph.facebook.com) for sending messages
and webhook endpoints for receiving inbound messages.
"""
import os
import asyncio
import aiohttp
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class WhatsAppAdapter(BaseMessagingAdapter):
    """WhatsApp Business Cloud API adapter.

    Environment variables:
        WHATSAPP_BUSINESS_PHONE_ID — the phone number ID of the business account
        WHATSAPP_ACCESS_TOKEN       — bearer token for the Cloud API
    """

    platform_name = "whatsapp"
    _API_BASE = "https://graph.facebook.com/v18.0"

    def __init__(self) -> None:
        self._phone_id = os.getenv("WHATSAPP_BUSINESS_PHONE_ID", "")
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        super().__init__(token=token)
        self._verify_token = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "manusclaw_verify")

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[WhatsApp] Not configured (WHATSAPP_ACCESS_TOKEN not set)")
            return
        logger.info(f"[WhatsApp] Connecting (phone_id={self._phone_id})...")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[WhatsApp] Stub mode — not configured")
            return
        logger.info("[WhatsApp] Webhook listener ready (webhook path: /webhooks/whatsapp)")
        # WhatsApp uses webhooks for inbound messages — the HTTP server in
        # app/server/main.py should route GET/POST /webhooks/whatsapp here.
        # In polling fallback mode we do nothing; messages arrive via webhook.

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured():
            logger.info(f"[WhatsApp:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            url = f"{self._API_BASE}/{self._phone_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": channel_id,
                "type": "text",
                "text": {"body": text[:4096]},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[WhatsApp] Send error {resp.status}: {body[:200]}")
                    else:
                        logger.debug("[WhatsApp] Message sent successfully")
        except Exception as e:
            logger.warning(f"[WhatsApp] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[WhatsApp] Disconnected")

    # ------------------------------------------------------------------
    # Webhook helpers — called by the HTTP server route handler
    # ------------------------------------------------------------------

    async def verify_webhook(self, mode: str, token: str, challenge: str) -> str | None:
        """Verify webhook subscription (GET /webhooks/whatsapp).

        Returns the challenge string on success, or *None* on failure.
        """
        if mode == "subscribe" and token == self._verify_token:
            logger.info("[WhatsApp] Webhook verified")
            return challenge
        logger.warning("[WhatsApp] Webhook verification failed")
        return None

    async def handle_webhook_event(self, payload: dict) -> list[IncomingMessage]:
        """Process an incoming webhook payload and return parsed messages."""
        messages: list[IncomingMessage] = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg_obj in value.get("messages", []):
                        contacts = value.get("contacts", [])
                        sender_name = contacts[0]["wa_id"] if contacts else "unknown"
                        msg = IncomingMessage(
                            platform="whatsapp",
                            user_id=msg_obj.get("from", sender_name),
                            channel_id=msg_obj.get("from", ""),
                            text=msg_obj.get("text", {}).get("body", ""),
                            message_id=msg_obj.get("id"),
                        )
                        messages.append(msg)
        except Exception as e:
            logger.error(f"[WhatsApp] Webhook parse error: {e}")
        return messages
