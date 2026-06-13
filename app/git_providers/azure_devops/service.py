from __future__ import annotations

"""
Azure DevOps Git Provider Service
===================================
Concrete implementation of :class:`GitProviderService` for Azure DevOps.

Uses the Azure DevOps REST API exclusively (``urllib``).  The
``azure-devops`` Python package is optional; when present it is used
for OAuth helper flows only.
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

# ── optional azure-devops import ──────────────────────────────────────────

try:
    from azure.devops.connection import Connection as AzdoConnection
    from msrest.authentication import BasicAuthentication

    _HAS_AZURE_DEVOPS = True
except ImportError:
    _HAS_AZURE_DEVOPS = False

# ── constants ─────────────────────────────────────────────────────────────

_AZDO_ORG_API = "https://dev.azure.com"
_AZDO_OAUTH_AUTHORIZE = "https://app.vssps.visualstudio.com/oauth2/authorize"
_AZDO_OAUTH_TOKEN = "https://app.vssps.visualstudio.com/oauth2/token"


def _parse_iso(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


class AzureDevOpsService(GitProviderService):
    """Azure DevOps provider using REST API."""

    provider_name = "azure_devops"

    def __init__(self, auth_info: AuthInfo) -> None:
        super().__init__(auth_info)
        self._client: Any = None
        self._organization = auth_info.extra.get("organization", "")
        self._org_url = f"{_AZDO_ORG_API}/{self._organization}" if self._organization else ""
        self._init_client()

    # ── client initialisation ─────────────────────────────────────────────

    def _init_client(self) -> None:
        if not _HAS_AZURE_DEVOPS:
            logger.debug("git_providers.azure_devops SDK not available, using REST fallback")
            self._client = None
            return
        try:
            if self.auth_info.token and self._org_url:
                credentials = BasicAuthentication("", self.auth_info.token)
                self._client = AzdoConnection(base_url=self._org_url, creds=credentials)
            else:
                self._client = None
        except Exception as exc:
            logger.warning("git_providers.azure_devops init_failed err=%s", exc)
            self._client = None

    # ── REST helpers ──────────────────────────────────────────────────────

    def _rest_request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
        is_core: bool = True,
    ) -> Any:
        """Make an authenticated REST request to Azure DevOps.

        ``is_core=True``  → use ``_apis`` query param for REST API versioning.
        """
        if params is None:
            params = {}
        if is_core and "api-version" not in params:
            params["api-version"] = "7.0"

        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_info.token:
            # PAT auth uses Basic with empty username
            encoded = base64.b64encode(f":{self.auth_info.token}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        elif self.auth_info.auth_type == AuthType.OAUTH and self.auth_info.token:
            headers["Authorization"] = f"Bearer {self.auth_info.token}"

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = resp_headers_get(exc, "Retry-After")
                wait = float(retry_after) if retry_after else 5.0
                raise RetryableError(f"Azure DevOps rate limit: {exc.reason}", wait_s=wait)
            if exc.code >= 500:
                raise RetryableError(
                    f"Azure DevOps server error {exc.code}: {exc.reason}", wait_s=2.0
                )
            if 400 <= exc.code < 500:
                raise NonRetryableError(
                    f"Azure DevOps client error {exc.code}: {exc.reason}"
                )
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"Azure DevOps network error: {exc}", wait_s=1.0)

    # ── mapping helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_repo(raw: Dict[str, Any], org: str = "") -> Repository:
        return Repository(
            id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            full_name=f"{org}/{raw.get('project', {}).get('name', '')}/{raw.get('name', '')}",
            url=raw.get("webUrl", ""),
            clone_url=raw.get("remoteUrl", ""),
            ssh_url="",
            description=raw.get("project", {}).get("description", "") or "",
            default_branch=raw.get("defaultBranch", "main"),
            is_private=raw.get("project", {}).get("visibility", "private") != "public",
            is_fork=False,
            owner=org,
            language="",
            stars=0,
            forks=0,
            open_issues_count=0,
            created_at="",
            updated_at="",
            provider="azure_devops",
        )

    @staticmethod
    def _map_pr(raw: Dict[str, Any]) -> PullRequest:
        status_map = {
            "active": PRStatus.OPEN,
            "completed": PRStatus.MERGED,
            "abandoned": PRStatus.CLOSED,
        }
        status = status_map.get(raw.get("status", ""), PRStatus.OPEN)
        is_draft = raw.get("isDraft", False)
        if is_draft and status == PRStatus.OPEN:
            status = PRStatus.DRAFT
        return PullRequest(
            id=str(raw.get("pullRequestId", "")),
            number=raw.get("pullRequestId", 0),
            title=raw.get("title", ""),
            body=raw.get("description", "") or "",
            state=status,
            source_branch=raw.get("sourceRefName", "").replace("refs/heads/", ""),
            target_branch=raw.get("targetRefName", "").replace("refs/heads/", ""),
            author=raw.get("createdBy", {}).get("uniqueName", ""),
            url=raw.get("url", ""),
            created_at=raw.get("creationDate", ""),
            updated_at="",
            draft=is_draft,
            mergeable=None,
            provider="azure_devops",
        )

    @staticmethod
    def _map_issue(raw: Dict[str, Any]) -> Issue:
        state = (
            IssueStatus.OPEN if raw.get("state") in ("Active", "New", "Approved", "Committed", "In Progress")
            else IssueStatus.CLOSED
        )
        return Issue(
            id=str(raw.get("id", "")),
            number=raw.get("id", 0),
            title=raw.get("title", "") or raw.get("name", ""),
            body=raw.get("description", "") or raw.get("text", ""),
            state=state,
            author=raw.get("createdBy", {}).get("uniqueName", ""),
            assignees=[raw.get("assignedTo", {}).get("uniqueName", "")] if raw.get("assignedTo") else [],
            labels=[],
            url=raw.get("url", ""),
            created_at=raw.get("createdDate", ""),
            updated_at=raw.get("changedDate", ""),
            provider="azure_devops",
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
        org = owner or self._organization
        if not org:
            raise NonRetryableError("Azure DevOps requires an organization name")

        url = f"{_AZDO_ORG_API}/{org}/_apis/git/repositories"
        data = self._rest_request(url, params={"$top": str(per_page), "$skip": str((page - 1) * per_page)})
        repos = data.get("value", []) if isinstance(data, dict) else data
        return [self._map_repo(r, org) for r in repos]

    def get_repo_impl(self, repo_id: str, **kwargs: Any) -> Repository:
        # repo_id format: org/project/repo
        parts = repo_id.split("/")
        if len(parts) >= 3:
            org, project, repo = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            org = self._organization
            project, repo = parts
        else:
            org = self._organization
            project = kwargs.get("project", "")
            repo = repo_id

        if not org:
            raise NonRetryableError("Azure DevOps requires an organization name")

        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}"
        data = self._rest_request(url)
        return self._map_repo(data, org)

    # ── branch operations ─────────────────────────────────────────────────

    def get_branches_impl(
        self,
        repo_id: str,
        *,
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Branch]:
        org, project, repo = self._parse_repo_id(repo_id)
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/refs"
        data = self._rest_request(url, params={"$top": str(per_page), "filter": "heads/"})
        refs = data.get("value", []) if isinstance(data, dict) else data
        return [
            Branch(
                name=r.get("name", "").replace("refs/heads/", ""),
                commit_sha=r.get("objectId", ""),
                is_default=False,
                is_protected=False,
            )
            for r in refs
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
        org, project, repo = self._parse_repo_id(repo_id)
        azdo_status = "active" if state == "open" else "completed" if state == "merged" else state
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests"
        data = self._rest_request(
            url,
            params={"searchCriteria.status": azdo_status, "$top": str(per_page)},
        )
        prs = data.get("value", []) if isinstance(data, dict) else data
        return [self._map_pr(p) for p in prs]

    def get_pr_impl(self, repo_id: str, pr_number: int, **kwargs: Any) -> PullRequest:
        org, project, repo = self._parse_repo_id(repo_id)
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests/{pr_number}"
        data = self._rest_request(url)
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
        org, project, repo = self._parse_repo_id(repo_id)
        payload = {
            "title": title,
            "description": body,
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "isDraft": draft,
        }
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests"
        data = self._rest_request(url, method="POST", body=payload)
        return self._map_pr(data)

    # ── issue operations (Azure DevOps Work Items) ────────────────────────

    def get_issues_impl(
        self,
        repo_id: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
        **kwargs: Any,
    ) -> List[Issue]:
        org, project, _repo = self._parse_repo_id(repo_id)
        wiql = {
            "query": f"""
            SELECT [System.Id], [System.Title], [System.State], [System.Description]
            FROM WorkItems
            WHERE [System.TeamProject] = '{project}'
            AND [System.WorkItemType] IN ('Bug', 'User Story', 'Task', 'Issue')
            AND [System.State] <> 'Closed' AND [System.State] <> 'Removed'
            ORDER BY [System.ChangedDate] DESC
            """
        }
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/wit/wiql"
        result = self._rest_request(url, method="POST", body=wiql)
        work_item_refs = result.get("workItems", [])

        issues: List[Issue] = []
        for ref in work_item_refs[:per_page]:
            wi_url = ref.get("url", "")
            if not wi_url:
                continue
            try:
                wi_data = self._rest_request(wi_url, is_core=False)
                fields = wi_data.get("fields", {})
                issues.append(
                    Issue(
                        id=str(wi_data.get("id", "")),
                        number=wi_data.get("id", 0),
                        title=fields.get("System.Title", ""),
                        body=fields.get("System.Description", "") or "",
                        state=IssueStatus.OPEN,
                        author=fields.get("System.CreatedBy", ""),
                        assignees=[fields.get("System.AssignedTo", "")] if fields.get("System.AssignedTo") else [],
                        labels=[fields.get("System.WorkItemType", "")],
                        url=wi_data.get("_links", {}).get("html", {}).get("href", ""),
                        created_at=fields.get("System.CreatedDate", ""),
                        updated_at=fields.get("System.ChangedDate", ""),
                        provider="azure_devops",
                    )
                )
            except Exception as exc:
                logger.warning("git_providers.azure_devops work_item err=%s", exc)
        return issues

    def get_issue_impl(self, repo_id: str, issue_number: int, **kwargs: Any) -> Issue:
        org, project, _repo = self._parse_repo_id(repo_id)
        url = f"{_AZDO_ORG_API}/{org}/_apis/wit/workitems/{issue_number}"
        data = self._rest_request(url)
        fields = data.get("fields", {})
        return Issue(
            id=str(data.get("id", "")),
            number=data.get("id", 0),
            title=fields.get("System.Title", ""),
            body=fields.get("System.Description", "") or "",
            state=IssueStatus.OPEN if fields.get("System.State") not in ("Closed", "Removed") else IssueStatus.CLOSED,
            author=fields.get("System.CreatedBy", ""),
            url=data.get("_links", {}).get("html", {}).get("href", ""),
            created_at=fields.get("System.CreatedDate", ""),
            updated_at=fields.get("System.ChangedDate", ""),
            provider="azure_devops",
        )

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
        org, project, _repo = self._parse_repo_id(repo_id)
        work_item_type = (labels or ["Issue"])[0] if labels else "Issue"
        patch_ops = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": body},
            {"op": "add", "path": "/fields/System.WorkItemType", "value": work_item_type},
        ]
        if assignees:
            patch_ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assignees[0]})

        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/wit/workitems/${work_item_type}"
        data = self._rest_request(url, method="POST", body=patch_ops, is_core=False)
        fields = data.get("fields", {})
        return Issue(
            id=str(data.get("id", "")),
            number=data.get("id", 0),
            title=fields.get("System.Title", title),
            body=fields.get("System.Description", body),
            state=IssueStatus.OPEN,
            url=data.get("_links", {}).get("html", {}).get("href", ""),
            provider="azure_devops",
        )

    # ── comment operations ────────────────────────────────────────────────

    def comment_on_issue_impl(
        self,
        repo_id: str,
        issue_number: int,
        body: str,
        **kwargs: Any,
    ) -> Comment:
        org, project, _repo = self._parse_repo_id(repo_id)
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/wit/workitems/{issue_number}/comments"
        payload = {"text": body}
        data = self._rest_request(url, method="POST", body=payload)
        return Comment(
            id=str(data.get("id", "")),
            body=data.get("text", body),
            author=data.get("createdBy", {}).get("uniqueName", ""),
            created_at=data.get("createdDate", ""),
            provider="azure_devops",
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
        # Azure DevOps code search requires the project name
        org = self._organization
        if not org:
            raise NonRetryableError("Azure DevOps code search requires an organization")

        search_url = f"{_AZDO_ORG_API}/{org}/_apis/search/codesearchresults"
        payload: Dict[str, Any] = {
            "searchText": query,
            "$top": per_page,
            "$skip": (page - 1) * per_page,
        }
        if repo:
            parts = repo.split("/")
            if len(parts) >= 2:
                payload["filters"] = {"Project": [parts[0]], "Repository": [parts[-1]]}
            else:
                payload["filters"] = {"Repository": [repo]}

        data = self._rest_request(search_url, method="POST", body=payload)
        results = data.get("results", [])
        return [
            FileContent(
                path=r.get("path", ""),
                content="",
                sha="",
                repository=r.get("repository", {}).get("name", repo or ""),
                provider="azure_devops",
            )
            for r in results
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
        org, project, repo = self._parse_repo_id(repo_id)
        url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/items"
        params: Dict[str, str] = {"path": path}
        if ref:
            # Branch or commit
            params["version"] = ref
            params["versionType"] = "branch"

        try:
            # Try to get raw content
            raw_url = url
            if params:
                raw_url += "?" + urllib.parse.urlencode({**params, "download": "true"})
            content_data = self._rest_request(raw_url, params={**params, "includeContent": "true"})
            content = ""
            if isinstance(content_data, dict):
                content = content_data.get("content", "")
            elif isinstance(content_data, str):
                content = content_data
            return FileContent(
                path=path,
                content=content,
                provider="azure_devops",
            )
        except Exception as exc:
            raise NonRetryableError(f"Azure DevOps file content error: {exc}")

    # ── suggested tasks ───────────────────────────────────────────────────

    def get_suggested_tasks_impl(
        self,
        repo_id: str,
        **kwargs: Any,
    ) -> List[SuggestedTask]:
        tasks: List[SuggestedTask] = []

        # 1. Open issues (work items)
        try:
            issues = self.get_issues_impl(repo_id, per_page=10)
            for issue in issues:
                tasks.append(
                    SuggestedTask(
                        task_type=TaskType.OPEN_ISSUE,
                        title=f"Open work item #{issue.number}: {issue.title}",
                        description=issue.body[:200] if issue.body else "",
                        repository=repo_id,
                        url=issue.url,
                        priority=5 if not issue.assignees else 2,
                        metadata={"issue_number": issue.number},
                        provider="azure_devops",
                    )
                )
        except Exception as exc:
            logger.warning("git_providers.azure_devops suggested_tasks.issues err=%s", exc)

        # 2. PRs with failing policies/checks
        try:
            org, project, repo = self._parse_repo_id(repo_id)
            prs = self.get_pull_requests_impl(repo_id, state="open", per_page=10)
            for pr in prs:
                # Check PR policies / status
                try:
                    pr_url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests/{pr.number}/statuses"
                    statuses = self._rest_request(pr_url)
                    for s in statuses.get("value", []):
                        if s.get("state") == "failed":
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.FAILING_CHECKS,
                                    title=f"Failing check on PR #{pr.number}: {s.get('description', 'unknown')}",
                                    repository=repo_id,
                                    url=pr.url,
                                    priority=8,
                                    metadata={"pr_number": pr.number},
                                    provider="azure_devops",
                                )
                            )
                            break
                except Exception:
                    pass

                # Merge conflicts
                try:
                    merge_url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests/{pr.number}"
                    pr_detail = self._rest_request(merge_url)
                    if pr_detail.get("mergeFailureType"):
                        tasks.append(
                            SuggestedTask(
                                task_type=TaskType.MERGE_CONFLICT,
                                title=f"Merge conflict on PR #{pr.number}: {pr.title}",
                                repository=repo_id,
                                url=pr.url,
                                priority=7,
                                metadata={"pr_number": pr.number},
                                provider="azure_devops",
                            )
                        )
                except Exception:
                    pass

                # Unresolved comments
                try:
                    threads_url = f"{_AZDO_ORG_API}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests/{pr.number}/threads"
                    threads = self._rest_request(threads_url)
                    for t in threads.get("value", []):
                        if t.get("status") == "active":
                            tasks.append(
                                SuggestedTask(
                                    task_type=TaskType.UNRESOLVED_COMMENTS,
                                    title=f"Unresolved comment on PR #{pr.number}",
                                    repository=repo_id,
                                    url=pr.url,
                                    priority=4,
                                    metadata={"pr_number": pr.number, "thread_id": t.get("id")},
                                    provider="azure_devops",
                                )
                            )
                            break
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("git_providers.azure_devops suggested_tasks.prs err=%s", exc)

        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # ── OAuth ─────────────────────────────────────────────────────────────

    def get_auth_url_impl(self, state: Optional[str] = None, **kwargs: Any) -> str:
        params = {
            "client_id": self.auth_info.client_id,
            "redirect_uri": self.auth_info.redirect_uri,
            "response_type": "Assertion",
            "scope": " ".join(self.auth_info.scopes) if self.auth_info.scopes else "vso.code vso.work",
        }
        if state:
            params["state"] = state
        return f"{_AZDO_OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def handle_callback_impl(self, code: str, state: str = "", **kwargs: Any) -> AuthInfo:
        payload = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self.auth_info.client_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": code,
            "redirect_uri": self.auth_info.redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(_AZDO_OAUTH_TOKEN, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise NonRetryableError(f"Azure DevOps OAuth callback failed: {exc}")

        new_auth = AuthInfo(
            auth_type=AuthType.OAUTH,
            token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
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
        WebhookEvent.PUSH: "git.push",
        WebhookEvent.PULL_REQUEST: "git.pullrequest.created",
        WebhookEvent.ISSUE: "workitem.created",
        WebhookEvent.COMMENT: "workitem.comment",
    }

    def register_webhook_impl(
        self, repo_id: str, config: WebhookConfig, **kwargs: Any
    ) -> WebhookConfig:
        org, project, repo = self._parse_repo_id(repo_id)
        events = [
            self._WEBHOOK_EVENT_MAP.get(e, "git.push") for e in config.events
        ] or ["git.push"]
        payload = {
            "eventType": events[0] if len(events) == 1 else "*",
            "publisherId": "tfs",
            "resourceVersion": "1.0",
            "consumerId": "webHooks",
            "consumerActionId": "httpRequest",
            "publisherInputs": {
                "projectId": project,
                "repository": repo,
            },
            "consumerInputs": {
                "url": config.url,
                "httpHeaders": f"X-Webhook-Secret:{config.secret}" if config.secret else "",
            },
        }
        url = f"{_AZDO_ORG_API}/{org}/_apis/hooks/subscriptions"
        data = self._rest_request(url, method="POST", body=payload)
        config.webhook_id = str(data.get("id", ""))
        config.provider = "azure_devops"
        return config

    def delete_webhook_impl(self, repo_id: str, webhook_id: str, **kwargs: Any) -> bool:
        org = self._organization
        if not org:
            return False
        url = f"{_AZDO_ORG_API}/{org}/_apis/hooks/subscriptions/{webhook_id}"
        try:
            self._rest_request(url, method="DELETE")
            return True
        except NonRetryableError:
            return False

    # ── utility ───────────────────────────────────────────────────────────

    def validate_token_impl(self) -> bool:
        if not self._organization:
            return False
        try:
            self._rest_request(f"{_AZDO_ORG_API}/{self._organization}/_apis/projects", params={"$top": "1"})
            return True
        except Exception:
            return False

    def get_authenticated_user_impl(self) -> Dict[str, Any]:
        url = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"
        data = self._rest_request(url, params={"api-version": "6.0"})
        return {
            "login": data.get("coreAttributes", {}).get("Account", {}).get("value", ""),
            "name": data.get("coreAttributes", {}).get("DisplayName", {}).get("value", ""),
            "email": data.get("coreAttributes", {}).get("Mail", {}).get("value", ""),
            "id": data.get("id", ""),
            "provider": "azure_devops",
        }

    # ── private helpers ───────────────────────────────────────────────────

    def _parse_repo_id(self, repo_id: str) -> tuple:
        """Parse ``org/project/repo`` format, filling gaps from instance state."""
        parts = repo_id.split("/")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return self._organization, parts[0], parts[1]
        return self._organization, parts[0] if len(parts) == 1 else "", repo_id


def resp_headers_get(exc: urllib.error.HTTPError, name: str) -> Optional[str]:
    """Safely get a response header from an HTTPError."""
    return exc.headers.get(name)
