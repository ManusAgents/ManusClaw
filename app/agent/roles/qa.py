from __future__ import annotations

"""
QARole — validates the Engineer's implementation against acceptance criteria.

Decision logic:
  validate_input  → implementation output must be non-trivial (> 100 chars)
  decide          → parses QA report for "APPROVED" vs "REWORK REQUIRED" verdict
  REWORK path     → publishes structured defect list back to Engineer via bus,
                    then escalates so the orchestrator can log the rework request
  APPROVED path   → broadcasts the approval signal to all roles

The QA verdict is determined by:
  1. LLM-generated QA report (via Manus with file/code access)
  2. Simple keyword parse: "REWORK REQUIRED" → BLOCKED escalation
                           "APPROVED"        → PROCEED
"""

from app.agent.roles.base_role import BaseRole, RoleMessage, RoleMessageBus
from app.logger import logger
from app.schema import RoleDecision

_MIN_IMPL_LEN = 100


class QARole(BaseRole):
    role_name        = "qa"
    role_description = "Validates implementation against acceptance criteria"
    max_retries      = 0   # QA does not retry itself; it sends feedback to Engineer

    specialist_prompt = """\
You are the QA agent of ManusClaw. You receive implementation output from the
Engineer and validate it against the acceptance criteria in the PRD.

Your QA process:
  1. Read each acceptance criterion from the PRD / implementation summary
  2. For each criterion: run a test (bash, python_execute, or manual check)
  3. Record: PASS / FAIL / PARTIAL with evidence for each criterion
  4. For any FAIL: describe the exact defect and which file/function is wrong
  5. If ANY P0 (must-have) criterion fails: verdict is REWORK REQUIRED
  6. If all P0 criteria pass: verdict is APPROVED

Your output MUST include:
  - QA REPORT header
  - Numbered results matching PRD acceptance criteria
  - Summary table: PASS count / FAIL count / PARTIAL count
  - Verdict line: exactly "Verdict: APPROVED" or "Verdict: REWORK REQUIRED"
  - If REWORK: bulleted defect list with file paths and descriptions
"""

    # ──────────────────────────────────────────────────────────────────────────
    # Input validation
    # ──────────────────────────────────────────────────────────────────────────

    def validate_input(self, context: str) -> tuple[bool, str]:
        if len(context.strip()) < _MIN_IMPL_LEN:
            return (
                False,
                "Implementation output is too short to validate. "
                "Ensure the Engineer ran and produced real output.",
            )
        return True, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Output decision
    # ──────────────────────────────────────────────────────────────────────────

    def decide(self, output: str) -> tuple[RoleDecision, str]:
        # FIX: Use explicit "Verdict:" line to avoid false positives when "APPROVED"
        # appears in code, comments, or test output (e.g. if status == "APPROVED").
        import re
        verdict_match = re.search(
            r"(?:^|\n)\s*Verdict\s*:\s*(APPROVED|REWORK REQUIRED)",
            output,
            re.IGNORECASE,
        )
        if verdict_match:
            verdict = verdict_match.group(1).upper()
            if "REWORK" in verdict:
                return RoleDecision.ESCALATE, "QA verdict is REWORK REQUIRED — defects found."
            return RoleDecision.PROCEED, ""
        # Fallback: check last 400 chars only (summary section) to cut false positives
        tail = output[-400:].upper()
        if "REWORK REQUIRED" in tail:
            return RoleDecision.ESCALATE, "QA verdict is REWORK REQUIRED — defects found."
        if "APPROVED" in tail:
            return RoleDecision.PROCEED, ""
        # Ambiguous — treat as rework to be safe
        return RoleDecision.ESCALATE, "QA verdict is unclear — defaulting to REWORK REQUIRED."

    # ──────────────────────────────────────────────────────────────────────────
    # Think → Act → Publish
    # ──────────────────────────────────────────────────────────────────────────

    async def _think_act_publish(self, context: str) -> str:
        from app.agent.manus import Manus

        # Pull implementation from bus if available; fall back to context
        msgs = await self.bus.drain(self.role_name)
        impl = next((m.artefact for m in msgs if m.artefact), context)

        logger.info(f"[{self.role_name}] Delegating QA validation to Manus.")

        qa_agent = Manus()
        qa_result = await qa_agent.run(
            f"You are a QA engineer. Validate the following implementation:\n\n"
            f"{impl}\n\n"
            f"For each stated feature or function:\n"
            f"  1. Run a test (bash or python_execute)\n"
            f"  2. Record PASS / FAIL / PARTIAL with evidence\n"
            f"Save the QA report to workspace/qa_report.md.\n"
            f"End your report with one of these exact lines:\n"
            f"  Verdict: APPROVED\n"
            f"  Verdict: REWORK REQUIRED\n"
            f"Then call terminate."
        )

        # Parse verdict
        decision, reason = self.decide(qa_result)

        if decision == RoleDecision.PROCEED:
            logger.info(f"[{self.role_name}] Verdict: APPROVED. Broadcasting.")
            await self.bus.publish(RoleMessage(
                from_role=self.role_name,
                to_role="*",
                content="QA Verdict: APPROVED",
                artefact=qa_result,
            ))
        else:
            # REWORK — extract defect block and publish feedback to Engineer
            defect_block = self._extract_defects(qa_result)
            logger.warning(f"[{self.role_name}] Verdict: REWORK REQUIRED — {reason}")
            logger.warning(f"[{self.role_name}] Defects:\n{defect_block}")

            # Feedback to Engineer so orchestrator can log it
            await self.bus.publish(RoleMessage(
                from_role=self.role_name,
                to_role="engineer",
                content=f"REWORK REQUIRED. Defects:\n{defect_block}",
                artefact=qa_result,
            ))
            # Broadcast verdict to all
            await self.bus.publish(RoleMessage(
                from_role=self.role_name,
                to_role="*",
                content=f"QA Verdict: REWORK REQUIRED\n{defect_block}",
                artefact=qa_result,
            ))

        return qa_result

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_defects(qa_report: str) -> str:
        """
        Extract the defect section from a QA report.
        Looks for lines after 'REWORK' or 'DEFECT' headers, or returns
        the last 800 chars as a fallback.
        """
        lower = qa_report.lower()
        for marker in ("defect", "rework", "fail"):
            idx = lower.rfind(marker)
            if idx != -1:
                return qa_report[idx : idx + 800].strip()
        return qa_report[-800:].strip()
