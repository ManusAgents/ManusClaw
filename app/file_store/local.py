"""
File Storage Backends — Local Filesystem
==========================================

:class:`LocalFileStore` implements the :class:`FileStore` interface using
the local filesystem.  Files are stored under a configurable base directory
with proper permissions and atomic writes.

Features:
    * Atomic writes (temp file + rename)
    * Restrictive file permissions (``0600`` on POSIX)
    * Recursive directory creation
    * Content-type preservation via extended attributes or sidecar files
    * Thread-safe operations via ``threading.Lock``
    * Metadata from ``os.stat`` (size, timestamps)

Usage::

    from app.file_store.local import LocalFileStore

    store = LocalFileStore(base_dir="/var/lib/manusclaw/files")
    await store.write("reports/q1.pdf", pdf_bytes, content_type="application/pdf")
    data = await store.read("reports/q1.pdf")
    url = await store.get_url("reports/q1.pdf")
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from app.file_store.base import (
    FileAlreadyExistsError,
    FileMetadata,
    FileNotFoundError,
    FileStore,
    FileStoreError,
    FileStorePermissionError,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Sidecar metadata file
# ──────────────────────────────────────────────────────────────────────────────

_SIDECAR_SUFFIX = ".meta.json"


def _write_sidecar(file_path: Path, metadata: dict[str, Any]) -> None:
    """Write a sidecar metadata file next to the data file."""
    meta_path = file_path.parent / f"{file_path.name}{_SIDECAR_SUFFIX}"
    try:
        meta_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: don't fail the write if sidecar can't be created
        pass


def _read_sidecar(file_path: Path) -> dict[str, Any]:
    """Read a sidecar metadata file, returning an empty dict on failure."""
    meta_path = file_path.parent / f"{file_path.name}{_SIDECAR_SUFFIX}"
    try:
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# LocalFileStore
# ──────────────────────────────────────────────────────────────────────────────

class LocalFileStore(FileStore):
    """
    Filesystem-backed file store.

    Files are stored under ``base_dir`` with their full path preserved.
    Metadata (content type, user metadata) is stored in sidecar
    ``.meta.json`` files.

    Args:
        base_dir: Root directory for file storage.  Created if it doesn't exist.

    Raises:
        FileStoreError: If the base directory cannot be created.
    """

    def __init__(self, base_dir: str = "") -> None:
        if not base_dir:
            base_dir = os.path.join(
                os.getenv("MANUSCLAW_HOME", str(Path.home() / ".manusclaw")),
                "file_store",
            )
        self._base_dir = Path(base_dir).resolve()
        self._lock = threading.Lock()

        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._set_secure_permissions(self._base_dir, directory=True)
        except OSError as exc:
            raise FileStoreError(
                f"Cannot create file store directory '{self._base_dir}': {exc}"
            )

        logger.debug(f"[LocalFileStore] Initialized at {self._base_dir}")

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        """Return the base directory path."""
        return self._base_dir

    @property
    def backend_name(self) -> str:
        return "local"

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
        file_path = self._resolve(path)

        with self._lock:
            if file_path.exists() and not overwrite:
                raise FileAlreadyExistsError(path)

            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert string data to bytes
            if isinstance(data, str):
                data = data.encode("utf-8")

            # Write atomically via temp file
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(file_path.parent),
                    prefix=".tmp_file_",
                )
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    self._set_secure_permissions(Path(tmp_path))
                    os.replace(tmp_path, str(file_path))
                except Exception:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

                self._set_secure_permissions(file_path)
            except OSError as exc:
                raise FileStoreError(
                    f"Failed to write file '{path}': {exc}", path=path
                )

            # Write sidecar metadata
            sidecar_data: dict[str, Any] = {
                "content_type": content_type,
                "user_metadata": metadata or {},
            }
            _write_sidecar(file_path, sidecar_data)

        # Return metadata
        return self._build_metadata(file_path, path)

    async def read(self, path: str) -> bytes:
        self._validate_path(path)
        file_path = self._resolve(path)

        with self._lock:
            if not file_path.exists():
                raise FileNotFoundError(path)
            if file_path.is_dir():
                raise FileStoreError(f"Path '{path}' is a directory, not a file", path=path)
            try:
                return file_path.read_bytes()
            except OSError as exc:
                raise FileStoreError(f"Failed to read file '{path}': {exc}", path=path)

    async def delete(self, path: str) -> bool:
        self._validate_path(path)
        file_path = self._resolve(path)
        meta_path = file_path.parent / f"{file_path.name}{_SIDECAR_SUFFIX}"

        with self._lock:
            if not file_path.exists():
                return False

            try:
                if file_path.is_dir():
                    shutil.rmtree(file_path)
                else:
                    file_path.unlink()
                # Clean up sidecar
                if meta_path.exists():
                    meta_path.unlink()
                return True
            except OSError as exc:
                raise FileStoreError(
                    f"Failed to delete file '{path}': {exc}", path=path
                )

    async def exists(self, path: str) -> bool:
        self._validate_path(path)
        with self._lock:
            return self._resolve(path).exists()

    async def list(self, prefix: str = "") -> list[FileMetadata]:
        self._validate_path(prefix or ".")
        search_dir = self._resolve(prefix) if prefix else self._base_dir

        results: list[FileMetadata] = []

        with self._lock:
            if not search_dir.exists():
                return results

            try:
                for file_path in search_dir.rglob("*"):
                    if file_path.is_file() and not file_path.name.endswith(_SIDECAR_SUFFIX):
                        # Skip sidecar files
                        if file_path.name.startswith(".tmp_file_"):
                            continue
                        rel_path = str(file_path.relative_to(self._base_dir))
                        results.append(self._build_metadata(file_path, rel_path))
            except OSError as exc:
                raise FileStoreError(
                    f"Failed to list files with prefix '{prefix}': {exc}",
                    path=prefix,
                )

        return sorted(results, key=lambda m: m.path)

    async def get_url(self, path: str, expires_in: int = 3600) -> str:
        self._validate_path(path)
        file_path = self._resolve(path)

        with self._lock:
            if not file_path.exists():
                raise FileNotFoundError(path)

        # Return a file:// URL for local files
        return file_path.as_uri()

    async def get_metadata(self, path: str) -> FileMetadata:
        self._validate_path(path)
        file_path = self._resolve(path)

        with self._lock:
            if not file_path.exists():
                raise FileNotFoundError(path)
            return self._build_metadata(file_path, path)

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        metadata: Optional[dict[str, str]] = None,
    ) -> FileMetadata:
        """
        Write data from an async stream to a local file.

        Supports true streaming — data is written incrementally without
        buffering the entire file in memory.
        """
        self._validate_path(path)
        file_path = self._resolve(path)

        with self._lock:
            if file_path.exists() and not overwrite:
                raise FileAlreadyExistsError(path)

            file_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(file_path.parent),
                    prefix=".tmp_stream_",
                )
                try:
                    with os.fdopen(fd, "wb") as f:
                        async for chunk in stream:
                            f.write(chunk)
                    os.replace(tmp_path, str(file_path))
                except Exception:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

                self._set_secure_permissions(file_path)
            except OSError as exc:
                raise FileStoreError(
                    f"Failed to stream-write file '{path}': {exc}", path=path
                )

            # Write sidecar metadata
            sidecar_data: dict[str, Any] = {
                "content_type": content_type,
                "user_metadata": metadata or {},
            }
            _write_sidecar(file_path, sidecar_data)

        return self._build_metadata(file_path, path)

    async def read_stream(
        self,
        path: str,
        chunk_size: int = 8192,
    ) -> AsyncIterator[bytes]:
        """
        Read a local file as an async stream of byte chunks.

        Uses aiofiles if available; otherwise falls back to synchronous
        chunked reads wrapped in async.
        """
        self._validate_path(path)
        file_path = self._resolve(path)

        with self._lock:
            if not file_path.exists():
                raise FileNotFoundError(path)

        # Use a separate read without holding the lock for the entire stream
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except OSError as exc:
            raise FileStoreError(
                f"Failed to stream-read file '{path}': {exc}", path=path
            )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        """Resolve a store path to a filesystem path."""
        resolved = (self._base_dir / path).resolve()
        # Security: ensure the resolved path is within base_dir
        try:
            resolved.relative_to(self._base_dir)
        except ValueError:
            raise FileStorePermissionError(
                path, detail="Path traversal detected"
            )
        return resolved

    def _build_metadata(self, file_path: Path, store_path: str) -> FileMetadata:
        """Build FileMetadata from a filesystem path."""
        try:
            stat_result = file_path.stat()
        except OSError:
            stat_result = None

        # Read sidecar for content type and user metadata
        sidecar = _read_sidecar(file_path)
        content_type = sidecar.get("content_type", "application/octet-stream")
        user_metadata = sidecar.get("user_metadata", {})

        created_at: Optional[datetime] = None
        modified_at: Optional[datetime] = None

        if stat_result is not None:
            created_at = datetime.fromtimestamp(
                stat_result.st_ctime, tz=timezone.utc
            )
            modified_at = datetime.fromtimestamp(
                stat_result.st_mtime, tz=timezone.utc
            )

        return FileMetadata(
            path=store_path,
            size_bytes=stat_result.st_size if stat_result else -1,
            content_type=content_type,
            created_at=created_at,
            modified_at=modified_at,
            extra=user_metadata,
        )

    @staticmethod
    def _validate_path(path: str) -> None:
        """Validate a path for safety."""
        if not path:
            raise FileStoreError("Path must not be empty")
        if ".." in Path(path).parts:
            raise FileStoreError(
                f"Path '{path}' contains '..' which is not allowed"
            )

    @staticmethod
    def _set_secure_permissions(path: Path, directory: bool = False) -> None:
        """Set restrictive permissions on POSIX systems."""
        if os.name != "posix":
            return
        try:
            if directory:
                path.chmod(stat.S_IRWXU)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
