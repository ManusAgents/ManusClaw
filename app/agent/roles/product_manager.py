from __future__ import annotations

"""
ProductManagerRole — translates a user goal into a complete PRD.

Decision logic:
  validate_input  → goal must be at least 5 meaningful words
  decide          → PRD must contain all 6 required section headers
  RETRY           → missing sections are named in the correction prompt
  ESCALATE        → goal is too vague after max_retries corrections
"""

from app.agent.roles.base_role import BaseRole, RoleMessage, RoleMessageBus
from app.logger import logger
from app.schema import RoleDecision


_REQUIRED_SECTIONS = [
    "OBJECTIVE",
    "IN SCOPE",
    "OUT OF SCOPE",
    "ACCEPTANCE CRITERIA",
    "TECHNICAL CONSTRAINTS",
    "PRIORITY ORDER",
]

_MIN_GOAL_WORDS = 5


class ProductManagerRole(BaseRole):
    role_name        = "product_manager"
    role_description = "Produces Product Requirements Documents (PRDs)"
    max_retries      = 2

    specialist_prompt = """\
You are the Product Manager agent of ManusClaw. Your sole job is to receive
a user goal and produce a structured Product Requirements Document (PRD).

Your PRD MUST include all six of these sections (exact headers required):

  1. OBJECTIVE — one clear sentence stating what will be built
  2. IN SCOPE — bullet list of features that will be implemented
  3. OUT OF SCOPE — bullet list of what will NOT be built
  4. ACCEPTANCE CRITERIA — numbered, measurable success conditions
  5. TECHNICAL CONSTRAINTS — language, frameworks, environment requirements
  6. PRIORITY ORDER — features ordered P0 (must-have) / P1 / P2

Be concrete. No vague language. The Architect and Engineer will act on this PRD
directly. Missing sections or vague language cause failures downstream.
"""

    # ──────────────────────────────────────────────────────────────────────────
    # Input validation
    # ──────────────────────────────────────────────────────────────────────────

    def validate_input(self, context: str) -> tuple[bool, str]:
        words = context.strip().split()
        if len(words) < _MIN_GOAL_WORDS:
            return (
                False,
                f"Goal is too short ({len(words)} words). "
                f"Please provide a more descriptive task (at least {_MIN_GOAL_WORDS} words).",
            )
        return True, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Output decision
    # ──────────────────────────────────────────────────────────────────────────

    def decide(self, output: str) -> tuple[RoleDecision, str]:
        missing = self._missing_sections(output, _REQUIRED_SECTIONS)
        if missing:
            reason = f"Missing sections: {', '.join(missing)}"
            return RoleDecision.RETRY, reason
        return RoleDecision.PROCEED, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Think → Act → Publish
    # ──────────────────────────────────────────────────────────────────────────

    async def _think_act_publish(self, context: str) -> str:
        logger.info(f"[{self.role_name}] Generating PRD for goal: {context[:80]}")

        # Initial PRD generation
        prd = await self._ask_llm(
            f"User goal:\n{context}\n\nWrite a complete PRD for this goal now. "
            f"Include ALL six sections: {', '.join(_REQUIRED_SECTIONS)}."
        )

        # Retry loop — correct missing sections
        for attempt in range(1, self.max_retries + 1):
            decision, reason = self.decide(prd)
            if decision == RoleDecision.PROCEED:
                break

            logger.warning(f"[{self.role_name}] Attempt {attempt}: {reason}. Retrying.")
            prd = await self._ask_llm(
                f"Your PRD is incomplete. {reason}. "
                f"Please regenerate the FULL PRD with ALL six required sections present. "
                f"Do not omit any section."
            )
        else:
            # Still incomplete after all retries
            decision, reason = self.decide(prd)
            if decision != RoleDecision.PROCEED:
                escalation = self.on_escalate(
                    f"Could not produce a complete PRD after {self.max_retries} attempts. "
                    f"Still missing: {reason}"
                )
                await self.bus.publish(RoleMessage(
                    from_role=self.role_name,
                    to_role="architect",
                    content=f"[ESCALATION] PRD incomplete: {reason}",
                    artefact=prd,
                ))
                return escalation

        logger.info(f"[{self.role_name}] PRD complete ({len(prd)} chars). Publishing to Architect.")
        await self.bus.publish(RoleMessage(
            from_role=self.role_name,
            to_role="architect",
            content="PRD complete. Please design the system architecture.",
            artefact=prd,
        ))
        return prd
