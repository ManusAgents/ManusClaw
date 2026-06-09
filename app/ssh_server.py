"""ManusClaw SSH Server.

Provides an SSH server for remote gateway control using a restricted shell.
SSH clients authenticate via public keys from ``~/.manusclaw/ssh/authorized_keys``
and can run a limited set of ManusClaw management commands.

Configuration (config.toml or env vars):

    [ssh]
    enabled = true
    port = 2222
    host_key_path = "~/.manusclaw/ssh/host_key"
    authorized_keys_path = "~/.manusclaw/ssh/authorized_keys"

Environment Variables:
    MANUSCLAW_SSH_ENABLED    — Enable SSH server (default: ``false``)
    MANUSCLAW_SSH_PORT       — SSH port (default: ``2222``)
    MANUSCLAW_SSH_HOST_KEY   — Path to host key file
    MANUSCLAW_SSH_AUTH_KEYS  — Path to authorized_keys file

Requirements:
    pip install asyncssh

Stub Mode:
    If ``asyncssh`` is not installed, the server logs a warning and does not start.
    All methods are safe to call — they log and return without error.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Optional

from app.logger import logger

# ─── Optional Import ──────────────────────────────────────────────────────────

_HAS_ASYNCSSH: bool = False
try:
    import asyncssh as _asyncssh_mod
    _HAS_ASYNCSSH = True
except ImportError:
    _asyncssh_mod = None  # type: ignore[assignment]

# ─── Configuration ──────────────────────────────────────────────────────────

_MANUSCLAW_HOME = Path(os.getenv("MANUSCLAW_HOME", str(Path.home() / ".manusclaw")))
_SSH_DIR = _MANUSCLAW_HOME / "ssh"

_ENABLED: bool = os.getenv("MANUSCLAW_SSH_ENABLED", "false").lower() in ("1", "true", "yes")
_PORT: int = int(os.getenv("MANUSCLAW_SSH_PORT", "2222"))
_HOST_KEY_PATH: str = os.getenv("MANUSCLAW_SSH_HOST_KEY", str(_SSH_DIR / "host_key"))
_AUTH_KEYS_PATH: str = os.getenv("MANUSCLAW_SSH_AUTH_KEYS", str(_SSH_DIR / "authorized_keys"))

# Default bind address
_HOST: str = os.getenv("MANUSCLAW_SSH_HOST", "0.0.0.0")

# ─── SSH Server State ──────────────────────────────────────────────────────

_server: Optional[Any] = None  # asyncssh SSHServer instance
_running: bool = False
_start_time: float = 0.0
_connections: set = set()


# ─── AsyncSSH Server Callback ───────────────────────────────────────────────

if _HAS_ASYNCSSH:
    import asyncssh  # type: ignore[no-redef]

    class _ManusClawSSHServer(asyncssh.SSHServer):  # type: ignore[misc]
        """AsyncSSH server callback for ManusClaw restricted shell.

        Handles:
        - Public key authentication from authorized_keys file
        - Creating restricted shell sessions
        - Connection tracking
        """

        def __init__(self) -> None:
            self._conn: Optional[asyncssh.SSHServerConnection] = None  # type: ignore[assignment]

        def connection_made(self, conn: Any) -> None:
            """Called when a new SSH connection is established."""
            self._conn = conn
            peer = conn.get_extra_info("peername", ("?", "?"))
            logger.info(f"[SSH] Connection from {peer[0]}:{peer[1]}")
            _connections.add(conn)

        def connection_lost(self, exc: Optional[Exception]) -> None:
            """Called when an SSH connection is closed."""
            if self._conn:
                _connections.discard(self._conn)
            reason = str(exc) if exc else "clean close"
            logger.info(f"[SSH] Connection closed: {reason}")

        def begin_auth(self, username: str) -> bool:
            """Return True to initiate authentication, False to skip (no auth).

            We always authenticate using public keys from authorized_keys.
            """
            return True

        def public_key_auth_supported(self) -> bool:
            """Indicate that we support public key authentication."""
            return True

        async def validate_public_key(self, username: str, key: Any) -> bool:
            """Validate a client's public key against authorized_keys.

            Args:
                username: The username the client is authenticating as.
                key: The client's public key.

            Returns:
                True if the key is authorized, False otherwise.
            """
            auth_keys_path = Path(_AUTH_KEYS_PATH)

            if not auth_keys_path.exists():
                logger.warning(
                    f"[SSH] No authorized_keys file at {auth_keys_path}. "
                    f"Rejecting all connections."
                )
                return False

            try:
                async with asyncssh.read_authorized_keys(auth_keys_path) as authorized:
                    # authorized is an async generator
                    async for auth_key in authorized:
                        if key == auth_key:
                            logger.info(f"[SSH] Authenticated user '{username}'")
                            return True
            except Exception as exc:
                logger.error(f"[SSH] Error reading authorized_keys: {exc}")

            logger.warning(f"[SSH] Key rejected for user '{username}'")
            return False

        async def check_channel_shell_request(
            self,
            channel: Any,
        ) -> bool:
            """Allow shell channel requests (restricted shell)."""
            return True

        async def session_requested(self) -> Optional[_RestrictedShellSession]:  # type: ignore[name-defined]
            """Create a new restricted shell session for the client."""
            return _RestrictedShellSession()

    class _RestrictedShellSession(asyncssh.SSHServerSession):  # type: ignore[misc]
        """SSH session that runs the ManusClaw restricted shell."""

        def __init__(self) -> None:
            self._chan: Optional[asyncssh.SSHServerChannel] = None  # type: ignore[assignment]
            self._shell: Optional[Any] = None  # Lazy import

        def connection_made(self, chan: Any) -> None:
            """Called when the SSH channel is established."""
            self._chan = chan
            from app.ssh.shell import RestrictedShell
            self._shell = RestrictedShell()

        def shell_requested(self) -> bool:
            """Approve shell requests."""
            return True

        async def start(self) -> None:
            """Start the shell session — display MOTD and prompt."""
            if self._chan is None or self._shell is None:
                return

            # Send MOTD
            self._chan.write(self._shell.motd + "\n")

            # Enter interactive loop
            while True:
                try:
                    self._chan.write(self._shell.prompt)
                    line_bytes = await self._chan.readline(max_lines=1)

                    if not line_bytes:
                        # EOF — client disconnected
                        break

                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
                    if not line:
                        continue

                    result = await self._shell.execute(line)

                    # Handle exit
                    parsed = self._shell._parse(line)
                    if parsed and parsed.command in ("exit", "quit"):
                        self._chan.write("Goodbye.\n")
                        self._chan.close()
                        break

                    # Send output
                    output = result.to_output()
                    if output:
                        self._chan.write(output + "\n")

                except asyncssh.BreakReceived:
                    # Ctrl+C — cancel current operation
                    self._chan.write("^C\n")
                    continue
                except Exception as exc:
                    self._chan.write(f"Error: {exc}\n")
                    break

        def terminal_size_changed(
            self,
            width: int,
            height: int,
            pixwidth: int,
            pixheight: int,
        ) -> None:
            """Handle terminal resize events."""
            pass

        def eof_received(self) -> bool:
            """Handle EOF from the client."""
            return True

        def close(self) -> None:
            """Clean up the session."""
            if self._chan:
                try:
                    self._chan.close()
                except Exception:
                    pass


# ─── Public API ─────────────────────────────────────────────────────────────

class SSHServer:
    """ManusClaw SSH server controller.

    Manages the lifecycle of the asyncssh SSH server for remote gateway control.
    Uses public key authentication and a restricted shell with whitelisted commands.

    Usage::

        ssh = SSHServer()
        await ssh.start()   # Blocks — runs the SSH server

        # In a separate task:
        await ssh.stop()    # Graceful shutdown
    """

    def __init__(
        self,
        port: int = _PORT,
        host: str = _HOST,
        host_key_path: str = _HOST_KEY_PATH,
        authorized_keys_path: str = _AUTH_KEYS_PATH,
        enabled: bool = _ENABLED,
    ) -> None:
        self.port = port
        self.host = host
        self.host_key_path = Path(host_key_path)
        self.authorized_keys_path = Path(authorized_keys_path)
        self.enabled = enabled

        self._server: Optional[Any] = None
        self._running: bool = False
        self._start_time: float = 0.0

    @property
    def running(self) -> bool:
        """Whether the SSH server is currently running."""
        return self._running

    async def start(self) -> None:
        """Start the SSH server.

        Creates the SSH directory and host keys if they don't exist,
        then starts listening for incoming SSH connections.

        If asyncssh is not installed, logs a warning and returns immediately
        (stub mode).
        """
        global _server, _running, _start_time

        if not self.enabled:
            logger.info("[SSH] SSH server disabled by configuration")
            return

        if not _HAS_ASYNCSSH:
            logger.warning(
                "[SSH] Stub mode — asyncssh not installed. "
                "Install with: pip install asyncssh"
            )
            return

        # Ensure SSH directory and keys exist
        self._ensure_keys()

        try:
            self._server = await _asyncssh_mod.create_server(
                _ManusClawSSHServer,
                self.host,
                self.port,
                server_host_keys=[str(self.host_key_path)],
            )
            self._running = True
            _server = self._server
            _running = True
            _start_time = time.time()
            self._start_time = time.time()

            logger.info(
                f"[SSH] Server started on {self.host}:{self.port} "
                f"(key: {self.host_key_path})"
            )

        except OSError as exc:
            logger.error(f"[SSH] Failed to start: {exc}")
            self._running = False

        except Exception as exc:
            logger.error(f"[SSH] Unexpected error: {exc}")
            self._running = False

    async def stop(self) -> None:
        """Stop the SSH server gracefully.

        Closes all active connections and stops listening.
        """
        global _server, _running, _connections

        self._running = False
        _running = False

        if self._server:
            self._server.close()
            await asyncio.sleep(0.5)

            # Wait for connections to close
            if _connections:
                logger.info(f"[SSH] Waiting for {len(_connections)} connection(s) to close")
                for conn in list(_connections):
                    try:
                        conn.close()
                    except Exception:
                        pass
                await asyncio.sleep(1.0)

            self._server = None
            _server = None

        logger.info("[SSH] Server stopped")

    def _ensure_keys(self) -> None:
        """Create the SSH directory and generate host keys if missing."""
        self.host_key_path.parent.mkdir(parents=True, exist_ok=True)

        if self.host_key_path.exists():
            return

        logger.info(f"[SSH] Generating host key: {self.host_key_path}")

        try:
            key = _asyncssh_mod.generate_private_key("ssh-rsa", key_size=2048)
            key.write_private_key(str(self.host_key_path))
            logger.info("[SSH] Host key generated successfully")
        except Exception as exc:
            logger.error(f"[SSH] Failed to generate host key: {exc}")
            raise

        # Also create an empty authorized_keys file if it doesn't exist
        if not self.authorized_keys_path.exists():
            self.authorized_keys_path.parent.mkdir(parents=True, exist_ok=True)
            self.authorized_keys_path.write_text(
                "# ManusClaw SSH authorized_keys\n"
                "# Add public keys below (one per line):\n"
                "# ssh-rsa AAAA... user@host\n",
                encoding="utf-8",
            )
            logger.info(
                f"[SSH] Created empty authorized_keys at {self.authorized_keys_path}"
            )

    async def status(self) -> dict[str, Any]:
        """Return the current status of the SSH server.

        Returns:
            Dictionary with server status information.
        """
        uptime = time.time() - self._start_time if self._start_time else 0

        return {
            "enabled": self.enabled,
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "host_key_path": str(self.host_key_path),
            "authorized_keys_path": str(self.authorized_keys_path),
            "active_connections": len(_connections),
            "uptime_s": uptime,
            "stub_mode": not _HAS_ASYNCSSH,
        }
