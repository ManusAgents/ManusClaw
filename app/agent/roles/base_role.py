from __future__ import annotations

"""
BaseRole — foundation for all multi-agent specialist roles.

Every role follows the Observe → Think → Act → Publish loop:
  Observe  — read incoming messages from the async message bus
  Think    — reason about the goal using the specialist prompt
  Act      — call the LLM (and optionally tools via a sub-agent)
  Publish  — push output to the bus for the next role

Decision framework:
  After producing output, every role calls decide() to classify its output:

    PROCEED  — output is complete; publish and continue the pipeline
    RETRY    — output is incomplete; retry with a correction prompt
    ESCALATE — input is fundamentally unclear; pipeline should log & continue
    BLOCKED  — role cannot proceed even after retries; mark and continue

Subclasses override:
  validate_input(context)  → (is_valid: bool, reason: str)
  decide(output)           → (RoleDecision, reason: str)
  _think_act_publish(ctx)  → str   [the main work method]
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.logger import logger
from app.schema import RoleDecision


# ──────────────────────────────────────────────────────────────────────────────
# Message bus
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RoleMessage:
    from_role: str
    to_role:   str            # "*" = broadcast to all
    content:   str
    artefact:  Optional[Any] = None   # structured output (PRD, design, code, …)
    ts:        datetime       = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return f"[{self.from_role} → {self.to_role}] {self.content[:120]}"


class RoleMessageBus:
    """
    Simple in-process async message bus.
    Roles subscribe by name; messages are delivered to named queues.
    Broadcast to "*" delivers to all queues except the sender.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def subscribe(self, role_name: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[role_name] = q
        return q

    async def publish(self, message: RoleMessage) -> None:
        if message.to_role == "*":
            for name, q in self._queues.items():
                if name != message.from_role:
                    await q.put(message)
        elif message.to_role in self._queues:
            await self._queues[message.to_role].put(message)

    async def drain(self, role_name: str) -> list[RoleMessage]:
        """Collect all pending messages without blocking."""
        q = self._queues.get(role_name)
        if not q:
            return []
        msgs: list[RoleMessage] = []
        while True:
            try:
                msgs.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return msgs


# ──────────────────────────────────────────────────────────────────────────────
# BaseRole
# ──────────────────────────────────────────────────────────────────────────────

class BaseRole(ABC):
    """
    Abstract base for all specialist roles.

    Class attributes to set in subclasses
    ─────────────────────────────────────
    role_name         str   unique role identifier used on the bus
    role_description  str   one-line description shown in logs
    specialist_prompt str   the expert persona / instructions for the LLM
    max_retries       int   max correction attempts before BLOCKED (default 2)
    """

    role_name:         str = "base"
    role_description:  str = "Generic role"
    specialist_prompt: str = ""
    max_retries:       int = 2

    def __init__(self, bus: RoleMessageBus) -> None:
        self.bus    = bus
        self._inbox = bus.subscribe(self.role_name)
        self._done  = False

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    async def run(self, initial_input: str) -> str:
        """
        Validate input, then run the Observe→Think→Act→Publish loop.
        Handles retries and escalation centrally.
        """
        from app.llm.llm import LLM
        from app.memory.short_term import ShortTermMemory
        from app.schema import Message

        self.llm    = LLM()
        self.memory = ShortTermMemory()
        self.memory.add(Message.system(self._build_system_prompt()))
        self.memory.add(Message.user(initial_input))

        t0 = time.monotonic()

        # 1 — Input validation
        valid, reason = self.validate_input(initial_input)
        if not valid:
            logger.warning(f"[{self.role_name}] Input validation failed: {reason}")
            return (
                f"[{self.role_name}] Cannot proceed — input validation failed.\n"
                f"Reason: {reason}\n"
                f"Input snippet: {initial_input[:200]}"
            )

        # 2 — Main work loop (with retries)
        result = await self._think_act_publish(initial_input)

        elapsed = time.monotonic() - t0
        logger.info(f"[{self.role_name}] ■ Done in {elapsed:.1f}s")
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Abstract — subclasses implement these
    # ──────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def _think_act_publish(self, context: str) -> str:
        """
        Role-specific Think → Act → Publish.

        Must:
          • Call self._ask_llm() one or more times to produce output.
          • Call self.decide(output) to assess completeness.
          • Retry up to self.max_retries times if decision is RETRY.
          • Call self.bus.publish() with the final output.
          • Return the primary output string.
        """

    # ──────────────────────────────────────────────────────────────────────────
    # Decision framework — override in subclasses for intelligence
    # ──────────────────────────────────────────────────────────────────────────

    def validate_input(self, context: str) -> tuple[bool, str]:
        """
        Validate the incoming context before starting work.
        Return (True, "") if valid, or (False, reason) if not.
        Override in subclasses for role-specific checks.
        """
        if not context or not context.strip():
            return False, "Input is empty."
        return True, ""

    def decide(self, output: str) -> tuple[RoleDecision, str]:
        """
        Classify the output produced by _think_act_publish.
        Returns (RoleDecision, reason_string).

        Override in subclasses to add intelligence:
          PROCEED  — output is complete, publish to next role
          RETRY    — output is incomplete, reason describes what's missing
          ESCALATE — input was fundamentally unclear
          BLOCKED  — unable to proceed even after retries
        """
        return RoleDecision.PROCEED, ""

    def on_escalate(self, reason: str) -> str:
        """Called when the role escalates. Override for custom behaviour."""
        logger.warning(f"[{self.role_name}] Escalating: {reason}")
        return f"[{self.role_name}] ESCALATED — {reason}"

    def on_blocked(self, reason: str) -> str:
        """Called when the role is blocked. Override for custom behaviour."""
        logger.error(f"[{self.role_name}] BLOCKED after {self.max_retries} retries: {reason}")
        return f"[{self.role_name}] BLOCKED — {reason}"

    # ──────────────────────────────────────────────────────────────────────────
    # Shared helpers available to all roles
    # ──────────────────────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        identity = (
            "You are ManusClaw, an autonomous AI engine created by The-JDdev (SHS Shobuj). "
            "You do not identify as any base LLM provider. You are ManusClaw.\n\n"
        )
        return identity + self.specialist_prompt

    async def _ask_llm(self, extra_context: str = "") -> str:
        """
        Send the current memory (+ optional extra) to the LLM.
        Appends the response to memory and returns the text content.
        """
        from app.schema import Message
        if extra_context:
            self.memory.add(Message.user(extra_context))
        response = await self.llm.ask(self.memory.messages)
        self.memory.add(response)
        return response.content or ""

    def _missing_sections(self, output: str, required: list[str]) -> list[str]:
        """Return which required section headers are absent from output."""
        upper = output.upper()
        return [s for s in required if s.upper() not in upper]
