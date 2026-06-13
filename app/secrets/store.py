"""
Secrets Management — Secrets Store
====================================

Abstract base class and filesystem-backed implementation for persistent
secret storage.

**SecretsStore** (ABC) defines the contract for all secret stores:

    * ``get(name)``         — retrieve and decrypt a secret by name
    * ``set(name, ...)``    — store (encrypt) a secret
    * ``delete(name)``      — remove a secret
    * ``list_secrets()``    — list all stored secrets (masked)
    * ``exists(name)``      — check if a secret exists

**FileSecretsStore** implements the ABC using:

    * Filesystem storage in a dedicated directory
    * Fernet encryption via :mod:`app.security.cipher`
    * One JSON file per secret (``<name>.json``)
    * Thread-safe operations via ``threading.Lock``
    * Atomic writes (write to temp file, then rename)
    * Proper file permissions (``0600`` on POSIX)

Usage::

    from app.secrets.store import FileSecretsStore

    store = FileSecretsStore(base_dir="/var/lib/manusclaw/secrets")
    store.set("openai_key", value="sk-abc123", source=SecretSource.STATIC)
    secret = store.get("openai_key")  # returns SecretStr
    secret.get_secret_value()         # "sk-abc123"
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.secrets.models import SecretEntry, SecretSource, SecretStr


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class SecretsStoreError(Exception):
    """Base exception for secrets store errors."""

    def __init__(self, message: str, name: str = "") -> None:
        self.name = name
        super().__init__(message)


class SecretNotFoundError(SecretsStoreError):
    """Raised when a requested secret does not exist."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Secret '{name}' not found", name=name)


