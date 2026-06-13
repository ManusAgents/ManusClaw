from __future__ import annotations

"""
GitHub Git Provider Service
============================
Concrete implementation of :class:`GitProviderService` for GitHub.

Uses **PyGithub** when available; falls back to direct REST API calls
via ``urllib`` when the library is not installed.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from app.exceptions import NonRetryableError, RetryableError
from app.logger import logger

from ..base import GitProviderService
from ..models import (
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

# ── optional PyGithub import ──────────────────────────────────────────────

try:
    from github import Github, GithubException
    from github.Auth import Token as GithubToken
    from github.Auth import AppAuthToken as GithubAppToken
    from github.GithubException import RateLimitExceededException

    _HAS_PYGITHUB = True
except ImportError:
    _HAS_PYGITHUB = False

# ── helpers ───────────────────────────────────────────────────────────────

_GITHUB_API = "https://api.github.com"
_GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_OAUTH_TOKEN = "https://github.com/login/oauth/access_token"


def _parse_iso(val: Any) -> str:
    """Safely coerce a datetime or string to ISO-format string."""
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


class GitHubService(GitProviderService):
    """GitHub provider backed by PyGithub (preferred) or raw REST API."""

    provider_name = "github"

    def __init__(self, auth_info: AuthInfo) -> None:
        super().__init__(auth_info)
        self._client: Any = None
        self._base_url: str = auth_info.extra.get("base_url", _GITHUB_API)
        self._init_client()

    # ── client initialisation ─────────────────────────────────────────────

    def _init_client(self) -> None:
        """Set up the PyGithub client if the library is present."""
        if not _HAS_PYGITHUB:
            logger.debug("git_providers.github PyGithub not available, using REST fallback")
            self._client = None
            return

        try:
            if self.auth_info.auth_type == AuthType.APP_TOKEN and self.auth_info.token:
                auth = GithubAppToken(self.auth_info.token)
                self._client = Github(
                    auth=auth,
                    base_url=self._base_url,
                )
            elif self.auth_info.token:
                auth = GithubToken(self.auth_info.token)
                self._client = Github(
                    auth=auth,
                    base_url=self._base_url,
                )
            else:
                self._client = Github(base_url=self._base_url)
        except Exception as exc:
            logger.warning("git_providers.github init_failed err=%s", exc)
            self._client = None

    # ── REST fallback helpers ─────────────────────────────────────────────

    def _rest_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Make an authenticated REST request to the GitHub API.

        Returns the parsed JSON body (dict or list).  Raises
        ``RetryableError`` on 429 / 5xx, ``NonRetryableError`` on 4xx.
        """
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: Dict[str, str] = {
            "Accept": "application/vnd.github+json",
        }
        if self.auth_info.token:
            headers["Authorization"] = f"Bearer {self.auth_info.token}"

        data = json.dumps(body).encode("utf-8") if body else None
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                rate_remaining = resp.headers.get("X-RateLimit-Remaining")
                rate_limit = resp.headers.get("X-RateLimit-Limit")
                rate_reset = resp.headers.get("X-RateLimit-Reset")
                if rate_remaining is not None:
                    self.update_rate_limit(
                        RateLimitInfo(
                            remaining=int(rate_remaining),
                            limit=int(rate_limit or 0),
                            reset_at=float(rate_reset) if rate_reset else None,
                            used=int(rate_limit or 0) - int(rate_remaining),
                        )
                    )
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                reset = exc.headers.get("X-RateLimit-Reset")
                wait = max(1.0, float(reset) - time.time()) if reset else 5.0
                raise RetryableError(
                    f"GitHub rate limit exceeded: {exc.reason}", wait_s=wait
                )
            if exc.code >= 500:
                raise RetryableError(
                    f"GitHub server error {exc.code}: {exc.reason}", wait_s=2.0
                )
            if 400 <= exc.code < 500:
                raise NonRetryableError(
                    f"GitHub client error {exc.code}: {exc.reason}"
                )
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"GitHub network error: {exc}", wait_s=1.0)

    # ── mapping helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_repo(raw: Any) -> Repository:
        """Map a PyGithub Repository or raw dict into our canonical model."""
        if isinstance(raw, dict):
            return Repository(
                id=str(raw.get("id", "")),
                name=raw.get("name", ""),
                full_name=raw.get("full_name", ""),
                url=raw.get("html_url", ""),
                clone_url=raw.get("clone_url", ""),
                ssh_url=raw.get("ssh_url", ""),
                description=raw.get("description", "") or "",
                default_branch=raw.get("default_branch", "main"),
                is_private=raw.get("private", False),
                is_fork=raw.get("fork", False),
                owner=raw.get("owner", {}).get("login", ""),
                language=raw.get("language", "") or "",
                stars=raw.get("stargazers_count", 0),
                forks=raw.get("forks_count", 0),
                open_issues_count=raw.get("open_issues_count", 0),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
                provider="github",
            )
        # PyGithub object
        return Repository(
            id=str(raw.id),
            name=raw.name,
            full_name=raw.full_name,
            url=raw.html_url,
            clone_url=raw.clone_url,
            ssh_url=raw.ssh_url,
            description=raw.description or "",
            default_branch=raw.default_branch or "main",
            is_private=raw.private,
            is_fork=raw.fork,
            owner=raw.owner.login if raw.owner else "",
            language=raw.language or "",
            stars=raw.stargazers_count,
            forks=raw.forks_count,
            open_issues_count=raw.open_issues_count,
            created_at=_parse_iso(raw.created_at),
            updated_at=_parse_iso(raw.updated_at),
            provider="github",
        )

    @staticmethod
    def _map_pr(raw: Any) -> PullRequest:
        if isinstance(raw, dict):
            state = PRStatus.DRAFT if raw.get("draft") else (
                PRStatus.MERGED if raw.get("merged") else
                PRStatus.OPEN if raw.get("state") == "open" else PRStatus.CLOSED
            )
            return PullRequest(
                id=str(raw.get("id", "")),
                number=raw.get("number", 0),
                title=raw.get("title", ""),
                body=raw.get("body", "") or "",
                state=state,
                source_branch=raw.get("head", {}).get("ref", ""),
                target_branch=raw.get("base", {}).get("ref", ""),
                author=raw.get("user", {}).get("login", ""),
                assignees=[a.get("login", "") for a in raw.get("assignees", [])],
                labels=[l.get("name", "") for l in raw.get("labels", [])],
                reviewers=[r.get("login", "") for r in raw.get("requested_reviewers", [])],
                url=raw.get("html_url", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
                merged_at=raw.get("merged_at"),
                draft=raw.get("draft", False),
                mergeable=raw.get("mergeable"),
                merge_commit_sha=raw.get("merge_commit_sha"),
                additions=raw.get("additions", 0),
                deletions=raw.get("deletions", 0),
                changed_files=raw.get("changed_files", 0),
                provider="github",
            )
        state = PRStatus.DRAFT if getattr(raw, "draft", False) else (
            PRStatus.MERGED if getattr(raw, "merged", False) else
            PRStatus.OPEN if raw.state == "open" else PRStatus.CLOSED
        )
        return PullRequest(
            id=str(raw.id),
            number=raw.number,
            title=raw.title,
            body=raw.body or "",
            state=state,
            source_branch=raw.head.ref if raw.head else "",
            target_branch=raw.base.ref if raw.base else "",
            author=raw.user.login if raw.user else "",
            assignees=[a.login for a in raw.assignees] if raw.assignees else [],
            labels=[l.name for l in raw.labels] if raw.labels else [],
            url=raw.html_url,
            created_at=_parse_iso(raw.created_at),
            updated_at=_parse_iso(raw.updated_at),
            merged_at=_parse_iso(raw.merged_at) if raw.merged_at else None,
            draft=getattr(raw, "draft", False),
            mergeable=raw.mergeable,
            merge_commit_sha=raw.merge_commit_sha,
            additions=getattr(raw, "additions", 0),
            deletions=getattr(raw, "deletions", 0),
            changed_files=getattr(raw, "changed_files", 0),
            provider="github",
        )

    @staticmethod
    def _map_issue(raw: Any) -> Issue:
        # Skip pull requests that appear in the issues endpoint
        if isinstance(raw, dict):
            if "pull_request" in raw:
                raise ValueError("Not an issue (is a PR)")
            return Issue(
                id=str(raw.get("id", "")),
                number=raw.get("number", 0),
                title=raw.get("title", ""),
                body=raw.get("body", "") or "",
                state=IssueStatus.OPEN if raw.get("state") == "open" else IssueStatus.CLOSED,
                author=raw.get("user", {}).get("login", ""),
                assignees=[a.get("login", "") for a in raw.get("assignees", [])],
                labels=[l.get("name", "") for l in raw.get("labels", [])],
                url=raw.get("html_url", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
                closed_at=raw.get("closed_at"),
                milestone=raw.get("milestone", {}).get("title", "") if raw.get("milestone") else "",
                comment_count=raw.get("comments", 0),
                provider="github",
            )
        if hasattr(raw, "pull_request") and raw.pull_request is not None:
            raise ValueError("Not an issue (is a PR)")
        return Issue(
            id=str(raw.id),
            number=raw.number,
            title=raw.title,
            body=raw.body or "",
            state=IssueStatus.OPEN if raw.state == "open" else IssueStatus.CLOSED,
            author=raw.user.login if raw.user else "",
            assignees=[a.login for a in raw.assignees] if raw.assignees else [],
            labels=[l.name for l in raw.labels] if raw.labels else [],
            url=raw.html_url,
            created_at=_parse_iso(raw.created_at),
            updated_at=_parse_iso(raw.updated_at),
            closed_at=_parse_iso(raw.closed_at) if raw.closed_at else None,
            milestone=raw.milestone.title if raw.milestone else "",
            comment_count=raw.comments,
            provider="github",
        )

    # ── repository operations ─────────────────────────────────────────────

    def get_repos_impl(
        self,
        owner: Optional[str] = None,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Repository]:
        if self._client is not None:
            try:
                if owner:
                    repos = self._client.get_organization(owner).get_repos(
                        sort="updated", direction="desc"
                    )
                else:
                    repos = self._client.get_user().get_repos(
                        sort="updated", direction="desc"
                    )
                result = []
                for i, r in enumerate(repos):
                    if i >= per_page * page:
                        break
                    if i >= per_page * (page - 1):
                        result.append(self._map_repo(r))
                return result
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        # REST fallback
        params: Dict[str, str] = {"page": str(page), "per_page": str(per_page)}
        path = f"/orgs/{owner}/repos" if owner else "/user/repos"
        data = self._rest_request(path, params=params)
        return [self._map_repo(r) for r in data]

    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
        if self._client is not None:
            try:
                return self._map_repo(self._client.get_repo(repo_id))
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(f"/repos/{repo_id}")
        return self._map_repo(data)

    # ── branch operations ─────────────────────────────────────────────────

    def get_branches_impl(
        self,
        repo_id: str,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Branch]:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                default = repo.default_branch
                result: List[Branch] = []
                for i, b in enumerate(repo.get_branches()):
                    if i >= per_page * page:
                        break
                    if i >= per_page * (page - 1):
                        result.append(
                            Branch(
                                name=b.name,
                                commit_sha=b.commit.sha,
                                is_default=b.name == default,
                                is_protected=b.protected,
                            )
                        )
                return result
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(
            f"/repos/{repo_id}/branches",
            params={"page": str(page), "per_page": str(per_page)},
        )
        default = kwargs.get("default_branch", "main")
        return [
            Branch(
                name=b.get("name", ""),
                commit_sha=b.get("commit", {}).get("sha", ""),
                is_default=b.get("name") == default,
                is_protected=b.get("protected", False),
            )
            for b in data
        ]

    # ── pull request operations ───────────────────────────────────────────

    def get_pull_requests_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[PullRequest]:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                pulls = repo.get_pulls(state=state, sort="updated", direction="desc")
                result: List[PullRequest] = []
                for i, p in enumerate(pulls):
                    if i >= per_page * page:
                        break
                    if i >= per_page * (page - 1):
                        result.append(self._map_pr(p))
                return result
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(
            f"/repos/{repo_id}/pulls",
            params={"state": state, "page": str(page), "per_page": str(per_page)},
        )
        return [self._map_pr(p) for p in data]

    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                return self._map_pr(repo.get_pull(pr_number))
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(f"/repos/{repo_id}/pulls/{pr_number}")
        return self._map_pr(data)

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
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                pr = repo.create_pull(
                    title=title,
                    body=body,
                    head=source_branch,
                    base=target_branch,
                    draft=draft,
                )
                return self._map_pr(pr)
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        payload = {
            "title": title,
            "body": body,
            "head": source_branch,
            "base": target_branch,
            "draft": draft,
        }
        data = self._rest_request(
            f"/repos/{repo_id}/pulls", method="POST", body=payload
        )
        return self._map_pr(data)

    # ── issue operations ──────────────────────────────────────────────────

    def get_issues_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Issue]:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                issues = repo.get_issues(state=state, sort="updated", direction="desc")
                result: List[Issue] = []
                for i, iss in enumerate(issues):
                    if i >= per_page * page:
                        break
                    if i >= per_page * (page - 1):
                        try:
                            result.append(self._map_issue(iss))
                        except ValueError:
                            continue  # skip PRs
                return result
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(
            f"/repos/{repo_id}/issues",
            params={"state": state, "page": str(page), "per_page": str(per_page)},
        )
        result: List[Issue] = []
        for item in data:
            try:
                result.append(self._map_issue(item))
            except ValueError:
                continue
        return result

    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                return self._map_issue(repo.get_issue(issue_number))
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(f"/repos/{repo_id}/issues/{issue_number}")
        return self._map_issue(data)

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
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                issue = repo.create_issue(
                    title=title,
                    body=body,
                    labels=labels or [],
                    assignees=assignees or [],
                )
                return self._map_issue(issue)
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = self._rest_request(
            f"/repos/{repo_id}/issues", method="POST", body=payload
        )
        return self._map_issue(data)

    # ── comment operations ────────────────────────────────────────────────

    def comment_on_issue_impl(
        self,
        repo_id: str,
        issue_number: int,
        body: str,
        **kwargs: Any,
    ) -> Comment:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                issue = repo.get_issue(issue_number)
                comment = issue.create_comment(body)
                return Comment(
                    id=str(comment.id),
                    body=comment.body,
                    author=comment.user.login if comment.user else "",
                    created_at=_parse_iso(comment.created_at),
                    updated_at=_parse_iso(comment.updated_at),
                    url=comment.html_url,
                    provider="github",
                )
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(
            f"/repos/{repo_id}/issues/{issue_number}/comments",
            method="POST",
            body={"body": body},
        )
        return Comment(
            id=str(data.get("id", "")),
            body=data.get("body", ""),
            author=data.get("user", {}).get("login", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            url=data.get("html_url", ""),
            provider="github",
        )

    # ── code search ───────────────────────────────────────────────────────

    def search_code_impl(
        self,
        query: str,
        *,
        repo: Optional[str] = None,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[FileContent]:
        q = query
        if repo:
            q += f" repo:{repo}"

        if self._client is not None:
            try:
                results = self._client.search_code(q, sort="indexed")
                items: List[FileContent] = []
                for i, item in enumerate(results):
                    if i >= per_page:
                        break
                    items.append(
                        FileContent(
                            path=item.path,
                            content="",  # search results don't include content
                            sha=item.sha,
                            repository=item.repository.full_name if item.repository else repo or "",
                            provider="github",
                        )
                    )
                return items
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request(
            "/search/code",
            params={"q": q, "page": str(page), "per_page": str(per_page)},
        )
        items_raw = data.get("items", [])
        return [
            FileContent(
                path=item.get("path", ""),
                content="",
                sha=item.get("sha", ""),
                repository=item.get("repository", {}).get("full_name", repo or ""),
                provider="github",
            )
            for item in items_raw
        ]

    # ── file content ──────────────────────────────────────────────────────

    def get_file_content_impl(
        self,
        repo_id: str,
        path: str,
        *,
        ref: Optional[str] = None,
        **kwargs: Any,
    ) -> FileContent:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                content_file = repo.get_contents(path, ref=ref)
                # get_contents can return a list for directories
                if isinstance(content_file, list):
                    raise NonRetryableError(f"Path '{path}' is a directory, not a file")
                content = ""
                if content_file.content:
                    try:
                        content = base64.b64decode(content_file.content).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        content = content_file.content
                return FileContent(
                    path=content_file.path,
                    content=content,
                    encoding="base64",
                    sha=content_file.sha,
                    size=content_file.size,
                    is_binary=False,
                    provider="github",
                )
            except RateLimitExceededException as exc:
                raise RetryableError(str(exc), wait_s=60.0)
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        params: Dict[str, str] = {}
        if ref:
            params["ref"] = ref
        data = self._rest_request(f"/repos/{repo_id}/contents/{path}", params=params)
        if isinstance(data, list):
            raise NonRetryableError(f"Path '{path}' is a directory, not a file")
        content = ""
        if data.get("content"):
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                content = data["content"]
        return FileContent(
            path=data.get("path", path),
            content=content,
            encoding="base64",
            sha=data.get("sha", ""),
            size=data.get("size", 0),
            is_binary=False,
            provider="github",
        )

    # ── suggested tasks ───────────────────────────────────────────────────

    def get_suggested_tasks_impl(
        self,
        repo_id: str,
        **kwargs: Any,
    ) -> List[SuggestedTask]:
        tasks: List[SuggestedTask] = []

        # 1. Open issues
        try:
            issues = self.get_issues_impl(repo_id, state="open", per_page=10)
            for issue in issues:
                tasks.append(
                    SuggestedTask(
                        task_type=TaskType.OPEN_ISSUE,
                        title=f"Open issue #{issue.number}: {issue.title}",
                        description=issue.body[:200] if issue.body else "",
                        repository=repo_id,
                        url=issue.url,
                        priority=5 if not issue.assignees else 2,
                        metadata={"issue_number": issue.number},
                        provider="github",
                    )
                )
        except Exception as exc:
            logger.warning("git_providers.github suggested_tasks.issues err=%s", exc)

        # 2. Failing checks on PRs
        try:
            prs = self.get_pull_requests_impl(repo_id, state="open", per_page=10)
            for pr in prs:
                # Check CI status via commit status / check runs
                if self._client is not None:
                    try:
                        repo = self._client.get_repo(repo_id)
                        commit = repo.get_commit(pr.source_branch)
                        statuses = commit.get_statuses()
                        for status in statuses:
                            if status.state == "failure":
                                tasks.append(
                                    SuggestedTask(
                                        task_type=TaskType.FAILING_CHECKS,
                                        title=f"Failing check on PR #{pr.number}: {status.context}",
                                        description=status.description or "",
                                        repository=repo_id,
                                        url=pr.url,
                                        priority=8,
                                        metadata={"pr_number": pr.number, "context": status.context},
                                        provider="github",
                                    )
                                )
                                break
                    except Exception:
                        pass
                else:
                    # REST fallback for check runs
                    try:
                        check_data = self._rest_request(
                            f"/repos/{repo_id}/commits/{pr.source_branch}/check-runs",
                            params={"per_page": "5"},
                        )
                        for cr in check_data.get("check_runs", []):
                            if cr.get("conclusion") == "failure":
                                tasks.append(
                                    SuggestedTask(
                                        task_type=TaskType.FAILING_CHECKS,
                                        title=f"Failing check on PR #{pr.number}: {cr.get('name', 'unknown')}",
                                        description=cr.get("output", {}).get("title", ""),
                                        repository=repo_id,
                                        url=pr.url,
                                        priority=8,
                                        metadata={"pr_number": pr.number, "check_name": cr.get("name")},
                                        provider="github",
                                    )
                                )
                                break
                    except Exception:
                        pass

                # 3. Merge conflicts
                if pr.mergeable is False:
                    tasks.append(
                        SuggestedTask(
                            task_type=TaskType.MERGE_CONFLICT,
                            title=f"Merge conflict on PR #{pr.number}: {pr.title}",
                            description=f"Source: {pr.source_branch} → Target: {pr.target_branch}",
                            repository=repo_id,
                            url=pr.url,
                            priority=7,
                            metadata={"pr_number": pr.number},
                            provider="github",
                        )
                    )

                # 4. Unresolved comments
                try:
                    if self._client is not None:
                        pull = self._client.get_repo(repo_id).get_pull(pr.number)
                        comments = pull.get_review_comments()
                        for c in comments:
                            if not getattr(c, "in_reply_to_id", None):
                                # Top-level review comment that might be unresolved
                                tasks.append(
                                    SuggestedTask(
                                        task_type=TaskType.UNRESOLVED_COMMENTS,
                                        title=f"Unresolved comment on PR #{pr.number}",
                                        description=c.body[:150] if c.body else "",
                                        repository=repo_id,
                                        url=c.html_url,
                                        priority=4,
                                        metadata={"pr_number": pr.number, "comment_id": c.id},
                                        provider="github",
                                    )
                                )
                                break  # one per PR is enough for suggestion
                    else:
                        comments_data = self._rest_request(
                            f"/repos/{repo_id}/pulls/{pr.number}/comments",
                            params={"per_page": "3"},
                        )
                        for cd in comments_data:
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.UNRESOLVED_COMMENTS,
                                    title=f"Unresolved comment on PR #{pr.number}",
                                    description=(cd.get("body", "") or "")[:150],
                                    repository=repo_id,
                                    url=cd.get("html_url", pr.url),
                                    priority=4,
                                    metadata={"pr_number": pr.number, "comment_id": cd.get("id")},
                                    provider="github",
                                )
                            )
                            break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("git_providers.github suggested_tasks.prs err=%s", exc)

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # ── OAuth ─────────────────────────────────────────────────────────────

    def get_auth_url_impl(self, state: Optional[str] = None, **kwargs: Any) -> str:
        params = {
            "client_id": self.auth_info.client_id,
            "redirect_uri": self.auth_info.redirect_uri,
            "scope": " ".join(self.auth_info.scopes) if self.auth_info.scopes else "repo user",
        }
        if state:
            params["state"] = state
        return f"{_GITHUB_OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        payload = {
            "client_id": self.auth_info.client_id,
            "client_secret": self.auth_info.client_secret,
            "code": code,
            "redirect_uri": self.auth_info.redirect_uri,
        }
        headers = {"Accept": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _GITHUB_OAUTH_TOKEN, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise NonRetryableError(f"GitHub OAuth callback failed: {exc}")

        if "error" in result:
            raise NonRetryableError(
                f"GitHub OAuth error: {result.get('error_description', result['error'])}"
            )

        new_auth = AuthInfo(
            auth_type=AuthType.PERSONAL_ACCESS_TOKEN,
            token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
            scopes=result.get("scope", "").split(",") if result.get("scope") else [],
            expires_at=time.time() + result.get("expires_in", 0) if result.get("expires_in") else None,
            client_id=self.auth_info.client_id,
            client_secret=self.auth_info.client_secret,
            redirect_uri=self.auth_info.redirect_uri,
            extra=self.auth_info.extra,
        )
        # Re-initialise with new credentials
        self.auth_info = new_auth
        self._init_client()
        return new_auth

    # ── webhooks ──────────────────────────────────────────────────────────

    _WEBHOOK_EVENT_MAP: Dict[WebhookEvent, str] = {
        WebhookEvent.PUSH: "push",
        WebhookEvent.PULL_REQUEST: "pull_request",
        WebhookEvent.ISSUE: "issues",
        WebhookEvent.COMMENT: "issue_comment",
        WebhookEvent.CHECK_RUN: "check_run",
        WebhookEvent.RELEASE: "release",
        WebhookEvent.MEMBER: "member",
    }

    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        events = [
            self._WEBHOOK_EVENT_MAP.get(e, "push") for e in config.events
        ] or ["push"]
        payload = {
            "name": "web",
            "active": config.active,
            "events": events,
            "config": {
                "url": config.url,
                "content_type": "json",
                "secret": config.secret,
            },
        }

        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                hook = repo.create_hook(
                    name="web",
                    config={"url": config.url, "content_type": "json", "secret": config.secret},
                    events=events,
                    active=config.active,
                )
                config.webhook_id = str(hook.id)
                config.provider = "github"
                return config
            except GithubException as exc:
                raise NonRetryableError(f"GitHub webhook registration failed: {exc}")

        data = self._rest_request(
            f"/repos/{repo_id}/hooks", method="POST", body=payload
        )
        config.webhook_id = str(data.get("id", ""))
        config.provider = "github"
        return config

    def delete_webhook_impl(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        if self._client is not None:
            try:
                repo = self._client.get_repo(repo_id)
                hook = repo.get_hook(int(webhook_id))
                hook.delete()
                return True
            except GithubException as exc:
                raise NonRetryableError(f"GitHub webhook deletion failed: {exc}")

        try:
            self._rest_request(
                f"/repos/{repo_id}/hooks/{webhook_id}", method="DELETE"
            )
            return True
        except NonRetryableError:
            return False

    # ── utility ───────────────────────────────────────────────────────────

    def validate_token_impl(self) -> bool:
        try:
            if self._client is not None:
                self._client.get_user().login
                return True
            self._rest_request("/user")
            return True
        except Exception:
            return False

    def get_authenticated_user_impl(self) -> Dict[str, Any]:
        if self._client is not None:
            try:
                user = self._client.get_user()
                return {
                    "login": user.login,
                    "name": user.name or "",
                    "email": user.email or "",
                    "avatar_url": user.avatar_url,
                    "id": user.id,
                    "provider": "github",
                }
            except GithubException as exc:
                raise NonRetryableError(f"GitHub error: {exc}")

        data = self._rest_request("/user")
        return {
            "login": data.get("login", ""),
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "avatar_url": data.get("avatar_url", ""),
            "id": data.get("id"),
            "provider": "github",
        }
