"""
Security Defense-in-Depth — Fernet Cipher
============================================

Symmetric encryption for data at rest using the Fernet construction
(AES-128-CBC with HMAC-SHA256 for authentication).  This module
provides a simple, safe API for encrypting sensitive data before
storing it to disk, database, or logs.

Key features:
    * **Fernet-based** — each ciphertext includes a timestamp and is
      authenticated, so tampering is detected at decrypt time.
    * **Prefixed tokens** — encrypted values are prefixed with
      ``FERNET_TOKEN_PREFIX`` (``"FNT:"``) so that downstream consumers
      can distinguish encrypted from plaintext values without trying
      decryption.
    * **Thread-safe** — the Fernet instance is immutable after
      construction; encrypt/decrypt are stateless.
    * **Crash-proof** — all errors are caught and wrapped in
      :class:`CipherError` with sanitised messages.  The caller never
      gets a raw ``cryptography`` exception that might leak key
      material.
    * **Key rotation** — :meth:`rotate_key` creates a new cipher with
      a fresh key while the old cipher remains available for decrypting
      existing tokens.

Environment:
    The encryption key can be provided directly or via the
    ``MANUSCLAW_FERNET_KEY`` environment variable.  If neither is
    available, a new key is generated at startup and logged (with a
    warning) so that the system works in development without
    configuration.

.. warning::

    Fernet keys are **not** derived from passwords.  They are 32-byte
    base64-encoded tokens.  Store the key securely (vault, KMS, etc.).
"""

from __future__ import annotations

import base64
import os
import threading
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

FERNET_TOKEN_PREFIX: str = "FNT:"
"""
Prefix applied to all encrypted tokens produced by :class:`Cipher`.

This allows downstream systems to distinguish encrypted from plaintext
values without attempting decryption.  Example encrypted value::

    FNT:gAAAAABoZXRf...
"""

_ENV_KEY_NAME = "MANUSCLAW_FERNET_KEY"


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class CipherError(Exception):
    """
    Raised when encryption or decryption fails.

    The message is sanitised to prevent key material from leaking.
    """

    def __init__(self, operation: str, detail: str = "") -> None:
        self.operation = operation
        self.detail = detail
        msg = f"Cipher {operation} failed"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class CipherKeyError(CipherError):
    """Raised when the Fernet key is missing or invalid."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("key", detail)


class CipherTokenError(CipherError):
    """Raised when decryption fails due to an invalid or tampered token."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("decrypt", detail)


# ──────────────────────────────────────────────────────────────────────────────
# Cipher class
# ──────────────────────────────────────────────────────────────────────────────

