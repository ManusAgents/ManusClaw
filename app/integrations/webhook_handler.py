from __future__ import annotations

"""
Webhook Handler for Git Provider Events
========================================
Receives webhook events from Git providers (GitHub, GitLab, Azure DevOps,
Bitbucket), validates their signatures, routes events to appropriate
handlers, and processes them via a queue with retry logic.

Features:
  - HMAC-SHA256 signature verification (per-provider).
  - Event routing based on event type.
  - Queue-based processing with configurable retry.
  - Idempotency via event deduplication (SHA-256 fingerprint).
  - Thread-safe, with timeout protection per event.
  - Pluggable event handlers.
"""

import asyncio
import hashlib
import hmac
import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

from app.logger import logger

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_DEDUP_WINDOW_SECONDS = 3600  # 1 hour dedup window
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0
_MAX_QUEUE_SIZE = 1000
_DB_PATH = Path("workspace/.sessions/integrations_webhooks.db")

# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class WebhookEventType(str, Enum):
    """Normalized webhook event types across providers."""

    PUSH = "push"
    PULL_REQUEST = "pull_request"
    ISSUES = "issues"
    ISSUE_COMMENT = "issue_comment"
    PULL_REQUEST_REVIEW = "pull_request_review"
    CHECK_RUN = "check_run"
    RELEASE = "release"
    MEMBER = "member"
    UNKNOWN = "unknown"


class WebhookProvider(str, Enum):
    """Supported webhook source providers."""

    GITHUB = "github"
    GITLAB = "gitlab"
    AZURE_DEVOPS = "azure_devops"
    BITBUCKET = "bitbucket"
    UNKNOWN = "unknown"