class SecretAlreadyExistsError(SecretsStoreError):
    """Raised when attempting to create a secret that already exists."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Secret '{name}' already exists", name=name)


class SecretEncryptionError(SecretsStoreError):
    """Raised when encryption or decryption fails."""

    def __init__(self, name: str, detail: str = "") -> None:
        msg = f"Encryption error for secret '{name}'"
        if detail:
            msg += f": {detail}"
        super().__init__(msg, name=name)


# ──────────────────────────────────────────────────────────────────────────────
# Abstract Base Class
# ──────────────────────────────────────────────────────────────────────────────

class SecretsStore(ABC):
    """
    Abstract base class for secrets storage backends.

    All methods must be thread-safe.  Secret values must be stored
    in encrypted form and never persisted in plaintext.
    """

    @abstractmethod
    def get(self, name: str) -> SecretStr:
        """
        Retrieve and decrypt a secret by name.

        Args:
            name: The unique name of the secret.

        Returns:
            A :class:`SecretStr` wrapping the decrypted value.

        Raises:
            SecretNotFoundError: If the secret does not exist.
            SecretEncryptionError: If decryption fails.
        """

    @abstractmethod
    def get_entry(self, name: str) -> SecretEntry:
        """
        Retrieve the full secret entry (metadata only, value encrypted).

        Args:
            name: The unique name of the secret.

        Returns:
            The :class:`SecretEntry` for the secret.

        Raises:
            SecretNotFoundError: If the secret does not exist.
        """

    @abstractmethod
    def set(
        self,
        name: str,
        value: str,
        source: SecretSource = SecretSource.STATIC,
        description: str = "",
        tags: Optional[list[str]] = None,
        lookup_url: Optional[str] = None,
        env_var: Optional[str] = None,
        overwrite: bool = False,
    ) -> SecretEntry:
        """
        Store a secret (encrypting the value).

        Args:
            name:        Unique name for the secret.
            value:       The plaintext value to encrypt and store.
            source:      How the secret value is obtained.
            description: Optional human-readable description.
            tags:        Optional tags for categorization.
            lookup_url:  URL for LOOKUP source secrets.
            env_var:     Environment variable name for ENV source secrets.
            overwrite:   If ``True``, overwrite an existing secret with the same name.

        Returns:
            The stored :class:`SecretEntry`.

        Raises:
            SecretAlreadyExistsError: If a secret with the same name exists
                                      and ``overwrite`` is ``False``.
            SecretEncryptionError: If encryption fails.
        """

    @abstractmethod
    def delete(self, name: str) -> bool:
        """
        Delete a secret by name.

        Args:
            name: The unique name of the secret.

        Returns:
            ``True`` if the secret was deleted, ``False`` if it did not exist.
        """

    @abstractmethod
    def list_secrets(self) -> list[SecretEntry]:
        """
        List all stored secrets.

        Returns:
            A list of :class:`SecretEntry` objects.  The ``encrypted_value``
            field is populated but should never be exposed via API.
        """

    @abstractmethod
    def exists(self, name: str) -> bool:
        """
        Check if a secret with the given name exists.

        Args:
            name: The unique name of the secret.

        Returns:
            ``True`` if the secret exists, ``False`` otherwise.
        """


# ──────────────────────────────────────────────────────────────────────────────
# Filesystem-backed implementation
# ──────────────────────────────────────────────────────────────────────────────

class FileSecretsStore(SecretsStore):
    """
    Filesystem-based secrets store with Fernet encryption.

    Each secret is stored as a separate JSON file (``<name>.json``) in the
    configured ``base_dir``.  The secret value is encrypted using the
    application's Fernet cipher (:func:`app.security.cipher.get_default_cipher`).

    Features:
        * Thread-safe via ``threading.Lock``
        * Atomic file writes (temp file + rename)
        * Restrictive file permissions (``0600`` on POSIX)
        * Idempotent directory creation

    Args:
        base_dir: Directory path for secret files.  Created if it does not exist.

    Raises:
        SecretsStoreError: If the base directory cannot be created or accessed.
    """

    def __init__(self, base_dir: str = "") -> None:
        if not base_dir:
            base_dir = os.path.join(
                os.getenv("MANUSCLAW_HOME", str(Path.home() / ".manusclaw")),
                "secrets",
            )
        self._base_dir = Path(base_dir)
        self._lock = threading.Lock()

        # Ensure directory exists with proper permissions
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._set_secure_permissions(self._base_dir, directory=True)
        except OSError as exc:
            raise SecretsStoreError(
                f"Cannot create secrets directory '{self._base_dir}': {exc}"
            )

        logger.debug(f"[FileSecretsStore] Initialized at {self._base_dir}")

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, name: str) -> SecretStr:
        entry = self.get_entry(name)

        # Resolve the value based on source type
        if entry.source == SecretSource.ENV:
            return self._resolve_env(entry)
        elif entry.source == SecretSource.LOOKUP:
            return self._resolve_lookup(entry)
        else:
            return self._decrypt_value(entry.encrypted_value, entry.name)

    def get_entry(self, name: str) -> SecretEntry:
        self._validate_name(name)
        file_path = self._secret_file(name)

        with self._lock:
            if not file_path.exists():
                raise SecretNotFoundError(name)
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                return SecretEntry.model_validate(data)
            except SecretNotFoundError:
                raise
            except Exception as exc:
                raise SecretsStoreError(
                    f"Failed to read secret '{name}': {exc}", name=name
                )

    def set(
        self,
        name: str,
        value: str,
        source: SecretSource = SecretSource.STATIC,
        description: str = "",
        tags: Optional[list[str]] = None,
        lookup_url: Optional[str] = None,
        env_var: Optional[str] = None,
        overwrite: bool = False,
    ) -> SecretEntry:
        self._validate_name(name)

        with self._lock:
            file_path = self._secret_file(name)

            if file_path.exists() and not overwrite:
                raise SecretAlreadyExistsError(name)

            # Encrypt the value for STATIC source
            encrypted_value = ""
            if source == SecretSource.STATIC:
                encrypted_value = self._encrypt_value(value, name)

            # Build the entry
            now = datetime.now(timezone.utc)
            entry = SecretEntry(
                name=name,
                source=source,
                encrypted_value=encrypted_value,
                description=description,
                tags=tags or [],
                created_at=now,
                updated_at=now,
                lookup_url=lookup_url,
                env_var=env_var,
            )

            # If overwriting, preserve the original created_at
            if file_path.exists():
                try:
                    old_data = json.loads(file_path.read_text(encoding="utf-8"))
                    entry.created_at = old_data.get(
                        "created_at",
                        entry.created_at.isoformat(),
                    )
                    if isinstance(entry.created_at, str):
                        entry.created_at = datetime.fromisoformat(entry.created_at)
                except Exception:
                    pass

            self._write_secret_file(file_path, entry)
            logger.info(f"[FileSecretsStore] Secret '{name}' {'updated' if overwrite else 'created'}")
            return entry

    def delete(self, name: str) -> bool:
        self._validate_name(name)
        file_path = self._secret_file(name)

        with self._lock:
            if not file_path.exists():
                return False
            try:
                file_path.unlink()
                logger.info(f"[FileSecretsStore] Secret '{name}' deleted")
                return True
            except OSError as exc:
                raise SecretsStoreError(
                    f"Failed to delete secret '{name}': {exc}", name=name
                )

    def list_secrets(self) -> list[SecretEntry]:
        entries: list[SecretEntry] = []

        with self._lock:
            for file_path in sorted(self._base_dir.glob("*.json")):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    entries.append(SecretEntry.model_validate(data))
                except Exception as exc:
                    logger.warning(
                        f"[FileSecretsStore] Skipping corrupt secret file "
                        f"{file_path.name}: {exc}"
                    )

        return entries

    def exists(self, name: str) -> bool:
        self._validate_name(name)
        with self._lock:
            return self._secret_file(name).exists()

    # ── Resolution helpers ───────────────────────────────────────────────────

    def _resolve_env(self, entry: SecretEntry) -> SecretStr:
        """Resolve a secret from an environment variable."""
        var_name = entry.env_var
        if not var_name:
            raise SecretsStoreError(
                f"Secret '{entry.name}' has source=ENV but no env_var configured",
                name=entry.name,
            )
        value = os.getenv(var_name)
        if value is None:
            raise SecretsStoreError(
                f"Environment variable '{var_name}' for secret '{entry.name}' is not set",
                name=entry.name,
            )
        return SecretStr(value)

    def _resolve_lookup(self, entry: SecretEntry) -> SecretStr:
        """Resolve a secret by fetching from a remote URL."""
        url = entry.lookup_url
        if not url:
            raise SecretsStoreError(
                f"Secret '{entry.name}' has source=LOOKUP but no lookup_url configured",
                name=entry.name,
            )
        try:
            import aiohttp
            # Synchronous fallback — use urllib if aiohttp is not available
            import urllib.request
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status != 200:
                    raise SecretsStoreError(
                        f"Lookup URL returned status {resp.status} for secret '{entry.name}'",
                        name=entry.name,
                    )
                value = resp.read().decode("utf-8").strip()
                if not value:
                    raise SecretsStoreError(
                        f"Lookup URL returned empty body for secret '{entry.name}'",
                        name=entry.name,
                    )
                return SecretStr(value)
        except SecretsStoreError:
            raise
        except Exception as exc:
            raise SecretsStoreError(
                f"Failed to lookup secret '{entry.name}' from URL: {exc}",
                name=entry.name,
            )

    # ── Encryption helpers ───────────────────────────────────────────────────

    @staticmethod
    def _encrypt_value(plaintext: str, name: str) -> str:
        """Encrypt a plaintext value using the default Fernet cipher."""
        try:
            from app.security.cipher import get_default_cipher
            cipher = get_default_cipher()
            return cipher.encrypt(plaintext)
        except Exception as exc:
            raise SecretEncryptionError(name, str(exc))

    @staticmethod
    def _decrypt_value(token: str, name: str) -> SecretStr:
        """Decrypt a Fernet token and return a SecretStr."""
        try:
            from app.security.cipher import get_default_cipher
            cipher = get_default_cipher()
            plaintext = cipher.decrypt(token)
            return SecretStr(plaintext)
        except Exception as exc:
            raise SecretEncryptionError(name, str(exc))

    # ── File I/O helpers ─────────────────────────────────────────────────────

    def _secret_file(self, name: str) -> Path:
        """Return the path to the JSON file for a named secret."""
        # Sanitize the name to prevent path traversal
        safe_name = name.replace("/", "_").replace("\\", "_").replace("..", "_")
        return self._base_dir / f"{safe_name}.json"

    def _write_secret_file(self, file_path: Path, entry: SecretEntry) -> None:
        """
        Write a secret entry to disk atomically.

        Uses a temporary file and rename to ensure atomicity.
        Sets restrictive permissions on the file after writing.
        """
        try:
            # Serialize entry to JSON
            data = entry.model_dump(mode="json")

            # Write to a temp file first, then rename for atomicity
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._base_dir),
                prefix=".tmp_secret_",
                suffix=".json",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False, default=str)

                # Set restrictive permissions before moving
                self._set_secure_permissions(Path(tmp_path))

                # Atomic rename
                os.replace(tmp_path, str(file_path))
            except Exception:
                # Clean up temp file on failure
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            # Set permissions on final file as well
            self._set_secure_permissions(file_path)

        except SecretsStoreError:
            raise
        except OSError as exc:
            raise SecretsStoreError(
                f"Failed to write secret file for '{entry.name}': {exc}",
                name=entry.name,
            )

    @staticmethod
    def _set_secure_permissions(path: Path, directory: bool = False) -> None:
        """
        Set restrictive permissions on a file or directory.

        On POSIX systems, sets ``0600`` for files and ``0700`` for directories.
        On Windows, this is a no-op (NTFS ACLs handle permissions differently).
        """
        if os.name != "posix":
            return

        try:
            if directory:
                path.chmod(stat.S_IRWXU)  # 0700 — rwx for owner only
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — rw for owner only
        except OSError:
            # Best-effort; don't fail if permissions can't be set
            pass

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate a secret name for path-safety."""
        if not name or not name.strip():
            raise SecretsStoreError("Secret name must not be empty")
        if len(name) > 256:
            raise SecretsStoreError("Secret name must be at most 256 characters")
        # Check for path traversal attempts
        if ".." in name or name.startswith("/"):
            raise SecretsStoreError(
                f"Invalid secret name '{name}': must not contain '..' or start with '/'"
            )
