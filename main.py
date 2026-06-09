#!/usr/bin/env python3
"""
ManusClaw — main entry point.
Runs the Manus agent on a single prompt to completion.

Usage:
    python main.py "Your task here"
    python main.py  # interactive prompt
"""
import asyncio
import sys

from app.agent.manus import Manus
from app.logger import logger


async def main(prompt: str) -> None:
    agent = Manus()
    logger.info(f"Running Manus with prompt: {prompt[:80]}")
    result = await agent.run(prompt)
    print("\n" + "=" * 60)
    print("FINAL OUTPUT:")
    print("=" * 60)
    print(result)
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        user_prompt = " ".join(sys.argv[1:])
    else:
        try:
            user_prompt = input("Enter your task: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo input provided. Using default task.")
            user_prompt = ""
        if not user_prompt:
            user_prompt = "Print 'Hello from ManusClaw!' using Python."

    asyncio.run(main(user_prompt))
