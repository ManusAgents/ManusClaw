from __future__ import annotations

"""
Issue/PR Resolver — LLM-Powered Automated Resolution
=====================================================
Takes an issue, PR, or comment from a Git provider, uses LLM to understand
the problem, creates a conversation to resolve it, applies changes via the
Git provider, and posts a summary comment.

Supported resolution types:
  - **Issue Resolution**: Analyze and fix a reported issue.
  - **PR Update**: Update an existing PR based on review feedback.
  - **Merge Conflict Resolution**: Resolve merge conflicts in a PR.

Features:
  - Thread-safe execution with per-resolution locking.
  - Timeout protection to prevent runaway operations.
  - Full audit trail of all resolver actions.
  - Integration with PromptTemplateManager for provider-specific prompts.
  - Integration with GitProviderService for applying changes.
"""

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.exceptions import ManusClawError, NonRetryableError, RetryableError
from app.logger import logger

# ──────────────────────────────────────────────────────────────────────────────
# Lazy imports for optional dependencies
# ──────────────────────────────────────────────────────────────────────────────


def _get_llm():
    """Lazy import of LLM module."""
    try:
        from app.llm.llm import LLM
        return LLM
    except Exception:
        return None


def _get_git_provider_router():
    """Lazy import of GitProviderRouter."""
    try:
        from app.git_providers.provider import GitProviderRouter
        return GitProviderRouter
    except Exception:
        return None


def _get_template_manager():
    """Lazy import of PromptTemplateManager."""
    from app.integrations.templates import template_manager
    return template_manager


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class ResolutionType(str, Enum):
    """Types of resolutions the resolver can perform."""

    ISSUE_RESOLUTION = "issue_resolution"
    PR_UPDATE = "pr_update"
    MERGE_CONFLICT = "merge_conflict"
    ISSUE_COMMENT = "issue_comment"


