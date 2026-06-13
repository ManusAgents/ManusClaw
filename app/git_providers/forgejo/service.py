from __future__ import annotations

"""
Forgejo Git Provider Service
==============================
Concrete implementation of :class:`GitProviderService` for Forgejo
(and Gitea, since Forgejo is API-compatible).

Uses the Forgejo/Gitea REST API via ``urllib``.
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

# ── constants ─────────────────────────────────────────────────────────────

_FORGEJO_OAUTH_AUTHORIZE = "/login/oauth/authorize"
_FORGEJO_OAUTH_TOKEN = "/login/oauth/access_token"


def _parse_iso(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


class ForgejoService(GitProviderService):
    """Forgejo / Gitea provider using the REST API (v1)."""

    provider_name = "forgejo"

    def __init__(self, auth_info: AuthInfo) -> None:
        super().__init__(auth_info)
        self._base_url: str = auth_info.extra.get(
            "base_url", "https://codeberg.org"
        ).rstrip("/")
        self._api_url = f"{self._base_url}/api/v1"

    # ── REST helpers ──────────────────────────────────────────────────────

    def _rest_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{self._api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.auth_info.token:
            # Forgejo supports both token and Authorization header
            if self.auth_info.auth_type == AuthType.OAUTH:
                headers["Authorization"] = f"Bearer {self.auth_info.token}"
            else:
                headers["Authorization"] = f"token {self.auth_info.token}"

        data = json.dumps(body).encode("utf-8") if body else None
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
                        )
                    )
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                reset = exc.headers.get("X-RateLimit-Reset")
                wait = max(1.0, float(reset) - time.time()) if reset else 5.0
                raise RetryableError(f"Forgejo rate limit: {exc.reason}", wait_s=wait)
            if exc.code >= 500:
                raise RetryableError(
                    f"Forgejo server error {exc.code}: {exc.reason}", wait_s=2.0
                )
            if 400 <= exc.code < 500:
                raise NonRetryableError(
                    f"Forgejo client error {exc.code}: {exc.reason}"
                )
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"Forgejo network error: {exc}", wait_s=1.0)

    # ── mapping helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_repo(raw: Dict[str, Any]) -> Repository:
        return Repository(
            id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            full_name=raw.get("full_name", ""),
            url=raw.get("html_url", ""),
            clone_url=raw.get("clone_url", ""),
            ssh_url=raw.get("ssh_url", ""),
            description=raw.get("description", "") or "",
            default_branch=raw.get("default_branch", "main") or "main",
            is_private=raw.get("private", False),
            is_fork=raw.get("fork", False),
            owner=raw.get("owner", {}).get("login", "") if isinstance(raw.get("owner"), dict) else "",
            language=raw.get("language", "") or "",
            stars=raw.get("stars_count", 0),
            forks=raw.get("forks_count", 0),
            open_issues_count=raw.get("open_issues_count", 0),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            provider="forgejo",
        )

    @staticmethod
    def _map_pr(raw: Dict[str, Any]) -> PullRequest:
        state_map = {
            "open": PRStatus.OPEN,
            "closed": PRStatus.CLOSED,
            "merged": PRStatus.MERGED,
        }
        state = state_map.get(raw.get("state", ""), PRStatus.OPEN)
        if raw.get("draft") and state == PRStatus.OPEN:
            state = PRStatus.DRAFT
        return PullRequest(
            id=str(raw.get("id", "")),
            number=raw.get("number", 0),
            title=raw.get("title", ""),
            body=raw.get("body", "") or "",
            state=state,
            source_branch=raw.get("head", {}).get("ref", raw.get("head", {}).get("label", "")),
            target_branch=raw.get("base", {}).get("ref", raw.get("base", {}).get("label", "")),
            author=raw.get("user", {}).get("login", ""),
            assignees=[a.get("login", "") for a in raw.get("assignees", [])],
            labels=[l.get("name", "") for l in raw.get("labels", [])],
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
            provider="forgejo",
        )

    @staticmethod
    def _map_issue(raw: Dict[str, Any]) -> Issue:
        # Skip pull requests that appear in the issues endpoint
        if raw.get("pull_request"):
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
            provider="forgejo",
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
        if owner:
            data = self._rest_request(
                f"/orgs/{owner}/repos",
                params={"page": str(page), "limit": str(per_page)},
            )
        else:
            data = self._rest_request(
                "/repos/search",
                params={"page": str(page), "limit": str(per_page), "sort": "updated"},
            )
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
        return [self._map_repo(r) for r in data]

    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
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
        data = self._rest_request(
            f"/repos/{repo_id}/branches",
            params={"page": str(page), "limit": str(per_page)},
        )
        repo = self.get_repo_impl(repo_id)
        return [
            Branch(
                name=b.get("name", ""),
                commit_sha=b.get("commit", {}).get("id", ""),
                is_default=b.get("name") == repo.default_branch,
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
        data = self._rest_request(
            f"/repos/{repo_id}/pulls",
            params={"state": state, "page": str(page), "limit": str(per_page), "sort": "updated"},
        )
        return [self._map_pr(p) for p in data]

    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
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
        payload = {
            "title": title,
            "body": body,
            "head": source_branch,
            "base": target_branch,
        }
        if draft:
            payload["draft"] = True
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
        data = self._rest_request(
            f"/repos/{repo_id}/issues",
            params={"state": state, "page": str(page), "limit": str(per_page), "type": "issues"},
        )
        result: List[Issue] = []
        for item in data:
            try:
                result.append(self._map_issue(item))
            except ValueError:
                continue
        return result

    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
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
        payload = {"body": body}
        data = self._rest_request(
            f"/repos/{repo_id}/issues/{issue_number}/comments",
            method="POST",
            body=payload,
        )
        return Comment(
            id=str(data.get("id", "")),
            body=data.get("body", body),
            author=data.get("user", {}).get("login", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            url=data.get("html_url", ""),
            provider="forgejo",
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
        params: Dict[str, str] = {
            "q": query,
            "page": str(page),
            "limit": str(per_page),
        }
        if repo:
            params["repo"] = repo

        data = self._rest_request("/repos/search", params=params)
        # Forgejo code search is repo-level; use grep if available
        # For now, return search results from the repository index
        items = data.get("data", data) if isinstance(data, dict) else data
        return [
            FileContent(
                path=item.get("path", ""),
                content="",
                repository=item.get("full_name", repo or ""),
                provider="forgejo",
            )
            for item in items
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
        params: Dict[str, str] = {}
        if ref:
            params["ref"] = ref

        data = self._rest_request(
            f"/repos/{repo_id}/contents/{path}", params=params
        )
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
            provider="forgejo",
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
                        provider="forgejo",
                    )
                )
        except Exception as exc:
            logger.warning("git_providers.forgejo suggested_tasks.issues err=%s", exc)

        # 2. PRs with failing checks and conflicts
        try:
            prs = self.get_pull_requests_impl(repo_id, state="open", per_page=10)
            for pr in prs:
                # Failing CI checks
                try:
                    # Forgejo Actions / CI status
                    runs = self._rest_request(
                        f"/repos/{repo_id}/actions/runs",
                        params={"branch": pr.source_branch, "limit": "3"},
                    )
                    for run in runs.get("workflow_runs", []) if isinstance(runs, dict) else []:
                        if run.get("status") == "completed" and run.get("conclusion") == "failure":
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.FAILING_CHECKS,
                                    title=f"Failing CI on PR #{pr.number}: {pr.title}",
                                    repository=repo_id,
                                    url=pr.url,
                                    priority=8,
                                    metadata={"pr_number": pr.number},
                                    provider="forgejo",
                                )
                            )
                            break
                except Exception:
                    pass

                # Merge conflicts
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
                            provider="forgejo",
                        )
                    )

                # Unresolved review comments
                try:
                    comments = self._rest_request(
                        f"/repos/{repo_id}/pulls/{pr.number}/comments",
                        params={"limit": "5"},
                    )
                    for c in comments:
                        tasks.append(
                            SuggestedTask(
                                task_type=TaskType.UNRESOLVED_COMMENTS,
                                title=f"Unresolved comment on PR #{pr.number}",
                                description=(c.get("body", "") or "")[:150],
                                repository=repo_id,
                                url=c.get("html_url", pr.url),
                                priority=4,
                                metadata={"pr_number": pr.number},
                                provider="forgejo",
                            )
                        )
                        break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("git_providers.forgejo suggested_tasks.prs err=%s", exc)

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # ── OAuth ─────────────────────────────────────────────────────────────

    def get_auth_url_impl(self, state: Optional[str] = None, **kwargs: Any) -> str:
        params = {
            "client_id": self.auth_info.client_id,
            "redirect_uri": self.auth_info.redirect_uri,
            "response_type": "code",
        }
        if self.auth_info.scopes:
            params["scope"] = " ".join(self.auth_info.scopes)
        else:
            params["scope"] = "repository issue"
        if state:
            params["state"] = state
        return f"{self._base_url}{_FORGEJO_OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        payload = {
            "client_id": self.auth_info.client_id,
            "client_secret": self.auth_info.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.auth_info.redirect_uri,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}{_FORGEJO_OAUTH_TOKEN}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise NonRetryableError(f"Forgejo OAuth callback failed: {exc}")

        new_auth = AuthInfo(
            auth_type=AuthType.OAUTH,
            token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
            scopes=result.get("scope", "").split(" ") if result.get("scope") else [],
            expires_at=time.time() + result.get("expires_in", 0) if result.get("expires_in") else None,
            client_id=self.auth_info.client_id,
            client_secret=self.auth_info.client_secret,
            redirect_uri=self.auth_info.redirect_uri,
            extra=self.auth_info.extra,
        )
        self.auth_info = new_auth
        return new_auth

    # ── webhooks ──────────────────────────────────────────────────────────

    _WEBHOOK_EVENT_MAP: Dict[WebhookEvent, str] = {
        WebhookEvent.PUSH: "push",
        WebhookEvent.PULL_REQUEST: "pull_request",
        WebhookEvent.ISSUE: "issues",
        WebhookEvent.COMMENT: "issue_comment",
        WebhookEvent.RELEASE: "release",
    }

    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        events = [
            self._WEBHOOK_EVENT_MAP.get(e, "push") for e in config.events
        ] or ["push"]
        payload = {
            "type": "gitea",  # Forgejo/Gitea webhook type
            "active": config.active,
            "events": events,
            "config": {
                "url": config.url,
                "content_type": "json",
                "secret": config.secret,
            },
        }
        data = self._rest_request(
            f"/repos/{repo_id}/hooks", method="POST", body=payload
        )
        config.webhook_id = str(data.get("id", ""))
        config.provider = "forgejo"
        return config

    def delete_webhook_impl(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        try:
            self._rest_request(f"/repos/{repo_id}/hooks/{webhook_id}", method="DELETE")
            return True
        except NonRetryableError:
            return False

    # ── utility ───────────────────────────────────────────────────────────

    def validate_token_impl(self) -> bool:
        try:
            self._rest_request("/user")
            return True
        except Exception:
            return False

    def get_authenticated_user_impl(self) -> Dict[str, Any]:
        data = self._rest_request("/user")
        return {
            "login": data.get("login", ""),
            "name": data.get("full_name", "") or data.get("login", ""),
            "email": data.get("email", ""),
            "avatar_url": data.get("avatar_url", ""),
            "id": data.get("id", ""),
            "provider": "forgejo",
        }
