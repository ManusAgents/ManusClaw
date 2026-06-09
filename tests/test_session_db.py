"""Tests for SessionDB — FTS5, branching, compression, jittered retries."""
import pytest
import asyncio


@pytest.mark.asyncio
async def test_create_and_close_session(tmp_db):
    sid = await tmp_db.create_session("test goal", "manus", "build")
    assert len(sid) == 12
    await tmp_db.close_session(sid, state="finished", step_count=3)
    sessions = await tmp_db.get_sessions(limit=5)
    assert any(s["id"] == sid for s in sessions)


@pytest.mark.asyncio
async def test_branch_session(tmp_db):
    parent = await tmp_db.create_session("parent goal")
    child = await tmp_db.branch_session(parent, new_goal="child goal")
    assert child != parent
    sessions = await tmp_db.get_sessions(limit=10)
    child_sess = next((s for s in sessions if s["id"] == child), None)
    assert child_sess is not None
    assert child_sess["parent_session_id"] == parent


@pytest.mark.asyncio
async def test_fts_search_messages(tmp_db):
    sid = await tmp_db.create_session("FTS test")
    await tmp_db.log_message(sid, "user", "The quick brown fox jumps over the lazy dog")
    await tmp_db.log_message(sid, "assistant", "A very unique xylophone response")
    results = await tmp_db.fts_search("xylophone", limit=5, search_in="messages")
    assert len(results) >= 1
    assert any("xylophone" in r.get("content", "").lower() for r in results)


@pytest.mark.asyncio
async def test_fts_search_no_results(tmp_db):
    results = await tmp_db.fts_search("zzzzzzquux12345", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_compress_session(tmp_db):
    sid = await tmp_db.create_session("compress test")
    await tmp_db.log_message(sid, "user", "First message")
    await tmp_db.log_message(sid, "assistant", "Second message")
    await tmp_db.compress_session(sid, "Summary: did stuff")
    msgs = await tmp_db.get_session_messages(sid)
    contents = [m["content"] for m in msgs]
    assert any("Summary" in c for c in contents)
    assert not any(c == "First message" for c in contents)


@pytest.mark.asyncio
async def test_tool_call_logging(tmp_db):
    sid = await tmp_db.create_session("tool log test")
    await tmp_db.log_tool_call(
        sid, step=1, tool_name="bash",
        args={"command": "echo hi"}, output="hi", error=None,
        attempt=1, duration_ms=50,
    )
    calls = await tmp_db.get_session_tool_calls(sid)
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "bash"
    assert calls[0]["success"] == 1
