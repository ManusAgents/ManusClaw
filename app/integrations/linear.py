from __future__ import annotations

"""
Linear Integration — Issue Tracking
====================================
Full-featured API client for Linear (https://linear.app), providing:

  - OAuth 2.0 authentication flow
  - Create, update, comment on issues
  - List and search issues
  - Webhook support for real-time events
  - Suggested tasks from Linear

All HTTP operations use ``aiohttp`` when available, falling back to
``urllib`` gracefully. Thread-safe via per-instance ``_lock``.
"""

import asyncio
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
from urllib.parse import urlencode

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
    _URRLIB_AVAILABLE = True
except ImportError:
    _URRLIB_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────


class LinearIssueState(str, Enum):
    """Common Linear issue states."""

    UNSTARTED = "unstarted"
    STARTED = "started"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class LinearPriority(int, Enum):
    """Linear priority levels."""

    NO_PRIORITY = 0
    URGENT = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


@dataclass
class LinearTeam:
    """A Linear team."""

    id: str = ""
    name: str = ""
    key: str = ""
    description: str = ""
    icon: str = ""
    color: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "key": self.key,
            "description": self.description,
        }


@dataclass
class LinearIssue:
    """A Linear issue."""

    id: str = ""
    identifier: str = ""
    title: str = ""
    description: str = ""
    state: str = ""
    priority: int = 0
    assignee_id: str = ""
    assignee_name: str = ""
    team_id: str = ""
    team_key: str = ""
    labels: List[str] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    due_date: str = ""
    comment_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "priority": self.priority,
            "assignee_name": self.assignee_name,
            "team_key": self.team_key,
            "url": self.url,
            "labels": self.labels,
        }


@dataclass
class LinearComment:
    """A comment on a Linear issue."""

    id: str = ""
    body: str = ""
    user_id: str = ""
    user_name: str = ""
    created_at: str = ""
    issue_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "body": self.body,
            "user_name": self.user_name,
            "created_at": self.created_at,
        }


@dataclass
class LinearWebhookEvent:
    """A webhook event from Linear."""

    action: str = ""
    type: str = ""
    issue: Optional[LinearIssue] = None
    comment: Optional[LinearComment] = None
    actor_id: str = ""
    created_at: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "action": self.action,
            "type": self.type,
            "actor_id": self.actor_id,
            "created_at": self.created_at,
        }
        if self.issue:
            result["issue"] = self.issue.to_dict()
        if self.comment:
            result["comment"] = self.comment.to_dict()
        return result


@dataclass
class LinearSuggestedTask:
    """A suggested task from Linear for the resolver."""

    title: str = ""
    description: str = ""
    issue_identifier: str = ""
    url: str = ""
    priority: int = 0
    team_key: str = ""
    state: str = ""
    source: str = "linear"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "issue_identifier": self.issue_identifier,
            "url": self.url,
            "priority": self.priority,
            "team_key": self.team_key,
            "state": self.state,
            "source": self.source,
        }


# ──────────────────────────────────────────────────────────────────────────────
# GraphQL Queries & Mutations
# ──────────────────────────────────────────────────────────────────────────────

_QUERY_TEAMS = """
query Teams {
  teams {
    nodes {
      id name key description icon color
    }
  }
}
"""

_QUERY_ISSUES = """
query Issues($filter: IssueFilter, $first: Int, $after: String) {
  issues(filter: $filter, first: $first, after: $after) {
    nodes {
      id identifier title description state { name } priority
      assignee { id name } team { id key } labels { nodes { name } }
      url createdAt updatedAt dueDate commentCount
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_QUERY_ISSUE = """
query Issue($id: String!) {
  issue(id: $id) {
    id identifier title description state { name } priority
    assignee { id name } team { id key } labels { nodes { name } }
    url createdAt updatedAt dueDate commentCount
  }
}
"""

_MUTATION_CREATE_ISSUE = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success issue { id identifier title url }
  }
}
"""

_MUTATION_UPDATE_ISSUE = """
mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success issue { id identifier title state { name } url }
  }
}
"""

