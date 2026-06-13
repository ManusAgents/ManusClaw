"""
Secrets Management — Secret Registry
======================================

The **SecretRegistry** provides a named registry of secrets that can be
looked up by agents during execution.  It acts as a runtime cache that
lazily resolves secrets from a backing :class:`SecretsStore`.

Features:

    * Named lookup — agents request secrets by name
    * Lazy resolution — secrets are resolved from the store on first access
    * Caching — resolved values are cached for the lifetime of the registry
    * Namespace support — secrets can be organized into namespaces
    * Thread-safe — all operations are protected by a lock
    * Audit logging — all access is logged (with masked values)

Usage::

    from app.secrets.registry import SecretRegistry
    from app.secrets.store import FileSecretsStore

    store = FileSecretsStore()
    registry = SecretRegistry(store=store)

    # Register named secrets
    registry.register("openai_key", "openai_api_key")
    registry.register("db_password", "database_password")

    # Resolve at runtime
    api_key = registry.resolve("openai_key")  # returns SecretStr
    api_key.get_secret_value()                # "sk-abc123"

    # List available names
    registry.list_names()  # ["openai_key", "db_password"]

    # Check availability
    registry.is_available("openai_key")  # True
"""

from __future__ import annotations

import threading
from typing import Optional

from app.logger import logger
from app.secrets.models import SecretEntry, SecretSource, SecretStr
from app.secrets.store import (
    SecretNotFoundError,
    SecretsStore,
    SecretsStoreError,
)


# ──────────────────────────────────────────────────────────────────────────────
# Registry entry
# ──────────────────────────────────────────────────────────────────────────────

class _RegistryEntry:
    """
    Internal registry entry that maps a registry name to a store name.

    Attributes:
        registry_name:  The name used by agents to look up the secret.
        store_name:     The name of the secret in the backing store.
        namespace:      Optional namespace for organization.
        description:    Optional human-readable description.
        _cached_value:  Cached resolved value (if any).
        _resolved:      Whether the value has been resolved.
    """

    __slots__ = (
        "registry_name",
        "store_name",
        "namespace",
        "description",
        "_cached_value",
        "_resolved",
    )

    def __init__(
        self,
        registry_name: str,
        store_name: str,
        namespace: str = "",
        description: str = "",
    ) -> None:
        self.registry_name = registry_name
        self.store_name = store_name
        self.namespace = namespace
        self.description = description
        self._cached_value: Optional[SecretStr] = None
        self._resolved: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Secret Registry
# ──────────────────────────────────────────────────────────────────────────────

