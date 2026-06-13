from __future__ import annotations

"""
Git Provider Integrations
==========================
Enterprise-grade git provider integrations for the ManusClaw platform.

Supports: GitHub, GitLab, Azure DevOps, Bitbucket, Forgejo/Gitea.

Quick start::

    from app.git_providers import GitProviderRouter, AuthInfo, AuthType

    router = GitProviderRouter()
    service = router.get_service("github", AuthInfo(
        auth_type=AuthType.PERSONAL_ACCESS_TOKEN,
        token="ghp_xxx",
    ))
    repos = service.get_repos()
    tasks = service.get_suggested_tasks("owner/repo")
"""

# ── Core ABC and models (always available) ────────────────────────────────

from .base import GitProviderService
from .models import (
    AuthInfo,
    AuthType,
    Branch,
    Comment,
    FileContent,
    Issue,
    IssueStatus,
    PRStatus,
    PullRequest,
    RateLimitInfo,
    Repository,
    SuggestedTask,
    TaskType,
    WebhookConfig,
    WebhookEvent,
)

# ── Router (always available) ────────────────────────────────────────────

from .provider import GitProviderRouter

# ── Suggested task generation (always available) ──────────────────────────

from .suggested_tasks import (
    SuggestedTaskGenerator,
    generate_suggested_tasks,
    generate_suggested_tasks_async,
    generate_suggested_tasks_for_repos,
)

# ── Provider implementations (may fail if optional deps are missing) ──────

try:
    from .github.service import GitHubService
except Exception:
    GitHubService = None  # type: ignore[assignment,misc]

try:
    from .gitlab.service import GitLabService
except Exception:
    GitLabService = None  # type: ignore[assignment,misc]

try:
    from .azure_devops.service import AzureDevOpsService
except Exception:
    AzureDevOpsService = None  # type: ignore[assignment,misc]

try:
    from .bitbucket.service import BitbucketService
except Exception:
    BitbucketService = None  # type: ignore[assignment,misc]

try:
    from .forgejo.service import ForgejoService
except Exception:
    ForgejoService = None  # type: ignore[assignment,misc]

# ── Public API ────────────────────────────────────────────────────────────

__all__ = [
    # Base class
    "GitProviderService",
    # Models
    "AuthInfo",
    "AuthType",
    "Branch",
    "Comment",
    "FileContent",
    "Issue",
    "IssueStatus",
    "PRStatus",
    "PullRequest",
    "RateLimitInfo",
    "Repository",
    "SuggestedTask",
    "TaskType",
    "WebhookConfig",
    "WebhookEvent",
    # Router
    "GitProviderRouter",
    # Suggested tasks
    "SuggestedTaskGenerator",
    "generate_suggested_tasks",
    "generate_suggested_tasks_async",
    "generate_suggested_tasks_for_repos",
    # Provider implementations
    "GitHubService",
    "GitLabService",
    "AzureDevOpsService",
    "BitbucketService",
    "ForgejoService",
]