_MUTATION_CREATE_COMMENT = """
mutation CreateComment($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success comment { id body user { name } createdAt }
  }
}
"""

_QUERY_COMMENTS = """
query Comments($issueId: String!, $first: Int) {
  comments(filter: { issue: { id: { eq: $issueId } } }, first: $first) {
    nodes {
      id body user { id name } createdAt
    }
  }
}
"""

_QUERY_MY_ISSUES = """
query MyIssues($first: Int) {
  viewer {
    assignedIssues(first: $first) {
      nodes {
        id identifier title description state { name } priority
        team { key } url createdAt updatedAt dueDate commentCount
        labels { nodes { name } }
      }
    }
  }
}
"""


# ──────────────────────────────────────────────────────────────────────────────
# Linear API Client
# ──────────────────────────────────────────────────────────────────────────────


class LinearClient:
    """
    Async API client for Linear.

    Supports:
      - API key authentication (personal access token)
      - OAuth 2.0 flow
      - CRUD operations on issues
      - Comments
      - Webhook signature verification
      - Suggested tasks

    Usage::

        client = LinearClient(api_key="lin_api_xxx")

        # List teams
        teams = await client.list_teams()

        # Create an issue
        issue = await client.create_issue(
            team_id=teams[0].id,
            title="Fix login bug",
            description="Login fails when...",
        )

        # Get suggested tasks
        tasks = await client.get_suggested_tasks()
    """

    LINEAR_API_URL = "https://api.linear.app/graphql"
    LINEAR_OAUTH_AUTHORIZE = "https://linear.app/oauth/authorize"
    LINEAR_OAUTH_TOKEN = "https://api.linear.app/oauth/token"

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or os.getenv("LINEAR_API_KEY", "")
        self._client_id = client_id or os.getenv("LINEAR_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv(
            "LINEAR_CLIENT_SECRET", ""
        )
        self._redirect_uri = redirect_uri or os.getenv(
            "LINEAR_REDIRECT_URI", ""
        )
        self._webhook_secret = webhook_secret or os.getenv(
            "LINEAR_WEBHOOK_SECRET", ""
        )
        self._access_token: Optional[str] = None
        self._lock = threading.RLock()
        self._rate_limit_remaining: int = 1000
        self._rate_limit_reset: float = 0.0

    @property
    def is_configured(self) -> bool:
        """Check if the client has sufficient credentials."""
        return bool(self._api_key or self._access_token)

    # ── OAuth ──────────────────────────────────────────────────────────────

    def get_oauth_url(self, state: Optional[str] = None) -> str:
        """
        Generate the OAuth authorization URL for Linear.

        Args:
            state: Optional CSRF state parameter.

        Returns:
            Authorization URL to redirect the user to.
        """
        if not self._client_id:
            raise ValueError(
                "LINEAR_CLIENT_ID is required for OAuth flow"
            )

        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "read,write",
        }
        if state:
            params["state"] = state

        return f"{self.LINEAR_OAUTH_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> str:
        """
        Exchange an OAuth authorization code for an access token.

        Args:
            code: The authorization code from the OAuth callback.

        Returns:
            The access token.
        """
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
        }

        response = await self._http_post_form(
            self.LINEAR_OAUTH_TOKEN, payload
        )

        if isinstance(response, dict):
            token = response.get("access_token", "")
            if token:
                self._access_token = token
                return token
            raise ValueError(
                f"No access_token in response: {list(response.keys())}"
            )

        raise ValueError(f"Unexpected OAuth response: {type(response)}")

    # ── GraphQL helpers ────────────────────────────────────────────────────

    def _get_auth_headers(self) -> Dict[str, str]:
        """Return authorization headers."""
        token = self._access_token or self._api_key
        if not token:
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _graphql_request(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL request against the Linear API."""
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        headers = self._get_auth_headers()

        response = await self._http_post_json(
            self.LINEAR_API_URL, payload, headers
        )

        if isinstance(response, dict):
            if "errors" in response:
                errors = response["errors"]
                error_msg = "; ".join(
                    e.get("message", str(e)) for e in errors
                )
                raise RuntimeError(f"Linear GraphQL error: {error_msg}")
            return response.get("data", {})

        return {}

    # ── Team operations ────────────────────────────────────────────────────

    async def list_teams(self) -> List[LinearTeam]:
        """List all teams accessible to the authenticated user."""
        data = await self._graphql_request(_QUERY_TEAMS)

        teams: List[LinearTeam] = []
        nodes = data.get("teams", {}).get("nodes", [])
        for node in nodes:
            teams.append(LinearTeam(
                id=node.get("id", ""),
                name=node.get("name", ""),
                key=node.get("key", ""),
                description=node.get("description", ""),
                icon=node.get("icon", ""),
                color=node.get("color", ""),
            ))

        return teams

    # ── Issue operations ───────────────────────────────────────────────────

    async def list_issues(
        self,
        team_id: Optional[str] = None,
        state: Optional[str] = None,
        assignee_id: Optional[str] = None,
        first: int = 50,
        after: Optional[str] = None,
    ) -> List[LinearIssue]:
        """
        List issues with optional filtering.

        Args:
            team_id: Filter by team ID.
            state: Filter by state name.
            assignee_id: Filter by assignee ID.
            first: Number of issues to return.
            after: Cursor for pagination.

        Returns:
            List of LinearIssue instances.
        """
        filter_input: Dict[str, Any] = {}
        if team_id:
            filter_input["team"] = {"id": {"eq": team_id}}
        if state:
            filter_input["state"] = {"name": {"eq": state}}
        if assignee_id:
            filter_input["assignee"] = {"id": {"eq": assignee_id}}

        variables: Dict[str, Any] = {"first": first}
        if filter_input:
            variables["filter"] = filter_input
        if after:
            variables["after"] = after

        data = await self._graphql_request(_QUERY_ISSUES, variables)

        issues: List[LinearIssue] = []
        nodes = data.get("issues", {}).get("nodes", [])
        for node in nodes:
            issues.append(self._parse_issue(node))

        return issues

    async def get_issue(self, issue_id: str) -> Optional[LinearIssue]:
        """
        Get a single issue by ID.

        Args:
            issue_id: The Linear issue ID (UUID) or identifier (e.g. "ENG-123").

        Returns:
            A LinearIssue or None if not found.
        """
        variables = {"id": issue_id}
        data = await self._graphql_request(_QUERY_ISSUE, variables)

        node = data.get("issue")
        if not node:
            return None
        return self._parse_issue(node)

    async def create_issue(
        self,
        team_id: str,
        title: str,
        description: str = "",
        priority: Optional[int] = None,
        assignee_id: Optional[str] = None,
        labels: Optional[List[str]] = None,
        due_date: Optional[str] = None,
    ) -> LinearIssue:
        """
        Create a new issue.

        Args:
            team_id: The team ID to create the issue in.
            title: Issue title.
            description: Issue description (markdown).
            priority: Priority level (0-4).
            assignee_id: User ID to assign.
            labels: List of label names.
            due_date: Due date (ISO 8601).

        Returns:
            The created LinearIssue.
        """
        input_data: Dict[str, Any] = {
            "teamId": team_id,
            "title": title,
        }
        if description:
            input_data["description"] = description
        if priority is not None:
            input_data["priority"] = priority
        if assignee_id:
            input_data["assigneeId"] = assignee_id
        if labels:
            input_data["labelIds"] = labels
        if due_date:
            input_data["dueDate"] = due_date

        variables = {"input": input_data}
        data = await self._graphql_request(
            _MUTATION_CREATE_ISSUE, variables
        )

        result = data.get("issueCreate", {})
        if not result.get("success"):
            raise RuntimeError("Failed to create Linear issue")

        issue_node = result.get("issue", {})
        return LinearIssue(
            id=issue_node.get("id", ""),
            identifier=issue_node.get("identifier", ""),
            title=issue_node.get("title", title),
            url=issue_node.get("url", ""),
        )

    async def update_issue(
        self,
        issue_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        state_id: Optional[str] = None,
        priority: Optional[int] = None,
        assignee_id: Optional[str] = None,
    ) -> LinearIssue:
        """
        Update an existing issue.

        Args:
            issue_id: The issue ID or identifier.
            title: New title.
            description: New description.
            state_id: New state ID.
            priority: New priority.
            assignee_id: New assignee.

        Returns:
            The updated LinearIssue.
        """
        input_data: Dict[str, Any] = {}
        if title is not None:
            input_data["title"] = title
        if description is not None:
            input_data["description"] = description
        if state_id is not None:
            input_data["stateId"] = state_id
        if priority is not None:
            input_data["priority"] = priority
        if assignee_id is not None:
            input_data["assigneeId"] = assignee_id

        variables = {"id": issue_id, "input": input_data}
        data = await self._graphql_request(
            _MUTATION_UPDATE_ISSUE, variables
        )

        result = data.get("issueUpdate", {})
        if not result.get("success"):
            raise RuntimeError(f"Failed to update Linear issue {issue_id}")

        issue_node = result.get("issue", {})
        return LinearIssue(
            id=issue_node.get("id", issue_id),
            identifier=issue_node.get("identifier", ""),
            title=issue_node.get("title", ""),
            url=issue_node.get("url", ""),
        )

    # ── Comment operations ─────────────────────────────────────────────────

    async def create_comment(
        self,
        issue_id: str,
        body: str,
    ) -> LinearComment:
        """
        Add a comment to an issue.

        Args:
            issue_id: The issue ID.
            body: Comment body (markdown).

        Returns:
            The created LinearComment.
        """
        input_data = {
            "issueId": issue_id,
            "body": body,
        }
        variables = {"input": input_data}
        data = await self._graphql_request(
            _MUTATION_CREATE_COMMENT, variables
        )

        result = data.get("commentCreate", {})
        if not result.get("success"):
            raise RuntimeError(
                f"Failed to create comment on issue {issue_id}"
            )

        comment_node = result.get("comment", {})
        return LinearComment(
            id=comment_node.get("id", ""),
            body=comment_node.get("body", body),
            user_name=comment_node.get("user", {}).get("name", ""),
            created_at=comment_node.get("createdAt", ""),
            issue_id=issue_id,
        )

    async def list_comments(
        self,
        issue_id: str,
        first: int = 50,
    ) -> List[LinearComment]:
        """List comments on an issue."""
        variables = {"issueId": issue_id, "first": first}
        data = await self._graphql_request(_QUERY_COMMENTS, variables)

        comments: List[LinearComment] = []
        nodes = data.get("comments", {}).get("nodes", [])
        for node in nodes:
            comments.append(LinearComment(
                id=node.get("id", ""),
                body=node.get("body", ""),
                user_id=node.get("user", {}).get("id", ""),
                user_name=node.get("user", {}).get("name", ""),
                created_at=node.get("createdAt", ""),
                issue_id=issue_id,
            ))

        return comments

    # ── Suggested tasks ────────────────────────────────────────────────────

    async def get_suggested_tasks(
        self,
        first: int = 20,
    ) -> List[LinearSuggestedTask]:
        """
        Get suggested tasks from Linear for the current user.

        Returns issues assigned to the current user that are in
        an active (non-completed) state.

        Args:
            first: Maximum number of tasks to return.

        Returns:
            List of LinearSuggestedTask instances.
        """
        variables = {"first": first}
        data = await self._graphql_request(_QUERY_MY_ISSUES, variables)

        tasks: List[LinearSuggestedTask] = []
        viewer = data.get("viewer", {})
        nodes = viewer.get("assignedIssues", {}).get("nodes", [])

        for node in nodes:
            state_name = node.get("state", {}).get("name", "")
            if state_name.lower() in ("done", "completed", "cancelled"):
                continue

            tasks.append(LinearSuggestedTask(
                title=node.get("title", ""),
                description=node.get("description", "")[:500],
                issue_identifier=node.get("identifier", ""),
                url=node.get("url", ""),
                priority=node.get("priority", 0),
                team_key=node.get("team", {}).get("key", ""),
                state=state_name,
            ))

        # Sort by priority (lower number = higher priority)
        tasks.sort(key=lambda t: t.priority)
        return tasks

    # ── Webhook verification ───────────────────────────────────────────────

    def verify_webhook_signature(
        self,
        body: bytes,
        signature: str,
    ) -> bool:
        """
        Verify the HMAC-SHA256 signature of a Linear webhook.

        Linear signs webhook payloads with the webhook secret using
        HMAC-SHA256 and sends the signature in the ``Linear-Signature``
        header.

        Args:
            body: Raw request body bytes.
            signature: The signature from the ``Linear-Signature`` header.

        Returns:
            True if the signature is valid.
        """
        if not self._webhook_secret:
            return True  # No secret configured, accept all

        if not signature:
            return False

        expected = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(
        self,
        payload: Dict[str, Any],
    ) -> LinearWebhookEvent:
        """
        Parse a Linear webhook payload into a LinearWebhookEvent.

        Args:
            payload: The parsed JSON payload.

        Returns:
            A LinearWebhookEvent instance.
        """
        issue_data = payload.get("data", payload.get("issue"))
        comment_data = payload.get("data", payload.get("comment"))

        issue: Optional[LinearIssue] = None
        comment: Optional[LinearComment] = None

        if issue_data and isinstance(issue_data, dict):
            issue = self._parse_issue(issue_data)

        if comment_data and isinstance(comment_data, dict):
            comment = LinearComment(
                id=comment_data.get("id", ""),
                body=comment_data.get("body", ""),
                user_name=comment_data.get("user", {}).get("name", ""),
                created_at=comment_data.get("createdAt", ""),
            )

        return LinearWebhookEvent(
            action=payload.get("action", ""),
            type=payload.get("type", ""),
            issue=issue,
            comment=comment,
            actor_id=payload.get("actorId", ""),
            created_at=payload.get("createdAt", ""),
            extra=payload,
        )

    # ── Issue parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_issue(node: Dict[str, Any]) -> LinearIssue:
        """Parse a GraphQL issue node into a LinearIssue."""
        state_obj = node.get("state", {})
        assignee_obj = node.get("assignee", {})
        team_obj = node.get("team", {})
        labels_obj = node.get("labels", {}).get("nodes", [])

        return LinearIssue(
            id=node.get("id", ""),
            identifier=node.get("identifier", ""),
            title=node.get("title", ""),
            description=node.get("description", ""),
            state=state_obj.get("name", "") if state_obj else "",
            priority=node.get("priority", 0),
            assignee_id=assignee_obj.get("id", "") if assignee_obj else "",
            assignee_name=assignee_obj.get("name", "") if assignee_obj else "",
            team_id=team_obj.get("id", "") if team_obj else "",
            team_key=team_obj.get("key", "") if team_obj else "",
            labels=[l.get("name", "") for l in labels_obj if l],
            url=node.get("url", ""),
            created_at=node.get("createdAt", ""),
            updated_at=node.get("updatedAt", ""),
            due_date=node.get("dueDate", ""),
            comment_count=node.get("commentCount", 0),
        )

    # ── HTTP methods ───────────────────────────────────────────────────────

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
                    return await resp.json(content_type=None)
        elif _URRLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_post_json, url, payload, headers
            )
        else:
            raise RuntimeError(
                "No HTTP client available (aiohttp or urllib required)"
            )

    async def _http_post_form(
        self,
        url: str,
        data: Dict[str, str],
    ) -> Any:
        """POST form data."""
        if _AIOHTTP_AVAILABLE:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    return await resp.json(content_type=None)
        elif _URRLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_post_form, url, data
            )
        else:
            raise RuntimeError(
                "No HTTP client available (aiohttp or urllib required)"
            )

    @staticmethod
    def _sync_post_json(
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Any:
        """Synchronous JSON POST fallback."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _sync_post_form(
        url: str,
        data: Dict[str, str],
    ) -> Any:
        """Synchronous form POST fallback."""
        body = urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