class SecretRegistry:
    """
    Named registry of secrets available to agents during execution.

    The registry maps human-friendly names to secret entries in a backing
    :class:`SecretsStore`.  Secrets are lazily resolved and cached for
    the lifetime of the registry instance.

    Args:
        store: The backing :class:`SecretsStore` for secret resolution.
               If ``None``, a :class:`FileSecretsStore` is created.

    Thread Safety:
        All public methods are thread-safe.
    """

    def __init__(self, store: Optional[SecretsStore] = None) -> None:
        self._store = store
        self._entries: dict[str, _RegistryEntry] = {}
        self._lock = threading.Lock()
        self._prefix_separator: str = "/"

    # ── Store access ─────────────────────────────────────────────────────────

    @property
    def store(self) -> SecretsStore:
        """
        Return the backing store, creating one lazily if needed.

        Returns:
            The :class:`SecretsStore` instance.
        """
        if self._store is None:
            from app.secrets.store import FileSecretsStore
            self._store = FileSecretsStore()
        return self._store

    # ── Registration ─────────────────────────────────────────────────────────

    def register(
        self,
        registry_name: str,
        store_name: str,
        namespace: str = "",
        description: str = "",
    ) -> None:
        """
        Register a named secret in the registry.

        Args:
            registry_name: The name agents will use to look up the secret.
            store_name:    The name of the secret in the backing store.
            namespace:     Optional namespace for organization.
            description:   Optional human-readable description.

        Raises:
            ValueError: If ``registry_name`` is empty.
        """
        registry_name = registry_name.strip()
        if not registry_name:
            raise ValueError("registry_name must not be empty")

        store_name = store_name.strip()
        if not store_name:
            raise ValueError("store_name must not be empty")

        with self._lock:
            self._entries[registry_name] = _RegistryEntry(
                registry_name=registry_name,
                store_name=store_name,
                namespace=namespace,
                description=description,
            )

        logger.debug(
            f"[SecretRegistry] Registered '{registry_name}' -> '{store_name}'"
            + (f" (ns: {namespace})" if namespace else "")
        )

    def unregister(self, registry_name: str) -> bool:
        """
        Remove a secret from the registry.

        This does not delete the secret from the backing store.

        Args:
            registry_name: The name to unregister.

        Returns:
            ``True`` if the entry was removed, ``False`` if it was not registered.
        """
        with self._lock:
            entry = self._entries.pop(registry_name, None)
            if entry is not None:
                logger.debug(f"[SecretRegistry] Unregistered '{registry_name}'")
                return True
            return False

    # ── Resolution ───────────────────────────────────────────────────────────

    def resolve(self, registry_name: str) -> SecretStr:
        """
        Resolve a named secret, returning its value as a :class:`SecretStr`.

        The value is cached after the first resolution.  Subsequent calls
        return the cached value without re-querying the store.

        Args:
            registry_name: The name of the secret to resolve.

        Returns:
            A :class:`SecretStr` wrapping the secret value.

        Raises:
            SecretNotFoundError: If the name is not registered or the
                                 backing store doesn't have the secret.
            SecretsStoreError: If resolution fails for any other reason.
        """
        with self._lock:
            entry = self._entries.get(registry_name)
            if entry is None:
                raise SecretNotFoundError(
                    f"Secret '{registry_name}' is not registered in the registry"
                )

            # Return cached value if available
            if entry._resolved and entry._cached_value is not None:
                logger.debug(
                    f"[SecretRegistry] Resolved '{registry_name}' from cache"
                )
                return entry._cached_value

        # Resolve from store (outside lock to avoid holding it during I/O)
        try:
            value = self.store.get(entry.store_name)
        except SecretNotFoundError:
            raise SecretNotFoundError(
                f"Secret '{entry.store_name}' (registered as '{registry_name}') "
                f"not found in the backing store"
            )
        except SecretsStoreError:
            raise
        except Exception as exc:
            raise SecretsStoreError(
                f"Failed to resolve secret '{registry_name}': {exc}",
                name=registry_name,
            )

        # Cache the result
        with self._lock:
            entry._cached_value = value
            entry._resolved = True

        logger.debug(
            f"[SecretRegistry] Resolved '{registry_name}' from store "
            f"(masked: {value.masked()})"
        )
        return value

    def resolve_all(self) -> dict[str, SecretStr]:
        """
        Resolve all registered secrets.

        Returns:
            A dictionary mapping registry names to resolved :class:`SecretStr` values.

        Raises:
            SecretsStoreError: If any secret fails to resolve.
        """
        result: dict[str, SecretStr] = {}
        with self._lock:
            names = list(self._entries.keys())

        for name in names:
            result[name] = self.resolve(name)

        return result

    # ── Query ────────────────────────────────────────────────────────────────

    def list_names(self, namespace: str = "") -> list[str]:
        """
        List registered secret names, optionally filtered by namespace.

        Args:
            namespace: If provided, only return secrets in this namespace.

        Returns:
            A sorted list of registry names.
        """
        with self._lock:
            if namespace:
                return sorted(
                    name
                    for name, entry in self._entries.items()
                    if entry.namespace == namespace
                )
            return sorted(self._entries.keys())

    def list_namespaces(self) -> list[str]:
        """
        List all namespaces that have at least one registered secret.

        Returns:
            A sorted list of namespace strings.
        """
        with self._lock:
            return sorted(
                {entry.namespace for entry in self._entries.values() if entry.namespace}
            )

    def is_available(self, registry_name: str) -> bool:
        """
        Check if a secret is registered and can be resolved.

        This checks both the registry and the backing store.

        Args:
            registry_name: The name to check.

        Returns:
            ``True`` if the secret is available, ``False`` otherwise.
        """
        with self._lock:
            entry = self._entries.get(registry_name)
            if entry is None:
                return False

        # Check the backing store
        try:
            return self.store.exists(entry.store_name)
        except Exception:
            return False

    def get_metadata(self, registry_name: str) -> dict:
        """
        Return metadata for a registered secret without resolving the value.

        Args:
            registry_name: The name to query.

        Returns:
            A dictionary with registry metadata (no secret value).

        Raises:
            SecretNotFoundError: If the name is not registered.
        """
        with self._lock:
            entry = self._entries.get(registry_name)
            if entry is None:
                raise SecretNotFoundError(
                    f"Secret '{registry_name}' is not registered"
                )
            return {
                "registry_name": entry.registry_name,
                "store_name": entry.store_name,
                "namespace": entry.namespace,
                "description": entry.description,
                "resolved": entry._resolved,
            }

    # ── Bulk operations ──────────────────────────────────────────────────────

    def clear_cache(self) -> None:
        """
        Clear all cached secret values.

        Subsequent calls to :meth:`resolve` will re-query the backing store.
        """
        with self._lock:
            for entry in self._entries.values():
                entry._cached_value = None
                entry._resolved = False
        logger.debug("[SecretRegistry] Cache cleared")

    def register_from_store(self, namespace: str = "") -> int:
        """
        Auto-register all secrets from the backing store.

        Each secret in the store is registered with its store name
        as the registry name.

        Args:
            namespace: Optional namespace to assign to all registered secrets.

        Returns:
            The number of secrets registered.
        """
        try:
            entries = self.store.list_secrets()
        except Exception as exc:
            logger.error(f"[SecretRegistry] Failed to list store secrets: {exc}")
            return 0

        count = 0
        for entry in entries:
            try:
                self.register(
                    registry_name=entry.name,
                    store_name=entry.name,
                    namespace=namespace,
                    description=entry.description,
                )
                count += 1
            except ValueError:
                continue

        logger.info(
            f"[SecretRegistry] Auto-registered {count} secrets from store"
            + (f" (namespace: {namespace})" if namespace else "")
        )
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, registry_name: str) -> bool:
        with self._lock:
            return registry_name in self._entries

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._entries)
            resolved = sum(
                1 for e in self._entries.values() if e._resolved
            )
        return (
            f"SecretRegistry(entries={count}, resolved={resolved}, "
            f"store={type(self.store).__name__})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Module-level default registry
# ──────────────────────────────────────────────────────────────────────────────

_default_registry: Optional[SecretRegistry] = None
_registry_lock = threading.Lock()


def get_default_registry() -> SecretRegistry:
    """
    Get or create the module-level default :class:`SecretRegistry`.

    The default registry uses a :class:`FileSecretsStore` and auto-registers
    all secrets from the store.
    """
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = SecretRegistry()
            _default_registry.register_from_store()
        return _default_registry
