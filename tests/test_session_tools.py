"""Tests for session CLI tools."""

import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.session_tools import SessionToolsCLI


@pytest.fixture
def cli():
    return SessionToolsCLI()


# ── Parser builds correctly ────────────────────────────────────────────────

def test_parser_builds(cli):
    parser = cli._build_parser()
    # Should have subcommands
    args = parser.parse_args(["list"])
    assert args.command == "list"

    args = parser.parse_args(["history", "sess-1"])
    assert args.command == "history"
    assert args.session_id == "sess-1"

    args = parser.parse_args(["send", "sess-1", "--message", "hello"])
    assert args.command == "send"
    assert args.message == "hello"

    args = parser.parse_args(["spawn", "--prompt", "do something"])
    assert args.command == "spawn"
    assert args.prompt == "do something"

    args = parser.parse_args(["delete", "sess-1"])
    assert args.command == "delete"

    args = parser.parse_args(["export", "sess-1", "--output", "out.json"])
    assert args.command == "export"


# ── Session list command ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_list_empty(cli, tmp_db):
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["list"])  # Should not raise


@pytest.mark.asyncio
async def test_cmd_list_with_sessions(cli, tmp_db):
    # Create a session
    await tmp_db.create_session(goal="Test goal", agent_name="manus")
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["list"])  # Should print sessions


# ── Session history command ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_history_empty(cli, tmp_db):
    await tmp_db.create_session(goal="test")
    sessions = await tmp_db.get_sessions(limit=1)
    session_id = sessions[0]["id"]
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["history", session_id])


# ── Session send command ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_send_session_not_found(cli, tmp_db):
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["send", "nonexistent", "--message", "hello"])
        # Should not raise — prints "not found"


# ── Session delete command ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_delete_force(cli, tmp_db):
    await tmp_db.create_session(goal="to-delete")
    sessions = await tmp_db.get_sessions(limit=1)
    session_id = sessions[0]["id"]
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["delete", session_id, "--force"])


# ── Session export command ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_export_not_found(cli, tmp_db):
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["export", "nonexistent"])


@pytest.mark.asyncio
async def test_cmd_export_with_session(cli, tmp_db, tmp_path):
    await tmp_db.create_session(goal="export test")
    sessions = await tmp_db.get_sessions(limit=1)
    session_id = sessions[0]["id"]
    output_file = str(tmp_path / "export.json")
    with patch.object(cli, '_get_db', return_value=tmp_db):
        await cli.run(["export", session_id, "--output", output_file])
    assert os.path.exists(output_file)
