from __future__ import annotations

"""
GitLab Git Provider Service
=============================
Concrete implementation of :class:`GitProviderService` for GitLab.

Uses **python-gitlab** when available; falls back to direct REST API calls
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

# ── optional python-gitlab import ─────────────────────────────────────────

try:
    import gitlab as gitlab_module
    import gitlab.v4.objects

    _HAS_PYTHON_GITLAB = True
except ImportError:
    _HAS_PYTHON_GITLAB = False

# ── constants ─────────────────────────────────────────────────────────────

_GITLAB_API = "https://gitlab.com/api/v4"
_GITLAB_OAUTH_AUTHORIZE = "https://gitlab.com/oauth/authorize"
_GITLAB_OAUTH_TOKEN = "https://gitlab.com/oauth/token"


def _parse_iso(val: Any) -> str:
    """Safely coerce a datetime or string to ISO-format string."""
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


class GitLabService(GitProviderService):
    """GitLab provider backed by python-gitlab (preferred) or raw REST API."""

    provider_name = "gitlab"

    def __init__(self, auth_info: AuthInfo) -> None:
        super().__init__(auth_info)
        self._client: Any = None
        self._base_url: str = auth_info.extra.get(
            "base_url", "https://gitlab.com"
        ).rstrip("/")
        self._api_url = f"{self._base_url}/api/v4"
        self._init_client()

    # ── client initialisation ─────────────────────────────────────────────

    def _init_client(self) -> None:
        if not _HAS_PYTHON_GITLAB:
            logger.debug("git_providers.gitlab python-gitlab not available, using REST fallback")
            self._client = None
            return

        try:
            if self.auth_info.token:
                self._client = gitlab_module.Gitlab(
                    self._base_url,
                    private_token=self.auth_info.token,
                    oauth_token=self.auth_info.token if self.auth_info.auth_type == AuthType.OAUTH else "",
                )
            else:
                self._client = gitlab_module.Gitlab(self._base_url)
        except Exception as exc:
            logger.warning("git_providers.gitlab init_failed err=%s", exc)
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
        url = f"{self._api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_info.token:
            if self.auth_info.auth_type == AuthType.OAUTH:
                headers["Authorization"] = f"Bearer {self.auth_info.token}"
            else:
                headers["PRIVATE-TOKEN"] = self.auth_info.token

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                rate_remaining = resp.headers.get("RateLimit-Remaining")
                rate_limit = resp.headers.get("RateLimit-Limit")
                rate_reset = resp.headers.get("RateLimit-Reset")
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
                reset = exc.headers.get("RateLimit-Reset")
                wait = max(1.0, float(reset) - time.time()) if reset else 10.0
                raise RetryableError(f"GitLab rate limit: {exc.reason}", wait_s=wait)
            if exc.code >= 500:
                raise RetryableError(f"GitLab server error {exc.code}: {exc.reason}", wait_s=2.0)
            if 400 <= exc.code < 500:
                raise NonRetryableError(f"GitLab client error {exc.code}: {exc.reason}")
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"GitLab network error: {exc}", wait_s=1.0)

    # ── URL-encoded project ID helper ─────────────────────────────────────

    @staticmethod
    def _project_path(repo_id: str) -> str:
        """Return a URL-encoded full path for the GitLab API."""
        return urllib.parse.quote(repo_id, safe="")

    # ── mapping helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_repo(raw: Any) -> Repository:
        if isinstance(raw, dict):
            return Repository(
                id=str(raw.get("id", "")),
                name=raw.get("name", ""),
                full_name=raw.get("path_with_namespace", ""),
                url=raw.get("web_url", ""),
                clone_url=raw.get("http_url_to_repo", ""),
                ssh_url=raw.get("ssh_url_to_repo", ""),
                description=raw.get("description", "") or "",
                default_branch=raw.get("default_branch", "main") or "main",
                is_private=raw.get("visibility", "private") != "public",
                is_fork=raw.get("forked_from_project") is not None,
                owner=raw.get("namespace", {}).get("path", ""),
                language=raw.get("programming_language", "") or "",
                stars=raw.get("star_count", 0),
                forks=raw.get("forks_count", 0),
                open_issues_count=raw.get("open_issues_count", 0),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("last_activity_at", ""),
                provider="gitlab",
            )
        return Repository(
            id=str(raw.id),
            name=raw.name,
            full_name=raw.path_with_namespace,
            url=raw.web_url,
            clone_url=raw.http_url_to_repo or "",
            ssh_url=raw.ssh_url_to_repo or "",
            description=raw.description or "",
            default_branch=getattr(raw, "default_branch", "main") or "main",
            is_private=getattr(raw, "visibility", "private") != "public",
            is_fork=bool(getattr(raw, "forked_from_project", None)),
            owner=raw.namespace["path"] if isinstance(raw.namespace, dict) else getattr(raw.namespace, "path", ""),
            language="",
            stars=getattr(raw, "star_count", 0),
            forks=getattr(raw, "forks_count", 0),
            open_issues_count=getattr(raw, "open_issues_count", 0),
            created_at=_parse_iso(getattr(raw, "created_at", "")),
            updated_at=_parse_iso(getattr(raw, "last_activity_at", "")),
            provider="gitlab",
        )

    @staticmethod
    def _map_mr(raw: Any) -> PullRequest:
        if isinstance(raw, dict):
            state = (
                PRStatus.MERGED if raw.get("state") == "merged"
                else PRStatus.CLOSED if raw.get("state") == "closed"
                else PRStatus.DRAFT if raw.get("draft")
                else PRStatus.OPEN
            )
            return PullRequest(
                id=str(raw.get("id", "")),
                number=raw.get("iid", 0),
                title=raw.get("title", ""),
                body=raw.get("description", "") or "",
                state=state,
                source_branch=raw.get("source_branch", ""),
                target_branch=raw.get("target_branch", ""),
                author=raw.get("author", {}).get("username", ""),
                assignees=[a.get("username", "") for a in raw.get("assignees", [])],
                labels=raw.get("labels", []),
                url=raw.get("web_url", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
                merged_at=raw.get("merged_at"),
                draft=raw.get("draft", False),
                mergeable=None,
                provider="gitlab",
            )
        state = (
            PRStatus.MERGED if raw.state == "merged"
            else PRStatus.CLOSED if raw.state == "closed"
            else PRStatus.DRAFT if getattr(raw, "draft", False)
            else PRStatus.OPEN
        )
        return PullRequest(
            id=str(raw.id),
            number=raw.iid,
            title=raw.title,
            body=raw.description or "",
            state=state,
            source_branch=raw.source_branch,
            target_branch=raw.target_branch,
            author=raw.author["username"] if isinstance(raw.author, dict) else getattr(raw.author, "username", ""),
            labels=raw.labels if isinstance(raw.labels, list) else [],
            url=raw.web_url,
            created_at=_parse_iso(raw.created_at),
            updated_at=_parse_iso(raw.updated_at),
            merged_at=_parse_iso(raw.merged_at) if raw.merged_at else None,
            draft=getattr(raw, "draft", False),
            mergeable=None,
            provider="gitlab",
        )

    @staticmethod
    def _map_issue(raw: Any) -> Issue:
        if isinstance(raw, dict):
            return Issue(
                id=str(raw.get("id", "")),
                number=raw.get("iid", 0),
                title=raw.get("title", ""),
                body=raw.get("description", "") or "",
                state=IssueStatus.OPEN if raw.get("state") == "opened" else IssueStatus.CLOSED,
                author=raw.get("author", {}).get("username", ""),
                assignees=[a.get("username", "") for a in raw.get("assignees", [])],
                labels=raw.get("labels", []),
                url=raw.get("web_url", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
                closed_at=raw.get("closed_at"),
                milestone=raw.get("milestone", {}).get("title", "") if raw.get("milestone") else "",
                provider="gitlab",
            )
        return Issue(
            id=str(raw.id),
            number=raw.iid,
            title=raw.title,
            body=raw.description or "",
            state=IssueStatus.OPEN if raw.state == "opened" else IssueStatus.CLOSED,
            author=raw.author["username"] if isinstance(raw.author, dict) else getattr(raw.author, "username", ""),
            labels=raw.labels if isinstance(raw.labels, list) else [],
            url=raw.web_url,
            created_at=_parse_iso(raw.created_at),
            updated_at=_parse_iso(raw.updated_at),
            closed_at=_parse_iso(raw.closed_at) if raw.closed_at else None,
            milestone=raw.milestone["title"] if isinstance(getattr(raw, "milestone", None), dict) else "",
            provider="gitlab",
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
                    projects = self._client.groups.get(owner).projects.list(
                        page=page, per_page=per_page
                    )
                else:
                    projects = self._client.projects.list(
                        owned=True, page=page, per_page=per_page
                    )
                return [self._map_repo(p) for p in projects]
            except Exception as exc:
                if "429" in str(exc):
                    raise RetryableError(str(exc), wait_s=10.0)
                raise NonRetryableError(f"GitLab error: {exc}")

        params: Dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "order_by": "updated_at",
        }
        if owner:
            # Try group first
            try:
                data = self._rest_request(
                    f"/groups/{urllib.parse.quote(owner, safe='')}/projects",
                    params=params,
                )
            except NonRetryableError:
                # Fall back to user projects
                params["username"] = owner
                data = self._rest_request("/users/{owner}/projects", params=params)
        else:
            data = self._rest_request("/projects", params=params)
        return [self._map_repo(r) for r in data]

    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
        path = self._project_path(repo_id)
        if self._client is not None:
            try:
                return self._map_repo(self._client.projects.get(path))
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(f"/projects/{path}")
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
        path = self._project_path(repo_id)
        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                default = project.default_branch or "main"
                branches = project.branches.list(page=page, per_page=per_page)
                return [
                    Branch(
                        name=b.name,
                        commit_sha=b.commit["id"] if isinstance(b.commit, dict) else getattr(b.commit, "id", ""),
                        is_default=b.name == default,
                        is_protected=getattr(b, "protected", False),
                    )
                    for b in branches
                ]
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/repository/branches",
            params={"page": str(page), "per_page": str(per_page)},
        )
        repo = self.get_repo_impl(repo_id)
        default = repo.default_branch
        return [
            Branch(
                name=b.get("name", ""),
                commit_sha=b.get("commit", {}).get("id", ""),
                is_default=b.get("name") == default,
                is_protected=b.get("protected", False),
            )
            for b in data
        ]

    # ── merge request operations ──────────────────────────────────────────

    def get_pull_requests_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[PullRequest]:
        path = self._project_path(repo_id)
        # Map common state names to GitLab conventions
        gl_state = "opened" if state == "open" else state

        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                mrs = project.mergerequests.list(
                    state=gl_state, page=page, per_page=per_page, order_by="updated_at"
                )
                return [self._map_mr(mr) for mr in mrs]
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/merge_requests",
            params={"state": gl_state, "page": str(page), "per_page": str(per_page)},
        )
        return [self._map_mr(mr) for mr in data]

    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        path = self._project_path(repo_id)
        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                return self._map_mr(project.mergerequests.get(pr_number))
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(f"/projects/{path}/merge_requests/{pr_number}")
        return self._map_mr(data)

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
        path = self._project_path(repo_id)
        payload = {
            "title": title,
            "description": body,
            "source_branch": source_branch,
            "target_branch": target_branch,
        }
        if draft:
            payload["title"] = f"Draft: {title}"

        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                mr = project.mergerequests.create(payload)
                return self._map_mr(mr)
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/merge_requests", method="POST", body=payload
        )
        return self._map_mr(data)

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
        path = self._project_path(repo_id)
        gl_state = "opened" if state == "open" else state

        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                issues = project.issues.list(
                    state=gl_state, page=page, per_page=per_page, order_by="updated_at"
                )
                return [self._map_issue(i) for i in issues]
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/issues",
            params={"state": gl_state, "page": str(page), "per_page": str(per_page)},
        )
        return [self._map_issue(i) for i in data]

    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        path = self._project_path(repo_id)
        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                return self._map_issue(project.issues.get(issue_number))
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(f"/projects/{path}/issues/{issue_number}")
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
        path = self._project_path(repo_id)
        payload: Dict[str, Any] = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        if assignees:
            payload["assignee_ids"] = assignees  # GitLab uses IDs; pass as-is

        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                issue = project.issues.create(payload)
                return self._map_issue(issue)
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/issues", method="POST", body=payload
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
        path = self._project_path(repo_id)
        if self._client is not None:
            try:
                project = self._client.projects.get(path)
                issue = project.issues.get(issue_number)
                note = issue.notes.create({"body": body})
                return Comment(
                    id=str(note.id),
                    body=note.body,
                    author=getattr(note.author, "username", "") if hasattr(note, "author") else "",
                    created_at=_parse_iso(getattr(note, "created_at", "")),
                    updated_at=_parse_iso(getattr(note, "updated_at", "")),
                    provider="gitlab",
                )
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{path}/issues/{issue_number}/notes",
            method="POST",
            body={"body": body},
        )
        return Comment(
            id=str(data.get("id", "")),
            body=data.get("body", ""),
            author=data.get("author", {}).get("username", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            provider="gitlab",
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
            "search": query,
            "page": str(page),
            "per_page": str(per_page),
        }
        if repo:
            params["project_id"] = self._project_path(repo)

        # GitLab search is project-scoped in the REST API
        if repo:
            path = self._project_path(repo)
            data = self._rest_request(f"/projects/{path}/search", params={"scope": "blobs", **params})
        else:
            data = self._rest_request("/search", params={"scope": "blobs", **params})

        items = data if isinstance(data, list) else data.get("items", data.get("results", []))
        return [
            FileContent(
                path=item.get("path", item.get("filename", "")),
                content=item.get("data", ""),
                repository=repo or "",
                provider="gitlab",
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
        proj = self._project_path(repo_id)
        params: Dict[str, str] = {}
        if ref:
            params["ref"] = ref

        if self._client is not None:
            try:
                project = self._client.projects.get(proj)
                file_obj = project.files.get(file_path=path, ref=ref or project.default_branch)
                content = ""
                try:
                    content = base64.b64decode(file_obj.content).decode("utf-8", errors="replace")
                except Exception:
                    content = file_obj.content
                return FileContent(
                    path=file_obj.file_path,
                    content=content,
                    encoding="base64",
                    sha=file_obj.commit_id,
                    size=getattr(file_obj, "size", 0),
                    provider="gitlab",
                )
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request(
            f"/projects/{proj}/repository/files/{urllib.parse.quote(path, safe='')}",
            params=params,
        )
        content = ""
        if data.get("content"):
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                content = data["content"]
        return FileContent(
            path=data.get("file_path", path),
            content=content,
            encoding="base64",
            sha=data.get("commit_id", ""),
            size=data.get("size", 0),
            provider="gitlab",
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
                        provider="gitlab",
                    )
                )
        except Exception as exc:
            logger.warning("git_providers.gitlab suggested_tasks.issues err=%s", exc)

        # 2. MRs with failing pipelines
        try:
            mrs = self.get_pull_requests_impl(repo_id, state="open", per_page=10)
            for mr in mrs:
                proj = self._project_path(repo_id)

                # Failing CI pipelines
                try:
                    pipelines = self._rest_request(
                        f"/projects/{proj}/pipelines",
                        params={"ref": mr.source_branch, "per_page": "1"},
                    )
                    if isinstance(pipelines, list):
                        for p in pipelines:
                            if p.get("status") == "failed":
                                tasks.append(
                                    SuggestedTask(
                                        task_type=TaskType.FAILING_CHECKS,
                                        title=f"Failing pipeline on MR !{mr.number}: {mr.title}",
                                        description=f"Pipeline #{p.get('id', '')} failed",
                                        repository=repo_id,
                                        url=mr.url,
                                        priority=8,
                                        metadata={"mr_number": mr.number, "pipeline_id": p.get("id")},
                                        provider="gitlab",
                                    )
                                )
                                break
                    elif isinstance(pipelines, dict):
                        for p in pipelines.get("pipelines", pipelines.get("items", [])):
                            if p.get("status") == "failed":
                                tasks.append(
                                    SuggestedTask(
                                        task_type=TaskType.FAILING_CHECKS,
                                        title=f"Failing pipeline on MR !{mr.number}: {mr.title}",
                                        repository=repo_id,
                                        url=mr.url,
                                        priority=8,
                                        metadata={"mr_number": mr.number},
                                        provider="gitlab",
                                    )
                                )
                                break
                except Exception:
                    pass

                # Merge conflicts
                try:
                    mr_detail = self._rest_request(
                        f"/projects/{proj}/merge_requests/{mr.number}"
                    )
                    if mr_detail.get("merge_status") == "cannot_be_merged":
                        tasks.append(
                            SuggestedTask(
                                task_type=TaskType.MERGE_CONFLICT,
                                title=f"Merge conflict on MR !{mr.number}: {mr.title}",
                                description=f"Source: {mr.source_branch} → Target: {mr.target_branch}",
                                repository=repo_id,
                                url=mr.url,
                                priority=7,
                                metadata={"mr_number": mr.number},
                                provider="gitlab",
                            )
                        )
                except Exception:
                    pass

                # Unresolved discussions
                try:
                    discussions = self._rest_request(
                        f"/projects/{proj}/merge_requests/{mr.number}/discussions",
                        params={"per_page": "5"},
                    )
                    disc_list = discussions if isinstance(discussions, list) else []
                    for d in disc_list:
                        if not d.get("resolved", True):
                            notes = d.get("notes", [])
                            first_note = notes[0] if notes else {}
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.UNRESOLVED_COMMENTS,
                                    title=f"Unresolved discussion on MR !{mr.number}",
                                    description=(first_note.get("body", "") or "")[:150],
                                    repository=repo_id,
                                    url=mr.url,
                                    priority=4,
                                    metadata={"mr_number": mr.number, "discussion_id": d.get("id")},
                                    provider="gitlab",
                                )
                            )
                            break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("git_providers.gitlab suggested_tasks.mrs err=%s", exc)

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # ── OAuth ─────────────────────────────────────────────────────────────

    def get_auth_url_impl(self, state: Optional[str] = None, **kwargs: Any) -> str:
        params = {
            "client_id": self.auth_info.client_id,
            "redirect_uri": self.auth_info.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.auth_info.scopes) if self.auth_info.scopes else "api read_repository",
        }
        if state:
            params["state"] = state
        return f"{self._base_url}/oauth/authorize?{urllib.parse.urlencode(params)}"

    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        payload = {
            "client_id": self.auth_info.client_id,
            "client_secret": self.auth_info.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.auth_info.redirect_uri,
        }
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/oauth/token", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise NonRetryableError(f"GitLab OAuth callback failed: {exc}")

        if "error" in result:
            raise NonRetryableError(
                f"GitLab OAuth error: {result.get('error_description', result['error'])}"
            )

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
        self._init_client()
        return new_auth

    # ── webhooks ──────────────────────────────────────────────────────────

    _WEBHOOK_EVENT_MAP: Dict[WebhookEvent, str] = {
        WebhookEvent.PUSH: "push_events",
        WebhookEvent.PULL_REQUEST: "merge_requests_events",
        WebhookEvent.ISSUE: "issues_events",
        WebhookEvent.COMMENT: "note_events",
        WebhookEvent.CHECK_RUN: "pipeline_events",
        WebhookEvent.RELEASE: "releases_events",
        WebhookEvent.MEMBER: "membership_events",
    }

    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        proj = self._project_path(repo_id)
        payload: Dict[str, Any] = {
            "url": config.url,
            "token": config.secret,
            "enable_ssl_verification": True,
        }
        for event in config.events:
            key = self._WEBHOOK_EVENT_MAP.get(event)
            if key:
                payload[key] = True

        if self._client is not None:
            try:
                project = self._client.projects.get(proj)
                hook = project.hooks.create(payload)
                config.webhook_id = str(hook.id)
                config.provider = "gitlab"
                return config
            except Exception as exc:
                raise NonRetryableError(f"GitLab webhook registration failed: {exc}")

        data = self._rest_request(
            f"/projects/{proj}/hooks", method="POST", body=payload
        )
        config.webhook_id = str(data.get("id", ""))
        config.provider = "gitlab"
        return config

    def delete_webhook_impl(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        proj = self._project_path(repo_id)
        if self._client is not None:
            try:
                project = self._client.projects.get(proj)
                project.hooks.delete(int(webhook_id))
                return True
            except Exception as exc:
                raise NonRetryableError(f"GitLab webhook deletion failed: {exc}")

        try:
            self._rest_request(f"/projects/{proj}/hooks/{webhook_id}", method="DELETE")
            return True
        except NonRetryableError:
            return False

    # ── utility ───────────────────────────────────────────────────────────

    def validate_token_impl(self) -> bool:
        try:
            if self._client is not None:
                self._client.user
                return True
            self._rest_request("/user")
            return True
        except Exception:
            return False

    def get_authenticated_user_impl(self) -> Dict[str, Any]:
        if self._client is not None:
            try:
                user = self._client.user
                return {
                    "login": getattr(user, "username", ""),
                    "name": getattr(user, "name", ""),
                    "email": getattr(user, "email", ""),
                    "avatar_url": getattr(user, "avatar_url", ""),
                    "id": getattr(user, "id", ""),
                    "provider": "gitlab",
                }
            except Exception as exc:
                raise NonRetryableError(f"GitLab error: {exc}")

        data = self._rest_request("/user")
        return {
            "login": data.get("username", ""),
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "avatar_url": data.get("avatar_url", ""),
            "id": data.get("id"),
            "provider": "gitlab",
        }
