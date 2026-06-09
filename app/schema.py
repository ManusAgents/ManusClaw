from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Roles & States
# ──────────────────────────────────────────────────────────────────────────────

class Role(str, Enum):
    SYSTEM    = "system"
    USER      = "user"
    ASSISTANT = "assistant"
    TOOL      = "tool"


class AgentState(str, Enum):
    IDLE     = "IDLE"
    RUNNING  = "RUNNING"
    FINISHED = "FINISHED"
    ERROR    = "ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# Message primitives
# ──────────────────────────────────────────────────────────────────────────────

class Function(BaseModel):
    name:      str
    arguments: str  # JSON-encoded string


class ToolCall(BaseModel):
    id:       str
    type:     Literal["function"] = "function"
    function: Function


class Message(BaseModel):
    role:         Role
    content:      Optional[str]           = None
    tool_calls:   Optional[list[ToolCall]] = None
    tool_call_id: Optional[str]           = None
    name:         Optional[str]           = None

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content:    Optional[str]           = None,
        tool_calls: Optional[list[ToolCall]] = None,
    ) -> "Message":
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, content: str, tool_call_id: str, name: str) -> "Message":
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


# ──────────────────────────────────────────────────────────────────────────────
# Memory — context-window-aware
# ──────────────────────────────────────────────────────────────────────────────

class Memory(BaseModel):
    messages:     list[Message] = Field(default_factory=list)
    max_messages: int           = 100

    def add(self, message: Message) -> None:
        self.messages.append(message)
        self._trim()

    def _trim(self) -> None:
        if len(self.messages) <= self.max_messages:
            return
        system = [m for m in self.messages if m.role == Role.SYSTEM]
        rest = [m for m in self.messages if m.role != Role.SYSTEM]
        keep = max(self.max_messages - len(system), 0)
        if not keep:
            self.messages = system
            return
        trimmed = list(rest[-keep:])
        # FIX: Prevent orphaned TOOL messages at the start of the trimmed window.
        # TOOL messages must follow an ASSISTANT message with tool_calls.
        # If the trim boundary cuts into the middle of an assistant+tool pair,
        # the API returns HTTP 400 "role: tool must follow assistant with tool_calls".
        while trimmed and trimmed[0].role == Role.TOOL:
            trimmed.pop(0)
        # Also drop ASSISTANT messages whose tool_calls were trimmed away
        while (trimmed and trimmed[0].role == Role.ASSISTANT
               and trimmed[0].tool_calls
               and (len(trimmed) < 2 or trimmed[1].role != Role.TOOL)):
            trimmed.pop(0)
        self.messages = system + trimmed

    def to_list(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.messages]

    def clear(self) -> None:
        self.messages = []

    def token_estimate(self) -> int:
        total = sum(len(m.content or "") for m in self.messages)
        return total // 4


# ──────────────────────────────────────────────────────────────────────────────
# PAORR loop primitives — Plan, Act, Observe, Reflect, Retry
# ──────────────────────────────────────────────────────────────────────────────

class Observation(BaseModel):
    """Result of a single tool execution."""
    tool_name:  str
    args:       dict[str, Any]
    output:     Optional[str]
    error:      Optional[str]
    success:    bool
    attempt:    int      = 1
    duration_ms: int     = 0
    timestamp:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def summary(self) -> str:
        if self.success:
            out = (self.output or "")[:400]
            return f"[{self.tool_name}] ✓ {out}"
        return f"[{self.tool_name}] ✗ ERROR: {self.error}"


class Reflection(BaseModel):
    """LLM-generated reflection on whether an observation solved the goal."""
    step_goal:            str
    observation_summary:  str
    solved:               bool
    reason:               str
    next_action:          Optional[str] = None

    def to_prompt(self) -> str:
        status = "SOLVED" if self.solved else "NOT SOLVED"
        lines = [
            f"Reflection [{status}]:",
            f"  Goal: {self.step_goal}",
            f"  Observation: {self.observation_summary}",
            f"  Reason: {self.reason}",
        ]
        if self.next_action:
            lines.append(f"  Next action: {self.next_action}")
        return "\n".join(lines)


class TaskStep(BaseModel):
    """A single step in the PAORR execution history."""
    step_number:  int
    goal:         str
    observations: list[Observation] = Field(default_factory=list)
    reflection:   Optional[Reflection] = None
    resolved:     bool     = False
    timestamp:    datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def summary(self) -> str:
        status = "✓" if self.resolved else "✗"
        obs_summaries = " | ".join(o.summary() for o in self.observations[-3:])
        return f"Step {self.step_number} {status}: {self.goal[:60]} → {obs_summaries}"


