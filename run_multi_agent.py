#!/usr/bin/env python3
"""
ManusClaw Multi-Agent Pipeline CLI

Usage:
    python run_multi_agent.py "Build a REST API for a todo list"
    python run_multi_agent.py --mode plan "Design a web scraper"
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main() -> None:
    parser = argparse.ArgumentParser(description="ManusClaw Multi-Agent Pipeline")
    parser.add_argument("goal", nargs="?", default=None, help="Task goal")
    parser.add_argument("--mode", choices=["build", "plan"], default="build")
    args = parser.parse_args()

    goal = args.goal
    if not goal:
        goal = input("Enter your goal: ").strip()
    if not goal:
        print("No goal provided.")
        sys.exit(1)

    from app.agent.orchestrator import MultiAgentOrchestrator
    from app.permissions.gate import AgentMode

    mode = AgentMode.PLAN if args.mode == "plan" else AgentMode.BUILD
    orch = MultiAgentOrchestrator(mode=mode)

    print(f"\n[ManusClaw] Running multi-agent pipeline...")
    print(f"[ManusClaw] Goal: {goal}")
    print(f"[ManusClaw] Mode: {args.mode}\n")

    result = await orch.run(goal)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
