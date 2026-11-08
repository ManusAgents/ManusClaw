from __future__ import annotations

"""
WebhookReliability — delivery idempotency and session affinity for webhooks.

Two production gaps closed here (both documented in IMPLEMENTATION_NOTES.md):

1. Idempotency — senders retry (GitHub, GitLab, Stripe, custom monitors).
   ``claim_delivery(hook_id, delivery_id)`` records a delivery ID exactly
   once; retries return False so the agent is NOT re-run for the same
   delivery. Rows are pruned after a retention window.
2. Session affinity — instead of constructing a fresh ``Manus()`` with a
   random session per trigger, every trigger of a hook reuses one stable
   session (``webhook-<hook_id>`` unless ``target_session`` is configured),
   so the agent keeps memory/context across deliveries of the same hook.

Storage shares the main SQLite database (same patterns as SessionDB:
WAL, re-entrant lock, jittered retry on lock). Per-hook asyncio locks
serialise concurrent deliveries so session state stays consistent.
"""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from app.db.session import _DB_PATH, _with_retry
from app.logger import logger

_RELIABILITY_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    hook_id      TEXT NOT NULL,
    delivery_id  TEXT NOT NULL,
    processed_at REAL NOT NULL,
    status       TEXT DEFAULT 'accepted',
    PRIMARY KEY (hook_id, delivery_id)
);

CREATE TABLE IF NOT EXISTS webhook_sessions (
    hook_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_RETENTION_S = 7 * 86400  # keep delivery IDs for 7 days
_PRUNE_INTERVAL_S = 3600  # prune at most once per hour


class WebhookReliability:
    """SQLite-backed delivery dedup, session affinity mapping, and locks."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.RLock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_prune = 0.0

    def _ensure(self) -> sqlite3.Connection:
        with self._db_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.executescript(_RELIABILITY_SCHEMA)
                self._conn.commit()
            return self._conn

    def _query(self, fn, *args, **kwargs):
        with self._db_lock:
            conn = self._ensure()
            return fn(conn, *args, **kwargs)

    # ─── Delivery idempotency ────────────────────────────────────────────

    async def claim_delivery(
        self, hook_id: str, delivery_id: str, status: str = "accepted"
    ) -> bool:
        """Record a delivery ID. True on first sight, False for duplicates."""
        if not delivery_id:
            return True

        def _claim():
            def _do(conn):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO webhook_deliveries"
                    " (hook_id, delivery_id, processed_at, status) VALUES (?,?,?,?)",
                    (hook_id, delivery_id, time.time(), status),
                )
                conn.commit()
                return cur.rowcount > 0
            return self._query(_do)

        is_new = await _with_retry(_claim)
        await self._prune_if_needed()
        return is_new

    async def delivery_seen(self, hook_id: str, delivery_id: str) -> bool:
        """True if this delivery ID was already processed (read-only check)."""
        def _check():
            def _do(conn):
                row = conn.execute(
                    "SELECT 1 FROM webhook_deliveries"
                    " WHERE hook_id = ? AND delivery_id = ?",
                    (hook_id, delivery_id),
                ).fetchone()
                return row is not None
            return self._query(_do)

        return await _with_retry(_check)

    async def _prune_if_needed(self) -> None:
        """Delete delivery records older than the retention window (throttled)."""
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        self._last_prune = now

        def _prune():
            def _do(conn):
                cur = conn.execute(
                    "DELETE FROM webhook_deliveries WHERE processed_at < ?",
                    (time.time() - _RETENTION_S,),
                )
                conn.commit()
                if cur.rowcount:
                    logger.debug(f"[WebhookReliability] Pruned {cur.rowcount} old delivery rows")
            return self._query(_do)

        await _with_retry(_prune)

    # ─── Session affinity ────────────────────────────────────────────────

    async def resolve_session_id(
        self, hook_id: str, target_session: str = ""
    ) -> str:
        """The persistent agent session for a hook.

        ``target_session`` (when configured) always wins and is persisted.
        Otherwise the stored mapping is reused, defaulting to the stable
        ``webhook-<hook_id>`` session.
        """
        if target_session:
            await self.remember_session(hook_id, target_session)
            return target_session

        async def _lookup():
            def _do():
                def _q(conn):
                    row = conn.execute(
                        "SELECT session_id FROM webhook_sessions WHERE hook_id = ?",
                        (hook_id,),
                    ).fetchone()
                    return row[0] if row else None
                return self._query(_q)
            return await _with_retry(_do)

        session_id = await _lookup()
        if session_id:
            return session_id

        session_id = f"webhook-{hook_id}"
        await self.remember_session(hook_id, session_id)
        return session_id

    async def remember_session(self, hook_id: str, session_id: str) -> None:
        """Persist (or update) the hook → session mapping."""
        def _remember():
            def _do(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO webhook_sessions (hook_id, session_id, updated_at)"
                    " VALUES (?,?,?)",
                    (hook_id, session_id, time.time()),
                )
                conn.commit()
            return self._query(_do)

        await _with_retry(_remember)

    # ─── Concurrency ─────────────────────────────────────────────────────

    def lock_for(self, hook_id: str) -> asyncio.Lock:
        """Serialise agent runs per hook (session state consistency)."""
        lock = self._locks.get(hook_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[hook_id] = lock
        return lock

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