class TaskHistory(BaseModel):
    """Persistent log of all steps across a task run."""
    task_id:       str
    original_goal: str
    steps:         list[TaskStep] = Field(default_factory=list)
    created_at:    datetime       = Field(default_factory=lambda: datetime.now(timezone.utc))

    def add_step(self, goal: str) -> TaskStep:
        step = TaskStep(step_number=len(self.steps) + 1, goal=goal)
        self.steps.append(step)
        return step

    def last_step(self) -> Optional[TaskStep]:
        return self.steps[-1] if self.steps else None

    def context_summary(self, max_steps: int = 5) -> str:
        recent = self.steps[-max_steps:]
        if not recent:
            return "No prior steps."
        lines = ["=== Task History (recent steps) ==="]
        for s in recent:
            lines.append(s.summary())
        lines.append("=== End History ===")
        return "\n".join(lines)

    def is_looping(self, window: int = 3) -> bool:
        if len(self.steps) < window:
            return False
        recent = self.steps[-window:]
        if all(not s.resolved for s in recent):
            tool_names = [
                o.tool_name
                for s in recent
                for o in s.observations
            ]
            if len(set(tool_names)) == 1 and tool_names:
                return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# ToolResult
# ──────────────────────────────────────────────────────────────────────────────

class ToolResult(BaseModel):
    output:       Optional[str] = None
    error:        Optional[str] = None
    system:       Optional[str] = None
    base64_image: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None

    def __str__(self) -> str:
        parts = []
        if self.output:
            parts.append(self.output)
        if self.error:
            parts.append(f"ERROR: {self.error}")
        if self.system:
            parts.append(f"[system: {self.system}]")
        return "\n".join(parts) if parts else "(no output)"


# ──────────────────────────────────────────────────────────────────────────────
# Planning primitives
# ──────────────────────────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    BLOCKED     = "blocked"


class PlanStep(BaseModel):
    id:               int
    description:      str
    status:           StepStatus   = StepStatus.NOT_STARTED
    assigned_to:      Optional[str] = None
    notes:            Optional[str] = None
    success_criteria: Optional[str] = None  # extracted from description parens
    success_score:    float          = 0.0  # 0.0-1.0 after completion


class Plan(BaseModel):
    id:    str
    title: str
    steps: list[PlanStep] = Field(default_factory=list)

    def next_step(self) -> Optional[PlanStep]:
        for step in self.steps:
            if step.status == StepStatus.NOT_STARTED:
                return step
        return None

    def is_complete(self) -> bool:
        return all(s.status == StepStatus.COMPLETED for s in self.steps)

    def completion_rate(self) -> float:
        if not self.steps:
            return 0.0
        done = sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)
        return done / len(self.steps)


# ──────────────────────────────────────────────────────────────────────────────
# Retry Policy
# ──────────────────────────────────────────────────────────────────────────────

