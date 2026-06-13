"""
File Storage Backends — Abstract Base Class
=============================================

Defines the :class:`FileStore` interface that all storage backends must
implement.  Every method is asynchronous to support I/O-bound operations
(especially cloud backends) and includes proper error handling contracts.

The ABC defines the following operations:

    * ``write(path, data)``     — Write data to a file
    * ``read(path)``            — Read data from a file
    * ``delete(path)``          — Delete a file
    * ``exists(path)``          — Check if a file exists
    * ``list(prefix)``          — List files with a given prefix
    * ``get_url(path)``         — Get a URL for accessing a file
    * ``get_metadata(path)``    — Get file metadata (size, timestamps, etc.)
    * ``write_stream(path, stream)``  — Write data from an async stream (large files)
    * ``read_stream(path)``     — Read data as an async stream (large files)

All backends must be thread-safe and handle retries for transient failures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Optional, Union


# ──────────────────────────────────────────────────────────────────────────────
# Metadata model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FileMetadata:
    """
    Immutable metadata about a stored file.

    Attributes:
        path:         The file's path/key within the store.
        size_bytes:   Size of the file in bytes (-1 if unknown).
        content_type: MIME type of the file (e.g. ``"application/octet-stream"``).
        created_at:   Timestamp when the file was created (if available).
        modified_at:  Timestamp when the file was last modified (if available).
        etag:         Entity tag for caching/validation (if available).
        extra:        Backend-specific metadata.
    """

    path: str
    size_bytes: int = -1
    content_type: str = "application/octet-stream"
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    etag: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class FileStoreError(Exception):
    """Base exception for file store errors."""

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(message)


class FileNotFoundError(FileStoreError):
    """Raised when a requested file does not exist."""

    def __init__(self, path: str) -> None:
        super().__init__(f"File not found: '{path}'", path=path)


class FileAlreadyExistsError(FileStoreError):
    """Raised when attempting to create a file that already exists."""

    def __init__(self, path: str) -> None:
        super().__init__(f"File already exists: '{path}'", path=path)


class FileStorePermissionError(FileStoreError):
    """Raised when access to a file is denied."""

    def __init__(self, path: str, detail: str = "") -> None:
        msg = f"Permission denied for file: '{path}'"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg, path=path)


class FileStoreConnectionError(FileStoreError):
    """Raised when the storage backend cannot be reached."""

    def __init__(self, path: str = "", detail: str = "") -> None:
        msg = "Storage backend connection error"
        if path:
            msg += f" for file: '{path}'"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg, path=path)


# ──────────────────────────────────────────────────────────────────────────────
# Abstract Base Class
# ──────────────────────────────────────────────────────────────────────────────

class FileStore(ABC):
    """
    Abstract base class for file storage backends.

    All methods are asynchronous to support cloud-based backends where
    I/O operations involve network calls.  Implementations must be
    thread-safe and should implement retry logic for transient failures.
    """

    @abstractmethod
    async def write(
        self,
        path: str,
        data: Union[bytes, str],
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        metadata: Optional[dict[str, str]] = None,
    ) -> FileMetadata:
        """
        Write data to a file.

        Args:
            path:         Destination path/key within the store.
            data:         The data to write (bytes or string).
            content_type: MIME type of the content.
            overwrite:    If ``True``, overwrite existing files.
                          If ``False``, raise :class:`FileAlreadyExistsError`.
            metadata:     Optional user-defined metadata key-value pairs.

        Returns:
            :class:`FileMetadata` for the written file.

        Raises:
            FileAlreadyExistsError: If the file exists and ``overwrite`` is ``False``.
            FileStoreError: If the write fails.
        """

    @abstractmethod
    async def read(self, path: str) -> bytes:
        """
        Read a file's content as bytes.

        Args:
            path: The path/key of the file to read.

        Returns:
            The file content as bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
            FileStoreError: If the read fails.
        """

    @abstractmethod
    async def delete(self, path: str) -> bool:
        """
        Delete a file.

        Args:
            path: The path/key of the file to delete.

        Returns:
            ``True`` if the file was deleted, ``False`` if it did not exist.

        Raises:
            FileStoreError: If the deletion fails.
        """

    @abstractmethod
    async def exists(self, path: str) -> bool:
        """
        Check if a file exists.

        Args:
            path: The path/key to check.

        Returns:
            ``True`` if the file exists, ``False`` otherwise.
        """

    @abstractmethod
    async def list(self, prefix: str = "") -> list[FileMetadata]:
        """
        List files with the given prefix.

        Args:
            prefix: Path prefix to filter by.  Empty string lists all files.

        Returns:
            A list of :class:`FileMetadata` for matching files.

        Raises:
            FileStoreError: If the listing fails.
        """

    @abstractmethod
    async def get_url(self, path: str, expires_in: int = 3600) -> str:
        """
        Get a URL for accessing a file.

        For cloud backends, this returns a presigned/signed URL.
        For local storage, this returns a file:// URL or HTTP URL.

        Args:
            path:       The path/key of the file.
            expires_in: URL expiration time in seconds (cloud backends only).

        Returns:
            A URL string for accessing the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            FileStoreError: If URL generation fails.
        """

    @abstractmethod
    async def get_metadata(self, path: str) -> FileMetadata:
        """
        Get metadata for a file.

        Args:
            path: The path/key of the file.

        Returns:
            :class:`FileMetadata` for the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            FileStoreError: If metadata retrieval fails.
        """

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        metadata: Optional[dict[str, str]] = None,
    ) -> FileMetadata:
        """
        Write data from an async stream to a file.

        This is the preferred method for large files.  The default
        implementation buffers the entire stream and calls :meth:`write`,
        but backends should override this for true streaming support.

        Args:
            path:         Destination path/key within the store.
            stream:       Async iterator yielding bytes chunks.
            content_type: MIME type of the content.
            overwrite:    If ``True``, overwrite existing files.
            metadata:     Optional user-defined metadata.

        Returns:
            :class:`FileMetadata` for the written file.
        """
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b"".join(chunks)
        return await self.write(path, data, content_type, overwrite, metadata)

    async def read_stream(
        self,
        path: str,
        chunk_size: int = 8192,
    ) -> AsyncIterator[bytes]:
        """
        Read a file as an async stream of byte chunks.

        This is the preferred method for large files.  The default
        implementation reads the entire file and yields it in chunks,
        but backends should override this for true streaming support.

        Args:
            path:       The path/key of the file to read.
            chunk_size: Size of each chunk in bytes.

        Yields:
            Bytes chunks of the file content.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        data = await self.read(path)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Return the name of this storage backend (e.g. 'local', 's3', 'gcs')."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} backend={self.backend_name}>"
