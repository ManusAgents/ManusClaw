"""ManusClaw Webhook System.

Provides incoming webhook management with HMAC-SHA256 verification.
Webhook triggers format a prompt from a template + payload and run the
agent in the background.

The WebhookManager persists webhook configurations in the SessionDB SQLite
database and exposes FastAPI endpoints for registration, triggering, and listing.

Classes:
    WebhookConfig   — dataclass for a single webhook configuration
    WebhookManager   — manages webhook registration, verification, and triggering
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.logger import logger

# ─── Database ─────────────────────────────────────────────────────────────────

_DB_PATH = Path("workspace/.sessions/manusclaw.db")

_WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
    hook_id        TEXT PRIMARY KEY,
    url            TEXT,
    prompt_template TEXT,
    hmac_secret    TEXT,
    target_session TEXT DEFAULT '',
    enabled        INTEGER DEFAULT 1,
    created_at     REAL,
    trigger_count  INTEGER DEFAULT 0,
    last_triggered REAL DEFAULT 0
);
"""


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class WebhookConfig:
    """Configuration for a single incoming webhook.

    Attributes:
        hook_id:         Unique identifier for the webhook.
        url:             The URL the webhook is associated with (informational).
        prompt_template: Jinja-like template for formatting the agent prompt.
                         Use ``{{payload.field}}`` to interpolate payload fields.
        hmac_secret:     Shared secret for HMAC-SHA256 signature verification.
                         Empty string disables verification.
        target_session:  Optional session ID to route the agent prompt to.
        enabled:         Whether the webhook is active.
    """
    hook_id: str
    url: str = ""
    prompt_template: str = ""
    hmac_secret: str = ""
    target_session: str = ""
    enabled: bool = True

    # Runtime metadata (not persisted)
    created_at: float = 0.0
    trigger_count: int = 0
    last_triggered: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary (excludes hmac_secret for safety)."""
        return {
            "hook_id": self.hook_id,
            "url": self.url,
            "prompt_template": self.prompt_template,
            "hmac_secret_set": bool(self.hmac_secret),
            "target_session": self.target_session,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "trigger_count": self.trigger_count,
            "last_triggered": self.last_triggered,
        }

    def to_db_row(self) -> tuple:
        """Return values suitable for SQLite INSERT/REPLACE."""
        return (
            self.hook_id,
            self.url,
            self.prompt_template,
            self.hmac_secret,
            self.target_session,
            1 if self.enabled else 0,
            self.created_at or time.time(),
            self.trigger_count,
            self.last_triggered,
        )


# ─── WebhookManager ──────────────────────────────────────────────────────────

class WebhookManager:
    """Manages webhook registration, HMAC verification, and agent triggering.

    Persists webhook configurations in the ManusClaw SQLite database.

    Usage::

        mgr = WebhookManager()
        mgr.register(WebhookConfig(
            hook_id="my-hook",
            url="https://example.com/webhook",
            prompt_template="Alert from {{payload.source}}: {{payload.message}}",
            hmac_secret="my-secret-key",
        ))

        # Trigger a webhook
        result = await mgr.trigger("my-hook", {"source": "monitor", "message": "CPU high"})

        # Verify HMAC signature
        valid = mgr.verify_hmac("my-hook", payload_bytes, received_signature)
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._cache: dict[str, WebhookConfig] = {}

    # ─── Database ───────────────────────────────────────────────────────────

    def _ensure(self) -> sqlite3.Connection:
        """Ensure database connection and schema are ready."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.executescript(_WEBHOOK_SCHEMA)
            self._conn.commit()
        return self._conn

    def _load_cache(self) -> None:
        """Load all webhook configs from the database into the in-memory cache."""
        conn = self._ensure()
        rows = conn.execute(
            "SELECT hook_id, url, prompt_template, hmac_secret, target_session, "
            "enabled, created_at, trigger_count, last_triggered FROM webhooks"
        ).fetchall()

        self._cache.clear()
        for row in rows:
            config = WebhookConfig(
                hook_id=row[0],
                url=row[1],
                prompt_template=row[2],
                hmac_secret=row[3],
                target_session=row[4],
                enabled=bool(row[5]),
                created_at=row[6],
                trigger_count=row[7],
                last_triggered=row[8],
            )
            self._cache[config.hook_id] = config

    # ─── Registration ───────────────────────────────────────────────────────

    def register(self, config: WebhookConfig) -> WebhookConfig:
        """Register a new webhook or update an existing one.

        Args:
            config: The webhook configuration to register.

        Returns:
            The registered configuration (with created_at set if new).
        """
        conn = self._ensure()

        # Set defaults
        if not config.hook_id:
            config.hook_id = uuid.uuid4().hex[:12]
        if not config.created_at:
            config.created_at = time.time()

        conn.execute(
            """INSERT OR REPLACE INTO webhooks
               (hook_id, url, prompt_template, hmac_secret, target_session,
                enabled, created_at, trigger_count, last_triggered)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            config.to_db_row(),
        )
        conn.commit()

        self._cache[config.hook_id] = config
        logger.info(f"[Webhooks] Registered webhook: {config.hook_id}")
        return config

    def unregister(self, hook_id: str) -> bool:
        """Remove a webhook by its ID.

        Args:
            hook_id: The webhook ID to remove.

        Returns:
            True if the webhook was found and removed, False otherwise.
        """
        conn = self._ensure()
        cursor = conn.execute("DELETE FROM webhooks WHERE hook_id = ?", (hook_id,))
        conn.commit()

        if cursor.rowcount > 0:
            self._cache.pop(hook_id, None)
            logger.info(f"[Webhooks] Unregistered webhook: {hook_id}")
            return True

        logger.warning(f"[Webhooks] Webhook not found: {hook_id}")
        return False

    # ─── Query ────────────────────────────────────────────────────────────

    def get(self, hook_id: str) -> Optional[WebhookConfig]:
        """Get a webhook configuration by ID.

        Checks the in-memory cache first, then falls back to the database.
        """
        if hook_id in self._cache:
            return self._cache[hook_id]

        conn = self._ensure()
        row = conn.execute(
            "SELECT hook_id, url, prompt_template, hmac_secret, target_session, "
            "enabled, created_at, trigger_count, last_triggered "
            "FROM webhooks WHERE hook_id = ?",
            (hook_id,),
        ).fetchone()

        if not row:
            return None

        config = WebhookConfig(
            hook_id=row[0],
            url=row[1],
            prompt_template=row[2],
            hmac_secret=row[3],
            target_session=row[4],
            enabled=bool(row[5]),
            created_at=row[6],
            trigger_count=row[7],
            last_triggered=row[8],
        )
        self._cache[hook_id] = config
        return config

    def list_all(self) -> list[WebhookConfig]:
        """List all registered webhooks.

        Loads from the database if the cache is empty.
        """
        if not self._cache:
            self._load_cache()
        return list(self._cache.values())

    # ─── HMAC Verification ────────────────────────────────────────────────

    def verify_hmac(
        self,
        hook_id: str,
        payload: bytes,
        signature: str,
    ) -> bool:
        """Verify an HMAC-SHA256 signature for a webhook payload.

        Args:
            hook_id: The webhook ID.
            payload: Raw request body bytes.
            signature: The HMAC signature from the request header.

        Returns:
            True if the signature is valid (or if HMAC is not configured for this hook).
        """
        config = self.get(hook_id)
        if config is None:
            logger.warning(f"[Webhooks] verify_hmac: unknown hook {hook_id}")
            return False

        if not config.hmac_secret:
            # No HMAC configured — accept without verification
            return True

        expected = hmac.new(
            config.hmac_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(expected, signature):
            return True

        logger.warning(f"[Webhooks] HMAC verification failed for {hook_id}")
        return False

    # ─── Trigger ─────────────────────────────────────────────────────────────

    async def trigger(
        self,
        hook_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Trigger a webhook and run the agent with the formatted prompt.

        Args:
            hook_id: The webhook ID to trigger.
            payload: The JSON payload from the incoming webhook request.

        Returns:
            Dictionary with trigger result status and optional agent output.
        """
        config = self.get(hook_id)
        if config is None:
            logger.error(f"[Webhooks] Trigger failed: unknown hook {hook_id}")
            return {"status": "error", "error": f"Unknown webhook: {hook_id}"}

        if not config.enabled:
            logger.warning(f"[Webhooks] Trigger skipped: webhook {hook_id} is disabled")
            return {"status": "error", "error": f"Webhook {hook_id} is disabled"}

        # Format the prompt from template + payload
        prompt = self._format_prompt(config.prompt_template, payload)
        if not prompt:
            prompt = json.dumps(payload, default=str)

        logger.info(f"[Webhooks] Triggering {hook_id}: {prompt[:100]}")

        # Update trigger stats
        conn = self._ensure()
        conn.execute(
            "UPDATE webhooks SET trigger_count = trigger_count + 1, "
            "last_triggered = ? WHERE hook_id = ?",
            (time.time(), hook_id),
        )
        conn.commit()
        config.trigger_count += 1
        config.last_triggered = time.time()

        # Run the agent
        try:
            from app.agent.manus import Manus

            agent = Manus(
                session_id=config.target_session or None,
            )
            output = await agent.run(prompt)
            logger.info(f"[Webhooks] Agent completed for {hook_id}")
            return {
                "status": "ok",
                "hook_id": hook_id,
                "agent_output": output[:4000],
                "trigger_count": config.trigger_count,
            }

        except Exception as exc:
            logger.error(f"[Webhooks] Agent error for {hook_id}: {exc}")
            return {
                "status": "error",
                "hook_id": hook_id,
                "error": str(exc),
            }

    # ─── Template Formatting ───────────────────────────────────────────────

    def _format_prompt(self, template: str, payload: dict[str, Any]) -> str:
        """Format a prompt template by interpolating payload fields.

        Supports ``{{payload.field}}`` and ``{{payload.nested.field}}`` syntax.
        Falls back to ``str()`` representation if the field is not found.
        """
        if not template:
            return ""

        import re

        def _replace(match: re.Match) -> str:
            path = match.group(1).strip()
            # Strip 'payload.' prefix if present
            if path.startswith("payload."):
                path = path[len("payload."):]

            # Navigate nested dicts
            value = payload
            for key in path.split("."):
                if isinstance(value, dict):
                    value = value.get(key, f"<{path}>")
                else:
                    return f"<{path}>"

            return str(value)

        result = re.sub(r"\{\{(.+?)\}\}", _replace, template)
        return result

    # ─── Cleanup ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
        self._cache.clear()


# ─── Global Instance ─────────────────────────────────────────────────────────

webhook_manager = WebhookManager()


def main_cli() -> None:
    """Entry point for ``manusclaw-webhook`` — list and manage webhooks from the CLI."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="manusclaw-webhook",
        description="ManusClaw Webhook Management",
    )
    parser.add_argument("--list", action="store_true", help="List all webhooks")
    parser.add_argument("--info", metavar="HOOK_ID", help="Show webhook details")
    parser.add_argument("--remove", metavar="HOOK_ID", help="Remove a webhook")
    parser.add_argument(
        "--add", nargs=5,
        metavar=("ID", "URL", "TEMPLATE", "SECRET", "SESSION"),
        help="Add a webhook: ID URL TEMPLATE SECRET SESSION",
    )

    args = parser.parse_args()
    mgr = WebhookManager()

    if args.list:
        hooks = mgr.list_all()
        if not hooks:
            print("No webhooks registered.")
        else:
            print(f"{'ID':<16} {'URL':<35} {'TRIGGERS':>9}  {'ENABLED'}")
            print("-" * 75)
            for h in hooks:
                status = "yes" if h.enabled else "no"
                print(
                    f"{h.hook_id:<16} {h.url[:34]:<35} "
                    f"{h.trigger_count:>9}  {status}"
                )
        mgr.close()
        return

    if args.info:
        hook = mgr.get(args.info)
        if hook:
            print(json.dumps(hook.to_dict(), indent=2, default=str))
        else:
            print(f"Webhook '{args.info}' not found.")
        mgr.close()
        return

    if args.remove:
        removed = mgr.unregister(args.remove)
        print(f"Removed '{args.remove}'." if removed else f"'{args.remove}' not found.")
        mgr.close()
        return

    if args.add:
        hook_id, url, template, secret, session = args.add
        config = WebhookConfig(
            hook_id=hook_id,
            url=url,
            prompt_template=template,
            hmac_secret=secret,
            target_session=session,
        )
        mgr.register(config)
        print(f"Registered webhook '{hook_id}'")
        mgr.close()
        return

    parser.print_help()
    mgr.close()
