from __future__ import annotations

"""
Bitbucket Git Provider Service
================================
Concrete implementation of :class:`GitProviderService` for Bitbucket
(Cloud). Uses the Bitbucket Cloud REST API v2 via ``urllib``.
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

_BITBUCKET_API = "https://api.bitbucket.org/2.0"
_BITBUCKET_OAUTH_AUTHORIZE = "https://bitbucket.org/site/oauth2/authorize"
_BITBUCKET_OAUTH_TOKEN = "https://bitbucket.org/site/oauth2/access_token"


def _parse_iso(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


class BitbucketService(GitProviderService):
    """Bitbucket Cloud provider using REST API v2."""

    provider_name = "bitbucket"

    def __init__(self, auth_info: AuthInfo) -> None:
        super().__init__(auth_info)
        self._workspace = auth_info.extra.get("workspace", "")

    # ── REST helpers ──────────────────────────────────────────────────────

    def _rest_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{_BITBUCKET_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_info.auth_type == AuthType.OAUTH and self.auth_info.token:
            headers["Authorization"] = f"Bearer {self.auth_info.token}"
        elif self.auth_info.token:
            # Basic auth with app password or personal access token
            # Bitbucket uses username:app_password for PATs
            username = self.auth_info.extra.get("username", "")
            encoded = base64.b64encode(
                f"{username}:{self.auth_info.token}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {encoded}"

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                rate_remaining = resp.headers.get("X-RateLimit-Remaining")
                rate_limit = resp.headers.get("X-RateLimit-Limit")
                if rate_remaining is not None:
                    self.update_rate_limit(
                        RateLimitInfo(
                            remaining=int(rate_remaining),
                            limit=int(rate_limit or 0),
                        )
                    )
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 10.0
                raise RetryableError(
                    f"Bitbucket rate limit: {exc.reason}", wait_s=wait
                )
            if exc.code >= 500:
                raise RetryableError(
                    f"Bitbucket server error {exc.code}: {exc.reason}", wait_s=2.0
                )
            if 400 <= exc.code < 500:
                raise NonRetryableError(
                    f"Bitbucket client error {exc.code}: {exc.reason}"
                )
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"Bitbucket network error: {exc}", wait_s=1.0)

    # ── mapping helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_repo(raw: Dict[str, Any]) -> Repository:
        owner = raw.get("owner", {})
        return Repository(
            id=str(raw.get("uuid", "")),
            name=raw.get("name", ""),
            full_name=raw.get("full_name", ""),
            url=raw.get("links", {}).get("html", {}).get("href", ""),
            clone_url=next(
                (c["href"] for c in raw.get("links", {}).get("clone", [])
                 if c.get("name") == "https"),
                "",
            ),
            ssh_url=next(
                (c["href"] for c in raw.get("links", {}).get("clone", [])
                 if c.get("name") == "ssh"),
                "",
            ),
            description=raw.get("description", "") or "",
            default_branch=raw.get("mainbranch", {}).get("name", "main"),
            is_private=raw.get("is_private", True),
            is_fork=bool(raw.get("parent")),
            owner=owner.get("username", "") if isinstance(owner, dict) else "",
            language=raw.get("language", ""),
            created_at=raw.get("created_on", ""),
            updated_at=raw.get("updated_on", ""),
            provider="bitbucket",
        )

    @staticmethod
    def _map_pr(raw: Dict[str, Any]) -> PullRequest:
        state_map = {
            "OPEN": PRStatus.OPEN,
            "MERGED": PRStatus.MERGED,
            "DECLINED": PRStatus.CLOSED,
            "SUPERSEDED": PRStatus.CLOSED,
        }
        return PullRequest(
            id=str(raw.get("id", "")),
            number=raw.get("id", 0),
            title=raw.get("title", ""),
            body=raw.get("description", "") or raw.get("summary", {}).get("raw", "") or "",
            state=state_map.get(raw.get("state", ""), PRStatus.OPEN),
            source_branch=raw.get("source", {}).get("branch", {}).get("name", ""),
            target_branch=raw.get("destination", {}).get("branch", {}).get("name", ""),
            author=raw.get("author", {}).get("username", ""),
            url=raw.get("links", {}).get("html", {}).get("href", ""),
            created_at=raw.get("created_on", ""),
            updated_at=raw.get("updated_on", ""),
            draft=False,
            mergeable=None,
            provider="bitbucket",
        )

    @staticmethod
    def _map_issue(raw: Dict[str, Any]) -> Issue:
        state = IssueStatus.OPEN if raw.get("state") in ("new", "open") else IssueStatus.CLOSED
        return Issue(
            id=str(raw.get("id", "")),
            number=raw.get("id", 0),
            title=raw.get("title", ""),
            body=raw.get("content", {}).get("raw", "") if isinstance(raw.get("content"), dict) else raw.get("content", ""),
            state=state,
            author=raw.get("reporter", {}).get("username", ""),
            assignee=raw.get("assignee", {}).get("username", "") if raw.get("assignee") else "",
            labels=[p.get("name", "") for p in raw.get("priority", [])] if isinstance(raw.get("priority"), list) else [],
            url=raw.get("links", {}).get("html", {}).get("href", ""),
            created_at=raw.get("created_on", ""),
            updated_at=raw.get("updated_on", ""),
            provider="bitbucket",
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
        workspace = owner or self._workspace
        if not workspace:
            # List all accessible repos
            data = self._rest_request(
                "/repositories",
                params={"page": str(page), "pagelen": str(per_page)},
            )
        else:
            data = self._rest_request(
                f"/repositories/{workspace}",
                params={"page": str(page), "pagelen": str(per_page)},
            )
        repos = data.get("values", []) if isinstance(data, dict) else data
        return [self._map_repo(r) for r in repos]

    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
        data = self._rest_request(f"/repositories/{repo_id}")
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
            f"/repositories/{repo_id}/refs/branches",
            params={"page": str(page), "pagelen": str(per_page)},
        )
        branches = data.get("values", []) if isinstance(data, dict) else data
        repo = self.get_repo_impl(repo_id)
        return [
            Branch(
                name=b.get("name", ""),
                commit_sha=b.get("target", {}).get("hash", ""),
                is_default=b.get("name") == repo.default_branch,
            )
            for b in branches
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
        bb_state = state.upper() if state in ("open", "merged", "declined") else "OPEN"
        data = self._rest_request(
            f"/repositories/{repo_id}/pullrequests",
            params={"state": bb_state, "page": str(page), "pagelen": str(per_page)},
        )
        prs = data.get("values", []) if isinstance(data, dict) else data
        return [self._map_pr(p) for p in prs]

    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        data = self._rest_request(f"/repositories/{repo_id}/pullrequests/{pr_number}")
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
        payload: Dict[str, Any] = {
            "title": title,
            "description": body,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target_branch}},
        }
        if draft:
            payload["draft"] = True
        data = self._rest_request(
            f"/repositories/{repo_id}/pullrequests", method="POST", body=payload
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
        params: Dict[str, str] = {
            "page": str(page),
            "pagelen": str(per_page),
        }
        if state == "open":
            params["state"] = "new"
        elif state == "closed":
            params["state"] = "resolved"

        data = self._rest_request(
            f"/repositories/{repo_id}/issues", params=params
        )
        issues = data.get("values", []) if isinstance(data, dict) else data
        return [self._map_issue(i) for i in issues]

    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        data = self._rest_request(f"/repositories/{repo_id}/issues/{issue_number}")
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
        payload: Dict[str, Any] = {"title": title}
        if body:
            payload["content"] = {"raw": body}
        if labels:
            payload["kind"] = labels[0]  # Bitbucket uses "kind" for issue type
        if assignees:
            payload["assignee"] = {"username": assignees[0]}

        data = self._rest_request(
            f"/repositories/{repo_id}/issues", method="POST", body=payload
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
        payload = {"content": {"raw": body}}
        data = self._rest_request(
            f"/repositories/{repo_id}/issues/{issue_number}/comments",
            method="POST",
            body=payload,
        )
        return Comment(
            id=str(data.get("id", "")),
            body=data.get("content", {}).get("raw", body),
            author=data.get("user", {}).get("username", ""),
            created_at=data.get("created_on", ""),
            provider="bitbucket",
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
        # Bitbucket has a search endpoint but it's limited
        # We'll use the repository file search
        if repo:
            data = self._rest_request(
                f"/repositories/{repo}/src",
                params={"q": query, "page": str(page), "pagelen": str(per_page)},
            )
            values = data.get("values", []) if isinstance(data, dict) else data
            return [
                FileContent(
                    path=v.get("path", ""),
                    content="",
                    repository=repo,
                    sha=v.get("commit", {}).get("hash", ""),
                    provider="bitbucket",
                )
                for v in values
            ]
        return []

    # ── file content ──────────────────────────────────────────────────────

    def get_file_content_impl(
        self,
        repo_id: str,
        path: str,
        *,
        ref: Optional[str] = None,
        **kwargs: Any,
    ) -> FileContent:
        # Bitbucket: /repositories/{workspace}/{repo_slug}/src/{commit}/{path}
        if not ref:
            ref = "HEAD"
        url_path = f"/repositories/{repo_id}/src/{ref}/{path}"
        headers: Dict[str, str] = {}
        if self.auth_info.auth_type == AuthType.OAUTH and self.auth_info.token:
            headers["Authorization"] = f"Bearer {self.auth_info.token}"
        elif self.auth_info.token:
            username = self.auth_info.extra.get("username", "")
            encoded = base64.b64encode(
                f"{username}:{self.auth_info.token}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {encoded}"

        full_url = f"{_BITBUCKET_API}{url_path}"
        req = urllib.request.Request(full_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                return FileContent(
                    path=path,
                    content=content,
                    provider="bitbucket",
                )
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise NonRetryableError(
                    f"Bitbucket file content error {exc.code}: {exc.reason}"
                )
            raise RetryableError(
                f"Bitbucket file content error {exc.code}: {exc.reason}", wait_s=2.0
            )
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"Bitbucket network error: {exc}", wait_s=1.0)

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
                        priority=5,
                        metadata={"issue_number": issue.number},
                        provider="bitbucket",
                    )
                )
        except Exception as exc:
            logger.warning("git_providers.bitbucket suggested_tasks.issues err=%s", exc)

        # 2. PR checks and conflicts
        try:
            prs = self.get_pull_requests_impl(repo_id, state="open", per_page=10)
            for pr in prs:
                # Check for failing pipelines
                try:
                    pipelines = self._rest_request(
                        f"/repositories/{repo_id}/pipelines/",
                        params={"pagelen": "3"},
                    )
                    for p in pipelines.get("values", []):
                        if p.get("state", {}).get("result", {}).get("name") == "FAILED":
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.FAILING_CHECKS,
                                    title=f"Failing pipeline on PR #{pr.number}: {pr.title}",
                                    repository=repo_id,
                                    url=pr.url,
                                    priority=8,
                                    metadata={"pr_number": pr.number},
                                    provider="bitbucket",
                                )
                            )
                            break
                except Exception:
                    pass

                # Merge conflicts - check PR diffstat
                try:
                    pr_detail = self._rest_request(
                        f"/repositories/{repo_id}/pullrequests/{pr.number}/diffstat"
                    )
                    if pr_detail.get("properties", {}).get("mergeConflict"):
                        tasks.append(
                            SuggestedTask(
                                task_type=TaskType.MERGE_CONFLICT,
                                title=f"Merge conflict on PR #{pr.number}: {pr.title}",
                                repository=repo_id,
                                url=pr.url,
                                priority=7,
                                metadata={"pr_number": pr.number},
                                provider="bitbucket",
                            )
                        )
                except Exception:
                    pass

                # Unresolved comments
                try:
                    comments = self._rest_request(
                        f"/repositories/{repo_id}/pullrequests/{pr.number}/comments",
                        params={"pagelen": "5"},
                    )
                    for c in comments.get("values", []):
                        if c.get("pending") and not c.get("deleted"):
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.UNRESOLVED_COMMENTS,
                                    title=f"Unresolved comment on PR #{pr.number}",
                                    description=(c.get("content", {}).get("raw", ""))[:150],
                                    repository=repo_id,
                                    url=pr.url,
                                    priority=4,
                                    metadata={"pr_number": pr.number},
                                    provider="bitbucket",
                                )
                            )
                            break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("git_providers.bitbucket suggested_tasks.prs err=%s", exc)

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
            params["scope"] = "repository issue:read pullrequest:read"
        if state:
            params["state"] = state
        return f"{_BITBUCKET_OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.auth_info.redirect_uri,
        }
        encoded_creds = base64.b64encode(
            f"{self.auth_info.client_id}:{self.auth_info.client_secret}".encode()
        ).decode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded_creds}",
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            _BITBUCKET_OAUTH_TOKEN, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise NonRetryableError(f"Bitbucket OAuth callback failed: {exc}")

        new_auth = AuthInfo(
            auth_type=AuthType.OAUTH,
            token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
            scopes=result.get("scopes", "").split(" ") if result.get("scopes") else [],
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
        WebhookEvent.PUSH: "repo:push",
        WebhookEvent.PULL_REQUEST: "pullrequest:created",
        WebhookEvent.ISSUE: "issue:created",
        WebhookEvent.COMMENT: "issue:comment_created",
    }

    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        events = [
            self._WEBHOOK_EVENT_MAP.get(e, "repo:push") for e in config.events
        ] or ["repo:push"]
        payload = {
            "description": "ManusClaw webhook",
            "url": config.url,
            "active": config.active,
            "events": events,
        }
        data = self._rest_request(
            f"/repositories/{repo_id}/hooks", method="POST", body=payload
        )
        config.webhook_id = str(data.get("uuid", ""))
        config.provider = "bitbucket"
        return config

    def delete_webhook_impl(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        try:
            self._rest_request(
                f"/repositories/{repo_id}/hooks/{webhook_id}", method="DELETE"
            )
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
            "login": data.get("username", ""),
            "name": data.get("display_name", ""),
            "email": "",
            "avatar_url": data.get("links", {}).get("avatar", {}).get("href", ""),
            "id": data.get("account_id", ""),
            "provider": "bitbucket",
        }
