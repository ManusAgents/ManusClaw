from __future__ import annotations

"""
PlanningFlow — multi-agent orchestration with PAORR-aware step dispatch.

Architecture
────────────
PlanningFlow sits ABOVE individual agents (Manus, DataAnalysisAgent) and BELOW
the MultiAgentOrchestrator. Its responsibilities are:

  1. Decompose a high-level goal into a numbered list of concrete steps
     (LLM-generated, each step carries a success criterion in parentheses)
  2. Dispatch each step to the appropriate agent
  3. Score the result against the declared success criterion (0.0–1.0)
  4. Retry blocked steps once with a different agent
  5. Replan remaining steps when a step is permanently blocked
  6. Enforce a global wall-clock timeout

Feedback loop (new in v3):
  After every step, _score_result(result, criterion) checks how many keywords
  from the criterion appear in the result. A score < 0.35 triggers an
  immediate replan of remaining steps — not just a retry.
"""

import asyncio
import re
import time
import uuid
from typing import Optional

from app.config import Config
from app.logger import logger
from app.schema import (
    FlowResult,
    FlowStepResult,
    Message,
    Plan,
    PlanStep,
    StepStatus,
)


_PLAN_SYSTEM = """\
You are a task decomposition expert. Break down the goal into a numbered list
of concrete, independently executable steps. For each step, also provide a
one-line success criterion in parentheses immediately after the action.

Format (follow exactly — no preamble, no explanation):
1. <action> (<success criterion>)
2. <action> (<success criterion>)
...
"""

_REPLAN_SYSTEM = """\
You are a task re-planner. The original plan had a blocked or low-scoring step.
Review what has been completed so far and output a revised numbered list of
REMAINING steps. Use the same numbered format with success criteria in parens.
Skip already-completed steps. Do not re-number from 1 — continue the existing
step count.
"""