class RetryPolicy(BaseModel):
    """
    Declarative retry configuration.
    Used by agents, flows, and the orchestrator.
    """
    max_attempts:     int   = 3
    base_wait_s:      float = 1.0
    max_wait_s:       float = 30.0
    exponential_base: float = 2.0
    jitter:           bool  = True

    def wait_for(self, attempt: int) -> float:
        """Return the wait duration (seconds) before the given attempt number."""
        import random
        wait = min(
            self.base_wait_s * (self.exponential_base ** (attempt - 1)),
            self.max_wait_s,
        )
        if self.jitter:
            wait += random.uniform(0, wait * 0.3)
        return round(wait, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Agent run contracts
# ──────────────────────────────────────────────────────────────────────────────

class AgentRunConfig(BaseModel):
    """
    Typed contract for starting an agent run.
    Passed to server endpoints and CLI entry points.
    """
    prompt:       str
    mode:         str            = "build"
    max_steps:    int            = 30
    timeout:      int            = 3600
    session_id:   Optional[str]  = None
    retry_policy: RetryPolicy    = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def _validate_mode(self) -> "AgentRunConfig":
        if self.mode not in ("build", "plan"):
            raise ValueError(f"mode must be 'build' or 'plan', got '{self.mode}'")
        return self


class AgentRunResult(BaseModel):
    """Typed result returned after an agent run completes."""
    session_id:  str
    agent_name:  str
    prompt:      str
    output:      str
    state:       AgentState
    step_count:  int
    duration_s:  float
    success:     bool
    trace_id:    Optional[str] = None

    @property
    def failed(self) -> bool:
        return not self.success


# ──────────────────────────────────────────────────────────────────────────────
# Role decision primitives
# ──────────────────────────────────────────────────────────────────────────────

class RoleDecision(str, Enum):
    """
    Decision made by a role after processing its input.

    PROCEED  — output is ready; publish to the next role
    RETRY    — output is incomplete; retry with a focused correction prompt
    ESCALATE — input is ambiguous/unclear; request clarification or skip
    BLOCKED  — role cannot proceed even after retries; pipeline should continue with error
    """
    PROCEED  = "proceed"
    RETRY    = "retry"
    ESCALATE = "escalate"
    BLOCKED  = "blocked"


class RoleResult(BaseModel):
    """Typed result from a single role execution."""
    role_name:         str
    decision:          RoleDecision
    output:            str
    artefact:          Optional[str] = None
    duration_s:        float         = 0.0
    retry_count:       int           = 0
    escalation_reason: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.decision in (RoleDecision.PROCEED, RoleDecision.ESCALATE)


# ──────────────────────────────────────────────────────────────────────────────
# Flow contracts
# ──────────────────────────────────────────────────────────────────────────────

class FlowStepResult(BaseModel):
    """Result of a single step in a PlanningFlow execution."""
    step_id:      int
    description:  str
    status:       StepStatus
    output:       Optional[str] = None
    error:        Optional[str] = None
    attempts:     int           = 1
    duration_s:   float         = 0.0
    success_score: float        = 0.0   # 0.0-1.0: how well success_criteria was met


class FlowResult(BaseModel):
    """Aggregated result from a complete PlanningFlow execution."""
    flow_id:        str
    goal:           str
    steps:          list[FlowStepResult] = Field(default_factory=list)
    total_duration_s: float              = 0.0
    timed_out:      bool                 = False

    @property
    def success_rate(self) -> float:
        if not self.steps:
            return 0.0
        done = sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)
        return done / len(self.steps)

    @property
    def avg_success_score(self) -> float:
        completed = [s for s in self.steps if s.status == StepStatus.COMPLETED]
        if not completed:
            return 0.0
        return sum(s.success_score for s in completed) / len(completed)


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline (Orchestrator) contracts
# ──────────────────────────────────────────────────────────────────────────────

class PipelineStageResult(BaseModel):
    """Result of a single role stage in the orchestrator pipeline."""
    role_name:   str
    status:      str            # "completed" | "error" | "skipped"
    output:      str
    duration_s:  float          = 0.0
    decision:    Optional[str]  = None


class PipelineResult(BaseModel):
    """Aggregated result from a complete MultiAgentOrchestrator run."""
    pipeline_id:     str
    goal:            str
    stages:          list[PipelineStageResult] = Field(default_factory=list)
    total_duration_s: float                    = 0.0
    timed_out:        bool                     = False
    verdict:          str                      = "unknown"  # "approved"|"rework"|"timeout"|"error"

    def to_summary(self) -> str:
        lines = [
            "═══════════════════════════════════════════════════",
            "  ManusClaw Multi-Agent Pipeline — Final Report",
            "═══════════════════════════════════════════════════",
            f"  Pipeline ID : {self.pipeline_id}",
            f"  Goal        : {self.goal[:80]}",
            f"  Duration    : {self.total_duration_s:.1f}s",
            f"  Verdict     : {self.verdict.upper()}",
            "",
        ]
        for stage in self.stages:
            icon = "✓" if stage.status == "completed" else "✗"
            output_preview = stage.output[:100] + "..." if len(stage.output) > 100 else stage.output[:100]
            lines.append(
                f"  {icon} {stage.role_name.replace('_', ' ').title():<20s} "
                f"[{stage.duration_s:.1f}s] — {output_preview}"
            )
        if self.timed_out:
            lines.append("\n  ⏱ Pipeline timed out before all stages completed.")
        lines.append("═══════════════════════════════════════════════════")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Tool call contract
# ──────────────────────────────────────────────────────────────────────────────

class ToolCallContract(BaseModel):
    """
    Typed contract for a tool call.
    Used for validation, documentation, and testing.
    """
    tool_name:            str
    args:                 dict[str, Any]
    expected_output_type: str            = "text"   # "text" | "json" | "file" | "image"
    timeout_s:            Optional[int]  = None
    retry_policy:         RetryPolicy    = Field(default_factory=RetryPolicy)

    def validate_args(self, schema: dict) -> tuple[bool, str]:
        """Validate args against a JSON Schema dict. Returns (valid, error_msg)."""
        required = schema.get("required", [])
        missing = [k for k in required if k not in self.args]
        if missing:
            return False, f"Missing required args: {missing}"
        return True, ""
