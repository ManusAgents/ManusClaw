from __future__ import annotations
"""Email adapter — stub implementation.

Email bots require SMTP for sending and IMAP idle for receiving.
The full implementation needs proper TLS handling and MIME formatting.
"""
import os
import asyncio
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class EmailAdapter(BaseMessagingAdapter):
    """Email adapter (stub) for receiving and sending via SMTP/IMAP.

    Environment variables:
        EMAIL_SMTP_HOST — SMTP server hostname
        EMAIL_SMTP_PORT — SMTP server port (default: ``587``)
        EMAIL_IMAP_HOST — IMAP server hostname
        EMAIL_IMAP_PORT — IMAP server port (default: ``993``)
        EMAIL_USER      — login username (email address)
        EMAIL_PASS      — login password / app-specific password
    """

    platform_name = "email"

    def __init__(self) -> None:
        self._smtp_host = os.getenv("EMAIL_SMTP_HOST", "")
        self._smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        self._imap_host = os.getenv("EMAIL_IMAP_HOST", "")
        self._imap_port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        user = os.getenv("EMAIL_USER", "")
        password = os.getenv("EMAIL_PASS", "")
        super().__init__(token=user if (user and password) else "")
        self._password = password

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Email] Not configured (EMAIL_USER / EMAIL_PASS not set)")
            return
        logger.info(f"[Email] Stub mode — SMTP {self._smtp_host}:{self._smtp_port}, IMAP {self._imap_host}:{self._imap_port}")

    async def start(self, on_message) -> None:
        if not self.is_configured():
            logger.info("[Email] Stub mode — not configured")
            return
        logger.info("[Email] Stub mode — IMAP IDLE polling not yet implemented")

    async def send(self, channel_id: str, text: str) -> None:
        """Send an email. ``channel_id`` should be the recipient address."""
        if not self.is_configured():
            logger.info(f"[Email:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(text[:10000])
            msg["Subject"] = "ManusClaw Response"
            msg["From"] = self.token
            msg["To"] = channel_id

            await asyncio.to_thread(
                self._smtp_send, msg
            )
            logger.debug(f"[Email] Message sent to {channel_id}")
        except Exception as e:
            logger.warning(f"[Email] Send failed: {e}")

    def _smtp_send(self, msg) -> None:
        """Blocking SMTP send — runs in a thread via asyncio.to_thread."""
        import smtplib

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
            server.starttls()
            server.login(self.token, self._password)
            server.send_message(msg)

    async def disconnect(self) -> None:
        self._running = False
        logger.info("[Email] Disconnected")
