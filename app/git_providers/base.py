from __future__ import annotations

"""
GitProviderService Abstract Base Class
=======================================
The contract that every git provider integration must fulfil.
All methods have both synchronous and asynchronous variants;
the default async implementations simply wrap the sync ones
via ``asyncio.to_thread`` so that concrete providers only need
to implement the sync path if they do not have native async SDKs.
"""

import asyncio
import functools
import time
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.logger import logger
from app.exceptions import RetryableError, NonRetryableError

from .models import (
    AuthInfo,
    Branch,
    Comment,
    FileContent,
    Issue,
    PullRequest,
    RateLimitInfo,
    Repository,
    SuggestedTask,
    WebhookConfig,
)

T = TypeVar("T")

# ──────────────────────────────────────────────────────────────────────────────
# Retry decorator with exponential back-off
# ──────────────────────────────────────────────────────────────────────────────


def _retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple = (RetryableError, ConnectionError, TimeoutError, OSError),
) -> Callable:
    """
    Decorator that retries a function with exponential back-off.

    Only retries on exceptions that are considered transient
    (network errors, rate-limit errors, etc.).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    # Honor RetryableError.wait_s when available
                    if isinstance(exc, RetryableError) and exc.wait_s > 0:
                        delay = max(delay, exc.wait_s)
                    logger.warning(
                        "git_providers.retry attempt=%d/%d delay=%.1fs func=%s err=%s",
                        attempt + 1,
                        max_retries,
                        delay,
                        func.__qualname__,
                        exc,
                    )
                    time.sleep(delay)
                except NonRetryableError:
                    raise
            raise last_exc  # type: ignore[misc]

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    if isinstance(exc, RetryableError) and exc.wait_s > 0:
                        delay = max(delay, exc.wait_s)
                    logger.warning(
                        "git_providers.async_retry attempt=%d/%d delay=%.1fs func=%s err=%s",
                        attempt + 1,
                        max_retries,
                        delay,
                        func.__qualname__,
                        exc,
                    )
                    await asyncio.sleep(delay)
                except NonRetryableError:
                    raise
            raise last_exc  # type: ignore[misc]

        import asyncio as _asyncio

        if _asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper

    return decorator


# ──────────────────────────────────────────────────────────────────────────────
# ABC
# ──────────────────────────────────────────────────────────────────────────────


class GitProviderService(ABC):
    """
    Abstract base class for all git provider integrations.

    Concrete implementations must override every ``_impl`` method.  The
    public methods (without ``_impl``) wrap the implementation with
    retry/back-off, rate-limit checks, thread-safety, and logging.

    Thread-safety is ensured through a per-instance ``_lock`` (reentrant
    so that internal methods may call each other without deadlocking).
    """

    provider_name: str = "base"

    # ── construction ──────────────────────────────────────────────────────

    def __init__(self, auth_info: AuthInfo) -> None:
        self.auth_info = auth_info
        self._lock = threading.RLock()
        self._rate_limit_info: RateLimitInfo = RateLimitInfo()
        self._last_request_at: float = 0.0
        self._min_interval: float = 0.1  # 100 ms between requests by default

    # ── rate-limit helpers ────────────────────────────────────────────────

    def _enforce_rate_limit(self) -> None:
        """Block until the minimum inter-request interval has elapsed."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    async def _enforce_rate_limit_async(self) -> None:
        """Async version of rate-limit enforcement."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def update_rate_limit(self, info: RateLimitInfo) -> None:
        """Update internal rate-limit state from provider response headers."""
        with self._lock:
            self._rate_limit_info = info
        if info.is_limited:
            logger.warning(
                "git_providers.rate_limit provider=%s remaining=%d limit=%d",
                self.provider_name,
                info.remaining,
                info.limit,
            )

    @property
    def rate_limit_info(self) -> RateLimitInfo:
        with self._lock:
            return self._rate_limit_info

    # ── repository operations ─────────────────────────────────────────────

    @abstractmethod
    def get_repos_impl(
        self,
        owner: Optional[str] = None,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Repository]:
        """List repositories accessible to the authenticated user."""
        ...

    def get_repos(
        self,
        owner: Optional[str] = None,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Repository]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_repos_impl(owner, page=page, per_page=per_page, **kwargs)

    async def get_repos_async(
        self,
        owner: Optional[str] = None,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Repository]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.get_repos, owner, page=page, per_page=per_page, **kwargs
        )

    # ──

    @abstractmethod
    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
        """Fetch a single repository by its full name (owner/repo)."""
        ...

    def get_repo(self, repo_id: str, **kwargs: Any) -> Repository:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_repo_impl(repo_id, **kwargs)

    async def get_repo_async(self, repo_id: str, **kwargs: Any) -> Repository:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.get_repo, repo_id, **kwargs)

    # ── branch operations ─────────────────────────────────────────────────

    @abstractmethod
    def get_branches_impl(
        self,
        repo_id: str,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Branch]:
        ...

    def get_branches(
        self,
        repo_id: str,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Branch]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_branches_impl(repo_id, page=page, per_page=per_page, **kwargs)

    async def get_branches_async(
        self,
        repo_id: str,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Branch]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.get_branches, repo_id, page=page, per_page=per_page, **kwargs
        )

    # ── pull-request operations ───────────────────────────────────────────

    @abstractmethod
    def get_pull_requests_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[PullRequest]:
        ...

    def get_pull_requests(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[PullRequest]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_pull_requests_impl(
                repo_id, state=state, page=page, per_page=per_page, **kwargs
            )

    async def get_pull_requests_async(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[PullRequest]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.get_pull_requests,
            repo_id,
            state=state,
            page=page,
            per_page=per_page,
            **kwargs,
        )

    # ──

    @abstractmethod
    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        ...

    def get_pr(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_pr_impl(repo_id, pr_number, **kwargs)

    async def get_pr_async(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.get_pr, repo_id, pr_number, **kwargs)

    # ──

    @abstractmethod
    def create_pr_impl(
        self,
        repo_id: str,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
        *,
        draft: bool = False,
        **kwargs: Any,
    ) -> PullRequest:
        ...

    def create_pr(
        self,
        repo_id: str,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
        *,
        draft: bool = False,
        **kwargs: Any,
    ) -> PullRequest:
        with self._lock:
            self._enforce_rate_limit()
            return self.create_pr_impl(
                repo_id,
                title,
                body,
                source_branch,
                target_branch,
                draft=draft,
                **kwargs,
            )

    async def create_pr_async(
        self,
        repo_id: str,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
        *,
        draft: bool = False,
        **kwargs: Any,
    ) -> PullRequest:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.create_pr,
            repo_id,
            title,
            body,
            source_branch,
            target_branch,
            draft=draft,
            **kwargs,
        )

    # ── issue operations ──────────────────────────────────────────────────

    @abstractmethod
    def get_issues_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Issue]:
        ...

    def get_issues(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Issue]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_issues_impl(
                repo_id, state=state, page=page, per_page=per_page, **kwargs
            )

    async def get_issues_async(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Issue]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.get_issues, repo_id, state=state, page=page, per_page=per_page, **kwargs
        )

    # ──

    @abstractmethod
    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        ...

    def get_issue(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_issue_impl(repo_id, issue_number, **kwargs)

    async def get_issue_async(
        self, repo_id: str, issue_number: int, **kwargs: Any
    ) -> Issue:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.get_issue, repo_id, issue_number, **kwargs)

    # ──

    @abstractmethod
    def create_issue_impl(
        self,
        repo_id: str,
        title: str,
        body: str = "",
        *,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Issue:
        ...

    def create_issue(
        self,
        repo_id: str,
        title: str,
        body: str = "",
        *,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Issue:
        with self._lock:
            self._enforce_rate_limit()
            return self.create_issue_impl(
                repo_id, title, body, labels=labels, assignees=assignees, **kwargs
            )

    async def create_issue_async(
        self,
        repo_id: str,
        title: str,
        body: str = "",
        *,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Issue:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.create_issue,
            repo_id,
            title,
            body,
            labels=labels,
            assignees=assignees,
            **kwargs,
        )

    # ── comment operations ────────────────────────────────────────────────

    @abstractmethod
    def comment_on_issue_impl(
        self,
        repo_id: str,
        issue_number: int,
        body: str,
        **kwargs: Any,
    ) -> Comment:
        ...

    def comment_on_issue(
        self,
        repo_id: str,
        issue_number: int,
        body: str,
        **kwargs: Any,
    ) -> Comment:
        with self._lock:
            self._enforce_rate_limit()
            return self.comment_on_issue_impl(repo_id, issue_number, body, **kwargs)

    async def comment_on_issue_async(
        self,
        repo_id: str,
        issue_number: int,
        body: str,
        **kwargs: Any,
    ) -> Comment:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.comment_on_issue, repo_id, issue_number, body, **kwargs
        )

    # ── code search ───────────────────────────────────────────────────────

    @abstractmethod
    def search_code_impl(
        self,
        query: str,
        *,
        repo: Optional[str] = None,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[FileContent]:
        ...

    def search_code(
        self,
        query: str,
        *,
        repo: Optional[str] = None,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[FileContent]:
        with self._lock:
            self._enforce_rate_limit()
            return self.search_code_impl(
                query, repo=repo, page=page, per_page=per_page, **kwargs
            )

    async def search_code_async(
        self,
        query: str,
        *,
        repo: Optional[str] = None,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[FileContent]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.search_code, query, repo=repo, page=page, per_page=per_page, **kwargs
        )

    # ── file content ──────────────────────────────────────────────────────

    @abstractmethod
    def get_file_content_impl(
        self,
        repo_id: str,
        path: str,
        *,
        ref: Optional[str] = None,
        **kwargs: Any,
    ) -> FileContent:
        ...

    def get_file_content(
        self,
        repo_id: str,
        path: str,
        *,
        ref: Optional[str] = None,
        **kwargs: Any,
    ) -> FileContent:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_file_content_impl(repo_id, path, ref=ref, **kwargs)

    async def get_file_content_async(
        self,
        repo_id: str,
        path: str,
        *,
        ref: Optional[str] = None,
        **kwargs: Any,
    ) -> FileContent:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(
            self.get_file_content, repo_id, path, ref=ref, **kwargs
        )

    # ── suggested tasks ───────────────────────────────────────────────────

    @abstractmethod
    def get_suggested_tasks_impl(
        self,
        repo_id: str,
        **kwargs: Any,
    ) -> List[SuggestedTask]:
        """
        Analyse repository state and return actionable tasks.

        Typical task sources:
        * OPEN_ISSUE      — unassigned or recently-created open issues
        * FAILING_CHECKS  — PRs with failed CI checks
        * MERGE_CONFLICT  — PRs with merge conflicts
        * UNRESOLVED_COMMENTS — PRs with unresolved review comments
        """
        ...

    def get_suggested_tasks(
        self,
        repo_id: str,
        **kwargs: Any,
    ) -> List[SuggestedTask]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_suggested_tasks_impl(repo_id, **kwargs)

    async def get_suggested_tasks_async(
        self,
        repo_id: str,
        **kwargs: Any,
    ) -> List[SuggestedTask]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.get_suggested_tasks, repo_id, **kwargs)

    # ── OAuth flow ────────────────────────────────────────────────────────

    @abstractmethod
    def get_auth_url_impl(self, state: Optional[str] = None, **kwargs: Any) -> str:
        """Return the URL to redirect the user to for OAuth authorisation."""
        ...

    def get_auth_url(self, state: Optional[str] = None, **kwargs: Any) -> str:
        return self.get_auth_url_impl(state=state, **kwargs)

    async def get_auth_url_async(self, state: Optional[str] = None, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.get_auth_url, state=state, **kwargs)

    # ──

    @abstractmethod
    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        """Exchange an OAuth callback code for access credentials."""
        ...

    def handle_callback(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        with self._lock:
            return self.handle_callback_impl(code, state=state, **kwargs)

    async def handle_callback_async(
        self, code: str, state: str = "", **kwargs: Any
    ) -> AuthInfo:
        return await asyncio.to_thread(self.handle_callback, code, state=state, **kwargs)

    # ── webhooks ──────────────────────────────────────────────────────────

    @abstractmethod
    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        """Register a new webhook on the given repository."""
        ...

    def register_webhook(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        with self._lock:
            self._enforce_rate_limit()
            return self.register_webhook_impl(repo_id, config, **kwargs)

    async def register_webhook_async(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.register_webhook, repo_id, config, **kwargs)

    # ──

    @abstractmethod
    def delete_webhook_impl(
        self, repo_id: str, webhook_id: str, **kwargs: Any
    ) -> bool:
        """Remove a previously registered webhook."""
        ...

    def delete_webhook(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        with self._lock:
            self._enforce_rate_limit()
            return self.delete_webhook_impl(repo_id, webhook_id, **kwargs)

    async def delete_webhook_async(
        self, repo_id: str, webhook_id: str, **kwargs: Any
    ) -> bool:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.delete_webhook, repo_id, webhook_id, **kwargs)

    # ── utility ───────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """Check whether authentication info is sufficient to make requests."""
        return self.auth_info.is_configured

    @abstractmethod
    def validate_token_impl(self) -> bool:
        """Verify that the current token is still valid by making a lightweight API call."""
        ...

    def validate_token(self) -> bool:
        with self._lock:
            try:
                return self.validate_token_impl()
            except Exception as exc:
                logger.warning(
                    "git_providers.validate_token provider=%s err=%s",
                    self.provider_name,
                    exc,
                )
                return False

    async def validate_token_async(self) -> bool:
        return await asyncio.to_thread(self.validate_token)

    @abstractmethod
    def get_authenticated_user_impl(self) -> Dict[str, Any]:
        """Return profile information for the authenticated user."""
        ...

    def get_authenticated_user(self) -> Dict[str, Any]:
        with self._lock:
            self._enforce_rate_limit()
            return self.get_authenticated_user_impl()

    async def get_authenticated_user_async(self) -> Dict[str, Any]:
        await self._enforce_rate_limit_async()
        return await asyncio.to_thread(self.get_authenticated_user)
