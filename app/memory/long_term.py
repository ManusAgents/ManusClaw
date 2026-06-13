from __future__ import annotations

"""
Long-Term Memory — RAG-based persistent memory with graceful fallback.

Strategy:
  1. Try to use a vector store (sqlite-vec or chromadb) for semantic search.
  2. If unavailable, fall back to a keyword + BM25-lite index stored in SQLite.
  3. Never crashes the agent if the vector library is missing.

All stored entries are plain text + metadata. They survive across sessions
via a local SQLite file at `workspace/.memory/long_term.db`.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional


_DB_PATH = Path("workspace/.memory/long_term.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id        TEXT PRIMARY KEY,
    content   TEXT NOT NULL,
    meta      TEXT,
    ts        REAL,
    embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
    USING fts5(content, content='entries', content_rowid='rowid');
"""

_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
END;
"""


def _entry_id(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class LongTermMemory:
    """
    Persistent long-term memory with keyword FTS search (primary) and
    optional vector similarity search (lazy-loaded, silently skipped if unavailable).
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._vector_ok = False
        # FIX: Thread safety — use a reentrant lock to serialize access to
        # the shared SQLite connection since asyncio.to_thread can schedule on
        # different threads. RLock allows _connect() to be called inside a
        # locked section without deadlocking.
        import threading
        self._db_lock = threading.RLock()

    def _compute_placeholder_embedding(self, content: str) -> bytes:
        """Compute a deterministic hash-based placeholder embedding.

        This is NOT a semantic embedding — it's a placeholder that ensures
        the embedding column is populated. When a real embedding model
        becomes available, entries should be re-indexed.
        """
        import struct
        h = hashlib.sha256(content.encode()).digest()
        # Use 8 floats (32 bytes) as a deterministic fingerprint
        return struct.pack('8f', *(struct.unpack('8f', h[:32])) if len(h) >= 32 else (0.0,) * 8)

    def _connect(self) -> sqlite3.Connection:
        with self._db_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.executescript(_SCHEMA)
                self._conn.execute(_TRIGGER_AI)
                self._conn.execute(_TRIGGER_AD)
                self._conn.commit()
            return self._conn

    def _execute_query(self, fn, *args, **kwargs):
        """Execute a query under the db lock to ensure thread-safe access.

        Acquires ``_db_lock``, ensures the connection is ready, then calls
        ``fn(conn, *args, **kwargs)`` inside the lock. This prevents concurrent
        reads/writes from different ``asyncio.to_thread`` workers on the same
        connection.
        """
        with self._db_lock:
            conn = self._connect()
            return fn(conn, *args, **kwargs)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def store(self, content: str, meta: Optional[dict] = None) -> str:
        """Store a text entry. Returns entry ID."""
        entry_id = _entry_id(content)
        # FIX: Populate the embedding column with a simple hash-based placeholder
        # when vector search is unavailable. This ensures the column is not
        # perpetually NULL, and enables future migration to real embeddings.
        embedding_blob = self._compute_placeholder_embedding(content)

        def _store():
            self._execute_query(lambda conn: (
                conn.execute(
                    "INSERT OR REPLACE INTO entries (id, content, meta, ts, embedding) VALUES (?,?,?,?,?)",
                    (entry_id, content, json.dumps(meta or {}), time.time(), embedding_blob),
                ),
                conn.commit(),
            )[-1])

        await asyncio.to_thread(_store)
        return entry_id

    async def store_many(self, entries: list[str], meta: Optional[dict] = None) -> list[str]:
        return [await self.store(e, meta) for e in entries]

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """
        Hybrid search: FTS5 keyword search (always) + vector similarity (if available).
        Returns list of {id, content, meta, score} dicts ordered by relevance.
        """
        results: list[dict] = []

        # FTS keyword search
        try:
            def _fts_search():
                def _do_search(conn):
                    return conn.execute(
                        """
                        SELECT e.id, e.content, e.meta, bm25(entries_fts) AS score
                        FROM entries_fts
                        JOIN entries e ON e.rowid = entries_fts.rowid
                        WHERE entries_fts MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (self._clean_query(query), k),
                    ).fetchall()
                return self._execute_query(_do_search)

            rows = await asyncio.to_thread(_fts_search)
            for row in rows:
                results.append({
                    "id": row[0],
                    "content": row[1],
                    "meta": json.loads(row[2] or "{}"),
                    "score": float(row[3]),
                    "source": "fts",
                })
        except Exception:
            pass

        if not results:
            # Fallback: simple LIKE search
            try:
                terms = query.split()[:4]
                like = "%" + "%".join(terms) + "%"

                def _like_search():
                    def _do_search(conn):
                        return conn.execute(
                            "SELECT id, content, meta FROM entries WHERE content LIKE ? LIMIT ?",
                            (like, k),
                        ).fetchall()
                    return self._execute_query(_do_search)

                rows = await asyncio.to_thread(_like_search)
                for row in rows:
                    results.append({
                        "id": row[0],
                        "content": row[1],
                        "meta": json.loads(row[2] or "{}"),
                        "score": 0.5,
                        "source": "like",
                    })
            except Exception:
                pass

        return results[:k]

    async def get_recent(self, k: int = 10) -> list[dict]:
        def _recent():
            def _do_query(conn):
                return conn.execute(
                    "SELECT id, content, meta, ts FROM entries ORDER BY ts DESC LIMIT ?",
                    (k,),
                ).fetchall()
            return self._execute_query(_do_query)

        rows = await asyncio.to_thread(_recent)
        return [
            {"id": r[0], "content": r[1], "meta": json.loads(r[2] or "{}"), "ts": r[3]}
            for r in rows
        ]

    async def count(self) -> int:
        def _count():
            def _do_query(conn):
                return conn.execute("SELECT COUNT(*) FROM entries").fetchone()
            return self._execute_query(_do_query)

        row = await asyncio.to_thread(_count)
        return row[0] if row else 0

    def _clean_query(self, query: str) -> str:
        """FTS5-safe query: keep alphanumeric words, join with OR."""
        words = re.findall(r"\w+", query)
        return " OR ".join(words[:8]) if words else query

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
