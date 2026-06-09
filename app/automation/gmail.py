"""ManusClaw Gmail Watcher.

Watches a Gmail inbox for new messages using the Gmail API Pub/Sub push
notifications. When a new email arrives, the watcher triggers the agent to
process the email and optionally auto-reply.

Supports two modes:
  1. **Live mode** — Uses the Gmail API with Pub/Sub push notifications.
     Requires `GOOGLE_APPLICATION_CREDENTIALS` or `GOOGLE_SERVICE_ACCOUNT_JSON`.
  2. **Stub mode** — Logs what it would do without connecting to Gmail.
     Activated automatically when credentials are not configured.

Environment Variables:
    GOOGLE_APPLICATION_CREDENTIALS  — Path to service account JSON credentials
    GOOGLE_SERVICE_ACCOUNT_JSON    — Alternative: inline JSON credentials
    GMAIL_WATCH_TOPIC_NAME          — Pub/Sub topic name (default: ``projects/{project}/topics/manusclaw-gmail``)
    GMAIL_PROJECT_ID                — GCP project ID for Pub/Sub
    GMAIL_USER_ADDRESS              — Email address to watch (default: ``me``)
    GMAIL_AUTO_REPLY                — Enable auto-reply (default: ``false``)
    GMAIL_LABEL_FILTER              — Only process emails with this label (optional)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.logger import logger

# ─── Optional Imports ────────────────────────────────────────────────────────

try:
    from google.oauth2 import service_account
    from googleapiclient import discovery, errors as google_errors
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False


# ─── Configuration ─────────────────────────────────────────────────────────────

_CREDENTIALS_PATH: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
_TOPIC_NAME: str = os.getenv("GMAIL_WATCH_TOPIC_NAME", "")
_PROJECT_ID: str = os.getenv("GMAIL_PROJECT_ID", "")
_USER_ADDRESS: str = os.getenv("GMAIL_USER_ADDRESS", "me")
_AUTO_REPLY: bool = os.getenv("GMAIL_AUTO_REPLY", "false").lower() in ("1", "true", "yes")
_LABEL_FILTER: str = os.getenv("GMAIL_LABEL_FILTER", "")


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class EmailMessage:
    """Parsed email message from Gmail."""
    message_id: str
    thread_id: str = ""
    subject: str = ""
    from_address: str = ""
    to_address: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    has_attachments: bool = False


@dataclass
class WatchResult:
    """Result of setting up a Gmail watch."""
    success: bool
    history_id: int = 0
    expiration: str = ""
    error: str = ""


# ─── GmailWatcher ──────────────────────────────────────────────────────────────

class GmailWatcher:
    """Watches a Gmail inbox and processes new emails with the ManusClaw agent.

    In stub mode (no credentials), all operations are logged but not executed.

    Usage::

        watcher = GmailWatcher()
        await watcher.watch(topic_name="projects/my-project/topics/gmail", project_id="my-project")

        # Create a FastAPI webhook handler for Pub/Sub push
        handler = watcher.create_webhook_handler()
    """

    def __init__(
        self,
        credentials_path: str = _CREDENTIALS_PATH,
        service_account_json: str = _SERVICE_ACCOUNT_JSON,
        topic_name: str = _TOPIC_NAME,
        project_id: str = _PROJECT_ID,
        user_address: str = _USER_ADDRESS,
        auto_reply: bool = _AUTO_REPLY,
        label_filter: str = _LABEL_FILTER,
    ) -> None:
        self._credentials_path = credentials_path
        self._service_account_json = service_account_json
        self._topic_name = topic_name
        self._project_id = project_id
        self._user_address = user_address
        self._auto_reply = auto_reply
        self._label_filter = label_filter

        self._service: Optional[Any] = None
        self._running: bool = False
        self._on_email_callbacks: list[Callable[[EmailMessage], Any]] = []

        self._stub_mode: bool = not self._is_configured()
        if self._stub_mode:
            logger.warning(
                "[Gmail] Stub mode — GOOGLE_APPLICATION_CREDENTIALS or "
                "GOOGLE_SERVICE_ACCOUNT_JSON not configured"
            )

    # ─── Properties ────────────────────────────────────────────────────────

    @property
    def is_stub(self) -> bool:
        """Whether the watcher is operating in stub mode."""
        return self._stub_mode

    @property
    def is_configured(self) -> bool:
        """Whether credentials are available."""
        return not self._stub_mode

    # ─── Configuration Check ───────────────────────────────────────────────

    def _is_configured(self) -> bool:
        """Check if Google credentials are available."""
        if not _HAS_GOOGLE:
            return False
        return bool(self._credentials_path or self._service_account_json)

    # ─── Service Initialization ────────────────────────────────────────────

    def _get_service(self) -> Any:
        """Lazily create the Gmail API service object."""
        if self._service is not None:
            return self._service

        if not self._is_configured():
            raise RuntimeError("Gmail credentials not configured — operating in stub mode")

        if self._credentials_path:
            creds = service_account.Credentials.from_service_account_file(
                self._credentials_path,
                scopes=["https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.modify"],
            )
        elif self._service_account_json:
            creds_info = json.loads(self._service_account_json)
            creds = service_account.Credentials.from_service_account_info(
                creds_info,
                scopes=["https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.modify"],
            )
        else:
            raise RuntimeError("No credentials provided")

        self._service = discovery.build("gmail", "v1", credentials=creds)
        return self._service

    # ─── Callbacks ─────────────────────────────────────────────────────────

    def on_email(self, callback: Callable[[EmailMessage], Any]) -> None:
        """Register a callback for incoming emails.

        Callback receives an ``EmailMessage`` instance.
        """
        self._on_email_callbacks.append(callback)

    # ─── Watch / Stop ─────────────────────────────────────────────────────

    async def watch(
        self,
        topic_name: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> WatchResult:
        """Set up Gmail Pub/Sub push notifications for new emails.

        Args:
            topic_name: Full Pub/Sub topic name (e.g. ``projects/xyz/topics/gmail``).
                        Falls back to the configured value.
            project_id: GCP project ID. Falls back to the configured value.

        Returns:
            WatchResult with success status and watch metadata.
        """
        topic = topic_name or self._topic_name
        project = project_id or self._project_id

        if not topic:
            logger.error("[Gmail] Cannot watch — no topic name provided")
            return WatchResult(success=False, error="No topic name configured")

        if self._stub_mode:
            logger.info(f"[Gmail:stub] Would watch topic: {topic} (project: {project})")
            return WatchResult(
                success=True,
                history_id=0,
                expiration="stub",
                error="stub_mode",
            )

        try:
            service = self._get_service()

            request_body: dict[str, Any] = {
                "topicName": topic,
                "labelIds": ["INBOX"],
            }
            if self._label_filter:
                request_body["labelFilterBehavior"] = "include"
                request_body["labelFilterAction"] = "include"
                request_body["labelIds"].append(self._label_filter)

            result = await asyncio.to_thread(
                service.users().watch(userId=self._user_address, body=request_body).execute
            )

            history_id = result.get("historyId", 0)
            expiration = result.get("expiration", "")
            logger.info(f"[Gmail] Watch set up: historyId={history_id}, expires={expiration}")
            return WatchResult(success=True, history_id=history_id, expiration=expiration)

        except google_errors.HttpError as exc:
            error_msg = f"HTTP {exc.resp.status}: {exc._get_reason()}"
            logger.error(f"[Gmail] Watch failed: {error_msg}")
            return WatchResult(success=False, error=error_msg)
        except Exception as exc:
            logger.error(f"[Gmail] Watch error: {exc}")
            return WatchResult(success=False, error=str(exc))

    async def stop(self) -> None:
        """Stop the Gmail watch subscription and clean up."""
        self._running = False

        if self._stub_mode:
            logger.info("[Gmail:stub] Would stop watching")
            return

        try:
            service = self._get_service()
            await asyncio.to_thread(
                service.users().stop(userId=self._user_address).execute
            )
            logger.info("[Gmail] Watch stopped")
        except Exception as exc:
            logger.warning(f"[Gmail] Stop error: {exc}")

    # ─── List Unread ────────────────────────────────────────────────────────

    async def list_unread(self, max_results: int = 10) -> list[EmailMessage]:
        """List unread emails from the inbox.

        Args:
            max_results: Maximum number of messages to return.

        Returns:
            List of ``EmailMessage`` instances.
        """
        if self._stub_mode:
            logger.info(f"[Gmail:stub] Would list {max_results} unread emails")
            return []

        try:
            service = self._get_service()

            # Search for unread messages
            query = "is:unread"
            if self._label_filter:
                query += f" label:{self._label_filter}"

            def _list():
                results = service.users().messages().list(
                    userId=self._user_address,
                    q=query,
                    maxResults=max_results,
                ).execute()
                messages = results.get("messages", [])
                emails: list[EmailMessage] = []
                for msg_ref in messages:
                    msg_id = msg_ref["id"]
                    msg = service.users().messages().get(
                        userId=self._user_address,
                        id=msg_id,
                        format="full",
                    ).execute()
                    emails.append(self._parse_message(msg))
                return emails

            return await asyncio.to_thread(_list)

        except Exception as exc:
            logger.error(f"[Gmail] List unread error: {exc}")
            return []

    # ─── Process Email ────────────────────────────────────────────────────────

    async def process_email(self, message_id: str) -> Optional[str]:
        """Process a single email with the ManusClaw agent.

        Fetches the email, runs the agent on its content, and optionally
        auto-replies.

        Args:
            message_id: Gmail message ID to process.

        Returns:
            Agent response text, or None if processing failed.
        """
        if self._stub_mode:
            logger.info(f"[Gmail:stub] Would process email {message_id}")
            return f"[stub] Would process email {message_id}"

        try:
            service = self._get_service()

            def _fetch():
                return service.users().messages().get(
                    userId=self._user_address,
                    id=message_id,
                    format="full",
                ).execute()

            msg_data = await asyncio.to_thread(_fetch)
            email = self._parse_message(msg_data)

            logger.info(f"[Gmail] Processing email: {email.subject[:60]} from {email.from_address}")

            # Notify callbacks
            for cb in self._on_email_callbacks:
                try:
                    await (cb(email) if asyncio.iscoroutinefunction(cb) else cb(email))
                except Exception as exc:
                    logger.error(f"[Gmail] Callback error: {exc}")

            # Run agent
            from app.agent.manus import Manus

            agent = Manus()
            prompt = (
                f"Process this email and determine if action is needed:\n\n"
                f"From: {email.from_address}\n"
                f"Subject: {email.subject}\n"
                f"Body:\n{email.body[:3000]}"
            )
            response = await agent.run(prompt)

            # Auto-reply if enabled
            if self._auto_reply and response:
                await self._send_reply(email, response)

            return response

        except Exception as exc:
            logger.error(f"[Gmail] Process email error: {exc}")
            return None

    # ─── Webhook Handler ───────────────────────────────────────────────────

    def create_webhook_handler(self):
        """Create a FastAPI-compatible webhook handler for Gmail Pub/Sub push.

        Returns a callable suitable for use as a FastAPI endpoint::

            from app.automation import GmailWatcher
            watcher = GmailWatcher()

            @app.post("/gmail/webhook")
            async def gmail_webhook(request: Request):
                return await watcher.create_webhook_handler()(await request.json())
        """
        async def _handler(body: dict[str, Any]) -> dict[str, Any]:
            """Process incoming Gmail Pub/Sub push notification."""
            try:
                # Pub/Sub push envelope
                message_data = body.get("message", {})
                if message_data:
                    # Decode Pub/Sub message data
                    import base64
                    encoded = message_data.get("data", "")
                    if encoded:
                        decoded = base64.b64decode(encoded).decode("utf-8")
                        pubsub_msg = json.loads(decoded)
                        email_data = pubsub_msg.get("email", {})
                        message_id = email_data.get("messageId", "")

                        if message_id:
                            logger.info(f"[Gmail] Pub/Sub push: processing message {message_id}")
                            await self.process_email(message_id)
                            return {"status": "ok", "processed": message_id}

                return {"status": "no_message"}

            except Exception as exc:
                logger.error(f"[Gmail] Webhook handler error: {exc}")
                return {"status": "error", "error": str(exc)}

        return _handler

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _parse_message(self, msg_data: dict[str, Any]) -> EmailMessage:
        """Parse a Gmail API message into an EmailMessage."""
        headers = {h["name"].lower(): h["value"] for h in msg_data.get("payload", {}).get("headers", [])}

        body = self._extract_body(msg_data.get("payload", {}))

        return EmailMessage(
            message_id=msg_data.get("id", ""),
            thread_id=msg_data.get("threadId", ""),
            subject=headers.get("subject", ""),
            from_address=headers.get("from", ""),
            to_address=headers.get("to", ""),
            date=headers.get("date", ""),
            snippet=msg_data.get("snippet", ""),
            body=body,
            labels=msg_data.get("labelIds", []),
            has_attachments=self._has_attachments(msg_data.get("payload", {})),
        )

    def _extract_body(self, payload: dict[str, Any]) -> str:
        """Extract plain text body from email payload."""
        if payload.get("mimeType") == "text/plain":
            return payload.get("body", {}).get("data", "")

        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                import base64
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            # Recurse into multipart
            if part.get("parts"):
                result = self._extract_body(part)
                if result:
                    return result

        return ""

    def _has_attachments(self, payload: dict[str, Any]) -> bool:
        """Check if the email has attachments."""
        if payload.get("parts"):
            for part in payload["parts"]:
                if part.get("filename"):
                    return True
                if part.get("parts") and self._has_attachments(part):
                    return True
        return False

    async def _send_reply(self, email: EmailMessage, body: str) -> None:
        """Send a reply to the email."""
        if self._stub_mode:
            logger.info(f"[Gmail:stub] Would reply to {email.from_address}")
            return

        try:
            service = self._get_service()

            # Modify message — mark as read
            await asyncio.to_thread(
                service.users().messages().modify(
                    userId=self._user_address,
                    id=email.message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute
            )

            logger.info(f"[Gmail] Marked {email.message_id} as read")

        except Exception as exc:
            logger.error(f"[Gmail] Reply error: {exc}")
