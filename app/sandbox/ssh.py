from __future__ import annotations

"""
SSH Sandbox — Remote code execution via SSH connection.

Same interface as DockerSandbox (start, exec, stop). Connects to a
remote host via ``asyncssh`` (preferred) or ``paramiko`` (fallback).

Configuration via environment variables:
  SSH_SANDBOX_HOST     — Remote hostname or IP
  SSH_SANDBOX_USER     — SSH username
  SSH_SANDBOX_KEY_PATH — Path to SSH private key
  SSH_SANDBOX_PORT     — SSH port (default 22)

When not configured, operates in stub mode with a clear error message.
"""

import asyncio
import os
from typing import Optional

from app.exceptions import SandboxError
from app.logger import logger
from app.schema import ToolResult

# Configuration
_SSH_HOST: str = os.getenv("SSH_SANDBOX_HOST", "")
_SSH_USER: str = os.getenv("SSH_SANDBOX_USER", "")
_SSH_KEY_PATH: str = os.getenv("SSH_SANDBOX_KEY_PATH", "")
_SSH_PORT: int = int(os.getenv("SSH_SANDBOX_PORT", "22"))
_SSH_TIMEOUT: int = 30


class SshSandbox:
    """Runs code in a remote SSH environment.

    Implements the same interface as ``DockerSandbox``:
    - ``start()`` — verify SSH connection
    - ``exec(code)`` — execute Python code on the remote host
    - ``stop()`` — close the SSH connection

    File transfer is supported via ``upload_file`` and ``download_file``.
    """

    def __init__(self) -> None:
        self.host = _SSH_HOST
        self.user = _SSH_USER
        self.key_path = _SSH_KEY_PATH
        self.port = _SSH_PORT
        self._timeout = _SSH_TIMEOUT
        self._conn = None  # asyncssh connection or paramiko client
        self._connected = False
        self._backend = None  # "asyncssh" or "paramiko"

    @property
    def is_configured(self) -> bool:
        """Check if SSH sandbox is properly configured."""
        return bool(self.host and self.user)

    async def start(self) -> None:
        """Establish SSH connection to the remote host."""
        if not self.is_configured:
            raise SandboxError(
                "SSH sandbox not configured. Set SSH_SANDBOX_HOST, "
                "SSH_SANDBOX_USER, and optionally SSH_SANDBOX_KEY_PATH."
            )

        # Try asyncssh first
        if await self._connect_asyncssh():
            return

        # Fallback to paramiko
        if await self._connect_paramiko():
            return

        raise SandboxError(
            "Cannot connect via SSH. Install asyncssh or paramiko: "
            "pip install asyncssh paramiko"
        )

    async def _connect_asyncssh(self) -> bool:
        """Try connecting via asyncssh."""
        try:
            import asyncssh  # type: ignore
        except ImportError:
            return False

        try:
            connect_kwargs: dict = {
                "host": self.host,
                "port": self.port,
                "username": self.user,
                "known_hosts": None,  # Accept any host key
            }
            if self.key_path:
                connect_kwargs["client_keys"] = [self.key_path]
            else:
                connect_kwargs["agent_path"] = None

            self._conn = await asyncssh.connect(**connect_kwargs)
            self._connected = True
            self._backend = "asyncssh"
            logger.info(
                f"[SshSandbox] Connected via asyncssh to {self.user}@{self.host}:{self.port}"
            )
            return True
        except Exception as e:
            logger.warning(f"[SshSandbox] asyncssh connection failed: {e}")
            self._conn = None
            return False

    async def _connect_paramiko(self) -> bool:
        """Try connecting via paramiko (blocking, run in thread)."""
        try:
            import paramiko  # type: ignore
        except ImportError:
            return False

        def _connect() -> bool:
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                kwargs: dict = {
                    "hostname": self.host,
                    "port": self.port,
                    "username": self.user,
                    "timeout": self._timeout,
                }
                if self.key_path:
                    key = paramiko.RSAKey.from_private_key_file(self.key_path)
                    kwargs["pkey"] = key
                client.connect(**kwargs)
                self._conn = client
                self._connected = True
                self._backend = "paramiko"
                return True
            except Exception as e:
                logger.warning(f"[SshSandbox] paramiko connection failed: {e}")
                self._conn = None
                return False

        return await asyncio.to_thread(_connect)

    async def exec(self, code: str) -> ToolResult:
        """Execute Python code on the remote host via SSH.

        Args:
            code: Python code string to execute.

        Returns:
            ToolResult with stdout output or error.
        """
        if not self._connected:
            await self.start()

        try:
            if self._backend == "asyncssh" and self._conn:
                return await self._exec_asyncssh(code)
            elif self._backend == "paramiko" and self._conn:
                return await self._exec_paramiko(code)
            else:
                return ToolResult(error="SSH not connected")
        except asyncio.TimeoutError:
            return ToolResult(error=f"SSH execution timed out after {self._timeout}s")
        except Exception as e:
            return ToolResult(error=f"SSH execution error: {e}")

    async def _exec_asyncssh(self, code: str) -> ToolResult:
        """Execute code via asyncssh."""
        import asyncssh  # type: ignore

        # Wrap code in python3 -c
        cmd = f'python3 -c {asyncssh.quote(code)}'

        try:
            result = await asyncio.wait_for(
                self._conn.run(cmd, check=True),
                timeout=self._timeout,
            )
            stdout = result.stdout.strip() if result.stdout else None
            stderr = result.stderr.strip() if result.stderr else None
            if result.exit_status != 0 and stderr:
                return ToolResult(output=stdout, error=stderr)
            return ToolResult(output=stdout or stderr)
        except asyncssh.ProcessError as e:
            return ToolResult(error=f"SSH process error: {e}")
        except Exception as e:
            return ToolResult(error=f"SSH exec error: {e}")

    async def _exec_paramiko(self, code: str) -> ToolResult:
        """Execute code via paramiko (blocking, run in thread)."""
        import shlex

        cmd = f"python3 -c {shlex.quote(code)}"

        def _run() -> tuple[Optional[str], Optional[str], int]:
            try:
                stdin, stdout, stderr = self._conn.exec_command(cmd, timeout=self._timeout)
                exit_code = stdout.channel.recv_exit_status()
                out = stdout.read().decode().strip() or None
                err = stderr.read().decode().strip() or None
                if exit_code != 0 and err:
                    return out, err, exit_code
                return out or err, None, exit_code
            except Exception as e:
                return None, str(e), -1

        output, error, code = await asyncio.to_thread(_run)
        return ToolResult(output=output, error=error)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a file to the remote host via SFTP.

        Args:
            local_path: Path to the local file.
            remote_path: Destination path on the remote host.
        """
        if not self._connected:
            await self.start()

        if self._backend == "asyncssh" and self._conn:
            await self._conn.sftp().put(local_path, remote_path)
            logger.debug(f"[SshSandbox] Uploaded {local_path} → {remote_path}")
        elif self._backend == "paramiko" and self._conn:

            def _upload() -> None:
                sftp = self._conn.open_sftp()
                sftp.put(local_path, remote_path)
                sftp.close()

            await asyncio.to_thread(_upload)
            logger.debug(f"[SshSandbox] Uploaded {local_path} → {remote_path}")
        else:
            raise SandboxError("SSH not connected")

    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the remote host via SFTP.

        Args:
            remote_path: Path to the remote file.
            local_path: Destination path on the local machine.
        """
        if not self._connected:
            await self.start()

        if self._backend == "asyncssh" and self._conn:
            await self._conn.sftp().get(remote_path, local_path)
            logger.debug(f"[SshSandbox] Downloaded {remote_path} → {local_path}")
        elif self._backend == "paramiko" and self._conn:

            def _download() -> None:
                sftp = self._conn.open_sftp()
                sftp.get(remote_path, local_path)
                sftp.close()

            await asyncio.to_thread(_download)
            logger.debug(f"[SshSandbox] Downloaded {remote_path} → {local_path}")
        else:
            raise SandboxError("SSH not connected")

    async def stop(self) -> None:
        """Close the SSH connection."""
        if self._conn:
            try:
                if self._backend == "asyncssh":
                    self._conn.close()
                elif self._backend == "paramiko":

                    def _close() -> None:
                        self._conn.close()

                    await asyncio.to_thread(_close)
            except Exception as e:
                logger.warning(f"[SshSandbox] Error closing connection: {e}")
            finally:
                self._conn = None
                self._connected = False
        logger.info("[SshSandbox] Disconnected")
