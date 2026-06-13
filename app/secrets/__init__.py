"""
Secrets Management — manusclaw Secrets Subsystem
==================================================

Enterprise-grade secrets management for the ManusClaw agent framework,
inspired by OpenHands' approach to secure credential handling.

Components:
    **Models**:
        - :class:`SecretSource` — enum for secret origin (STATIC, LOOKUP, ENV)
        - :class:`SecretStr` — string wrapper that masks values in repr/str
        - :class:`SecretEntry` — persistence model with encrypted values

    **Store**:
        - :class:`SecretsStore` — abstract base class for secret storage
        - :class:`FileSecretsStore` — filesystem-backed store with Fernet encryption

    **Registry**:
        - :class:`SecretRegistry` — named registry for agent-accessible secrets

    **API**:
        - FastAPI router with CRUD endpoints (values always masked)

Quick start::

    from app.secrets import FileSecretsStore, SecretRegistry, SecretSource, SecretStr

    # Create a store and register secrets
    store = FileSecretsStore()
    store.set("openai_key", value="sk-abc123", source=SecretSource.STATIC)

    # Use the registry for agent access
    registry = SecretRegistry(store=store)
    registry.register("api_key", "openai_key")

    # Resolve at runtime
    secret = registry.resolve("api_key")  # SecretStr
    secret.get_secret_value()              # "sk-abc123"
"""

from app.secrets.models import (
    SecretCreateRequest,
    SecretEntry,
    SecretListResponse,
    SecretResponse,
    SecretSource,
    SecretStr,
    SecretUpdateRequest,
)
from app.secrets.store import (
    FileSecretsStore,
    SecretAlreadyExistsError,
    SecretEncryptionError,
    SecretNotFoundError,
    SecretsStore,
    SecretsStoreError,
)
from app.secrets.registry import (
    SecretRegistry,
    get_default_registry,
)

__all__ = [
    # Models
    "SecretSource",
    "SecretStr",
    "SecretEntry",
    "SecretCreateRequest",
    "SecretUpdateRequest",
    "SecretResponse",
    "SecretListResponse",
    # Store
    "SecretsStore",
    "FileSecretsStore",
    "SecretsStoreError",
    "SecretNotFoundError",
    "SecretAlreadyExistsError",
    "SecretEncryptionError",
    # Registry
    "SecretRegistry",
    "get_default_registry",
]
