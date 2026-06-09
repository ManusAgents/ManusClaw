"""ManusClaw Email Tool.

Agent tool for sending, reading, searching, and replying to emails.
Extends BaseTool from ``app.tool.base``.

Tool name: ``email``

Actions:
    - ``send(to, subject, body)`` — Send an email
    - ``read(label, max)`` — Read emails from a label
    - ``search(query)`` — Search emails
    - ``reply(message_id, body)`` — Reply to a specific email

Requires Google API credentials for full functionality. Falls back to stub
mode when credentials are not available.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from app.logger import logger
from app.schema import ToolResult
from app.tool.base import BaseTool

# ─── Optional Imports ────────────────────────────────────────────────────────

try:
    from google.oauth2 import service_account
    from googleapiclient import discovery, errors as google_errors
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False

# ─── Configuration ─────────────────────────────────────────────────────────

_CREDENTIALS_PATH: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
_USER_ADDRESS: str = os.getenv("GMAIL_USER_ADDRESS", "me")

# Gmail scopes for read + send
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


class EmailTool(BaseTool):
    """Agent tool for email operations.

    Supports sending, reading, searching, and replying to emails via the
    Gmail API. Operates in stub mode when credentials are not configured.

    Tool name: ``email``
    """

    name: str = "email"
    description: str = (
        "Send, read, search, and reply to emails. "
        "Actions: send(to, subject, body), read(label, max), search(query), reply(message_id, body). "
        "Requires Gmail API credentials; operates in stub mode without them."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "read", "search", "reply"],
                "description": "The email action to perform.",
            },
            "to": {
                "type": "string",
                "description": "Recipient email address (for 'send' action).",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line (for 'send' action).",
            },
            "body": {
                "type": "string",
                "description": "Email body text (for 'send' and 'reply' actions).",
            },
            "message_id": {
                "type": "string",
                "description": "Gmail message ID to reply to (for 'reply' action).",
            },
            "label": {
                "type": "string",
                "description": "Label to read from (for 'read' action, default: INBOX).",
            },
            "max": {
                "type": "integer",
                "description": "Maximum number of emails to return (for 'read' action, default: 10).",
            },
            "query": {
                "type": "string",
                "description": "Gmail search query (for 'search' action).",
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._service: Optional[Any] = None
        self._stub_mode: bool = not self._is_configured()

        if self._stub_mode:
            logger.info("[EmailTool] Operating in stub mode — no Gmail credentials")

    @property
    def is_stub(self) -> bool:
        """Whether the tool is operating in stub mode."""
        return self._stub_mode

    def _is_configured(self) -> bool:
        """Check if Gmail API credentials are available."""
        if not _HAS_GOOGLE:
            return False
        return bool(_CREDENTIALS_PATH or _SERVICE_ACCOUNT_JSON)

    def _get_service(self) -> Any:
        """Lazily create and cache the Gmail API service."""
        if self._service is not None:
            return self._service

        if _CREDENTIALS_PATH:
            creds = service_account.Credentials.from_service_account_file(
                _CREDENTIALS_PATH, scopes=_SCOPES
            )
        elif _SERVICE_ACCOUNT_JSON:
            creds_info = json.loads(_SERVICE_ACCOUNT_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_info, scopes=_SCOPES
            )
        else:
            raise RuntimeError("No Gmail credentials configured")

        self._service = discovery.build("gmail", "v1", credentials=creds)
        return self._service

    # ─── Execute ─────────────────────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute an email action.

        Args:
            action: One of ``send``, ``read``, ``search``, ``reply``.
            to: Recipient email (for send).
            subject: Subject line (for send).
            body: Email body (for send/reply).
            message_id: Gmail message ID (for reply).
            label: Label to read from (for read).
            max: Max results (for read).
            query: Search query (for search).

        Returns:
            ToolResult with output or error.
        """
        action = kwargs.get("action", "")

        if self._stub_mode:
            return self._stub_execute(action, kwargs)

        try:
            handler = {
                "send": self._action_send,
                "read": self._action_read,
                "search": self._action_search,
                "reply": self._action_reply,
            }.get(action)

            if handler is None:
                return ToolResult(error=f"Unknown action: {action}. Use: send, read, search, reply")

            return await handler(kwargs)

        except google_errors.HttpError as exc:
            error_msg = f"Gmail API HTTP {exc.resp.status}: {exc._get_reason()}"
            logger.error(f"[EmailTool] {error_msg}")
            return ToolResult(error=error_msg)
        except Exception as exc:
            logger.error(f"[EmailTool] Error: {exc}")
            return ToolResult(error=str(exc))

    # ─── Stub Mode ─────────────────────────────────────────────────────────

    def _stub_execute(self, action: str, kwargs: dict[str, Any]) -> ToolResult:
        """Execute in stub mode — logs what would happen."""
        if action == "send":
            to = kwargs.get("to", "?")
            subject = kwargs.get("subject", "?")
            body = kwargs.get("body", "?")[:100]
            output = (
                f"[stub] Would send email:\n"
                f"  To: {to}\n"
                f"  Subject: {subject}\n"
                f"  Body: {body}…"
            )
            logger.info(f"[EmailTool:stub] send to={to}, subject={subject}")

        elif action == "read":
            label = kwargs.get("label", "INBOX")
            max_results = kwargs.get("max", 10)
            output = (
                f"[stub] Would read up to {max_results} emails from label '{label}'.\n"
                f"  Configure GOOGLE_APPLICATION_CREDENTIALS to enable."
            )
            logger.info(f"[EmailTool:stub] read label={label}, max={max_results}")

        elif action == "search":
            query = kwargs.get("query", "")
            output = (
                f"[stub] Would search emails with query: {query}\n"
                f"  Configure GOOGLE_APPLICATION_CREDENTIALS to enable."
            )
            logger.info(f"[EmailTool:stub] search query={query}")

        elif action == "reply":
            message_id = kwargs.get("message_id", "?")
            body_preview = kwargs.get("body", "")[:100]
            output = (
                f"[stub] Would reply to message {message_id}:\n"
                f"  Body: {body_preview}…"
            )
            logger.info(f"[EmailTool:stub] reply to={message_id}")

        else:
            return ToolResult(error=f"Unknown action: {action}")

        return ToolResult(output=output)

    # ─── Action: Send ────────────────────────────────────────────────────────

    async def _action_send(self, kwargs: dict[str, Any]) -> ToolResult:
        """Send an email."""
        import base64
        from email.mime.text import MIMEText

        to_addr = kwargs.get("to", "")
        subject = kwargs.get("subject", "")
        body_text = kwargs.get("body", "")

        if not to_addr:
            return ToolResult(error="Missing required parameter: to")
        if not subject:
            return ToolResult(error="Missing required parameter: subject")

        # Create the MIME message
        msg = MIMEText(body_text)
        msg["To"] = to_addr
        msg["From"] = _USER_ADDRESS
        msg["Subject"] = subject

        # Encode for Gmail API
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        def _send():
            service = self._get_service()
            service.users().messages().send(
                userId=_USER_ADDRESS,
                body={"raw": raw},
            ).execute()

        import asyncio
        await asyncio.to_thread(_send)

        logger.info(f"[EmailTool] Sent email to {to_addr}: {subject[:60]}")
        return ToolResult(output=f"Email sent successfully to {to_addr}")

    # ─── Action: Read ───────────────────────────────────────────────────────

    async def _action_read(self, kwargs: dict[str, Any]) -> ToolResult:
        """Read emails from a label."""
        label = kwargs.get("label", "INBOX")
        max_results = min(int(kwargs.get("max", 10)), 50)

        import asyncio

        def _read():
            service = self._get_service()
            results = service.users().messages().list(
                userId=_USER_ADDRESS,
                labelIds=[label],
                maxResults=max_results,
            ).execute()
            messages = results.get("messages", [])

            emails: list[str] = []
            for msg_ref in messages:
                msg_id = msg_ref["id"]
                msg = service.users().messages().get(
                    userId=_USER_ADDRESS,
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                snippet = msg.get("snippet", "")[:200]
                emails.append(
                    f"ID: {msg_id}\n"
                    f"From: {headers.get('From', '?')}\n"
                    f"Subject: {headers.get('Subject', '?')}\n"
                    f"Date: {headers.get('Date', '?')}\n"
                    f"Snippet: {snippet}\n"
                )
            return emails

        email_list = await asyncio.to_thread(_read)

        if not email_list:
            return ToolResult(output=f"No emails found in label '{label}'.")

        output = f"Found {len(email_list)} emails in '{label}':\n\n"
        output += "\n---\n".join(email_list)
        logger.info(f"[EmailTool] Read {len(email_list)} emails from '{label}'")
        return ToolResult(output=output)

    # ─── Action: Search ──────────────────────────────────────────────────────

    async def _action_search(self, kwargs: dict[str, Any]) -> ToolResult:
        """Search emails using Gmail search query."""
        query = kwargs.get("query", "")
        if not query:
            return ToolResult(error="Missing required parameter: query")

        import asyncio

        def _search():
            service = self._get_service()
            results = service.users().messages().list(
                userId=_USER_ADDRESS,
                q=query,
                maxResults=20,
            ).execute()
            messages = results.get("messages", [])

            emails: list[str] = []
            for msg_ref in messages:
                msg_id = msg_ref["id"]
                msg = service.users().messages().get(
                    userId=_USER_ADDRESS,
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                snippet = msg.get("snippet", "")[:200]
                emails.append(
                    f"ID: {msg_id}\n"
                    f"From: {headers.get('From', '?')}\n"
                    f"Subject: {headers.get('Subject', '?')}\n"
                    f"Date: {headers.get('Date', '?')}\n"
                    f"Snippet: {snippet}\n"
                )
            return emails

        email_list = await asyncio.to_thread(_search)

        if not email_list:
            return ToolResult(output=f"No emails found matching query: '{query}'")

        output = f"Found {len(email_list)} results for '{query}':\n\n"
        output += "\n---\n".join(email_list)
        logger.info(f"[EmailTool] Search '{query}' returned {len(email_list)} results")
        return ToolResult(output=output)

    # ─── Action: Reply ──────────────────────────────────────────────────────

    async def _action_reply(self, kwargs: dict[str, Any]) -> ToolResult:
        """Reply to a specific email message."""
        import base64
        import asyncio
        from email.mime.text import MIMEText

        message_id = kwargs.get("message_id", "")
        body_text = kwargs.get("body", "")

        if not message_id:
            return ToolResult(error="Missing required parameter: message_id")
        if not body_text:
            return ToolResult(error="Missing required parameter: body")

        def _reply():
            service = self._get_service()

            # Get original message to construct thread
            original = service.users().messages().get(
                userId=_USER_ADDRESS,
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Message-ID", "References"],
            ).execute()

            headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}

            # Construct reply
            msg = MIMEText(body_text)
            msg["To"] = headers.get("From", "")
            msg["From"] = _USER_ADDRESS
            msg["Subject"] = f"Re: {headers.get('Subject', '')}"
            msg["In-Reply-To"] = headers.get("Message-ID", "")
            msg["References"] = headers.get("References", "") + " " + headers.get("Message-ID", "")

            # Thread the reply
            thread_id = original.get("threadId", "")
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

            send_body: dict[str, str] = {"raw": raw}
            if thread_id:
                send_body["threadId"] = thread_id

            service.users().messages().send(
                userId=_USER_ADDRESS,
                body=send_body,
            ).execute()

            return headers.get("From", "?")

        to_addr = await asyncio.to_thread(_reply)

        logger.info(f"[EmailTool] Replied to message {message_id} ({to_addr})")
        return ToolResult(output=f"Reply sent to {to_addr} for message {message_id}")
