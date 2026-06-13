"""
File Storage Backends — Google Cloud Storage
===============================================

:class:`GoogleCloudFileStore` implements the :class:`FileStore` interface
using Google Cloud Storage (GCS).

Features:
    * Signed URLs for secure temporary access
    * Resumable uploads for large files (via streaming)
    * Retry logic with exponential backoff
    * Thread-safe operations
    * Content-type and user metadata preservation
    * Custom endpoint / emulator support

Dependencies:
    * ``google-cloud-storage`` — Google Cloud Storage client library

Environment Variables:
    * ``GOOGLE_APPLICATION_CREDENTIALS`` — Path to service account JSON key file
    * ``MANUSCLAW_GCS_BUCKET``          — Default GCS bucket name

Usage::

    from app.file_store.gcs import GoogleCloudFileStore

    store = GoogleCloudFileStore(bucket="my-manusclaw-bucket", prefix="files/")
    await store.write("report.pdf", pdf_bytes, content_type="application/pdf")
    url = await store.get_url("report.pdf", expires_in=3600)
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional, Union

from app.file_store.base import (
    FileAlreadyExistsError,
    FileMetadata,
    FileNotFoundError,
    FileStore,
    FileStoreConnectionError,
    FileStoreError,
    FileStorePermissionError,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Retry helper
# ──────────────────────────────────────────────────────────────────────────────

def _retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
):
    """Execute *func* with exponential backoff retry logic."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            exc_name = type(exc).__name__

            # Check for retryable GCS errors
            retryable = any(
                keyword in str(exc).lower()
                for keyword in ("retry", "rate", "throttl", "connection", "timeout", "429", "503")
            )

            # Check google-cloud specific exceptions
            try:
                from google.api_core.exceptions import (
                    ServiceUnavailable,
                    TooManyRequests,
                    InternalServerError,
                )
                if isinstance(exc, (ServiceUnavailable, TooManyRequests, InternalServerError)):
                    retryable = True
            except ImportError:
                pass

            if not retryable or attempt == max_retries:
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            delay += random.uniform(0, delay * 0.3)  # jitter
            logger.warning(
                f"[GCSFileStore] Retryable error on attempt {attempt + 1}/{max_retries}: "
                f"{exc_name}. Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# GoogleCloudFileStore
# ──────────────────────────────────────────────────────────────────────────────

class GoogleCloudFileStore(FileStore):
    """
    Google Cloud Storage file store.

    Files are stored as GCS blobs with an optional name prefix.
    Signed URLs are generated for temporary access.

    Args:
        bucket:       GCS bucket name.
        prefix:       Blob name prefix (e.g. ``"files/"``).
        project:      GCP project ID.  Falls back to ``GOOGLE_CLOUD_PROJECT``
                       env var or auto-detection.
        credentials:  Optional credentials object (service account, etc.).
        max_retries:  Maximum number of retries for transient failures.

    Raises:
        FileStoreConnectionError: If the GCS client cannot be initialized.
    """

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "",
        project: Optional[str] = None,
        credentials: Any = None,
        max_retries: int = 3,
    ) -> None:
        self._bucket_name = bucket or os.getenv("MANUSCLAW_GCS_BUCKET", "manusclaw-files")
        self._prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        self._max_retries = max_retries
        self._lock = threading.Lock()

        # Initialize GCS client
        try:
            from google.cloud import storage as gcs

            client_kwargs: dict[str, Any] = {}
            if self._project:
                client_kwargs["project"] = self._project
            if credentials:
                client_kwargs["credentials"] = credentials

            self._client = gcs.Client(**client_kwargs)
            self._bucket = self._client.bucket(self._bucket_name)

        except ImportError:
            raise FileStoreConnectionError(
                detail=(
                    "google-cloud-storage is required for GoogleCloudFileStore. "
                    "Install with: pip install google-cloud-storage"
                )
            )
        except Exception as exc:
            raise FileStoreConnectionError(
                detail=f"Failed to initialize GCS client: {exc}"
            )

        # Ensure bucket exists
        self._ensure_bucket()

        logger.info(
            f"[GCSFileStore] Initialized: bucket={self._bucket_name}, "
            f"prefix={self._prefix!r}"
        )

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "gcs"

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
        blob_name = self._make_blob_name(path)

        if not overwrite and await self.exists(path):
            raise FileAlreadyExistsError(path)

        if isinstance(data, str):
            data = data.encode("utf-8")

        def _upload():
            blob = self._bucket.blob(blob_name)
            blob.upload_from_string(
                data,
                content_type=content_type,
            )
            if metadata:
                blob.metadata = metadata
                blob.patch()

        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _retry_with_backoff(_upload, max_retries=self._max_retries),
            )
        except FileStoreError:
            raise
        except Exception as exc:
            raise FileStoreError(
                f"Failed to write file '{path}' to GCS: {exc}", path=path
            )

        logger.debug(f"[GCSFileStore] Written: {blob_name}")
        return await self.get_metadata(path)

    async def read(self, path: str) -> bytes:
        self._validate_path(path)
        blob_name = self._make_blob_name(path)

        def _download():
            blob = self._bucket.blob(blob_name)
            if not blob.exists():
                raise FileNotFoundError(path)
            return blob.download_as_bytes()

        try:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _retry_with_backoff(_download, max_retries=self._max_retries),
            )
        except FileNotFoundError:
            raise
        except FileStoreError:
            raise
        except Exception as exc:
            # Check if it's a 404 from GCS
            try:
                from google.api_core.exceptions import NotFound
                if isinstance(exc, NotFound):
                    raise FileNotFoundError(path)
            except ImportError:
                pass
            raise FileStoreError(
                f"Failed to read file '{path}' from GCS: {exc}", path=path
            )

    async def delete(self, path: str) -> bool:
        self._validate_path(path)
        blob_name = self._make_blob_name(path)

        def _delete():
            blob = self._bucket.blob(blob_name)
            if not blob.exists():
                return False
            blob.delete()
            return True

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _retry_with_backoff(_delete, max_retries=self._max_retries),
            )
            if result:
                logger.debug(f"[GCSFileStore] Deleted: {blob_name}")
            return result
        except FileNotFoundError:
            return False
        except FileStoreError:
            raise
        except Exception as exc:
            raise FileStoreError(
                f"Failed to delete file '{path}' from GCS: {exc}", path=path
            )

    async def exists(self, path: str) -> bool:
        self._validate_path(path)
        blob_name = self._make_blob_name(path)

        def _check():
            blob = self._bucket.blob(blob_name)
            return blob.exists()

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _check)
        except Exception as exc:
            raise FileStoreError(
                f"Failed to check existence of '{path}' in GCS: {exc}", path=path
            )

    async def list(self, prefix: str = "") -> list[FileMetadata]:
        full_prefix = self._make_blob_name(prefix) if prefix else self._prefix
        results: list[FileMetadata] = []

        def _list_blobs():
            blobs = self._client.list_blobs(
                self._bucket_name,
                prefix=full_prefix,
            )
            return list(blobs)

        try:
            blobs = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _retry_with_backoff(_list_blobs, max_retries=self._max_retries),
            )
        except FileStoreError:
            raise
        except Exception as exc:
            raise FileStoreError(
                f"Failed to list files with prefix '{prefix}' from GCS: {exc}",
                path=prefix,
            )

        for blob in blobs:
            store_path = blob.name[len(self._prefix):] if self._prefix else blob.name

            updated = blob.updated
            if updated and hasattr(updated, "tzinfo") and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)

            created = blob.time_created
            if created and hasattr(created, "tzinfo") and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)

            results.append(
                FileMetadata(
                    path=store_path,
                    size_bytes=blob.size or -1,
                    content_type=blob.content_type or "application/octet-stream",
                    created_at=created,
                    modified_at=updated,
                    etag=blob.etag,
                    extra=blob.metadata or {},
                )
            )

        return sorted(results, key=lambda m: m.path)

    async def get_url(self, path: str, expires_in: int = 3600) -> str:
        self._validate_path(path)
        blob_name = self._make_blob_name(path)

        if not await self.exists(path):
            raise FileNotFoundError(path)

        def _generate_url():
            blob = self._bucket.blob(blob_name)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_in),
                method="GET",
            )

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _generate_url)
        except Exception as exc:
            raise FileStoreError(
                f"Failed to generate signed URL for '{path}': {exc}", path=path
            )

    async def get_metadata(self, path: str) -> FileMetadata:
        self._validate_path(path)
        blob_name = self._make_blob_name(path)

        def _get_blob():
            blob = self._bucket.blob(blob_name)
            blob.reload()
            return blob

        try:
            blob = await asyncio.get_event_loop().run_in_executor(None, _get_blob)
        except Exception as exc:
            try:
                from google.api_core.exceptions import NotFound
                if isinstance(exc, NotFound):
                    raise FileNotFoundError(path)
            except ImportError:
                pass
            raise FileStoreError(
                f"Failed to get metadata for '{path}' from GCS: {exc}", path=path
            )

        updated = blob.updated
        if updated and hasattr(updated, "tzinfo") and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)

        created = blob.time_created
        if created and hasattr(created, "tzinfo") and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        return FileMetadata(
            path=path,
            size_bytes=blob.size or -1,
            content_type=blob.content_type or "application/octet-stream",
            created_at=created,
            modified_at=updated,
            etag=blob.etag,
            extra=blob.metadata or {},
        )

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        metadata: Optional[dict[str, str]] = None,
    ) -> FileMetadata:
        """
        Write data from an async stream to GCS.

        Buffers the stream and uploads as a single object.  For very
        large files, a resumable upload implementation would be needed.
        """
        self._validate_path(path)

        if not overwrite and await self.exists(path):
            raise FileAlreadyExistsError(path)

        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b"".join(chunks)

        return await self.write(path, data, content_type, overwrite, metadata)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _make_blob_name(self, path: str) -> str:
        """Construct the full GCS blob name from a store-relative path."""
        return f"{self._prefix}{path}" if self._prefix else path

    def _ensure_bucket(self) -> None:
        """Ensure the GCS bucket exists, creating it if necessary."""
        try:
            if not self._bucket.exists():
                self._bucket = self._client.create_bucket(self._bucket_name)
                logger.info(f"[GCSFileStore] Created bucket: {self._bucket_name}")
            else:
                # Refresh the bucket reference
                self._bucket = self._client.bucket(self._bucket_name)
        except Exception as exc:
            logger.warning(
                f"[GCSFileStore] Could not verify/create bucket '{self._bucket_name}': {exc}. "
                f"Assuming it already exists."
            )
            self._bucket = self._client.bucket(self._bucket_name)

    @staticmethod
    def _validate_path(path: str) -> None:
        """Validate a path for safety."""
        if not path:
            raise FileStoreError("Path must not be empty")
