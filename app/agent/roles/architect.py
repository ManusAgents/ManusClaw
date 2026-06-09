from __future__ import annotations

"""
ArchitectRole — translates a PRD into a concrete system design + task DAG.

Decision logic:
  validate_input  → input must look like a PRD (contains OBJECTIVE)
  decide          → design must contain IMPLEMENTATION PLAN and at least one [TASK-
  RETRY           → missing sections are named in the correction prompt
"""

from app.agent.roles.base_role import BaseRole, RoleMessage, RoleMessageBus
from app.logger import logger
from app.schema import RoleDecision


_REQUIRED_DESIGN_SECTIONS = [
    "SYSTEM OVERVIEW",
    "FILE STRUCTURE",
    "COMPONENT MAP",
    "DATA FLOW",
    "TECHNOLOGY STACK",
    "IMPLEMENTATION PLAN",
]

_TASK_MARKER = "[TASK-"


class ArchitectRole(BaseRole):
    role_name        = "architect"
    role_description = "Produces system design and implementation task DAG from PRD"
    max_retries      = 2

    specialist_prompt = """\
You are the Architect agent of ManusClaw. You receive a PRD and produce a
concrete system design document with a dependency-aware task list.

Your design MUST include all six of these sections (exact headers required):

  1. SYSTEM OVERVIEW — high-level architecture (1 paragraph)
  2. FILE STRUCTURE — exact directory tree with a purpose comment per file
  3. COMPONENT MAP — each component: responsibility, public interface
  4. DATA FLOW — numbered steps showing how data moves between components
  5. TECHNOLOGY STACK — exact library names and versions
  6. IMPLEMENTATION PLAN — ordered task DAG for the Engineer
     Format each task exactly as:
       [TASK-N] <action verb + description> | File: <path> | Deps: [TASK-X, …]

The implementation plan is a directed acyclic graph — declare dependencies
explicitly so the Engineer can execute tasks in the correct order.
"""

    # ──────────────────────────────────────────────────────────────────────────
    # Input validation
    # ──────────────────────────────────────────────────────────────────────────

    def validate_input(self, context: str) -> tuple[bool, str]:
        if len(context.strip()) < 50:
            return False, "Input is too short to be a valid PRD."
        if "objective" not in context.lower() and "goal" not in context.lower():
            return (
                False,
                "Input does not appear to be a PRD — 'OBJECTIVE' or 'GOAL' not found. "
                "Ensure the ProductManager ran first.",
            )
        return True, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Output decision
    # ──────────────────────────────────────────────────────────────────────────

    def decide(self, output: str) -> tuple[RoleDecision, str]:
        missing = self._missing_sections(output, _REQUIRED_DESIGN_SECTIONS)
        if missing:
            return RoleDecision.RETRY, f"Missing design sections: {', '.join(missing)}"
        if _TASK_MARKER not in output:
            return (
                RoleDecision.RETRY,
                "IMPLEMENTATION PLAN must contain at least one task in [TASK-N] format.",
            )
        return RoleDecision.PROCEED, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Think → Act → Publish
    # ──────────────────────────────────────────────────────────────────────────

    async def _think_act_publish(self, context: str) -> str:
        # Pull PRD from bus if available; fall back to context
        msgs = await self.bus.drain(self.role_name)
        prd = next((m.artefact for m in msgs if m.artefact), context)

        logger.info(f"[{self.role_name}] Generating system design from PRD ({len(str(prd))} chars).")

        design = await self._ask_llm(
            f"PRD:\n{prd}\n\nWrite a complete system design document now. "
            f"Include ALL six sections: {', '.join(_REQUIRED_DESIGN_SECTIONS)}. "
            f"Format every task in the IMPLEMENTATION PLAN as [TASK-N] …"
        )

        # Retry loop — correct missing sections or missing tasks
        for attempt in range(1, self.max_retries + 1):
            decision, reason = self.decide(design)
            if decision == RoleDecision.PROCEED:
                break

            logger.warning(f"[{self.role_name}] Attempt {attempt}: {reason}. Retrying.")
            design = await self._ask_llm(
                f"Your design is incomplete. {reason}. "
                f"Please regenerate the FULL design document with ALL required sections "
                f"and a properly formatted IMPLEMENTATION PLAN."
            )
        else:
            decision, reason = self.decide(design)
            if decision != RoleDecision.PROCEED:
                logger.error(f"[{self.role_name}] Could not produce complete design: {reason}")
                # Fix: do NOT publish incomplete design downstream — return error instead
                await self.bus.publish(RoleMessage(
                    from_role=self.role_name,
                    to_role="engineer",
                    content=f"Architect failed to produce complete design: {reason}",
                    artefact=None,
                ))
                return f"ARCHITECT ERROR: {reason}"

        logger.info(f"[{self.role_name}] Design complete ({len(design)} chars). Publishing to Engineer.")
        await self.bus.publish(RoleMessage(
            from_role=self.role_name,
            to_role="engineer",
            content="Design complete. Please implement each [TASK-N] in the plan.",
            artefact=design,
        ))
        return design
