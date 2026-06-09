from __future__ import annotations

"""
ManusClaw Exception Hierarchy
==============================
All exceptions inherit from ManusClawError.

Retry semantics:
  RetryableError    → the caller SHOULD retry after a wait
  NonRetryableError → the caller MUST NOT retry; propagate immediately

Layer-specific exceptions carry their layer name so that log aggregators
and error handlers can bucket them without string matching.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Root
# ──────────────────────────────────────────────────────────────────────────────

class ManusClawError(Exception):
    """Base exception for all ManusClaw errors."""


# ──────────────────────────────────────────────────────────────────────────────
# Retry semantics
# ──────────────────────────────────────────────────────────────────────────────

class RetryableError(ManusClawError):
    """
    The operation failed but MAY succeed if retried after `wait_s` seconds.
    Callers should honour the wait before retrying.
    """
    def __init__(self, message: str, wait_s: float = 2.0) -> None:
        super().__init__(message)
        self.wait_s = wait_s


class NonRetryableError(ManusClawError):
    """
    The operation failed in a way that retrying will not fix.
    Callers must propagate this immediately.
    """


# ──────────────────────────────────────────────────────────────────────────────
# LLM layer
# ──────────────────────────────────────────────────────────────────────────────

class TokenLimitExceeded(NonRetryableError):
    """LLM context window overflow — retrying with the same messages won't help."""


class RateLimitError(RetryableError):
    """LLM provider rate limit hit — retry after wait_s."""
    def __init__(self, message: str = "Rate limited", wait_s: float = 5.0) -> None:
        super().__init__(message, wait_s=wait_s)


class LLMAuthError(NonRetryableError):
    """Invalid or missing API key."""


# ──────────────────────────────────────────────────────────────────────────────
# Tool layer
# ──────────────────────────────────────────────────────────────────────────────

class ToolError(ManusClawError):
    """A tool encountered an error during execution."""
    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"[{tool_name}] {message}")
        self.tool_name = tool_name


class ToolNotFoundError(NonRetryableError):
    """Requested tool does not exist in the collection."""
    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool '{tool_name}' not found.")
        self.tool_name = tool_name


class PermissionDeniedError(NonRetryableError):
    """Tool call was blocked by the permission gate."""
    def __init__(self, tool_name: str, reason: str = "") -> None:
        msg = f"Permission denied for tool '{tool_name}'."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg)
        self.tool_name = tool_name


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

class ConfigError(NonRetryableError):
    """Invalid or missing configuration value."""


class MissingEnvVar(ConfigError):
    """A required environment variable is not set."""
    def __init__(self, var_name: str) -> None:
        super().__init__(f"Required environment variable '{var_name}' is not set.")
        self.var_name = var_name


# ──────────────────────────────────────────────────────────────────────────────
# Sandbox
# ──────────────────────────────────────────────────────────────────────────────

class SandboxError(ManusClawError):
    """Docker sandbox-related error."""


# ──────────────────────────────────────────────────────────────────────────────
# MCP
# ──────────────────────────────────────────────────────────────────────────────

class MCPError(ManusClawError):
    """MCP server/client integration error."""

    def __init__(self, server_name: str, message: str) -> None:
        super().__init__(f"[MCP:{server_name}] {message}")
        self.server_name = server_name


# ──────────────────────────────────────────────────────────────────────────────
# Agent layer
# ──────────────────────────────────────────────────────────────────────────────

class AgentError(ManusClawError):
    """Base class for agent-level errors."""
    def __init__(self, agent_name: str, message: str) -> None:
        super().__init__(f"[Agent:{agent_name}] {message}")
        self.agent_name = agent_name


class AgentTimeoutError(AgentError):
    """Agent exceeded its max_steps or wall-clock timeout."""


class AgentLoopError(AgentError):
    """Agent detected it is stuck in an infinite loop."""


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator layer
# ──────────────────────────────────────────────────────────────────────────────

class OrchestratorError(ManusClawError):
    """Error raised by the MultiAgentOrchestrator."""

    def __init__(self, message: str, pipeline: list[str] | None = None) -> None:
        super().__init__(message)
        self.pipeline = pipeline or []


class PipelineCycleError(OrchestratorError):
    """The declared dependency graph contains a cycle."""


# ──────────────────────────────────────────────────────────────────────────────
# Flow layer
# ──────────────────────────────────────────────────────────────────────────────

class FlowError(ManusClawError):
    """Error raised by the PlanningFlow."""

    def __init__(self, message: str, step_id: int | None = None) -> None:
        super().__init__(message)
        self.step_id = step_id


class FlowTimeoutError(FlowError):
    """PlanningFlow exceeded its global timeout."""


# ──────────────────────────────────────────────────────────────────────────────
# Role layer
# ──────────────────────────────────────────────────────────────────────────────

class RoleError(ManusClawError):
    """Error raised by a specialist role."""

    def __init__(self, role_name: str, message: str) -> None:
        super().__init__(f"[Role:{role_name}] {message}")
        self.role_name = role_name


class RoleValidationError(RoleError):
    """Role received an input that failed validation."""


class RoleEscalationError(RoleError):
    """Role escalated because it cannot proceed with the given input."""