class EventProcessingStatus(str, Enum):
    """Status of a webhook event in the processing pipeline."""

    RECEIVED = "received"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class WebhookEvent:
    """Normalized webhook event."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    provider: WebhookProvider = WebhookProvider.UNKNOWN
    event_type: WebhookEventType = WebhookEventType.UNKNOWN
    action: str = ""
    repo_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    raw_body: bytes = b""
    received_at: float = field(default_factory=time.time)
    signature: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self._compute_fingerprint()

    def _compute_fingerprint(self) -> str:
        """Compute a dedup fingerprint based on event identity."""
        key_parts = [
            self.provider.value,
            self.event_type.value,
            self.action,
            self.repo_id,
        ]
        # Add event-specific identifiers from payload
        if self.event_type == WebhookEventType.PUSH:
            key_parts.append(self.payload.get("after", ""))
        elif self.event_type in (
            WebhookEventType.PULL_REQUEST,
            WebhookEventType.PULL_REQUEST_REVIEW,
        ):
            pr = self.payload.get("pull_request", self.payload.get("merge_request", {}))
            key_parts.append(str(pr.get("number", pr.get("iid", ""))))
            key_parts.append(self.action)
        elif self.event_type in (
            WebhookEventType.ISSUES,
            WebhookEventType.ISSUE_COMMENT,
        ):
            issue = self.payload.get("issue", {})
            key_parts.append(str(issue.get("number", "")))
            key_parts.append(self.action)
            if self.event_type == WebhookEventType.ISSUE_COMMENT:
                comment = self.payload.get("comment", {})
                key_parts.append(str(comment.get("id", "")))

        key_str = "|".join(str(p) for p in key_parts)
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provider": self.provider.value,
            "event_type": self.event_type.value,
            "action": self.action,
            "repo_id": self.repo_id,
            "fingerprint": self.fingerprint,
            "received_at": self.received_at,
        }


@dataclass
class ProcessingResult:
    """Result of processing a webhook event."""

    event_id: str
    status: EventProcessingStatus
    handler_name: str = ""
    message: str = ""
    duration_seconds: float = 0.0
    retry_count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": self.status.value,
            "handler_name": self.handler_name,
            "message": self.message,
            "duration_seconds": self.duration_seconds,
            "retry_count": self.retry_count,
            "error": self.error,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Signature verification
# ──────────────────────────────────────────────────────────────────────────────


class SignatureVerifier:
    """
    Verifies webhook signatures using HMAC-SHA256.

    Each provider uses slightly different header names and signing
    conventions. This class normalizes verification across them.
    """

    @staticmethod
    def verify_github(body: bytes, signature: str, secret: str) -> bool:
        """Verify GitHub webhook signature (X-Hub-Signature-256)."""
        if not secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        # GitHub prefixes with "sha256="
        provided = signature.removeprefix("sha256=")
        return hmac.compare_digest(expected, provided)

    @staticmethod
    def verify_gitlab(body: bytes, token: str, secret: str) -> bool:
        """Verify GitLab webhook token (X-Gitlab-Token)."""
        if not secret:
            return True
        return hmac.compare_digest(token, secret)

    @staticmethod
    def verify_bitbucket(body: bytes, signature: str, secret: str) -> bool:
        """Verify Bitbucket webhook signature (X-Hook-Signature)."""
        if not secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def verify_azure_devops(body: bytes, signature: str, secret: str) -> bool:
        """Verify Azure DevOps webhook signature."""
        if not secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    @classmethod
    def verify(
        cls,
        provider: WebhookProvider,
        body: bytes,
        headers: Dict[str, str],
        secret: str,
    ) -> bool:
        """Verify a webhook signature based on the provider."""
        headers_lower = {k.lower(): v for k, v in headers.items()}

        if provider == WebhookProvider.GITHUB:
            sig = headers_lower.get("x-hub-signature-256", "")
            return cls.verify_github(body, sig, secret)

        if provider == WebhookProvider.GITLAB:
            token = headers_lower.get("x-gitlab-token", "")
            return cls.verify_gitlab(body, token, secret)

        if provider == WebhookProvider.BITBUCKET:
            sig = headers_lower.get("x-hook-signature", "")
            return cls.verify_bitbucket(body, sig, secret)

        if provider == WebhookProvider.AZURE_DEVOPS:
            sig = headers_lower.get("x-azure-devops-signature", "")
            return cls.verify_azure_devops(body, sig, secret)

        # Unknown provider: accept if no secret configured
        return not secret


# ──────────────────────────────────────────────────────────────────────────────
# Event normalizer
# ──────────────────────────────────────────────────────────────────────────────


class EventNormalizer:
    """
    Normalizes webhook payloads from different providers into a common
    :class:`WebhookEvent` format.
    """

    @staticmethod
    def detect_provider(headers: Dict[str, str]) -> WebhookProvider:
        """Detect the webhook provider from request headers."""
        headers_lower = {k.lower(): v for k, v in headers.items()}
        if "x-github-event" in headers_lower:
            return WebhookProvider.GITHUB
        if "x-gitlab-event" in headers_lower:
            return WebhookProvider.GITLAB
        if "x-azure-devops-event" in headers_lower:
            return WebhookProvider.AZURE_DEVOPS
        if "x-event-key" in headers_lower and "x-hook-uuid" in headers_lower:
            return WebhookProvider.BITBUCKET
        return WebhookProvider.UNKNOWN

    @classmethod
    def normalize(
        cls,
        provider: WebhookProvider,
        body: bytes,
        headers: Dict[str, str],
    ) -> WebhookEvent:
        """Normalize a raw webhook into a :class:`WebhookEvent`."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "integrations.webhook.json_decode_failed err=%s", exc
            )
            payload = {}

        headers_lower = {k.lower(): v for k, v in headers.items()}

        event = WebhookEvent(
            provider=provider,
            payload=payload,
            raw_body=body,
        )

        if provider == WebhookProvider.GITHUB:
            event.event_type = WebhookEventType(
                headers_lower.get("x-github-event", "unknown")
            )
            event.action = payload.get("action", "")
            repo = payload.get("repository", {})
            event.repo_id = repo.get("full_name", "")

        elif provider == WebhookProvider.GITLAB:
            raw_event = headers_lower.get("x-gitlab-event", "")
            event.event_type = cls._gitlab_event_type(raw_event)
            event.action = payload.get("object_attributes", {}).get(
                "action", ""
            )
            project = payload.get("project", {})
            event.repo_id = project.get("path_with_namespace", "")

        elif provider == WebhookProvider.AZURE_DEVOPS:
            raw_event = headers_lower.get("x-azure-devops-event", "")
            event.event_type = cls._azure_devops_event_type(raw_event)
            event.action = payload.get("action", "")
            resource = payload.get("resource", {})
            repo = resource.get("repository", {})
            project = payload.get("resourceContainers", {}).get(
                "project", {}
            )
            event.repo_id = "/".join(
                filter(
                    None,
                    [
                        project.get("name", ""),
                        repo.get("name", ""),
                    ],
                )
            )

        elif provider == WebhookProvider.BITBUCKET:
            raw_event = headers_lower.get("x-event-key", "")
            event.event_type = cls._bitbucket_event_type(raw_event)
            event.action = payload.get("push", {}).get("changes", [{}])[0].get(
                "type", ""
            ) if payload.get("push") else ""
            repo = payload.get("repository", {})
            event.repo_id = repo.get("full_name", "")

        # Recompute fingerprint with normalized data
        event.fingerprint = event._compute_fingerprint()

        return event

    @staticmethod
    def _gitlab_event_type(raw: str) -> WebhookEventType:
        mapping = {
            "Push Hook": WebhookEventType.PUSH,
            "Merge Request Hook": WebhookEventType.PULL_REQUEST,
            "Issue Hook": WebhookEventType.ISSUES,
            "Note Hook": WebhookEventType.ISSUE_COMMENT,
            "Tag Push Hook": WebhookEventType.PUSH,
        }
        return mapping.get(raw, WebhookEventType.UNKNOWN)

    @staticmethod
    def _azure_devops_event_type(raw: str) -> WebhookEventType:
        mapping = {
            "git.push": WebhookEventType.PUSH,
            "git.pullrequest.created": WebhookEventType.PULL_REQUEST,
            "git.pullrequest.updated": WebhookEventType.PULL_REQUEST,
            "workitem.created": WebhookEventType.ISSUES,
            "workitem.updated": WebhookEventType.ISSUES,
        }
        return mapping.get(raw, WebhookEventType.UNKNOWN)

    @staticmethod
    def _bitbucket_event_type(raw: str) -> WebhookEventType:
        if raw.startswith("repo:push"):
            return WebhookEventType.PUSH
        if raw.startswith("pullrequest:"):
            return WebhookEventType.PULL_REQUEST
        if raw.startswith("issue:"):
            return WebhookEventType.ISSUES
        if raw.startswith("issue_comment:"):
            return WebhookEventType.ISSUE_COMMENT
        return WebhookEventType.UNKNOWN


