from __future__ import annotations

"""
ManusClaw multi-agent CLI entry point.
Installed via pyproject.toml [project.scripts].
"""

import argparse
import asyncio
import sys


async def _run(goal: str, mode: str) -> None:
    from app.agent.orchestrator import MultiAgentOrchestrator
    from app.permissions.gate import AgentMode
    from app.logger import logger

    agent_mode = AgentMode.PLAN if mode.lower() == "plan" else AgentMode.BUILD
    orchestrator = MultiAgentOrchestrator(mode=agent_mode)
    logger.info(f"Multi-agent pipeline starting. Goal: {goal[:80]}")
    result = await orchestrator.run(goal)
    print("\n" + "=" * 60)
    print("PIPELINE OUTPUT:")
    print("=" * 60)
    print(result)
    print("=" * 60)


def run_cli() -> None:
    parser = argparse.ArgumentParser(description="ManusClaw Multi-Agent Pipeline")
    parser.add_argument("goal", nargs="?", default=None, help="Task goal")
    parser.add_argument("--mode", default="build", choices=["build", "plan"],
                        help="Agent mode: build (auto) or plan (approval-gated)")
    args = parser.parse_args()

    if args.goal:
        goal = args.goal
    else:
        try:
            goal = input("Enter goal: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

    if not goal:
        print("No goal provided.")
        sys.exit(1)

    asyncio.run(_run(goal, args.mode))
