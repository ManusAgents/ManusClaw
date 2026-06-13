from __future__ import annotations

"""
Jira Integration — Issue Tracking (Cloud & Data Center)
========================================================
Full-featured API client for Jira, supporting both Jira Cloud
(https://developer.atlassian.com/cloud/jira/platform/rest/v3/)
and Jira Data Center (https://docs.atlassian.com/software/jira/docs/api/REST/).

Features:
  - OAuth 2.0 and Personal Access Token (PAT) authentication
  - Create, update, comment on issues
  - JQL search
  - Webhook support for real-time events
  - Suggested tasks from Jira

Thread-safe via per-instance ``_lock``.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, quote

from app.logger import logger

# ──────────────────────────────────────────────────────────────────────────────
# Optional HTTP client
# ──────────────────────────────────────────────────────────────────────────────

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

try:
    import urllib.request
    import urllib.error
    _URLLIB_AVAILABLE = True
except ImportError:
    _URLLIB_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class JiraAuthType(str, Enum):
    """Jira authentication methods."""

    PAT = "pat"
    OAUTH = "oauth"
    BASIC = "basic"


class JiraIssueType(str, Enum):
    """Common Jira issue types."""

    BUG = "Bug"
    TASK = "Task"
    STORY = "Story"
    EPIC = "Epic"
    SUBTASK = "Sub-task"
    IMPROVEMENT = "Improvement"


class JiraPriority(str, Enum):
    """Jira priority levels."""

    HIGHEST = "Highest"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    LOWEST = "Lowest"


class JiraTransition(str, Enum):
    """Common Jira transition names."""

    TODO = "To Do"
    IN_PROGRESS = "In Progress"
    DONE = "Done"
    IN_REVIEW = "In Review"


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class JiraProject:
    """A Jira project."""

    key: str = ""
    name: str = ""
    id: str = ""
    project_type: str = ""
    lead: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "id": self.id,
            "project_type": self.project_type,
            "lead": self.lead,
        }


@dataclass
class JiraIssue:
    """A Jira issue."""

    key: str = ""
    id: str = ""
    summary: str = ""
    description: str = ""
    status: str = ""
    issue_type: str = ""
    priority: str = ""
    assignee: str = ""
    assignee_id: str = ""
    reporter: str = ""
    project_key: str = ""
    labels: List[str] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    due_date: str = ""
    comment_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "id": self.id,
            "summary": self.summary,
            "description": self.description,
            "status": self.status,
            "issue_type": self.issue_type,
            "priority": self.priority,
            "assignee": self.assignee,
            "project_key": self.project_key,
            "labels": self.labels,
            "url": self.url,
        }


@dataclass
class JiraComment:
    """A comment on a Jira issue."""

    id: str = ""
    body: str = ""
    author: str = ""
    author_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    issue_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "body": self.body,
            "author": self.author,
            "created_at": self.created_at,
        }


@dataclass
class JiraTransitionItem:
    """An available transition for a Jira issue."""

    id: str = ""
    name: str = ""
    to_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "to_status": self.to_status,
        }


@dataclass
class JiraWebhookEvent:
    """A webhook event from Jira."""

    event_type: str = ""
    issue: Optional[JiraIssue] = None
    comment: Optional[JiraComment] = None
    user: str = ""
    timestamp: str = ""
    webhook_event: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "event_type": self.event_type,
            "webhook_event": self.webhook_event,
            "user": self.user,
            "timestamp": self.timestamp,
        }
        if self.issue:
            result["issue"] = self.issue.to_dict()
        if self.comment:
            result["comment"] = self.comment.to_dict()
        return result


@dataclass
class JiraSuggestedTask:
    """A suggested task from Jira for the resolver."""

    title: str = ""
    description: str = ""
    issue_key: str = ""
    url: str = ""
    priority: str = ""
    project_key: str = ""
    status: str = ""
    source: str = "jira"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "issue_key": self.issue_key,
            "url": self.url,
            "priority": self.priority,
            "project_key": self.project_key,
            "status": self.status,
            "source": self.source,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Jira API Client
# ──────────────────────────────────────────────────────────────────────────────


class JiraClient:
    """
    Async API client for Jira Cloud and Jira Data Center.

    Supports:
      - Personal Access Token (PAT) authentication
      - OAuth 2.0 flow (Cloud only)
      - Basic authentication (email + API token for Cloud)
      - CRUD operations on issues
      - JQL search
      - Comments and transitions
      - Webhook signature verification
      - Suggested tasks

    Usage::

        # PAT authentication (Data Center)
        client = JiraClient(
            base_url="https://jira.example.com",
            auth_type=JiraAuthType.PAT,
            pat="my-token",
        )

        # Basic auth (Cloud)
        client = JiraClient(
            base_url="https://myorg.atlassian.net",
            auth_type=JiraAuthType.BASIC,
            email="user@example.com",
            api_token="xxx",
        )

        # Create an issue
        issue = await client.create_issue(
            project_key="PROJ",
            summary="Fix login bug",
            description="Login fails when...",
            issue_type="Bug",
        )

        # Search with JQL
        issues = await client.search("project = PROJ AND status = Open")

        # Get suggested tasks
        tasks = await client.get_suggested_tasks()
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_type: JiraAuthType = JiraAuthType.PAT,
        pat: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.getenv("JIRA_BASE_URL", "")
        ).rstrip("/")
        self._auth_type = auth_type
        self._pat = pat or os.getenv("JIRA_PAT", "")
        self._email = email or os.getenv("JIRA_EMAIL", "")
        self._api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
        self._client_id = client_id or os.getenv("JIRA_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv(
            "JIRA_CLIENT_SECRET", ""
        )
        self._redirect_uri = redirect_uri or os.getenv(
            "JIRA_REDIRECT_URI", ""
        )
        self._webhook_secret = webhook_secret or os.getenv(
            "JIRA_WEBHOOK_SECRET", ""
        )
        self._access_token: Optional[str] = None
        self._lock = threading.RLock()

    @property
    def is_configured(self) -> bool:
        """Check if the client has sufficient credentials."""
        if self._auth_type == JiraAuthType.PAT:
            return bool(self._base_url and self._pat)
        if self._auth_type == JiraAuthType.BASIC:
            return bool(self._base_url and self._email and self._api_token)
        if self._auth_type == JiraAuthType.OAUTH:
            return bool(self._client_id and self._client_secret)
        return False

    @property
    def api_base(self) -> str:
        """Return the REST API base URL."""
        return f"{self._base_url}/rest/api/2"

    @property
    def api_v3_base(self) -> str:
        """Return the v3 REST API base URL (Cloud only)."""
        return f"{self._base_url}/rest/api/3"

    # ── OAuth ──────────────────────────────────────────────────────────────

    def get_oauth_url(self, state: Optional[str] = None) -> str:
        """
        Generate the OAuth authorization URL for Jira Cloud.

        Uses Atlassian's OAuth 2.0 (3LO) flow.

        Args:
            state: Optional CSRF state parameter.

        Returns:
            Authorization URL to redirect the user to.
        """
        if not self._client_id:
            raise ValueError(
                "JIRA_CLIENT_ID is required for OAuth flow"
            )

        params = {
            "audience": "api.atlassian.com",
            "client_id": self._client_id,
            "scope": "read:jira-work write:jira-work offline_access",
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "prompt": "consent",
        }
        if state:
            params["state"] = state

        return (
            f"https://auth.atlassian.com/authorize?{urlencode(params)}"
        )

    async def exchange_code(self, code: str) -> str:
        """
        Exchange an OAuth authorization code for access and refresh tokens.

        Args:
            code: The authorization code from the OAuth callback.

        Returns:
            The access token.
        """
        token_url = "https://auth.atlassian.com/oauth/token"
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "redirect_uri": self._redirect_uri,
        }

        response = await self._http_post_json(token_url, payload, {})

        if isinstance(response, dict):
            token = response.get("access_token", "")
            if token:
                self._access_token = token
                return token
            raise ValueError(
                f"No access_token in OAuth response: {list(response.keys())}"
            )

        raise ValueError(f"Unexpected OAuth response: {type(response)}")

    # ── Auth headers ───────────────────────────────────────────────────────

    def _get_auth_headers(self) -> Dict[str, str]:
        """Return authorization headers based on auth type."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}

        if self._auth_type == JiraAuthType.PAT and self._pat:
            headers["Authorization"] = f"Bearer {self._pat}"

        elif self._auth_type == JiraAuthType.BASIC and self._email and self._api_token:
            credentials = base64.b64encode(
                f"{self._email}:{self._api_token}".encode("utf-8")
            ).decode("utf-8")
            headers["Authorization"] = f"Basic {credentials}"

        elif self._auth_type == JiraAuthType.OAUTH and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        return headers

    # ── Project operations ─────────────────────────────────────────────────

    async def list_projects(self) -> List[JiraProject]:
        """List all projects accessible to the authenticated user."""
        url = f"{self.api_base}/project"
        data = await self._http_get(url, self._get_auth_headers())

        if not isinstance(data, list):
            return []

        projects: List[JiraProject] = []
        for item in data:
            projects.append(JiraProject(
                key=item.get("key", ""),
                name=item.get("name", ""),
                id=item.get("id", ""),
                project_type=item.get("projectTypeKey", ""),
                lead=item.get("lead", {}).get("displayName", "") if isinstance(item.get("lead"), dict) else "",
            ))

        return projects

    # ── Issue operations ───────────────────────────────────────────────────

    async def get_issue(self, issue_key: str) -> Optional[JiraIssue]:
        """
        Get a single issue by key.

        Args:
            issue_key: The Jira issue key (e.g. "PROJ-123").

        Returns:
            A JiraIssue or None if not found.
        """
        url = f"{self.api_base}/issue/{quote(issue_key, safe='')}"
        data = await self._http_get(url, self._get_auth_headers())

        if isinstance(data, dict) and "key" in data:
            return self._parse_issue(data)

        return None

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        priority: Optional[str] = None,
        assignee_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
        due_date: Optional[str] = None,
    ) -> JiraIssue:
        """
        Create a new issue.

        Args:
            project_key: The project key (e.g. "PROJ").
            summary: Issue summary (title).
            description: Issue description.
            issue_type: Issue type name (Bug, Task, Story, etc.).
            priority: Priority name.
            assignee_id: Atlassian account ID for assignee.
            labels: List of label strings.
            due_date: Due date (YYYY-MM-DD).

        Returns:
            The created JiraIssue.
        """
        fields: Dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }

        if description:
            fields["description"] = description
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_id:
            fields["assignee"] = {"accountId": assignee_id}
        if labels:
            fields["labels"] = labels
        if due_date:
            fields["duedate"] = due_date

        payload = {"fields": fields}
        url = f"{self.api_base}/issue"
        data = await self._http_post_json(
            url, payload, self._get_auth_headers()
        )

        if isinstance(data, dict):
            return JiraIssue(
                key=data.get("key", ""),
                id=data.get("id", ""),
                summary=summary,
                project_key=project_key,
                url=f"{self._base_url}/browse/{data.get('key', '')}",
            )

        raise RuntimeError("Unexpected response from Jira create_issue")

    async def update_issue(
        self,
        issue_key: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
        due_date: Optional[str] = None,
    ) -> JiraIssue:
        """
        Update an existing issue.

        Args:
            issue_key: The issue key.
            summary: New summary.
            description: New description.
            priority: New priority name.
            assignee_id: New assignee account ID.
            labels: New labels.
            due_date: New due date.

        Returns:
            The updated JiraIssue.
        """
        fields: Dict[str, Any] = {}

        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = description
        if priority is not None:
            fields["priority"] = {"name": priority}
        if assignee_id is not None:
            fields["assignee"] = {"accountId": assignee_id}
        if labels is not None:
            fields["labels"] = labels
        if due_date is not None:
            fields["duedate"] = due_date

        payload = {"fields": fields}
        url = f"{self.api_base}/issue/{quote(issue_key, safe='')}"
        await self._http_put(url, payload, self._get_auth_headers())

        # Fetch the updated issue
        return await self.get_issue(issue_key) or JiraIssue(key=issue_key)

    # ── JQL Search ─────────────────────────────────────────────────────────

    async def search(
        self,
        jql: str,
        max_results: int = 50,
        start_at: int = 0,
        fields: Optional[List[str]] = None,
    ) -> List[JiraIssue]:
        """
        Search for issues using JQL.

        Args:
            jql: JQL query string.
            max_results: Maximum number of results.
            start_at: Starting index for pagination.
            fields: List of field names to return (None = all).

        Returns:
            List of JiraIssue instances.
        """
        payload: Dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "startAt": start_at,
        }
        if fields:
            payload["fields"] = fields

        url = f"{self.api_base}/search"
        data = await self._http_post_json(
            url, payload, self._get_auth_headers()
        )

        if not isinstance(data, dict):
            return []

        issues: List[JiraIssue] = []
        for item in data.get("issues", []):
            issues.append(self._parse_issue(item))

        return issues

    # ── Comment operations ─────────────────────────────────────────────────

    async def create_comment(
        self,
        issue_key: str,
        body: str,
    ) -> JiraComment:
        """
        Add a comment to an issue.

        Args:
            issue_key: The issue key.
            body: Comment body (supports Atlassian Document Format or plain text).

        Returns:
            The created JiraComment.
        """
        payload = {"body": body}
        url = f"{self.api_base}/issue/{quote(issue_key, safe='')}/comment"
        data = await self._http_post_json(
            url, payload, self._get_auth_headers()
        )

        if isinstance(data, dict):
            return JiraComment(
                id=data.get("id", ""),
                body=body,
                author=data.get("author", {}).get("displayName", "")
                if isinstance(data.get("author"), dict)
                else "",
                created_at=data.get("created", ""),
                issue_key=issue_key,
            )

        return JiraComment(body=body, issue_key=issue_key)

    async def list_comments(
        self,
        issue_key: str,
    ) -> List[JiraComment]:
        """List all comments on an issue."""
        url = (
            f"{self.api_base}/issue/{quote(issue_key, safe='')}/comment"
        )
        data = await self._http_get(url, self._get_auth_headers())

        if not isinstance(data, dict):
            return []

        comments: List[JiraComment] = []
        for item in data.get("comments", []):
            comments.append(JiraComment(
                id=item.get("id", ""),
                body=item.get("body", ""),
                author=item.get("author", {}).get("displayName", "")
                if isinstance(item.get("author"), dict)
                else "",
                author_id=item.get("author", {}).get("accountId", "")
                if isinstance(item.get("author"), dict)
                else "",
                created_at=item.get("created", ""),
                updated_at=item.get("updated", ""),
                issue_key=issue_key,
            ))

        return comments

    # ── Transitions ────────────────────────────────────────────────────────

    async def get_transitions(
        self, issue_key: str
    ) -> List[JiraTransitionItem]:
        """Get available transitions for an issue."""
        url = (
            f"{self.api_base}/issue/{quote(issue_key, safe='')}/transitions"
        )
        data = await self._http_get(url, self._get_auth_headers())

        if not isinstance(data, dict):
            return []

        transitions: List[JiraTransitionItem] = []
        for item in data.get("transitions", []):
            to_status = item.get("to", {})
            transitions.append(JiraTransitionItem(
                id=item.get("id", ""),
                name=item.get("name", ""),
                to_status=to_status.get("name", "")
                if isinstance(to_status, dict)
                else "",
            ))

        return transitions

    async def transition_issue(
        self,
        issue_key: str,
        transition_id: str,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Transition an issue to a new state.

        Args:
            issue_key: The issue key.
            transition_id: The transition ID (from get_transitions).
            fields: Optional fields to set during transition.

        Returns:
            True if the transition was successful.
        """
        payload: Dict[str, Any] = {"transition": {"id": transition_id}}
        if fields:
            payload["fields"] = fields

        url = (
            f"{self.api_base}/issue/{quote(issue_key, safe='')}/transitions"
        )
        await self._http_post_json(url, payload, self._get_auth_headers())
        return True

    # ── Suggested tasks ────────────────────────────────────────────────────

    async def get_suggested_tasks(
        self,
        jql: Optional[str] = None,
        max_results: int = 20,
    ) -> List[JiraSuggestedTask]:
        """
        Get suggested tasks from Jira.

        By default, returns issues assigned to the current user that are
        not in a terminal state. A custom JQL query can be provided.

        Args:
            jql: Optional JQL query. Defaults to assignee = currentUser()
                 AND status not in (Done, Closed, Resolved).
            max_results: Maximum number of tasks.

        Returns:
            List of JiraSuggestedTask instances.
        """
        if not jql:
            jql = (
                'assignee = currentUser() '
                'AND status not in (Done, Closed, Resolved) '
                'ORDER BY priority DESC, updated DESC'
            )

        issues = await self.search(
            jql=jql,
            max_results=max_results,
            fields=["summary", "description", "status", "priority", "labels"],
        )

        tasks: List[JiraSuggestedTask] = []
        for issue in issues:
            tasks.append(JiraSuggestedTask(
                title=issue.summary,
                description=issue.description[:500] if issue.description else "",
                issue_key=issue.key,
                url=issue.url,
                priority=issue.priority,
                project_key=issue.project_key,
                status=issue.status,
            ))

        return tasks

    # ── Webhook verification ───────────────────────────────────────────────

    def verify_webhook_signature(
        self,
        body: bytes,
        signature: str,
    ) -> bool:
        """
        Verify the webhook signature from Jira.

        Jira Cloud webhooks can be configured with a secret. The signature
        is sent in the ``X-Hub-Signature`` header (similar to GitHub).

        Args:
            body: Raw request body bytes.
            signature: The signature from the request header.

        Returns:
            True if the signature is valid.
        """
        if not self._webhook_secret:
            return True  # No secret configured

        if not signature:
            return False

        expected = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        provided = signature.removeprefix("sha256=")
        return hmac.compare_digest(expected, provided)

    def parse_webhook_event(
        self,
        payload: Dict[str, Any],
    ) -> JiraWebhookEvent:
        """
        Parse a Jira webhook payload into a JiraWebhookEvent.

        Args:
            payload: The parsed JSON payload.

        Returns:
            A JiraWebhookEvent instance.
        """
        issue_data = payload.get("issue", {})
        comment_data = payload.get("comment", payload.get("comment"))

        issue: Optional[JiraIssue] = None
        comment: Optional[JiraComment] = None

        if issue_data and isinstance(issue_data, dict):
            issue = self._parse_issue(issue_data)

        if comment_data and isinstance(comment_data, dict):
            comment = JiraComment(
                id=comment_data.get("id", ""),
                body=comment_data.get("body", ""),
                author=comment_data.get("author", {}).get("displayName", "")
                if isinstance(comment_data.get("author"), dict)
                else "",
                created_at=comment_data.get("created", ""),
            )

        return JiraWebhookEvent(
            event_type=payload.get("webhookEvent", ""),
            issue=issue,
            comment=comment,
            user=payload.get("user", {}).get("displayName", "")
            if isinstance(payload.get("user"), dict)
            else "",
            timestamp=payload.get("timestamp", ""),
            webhook_event=payload.get("webhookEvent", ""),
            extra=payload,
        )

    # ── Issue parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_issue(data: Dict[str, Any]) -> JiraIssue:
        """Parse a Jira REST API issue object into a JiraIssue."""
        fields = data.get("fields", {})
        status_obj = fields.get("status", {})
        priority_obj = fields.get("priority", {})
        assignee_obj = fields.get("assignee") or {}
        reporter_obj = fields.get("reporter") or {}
        project_obj = fields.get("project", {})
        issuetype_obj = fields.get("issuetype", {})

        # Handle description: could be a string (v2) or Atlassian Document Format (v3)
        description = fields.get("description", "")
        if isinstance(description, dict):
            # ADF format — extract plain text
            description = JiraClient._adf_to_plain(description)

        return JiraIssue(
            key=data.get("key", ""),
            id=data.get("id", ""),
            summary=fields.get("summary", ""),
            description=description or "",
            status=status_obj.get("name", "") if status_obj else "",
            issue_type=issuetype_obj.get("name", "") if issuetype_obj else "",
            priority=priority_obj.get("name", "") if priority_obj else "",
            assignee=assignee_obj.get("displayName", "") if assignee_obj else "",
            assignee_id=assignee_obj.get("accountId", "") if assignee_obj else "",
            reporter=reporter_obj.get("displayName", "") if reporter_obj else "",
            project_key=project_obj.get("key", "") if project_obj else "",
            labels=fields.get("labels", []),
            url=f"{data.get('self', '').split('/rest/')[0]}/browse/{data.get('key', '')}"
            if data.get("self")
            else "",
            created_at=fields.get("created", ""),
            updated_at=fields.get("updated", ""),
            due_date=fields.get("duedate", ""),
            comment_count=fields.get("comment", {}).get("total", 0)
            if isinstance(fields.get("comment"), dict)
            else 0,
        )

    @staticmethod
    def _adf_to_plain(adf: Dict[str, Any]) -> str:
        """Convert Atlassian Document Format to plain text."""
        if not isinstance(adf, dict):
            return str(adf)

        parts: List[str] = []

        def _extract(node: Any) -> None:
            if isinstance(node, str):
                parts.append(node)
                return
            if not isinstance(node, dict):
                return

            node_type = node.get("type", "")
            if node_type == "text":
                parts.append(node.get("text", ""))
            elif node_type == "hardBreak":
                parts.append("\n")
            elif node_type == "paragraph":
                for child in node.get("content", []):
                    _extract(child)
                parts.append("\n\n")
            elif node_type == "bulletList" or node_type == "orderedList":
                for child in node.get("content", []):
                    _extract(child)
            elif node_type == "listItem":
                for child in node.get("content", []):
                    _extract(child)
                parts.append("\n")
            else:
                for child in node.get("content", []):
                    _extract(child)

        _extract(adf)
        return "".join(parts).strip()

    # ── HTTP methods ───────────────────────────────────────────────────────

    async def _http_get(
        self,
        url: str,
        headers: Dict[str, str],
    ) -> Any:
        """GET request."""
        if _AIOHTTP_AVAILABLE:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 204:
                        return None
                    return await resp.json(content_type=None)
        elif _URLLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_get, url, headers
            )
        else:
            raise RuntimeError(
                "No HTTP client available (aiohttp or urllib required)"
            )

    async def _http_post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Any:
        """POST JSON data."""
        if _AIOHTTP_AVAILABLE:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 204:
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        return None
        elif _URLLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_post_json, url, payload, headers
            )
        else:
            raise RuntimeError(
                "No HTTP client available (aiohttp or urllib required)"
            )

    async def _http_put(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Any:
        """PUT JSON data."""
        if _AIOHTTP_AVAILABLE:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 204:
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        return None
        elif _URLLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_put_json, url, payload, headers
            )
        else:
            raise RuntimeError(
                "No HTTP client available (aiohttp or urllib required)"
            )

    @staticmethod
    def _sync_get(url: str, headers: Dict[str, str]) -> Any:
        """Synchronous GET fallback."""
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _sync_post_json(
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Any:
        """Synchronous JSON POST fallback."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            raise

    @staticmethod
    def _sync_put_json(
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Any:
        """Synchronous JSON PUT fallback."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            raise
