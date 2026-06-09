from __future__ import annotations
"""IRC adapter — pure async IRC client using asyncio TCP streams.

Connects to an IRC server, joins configured channels, and relays PRIVMSG
events as incoming messages.  Environment-driven configuration.
"""
import os
import asyncio
import re
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.logger import logger


class IRCAdapter(BaseMessagingAdapter):
    """IRC protocol adapter.

    Environment variables:
        IRC_SERVER   — hostname of the IRC server (e.g. ``irc.libera.chat``)
        IRC_PORT     — port number (default: ``6667``, use ``6697`` for TLS)
        IRC_NICK     — bot nickname
        IRC_CHANNELS — comma-separated list of channels to join (``#general,#dev``)
        IRC_PASS     — optional NickServ / server password
    """

    platform_name = "irc"

    def __init__(self) -> None:
        self._server = os.getenv("IRC_SERVER", "")
        self._port = int(os.getenv("IRC_PORT", "6667"))
        self._nick = os.getenv("IRC_NICK", "")
        channels = os.getenv("IRC_CHANNELS", "")
        self._channels = [c.strip() for c in channels.split(",") if c.strip()]
        self._pass = os.getenv("IRC_PASS", "")
        super().__init__(token=self._server if (self._server and self._nick) else "")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if not self.is_configured():
            logger.info("[IRC] Not configured (IRC_SERVER / IRC_NICK not set)")
            return
        logger.info(f"[IRC] Connecting to {self._server}:{self._port} as {self._nick}...")
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._server, self._port
            )
            if self._pass:
                self._write(f"PASS {self._pass}")
            self._write(f"NICK {self._nick}")
            self._write(f"USER {self._nick} 0 * :{self._nick}")
            logger.info("[IRC] TCP connection established")
        except Exception as e:
            logger.warning(f"[IRC] Connection failed: {e}")

    def _write(self, line: str) -> None:
        """Send a raw line to the IRC server."""
        if self._writer:
            self._writer.write(f"{line}\r\n".encode("utf-8"))

    async def _register_and_join(self) -> None:
        """Wait for RPL_ENDOFMOTD / ERR_NOMOTD then join channels."""
        assert self._reader is not None
        while True:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("[IRC] Timeout waiting for registration")
                break
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            logger.debug(f"[IRC] <<< {decoded}")
            if re.search(r"^(:\S+ )?(001|422)", decoded):
                logger.info("[IRC] Registered with server")
                for channel in self._channels:
                    self._write(f"JOIN {channel}")
                break

    async def start(self, on_message) -> None:
        if not self.is_configured() or not self._writer:
            logger.info("[IRC] Stub mode — not configured or not connected")
            return
        logger.info("[IRC] Starting message loop")
        self._running = True

        asyncio.create_task(self._register_and_join())

        # Spawn a keep-alive PING task
        async def _ping_loop():
            while self._running:
                await asyncio.sleep(60)
                if self._writer and not self._writer.is_closing():
                    self._write("PING :manusclaw")

        asyncio.create_task(_ping_loop())

        assert self._reader is not None
        while self._running:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=120)
            except asyncio.TimeoutError:
                continue
            if not line:
                logger.warning("[IRC] Connection closed by server")
                break

            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue

            # Handle PING
            if decoded.startswith("PING "):
                self._write(decoded.replace("PING", "PONG", 1))
                continue

            # Parse PRIVMSG
            match = re.match(
                r":(?P<nick>[^!]+)![^@]+@[^ ]+ PRIVMSG (?P<channel>\S+) :(?P<text>.*)",
                decoded,
            )
            if match:
                msg = IncomingMessage(
                    platform="irc",
                    user_id=match.group("nick"),
                    channel_id=match.group("channel"),
                    text=match.group("text"),
                )
                await on_message(msg)

    async def send(self, channel_id: str, text: str) -> None:
        if not self.is_configured() or not self._writer:
            logger.info(f"[IRC:stub] Would send to {channel_id}: {text[:80]}")
            return
        try:
            # IRC messages are limited to 512 bytes total
            for chunk in self._split_irc_message(text):
                self._write(f"PRIVMSG {channel_id} :{chunk}")
                await asyncio.sleep(0.5)  # avoid excess flood
        except Exception as e:
            logger.warning(f"[IRC] Send failed: {e}")

    @staticmethod
    def _split_irc_message(text: str, max_len: int = 400) -> list[str]:
        """Split a long message into IRC-safe chunks."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            chunks.append(text[:max_len])
            text = text[max_len:]
        return chunks

    async def disconnect(self) -> None:
        self._running = False
        if self._writer and not self._writer.is_closing():
            self._write("QUIT :ManusClaw IRC adapter shutting down")
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("[IRC] Disconnected")
