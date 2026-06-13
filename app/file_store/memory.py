"""
File Storage Backends — In-Memory (Testing)
==============================================

:class:`InMemoryFileStore` implements the :class:`FileStore` interface
using in-memory dictionaries.  This is intended **only** for testing and
development — data is lost when the process exits.

Features:
    * Zero-dependency (no filesystem, no cloud SDK)
    * Thread-safe operations
    * Full metadata tracking
    * Supports all FileStore operations
    * Content-type and user metadata preservation

Usage::

    from app.file_store.memory import InMemoryFileStore

    store = InMemoryFileStore()
    await store.write("test.txt", b"hello world", content_type="text/plain")
    data = await store.read("test.txt")  # b"hello world"
    await store.delete("test.txt")
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional, Union

from app.file_store.base import (
    FileAlreadyExistsError,
    FileMetadata,
    FileNotFoundError,
    FileStore,
    FileStoreError,
)


# ──────────────────────────────────────────────────────────────────────────────
# In-memory file record
# ──────────────────────────────────────────────────────────────────────────────

class _MemoryFile:
    """Internal representation of a stored file."""

    __slots__ = ("data", "content_type", "metadata", "created_at", "modified_at", "etag")

    def __init__(
        self,
        data: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self.data = data
        self.content_type = content_type
        self.metadata = metadata
        self.created_at = datetime.now(timezone.utc)
        self.modified_at = self.created_at
        self.etag = f'"{hash(data) & 0xFFFFFFFF:08x}"'


# ──────────────────────────────────────────────────────────────────────────────
# InMemoryFileStore
# ──────────────────────────────────────────────────────────────────────────────

class InMemoryFileStore(FileStore):
    """
    In-memory file store for testing and development.

    Files are stored in a thread-safe dictionary.  All data is lost
    when the process exits.

    Args:
        max_file_size: Maximum file size in bytes (default: 100 MB).
                       Set to ``0`` for unlimited.
    """

    def __init__(self, max_file_size: int = 100 * 1024 * 1024) -> None:
        self._files: dict[str, _MemoryFile] = {}
        self._lock = threading.Lock()
        self._max_file_size = max_file_size

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "memory"

    # ── Public API ───────────────────────────────────────────────────────────

    async def write(
        self,
        path: str,
        data: Union[bytes, str],
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        metadata: Optional[dict[str, str]] = None,
    ) -> FileMetadata:
        self._validate_path(path)

        if isinstance(data, str):
            data = data.encode("utf-8")

        # Check size limit
        if self._max_file_size > 0 and len(data) > self._max_file_size:
            raise FileStoreError(
                f"File size ({len(data)} bytes) exceeds maximum "
                f"({self._max_file_size} bytes)",
                path=path,
            )

        with self._lock:
            if path in self._files and not overwrite:
                raise FileAlreadyExistsError(path)

            # Preserve created_at if overwriting
            existing = self._files.get(path)
            file_record = _MemoryFile(
                data=data,
                content_type=content_type,
                metadata=metadata or {},
            )
            if existing is not None:
                file_record.created_at = existing.created_at

            self._files[path] = file_record

        return self._build_metadata(path, file_record)

    async def read(self, path: str) -> bytes:
        self._validate_path(path)

        with self._lock:
            file_record = self._files.get(path)
            if file_record is None:
                raise FileNotFoundError(path)
            return file_record.data

    async def delete(self, path: str) -> bool:
        self._validate_path(path)

        with self._lock:
            if path not in self._files:
                return False
            del self._files[path]
            return True

    async def exists(self, path: str) -> bool:
        self._validate_path(path)
        with self._lock:
            return path in self._files

    async def list(self, prefix: str = "") -> list[FileMetadata]:
        with self._lock:
            results: list[FileMetadata] = []
            for path, file_record in self._files.items():
                if prefix and not path.startswith(prefix):
                    continue
                results.append(self._build_metadata(path, file_record))

        return sorted(results, key=lambda m: m.path)

    async def get_url(self, path: str, expires_in: int = 3600) -> str:
        self._validate_path(path)

        with self._lock:
            if path not in self._files:
                raise FileNotFoundError(path)

        # In-memory store doesn't have real URLs; return a mock URL
        return f"memory://{path}"

    async def get_metadata(self, path: str) -> FileMetadata:
        self._validate_path(path)

        with self._lock:
            file_record = self._files.get(path)
            if file_record is None:
                raise FileNotFoundError(path)
            return self._build_metadata(path, file_record)

    # ── Testing helpers ──────────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all stored files.  Useful for test teardown."""
        with self._lock:
            self._files.clear()

    @property
    def file_count(self) -> int:
        """Return the number of stored files."""
        with self._lock:
            return len(self._files)

    @property
    def total_size(self) -> int:
        """Return the total size of all stored files in bytes."""
        with self._lock:
            return sum(len(f.data) for f in self._files.values())

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _build_metadata(path: str, file_record: _MemoryFile) -> FileMetadata:
        """Build FileMetadata from an in-memory file record."""
        return FileMetadata(
            path=path,
            size_bytes=len(file_record.data),
            content_type=file_record.content_type,
            created_at=file_record.created_at,
            modified_at=file_record.modified_at,
            etag=file_record.etag,
            extra=file_record.metadata,
        )

    @staticmethod
    def _validate_path(path: str) -> None:
        """Validate a path for safety."""
        if not path:
            raise FileStoreError("Path must not be empty")
