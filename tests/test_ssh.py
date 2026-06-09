"""Tests for SSH server and restricted shell."""

import os
import pytest

# ── Ensure stub mode ────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_ssh_env(monkeypatch):
    monkeypatch.delenv("MANUSCLAW_SSH_ENABLED", raising=False)
    monkeypatch.delenv("MANUSCLAW_SSH_PORT", raising=False)


# ── SSHServer stub mode ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ssh_server_stub_mode():
    from app.ssh_server import SSHServer
    server = SSHServer(enabled=True)
    assert not server.running
    await server.start()  # Should return immediately in stub mode (no asyncssh)
    # If asyncssh IS installed but enabled=true, it tries to bind.
    # In either case, it should not raise.
    await server.stop()


@pytest.mark.asyncio
async def test_ssh_server_status():
    from app.ssh_server import SSHServer
    server = SSHServer(enabled=False)
    status = await server.status()
    assert status["enabled"] is False
    assert status["running"] is False
    assert "stub_mode" in status


# ── RestrictedShell command validation ────────────────────────────────────

@pytest.mark.asyncio
async def test_shell_allowed_command_status():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("status")
    assert result.success
    assert "ManusClaw Server" in result.stdout


@pytest.mark.asyncio
async def test_shell_allowed_command_help():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("help")
    assert result.success
    assert "Available commands" in result.stdout


@pytest.mark.asyncio
async def test_shell_allowed_command_clear():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("clear")
    assert result.success


@pytest.mark.asyncio
async def test_shell_empty_input():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("")
    assert result.exit_code == 0
    assert result.stdout == ""


# ── Dangerous command rejection ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shell_reject_pipe():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("status | grep Running")
    assert not result.success
    assert "disallowed" in result.stderr.lower() or "rejected" in result.stderr.lower()


@pytest.mark.asyncio
async def test_shell_reject_semicolon():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("status; rm -rf /")
    assert not result.success


@pytest.mark.asyncio
async def test_shell_reject_backticks():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("echo `cat /etc/passwd`")
    assert not result.success


@pytest.mark.asyncio
async def test_shell_reject_dollar_sign():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("echo $(whoami)")
    assert not result.success


@pytest.mark.asyncio
async def test_shell_reject_and_redirect():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("status > /tmp/out")
    assert not result.success


@pytest.mark.asyncio
async def test_shell_reject_unknown_command():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    result = await shell.execute("hack_the_gibson")
    assert not result.success
    assert "Unknown command" in result.stderr


@pytest.mark.asyncio
async def test_shell_reject_bash_exploits():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    for cmd in ("bash -i", "sh -c", "python3 -c 'import os'", "exec('rm -rf /')"):
        result = await shell.execute(cmd)
        assert not result.success, f"Command should be rejected: {cmd}"


# ── Shell properties ─────────────────────────────────────────────────────

def test_shell_motd():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    assert "ManusClaw" in shell.motd


def test_shell_prompt():
    from app.ssh.shell import RestrictedShell
    shell = RestrictedShell()
    assert shell.prompt == "manusclaw> "
