"""
File Storage Backends — manusclaw File Store Subsystem
========================================================

Enterprise-grade file storage backends for the ManusClaw agent framework,
inspired by OpenHands' approach to flexible storage abstraction.

Components:
    **Base**:
        - :class:`FileStore` — abstract base class for all storage backends
        - :class:`FileMetadata` — immutable metadata about a stored file
        - :class:`FileStoreError` — base exception for store errors

    **Backends**:
        - :class:`LocalFileStore` — filesystem-backed storage
        - :class:`S3FileStore` — AWS S3 / MinIO storage
        - :class:`GoogleCloudFileStore` — Google Cloud Storage
        - :class:`InMemoryFileStore` — in-memory storage (testing)

    **Factory**:
        - :class:`FileStoreFactory` — creates backends based on config
        - :func:`get_default_file_store` — module-level default store

Quick start::

    from app.file_store import FileStoreFactory, LocalFileStore, InMemoryFileStore

    # Auto-detect backend from environment
    store = FileStoreFactory.create()

    # Explicit backend
    store = LocalFileStore(base_dir="/var/lib/manusclaw/files")
    await store.write("hello.txt", b"Hello, World!", content_type="text/plain")
    data = await store.read("hello.txt")
    url = await store.get_url("hello.txt")

    # Testing
    store = InMemoryFileStore()
    await store.write("test.txt", b"test data")
"""

from app.file_store.base import (
    FileAlreadyExistsError,
    FileMetadata,
    FileNotFoundError,
    FileStore,
    FileStoreConnectionError,
    FileStoreError,
    FileStorePermissionError,
)
from app.file_store.local import LocalFileStore
from app.file_store.memory import InMemoryFileStore
from app.file_store.factory import (
    FileStoreFactory,
    get_default_file_store,
)

# Lazy imports for cloud backends (may not have dependencies installed)
# These are available via FileStoreFactory.create(backend="s3") etc.

__all__ = [
    # Base
    "FileStore",
    "FileMetadata",
    "FileStoreError",
    "FileNotFoundError",
    "FileAlreadyExistsError",
    "FileStorePermissionError",
    "FileStoreConnectionError",
    # Backends
    "LocalFileStore",
    "InMemoryFileStore",
    # Factory
    "FileStoreFactory",
    "get_default_file_store",
]


def __getattr__(name: str):
    """
    Lazy import for cloud backends that may not have dependencies installed.

    Allows ``from app.file_store import S3FileStore`` to work even when
    boto3 is not installed — the ImportError is raised only when the
    class is actually used.
    """
    if name == "S3FileStore":
        from app.file_store.s3 import S3FileStore
        return S3FileStore
    if name == "GoogleCloudFileStore":
        from app.file_store.gcs import GoogleCloudFileStore
        return GoogleCloudFileStore
    raise AttributeError(f"module 'app.file_store' has no attribute {name!r}")
