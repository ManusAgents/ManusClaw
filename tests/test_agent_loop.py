"""Tests for the PAORR agent loop in BaseAgent."""
import pytest
import os
os.environ["APP_ENV"] = "test"

from app.config import Config
Config.reset()


@pytest.mark.asyncio
async def test_agent_runs_to_completion():
    """Agent should complete a simple task using MockLLM."""
    from app.agent.manus import Manus
    from app.schema import AgentState
    agent = Manus()
    agent._max_steps = 5
    result = await agent.run("Say hello")
    assert isinstance(result, str)
    assert len(result) > 0
    assert agent.state in (AgentState.FINISHED, AgentState.ERROR)


@pytest.mark.asyncio
async def test_agent_respects_max_steps():
    """Agent should stop at max_steps."""
    from app.agent.manus import Manus
    Config.reset()
    agent = Manus()
    agent._max_steps = 2
    await agent.run("Do an infinite task")
    assert agent._step_count <= 2


@pytest.mark.asyncio
async def test_agent_state_is_idle_before_run():
    from app.agent.manus import Manus
    from app.schema import AgentState
    agent = Manus()
    assert agent.state == AgentState.IDLE


@pytest.mark.asyncio
async def test_token_budget_grace_call():
    """Token budget should allow one grace call after exhaustion."""
    from app.llm.token_tracker import TokenBudget
    budget = TokenBudget(max_tokens=10)
    # Simulate recording usage that exceeds budget
    budget.record({"usage": {"prompt_tokens": 6, "completion_tokens": 5}})
    assert budget.is_exhausted
    assert not budget.grace_used
    assert budget.use_grace() is True
    assert budget.grace_used
    assert budget.use_grace() is False  # second call returns False


@pytest.mark.asyncio
async def test_duplicate_detection():
    """Agent should detect duplicate responses."""
    from app.agent.manus import Manus
    from app.schema import Message, Role
    agent = Manus()
    for _ in range(4):
        agent.memory.add(Message(role=Role.ASSISTANT, content="same response"))
    assert agent._is_stuck_by_duplicates()
