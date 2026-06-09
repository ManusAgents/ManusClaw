from __future__ import annotations
"""Twitch adapter — IRC-based Twitch chat connection.

Twitch chat uses a modified IRC protocol over TLS.  This adapter connects
to ``irc.chat.twitch.tv:4443`` and joins the configured channel.
"""
import os
import asyncio
import re
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class TwitchAdapter(BaseMessagingAdapter):
    """Twitch chat adapter.

    Environment variables:
        TWITCH_BOT_TOKEN  — OAuth token (``oauth:xxxxxxxxxx`` from Twitch)
        TWITCH_CHANNEL     — channel name to join (without the ``#`` prefix)
        TWITCH_BOT_NICK    — bot username (defaults to the channel name)
    """

    platform_name = "twitch"
    _TWITCH_IRC_HOST = "irc.chat.twitch.tv"
    _TWITCH_IRC_PORT = 4443  # TLS

    def __init__(self) -> None:
        self._bot_token = os.getenv("TWITCH_BOT_TOKEN", "")
        self._channel = os.getenv("TWITCH_CHANNEL", "")
        self._nick = os.getenv("TWITCH_BOT_NICK", self._channel)
        super().__init__(token=self._bot_token if (self._bot_token and self._channel) else "")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[Twitch] Not configured (TWITCH_BOT_TOKEN / TWITCH_CHANNEL not set)")
            return
        logger.info(f"[Twitch] Connecting to {self._TWITCH_IRC_HOST} as {self._nick}...")
        try:
            import ssl

            ctx = ssl.create_default_context()
            self._reader, self._writer = await asyncio.open_connection(
                self._TWITCH_IRC_HOST, self._TWITCH_IRC_PORT, ssl=ctx,
            )
            # Authenticate
            self._write(f"PASS {self._bot_token}")
            self._write(f"NICK {self._nick}")
            logger.info("[Twitch] TCP+TLS connection established")
        except Exception as e:
            logger.warning(f"[Twitch] Connection failed: {e}")

    def _write(self, line: str) -> None:
        if self._writer:
            self._writer.write(f"{line}\r\n".encode("utf-8"))

    async def start(self, on_message) -> None:
        if not self.is_configured() or not self._writer:
            logger.info("[Twitch] Stub mode — not configured or not connected")
            return
        logger.info(f"[Twitch] Starting chat loop for #{self._channel}")
        self._running = True

        # Join channel after a short delay for registration
        await asyncio.sleep(2)
        self._write(f"JOIN #{self._channel}")
        self._write(f"CAP REQ :twitch.tv/tags twitch.tv/commands")

        # PING keep-alive task
        async def _ping_loop():
            while self._running:
                await asyncio.sleep(120)
                if self._writer and not self._writer.is_closing():
                    self._write("PING :tmi.twitch.tv")

        asyncio.create_task(_ping_loop())

        assert self._reader is not None
        while self._running:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                continue
            if not line:
                logger.warning("[Twitch] Connection closed by server")
                break

            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue

            # Handle PING/PONG
            if decoded.startswith("PING "):
                self._write(decoded.replace("PING", "PONG", 1))
                continue

            # Handle JOIN confirmation
            if re.search(r"JOIN :#", decoded):
                logger.debug(f"[Twitch] {decoded}")
                continue

            # Parse PRIVMSG
            # @badge-info=...;user-id=123 :user!user@user.tmi.twitch.tv PRIVMSG #channel :message
            match = re.match(
                r"@\S+ :(?P<nick>[^!]+)!\S+@\S+\.tmi\.twitch\.tv PRIVMSG #(?P<channel>\S+) :(?P<text>.*)",
                decoded,
            )
            if match:
                msg = IncomingMessage(
                    platform="twitch",
                    user_id=match.group("nick"),
                    channel_id=f"#{match.group('channel')}",
                    text=match.group("text"),
                )
                await on_message(msg)

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured() or not self._writer:
            logger.info(f"[Twitch:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            # Twitch messages are limited to ~500 characters
            chunk = text[:500]
            self._write(f"PRIVMSG {channel_id} :{chunk}")
            await asyncio.sleep(1.5)  # Twitch rate limit: 20 msgs / 30 sec
        except Exception as e:
            logger.warning(f"[Twitch] Send failed: {e}")

    async def disconnect(self) -> None:
        self._running = False
        if self._writer and not self._writer.is_closing():
            self._write(f"PART #{self._channel}")
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("[Twitch] Disconnected")
