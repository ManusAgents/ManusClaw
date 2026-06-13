from __future__ import annotations

"""
Git Provider Data Models
========================
Canonical data structures used across all git provider integrations.
Every provider maps its native API responses into these common models
so that downstream consumers remain provider-agnostic.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class TaskType(str, Enum):
    """Categories of actionable tasks surfaced from git repositories."""

    OPEN_ISSUE = "OPEN_ISSUE"
    FAILING_CHECKS = "FAILING_CHECKS"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    UNRESOLVED_COMMENTS = "UNRESOLVED_COMMENTS"


class PRStatus(str, Enum):
    """Pull / merge request lifecycle states."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"
    DRAFT = "DRAFT"


class IssueStatus(str, Enum):
    """Issue lifecycle states."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class AuthType(str, Enum):
    """Supported authentication mechanisms."""

    OAUTH = "OAUTH"
    PERSONAL_ACCESS_TOKEN = "PERSONAL_ACCESS_TOKEN"
    APP_TOKEN = "APP_TOKEN"
    SSH_KEY = "SSH_KEY"


class WebhookEvent(str, Enum):
    """Common webhook event types across providers."""

    PUSH = "PUSH"
    PULL_REQUEST = "PULL_REQUEST"
    ISSUE = "ISSUE"
    COMMENT = "COMMENT"
    CHECK_RUN = "CHECK_RUN"
    RELEASE = "RELEASE"
    MEMBER = "MEMBER"


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class AuthInfo:
    """Authentication credentials and metadata for a git provider."""

    auth_type: AuthType
    token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: List[str] = field(default_factory=list)
    expires_at: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Return True when the token has a known expiry that has passed."""
        if self.expires_at is None:
            return False
        import time

        return time.time() >= self.expires_at

    @property
    def is_configured(self) -> bool:
        """Return True when enough credentials are present to attempt auth."""
        if self.auth_type == AuthType.OAUTH:
            return bool(self.client_id and self.client_secret)
        return bool(self.token)


@dataclass
class Repository:
    """Normalised representation of a git repository."""

    id: str
    name: str
    full_name: str
    url: str
    clone_url: str = ""
    ssh_url: str = ""
    description: str = ""
    default_branch: str = "main"
    is_private: bool = False
    is_fork: bool = False
    owner: str = ""
    language: str = ""
    stars: int = 0
    forks: int = 0
    open_issues_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Branch:
    """Normalised representation of a git branch."""

    name: str
    commit_sha: str
    is_default: bool = False
    is_protected: bool = False
    created_at: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PullRequest:
    """Normalised representation of a pull / merge request."""

    id: str
    number: int
    title: str
    body: str = ""
    state: PRStatus = PRStatus.OPEN
    source_branch: str = ""
    target_branch: str = ""
    author: str = ""
    assignees: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    reviewers: List[str] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    merged_at: Optional[str] = None
    draft: bool = False
    mergeable: Optional[bool] = None
    merge_commit_sha: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Issue:
    """Normalised representation of a git issue."""

    id: str
    number: int
    title: str
    body: str = ""
    state: IssueStatus = IssueStatus.OPEN
    author: str = ""
    assignees: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    closed_at: Optional[str] = None
    milestone: str = ""
    comment_count: int = 0
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Comment:
    """Normalised representation of a comment on an issue or PR."""

    id: str
    body: str
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    parent_id: Optional[str] = None
    is_resolved: Optional[bool] = None
    url: str = ""
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FileContent:
    """Normalised representation of a file fetched from a repository."""

    path: str
    content: str
    encoding: str = "utf-8"
    sha: str = ""
    size: int = 0
    is_binary: bool = False
    commit_sha: str = ""
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SuggestedTask:
    """An actionable task surfaced from repository analysis."""

    task_type: TaskType
    title: str
    description: str = ""
    repository: str = ""
    url: str = ""
    priority: int = 0  # 0 = lowest, higher = more urgent
    metadata: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SuggestedTask):
            return NotImplemented
        return self.priority < other.priority

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SuggestedTask):
            return NotImplemented
        return (
            self.task_type == other.task_type
            and self.title == other.title
            and self.repository == other.repository
        )

    def __hash__(self) -> int:
        return hash((self.task_type, self.title, self.repository))


@dataclass
class WebhookConfig:
    """Configuration for registering a webhook on a repository."""

    url: str
    events: List[WebhookEvent] = field(default_factory=lambda: [WebhookEvent.PUSH])
    secret: str = ""
    active: bool = True
    webhook_id: Optional[str] = None
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitInfo:
    """Current rate-limit status reported by a provider."""

    remaining: int = 0
    limit: int = 0
    reset_at: Optional[float] = None
    used: int = 0

    @property
    def is_limited(self) -> bool:
        return self.remaining <= 0

    @property
    def usage_percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return (self.used / self.limit) * 100.0