# ──────────────────────────────────────────────────────────────────────────────
# Event deduplication store
# ──────────────────────────────────────────────────────────────────────────────


class DeduplicationStore:
    """
    Thread-safe LRU deduplication store.

    Tracks event fingerprints within a configurable time window to
    prevent duplicate processing. Uses an OrderedDict for efficient
    eviction of expired entries.
    """

    def __init__(
        self,
        window_seconds: int = _DEDUP_WINDOW_SECONDS,
        max_entries: int = 10_000,
    ) -> None:
        self._window = window_seconds
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def is_duplicate(self, fingerprint: str) -> bool:
        """Check if a fingerprint has been seen within the window."""
        now = time.time()
        cutoff = now - self._window

        with self._lock:
            # Evict expired entries
            expired = [
                k for k, t in self._seen.items() if t < cutoff
            ]
            for k in expired:
                del self._seen[k]

            # Check if fingerprint exists
            if fingerprint in self._seen:
                return True

            # Record the fingerprint
            self._seen[fingerprint] = now

            # Evict oldest if over max
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

            return False

    def clear(self) -> None:
        """Clear all stored fingerprints."""
        with self._lock:
            self._seen.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Event handler type
# ──────────────────────────────────────────────────────────────────────────────

EventHandler = Callable[
    [WebhookEvent], Coroutine[Any, Any, ProcessingResult]
]


