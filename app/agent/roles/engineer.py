from __future__ import annotations

"""
EngineerRole — implements code from the Architect's task DAG using Manus.

Decision logic:
  validate_input  → input must contain [TASK- or "IMPLEMENTATION PLAN"
  decide          → checks that implementation output is substantive (> 200 chars)
                    and mentions key task-completion signals
"""

from app.agent.roles.base_role import BaseRole, RoleMessage, RoleMessageBus
from app.logger import logger
from app.schema import RoleDecision

_TASK_MARKER          = "[TASK-"
_COMPLETION_SIGNALS   = ["workspace/", "saved", "written", "created", "complete", "done"]
_MIN_OUTPUT_LEN       = 200


class EngineerRole(BaseRole):
    role_name        = "engineer"
    role_description = "Implements code from the Architect's plan using Manus tools"
    max_retries      = 1    # Manus is expensive — only one retry attempt

    specialist_prompt = """\
You are the Engineer agent of ManusClaw. You receive an implementation plan
from the Architect and execute each task using available tools.

Your process for EVERY task:
  1. Read the task description carefully
  2. Identify the correct tool (python_execute, bash, str_replace_editor)
  3. Write or run the code
  4. Verify the output matches the acceptance criterion
  5. If it fails: debug, fix, re-run (up to 3 attempts per task)
  6. Mark the task complete only when verified

Rules:
  - Always run code — never just write it and assume it works
  - Save all outputs and generated files to workspace/
  - Use str_replace_editor to write files, bash/python_execute to run them
  - After implementing all tasks, call terminate with a completion summary
"""

    # ──────────────────────────────────────────────────────────────────────────
    # Input validation
    # ──────────────────────────────────────────────────────────────────────────

    def validate_input(self, context: str) -> tuple[bool, str]:
        upper = context.upper()
        if _TASK_MARKER.upper() not in upper and "IMPLEMENTATION PLAN" not in upper:
            return (
                False,
                f"Input does not appear to be a design document — "
                f"'{_TASK_MARKER}' or 'IMPLEMENTATION PLAN' not found. "
                f"Ensure the Architect ran first.",
            )
        return True, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Output decision
    # ──────────────────────────────────────────────────────────────────────────

    def decide(self, output: str) -> tuple[RoleDecision, str]:
        if len(output) < _MIN_OUTPUT_LEN:
            return RoleDecision.RETRY, "Implementation output is too short — tasks may not have run."
        lower = output.lower()
        hits = sum(1 for sig in _COMPLETION_SIGNALS if sig in lower)
        if hits == 0:
            return (
                RoleDecision.RETRY,
                "No task-completion signals found in output. "
                "Ensure all tasks were run and files were saved.",
            )
        return RoleDecision.PROCEED, ""

    # ──────────────────────────────────────────────────────────────────────────
    # Think → Act → Publish
    # ──────────────────────────────────────────────────────────────────────────

    async def _think_act_publish(self, context: str) -> str:
        from app.agent.manus import Manus

        # Pull design from bus if available; fall back to context
        msgs = await self.bus.drain(self.role_name)
        design = next((m.artefact for m in msgs if m.artefact), context)

        logger.info(
            f"[{self.role_name}] Delegating implementation to Manus "
            f"({design[:80].strip()!r}…)."
        )

        engineer_agent = Manus()
        implementation_result = await engineer_agent.run(
            f"You are implementing code based on this design plan.\n\n"
            f"DESIGN PLAN:\n{design}\n\n"
            f"Implement EVERY [TASK-N] item in the plan. Run and verify each one. "
            f"Save all outputs and generated files to workspace/. "
            f"When all tasks are done, call terminate with a completion summary "
            f"listing each task and its status."
        )

        # Decision check — retry once with a corrective prompt if output looks thin
        decision, reason = self.decide(implementation_result)
        if decision == RoleDecision.RETRY:
            logger.warning(f"[{self.role_name}] First pass thin: {reason}. Retrying.")
            engineer_agent2 = Manus()
            implementation_result = await engineer_agent2.run(
                f"The previous implementation attempt was incomplete. {reason}.\n\n"
                f"Please re-implement all remaining tasks from this plan:\n\n{design}\n\n"
                f"Verify each task is complete before calling terminate."
            )

        logger.info(f"[{self.role_name}] Implementation done ({len(implementation_result)} chars). Publishing to QA.")
        await self.bus.publish(RoleMessage(
            from_role=self.role_name,
            to_role="qa",
            content="Implementation complete. Please run QA validation.",
            artefact=implementation_result,
        ))
        return implementation_result
