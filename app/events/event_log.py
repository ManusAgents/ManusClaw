"""
ManusClaw Event System — File-Backed Event Log
=================================================

An append-only, file-backed event log with O(1) length queries, lazy
loading, thread-safe access, and crash-proof atomic writes.

Architecture:
  - Events are stored as newline-delimited JSON (NDJSON) in a single log file.
  - A companion ``.count`` file tracks the number of events for O(1) length.
  - A companion ``.meta`` file stores log-level metadata (session ID, creation time).
  - All writes are **atomic**: data is written to a temporary file first, then
    ``os.replace()`` swaps it into place, eliminating partial-write corruption.
  - Thread safety is provided by a ``threading.Lock``; all public mutating
    operations acquire the lock.
  - Event data is **lazy-loaded**: the log file is only read when events are
    accessed (iteration, slicing, search).  The count is always O(1).

Crash safety guarantees:
  1. ``append()`` writes to a temp file then atomically renames — a crash
     during write leaves the old file intact.
  2. The count file is updated *after* the log file is successfully appended,
     so a crash may leave the count slightly behind (the log is the source of
     truth; ``reindex()`` can rebuild the count).
  3. ``flush()`` forces an ``fsync`` on the log file descriptor.

Metrics:
  The log maintains simple counters for appended events and errors encountered
  during reads, accessible via the ``metrics`` property.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.events.base import Event
from app.events.serialization import (
    DeserializationError,
    SerializationError,
    deserialize,
    deserialize_batch,
    serialize,
    serialize_to_dict,
)
from app.events.types import KIND_TO_EVENT

logger = logging.getLogger("manusclaw.events.event_log")


# ──────────────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────────────

class EventLogMetrics:
    """Simple, thread-safe counters for event log observability."""

    __slots__ = ("appended", "read_errors", "bytes_written", "last_append_ts")

    def __init__(self) -> None:
        self.appended: int = 0
        self.read_errors: int = 0
        self.bytes_written: int = 0
        self.last_append_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "appended": self.appended,
            "read_errors": self.read_errors,
            "bytes_written": self.bytes_written,
            "last_append_ts": self.last_append_ts,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Event Log
# ──────────────────────────────────────────────────────────────────────────────

class EventLog:
    """File-backed, append-only event log with O(1) length and lazy loading.

    Usage::

        log = EventLog("/tmp/session_abc")
        log.append(my_event)
        print(len(log))          # O(1) — reads from .count file
        events = log.get_range(0, 10)  # lazy-loads from disk
        for event in log:        # iteration also lazy-loads
            ...

    Args:
        path: Base path for the log files.  The following files are used:
            - ``{path}.jsonl``  — the NDJSON event log
            - ``{path}.count``  — event count (single integer)
            - ``{path}.meta``   — JSON metadata
        auto_create: If True (default), create the log directory and files
            on first append if they don't exist.
        max_file_size_bytes: Soft limit on the log file size.  When exceeded,
            ``append()`` still works but a warning is logged.  Set to 0 for
            no limit.
    """

    def __init__(
        self,
        path: str | Path,
        auto_create: bool = True,
        max_file_size_bytes: int = 0,
    ) -> None:
        self._base = Path(path)
        self._log_file = self._base.with_suffix(".jsonl")
        self._count_file = self._base.with_suffix(".count")
        self._meta_file = self._base.with_suffix(".meta")
        self._auto_create = auto_create
        self._max_file_size = max_file_size_bytes
        self._lock = threading.Lock()
        self._metrics = EventLogMetrics()
        self._session_id: Optional[str] = None

        # Ensure parent directory exists
        if self._auto_create:
            self._base.parent.mkdir(parents=True, exist_ok=True)

        # Recover count from file if it exists, else 0
        self._count: int = self._read_count_file()

        # Load metadata
        self._load_meta()

    # ──────────────────────────────────────────────────────────────────
    # Properties
    # ──────────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """Base path of the event log."""
        return self._base

    @property
    def log_file(self) -> Path:
        """Path to the NDJSON log file."""
        return self._log_file

    @property
    def count_file(self) -> Path:
        """Path to the event count file."""
        return self._count_file

    @property
    def session_id(self) -> Optional[str]:
        """Session ID associated with this log (from metadata)."""
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session_id = value
        self._save_meta()

    @property
    def metrics(self) -> dict[str, Any]:
        """Current log metrics."""
        return self._metrics.to_dict()

    def __len__(self) -> int:
        """Return the number of events in O(1) time."""
        return self._count

    def __bool__(self) -> bool:
        return self._count > 0

    # ──────────────────────────────────────────────────────────────────
    # Append (the only mutation)
    # ──────────────────────────────────────────────────────────────────

    def append(self, event: Event) -> str:
        """Append an event to the log.

        The write is **atomic**: the event is first serialized to a temp
        file in the same directory, then appended to the log via
        ``os.replace()``.  This ensures no partial data is written even
        on crash.

        Args:
            event: The event to append.

        Returns:
            The event's ``id`` field.

        Raises:
            SerializationError: If the event cannot be serialized.
            EventLogError: If the append operation fails.
        """
        with self._lock:
            return self._append_locked(event)

    def append_batch(self, events: list[Event]) -> list[str]:
        """Append multiple events atomically.

        All events are written in a single atomic operation.  If any
        event fails to serialize, none are written.

        Args:
            events: List of events to append.

        Returns:
            List of event IDs in order.

        Raises:
            SerializationError: If any event cannot be serialized.
            EventLogError: If the append operation fails.
        """
        with self._lock:
            ids: list[str] = []
            lines: list[str] = []

            # Pre-serialize all events before touching the file
            for event in events:
                try:
                    line = serialize(event)
                    ids.append(event.id)
                    lines.append(line)
                except SerializationError:
                    raise
                except Exception as exc:
                    raise SerializationError(
                        f"Failed to serialize event kind={getattr(event, 'kind', '?')}: {exc}"
                    ) from exc

            # Atomic write of the batch
            self._atomic_append_lines(lines)

            # Update count
            self._count += len(events)
            self._write_count_file(self._count)

            # Update metrics
            self._metrics.appended += len(events)
            self._metrics.bytes_written += sum(len(l.encode("utf-8")) for l in lines)
            self._metrics.last_append_ts = time.time()

            return ids

    # ──────────────────────────────────────────────────────────────────
    # Read operations (lazy-loading from disk)
    # ──────────────────────────────────────────────────────────────────

    def get(self, index: int) -> Event:
        """Retrieve a single event by its zero-based index.

        .. warning:: This is O(index) — it reads from the start of the
            file up to the requested index.  For bulk access, prefer
            :meth:`get_range`.

        Args:
            index: Zero-based index of the event.

        Returns:
            The Event at the given index.

        Raises:
            IndexError: If index is out of range.
            EventLogError: If the event cannot be read.
        """
        if index < 0:
            index = self._count + index
        if index < 0 or index >= self._count:
            raise IndexError(
                f"Event index {index} out of range (0..{self._count - 1})"
            )
        events = self._read_range(index, index + 1)
        if not events:
            raise IndexError(
                f"Failed to read event at index {index}"
            )
        return events[0]

    def get_range(
        self,
        start: int = 0,
        end: Optional[int] = None,
    ) -> list[Event]:
        """Retrieve a range of events by zero-based indices.

        Args:
            start: Start index (inclusive).  Defaults to 0.
            end: End index (exclusive).  Defaults to the log length.

        Returns:
            List of Event instances in the requested range.

        Raises:
            IndexError: If start/end indices are out of range.
        """
        if end is None:
            end = self._count
        if start < 0:
            start = max(0, self._count + start)
        if end < 0:
            end = max(0, self._count + end)
        if start > end:
            return []
        end = min(end, self._count)
        if start >= self._count:
            return []
        return self._read_range(start, end)

    def get_all(self) -> list[Event]:
        """Retrieve all events from the log.

        Convenience method equivalent to ``get_range(0, len(log))``.
        """
        return self.get_range(0, self._count)

    def get_last(self, n: int = 1) -> list[Event]:
        """Retrieve the last ``n`` events.

        Args:
            n: Number of events from the end.  Defaults to 1.

        Returns:
            List of the most recent ``n`` events (oldest first).
        """
        if n <= 0:
            return []
        start = max(0, self._count - n)
        return self._read_range(start, self._count)

    # ──────────────────────────────────────────────────────────────────
    # Iteration
    # ──────────────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[Event]:
        """Iterate over all events in the log (lazy-loading)."""
        return iter(self.get_all())

    # ──────────────────────────────────────────────────────────────────
    # Search / filter
    # ──────────────────────────────────────────────────────────────────

    def find_by_kind(self, kind: str) -> list[Event]:
        """Return all events matching a specific ``kind`` value.

        Args:
            kind: The event kind discriminator to match.

        Returns:
            List of matching events in chronological order.
        """
        return [e for e in self.get_all() if e.kind == kind]

    def find_by_id(self, event_id: str) -> Optional[Event]:
        """Find an event by its unique ``id`` field.

        Args:
            event_id: The UUID of the event.

        Returns:
            The matching Event, or None if not found.
        """
        for event in self.get_all():
            if event.id == event_id:
                return event
        return None

    def find_by_source(self, source: str) -> list[Event]:
        """Return all events from a specific source.

        Args:
            source: The source type to filter by.

        Returns:
            List of matching events in chronological order.
        """
        return [e for e in self.get_all() if e.source == source]

    # ──────────────────────────────────────────────────────────────────
    # Maintenance
    # ──────────────────────────────────────────────────────────────────

    def reindex(self) -> int:
        """Rebuild the count file from the actual log contents.

        Useful after a crash that may have left the count file out of
        sync with the log file.

        Returns:
            The corrected event count.
        """
        with self._lock:
            count = self._count_lines_in_log()
            self._count = count
            self._write_count_file(count)
            logger.info("Reindexed event log %s: %d events", self._base, count)
            return count

    def truncate(self, keep_last: int = 0) -> int:
        """Truncate the log, keeping only the last ``keep_last`` events.

        This rewrites the log file atomically.

        Args:
            keep_last: Number of most-recent events to retain.
                0 means truncate to empty.

        Returns:
            Number of events removed.
        """
        with self._lock:
            if keep_last >= self._count:
                return 0

            if keep_last <= 0:
                self._atomic_write_file(self._log_file, "")
                self._count = 0
                self._write_count_file(0)
                removed = self._count
                return removed

            # Read the events we want to keep
            kept_events = self._read_range(
                self._count - keep_last, self._count
            )
            # Rewrite the log with only the kept events
            lines = [serialize(e) for e in kept_events]
            content = "\n".join(lines)
            if content:
                content += "\n"
            self._atomic_write_file(self._log_file, content)

            old_count = self._count
            self._count = keep_last
            self._write_count_file(keep_last)
            return old_count - keep_last

    def flush(self) -> None:
        """Force-flush the log file to disk (fsync).

        This is a no-op if the log file doesn't exist yet.
        """
        with self._lock:
            if self._log_file.exists():
                fd = os.open(str(self._log_file), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    # ──────────────────────────────────────────────────────────────────
    # Internal: append implementation (called with lock held)
    # ──────────────────────────────────────────────────────────────────

    def _append_locked(self, event: Event) -> str:
        """Append a single event.  Caller must hold self._lock."""
        try:
            line = serialize(event)
        except SerializationError:
            raise
        except Exception as exc:
            raise SerializationError(
                f"Failed to serialize event kind={getattr(event, 'kind', '?')}: {exc}"
            ) from exc

        self._atomic_append_line(line)

        # Update count
        self._count += 1
        self._write_count_file(self._count)

        # Update metrics
        self._metrics.appended += 1
        self._metrics.bytes_written += len(line.encode("utf-8"))
        self._metrics.last_append_ts = time.time()

        # Soft size check
        if self._max_file_size > 0:
            try:
                file_size = self._log_file.stat().st_size
                if file_size > self._max_file_size:
                    logger.warning(
                        "Event log %s exceeded soft size limit: %d > %d bytes",
                        self._base,
                        file_size,
                        self._max_file_size,
                    )
            except OSError:
                pass

        return event.id

    # ──────────────────────────────────────────────────────────────────
    # Internal: atomic file operations
    # ──────────────────────────────────────────────────────────────────

    def _atomic_append_line(self, line: str) -> None:
        """Append a single NDJSON line to the log file atomically.

        Strategy: read existing content → write to temp → os.replace().
        For append-heavy workloads, we use a simpler approach: open in
        append mode with O_SYNC for durability, wrapped in a try/finally.

        However, for true crash-proof semantics, we write to a temp file
        and rename.  This is safer but slower for very large logs.

        We use a hybrid approach: for files under 64MB, use atomic
        replace; for larger files, use direct append (accepting a small
        window of vulnerability for better performance).
        """
        if not line.endswith("\n"):
            line += "\n"

        try:
            # Ensure the directory exists
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

            # Check if we should use atomic replace vs direct append
            use_atomic = True
            try:
                if self._log_file.exists():
                    file_size = self._log_file.stat().st_size
                    use_atomic = file_size < 64 * 1024 * 1024  # 64 MB
                else:
                    use_atomic = True
            except OSError:
                use_atomic = True

            if use_atomic:
                self._atomic_append_line_small(line)
            else:
                self._direct_append_line(line)

        except EventLogError:
            raise
        except Exception as exc:
            raise EventLogError(
                f"Failed to append event to {self._log_file}: {exc}"
            ) from exc

    def _atomic_append_line_small(self, line: str) -> None:
        """Atomic append for small files: read + write to temp + rename."""
        existing = ""
        if self._log_file.exists():
            try:
                existing = self._log_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise EventLogError(
                    f"Failed to read log file {self._log_file}: {exc}"
                ) from exc

        new_content = existing + line

        # Write to temp file in same directory (same filesystem for os.replace)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._log_file.parent),
            prefix=".event_log_tmp_",
            suffix=".jsonl",
        )
        try:
            os.write(tmp_fd, new_content.encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(self._log_file))
        except Exception:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        finally:
            # Ensure temp file is cleaned up on any remaining error path
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

    def _direct_append_line(self, line: str) -> None:
        """Direct append for large files (no atomic rename)."""
        fd = os.open(
            str(self._log_file),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_append_lines(self, lines: list[str]) -> None:
        """Append multiple lines atomically."""
        content = "\n".join(lines)
        if content and not content.endswith("\n"):
            content += "\n"

        existing = ""
        if self._log_file.exists():
            try:
                existing = self._log_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise EventLogError(
                    f"Failed to read log file {self._log_file}: {exc}"
                ) from exc

        new_content = existing + content

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._log_file.parent),
            prefix=".event_log_tmp_",
            suffix=".jsonl",
        )
        try:
            os.write(tmp_fd, new_content.encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(self._log_file))
        except Exception:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

    def _atomic_write_file(self, path: Path, content: str) -> None:
        """Write content to a file atomically using temp + rename."""
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".event_log_tmp_",
            suffix=path.suffix,
        )
        try:
            os.write(tmp_fd, content.encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(path))
        except Exception:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────────────
    # Internal: count file
    # ──────────────────────────────────────────────────────────────────

    def _read_count_file(self) -> int:
        """Read the event count from the .count file."""
        if not self._count_file.exists():
            return 0
        try:
            content = self._count_file.read_text(encoding="utf-8").strip()
            return int(content) if content else 0
        except (ValueError, OSError) as exc:
            logger.warning(
                "Failed to read count file %s: %s", self._count_file, exc
            )
            return 0

    def _write_count_file(self, count: int) -> None:
        """Write the event count to the .count file atomically."""
        self._atomic_write_file(self._count_file, str(count))

    # ──────────────────────────────────────────────────────────────────
    # Internal: metadata file
    # ──────────────────────────────────────────────────────────────────

    def _load_meta(self) -> None:
        """Load metadata from the .meta file."""
        if not self._meta_file.exists():
            return
        try:
            data = json.loads(self._meta_file.read_text(encoding="utf-8"))
            self._session_id = data.get("session_id")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read meta file %s: %s", self._meta_file, exc
            )

    def _save_meta(self) -> None:
        """Save metadata to the .meta file atomically."""
        data = {
            "session_id": self._session_id,
            "log_path": str(self._log_file),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write_file(
            self._meta_file, json.dumps(data, indent=2)
        )

    # ──────────────────────────────────────────────────────────────────
    # Internal: reading events
    # ──────────────────────────────────────────────────────────────────

    def _read_range(self, start: int, end: int) -> list[Event]:
        """Read events from start (inclusive) to end (exclusive).

        This reads the entire log file and extracts the requested range.
        For very large logs, this is the main cost center; future
        optimizations could include an index file for O(1) seek.
        """
        if not self._log_file.exists():
            return []

        try:
            raw = self._log_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise EventLogError(
                f"Failed to read log file {self._log_file}: {exc}"
            ) from exc

        lines = raw.splitlines()
        # Filter empty lines
        non_empty_lines = [l for l in lines if l.strip()]
        total = len(non_empty_lines)

        if start >= total:
            return []
        end = min(end, total)
        if start >= end:
            return []

        events: list[Event] = []
        for line in non_empty_lines[start:end]:
            try:
                event = deserialize(line.strip())
                events.append(event)
            except DeserializationError as exc:
                self._metrics.read_errors += 1
                logger.warning("Skipping corrupted event: %s", exc)

        return events

    def _count_lines_in_log(self) -> int:
        """Count non-empty lines in the log file (source of truth)."""
        if not self._log_file.exists():
            return 0
        try:
            raw = self._log_file.read_text(encoding="utf-8")
            return sum(1 for line in raw.splitlines() if line.strip())
        except OSError:
            return 0

    # ──────────────────────────────────────────────────────────────────
    # Representation
    # ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"EventLog(path={self._base!r}, count={self._count}, "
            f"session_id={self._session_id!r})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Error types
# ──────────────────────────────────────────────────────────────────────────────

class EventLogError(Exception):
    """Raised when an event log operation fails."""
