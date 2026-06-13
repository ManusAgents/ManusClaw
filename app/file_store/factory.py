"""
File Storage Backends — Factory
=================================

:class:`FileStoreFactory` creates the appropriate :class:`FileStore`
instance based on configuration or environment variables.

Supported backends:

    * ``local``  — :class:`LocalFileStore` (filesystem)
    * ``s3``     — :class:`S3FileStore` (AWS S3 / MinIO)
    * ``gcs``    — :class:`GoogleCloudFileStore` (Google Cloud Storage)
    * ``memory`` — :class:`InMemoryFileStore` (testing)

Configuration sources (in priority order):

    1. Explicit ``backend`` parameter to :meth:`create`
    2. ``MANUSCLAW_FILE_STORE_BACKEND`` environment variable
    3. Application config (``file_store.backend``)
    4. Default: ``local``

Usage::

    from app.file_store.factory import FileStoreFactory

    # Auto-detect from environment
    store = FileStoreFactory.create()

    # Explicit backend
    store = FileStoreFactory.create(backend="s3", bucket="my-bucket")

    # List available backends
    backends = FileStoreFactory.list_backends()
"""

from __future__ import annotations

import os
from typing import Any, Optional

from app.file_store.base import FileStore, FileStoreConnectionError, FileStoreError
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

class FileStoreFactory:
    """
    Factory for creating :class:`FileStore` instances.

    All creation methods are class methods for convenience.  The factory
    also provides introspection methods to check which backends are
    available.
    """

    # Registry of known backends
    _BACKEND_REGISTRY: dict[str, str] = {
        "local": "app.file_store.local.LocalFileStore",
        "s3": "app.file_store.s3.S3FileStore",
        "gcs": "app.file_store.gcs.GoogleCloudFileStore",
        "memory": "app.file_store.memory.InMemoryFileStore",
    }

    @classmethod
    def create(
        cls,
        backend: Optional[str] = None,
        **kwargs: Any,
    ) -> FileStore:
        """
        Create a :class:`FileStore` instance.

        Args:
            backend: Explicit backend name (``local``, ``s3``, ``gcs``, ``memory``).
                     If ``None``, the backend is determined from environment
                     variables or defaults to ``local``.
            **kwargs: Additional keyword arguments passed to the backend
                      constructor.

        Returns:
            A :class:`FileStore` instance.

        Raises:
            FileStoreError: If the backend is unknown.
            FileStoreConnectionError: If the backend cannot be initialized.
        """
        if backend is None:
            backend = cls._detect_backend()

        backend = (backend or "local").lower().strip()

        if backend not in cls._BACKEND_REGISTRY:
            raise FileStoreError(
                f"Unknown file store backend: '{backend}'. "
                f"Available: {list(cls._BACKEND_REGISTRY.keys())}"
            )

        logger.info(f"[FileStoreFactory] Creating backend: {backend}")

        if backend == "local":
            return cls._create_local(**kwargs)
        elif backend == "s3":
            return cls._create_s3(**kwargs)
        elif backend == "gcs":
            return cls._create_gcs(**kwargs)
        elif backend == "memory":
            return cls._create_memory(**kwargs)
        else:
            # Fallback (should not happen due to the check above)
            return cls._create_local(**kwargs)

    @classmethod
    def list_backends(cls) -> list[str]:
        """
        Return a list of available backend names.

        A backend is "available" if its required dependencies are
        installed and importable.
        """
        available: list[str] = []

        # Local (always available)
        available.append("local")

        # Memory (always available)
        available.append("memory")

        # S3
        try:
            import boto3  # noqa: F401
            available.append("s3")
        except ImportError:
            pass

        # GCS
        try:
            from google.cloud import storage  # noqa: F401
            available.append("gcs")
        except ImportError:
            pass

        return available

    @classmethod
    def is_available(cls, backend: str) -> bool:
        """
        Check if a specific backend is available.

        Args:
            backend: Backend name to check.

        Returns:
            ``True`` if the backend's dependencies are installed.
        """
        return backend.lower().strip() in cls.list_backends()

    # ── Private creation methods ─────────────────────────────────────────────

    @classmethod
    def _create_local(cls, **kwargs: Any) -> FileStore:
        """Create a LocalFileStore."""
        from app.file_store.local import LocalFileStore

        base_dir = kwargs.pop("base_dir", "")
        return LocalFileStore(base_dir=base_dir)

    @classmethod
    def _create_s3(cls, **kwargs: Any) -> FileStore:
        """Create an S3FileStore."""
        try:
            from app.file_store.s3 import S3FileStore
        except ImportError:
            raise FileStoreConnectionError(
                detail="boto3 is required for the S3 backend. Install with: pip install boto3"
            )

        bucket = kwargs.pop("bucket", "")
        prefix = kwargs.pop("prefix", "")
        region = kwargs.pop("region", "")
        endpoint_url = kwargs.pop("endpoint_url", None)
        max_retries = kwargs.pop("max_retries", 3)

        return S3FileStore(
            bucket=bucket,
            prefix=prefix,
            region=region,
            endpoint_url=endpoint_url,
            max_retries=max_retries,
        )

    @classmethod
    def _create_gcs(cls, **kwargs: Any) -> FileStore:
        """Create a GoogleCloudFileStore."""
        try:
            from app.file_store.gcs import GoogleCloudFileStore
        except ImportError:
            raise FileStoreConnectionError(
                detail=(
                    "google-cloud-storage is required for the GCS backend. "
                    "Install with: pip install google-cloud-storage"
                )
            )

        bucket = kwargs.pop("bucket", "")
        prefix = kwargs.pop("prefix", "")
        project = kwargs.pop("project", None)
        credentials = kwargs.pop("credentials", None)
        max_retries = kwargs.pop("max_retries", 3)

        return GoogleCloudFileStore(
            bucket=bucket,
            prefix=prefix,
            project=project,
            credentials=credentials,
            max_retries=max_retries,
        )

    @classmethod
    def _create_memory(cls, **kwargs: Any) -> FileStore:
        """Create an InMemoryFileStore."""
        from app.file_store.memory import InMemoryFileStore

        max_file_size = kwargs.pop("max_file_size", 100 * 1024 * 1024)
        return InMemoryFileStore(max_file_size=max_file_size)

    # ── Backend detection ────────────────────────────────────────────────────

    @classmethod
    def _detect_backend(cls) -> str:
        """
        Detect the configured backend from environment or config.

        Priority:
            1. ``MANUSCLAW_FILE_STORE_BACKEND`` environment variable
            2. Application config
            3. Default: ``local``
        """
        # Environment variable
        env_backend = os.getenv("MANUSCLAW_FILE_STORE_BACKEND", "").strip().lower()
        if env_backend:
            return env_backend

        # Try config
        try:
            from app.config import Config
            cfg = Config.get()
            # Check if file_store config exists in the raw data
            raw = getattr(cfg._data, "__dict__", {})
            file_store_raw = raw.get("file_store", {})
            if isinstance(file_store_raw, dict):
                backend = file_store_raw.get("backend", "")
                if backend:
                    return backend
        except Exception:
            pass

        # Default
        return "local"


# ──────────────────────────────────────────────────────────────────────────────
# Convenience function
# ──────────────────────────────────────────────────────────────────────────────

_default_store: Optional[FileStore] = None
_store_lock = __import__("threading").Lock()


def get_default_file_store() -> FileStore:
    """
    Get or create the module-level default file store.

    The backend is determined by :class:`FileStoreFactory._detect_backend`.
    """
    global _default_store
    with _store_lock:
        if _default_store is None:
            _default_store = FileStoreFactory.create()
        return _default_store
