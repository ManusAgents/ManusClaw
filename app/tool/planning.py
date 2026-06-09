from __future__ import annotations

import uuid
from typing import Any, Optional

from app.schema import Plan, PlanStep, StepStatus, ToolResult
from app.tool.base import BaseTool


class PlanningTool(BaseTool):
    name = "planning"
    description = "Create and manage multi-step execution plans."
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["create", "update", "mark_step", "get"],
                "description": "Operation: create, update, mark_step, or get.",
            },
            "plan_id": {"type": "string", "description": "Plan ID (omit to auto-generate on create)."},
            "title": {"type": "string", "description": "Plan title (create/update)."},
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of step descriptions (create/update).",
            },
            "step_index": {"type": "integer", "description": "Zero-based step index (mark_step)."},
            "step_status": {
                "type": "string",
                "enum": ["not_started", "in_progress", "completed", "blocked"],
                "description": "New status for the step.",
            },
            "notes": {"type": "string", "description": "Notes for a step."},
        },
        "required": ["command"],
    }

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}

    async def execute(
        self,
        command: str,
        plan_id: Optional[str] = None,
        title: Optional[str] = None,
        steps: Optional[list[str]] = None,
        step_index: Optional[int] = None,
        step_status: Optional[str] = None,
        notes: Optional[str] = None,
        **_: Any,
    ) -> ToolResult:
        if command == "create":
            pid = plan_id or str(uuid.uuid4())[:8]
            plan = Plan(
                id=pid,
                title=title or "Untitled Plan",
                steps=[PlanStep(id=i, description=d) for i, d in enumerate(steps or [])],
            )
            self._plans[pid] = plan
            return ToolResult(output=self._render(plan))

        if command == "get":
            if not plan_id or plan_id not in self._plans:
                return ToolResult(error=f"Plan '{plan_id}' not found.")
            return ToolResult(output=self._render(self._plans[plan_id]))

        if command == "update":
            if not plan_id or plan_id not in self._plans:
                return ToolResult(error=f"Plan '{plan_id}' not found.")
            plan = self._plans[plan_id]
            if title:
                plan.title = title
            if steps is not None:
                plan.steps = [PlanStep(id=i, description=d) for i, d in enumerate(steps)]
            return ToolResult(output=self._render(plan))

        if command == "mark_step":
            if not plan_id or plan_id not in self._plans:
                return ToolResult(error=f"Plan '{plan_id}' not found.")
            plan = self._plans[plan_id]
            if step_index is None or step_index >= len(plan.steps):
                return ToolResult(error=f"Invalid step_index {step_index}.")
            step = plan.steps[step_index]
            if step_status:
                step.status = StepStatus(step_status)
            if notes:
                step.notes = notes
            return ToolResult(output=self._render(plan))

        return ToolResult(error=f"Unknown command: {command}")

    def _render(self, plan: Plan) -> str:
        icons = {
            StepStatus.NOT_STARTED: "[ ]",
            StepStatus.IN_PROGRESS: "[→]",
            StepStatus.COMPLETED: "[✓]",
            StepStatus.BLOCKED: "[✗]",
        }
        lines = [f"Plan: {plan.title} (id={plan.id})", ""]
        for step in plan.steps:
            icon = icons.get(step.status, "[ ]")
            note = f" — {step.notes}" if step.notes else ""
            lines.append(f"  {icon} {step.id}. {step.description}{note}")
        return "\n".join(lines)

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        return self._plans.get(plan_id)