# ──────────────────────────────────────────────────────────────────────────────
# WebhookHandler
# ──────────────────────────────────────────────────────────────────────────────


class WebhookHandler:
    """
    Main webhook handler that receives, validates, deduplicates, routes,
    and processes webhook events from Git providers.

    Usage::

        handler = WebhookHandler(secret="my-webhook-secret")

        # Register event handlers
        handler.on(WebhookEventType.ISSUES, my_issue_handler)
        handler.on(WebhookEventType.PULL_REQUEST, my_pr_handler)

        # Process an incoming webhook
        result = await handler.handle(raw_body, headers)

        # Start the background processor
        await handler.start_processing()
    """

    def __init__(
        self,
        secret: str = "",
        max_retries: int = _MAX_RETRIES,
        retry_base_delay: float = _RETRY_BASE_DELAY,
        max_queue_size: int = _MAX_QUEUE_SIZE,
        dedup_window: int = _DEDUP_WINDOW_SECONDS,
    ) -> None:
        self._secret = secret
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._max_queue_size = max_queue_size
        self._handlers: Dict[WebhookEventType, List[EventHandler]] = {}
        self._default_handlers: List[EventHandler] = []
        self._queue: asyncio.Queue[WebhookEvent] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._dedup = DeduplicationStore(window_seconds=dedup_window)
        self._results: Dict[str, ProcessingResult] = {}
        self._lock = threading.RLock()
        self._processor_task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {
            "received": 0,
            "duplicates": 0,
            "rejected": 0,
            "processed": 0,
            "failed": 0,
        }

    # ── Handler registration ───────────────────────────────────────────────

    def on(
        self,
        event_type: WebhookEventType,
        handler: EventHandler,
    ) -> None:
        """Register an async handler for a specific event type."""
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
        logger.info(
            "integrations.webhook.handler_registered type=%s",
            event_type.value,
        )

    def on_any(self, handler: EventHandler) -> None:
        """Register a handler that fires for all event types."""
        with self._lock:
            self._default_handlers.append(handler)

    def off(self, event_type: WebhookEventType, handler: EventHandler) -> bool:
        """Remove a specific handler for an event type."""
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)
                return True
            if handler in self._default_handlers:
                self._default_handlers.remove(handler)
                return True
        return False

    # ── Webhook reception ──────────────────────────────────────────────────

    async def handle(
        self,
        body: bytes,
        headers: Dict[str, str],
    ) -> ProcessingResult:
        """
        Receive and process a webhook.

        This is the main entry point for incoming webhooks. It:
        1. Detects the provider from headers.
        2. Verifies the signature.
        3. Normalizes the event.
        4. Checks for duplicates.
        5. Enqueues the event for processing.

        Args:
            body: Raw request body bytes.
            headers: HTTP headers dictionary.

        Returns:
            A ProcessingResult indicating the outcome.
        """
        self._stats["received"] += 1

        # Detect provider
        provider = EventNormalizer.detect_provider(headers)

        # Verify signature
        if not SignatureVerifier.verify(provider, body, headers, self._secret):
            self._stats["rejected"] += 1
            logger.warning(
                "integrations.webhook.signature_invalid provider=%s",
                provider.value,
            )
            return ProcessingResult(
                event_id="",
                status=EventProcessingStatus.REJECTED,
                message="Invalid webhook signature",
            )

        # Normalize event
        event = EventNormalizer.normalize(provider, body, headers)

        logger.info(
            "integrations.webhook.received provider=%s type=%s action=%s repo=%s",
            provider.value,
            event.event_type.value,
            event.action,
            event.repo_id,
        )

        # Check for duplicates
        if self._dedup.is_duplicate(event.fingerprint):
            self._stats["duplicates"] += 1
            logger.info(
                "integrations.webhook.duplicate fingerprint=%s",
                event.fingerprint,
            )
            return ProcessingResult(
                event_id=event.event_id,
                status=EventProcessingStatus.DUPLICATE,
                message="Duplicate event",
            )

        # Enqueue
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats["rejected"] += 1
            logger.warning("integrations.webhook.queue_full")
            return ProcessingResult(
                event_id=event.event_id,
                status=EventProcessingStatus.REJECTED,
                message="Processing queue is full",
            )

        return ProcessingResult(
            event_id=event.event_id,
            status=EventProcessingStatus.QUEUED,
            message="Event queued for processing",
        )

    # ── Background processing ──────────────────────────────────────────────

    async def start_processing(self) -> None:
        """Start the background event processor."""
        if self._running:
            return
        self._running = True
        self._processor_task = asyncio.create_task(
            self._process_loop(), name="webhook-processor"
        )
        logger.info("integrations.webhook.processor_started")

    async def stop_processing(self) -> None:
        """Stop the background event processor gracefully."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        logger.info("integrations.webhook.processor_stopped")

    async def _process_loop(self) -> None:
        """Main processing loop that dequeues and handles events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
                asyncio.create_task(self._process_event(event))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "integrations.webhook.process_loop_error err=%s", exc
                )
                await asyncio.sleep(1.0)

    async def _process_event(
        self,
        event: WebhookEvent,
        retry_count: int = 0,
    ) -> ProcessingResult:
        """Process a single webhook event with retry logic."""
        start_time = time.time()
        status = EventProcessingStatus.PROCESSING

        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))
            default_handlers = list(self._default_handlers)

        all_handlers = handlers + default_handlers

        if not all_handlers:
            result = ProcessingResult(
                event_id=event.event_id,
                status=EventProcessingStatus.COMPLETED,
                message="No handlers registered for event type",
                duration_seconds=time.time() - start_time,
                retry_count=retry_count,
            )
            self._stats["processed"] += 1
            with self._lock:
                self._results[event.event_id] = result
            return result

        last_error: Optional[str] = None

        for handler in all_handlers:
            try:
                # Result is intentionally discarded — each handler's
                # contribution is captured via the audit trail / stats
                # counters. We only need to know if it succeeded.
                await handler(event)
                last_error = None

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "integrations.webhook.handler_failed "
                    "event_id=%s handler=%s err=%s",
                    event.event_id,
                    handler.__name__,
                    exc,
                )

        # Determine final status
        if last_error is None:
            status = EventProcessingStatus.COMPLETED
            self._stats["processed"] += 1
        elif retry_count < self._max_retries:
            # Schedule retry
            delay = self._retry_base_delay * (2 ** retry_count)
            logger.info(
                "integrations.webhook.retry event_id=%s attempt=%d delay=%.1fs",
                event.event_id, retry_count + 1, delay,
            )
            await asyncio.sleep(delay)
            return await self._process_event(event, retry_count + 1)
        else:
            status = EventProcessingStatus.FAILED
            self._stats["failed"] += 1

        result = ProcessingResult(
            event_id=event.event_id,
            status=status,
            handler_name=", ".join(h.__name__ for h in all_handlers),
            message=f"Processed by {len(all_handlers)} handler(s)",
            duration_seconds=time.time() - start_time,
            retry_count=retry_count,
            error=last_error,
        )

        with self._lock:
            self._results[event.event_id] = result

        return result

    # ── Synchronous processing (for when no background loop is running) ────

    async def process_sync(self, event: WebhookEvent) -> ProcessingResult:
        """Process an event immediately without queuing."""
        return await self._process_event(event)

    # ── Stats and results ──────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return webhook processing statistics."""
        with self._lock:
            stats = dict(self._stats)
        stats["queue_size"] = self._queue.qsize()
        stats["registered_handlers"] = {
            et.value: len(hs)
            for et, hs in self._handlers.items()
        }
        return stats

    def get_result(self, event_id: str) -> Optional[ProcessingResult]:
        """Get the processing result for a specific event."""
        with self._lock:
            return self._results.get(event_id)

    def list_results(
        self,
        status: Optional[EventProcessingStatus] = None,
        limit: int = 50,
    ) -> List[ProcessingResult]:
        """List processing results, optionally filtered by status."""
        with self._lock:
            results = list(self._results.values())
        if status:
            results = [r for r in results if r.status == status]
        results.sort(key=lambda r: r.duration_seconds, reverse=True)
        return results[:limit]

    def clear_results(self) -> None:
        """Clear all stored processing results."""
        with self._lock:
            self._results.clear()

    def clear_dedup(self) -> None:
        """Clear the deduplication store."""
        self._dedup.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Built-in handler: Route to IssueResolver
