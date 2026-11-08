from __future__ import annotations

"""
UserDB — users, per-user API keys, and session ownership.

Storage lives in the same SQLite database as SessionDB
(``workspace/.sessions/manusclaw.db``) so ownership rows can be joined
with session data. Follows the SessionDB patterns: WAL mode, a re-entrant
lock around one shared connection, and jittered retries for
"database is locked" errors.

API keys are stored hashed (PBKDF2-HMAC-SHA256 with a per-key salt);
the plaintext is shown exactly once at creation. A short key prefix is
stored alongside the hash for indexed lookup and display.

Tables:
    users           — user accounts (username, role, disabled flag)
    api_keys        — hashed API keys owned by users
    session_owners  — session_id → owning user_id (first claim wins)
"""

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.db.session import _DB_PATH, _with_retry
from app.logger import logger

_USERS_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    name         TEXT DEFAULT '',
    key_prefix   TEXT NOT NULL,
    key_hash     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_used_at REAL DEFAULT 0,
    revoked_at   REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS session_owners (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_owners_user ON session_owners(user_id);
"""

_PBKDF2_ITERATIONS_DEFAULT = 600_000
_KEY_PREFIX = "mck_"


def _iterations() -> int:
    """PBKDF2 iteration count (env-overridable, floor 1000 for sanity)."""
    try:
        return max(1000, int(os.getenv("MANUSCLAW_PBKDF2_ITERATIONS", str(_PBKDF2_ITERATIONS_DEFAULT))))
    except ValueError:
        return _PBKDF2_ITERATIONS_DEFAULT


def generate_api_key() -> str:
    """Generate a new plaintext API key (shown once, stored only as a hash)."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    """Hash an API key with PBKDF2-HMAC-SHA256 and a random salt.

    Format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` — the
    iteration count travels with the hash so it can be raised later
    without invalidating existing keys.
    """
    iterations = _iterations()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw_key.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_key(raw_key: str, stored: str) -> bool:
    """Constant-time verification of a raw key against a stored hash."""
    try:
        scheme, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", raw_key.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class UserDB:
    """Async SQLite store for users, API keys, and session ownership."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.RLock()

    def _ensure(self) -> sqlite3.Connection:
        with self._db_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.executescript(_USERS_SCHEMA)
                self._conn.commit()
            return self._conn

    def _query(self, fn, *args, **kwargs):
        """Run ``fn(conn, ...)`` under the DB lock (mirrors SessionDB)."""
        with self._db_lock:
            conn = self._ensure()
            return fn(conn, *args, **kwargs)

    # ─── Users ───────────────────────────────────────────────────────────

    async def create_user(self, username: str, role: str = "user") -> dict[str, Any]:
        """Create a user. Raises ValueError on duplicate username or bad role."""
        username = (username or "").strip()
        if not username or len(username) > 64:
            raise ValueError("Username must be 1-64 characters.")
        if role not in ("user", "admin"):
            raise ValueError("Role must be 'user' or 'admin'.")

        def _create():
            def _do(conn):
                existing = conn.execute(
                    "SELECT user_id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if existing:
                    raise ValueError(f"User already exists: {username}")
                user_id = uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO users (user_id, username, role, created_at) VALUES (?,?,?,?)",
                    (user_id, username, role, time.time()),
                )
                conn.commit()
                return {"user_id": user_id, "username": username, "role": role}
            return self._query(_do)

        return await _with_retry(_create)

    async def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        def _get():
            def _do(conn):
                row = conn.execute(
                    "SELECT user_id, username, role, created_at, disabled"
                    " FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "user_id": row[0], "username": row[1], "role": row[2],
                    "created_at": row[3], "disabled": bool(row[4]),
                }
            return self._query(_do)

        return await _with_retry(_get)

    async def get_user_by_name(self, username: str) -> Optional[dict[str, Any]]:
        def _get():
            def _do(conn):
                row = conn.execute(
                    "SELECT user_id, username, role, created_at, disabled"
                    " FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "user_id": row[0], "username": row[1], "role": row[2],
                    "created_at": row[3], "disabled": bool(row[4]),
                }
            return self._query(_do)

        return await _with_retry(_get)

    async def list_users(self) -> list[dict[str, Any]]:
        def _list():
            def _do(conn):
                rows = conn.execute(
                    "SELECT u.user_id, u.username, u.role, u.created_at, u.disabled,"
                    " (SELECT COUNT(*) FROM api_keys k"
                    "   WHERE k.user_id = u.user_id AND k.revoked_at = 0)"
                    " FROM users u ORDER BY u.created_at"
                ).fetchall()
                return [
                    {
                        "user_id": r[0], "username": r[1], "role": r[2],
                        "created_at": r[3], "disabled": bool(r[4]),
                        "active_keys": r[5],
                    }
                    for r in rows
                ]
            return self._query(_do)

        return await _with_retry(_list)

    async def set_user_role(self, user_id: str, role: str) -> bool:
        if role not in ("user", "admin"):
            raise ValueError("Role must be 'user' or 'admin'.")

        def _set():
            def _do(conn):
                cur = conn.execute(
                    "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id)
                )
                conn.commit()
                return cur.rowcount > 0
            return self._query(_do)

        return await _with_retry(_set)

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        def _set():
            def _do(conn):
                cur = conn.execute(
                    "UPDATE users SET disabled = ? WHERE user_id = ?",
                    (1 if disabled else 0, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            return self._query(_do)

        return await _with_retry(_set)

    # ─── API keys ────────────────────────────────────────────────────────

    async def create_api_key(
        self,
        user_id: str,
        name: str = "",
        raw_key: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        """Create an API key for a user.

        Args:
            user_id: Owning user.
            name: Optional human label for the key.
            raw_key: Use this exact plaintext instead of generating one
                (bootstrap path). Still stored only as a hash.

        Returns:
            ``(key_row, plaintext)`` — the plaintext is returned exactly
            once and never stored.
        """
        plaintext = raw_key or generate_api_key()

        def _create():
            def _do(conn):
                user = conn.execute(
                    "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                if not user:
                    raise ValueError(f"User not found: {user_id}")
                key_id = uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO api_keys"
                    " (key_id, user_id, name, key_prefix, key_hash, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (key_id, user_id, name, plaintext[:16], hash_key(plaintext), time.time()),
                )
                conn.commit()
                return {"key_id": key_id, "name": name, "key_prefix": plaintext[:16]}
            return self._query(_do)

        row = await _with_retry(_create)
        return row, plaintext

    async def list_api_keys(
        self, user_id: str, include_revoked: bool = False
    ) -> list[dict[str, Any]]:
        def _list():
            def _do(conn):
                sql = (
                    "SELECT key_id, name, key_prefix, created_at, last_used_at, revoked_at"
                    " FROM api_keys WHERE user_id = ?"
                )
                params: list[Any] = [user_id]
                if not include_revoked:
                    sql += " AND revoked_at = 0"
                sql += " ORDER BY created_at"
                rows = conn.execute(sql, params).fetchall()
                return [
                    {
                        "key_id": r[0], "name": r[1], "key_prefix": r[2],
                        "created_at": r[3], "last_used_at": r[4],
                        "revoked": bool(r[5]),
                    }
                    for r in rows
                ]
            return self._query(_do)

        return await _with_retry(_list)

    async def revoke_api_key(self, key_id: str, user_id: Optional[str] = None) -> bool:
        """Revoke a key. When ``user_id`` is set, only that owner's keys match."""
        def _revoke():
            def _do(conn):
                if user_id is None:
                    cur = conn.execute(
                        "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at = 0",
                        (time.time(), key_id),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE api_keys SET revoked_at = ?"
                        " WHERE key_id = ? AND user_id = ? AND revoked_at = 0",
                        (time.time(), key_id, user_id),
                    )
                conn.commit()
                return cur.rowcount > 0
            return self._query(_do)

        return await _with_retry(_revoke)

    async def resolve_api_key(self, raw_key: str) -> Optional[dict[str, Any]]:
        """Resolve a plaintext API key to its owning user (None if invalid).

        Prefix-indexed lookup followed by constant-time hash verification.
        Disabled users' keys do not resolve.
        """
        if not raw_key:
            return None
        prefix = raw_key[:16]

        def _resolve():
            def _do(conn):
                rows = conn.execute(
                    "SELECT k.key_id, k.user_id, k.key_hash, u.username, u.role, u.disabled"
                    " FROM api_keys k JOIN users u ON u.user_id = k.user_id"
                    " WHERE k.key_prefix = ? AND k.revoked_at = 0",
                    (prefix,),
                ).fetchall()
                for row in rows:
                    if not verify_key(raw_key, row[2]):
                        continue
                    conn.execute(
                        "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                        (time.time(), row[0]),
                    )
                    conn.commit()
                    if row[5]:
                        return None  # user disabled — key not usable
                    return {
                        "key_id": row[0], "user_id": row[1],
                        "username": row[3], "role": row[4],
                    }
                return None
            return self._query(_do)

        return await _with_retry(_resolve)

    async def user_has_raw_key(self, user_id: str, raw_key: str) -> bool:
        """True if any of the user's active keys verifies against ``raw_key``."""
        def _check():
            def _do(conn):
                rows = conn.execute(
                    "SELECT key_hash FROM api_keys WHERE user_id = ? AND revoked_at = 0",
                    (user_id,),
                ).fetchall()
                return any(verify_key(raw_key, r[0]) for r in rows)
            return self._query(_do)

        return await _with_retry(_check)

    async def has_any_active_keys(self) -> bool:
        """True if at least one non-revoked API key exists (auth is enforceable)."""
        def _check():
            def _do(conn):
                row = conn.execute(
                    "SELECT 1 FROM api_keys WHERE revoked_at = 0 LIMIT 1"
                ).fetchone()
                return row is not None
            return self._query(_do)

        return await _with_retry(_check)

    # ─── Session ownership ───────────────────────────────────────────────

    async def set_session_owner(self, session_id: str, user_id: str) -> None:
        """Tag a session with its owner. First claim wins (INSERT OR IGNORE)."""
        def _set():
            def _do(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO session_owners (session_id, user_id, created_at)"
                    " VALUES (?,?,?)",
                    (session_id, user_id, time.time()),
                )
                conn.commit()
            return self._query(_do)

        await _with_retry(_set)

    async def get_session_owner(self, session_id: str) -> Optional[str]:
        def _get():
            def _do(conn):
                row = conn.execute(
                    "SELECT user_id FROM session_owners WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return row[0] if row else None
            return self._query(_do)

        return await _with_retry(_get)

    async def owned_session_ids(self, user_id: str, limit: int = 100) -> list[str]:
        """Session IDs owned by a user, newest first."""
        def _get():
            def _do(conn):
                rows = conn.execute(
                    "SELECT session_id FROM session_owners WHERE user_id = ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
                return [r[0] for r in rows]
            return self._query(_do)

        return await _with_retry(_get)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