class Cipher:
    """
    Thread-safe Fernet cipher for encrypting data at rest.

    Args:
        key:        Base64-encoded 32-byte Fernet key.  If ``None``,
                    the key is read from ``MANUSCLAW_FERNET_KEY`` env
                    var; if that is also unset, a new key is generated
                    (suitable for development only).
        prefix:     Token prefix (default: ``FERNET_TOKEN_PREFIX``).
    """

    def __init__(
        self,
        key: Optional[str] = None,
        prefix: str = FERNET_TOKEN_PREFIX,
    ) -> None:
        self._prefix = prefix
        self._lock = threading.Lock()

        # Resolve key
        resolved_key = key or os.getenv(_ENV_KEY_NAME)
        if resolved_key:
            try:
                self._fernet = Fernet(resolved_key.encode() if isinstance(resolved_key, str) else resolved_key)
                self._key_source = "provided"
            except Exception as exc:
                raise CipherKeyError(
                    f"Invalid Fernet key (sanitised). "
                    f"Ensure the key is a valid base64-encoded 32-byte token."
                ) from exc
        else:
            # Generate a new key for development
            new_key = Fernet.generate_key()
            self._fernet = Fernet(new_key)
            self._key_source = "generated"

            # Warn — generated keys are not persistent
            import warnings
            warnings.warn(
                f"No Fernet key provided (set {_ENV_KEY_NAME} env var). "
                f"A temporary key has been generated — encrypted data "
                f"will NOT survive a restart.",
                stacklevel=2,
            )

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def key_source(self) -> str:
        """How the key was obtained: ``"provided"`` or ``"generated"``."""
        return self._key_source

    @property
    def prefix(self) -> str:
        """Token prefix used by this cipher instance."""
        return self._prefix

    # ── Encrypt ──────────────────────────────────────────────────────────

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt *plaintext* and return a prefixed token.

        The returned string has the form ``{prefix}{base64_ciphertext}``.

        Args:
            plaintext: The string to encrypt.

        Returns:
            Prefixed Fernet token string.

        Raises:
            CipherError: If encryption fails (should be extremely rare).
        """
        if plaintext is None:
            raise CipherError("encrypt", "plaintext must not be None")

        try:
            ciphertext = self._fernet.encrypt(plaintext.encode("utf-8"))
            return f"{self._prefix}{ciphertext.decode('ascii')}"
        except Exception as exc:
            # Never leak key material in the exception
            raise CipherError("encrypt", f"Encryption error: {type(exc).__name__}") from exc

    # ── Decrypt ──────────────────────────────────────────────────────────

    def decrypt(self, token: str) -> str:
        """
        Decrypt a prefixed Fernet token.

        Args:
            token: A string produced by :meth:`encrypt`, including the
                   prefix.

        Returns:
            The original plaintext string.

        Raises:
            CipherTokenError: If the token is invalid, tampered, or
                              was encrypted with a different key.
            CipherError: For any other decryption failure.
        """
        if token is None:
            raise CipherTokenError("token must not be None")

        # Strip prefix
        ciphertext = self._strip_prefix(token)

        try:
            plaintext_bytes = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext_bytes.decode("utf-8")
        except InvalidToken:
            raise CipherTokenError(
                "Invalid or tampered token. "
                "The token may have been encrypted with a different key."
            )
        except Exception as exc:
            raise CipherError(
                "decrypt", f"Decryption error: {type(exc).__name__}"
            ) from exc

    # ── Utility ──────────────────────────────────────────────────────────

    def is_encrypted(self, value: str) -> bool:
        """
        Check if *value* looks like an encrypted token (has the prefix).

        This is a quick prefix check — it does **not** validate the
        token or attempt decryption.
        """
        if not value:
            return False
        return value.startswith(self._prefix)

    def encrypt_if_not_encrypted(self, plaintext: str) -> str:
        """
        Encrypt *plaintext* only if it is not already encrypted.

        Useful for idempotent encryption (e.g. re-encrypting a database
        row that might already contain a token).
        """
        if self.is_encrypted(plaintext):
            return plaintext
        return self.encrypt(plaintext)

    def decrypt_if_encrypted(self, token: str) -> str:
        """
        Decrypt *token* only if it looks like an encrypted token.

        If the value does not have the prefix, it is returned as-is.
        This is useful for backward-compatible code that may encounter
        both encrypted and plaintext values.
        """
        if self.is_encrypted(token):
            return self.decrypt(token)
        return token

    # ── Key rotation ─────────────────────────────────────────────────────

    def rotate_key(self, new_key: str) -> "Cipher":
        """
        Create a new :class:`Cipher` with *new_key*.

        The **current** cipher instance is stored internally so that
        existing tokens can still be decrypted via
        :meth:`decrypt_with_previous_key`.

        Args:
            new_key: Base64-encoded 32-byte Fernet key.

        Returns:
            A new :class:`Cipher` instance with the new key.
        """
        new_cipher = Cipher(key=new_key, prefix=self._prefix)
        # Preserve reference to old cipher for key rotation scenarios
        new_cipher._previous_cipher = self  # type: ignore[attr-defined]
        return new_cipher

    def decrypt_with_previous_key(self, token: str) -> Optional[str]:
        """
        Attempt to decrypt *token* using the previous cipher (if any).

        This supports key rotation: after rotating to a new key, old
        tokens can still be decrypted with the previous key.

        Returns ``None`` if there is no previous cipher or decryption
        fails.
        """
        prev: Optional["Cipher"] = getattr(self, "_previous_cipher", None)
        if prev is None:
            return None
        try:
            return prev.decrypt(token)
        except CipherError:
            return None

    # ── Internal helpers ─────────────────────────────────────────────────

    def _strip_prefix(self, token: str) -> str:
        """Remove the prefix from a token string."""
        if token.startswith(self._prefix):
            return token[len(self._prefix):]
        # No prefix — assume it's a raw ciphertext (backward compat)
        return token


# ──────────────────────────────────────────────────────────────────────────────
# Module-level convenience
# ──────────────────────────────────────────────────────────────────────────────

_default_cipher: Optional[Cipher] = None
_cipher_lock = threading.Lock()


def get_default_cipher() -> Cipher:
    """
    Get or create the module-level default :class:`Cipher`.

    The default cipher reads its key from ``MANUSCLAW_FERNET_KEY`` or
    generates one if the env var is not set.  This is safe for
    development but a key should be provided in production.
    """
    global _default_cipher
    with _cipher_lock:
        if _default_cipher is None:
            _default_cipher = Cipher()
        return _default_cipher


def encrypt(plaintext: str) -> str:
    """Encrypt using the default cipher.  See :meth:`Cipher.encrypt`."""
    return get_default_cipher().encrypt(plaintext)


def decrypt(token: str) -> str:
    """Decrypt using the default cipher.  See :meth:`Cipher.decrypt`."""
    return get_default_cipher().decrypt(token)


def is_encrypted(value: str) -> bool:
    """Check if *value* looks encrypted.  See :meth:`Cipher.is_encrypted`."""
    return get_default_cipher().is_encrypted(value)
