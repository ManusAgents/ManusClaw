#!/usr/bin/env python3
"""
ManusClaw — PlanningFlow entry point.
Decomposes the goal into steps and runs each with an appropriate agent.

Usage:
    python run_flow.py "Analyse this CSV and generate a bar chart"
"""
import asyncio
import sys

from app.config import Config
from app.flow.planning import PlanningFlow
from app.logger import logger


async def main(goal: str) -> None:
    cfg = Config.get()
    flow = PlanningFlow(timeout=cfg.runflow.timeout)
    logger.info(f"[PlanningFlow] Goal: {goal}")
    result = await flow.run(goal)
    print("\n" + "=" * 60)
    print("FLOW RESULT:")
    print("=" * 60)
    print(result)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        goal = " ".join(sys.argv[1:])
    else:
        goal = input("Enter goal: ").strip()
        if not goal:
            goal = "Write a hello world Python script and save it to workspace/hello.py"
    asyncio.run(main(goal))
