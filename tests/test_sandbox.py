"""Tests for sandbox backends (factory, SshSandbox, OpenShellSandbox)."""

import os
import pytest
from unittest.mock import patch, MagicMock

# ── Ensure stub mode ────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_sandbox_env(monkeypatch):
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    monkeypatch.delenv("SSH_SANDBOX_HOST", raising=False)
    monkeypatch.delenv("SSH_SANDBOX_USER", raising=False)
    monkeypatch.delenv("SSH_SANDBOX_KEY_PATH", raising=False)


# ── create_sandbox() factory ───────────────────────────────────────────────

def test_create_sandbox_defaults_to_docker():
    """Without SANDBOX_BACKEND env var, should try Docker."""
    from app.sandbox.factory import create_sandbox
    with patch("app.sandbox.factory._create_docker") as mock_docker:
        mock_docker.return_value = MagicMock()
        sandbox = create_sandbox()
        mock_docker.assert_called_once()


def test_create_sandbox_explicit_backend():
    from app.sandbox.factory import create_sandbox
    with patch("app.sandbox.factory._create_openshell") as mock_openshell:
        mock_openshell.return_value = MagicMock()
        sandbox = create_sandbox(backend="openshell")
        mock_openshell.assert_called_once()


def test_create_sandbox_unknown_backend_falls_back():
    from app.sandbox.factory import create_sandbox
    with patch("app.sandbox.factory._create_docker") as mock_docker:
        mock_docker.return_value = MagicMock()
        sandbox = create_sandbox(backend="nonexistent_backend")
        mock_docker.assert_called_once()


# ── SshSandbox stub mode ───────────────────────────────────────────────────

def test_ssh_sandbox_not_configured():
    from app.sandbox.ssh import SshSandbox
    sandbox = SshSandbox()
    assert not sandbox.is_configured
    assert sandbox.host == ""
    assert sandbox.user == ""


@pytest.mark.asyncio
async def test_ssh_sandbox_start_raises_when_not_configured():
    from app.sandbox.ssh import SshSandbox
    from app.exceptions import SandboxError
    sandbox = SshSandbox()
    with pytest.raises(SandboxError, match="not configured"):
        await sandbox.start()


# ── OpenShellSandbox stub mode ────────────────────────────────────────────

def test_openshell_sandbox_not_available_on_non_linux():
    from app.sandbox.openshell import OpenShellSandbox
    import sys
    # If not on linux, is_available should be False
    sandbox = OpenShellSandbox()
    if sys.platform != "linux":
        assert not sandbox.is_available
    else:
        assert sandbox.is_available


@pytest.mark.asyncio
async def test_openshell_sandbox_exec_command_stub():
    """On non-Linux, exec_command should return an error ToolResult."""
    from app.sandbox.openshell import OpenShellSandbox
    import sys
    if sys.platform == "linux":
        pytest.skip("OpenShell available on Linux — needs unshare")
    sandbox = OpenShellSandbox()
    result = await sandbox.exec_command("print('hello')")
    assert result.error is not None
    assert "Linux" in result.error or "unshare" in result.error


@pytest.mark.asyncio
async def test_openshell_sandbox_start_stub():
    from app.sandbox.openshell import OpenShellSandbox
    from app.exceptions import SandboxError
    import sys
    if sys.platform == "linux":
        pytest.skip("OpenShell available on Linux")
    sandbox = OpenShellSandbox()
    with pytest.raises(SandboxError):
        await sandbox.start()


# ── list_available_backends ────────────────────────────────────────────────

def test_list_available_backends():
    from app.sandbox.factory import list_available_backends
    backends = list_available_backends()
    assert isinstance(backends, list)
    # Docker should always be importable (module exists)
    assert "docker" in backends
