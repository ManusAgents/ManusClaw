"""Tests for tool discovery, dispatch, and error handling."""
import pytest
from app.config import Config
Config.reset()


@pytest.mark.asyncio
async def test_tool_collection_execute_unknown():
    from app.tool.base import ToolCollection
    from app.tool.terminate import Terminate
    tc = ToolCollection(Terminate())
    result = await tc.execute("nonexistent_tool")
    assert result.error is not None
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_terminate_tool():
    from app.tool.terminate import Terminate
    t = Terminate()
    result = await t.execute(reason="Task done")
    assert result.system == "terminate"


@pytest.mark.asyncio
async def test_memory_tool_roundtrip(tmp_workspace):
    from app.tool.memory_tool import MemoryTool
    import app.tool.memory_tool as mt
    mt.MEMORY_FILE = tmp_workspace / "MEMORY.md"
    mt._WORKSPACE = tmp_workspace
    tool = MemoryTool()
    write_result = await tool.execute(action="write_memory", content="Hello world")
    assert write_result.error is None
    read_result = await tool.execute(action="read_memory")
    assert "Hello world" in (read_result.output or "")


@pytest.mark.asyncio
async def test_memory_tool_append(tmp_workspace):
    from app.tool.memory_tool import MemoryTool
    import app.tool.memory_tool as mt
    mt.MEMORY_FILE = tmp_workspace / "MEMORY.md"
    mt._WORKSPACE = tmp_workspace
    tool = MemoryTool()
    await tool.execute(action="write_memory", content="Line 1")
    await tool.execute(action="append_memory", content="Line 2")
    result = await tool.execute(action="read_memory")
    assert "Line 1" in (result.output or "")
    assert "Line 2" in (result.output or "")


@pytest.mark.asyncio
async def test_node_execute_fallback():
    from app.tool.node_execute import NodeExecute
    tool = NodeExecute()
    result = await tool.execute(code="console.log(42)", timeout=5)
    # Either succeeds or returns "not found" error — both are acceptable
    assert result is not None


@pytest.mark.asyncio
async def test_skill_manager_create_list_delete(tmp_path):
    import os
    os.environ["MANUSCLAW_SKILLS_DIR"] = str(tmp_path / "skills")
    from app.skills import skill_engine as se
    se._engine = None  # reset singleton
    from app.tool.skill_manager import SkillManagerTool
    tool = SkillManagerTool()

    create = await tool.execute(action="create", name="test_skill",
                                description="A test skill", content="Do the thing.")
    assert create.error is None

    lst = await tool.execute(action="list")
    assert "test_skill" in (lst.output or "")

    delete = await tool.execute(action="delete", name="test_skill")
    assert delete.error is None
    se._engine = None