class PlanningFlow:
    """
    Decomposes a goal into steps, dispatches each to an agent, scores results,
    and handles failures with re-planning.

    Parameters
    ----------
    timeout : int
        Global wall-clock timeout in seconds (default 3600).
    score_replan_threshold : float
        If a completed step scores below this threshold the remaining steps are
        replanned immediately (default 0.35).
    """

    def __init__(
        self,
        timeout: int   = 3600,
        score_replan_threshold: float = 0.35,
    ) -> None:
        self._timeout   = timeout
        self._threshold = score_replan_threshold

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    async def run(self, goal: str) -> str:
        """Run the full plan → dispatch → score → replan loop. Returns a summary."""
        flow_result = await self._run_flow(goal)
        return self._format_summary(flow_result)

    async def run_full(self, goal: str) -> FlowResult:
        """Like run() but returns a typed FlowResult instead of a string."""
        return await self._run_flow(goal)

    # ──────────────────────────────────────────────────────────────────────────
    # Core loop
    # ──────────────────────────────────────────────────────────────────────────

    async def _run_flow_body(self, plan: Plan, flow: FlowResult, flow_id: str) -> None:
        step_idx = 0
        while step_idx < len(plan.steps):
            step = plan.steps[step_idx]

            if step.status == StepStatus.COMPLETED:
                step_idx += 1
                continue

            logger.info(
                f"[PlanningFlow:{flow_id}] "
                f"Step {step.id + 1}/{len(plan.steps)}: {step.description}"
            )
            step.status = StepStatus.IN_PROGRESS

            agent      = self._select_agent(step)
            result     = None
            success    = False
            attempts   = 1
            error_msg  = None  # Fix: track error message for blocked steps
            t_step     = time.monotonic()  # Fix: track step duration

            # ── First attempt ──────────────────────────────────────────
            try:
                result = await agent.run(step.description)
                step.status = StepStatus.COMPLETED
                success = True
            except Exception as e:
                logger.warning(
                    f"[PlanningFlow:{flow_id}] "
                    f"Step {step.id + 1} failed (attempt 1): {e}"
                )
                error_msg = str(e)  # Fix: capture error message

            # ── Retry with a fresh agent ───────────────────────────────
            if not success:
                attempts = 2
                logger.info(
                    f"[PlanningFlow:{flow_id}] "
                    f"Retrying step {step.id + 1} with safe agent…"
                )
                retry_agent = self._select_agent(step, prefer_safe=True)
                try:
                    result = await retry_agent.run(
                        f"RETRY: The previous attempt at this step failed. "
                        f"Try a completely different approach.\n\nStep: {step.description}"
                    )
                    step.status = StepStatus.COMPLETED
                    success = True
                except Exception as e2:
                    logger.error(
                        f"[PlanningFlow:{flow_id}] "
                        f"Step {step.id + 1} blocked after retry: {e2}"
                    )
                    step.status = StepStatus.BLOCKED
                    error_msg = str(e2)  # Fix: capture blocked error

            # ── Score the result ───────────────────────────────────────
            score = 0.0
            if success and result:
                score = self._score_result(result, step.success_criteria or "")
                step.success_score = score
                logger.info(
                    f"[PlanningFlow:{flow_id}] "
                    f"Step {step.id + 1} score: {score:.2f} "
                    f"(criteria: {step.success_criteria or 'none'})"
                )

            # ── Record in flow result ──────────────────────────────────
            elapsed_s = round(time.monotonic() - t_step, 2)  # Fix: compute step duration
            flow.steps.append(FlowStepResult(
                step_id       = step.id,
                description   = step.description,
                status        = step.status,
                output        = (result or "")[:400],
                error         = error_msg,  # Fix: populate error field for blocked steps
                attempts      = attempts,
                duration_s    = elapsed_s,  # Fix: populate duration
                success_score = score,
            ))

            # ── Replan if blocked OR score too low ─────────────────────
            should_replan = (
                step.status == StepStatus.BLOCKED
                or (success and score < self._threshold and step.success_criteria)
            )
            remaining = plan.steps[step_idx + 1:]
            if should_replan and remaining:
                cause = (
                    "step blocked"
                    if step.status == StepStatus.BLOCKED
                    else f"low success score ({score:.2f})"
                )
                logger.info(
                    f"[PlanningFlow:{flow_id}] Replanning remaining steps "
                    f"(cause: {cause})…"
                )
                completed_ctx = "\n".join(
                    f"✓ {s.description}"
                    for s in plan.steps[:step_idx]
                    if s.status == StepStatus.COMPLETED
                )
                new_plan = await self._replan(
                    flow.goal, completed_ctx, remaining, cause
                )
                plan.steps = plan.steps[:step_idx + 1] + new_plan.steps
                # Fix: offset new step IDs to avoid collisions with existing IDs
                existing_count = len(plan.steps[:step_idx + 1])
                for new_step in new_plan.steps:
                    new_step.id += existing_count

            step_idx += 1

    async def _run_flow(self, goal: str) -> FlowResult:
        flow_id = str(uuid.uuid4())[:8]
        logger.info(f"[PlanningFlow:{flow_id}] ▶ Goal: {goal[:120]}")

        plan = await self._create_plan(goal)
        flow = FlowResult(flow_id=flow_id, goal=goal)

        timed_out = False

        try:
            if hasattr(asyncio, "timeout"):
                async with asyncio.timeout(self._timeout):
                    await self._run_flow_body(plan, flow, flow_id)
            else:
                await asyncio.wait_for(
                    self._run_flow_body(plan, flow, flow_id),
                    timeout=self._timeout,
                )
        except asyncio.TimeoutError:
            logger.warning(f"[PlanningFlow:{flow_id}] Global timeout reached.")
            timed_out = True

        flow.timed_out = timed_out
        logger.info(
            f"[PlanningFlow:{flow_id}] ■ Complete. "
            f"success_rate={flow.success_rate:.0%} "
            f"avg_score={flow.avg_success_score:.2f}"
        )
        return flow

    # ──────────────────────────────────────────────────────────────────────────
    # Plan creation / re-planning
    # ──────────────────────────────────────────────────────────────────────────

    async def _create_plan(self, goal: str) -> Plan:
        from app.llm.llm import LLM
        llm      = LLM()
        response = await llm.ask([
            Message.system(_PLAN_SYSTEM),
            Message.user(f"Goal: {goal}"),
        ])
        return self._parse_plan(response.content or "", goal)

    async def _replan(
        self,
        goal: str,
        completed_ctx: str,
        remaining: list[PlanStep],
        cause: str,
    ) -> Plan:
        from app.llm.llm import LLM
        llm    = LLM()
        prompt = (
            f"Original goal: {goal}\n\n"
            f"Reason for replan: {cause}\n\n"
            f"Completed so far:\n{completed_ctx or '(none)'}\n\n"
            f"Remaining steps (may need revision):\n"
            + "\n".join(f"- {s.description}" for s in remaining)
        )
        response = await llm.ask([
            Message.system(_REPLAN_SYSTEM),
            Message.user(prompt),
        ])
        return self._parse_plan(response.content or "", goal)

    def _parse_plan(self, raw: str, goal: str) -> Plan:
        steps: list[PlanStep] = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in (". ", ") ", "- "):
                parts = line.split(sep, 1)
                if len(parts) == 2 and parts[0].lstrip("0123456789").strip() == "":
                    line = parts[1].strip()
                    break
            if not line:
                continue

            # Extract success criterion from trailing parentheses
            criterion: Optional[str] = None
            m = re.search(r"\(([^)]{5,})\)\s*$", line)
            if m:
                criterion = m.group(1).strip()
                line      = line[:m.start()].strip()

            if line:
                steps.append(PlanStep(
                    id               = len(steps),
                    description      = line,
                    success_criteria = criterion,
                ))

        if not steps:
            steps = [PlanStep(id=0, description=goal)]

        logger.info(f"[PlanningFlow] Plan: {len(steps)} steps.")
        return Plan(id=str(uuid.uuid4())[:8], title=goal[:80], steps=steps)

    # ──────────────────────────────────────────────────────────────────────────
    # Success scoring
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _score_result(result: str, criteria: str) -> float:
        """
        Score how well the result meets the success criterion.

        Approach: extract significant keywords from the criterion,
        count how many appear in the result (case-insensitive), then
        normalise to 0.0–1.0.

        Returns 1.0 when no criterion is defined (can't fail).
        """
        if not criteria:
            return 1.0

        # Tokenise: words ≥ 4 chars, ignore stop words
        _STOP = {"the", "and", "has", "have", "been", "with", "that", "this",
                  "from", "into", "should", "must", "will", "file", "saved",
                  "written", "each", "every"}
        tokens = [
            w.lower()
            for w in re.findall(r"[a-zA-Z]{4,}", criteria)
            if w.lower() not in _STOP
        ]
        if not tokens:
            return 1.0

        result_lower = result.lower()
        hits = sum(1 for t in tokens if t in result_lower)
        return round(hits / len(tokens), 2)

    # ──────────────────────────────────────────────────────────────────────────
    # Agent selection
    # ──────────────────────────────────────────────────────────────────────────

    def _select_agent(self, step: PlanStep, prefer_safe: bool = False):
        """
        FIX: Cache agent instances instead of creating a new one per step.
        Creating Manus() per step spawns a new Bash subprocess each time,
        leaking file descriptors and memory for the duration of the flow.
        Agents are reused across steps and cleaned up when the flow ends.
        """
        from app.agent.manus import Manus
        from app.agent.data_analysis import DataAnalysisAgent

        if not hasattr(self, "_agent_cache"):
            self._agent_cache: dict = {}

        cfg = Config.get()
        desc_lower = step.description.lower()

        if (
            cfg.runflow.enable_data_analysis
            and not prefer_safe
            and any(kw in desc_lower for kw in
                    ["chart", "graph", "plot", "analysis", "visuali", "data"])
        ):
            if "data_analysis" not in self._agent_cache:
                self._agent_cache["data_analysis"] = DataAnalysisAgent()
            return self._agent_cache["data_analysis"]

        if "manus" not in self._agent_cache:
            self._agent_cache["manus"] = Manus()
        return self._agent_cache["manus"]

    # ──────────────────────────────────────────────────────────────────────────
    # Summary formatting
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _format_summary(flow: FlowResult) -> str:
        completed = [s for s in flow.steps if s.status == StepStatus.COMPLETED]
        blocked   = [s for s in flow.steps if s.status == StepStatus.BLOCKED]
        in_prog   = [s for s in flow.steps if s.status == StepStatus.IN_PROGRESS]

        lines = ["=== PlanningFlow Result ===", ""]

        if completed:
            lines.append("Completed:")
            for s in completed:
                lines.append(
                    f"  ✓ Step {s.step_id + 1}: {s.description[:60]} "
                    f"[score={s.success_score:.2f}]"
                )
                if s.output:
                    lines.append(f"    → {s.output[:200]}")
            lines.append("")

        if blocked or in_prog:
            lines.append("Blocked / Incomplete:")
            for s in blocked + in_prog:
                icon = "✗" if s.status == StepStatus.BLOCKED else "⏸"
                lines.append(f"  {icon} Step {s.step_id + 1}: {s.description[:60]}")
                if s.error:
                    lines.append(f"    Error: {s.error[:120]}")
            lines.append("")

        lines.append(
            f"Total: {len(completed)} completed, {len(blocked)} blocked. "
            f"Avg success score: {flow.avg_success_score:.2f}."
        )
        if flow.timed_out:
            lines.append("⏱ Flow timed out before all steps completed.")

        return "\n".join(lines)
