from __future__ import annotations

"""
Docker-based sandboxing for code execution.
Optional dependency — falls back gracefully if Docker is unavailable.
"""

import asyncio
from typing import Any, Optional

from app.config import Config
from app.exceptions import SandboxError
from app.logger import logger
from app.schema import ToolResult


class DockerSandbox:
    """Runs code in an isolated Docker container."""

    def __init__(self) -> None:
        cfg = Config.get().sandbox
        self.image = cfg.docker_image
        self.memory = cfg.memory_limit
        self.timeout = cfg.timeout
        self._container_id: Optional[str] = None

    async def start(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "-d", "--rm",
                f"--memory={self.memory}",
                "--network=none",
                self.image, "sleep", str(self.timeout * 10),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise SandboxError(f"Docker start failed: {stderr.decode()}")
            self._container_id = stdout.decode().strip()
            logger.info(f"[Sandbox] Started container {self._container_id[:12]}")
        except FileNotFoundError:
            raise SandboxError("Docker not found. Install Docker to use sandbox mode.")

    async def exec(self, code: str) -> ToolResult:
        if not self._container_id:
            await self.start()
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id,
            "python3", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await self.stop()
            return ToolResult(error=f"Execution timed out after {self.timeout}s")
        # Fix: only treat stderr as error when process failed (non-zero return code)
        # Many programs write diagnostics to stderr even on success
        error = stderr.decode().strip() or None if proc.returncode != 0 else None
        return ToolResult(
            output=stdout.decode().strip() or None,
            error=error,
        )

    async def stop(self) -> None:
        if self._container_id:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", self._container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            self._container_id = None


class DaytonaSandbox:
    """Stub for Daytona cloud sandbox integration."""

    async def start(self) -> None:
        raise NotImplementedError(
            "Daytona integration is not yet implemented. "
            "Configure DAYTONA_API_KEY and daytona SDK to enable."
        )

    async def exec(self, code: str) -> ToolResult:
        raise NotImplementedError("Daytona not configured.")

    async def stop(self) -> None:
        pass