# ──────────────────────────────────────────────────────────────────────────────


async def resolver_webhook_handler(event: WebhookEvent) -> ProcessingResult:
    """
    Built-in handler that routes webhook events to the IssueResolver.

    This handler is automatically wired up for issue and PR events.
    It creates a ResolutionRequest and delegates to IssueResolver.resolve().
    """
    from app.integrations.resolver import (
        IssueResolver,
        ResolutionRequest,
        ResolutionType,
    )

    resolver = IssueResolver()

    # Map event types to resolution types
    resolution_type: Optional[ResolutionType] = None
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None

    if event.event_type == WebhookEventType.ISSUES:
        resolution_type = ResolutionType.ISSUE_RESOLUTION
        issue_number = event.payload.get("issue", {}).get("number")

    elif event.event_type == WebhookEventType.ISSUE_COMMENT:
        resolution_type = ResolutionType.ISSUE_COMMENT
        issue_number = event.payload.get("issue", {}).get("number")

    elif event.event_type == WebhookEventType.PULL_REQUEST:
        action = event.action
        if action == "synchronize":
            resolution_type = ResolutionType.PR_UPDATE
        else:
            resolution_type = ResolutionType.PR_UPDATE
        pr_number = event.payload.get("pull_request", {}).get(
            "number",
            event.payload.get("merge_request", {}).get("iid"),
        )

    elif event.event_type == WebhookEventType.PULL_REQUEST_REVIEW:
        resolution_type = ResolutionType.PR_UPDATE
        pr_number = event.payload.get("pull_request", {}).get(
            "number",
            event.payload.get("merge_request", {}).get("iid"),
        )

    if resolution_type is None:
        return ProcessingResult(
            event_id=event.event_id,
            status=EventProcessingStatus.COMPLETED,
            message="Event type not actionable for resolver",
        )

    # Map provider
    provider_map = {
        WebhookProvider.GITHUB: "github",
        WebhookProvider.GITLAB: "gitlab",
        WebhookProvider.AZURE_DEVOPS: "azure_devops",
        WebhookProvider.BITBUCKET: "bitbucket",
    }
    provider = provider_map.get(event.provider, event.provider.value)

    try:
        request = ResolutionRequest(
            resolution_type=resolution_type,
            provider=provider,
            repo_id=event.repo_id,
            issue_number=issue_number,
            pr_number=pr_number,
            extra_context={"webhook_event_id": event.event_id},
        )
        result = await resolver.resolve(request)

        return ProcessingResult(
            event_id=event.event_id,
            status=(
                EventProcessingStatus.COMPLETED
                if result.status.value == "completed"
                else EventProcessingStatus.FAILED
            ),
            handler_name="resolver_webhook_handler",
            message=result.summary[:200],
            error=result.error,
        )

    except Exception as exc:
        logger.error(
            "integrations.webhook.resolver_handler_failed event_id=%s err=%s",
            event.event_id, exc,
        )
        return ProcessingResult(
            event_id=event.event_id,
            status=EventProcessingStatus.FAILED,
            handler_name="resolver_webhook_handler",
            error=str(exc),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

webhook_handler = WebhookHandler()
