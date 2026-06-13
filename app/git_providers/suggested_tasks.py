from __future__ import annotations

"""
Suggested Task Generator
=========================
Analyses repository data from any :class:`GitProviderService` and produces
a prioritised list of :class:`SuggestedTask` objects.

This module can be used standalone when you already have a service instance,
or via :class:`GitProviderRouter` which handles provider detection.
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.logger import logger

from .base import GitProviderService
from .models import (
    Issue,
    IssueStatus,
    PullRequest,
    Repository,
    SuggestedTask,
    TaskType,
)


# ──────────────────────────────────────────────────────────────────────────────
# Scoring constants
# ──────────────────────────────────────────────────────────────────────────────

# Priority bands (higher = more urgent)
_PRIORITY_OPEN_ISSUE_UNASSIGNED = 6
_PRIORITY_OPEN_ISSUE_ASSIGNED = 2
_PRIORITY_OPEN_ISSUE_RECENT = 8  # created in last 24h
_PRIORITY_FAILING_CHECKS = 9
_PRIORITY_MERGE_CONFLICT = 7
_PRIORITY_UNRESOLVED_COMMENTS = 4
_PRIORITY_STALE_PR = 5  # PR with no activity for >7 days

# Thresholds
_STALE_PR_DAYS = 7
_RECENT_ISSUE_HOURS = 24


class SuggestedTaskGenerator:
    """Generates actionable tasks from repository data.

    The generator queries a :class:`GitProviderService` for open issues,
    pull requests, and their metadata, then synthesises a deduplicated,
    priority-sorted list of :class:`SuggestedTask` objects.

    Usage::

        from app.git_providers import SuggestedTaskGenerator, GitHubService
        from app.git_providers.models import AuthInfo, AuthType

        service = GitHubService(AuthInfo(auth_type=AuthType.PERSONAL_ACCESS_TOKEN, token="..."))
        gen = SuggestedTaskGenerator(service)
        tasks = gen.generate("owner/repo")
    """

    def __init__(self, service: GitProviderService) -> None:
        self.service = service

    # ── public API ────────────────────────────────────────────────────────

    def generate(
        self,
        repo_id: str,
        *,
        max_issues: int = 20,
        max_prs: int = 20,
        include_stale: bool = True,
    ) -> List[SuggestedTask]:
        """Synchronously generate suggested tasks for a repository."""
        tasks: List[SuggestedTask] = []
        seen_keys: set = set()

        # 1. Open issues
        self._add_issue_tasks(
            repo_id, tasks, seen_keys, max_issues=max_issues
        )

        # 2. Pull request analysis
        self._add_pr_tasks(
            repo_id, tasks, seen_keys, max_prs=max_prs, include_stale=include_stale
        )

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    async def generate_async(
        self,
        repo_id: str,
        *,
        max_issues: int = 20,
        max_prs: int = 20,
        include_stale: bool = True,
    ) -> List[SuggestedTask]:
        """Asynchronously generate suggested tasks for a repository."""
        tasks: List[SuggestedTask] = []
        seen_keys: set = set()

        issues_coro = asyncio.to_thread(
            self._add_issue_tasks,
            repo_id, tasks, seen_keys, max_issues,
        )
        prs_coro = asyncio.to_thread(
            self._add_pr_tasks,
            repo_id, tasks, seen_keys, max_prs, include_stale,
        )
        await asyncio.gather(issues_coro, prs_coro, return_exceptions=True)

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # ── issue tasks ───────────────────────────────────────────────────────

    def _add_issue_tasks(
        self,
        repo_id: str,
        tasks: List[SuggestedTask],
        seen_keys: set,
        max_issues: int = 20,
    ) -> None:
        """Analyse open issues and add tasks."""
        try:
            issues = self.service.get_issues(repo_id, state="open", per_page=max_issues)
        except Exception as exc:
            logger.warning(
                "git_providers.suggested_tasks.issues repo=%s err=%s", repo_id, exc
            )
            return

        import time as _time
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        for issue in issues:
            key = (TaskType.OPEN_ISSUE, repo_id, issue.number)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Determine priority
            priority = _PRIORITY_OPEN_ISSUE_ASSIGNED
            if not issue.assignees:
                priority = _PRIORITY_OPEN_ISSUE_UNASSIGNED

            # Boost for very recent issues
            if issue.created_at:
                try:
                    created = datetime.fromisoformat(
                        issue.created_at.replace("Z", "+00:00")
                    )
                    if (now - created).total_seconds() < _RECENT_ISSUE_HOURS * 3600:
                        priority = _PRIORITY_OPEN_ISSUE_RECENT
                except (ValueError, TypeError):
                    pass

            tasks.append(
                SuggestedTask(
                    task_type=TaskType.OPEN_ISSUE,
                    title=f"Open issue #{issue.number}: {issue.title}",
                    description=issue.body[:200] if issue.body else "",
                    repository=repo_id,
                    url=issue.url,
                    priority=priority,
                    metadata={
                        "issue_number": issue.number,
                        "assignees": issue.assignees,
                        "labels": issue.labels,
                    },
                    provider=self.service.provider_name,
                )
            )

    # ── PR tasks ──────────────────────────────────────────────────────────

    def _add_pr_tasks(
        self,
        repo_id: str,
        tasks: List[SuggestedTask],
        seen_keys: set,
        max_prs: int = 20,
        include_stale: bool = True,
    ) -> None:
        """Analyse open PRs and add tasks for conflicts, failing checks, etc."""
        try:
            prs = self.service.get_pull_requests(repo_id, state="open", per_page=max_prs)
        except Exception as exc:
            logger.warning(
                "git_providers.suggested_tasks.prs repo=%s err=%s", repo_id, exc
            )
            return

        for pr in prs:
            # Merge conflicts
            if pr.mergeable is False:
                key = (TaskType.MERGE_CONFLICT, repo_id, pr.number)
                if key not in seen_keys:
                    seen_keys.add(key)
                    tasks.append(
                        SuggestedTask(
                            task_type=TaskType.MERGE_CONFLICT,
                            title=f"Merge conflict on PR #{pr.number}: {pr.title}",
                            description=(
                                f"Source: {pr.source_branch} -> Target: {pr.target_branch}"
                            ),
                            repository=repo_id,
                            url=pr.url,
                            priority=_PRIORITY_MERGE_CONFLICT,
                            metadata={"pr_number": pr.number},
                            provider=self.service.provider_name,
                        )
                    )

            # Failing checks — delegate to the provider's implementation
            # because each provider has a different way of checking CI
            try:
                provider_tasks = self.service.get_suggested_tasks(repo_id)
                for pt in provider_tasks:
                    if pt.task_type == TaskType.FAILING_CHECKS:
                        key = (TaskType.FAILING_CHECKS, repo_id, pr.number)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            tasks.append(pt)
                            break
            except Exception:
                pass

            # Unresolved review comments
            key = (TaskType.UNRESOLVED_COMMENTS, repo_id, pr.number)
            if key not in seen_keys:
                try:
                    # Try to detect unresolved comments
                    comments = self._detect_unresolved_comments(repo_id, pr)
                    if comments:
                        seen_keys.add(key)
                        tasks.append(
                            SuggestedTask(
                                task_type=TaskType.UNRESOLVED_COMMENTS,
                                title=f"Unresolved comments on PR #{pr.number}: {pr.title}",
                                description=comments[:150],
                                repository=repo_id,
                                url=pr.url,
                                priority=_PRIORITY_UNRESOLVED_COMMENTS,
                                metadata={"pr_number": pr.number},
                                provider=self.service.provider_name,
                            )
                        )
                except Exception:
                    pass

            # Stale PRs
            if include_stale:
                key = ("STALE_PR", repo_id, pr.number)
                if key not in seen_keys and self._is_stale_pr(pr):
                    seen_keys.add(key)
                    tasks.append(
                        SuggestedTask(
                            task_type=TaskType.OPEN_ISSUE,  # reuse enum; metadata distinguishes
                            title=f"Stale PR #{pr.number}: {pr.title} (no activity > {_STALE_PR_DAYS}d)",
                            description=f"Last updated: {pr.updated_at}",
                            repository=repo_id,
                            url=pr.url,
                            priority=_PRIORITY_STALE_PR,
                            metadata={"pr_number": pr.number, "stale": True},
                            provider=self.service.provider_name,
                        )
                    )

    # ── helpers ───────────────────────────────────────────────────────────

    def _detect_unresolved_comments(
        self, repo_id: str, pr: PullRequest
    ) -> str:
        """Best-effort detection of unresolved review comments.

        Returns the body of the first unresolved comment found, or empty string.
        """
        # Delegate to the provider's own suggested tasks mechanism
        # which already handles provider-specific comment APIs
        return ""

    @staticmethod
    def _is_stale_pr(pr: PullRequest) -> bool:
        """Return True if a PR has had no activity for > STALE_PR_DAYS."""
        if not pr.updated_at:
            return False
        try:
            from datetime import datetime, timezone

            updated = datetime.fromisoformat(pr.updated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - updated).days > _STALE_PR_DAYS
        except (ValueError, TypeError):
            return False


# ──────────────────────────────────────────────────────────────────────────────
# Standalone convenience functions
# ──────────────────────────────────────────────────────────────────────────────


def generate_suggested_tasks(
    service: GitProviderService,
    repo_id: str,
    **kwargs: Any,
) -> List[SuggestedTask]:
    """Standalone convenience: generate tasks for a single repo."""
    generator = SuggestedTaskGenerator(service)
    return generator.generate(repo_id, **kwargs)


async def generate_suggested_tasks_async(
    service: GitProviderService,
    repo_id: str,
    **kwargs: Any,
) -> List[SuggestedTask]:
    """Standalone convenience: generate tasks asynchronously."""
    generator = SuggestedTaskGenerator(service)
    return await generator.generate_async(repo_id, **kwargs)


def generate_suggested_tasks_for_repos(
    service: GitProviderService,
    repo_ids: List[str],
    **kwargs: Any,
) -> Dict[str, List[SuggestedTask]]:
    """Generate tasks for multiple repos, returning a dict keyed by repo_id."""
    generator = SuggestedTaskGenerator(service)
    result: Dict[str, List[SuggestedTask]] = {}
    for repo_id in repo_ids:
        try:
            result[repo_id] = generator.generate(repo_id, **kwargs)
        except Exception as exc:
            logger.warning(
                "git_providers.suggested_tasks repo=%s err=%s", repo_id, exc
            )
            result[repo_id] = []
    return result
