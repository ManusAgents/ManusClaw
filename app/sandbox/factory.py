from __future__ import annotations

"""
Sandbox Factory — Creates the appropriate sandbox backend based on configuration.

Reads ``sandbox.backend`` from config (``docker``, ``ssh``, ``openshell``).
Falls back to ``DockerSandbox`` if not specified or on error.

Usage::

    from app.sandbox.factory import create_sandbox
    sandbox = create_sandbox()
    await sandbox.start()
    result = await sandbox.exec("print('hello')")
"""

import os
from typing import Optional

from app.logger import logger
from app.schema import ToolResult


def create_sandbox(backend: Optional[str] = None) -> "BaseSandbox":
    """Create and return a sandbox instance based on the configured backend.

    Args:
        backend: Explicit backend name (``docker``, ``ssh``, ``openshell``).
                 If ``None``, reads from config or environment variable
                 ``SANDBOX_BACKEND``. Defaults to ``docker``.

    Returns:
        A sandbox instance with ``start()``, ``exec()``, and ``stop()`` methods.

    Raises:
        RuntimeError: If the specified backend is not available.
    """
    if backend is None:
        # Try config first, then environment
        backend = _get_configured_backend()

    backend = (backend or "docker").lower().strip()

    if backend == "docker":
        return _create_docker()
    elif backend == "ssh":
        return _create_ssh()
    elif backend == "openshell":
        return _create_openshell()
    else:
        logger.warning(
            f"[SandboxFactory] Unknown backend '{backend}', falling back to Docker"
        )
        return _create_docker()


def _get_configured_backend() -> str:
    """Read the configured sandbox backend from config or environment."""
    # Environment variable takes highest priority
    env_backend = os.getenv("SANDBOX_BACKEND", "")
    if env_backend:
        return env_backend

    # Try config system
    try:
        from app.config import Config
        cfg = Config.get()
        # sandbox.backend is not in the default SandboxConfig,
        # but could be in raw config data
        raw = getattr(cfg._data, "__dict__", {})
        sandbox_raw = raw.get("sandbox", {})
        if isinstance(sandbox_raw, dict):
            backend = sandbox_raw.get("backend", "")
            if backend:
                return backend
    except Exception:
        pass

    return "docker"


def _create_docker() -> "BaseSandbox":
    """Create a Docker sandbox instance."""
    try:
        from app.sandbox.docker import DockerSandbox
        return DockerSandbox()
    except Exception as e:
        logger.error(f"[SandboxFactory] Docker sandbox creation failed: {e}")
        raise RuntimeError(f"Cannot create Docker sandbox: {e}")


def _create_ssh() -> "BaseSandbox":
    """Create an SSH sandbox instance."""
    try:
        from app.sandbox.ssh import SshSandbox
        sandbox = SshSandbox()
        if not sandbox.is_configured:
            logger.warning(
                "[SandboxFactory] SSH sandbox requested but not configured. "
                "Set SSH_SANDBOX_HOST, SSH_SANDBOX_USER, and SSH_SANDBOX_KEY_PATH."
            )
            logger.info("[SandboxFactory] Falling back to Docker sandbox")
            return _create_docker()
        return sandbox
    except Exception as e:
        logger.error(f"[SandboxFactory] SSH sandbox creation failed: {e}")
        logger.info("[SandboxFactory] Falling back to Docker sandbox")
        return _create_docker()


def _create_openshell() -> "BaseSandbox":
    """Create an OpenShell (Linux namespace) sandbox instance."""
    try:
        from app.sandbox.openshell import OpenShellSandbox
        sandbox = OpenShellSandbox()
        if not sandbox.is_available:
            logger.warning(
                "[SandboxFactory] OpenShell sandbox requested but not available "
                f"(platform: {os.sys.platform}). Falling back to Docker sandbox."
            )
            return _create_docker()
        return sandbox
    except Exception as e:
        logger.error(f"[SandboxFactory] OpenShell sandbox creation failed: {e}")
        logger.info("[SandboxFactory] Falling back to Docker sandbox")
        return _create_docker()


def list_available_backends() -> list[str]:
    """Return a list of available sandbox backend names."""
    available = []

    # Docker
    try:
        from app.sandbox.docker import DockerSandbox
        available.append("docker")
    except ImportError:
        pass

    # SSH
    try:
        from app.sandbox.ssh import SshSandbox
        if SshSandbox().is_configured:
            available.append("ssh")
    except ImportError:
        pass

    # OpenShell
    try:
        from app.sandbox.openshell import OpenShellSandbox
        if OpenShellSandbox().is_available:
            available.append("openshell")
    except ImportError:
        pass

    return available


# Type alias for the base sandbox interface
# (Not a real base class — all sandboxes implement the same duck-typed interface)
BaseSandbox = object