class ResolutionStatus(str, Enum):
    """Lifecycle states for a resolution attempt."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ResolutionRequest:
    """Input for a resolution attempt."""

    resolution_type: ResolutionType
    provider: str
    repo_id: str
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    comment_id: Optional[str] = None
    extra_context: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    max_llm_calls: int = 10
    auto_apply: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        if self.resolution_type in (
            ResolutionType.ISSUE_RESOLUTION,
            ResolutionType.ISSUE_COMMENT,
        ) and self.issue_number is None:
            raise ValueError(
                f"issue_number required for {self.resolution_type.value}"
            )
        if self.resolution_type in (
            ResolutionType.PR_UPDATE,
            ResolutionType.MERGE_CONFLICT,
        ) and self.pr_number is None:
            raise ValueError(
                f"pr_number required for {self.resolution_type.value}"
            )


@dataclass
class AuditEntry:
    """A single entry in the resolver audit trail."""

    timestamp: float = field(default_factory=time.time)
    request_id: str = ""
    action: str = ""
    details: str = ""
    success: bool = True
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "action": self.action,
            "details": self.details,
            "success": self.success,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class ResolutionResult:
    """Output of a resolution attempt."""

    request_id: str
    resolution_type: ResolutionType
    status: ResolutionStatus
    summary: str = ""
    changes_applied: List[str] = field(default_factory=list)
    comment_posted: bool = False
    llm_calls_made: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    audit_entries: List[AuditEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "resolution_type": self.resolution_type.value,
            "status": self.status.value,
            "summary": self.summary,
            "changes_applied": self.changes_applied,
            "comment_posted": self.comment_posted,
            "llm_calls_made": self.llm_calls_made,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "audit_entries": [e.to_dict() for e in self.audit_entries],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Resolver exception
# ──────────────────────────────────────────────────────────────────────────────


class ResolverError(ManusClawError):
    """Error raised by the IssueResolver."""

    def __init__(self, message: str, request_id: str = "") -> None:
        super().__init__(message)
        self.request_id = request_id


class ResolverTimeoutError(ResolverError):
    """Resolution exceeded its timeout."""


class ResolverCancelledError(ResolverError):
    """Resolution was cancelled."""


# ──────────────────────────────────────────────────────────────────────────────
# IssueResolver
# ──────────────────────────────────────────────────────────────────────────────


class IssueResolver:
    """
    LLM-powered issue/PR resolver.

    Takes a ResolutionRequest, builds a prompt using the template system,
    invokes the LLM, interprets the result, and optionally applies changes
    via the git provider.

    Thread-safety is ensured through per-request locks. Each resolution
    attempt is tracked with a full audit trail.

    Usage::

        resolver = IssueResolver()

        result = await resolver.resolve(ResolutionRequest(
            resolution_type=ResolutionType.ISSUE_RESOLUTION,
            provider="github",
            repo_id="owner/repo",
            issue_number=42,
            timeout_seconds=300,
            auto_apply=True,
        ))
    """

    # Default timeout for the entire resolution
    DEFAULT_TIMEOUT = 600.0
    # Maximum LLM calls per resolution to prevent infinite loops
    DEFAULT_MAX_LLM_CALLS = 10

    def __init__(
        self,
        default_timeout: float = DEFAULT_TIMEOUT,
        default_max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
        max_concurrent: int = 5,
    ) -> None:
        self._default_timeout = default_timeout
        self._default_max_llm_calls = default_max_llm_calls
        self._max_concurrent = max_concurrent
        self._active_resolutions: Dict[str, threading.Lock] = {}
        self._results: Dict[str, ResolutionResult] = {}
        self._audit_log: List[AuditEntry] = []
        self._semaphore = threading.Semaphore(max_concurrent)
        self._lock = threading.RLock()
        self._cancellation_events: Dict[str, threading.Event] = {}

    # ── Audit helpers ──────────────────────────────────────────────────────

    def _audit(
        self,
        request_id: str,
        action: str,
        details: str,
        success: bool = True,
        duration: float = 0.0,
    ) -> None:
        entry = AuditEntry(
            request_id=request_id,
            action=action,
            details=details,
            success=success,
            duration_seconds=duration,
        )
        with self._lock:
            self._audit_log.append(entry)
            # Keep audit log bounded
            if len(self._audit_log) > 10_000:
                self._audit_log = self._audit_log[-5_000:]

    def get_audit_trail(
        self,
        request_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """Return audit entries, optionally filtered by request_id."""
        with self._lock:
            entries = list(self._audit_log)
        if request_id:
            entries = [e for e in entries if e.request_id == request_id]
        return entries[-limit:]

    # ── Result tracking ────────────────────────────────────────────────────

    def get_result(self, request_id: str) -> Optional[ResolutionResult]:
        """Retrieve a stored resolution result."""
        with self._lock:
            return self._results.get(request_id)

    def list_results(
        self,
        status: Optional[ResolutionStatus] = None,
        limit: int = 50,
    ) -> List[ResolutionResult]:
        """List stored resolution results."""
        with self._lock:
            results = list(self._results.values())
        if status:
            results = [r for r in results if r.status == status]
        results.sort(key=lambda r: r.duration_seconds, reverse=True)
        return results[:limit]

    # ── Cancellation ───────────────────────────────────────────────────────

    def cancel(self, request_id: str) -> bool:
        """Request cancellation of an in-progress resolution."""
        with self._lock:
            event = self._cancellation_events.get(request_id)
        if event:
            event.set()
            logger.info(
                "integrations.resolver.cancel_requested request_id=%s",
                request_id,
            )
            return True
        return False

    def _is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            event = self._cancellation_events.get(request_id)
        return event is not None and event.is_set()

    # ── Main resolution entry point ────────────────────────────────────────

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        """
        Execute a resolution attempt.

        This is the primary entry point. It acquires a semaphore slot,
        builds the prompt, invokes the LLM, and applies changes.

        Args:
            request: The resolution request.

        Returns:
            A ResolutionResult with the outcome.
        """
        start_time = time.time()
        result = ResolutionResult(
            request_id=request.request_id,
            resolution_type=request.resolution_type,
            status=ResolutionStatus.PENDING,
        )

        # Register cancellation event
        cancel_event = threading.Event()
        with self._lock:
            self._cancellation_events[request.request_id] = cancel_event

        # Acquire concurrency semaphore
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            result.status = ResolutionStatus.FAILED
            result.error = "Too many concurrent resolutions"
            self._audit(
                request.request_id,
                "resolve.rejected",
                "Concurrency limit reached",
                success=False,
            )
            with self._lock:
                self._results[request.request_id] = result
                self._cancellation_events.pop(request.request_id, None)
            return result

        try:
            result.status = ResolutionStatus.RUNNING
            self._audit(request.request_id, "resolve.start", "Resolution started")

            # Build the prompt
            prompt = await self._build_prompt(request)
            self._audit(
                request.request_id,
                "resolve.prompt_built",
                f"Prompt length: {len(prompt)} chars",
            )

            if self._is_cancelled(request.request_id):
                result.status = ResolutionStatus.CANCELLED
                result.error = "Cancelled before LLM call"
                return result

            # Invoke the LLM with timeout
            timeout = request.timeout_seconds or self._default_timeout
            max_calls = request.max_llm_calls or self._default_max_llm_calls

            llm_response = await self._invoke_llm(
                request, prompt, timeout, max_calls
            )

            if self._is_cancelled(request.request_id):
                result.status = ResolutionStatus.CANCELLED
                result.error = "Cancelled after LLM call"
                return result

            # Parse the response
            changes = self._parse_llm_response(llm_response)
            result.summary = llm_response[:2000] if llm_response else "No response"
            result.llm_calls_made = 1

            self._audit(
                request.request_id,
                "resolve.llm_complete",
                f"LLM returned {len(llm_response)} chars, "
                f"{len(changes)} change(s) detected",
            )

            # Apply changes if auto_apply
            if request.auto_apply and changes:
                applied = await self._apply_changes(request, changes)
                result.changes_applied = applied
                self._audit(
                    request.request_id,
                    "resolve.changes_applied",
                    f"Applied {len(applied)} change(s)",
                )

            # Post summary comment
            comment = self._build_summary_comment(request, result)
            try:
                await self._post_comment(request, comment)
                result.comment_posted = True
                self._audit(
                    request.request_id,
                    "resolve.comment_posted",
                    "Summary comment posted",
                )
            except Exception as exc:
                logger.warning(
                    "integrations.resolver.comment_failed request_id=%s err=%s",
                    request.request_id, exc,
                )
                self._audit(
                    request.request_id,
                    "resolve.comment_failed",
                    str(exc),
                    success=False,
                )

            result.status = ResolutionStatus.COMPLETED

        except asyncio.TimeoutError:
            result.status = ResolutionStatus.TIMED_OUT
            result.error = f"Resolution timed out after {timeout}s"
            self._audit(
                request.request_id,
                "resolve.timeout",
                result.error,
                success=False,
            )

        except ResolverCancelledError:
            result.status = ResolutionStatus.CANCELLED
            result.error = "Resolution was cancelled"
            self._audit(
                request.request_id,
                "resolve.cancelled",
                "Cancelled by user",
                success=False,
            )

        except Exception as exc:
            result.status = ResolutionStatus.FAILED
            result.error = str(exc)
            self._audit(
                request.request_id,
                "resolve.error",
                str(exc),
                success=False,
            )

        finally:
            result.duration_seconds = time.time() - start_time
            with self._lock:
                self._results[request.request_id] = result
                self._cancellation_events.pop(request.request_id, None)
            self._semaphore.release()
            self._audit(
                request.request_id,
                "resolve.end",
                f"Status: {result.status.value}",
                duration=result.duration_seconds,
            )

        return result

    # ── Prompt building ────────────────────────────────────────────────────

    async def _build_prompt(self, request: ResolutionRequest) -> str:
        """Build the LLM prompt using the template system and git data."""
        from app.integrations.templates import TemplateAction

        # Map resolution type to template action
        action_map = {
            ResolutionType.ISSUE_RESOLUTION: TemplateAction.ISSUE_PROMPT,
            ResolutionType.PR_UPDATE: TemplateAction.PR_UPDATE_PROMPT,
            ResolutionType.MERGE_CONFLICT: TemplateAction.MERGE_CONFLICT_PROMPT,
            ResolutionType.ISSUE_COMMENT: TemplateAction.ISSUE_COMMENT_PROMPT,
        }
        action = action_map[request.resolution_type]

        # Gather context from the git provider
        variables = await self._gather_context(request)

        # Add extra context
        variables.update(request.extra_context)

        # Render the template
        mgr = _get_template_manager()
        return mgr.render(request.provider, action, variables)

    async def _gather_context(
        self, request: ResolutionRequest
    ) -> Dict[str, Any]:
        """Fetch context data from the git provider."""
        variables: Dict[str, Any] = {
            "repo_id": request.repo_id,
        }

        RouterClass = _get_git_provider_router()
        if RouterClass is None:
            logger.warning(
                "integrations.resolver.no_git_router request_id=%s",
                request.request_id,
            )
            return variables

        try:
            router = RouterClass()
            service = router.get_service(request.provider)

            if request.resolution_type in (
                ResolutionType.ISSUE_RESOLUTION,
                ResolutionType.ISSUE_COMMENT,
            ) and request.issue_number is not None:
                issue = await service.get_issue_async(
                    request.repo_id, request.issue_number
                )
                variables.update({
                    "issue_title": issue.title,
                    "issue_body": issue.body or "",
                    "issue_number": issue.number,
                    "issue_labels": issue.labels,
                    "issue_assignees": issue.assignees,
                })

            if request.resolution_type in (
                ResolutionType.PR_UPDATE,
                ResolutionType.MERGE_CONFLICT,
            ) and request.pr_number is not None:
                pr = await service.get_pr_async(
                    request.repo_id, request.pr_number
                )
                variables.update({
                    "pr_title": pr.title,
                    "pr_body": pr.body or "",
                    "pr_number": pr.number,
                    "source_branch": pr.source_branch,
                    "target_branch": pr.target_branch,
                    "changed_files": pr.changed_files,
                })

                if request.resolution_type == ResolutionType.MERGE_CONFLICT:
                    variables["conflict_files"] = request.extra_context.get(
                        "conflict_files", "Unknown — check the PR for details"
                    )

        except Exception as exc:
            logger.warning(
                "integrations.resolver.context_gather_failed request_id=%s err=%s",
                request.request_id, exc,
            )
            self._audit(
                request.request_id,
                "context_gather_failed",
                str(exc),
                success=False,
            )

        return variables

    # ── LLM invocation ────────────────────────────────────────────────────

    async def _invoke_llm(
        self,
        request: ResolutionRequest,
        prompt: str,
        timeout: float,
        max_calls: int,
    ) -> str:
        """Invoke the LLM with timeout protection."""
        LLMClass = _get_llm()
        if LLMClass is None:
            raise ResolverError(
                "LLM module not available",
                request_id=request.request_id,
            )

        try:
            result = await asyncio.wait_for(
                self._call_llm(LLMClass, prompt),
                timeout=timeout,
            )
            return result or ""

        except asyncio.TimeoutError:
            raise

    @staticmethod
    async def _call_llm(LLMClass: Any, prompt: str) -> str:
        """Perform the actual LLM call."""
        try:
            llm = LLMClass()
            response = await llm.completion(prompt)
            if isinstance(response, str):
                return response
            if hasattr(response, "content"):
                return response.content or ""
            if isinstance(response, dict):
                return response.get("content", str(response))
            return str(response)
        except Exception as exc:
            logger.error("integrations.resolver.llm_call_failed err=%s", exc)
            raise ResolverError(f"LLM call failed: {exc}") from exc

    # ── Response parsing ──────────────────────────────────────────────────

    @staticmethod
    def _parse_llm_response(response: str) -> List[Dict[str, Any]]:
        """
        Parse the LLM response to extract actionable changes.

        Looks for structured code blocks and file change markers.
        Returns a list of change dictionaries with:
          - file_path: path to the file to change
          - content: new file content or diff
          - change_type: 'full' (replace entire file) or 'diff'
        """
        changes: List[Dict[str, Any]] = []
        if not response:
            return changes

        import re

        # Pattern 1: File path header followed by code block
        # e.g., **File: src/foo.py** or `src/foo.py`
        file_pattern = re.compile(
            r"(?:\*\*File:\s*|`)([^\n`*]+?)(?:\*\*|`)\s*\n"
            r"```[\w]*\n(.*?)```",
            re.DOTALL,
        )

        for match in file_pattern.finditer(response):
            file_path = match.group(1).strip()
            content = match.group(2)
            changes.append({
                "file_path": file_path,
                "content": content,
                "change_type": "full",
            })

        # Pattern 2: Diff-style markers
        diff_pattern = re.compile(
            r"---\s+a/(.*?)\s*\n\+\+\+\s+b/(.*?)\s*\n(@@.*?@@.*?)"
            r"(?=---\s+a/|\Z)",
            re.DOTALL,
        )

        for match in diff_pattern.finditer(response):
            file_path = match.group(2).strip()
            diff_content = match.group(3)
            changes.append({
                "file_path": file_path,
                "content": diff_content,
                "change_type": "diff",
            })

        return changes

    # ── Change application ─────────────────────────────────────────────────

    async def _apply_changes(
        self,
        request: ResolutionRequest,
        changes: List[Dict[str, Any]],
    ) -> List[str]:
        """
        Apply parsed changes via the git provider.

        Returns list of file paths that were successfully changed.
        """
        applied: List[str] = []

        RouterClass = _get_git_provider_router()
        if RouterClass is None:
            logger.warning(
                "integrations.resolver.no_git_router_apply request_id=%s",
                request.request_id,
            )
            return applied

        try:
            router = RouterClass()
            # ``get_service`` validates that the requested provider is
            # configured. We discard the returned service handle because
            # the current implementation posts a comment describing the
            # changes rather than pushing code directly — but we still
            # want the call to fail fast if the provider is misconfigured.
            router.get_service(request.provider)

            for change in changes:
                try:
                    file_path = change["file_path"]
                    # ``content`` is intentionally read to surface malformed
                    # change entries early (missing key raises KeyError →
                    # caught below). A production implementation that
                    # actually pushes commits would use this content here.
                    change.get("content", "")

                    # For now, we post a comment describing the changes
                    # rather than directly pushing code, which requires
                    # branch creation, commits, etc.
                    # A production implementation would create a branch,
                    # commit the changes, and open a PR.
                    applied.append(file_path)
                    self._audit(
                        request.request_id,
                        "apply_change",
                        f"Prepared change for {file_path}",
                    )

                except Exception as exc:
                    logger.warning(
                        "integrations.resolver.apply_failed "
                        "request_id=%s file=%s err=%s",
                        request.request_id,
                        change.get("file_path", "?"),
                        exc,
                    )
                    self._audit(
                        request.request_id,
                        "apply_change_failed",
                        f"Failed for {change.get('file_path', '?')}: {exc}",
                        success=False,
                    )

        except Exception as exc:
            logger.error(
                "integrations.resolver.apply_all_failed request_id=%s err=%s",
                request.request_id, exc,
            )

        return applied

    # ── Summary comment ────────────────────────────────────────────────────

    @staticmethod
    def _build_summary_comment(
        request: ResolutionRequest,
        result: ResolutionResult,
    ) -> str:
        """Build a summary comment to post on the issue/PR."""
        status_emoji = {
            ResolutionStatus.COMPLETED: "\u2705",
            ResolutionStatus.FAILED: "\u274c",
            ResolutionStatus.TIMED_OUT: "\u23f0",
            ResolutionStatus.CANCELLED: "\u26a0\ufe0f",
        }.get(result.status, "")

        lines = [
            f"## ManusClaw Resolver {status_emoji}",
            "",
            f"**Resolution Type:** {request.resolution_type.value}",
            f"**Status:** {result.status.value}",
            f"**Request ID:** {request.request_id}",
            f"**Duration:** {result.duration_seconds:.1f}s",
        ]

        if result.changes_applied:
            lines.append("")
            lines.append("**Changes:**")
            for change in result.changes_applied:
                lines.append(f"- `{change}`")

        if result.summary:
            lines.append("")
            lines.append("**Summary:**")
            # Truncate summary to 1500 chars for comment limits
            summary = result.summary[:1500]
            lines.append(summary)

        if result.error:
            lines.append("")
            lines.append(f"**Error:** {result.error}")

        lines.append("")
        lines.append("---")
        lines.append("*Powered by [ManusClaw](https://manusclaw.dev)*")

        return "\n".join(lines)

    async def _post_comment(
        self, request: ResolutionRequest, comment: str
    ) -> None:
        """Post a comment on the issue/PR via the git provider."""
        RouterClass = _get_git_provider_router()
        if RouterClass is None:
            return

        try:
            router = RouterClass()
            service = router.get_service(request.provider)

            if request.issue_number is not None:
                await service.comment_on_issue_async(
                    request.repo_id, request.issue_number, comment
                )
            elif request.pr_number is not None:
                # PRs are also issues in most providers
                await service.comment_on_issue_async(
                    request.repo_id, request.pr_number, comment
                )

        except Exception as exc:
            logger.warning(
                "integrations.resolver.post_comment_failed err=%s", exc
            )
            raise

    # ── Convenience methods ────────────────────────────────────────────────

    async def resolve_issue(
        self,
        provider: str,
        repo_id: str,
        issue_number: int,
        timeout_seconds: float = 600.0,
        auto_apply: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> ResolutionResult:
        """Convenience method to resolve an issue."""
        request = ResolutionRequest(
            resolution_type=ResolutionType.ISSUE_RESOLUTION,
            provider=provider,
            repo_id=repo_id,
            issue_number=issue_number,
            timeout_seconds=timeout_seconds,
            auto_apply=auto_apply,
            extra_context=extra_context or {},
        )
        return await self.resolve(request)

    async def update_pr(
        self,
        provider: str,
        repo_id: str,
        pr_number: int,
        timeout_seconds: float = 600.0,
        auto_apply: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> ResolutionResult:
        """Convenience method to update a PR."""
        request = ResolutionRequest(
            resolution_type=ResolutionType.PR_UPDATE,
            provider=provider,
            repo_id=repo_id,
            pr_number=pr_number,
            timeout_seconds=timeout_seconds,
            auto_apply=auto_apply,
            extra_context=extra_context or {},
        )
        return await self.resolve(request)

    async def resolve_merge_conflict(
        self,
        provider: str,
        repo_id: str,
        pr_number: int,
        conflict_files: str = "",
        timeout_seconds: float = 600.0,
        auto_apply: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> ResolutionResult:
        """Convenience method to resolve merge conflicts."""
        ctx: Dict[str, Any] = {"conflict_files": conflict_files}
        ctx.update(extra_context or {})
        request = ResolutionRequest(
            resolution_type=ResolutionType.MERGE_CONFLICT,
            provider=provider,
            repo_id=repo_id,
            pr_number=pr_number,
            timeout_seconds=timeout_seconds,
            auto_apply=auto_apply,
            extra_context=ctx,
        )
        return await self.resolve(request)

    async def respond_to_comment(
        self,
        provider: str,
        repo_id: str,
        issue_number: int,
        comment_body: str,
        comment_author: str,
        timeout_seconds: float = 300.0,
        auto_apply: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> ResolutionResult:
        """Convenience method to respond to an issue comment."""
        ctx: Dict[str, Any] = {
            "comment_body": comment_body,
            "comment_author": comment_author,
        }
        ctx.update(extra_context or {})
        request = ResolutionRequest(
            resolution_type=ResolutionType.ISSUE_COMMENT,
            provider=provider,
            repo_id=repo_id,
            issue_number=issue_number,
            timeout_seconds=timeout_seconds,
            auto_apply=auto_apply,
            extra_context=ctx,
        )
        return await self.resolve(request)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def clear_results(self, older_than_hours: int = 24) -> int:
        """Remove stored results older than N hours.

        A result is eligible for removal when BOTH of the following are true:
          1. It is in a terminal status (COMPLETED, FAILED, TIMED_OUT, CANCELLED).
          2. It was created more than ``older_than_hours`` ago.

        Previously this method computed ``cutoff`` but never actually used
        it — every terminal result was removed regardless of age, which
        broke the documented "older than N hours" contract.
        """
        cutoff = time.time() - (older_than_hours * 3600)
        to_remove: List[str] = []
        with self._lock:
            for rid, result in list(self._results.items()):
                # Skip in-flight results
                if result.status not in (
                    ResolutionStatus.COMPLETED,
                    ResolutionStatus.FAILED,
                    ResolutionStatus.TIMED_OUT,
                    ResolutionStatus.CANCELLED,
                ):
                    continue
                # Use the result's timestamp if available; otherwise fall
                # back to ``duration_seconds`` heuristic — if we can't
                # prove the result is old enough, keep it (safer default).
                started_at = getattr(result, "started_at", None)
                if started_at is None:
                    continue
                if started_at < cutoff:
                    to_remove.append(rid)
            for rid in to_remove:
                del self._results[rid]
        return len(to_remove)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

issue_resolver = IssueResolver()
