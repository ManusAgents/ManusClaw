from __future__ import annotations

"""
Slack Integration — Agent Interaction & Workflow
=================================================
Slack Bot for ManusClaw agent interaction, separate from the messaging
layer. This module provides:

  - Slash commands: /manusclaw, /resolve, /review
  - Thread-based conversations
  - File upload support
  - Interactive messages with buttons
  - Resolver workflow integration

Uses the Slack Web API and Socket Mode for real-time interaction.

Thread-safe. Optional dependencies (aiohttp, slack-bolt) handled gracefully.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional
from urllib.parse import urlencode

from app.logger import logger

# ──────────────────────────────────────────────────────────────────────────────
# Optional dependencies
# ──────────────────────────────────────────────────────────────────────────────

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

try:
    from slack_bolt import App as SlackBoltApp
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    _SLACK_BOLT_AVAILABLE = True
except ImportError:
    _SLACK_BOLT_AVAILABLE = False

try:
    import urllib.request
    import urllib.error
    _URLLIB_AVAILABLE = True
except ImportError:
    _URLLIB_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

SLACK_API_BASE = "https://slack.com/api"
MAX_MESSAGE_LENGTH = 40000  # Slack's limit for message text
MAX_BLOCK_TEXT_LENGTH = 3000  # Slack's limit for text in blocks
MAX_FILE_SIZE = 1024 * 1024  # 1MB for file uploads


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class SlackEventType(str, Enum):
    """Slack event types we handle."""

    MESSAGE = "message"
    APP_MENTION = "app_mention"
    SLASH_COMMAND = "slash_command"
    INTERACTION = "interaction"
    FILE_SHARED = "file_shared"
    REACTION_ADDED = "reaction_added"


class SlashCommand(str, Enum):
    """Supported slash commands."""

    MANUSCLAW = "/manusclaw"
    RESOLVE = "/resolve"
    REVIEW = "/review"


class InteractionType(str, Enum):
    """Slack interaction types."""

    BUTTON = "button"
    OVERFLOW = "overflow"
    DATEPICKER = "datepicker"
    STATIC_SELECT = "static_select"


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SlackMessage:
    """A Slack message."""

    channel_id: str = ""
    channel_name: str = ""
    user_id: str = ""
    user_name: str = ""
    text: str = ""
    thread_ts: str = ""
    timestamp: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    is_bot: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "text": self.text,
            "thread_ts": self.thread_ts,
            "timestamp": self.timestamp,
        }


@dataclass
class SlashCommandPayload:
    """A parsed slash command payload."""

    command: str = ""
    text: str = ""
    channel_id: str = ""
    channel_name: str = ""
    user_id: str = ""
    user_name: str = ""
    team_id: str = ""
    trigger_id: str = ""
    response_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "text": self.text,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
        }


@dataclass
class InteractionPayload:
    """A parsed interaction (button click, etc.) payload."""

    type: str = ""
    action_id: str = ""
    block_id: str = ""
    value: str = ""
    channel_id: str = ""
    user_id: str = ""
    response_url: str = ""
    trigger_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "action_id": self.action_id,
            "value": self.value,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
        }


@dataclass
class SlackConversation:
    """Tracks a thread-based conversation in Slack."""

    conversation_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:12]
    )
    channel_id: str = ""
    thread_ts: str = ""
    user_id: str = ""
    messages: List[SlackMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    is_active: bool = True
    resolution_id: Optional[str] = None

    def add_message(self, message: SlackMessage) -> None:
        """Add a message to the conversation."""
        self.messages.append(message)
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "user_id": self.user_id,
            "message_count": len(self.messages),
            "created_at": self.created_at,
            "is_active": self.is_active,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Block Kit Builder
# ──────────────────────────────────────────────────────────────────────────────


class BlockKit:
    """Helper for building Slack Block Kit messages."""

    @staticmethod
    def header(text: str) -> Dict[str, Any]:
        """Build a header block."""
        return {
            "type": "header",
            "text": {"type": "plain_text", "text": text[:150]},
        }

    @staticmethod
    def section(text: str, markdown: bool = True) -> Dict[str, Any]:
        """Build a section block."""
        text_type = "mrkdwn" if markdown else "plain_text"
        return {
            "type": "section",
            "text": {"type": text_type, "text": text[:MAX_BLOCK_TEXT_LENGTH]},
        }

    @staticmethod
    def divider() -> Dict[str, Any]:
        """Build a divider block."""
        return {"type": "divider"}

    @staticmethod
    def actions(*buttons: Dict[str, Any]) -> Dict[str, Any]:
        """Build an actions block with buttons."""
        return {
            "type": "actions",
            "elements": list(buttons),
        }

    @staticmethod
    def button(
        text: str,
        action_id: str,
        value: str = "",
        style: str = "",
    ) -> Dict[str, Any]:
        """Build a button element."""
        element: Dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": text[:75]},
            "action_id": action_id,
        }
        if value:
            element["value"] = value
        if style in ("primary", "danger"):
            element["style"] = style
        return element

    @staticmethod
    def context(*texts: str) -> Dict[str, Any]:
        """Build a context block."""
        elements = [
            {"type": "mrkdwn", "text": t[:MAX_BLOCK_TEXT_LENGTH]}
            for t in texts
        ]
        return {"type": "context", "elements": elements}

    @staticmethod
    def fields(*texts: str) -> Dict[str, Any]:
        """Build a section with fields."""
        return {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": t[:MAX_BLOCK_TEXT_LENGTH]}
                for t in texts
            ],
        }

    @staticmethod
    def image_block(url: str, alt_text: str, title: str = "") -> Dict[str, Any]:
        """Build an image block."""
        block: Dict[str, Any] = {
            "type": "image",
            "image_url": url,
            "alt_text": alt_text,
        }
        if title:
            block["title"] = {"type": "plain_text", "text": title}
        return block


# ──────────────────────────────────────────────────────────────────────────────
# Slack Integration Client
# ──────────────────────────────────────────────────────────────────────────────


class SlackIntegration:
    """
    Slack Bot integration for ManusClaw agent interaction and workflows.

    This is separate from the messaging Slack adapter — it provides
    slash commands, interactive messages, thread-based conversations,
    and resolver workflow integration.

    Usage::

        slack = SlackIntegration(
            bot_token="xoxb-xxx",
            app_token="xapp-xxx",
        )

        # Register command handlers
        slack.on_command("/resolve", handle_resolve)
        slack.on_interaction("resolve_issue", handle_resolve_button)

        # Start the bot
        await slack.start()

        # Or use the API directly
        await slack.send_message("C12345", "Hello!", thread_ts="1234567890.123456")
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        app_token: Optional[str] = None,
        signing_secret: Optional[str] = None,
    ) -> None:
        self._bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN", "")
        self._app_token = app_token or os.getenv("SLACK_APP_TOKEN", "")
        self._signing_secret = signing_secret or os.getenv(
            "SLACK_SIGNING_SECRET", ""
        )
        self._conversations: Dict[str, SlackConversation] = {}
        self._command_handlers: Dict[str, Callable] = {}
        self._interaction_handlers: Dict[str, Callable] = {}
        self._event_handlers: Dict[SlackEventType, List[Callable]] = {}
        self._lock = threading.RLock()
        self._running = False
        self._bolt_app: Optional[Any] = None

    @property
    def is_configured(self) -> bool:
        """Check if the Slack integration is configured."""
        return bool(self._bot_token)

    # ── Handler registration ───────────────────────────────────────────────

    def on_command(
        self,
        command: str,
        handler: Callable[
            [SlashCommandPayload], Coroutine[Any, Any, Optional[str]]
        ],
    ) -> None:
        """
        Register a handler for a slash command.

        Handler signature: async def handler(payload: SlashCommandPayload) -> str
        The return value is sent as the command response.
        """
        with self._lock:
            self._command_handlers[command] = handler
        logger.info(
            "integrations.slack.command_registered command=%s", command
        )

    def on_interaction(
        self,
        action_id: str,
        handler: Callable[
            [InteractionPayload], Coroutine[Any, Any, Optional[str]]
        ],
    ) -> None:
        """
        Register a handler for an interaction (button click, etc.).

        Handler signature: async def handler(payload: InteractionPayload) -> str
        """
        with self._lock:
            self._interaction_handlers[action_id] = handler
        logger.info(
            "integrations.slack.interaction_registered action_id=%s",
            action_id,
        )

    def on_event(
        self,
        event_type: SlackEventType,
        handler: Callable,
    ) -> None:
        """Register a handler for a Slack event."""
        with self._lock:
            if event_type not in self._event_handlers:
                self._event_handlers[event_type] = []
            self._event_handlers[event_type].append(handler)

    # ── Bot lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the Slack bot.

        Uses Socket Mode if slack-bolt is available, otherwise logs a
        warning and runs in API-only mode (can send messages but not
        receive events).
        """
        if not self.is_configured:
            logger.info(
                "integrations.slack.not_configured "
                "(SLACK_BOT_TOKEN not set)"
            )
            return

        if _SLACK_BOLT_AVAILABLE and self._app_token:
            await self._start_socket_mode()
        else:
            logger.info(
                "integrations.slack.api_only_mode "
                "(slack-bolt or SLACK_APP_TOKEN not available)"
            )
            self._running = True

    async def stop(self) -> None:
        """Stop the Slack bot."""
        self._running = False
        logger.info("integrations.slack.stopped")

    async def _start_socket_mode(self) -> None:
        """Start Slack in Socket Mode using slack-bolt."""
        if not _SLACK_BOLT_AVAILABLE:
            logger.warning(
                "integrations.slack.bolt_not_available"
            )
            return

        try:
            self._bolt_app = SlackBoltApp(
                token=self._bot_token,
                signing_secret=self._signing_secret,
            )

            # Register built-in command handlers with bolt
            self._register_bolt_commands()

            handler = SocketModeHandler(self._bolt_app, self._app_token)
            self._running = True
            logger.info("integrations.slack.socket_mode_starting")

            # Start in a thread so we don't block the event loop
            await asyncio.to_thread(handler.start)

        except Exception as exc:
            logger.error(
                "integrations.slack.socket_mode_failed err=%s", exc
            )
            self._running = True  # Fall back to API-only mode

    def _register_bolt_commands(self) -> None:
        """Register slash commands with the bolt app."""
        if self._bolt_app is None:
            return

        @self._bolt_app.command("/manusclaw")
        async def handle_manusclaw(ack, say, command):
            await ack()
            payload = self._parse_slash_command(command)
            handler = self._command_handlers.get("/manusclaw")
            if handler:
                response = await handler(payload)
                if response:
                    await say(response)
            else:
                await say(self._default_manusclaw_response(payload))

        @self._bolt_app.command("/resolve")
        async def handle_resolve(ack, say, command):
            await ack()
            payload = self._parse_slash_command(command)
            handler = self._command_handlers.get("/resolve")
            if handler:
                response = await handler(payload)
                if response:
                    await say(response)
            else:
                await say(self._default_resolve_response(payload))

        @self._bolt_app.command("/review")
        async def handle_review(ack, say, command):
            await ack()
            payload = self._parse_slash_command(command)
            handler = self._command_handlers.get("/review")
            if handler:
                response = await handler(payload)
                if response:
                    await say(response)
            else:
                await say(self._default_review_response(payload))

        @self._bolt_app.event("app_mention")
        async def handle_app_mention(event, say):
            message = self._parse_message_event(event)
            handlers = self._event_handlers.get(SlackEventType.APP_MENTION, [])
            for handler in handlers:
                try:
                    await handler(message)
                except Exception as exc:
                    logger.warning(
                        "integrations.slack.mention_handler_failed err=%s",
                        exc,
                    )

        @self._bolt_app.event("message")
        async def handle_message(event, say):
            # Skip bot messages
            if event.get("bot_id") or event.get("subtype"):
                return
            message = self._parse_message_event(event)
            if message.thread_ts:
                await self._handle_thread_message(message)

        @self._bolt_app.action(re.compile(r"manusclaw_.*"))
        async def handle_interaction(ack, body, respond):
            await ack()
            payload = self._parse_interaction_body(body)
            handler = self._interaction_handlers.get(payload.action_id)
            if handler:
                response = await handler(payload)
                if response:
                    await respond(response)

    # ── Message sending ────────────────────────────────────────────────────

    async def send_message(
        self,
        channel_id: str,
        text: str,
        thread_ts: str = "",
        blocks: Optional[List[Dict[str, Any]]] = None,
        reply_broadcast: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a message to a Slack channel.

        Args:
            channel_id: The channel ID.
            text: Message text (fallback for blocks).
            thread_ts: Thread timestamp to reply in a thread.
            blocks: Optional Block Kit blocks.
            reply_broadcast: If True, broadcasts thread reply to channel.

        Returns:
            Slack API response.
        """
        if not self.is_configured:
            logger.info(
                "integrations.slack.stub_send channel=%s text=%s",
                channel_id,
                text[:80],
            )
            return {"ok": False, "error": "not_configured"}

        payload: Dict[str, Any] = {
            "channel": channel_id,
            "text": text[:MAX_MESSAGE_LENGTH],
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        if reply_broadcast:
            payload["reply_broadcast"] = True

        return await self._api_post("chat.postMessage", payload)

    async def send_ephemeral(
        self,
        channel_id: str,
        user_id: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Send an ephemeral message visible only to a specific user.

        Args:
            channel_id: The channel ID.
            user_id: The user ID who will see the message.
            text: Message text.
            blocks: Optional Block Kit blocks.

        Returns:
            Slack API response.
        """
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}

        payload: Dict[str, Any] = {
            "channel": channel_id,
            "user": user_id,
            "text": text[:MAX_MESSAGE_LENGTH],
        }
        if blocks:
            payload["blocks"] = blocks

        return await self._api_post("chat.postEphemeral", payload)

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing message."""
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}

        payload: Dict[str, Any] = {
            "channel": channel_id,
            "ts": ts,
            "text": text[:MAX_MESSAGE_LENGTH],
        }
        if blocks:
            payload["blocks"] = blocks

        return await self._api_post("chat.update", payload)

    # ── File uploads ───────────────────────────────────────────────────────

    async def upload_file(
        self,
        channel_id: str,
        content: str,
        filename: str = "output.txt",
        title: str = "",
        initial_comment: str = "",
        thread_ts: str = "",
    ) -> Dict[str, Any]:
        """
        Upload a file to a Slack channel.

        Args:
            channel_id: The channel ID.
            content: File content as string.
            filename: Filename to display.
            title: File title.
            initial_comment: Comment to post with the file.
            thread_ts: Thread timestamp for threaded upload.

        Returns:
            Slack API response.
        """
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}

        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE]

        payload: Dict[str, Any] = {
            "channels": channel_id,
            "content": content,
            "filename": filename,
        }
        if title:
            payload["title"] = title
        if initial_comment:
            payload["initial_comment"] = initial_comment
        if thread_ts:
            payload["thread_ts"] = thread_ts

        return await self._api_post("files.upload", payload)

    # ── Interactive messages ───────────────────────────────────────────────

    async def send_interactive_resolve(
        self,
        channel_id: str,
        issue_title: str,
        issue_url: str,
        provider: str,
        repo_id: str,
        issue_number: int,
        thread_ts: str = "",
    ) -> Dict[str, Any]:
        """
        Send an interactive message with resolve/review buttons.

        Args:
            channel_id: The channel ID.
            issue_title: Issue title.
            issue_url: Link to the issue.
            provider: Git provider name.
            repo_id: Repository identifier.
            issue_number: Issue number.
            thread_ts: Optional thread timestamp.

        Returns:
            Slack API response.
        """
        blocks = [
            BlockKit.header("ManusClaw Issue Resolution"),
            BlockKit.section(
                f"*Issue:* <{issue_url}|{issue_title}>\n"
                f"*Provider:* {provider} | *Repo:* `{repo_id}` | "
                f"*Issue:* #{issue_number}"
            ),
            BlockKit.divider(),
            BlockKit.actions(
                BlockKit.button(
                    "Resolve Issue",
                    "manusclaw_resolve",
                    value=json.dumps({
                        "provider": provider,
                        "repo_id": repo_id,
                        "issue_number": issue_number,
                    }),
                    style="primary",
                ),
                BlockKit.button(
                    "Review Only",
                    "manusclaw_review",
                    value=json.dumps({
                        "provider": provider,
                        "repo_id": repo_id,
                        "issue_number": issue_number,
                    }),
                ),
                BlockKit.button(
                    "Dismiss",
                    "manusclaw_dismiss",
                    value="dismiss",
                    style="danger",
                ),
            ),
            BlockKit.context(
                "_Powered by ManusClaw_ | "
                "Resolve: auto-fix with LLM | Review: analysis only"
            ),
        ]

        return await self.send_message(
            channel_id,
            text=f"ManusClaw: Issue Resolution - {issue_title}",
            thread_ts=thread_ts,
            blocks=blocks,
        )

    async def send_resolution_status(
        self,
        channel_id: str,
        status: str,
        summary: str,
        request_id: str = "",
        duration: float = 0.0,
        thread_ts: str = "",
    ) -> Dict[str, Any]:
        """Send a resolution status update message."""
        status_emoji = {
            "completed": "\u2705",
            "failed": "\u274c",
            "running": "\U0001f504",
            "timed_out": "\u23f0",
        }.get(status, "\u2753")

        blocks = [
            BlockKit.header(f"{status_emoji} Resolution {status.title()}"),
            BlockKit.section(
                f"*Request:* `{request_id}` | "
                f"*Duration:* {duration:.1f}s\n\n"
                f"{summary[:MAX_BLOCK_TEXT_LENGTH - 100]}"
            ),
        ]

        if status == "completed":
            blocks.append(
                BlockKit.actions(
                    BlockKit.button(
                        "View Changes",
                        "manusclaw_view_changes",
                        value=request_id,
                    ),
                )
            )

        blocks.append(
            BlockKit.context("_Powered by ManusClaw_")
        )

        return await self.send_message(
            channel_id,
            text=f"ManusClaw Resolution: {status}",
            thread_ts=thread_ts,
            blocks=blocks,
        )

    # ── Thread-based conversations ─────────────────────────────────────────

    async def _handle_thread_message(self, message: SlackMessage) -> None:
        """Handle a message posted in a thread we're tracking."""
        with self._lock:
            conversation = self._conversations.get(message.thread_ts)

        if conversation and conversation.is_active:
            conversation.add_message(message)
            logger.info(
                "integrations.slack.thread_message "
                "conversation=%s user=%s",
                conversation.conversation_id,
                message.user_id,
            )

            # Notify event handlers
            handlers = self._event_handlers.get(SlackEventType.MESSAGE, [])
            for handler in handlers:
                try:
                    await handler(message)
                except Exception as exc:
                    logger.warning(
                        "integrations.slack.thread_handler_failed err=%s",
                        exc,
                    )

    def get_or_create_conversation(
        self,
        channel_id: str,
        thread_ts: str,
        user_id: str = "",
    ) -> SlackConversation:
        """Get an existing conversation or create a new one."""
        with self._lock:
            if thread_ts in self._conversations:
                return self._conversations[thread_ts]

            conversation = SlackConversation(
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
            )
            self._conversations[thread_ts] = conversation
            return conversation

    def end_conversation(self, thread_ts: str) -> Optional[SlackConversation]:
        """Mark a conversation as ended."""
        with self._lock:
            conversation = self._conversations.get(thread_ts)
            if conversation:
                conversation.is_active = False
                return conversation
        return None

    def list_active_conversations(self) -> List[SlackConversation]:
        """List all active conversations."""
        with self._lock:
            return [
                c for c in self._conversations.values() if c.is_active
            ]

    # ── Default slash command responses ────────────────────────────────────

    @staticmethod
    def _default_manusclaw_response(
        payload: SlashCommandPayload,
    ) -> str:
        """Default response for /manusclaw command."""
        return (
            f"Hello <@{payload.user_id}>! I'm ManusClaw, your AI-powered "
            f"development assistant.\n\n"
            f"*Available commands:*\n"
            f"• `/manusclaw status` — Check my status\n"
            f"• `/resolve <issue-url>` — Resolve an issue with AI\n"
            f"• `/review <pr-url>` — Review a pull request\n\n"
            f"Or just mention me in a thread to start a conversation!"
        )

    @staticmethod
    def _default_resolve_response(
        payload: SlashCommandPayload,
    ) -> str:
        """Default response for /resolve command."""
        text = payload.text.strip()
        if not text:
            return (
                "Please provide an issue URL or reference.\n"
                "Example: `/resolve https://github.com/owner/repo/issues/42`"
            )
        return (
            f"Starting resolution for: `{text}`\n"
            f"I'll analyze the issue and propose a fix. "
            f"You'll see updates in this thread."
        )

    @staticmethod
    def _default_review_response(
        payload: SlashCommandPayload,
    ) -> str:
        """Default response for /review command."""
        text = payload.text.strip()
        if not text:
            return (
                "Please provide a PR URL.\n"
                "Example: `/review https://github.com/owner/repo/pull/123`"
            )
        return (
            f"Starting review for: `{text}`\n"
            f"I'll analyze the PR and provide feedback."
        )

    # ── Payload parsing ────────────────────────────────────────────────────

    @staticmethod
    def _parse_slash_command(data: Any) -> SlashCommandPayload:
        """Parse a slash command payload from slack-bolt or HTTP request."""
        if isinstance(data, dict):
            return SlashCommandPayload(
                command=data.get("command", ""),
                text=data.get("text", ""),
                channel_id=data.get("channel_id", ""),
                channel_name=data.get("channel_name", ""),
                user_id=data.get("user_id", ""),
                user_name=data.get("user_name", ""),
                team_id=data.get("team_id", ""),
                trigger_id=data.get("trigger_id", ""),
                response_url=data.get("response_url", ""),
            )
        # slack-bolt command object
        return SlashCommandPayload(
            command=getattr(data, "command", ""),
            text=getattr(data, "text", ""),
            channel_id=getattr(data, "channel_id", ""),
            channel_name=getattr(data, "channel_name", ""),
            user_id=getattr(data, "user_id", ""),
            user_name=getattr(data, "user_name", ""),
            team_id=getattr(data, "team_id", ""),
            trigger_id=getattr(data, "trigger_id", ""),
            response_url=getattr(data, "response_url", ""),
        )

    @staticmethod
    def _parse_message_event(event: Any) -> SlackMessage:
        """Parse a message event from slack-bolt."""
        if isinstance(event, dict):
            return SlackMessage(
                channel_id=event.get("channel", ""),
                user_id=event.get("user", ""),
                text=event.get("text", ""),
                thread_ts=event.get("thread_ts", ""),
                timestamp=event.get("ts", ""),
                is_bot=bool(event.get("bot_id")),
                extra=event,
            )
        return SlackMessage(
            channel_id=getattr(event, "channel", ""),
            user_id=getattr(event, "user", ""),
            text=getattr(event, "text", ""),
            thread_ts=getattr(event, "thread_ts", ""),
            timestamp=getattr(event, "ts", ""),
            is_bot=bool(getattr(event, "bot_id", None)),
        )

    @staticmethod
    def _parse_interaction_body(body: Any) -> InteractionPayload:
        """Parse an interaction payload from slack-bolt."""
        if isinstance(body, dict):
            action = body.get("actions", [{}])[0] if body.get("actions") else {}
            channel = body.get("channel", {})
            user = body.get("user", {})
            return InteractionPayload(
                type=body.get("type", ""),
                action_id=action.get("action_id", ""),
                block_id=action.get("block_id", ""),
                value=action.get("value", ""),
                channel_id=channel.get("id", ""),
                user_id=user.get("id", ""),
                response_url=body.get("response_url", ""),
                trigger_id=body.get("trigger_id", ""),
                extra=body,
            )
        return InteractionPayload(
            type=getattr(body, "type", ""),
            action_id="",
            value="",
        )

    # ── Signature verification ─────────────────────────────────────────────

    def verify_signature(
        self,
        timestamp: str,
        body: str,
        signature: str,
    ) -> bool:
        """
        Verify a Slack request signature.

        Slack signs requests using HMAC-SHA256 with the signing secret.

        Args:
            timestamp: The X-Slack-Request-Timestamp header value.
            body: The raw request body string.
            signature: The X-Slack-Signature header value.

        Returns:
            True if the signature is valid.
        """
        if not self._signing_secret:
            return True

        # Check timestamp to prevent replay attacks (5 minute window)
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > 300:
                return False
        except (ValueError, TypeError):
            return False

        sig_basestring = f"v0:{timestamp}:{body}"
        expected = "v0=" + hmac.new(
            self._signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    # ── Slack Web API ──────────────────────────────────────────────────────

    async def _api_post(
        self,
        endpoint: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Make a POST request to the Slack Web API."""
        url = f"{SLACK_API_BASE}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json",
        }

        if _AIOHTTP_AVAILABLE:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        return {"ok": False, "error": "json_decode_failed"}

        elif _URLLIB_AVAILABLE:
            return await asyncio.to_thread(
                self._sync_api_post, url, payload, headers
            )

        return {"ok": False, "error": "no_http_client"}

    @staticmethod
    def _sync_api_post(
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        """Synchronous API POST fallback."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Built-in resolver integration ──────────────────────────────────────

    async def handle_resolve_command(
        self,
        payload: SlashCommandPayload,
    ) -> str:
        """
        Handle the /resolve slash command by triggering the IssueResolver.

        Parses the issue URL to extract provider, repo, and issue number,
        then delegates to the resolver.
        """
        from app.integrations.resolver import (
            IssueResolver,
            ResolutionRequest,
            ResolutionType,
        )

        text = payload.text.strip()
        if not text:
            return (
                "Please provide an issue URL or reference.\n"
                "Example: `/resolve https://github.com/owner/repo/issues/42`"
            )

        # Parse the URL
        parsed = self._parse_issue_reference(text)
        if not parsed:
            return (
                f"Could not parse issue reference from: `{text}`\n"
                f"Supported formats:\n"
                f"• `https://github.com/owner/repo/issues/42`\n"
                f"• `owner/repo#42`\n"
                f"• `github:owner/repo:42`"
            )

        provider, repo_id, issue_number = parsed

        resolver = IssueResolver()
        try:
            result = await resolver.resolve_issue(
                provider=provider,
                repo_id=repo_id,
                issue_number=issue_number,
                timeout_seconds=600.0,
            )

            if result.status.value == "completed":
                return (
                    f"Resolution completed in {result.duration_seconds:.1f}s\n\n"
                    f"*Summary:* {result.summary[:1000]}\n\n"
                    f"{'*Changes:* ' + ', '.join(f'`{c}`' for c in result.changes_applied) if result.changes_applied else ''}"
                )
            else:
                return (
                    f"Resolution {result.status.value}: {result.error or 'Unknown error'}"
                )

        except Exception as exc:
            return f"Resolution failed: {exc}"

    async def handle_review_command(
        self,
        payload: SlashCommandPayload,
    ) -> str:
        """
        Handle the /review slash command by triggering the IssueResolver
        in PR update mode.
        """
        from app.integrations.resolver import (
            IssueResolver,
        )

        text = payload.text.strip()
        if not text:
            return "Please provide a PR URL.\nExample: `/review https://github.com/owner/repo/pull/123`"

        parsed = self._parse_pr_reference(text)
        if not parsed:
            return f"Could not parse PR reference from: `{text}`"

        provider, repo_id, pr_number = parsed

        resolver = IssueResolver()
        try:
            result = await resolver.update_pr(
                provider=provider,
                repo_id=repo_id,
                pr_number=pr_number,
                timeout_seconds=600.0,
            )

            if result.status.value == "completed":
                return (
                    f"Review completed in {result.duration_seconds:.1f}s\n\n"
                    f"*Summary:* {result.summary[:1000]}"
                )
            else:
                return f"Review {result.status.value}: {result.error or 'Unknown error'}"

        except Exception as exc:
            return f"Review failed: {exc}"

    # ── URL/reference parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_issue_reference(
        text: str,
    ) -> Optional[tuple]:
        """
        Parse an issue reference to (provider, repo_id, issue_number).

        Supported formats:
          - https://github.com/owner/repo/issues/42
          - owner/repo#42
          - github:owner/repo:42
        """
        import re

        # Full URL format
        url_match = re.match(
            r"https?://(?:github\.com|gitlab\.com|bitbucket\.org|dev\.azure\.com)/"
            r"([^/]+/[^/]+)/(?:issues|-/issues)/(\d+)",
            text,
        )
        if url_match:
            repo_id = url_match.group(1)
            issue_number = int(url_match.group(2))
            # Detect provider from URL
            if "github.com" in text:
                provider = "github"
            elif "gitlab.com" in text:
                provider = "gitlab"
            elif "bitbucket.org" in text:
                provider = "bitbucket"
            else:
                provider = "azure_devops"
            return provider, repo_id, issue_number

        # Shorthand: owner/repo#42
        shorthand_match = re.match(
            r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)$", text
        )
        if shorthand_match:
            return "github", shorthand_match.group(1), int(shorthand_match.group(2))

        # Colon-separated: github:owner/repo:42
        colon_match = re.match(
            r"^(github|gitlab|azure_devops|bitbucket):"
            r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+):(\d+)$",
            text,
        )
        if colon_match:
            return colon_match.group(1), colon_match.group(2), int(colon_match.group(3))

        return None

    @staticmethod
    def _parse_pr_reference(
        text: str,
    ) -> Optional[tuple]:
        """Parse a PR reference to (provider, repo_id, pr_number)."""
        import re

        url_match = re.match(
            r"https?://(?:github\.com|gitlab\.com|bitbucket\.org)/"
            r"([^/]+/[^/]+)/(?:pull|merge_requests|pull-requests)/(\d+)",
            text,
        )
        if url_match:
            repo_id = url_match.group(1)
            pr_number = int(url_match.group(2))
            if "github.com" in text:
                provider = "github"
            elif "gitlab.com" in text:
                provider = "gitlab"
            else:
                provider = "bitbucket"
            return provider, repo_id, pr_number

        shorthand_match = re.match(
            r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)!(\d+)$", text
        )
        if shorthand_match:
            return "github", shorthand_match.group(1), int(shorthand_match.group(2))

        colon_match = re.match(
            r"^(github|gitlab|azure_devops|bitbucket):"
            r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+):pr(\d+)$",
            text,
        )
        if colon_match:
            return colon_match.group(1), colon_match.group(2), int(colon_match.group(3))

        return None


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

slack_integration = SlackIntegration()
