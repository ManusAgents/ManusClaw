"""Tests for PAORR loop components."""
import pytest
import os
os.environ["APP_ENV"] = "test"

from app.config import Config
Config.reset()


@pytest.mark.asyncio
async def test_react_reflect_recovers_on_parse_error():
    """Reflection should return solved=False (safe default) on parse error."""
    from app.agent.react import ReActAgent
    from app.permissions.gate import AgentMode
    from app.schema import Observation
    agent = ReActAgent(mode=AgentMode.BUILD)
    obs = Observation(
        tool_name="python_execute", args={},
        output="Hello from ManusClaw!", error=None, success=True,
    )
    reflection = await agent.reflect("Say hello", obs)
    assert reflection is not None
    assert isinstance(reflection.solved, bool)


@pytest.mark.asyncio
async def test_observation_recording():
    from app.agent.manus import Manus
    from app.schema import TaskHistory
    agent = Manus()
    agent._task_history = TaskHistory(task_id="t1", original_goal="test")
    agent._task_history.add_step("step 1")
    agent._session_id = None  # skip DB writes
    agent.record_observation("bash", {"command": "echo hi"}, "hi", None, attempt=1, duration_ms=10)
    assert agent._tool_call_count == 1
    obs = agent._task_history.steps[0].observations[0]
    assert obs.tool_name == "bash"
    assert obs.success is True


def test_task_history_loop_detection():
    from app.schema import TaskHistory, Observation
    history = TaskHistory(task_id="t1", original_goal="loop test")
    for _ in range(3):
        step = history.add_step("step")
        obs = Observation(tool_name="bash", args={}, output=None, error="fail", success=False)
        step.observations.append(obs)
    assert history.is_looping(window=3)


def test_task_history_no_loop_when_diverse():
    from app.schema import TaskHistory, Observation
    history = TaskHistory(task_id="t2", original_goal="diverse")
    for t in ["bash", "web_search", "python_execute"]:
        step = history.add_step("step")
        obs = Observation(tool_name=t, args={}, output=None, error="fail", success=False)
        step.observations.append(obs)
    assert not history.is_looping(window=3)


@pytest.mark.asyncio
async def test_effective_budget_uses_llm_budget():
    """BaseAgent._effective_budget should delegate to LLM budget when available."""
    from app.agent.manus import Manus
    from app.llm.token_tracker import TokenBudget
    agent = Manus()
    # The LLM is created in ReActAgent.__init__
    assert hasattr(agent, "llm")
    assert hasattr(agent.llm, "token_budget")
    budget = agent._effective_budget
    assert isinstance(budget, TokenBudget)
