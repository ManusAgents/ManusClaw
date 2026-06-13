"""
Secrets Management — Data Models
==================================

Core data models for the ManusClaw secrets management subsystem.

**SecretSource** defines how a secret's value is obtained:

    * ``STATIC`` — a literal value provided at registration time
    * ``LOOKUP`` — fetched from a remote URL at resolution time
    * ``ENV``    — read from an environment variable at resolution time

**SecretStr** is a string wrapper that prevents accidental leakage of
sensitive values through ``repr()``, ``str()``, or log output.  The
underlying value is only accessible via :meth:`SecretStr.get_secret_value`.

**SecretEntry** is the persistence model that represents a stored secret,
including its source, encrypted value, metadata, and timestamps.

Usage::

    from app.secrets.models import SecretSource, SecretStr, SecretEntry

    # Wrap a sensitive value
    token = SecretStr("sk-abc123")
    print(token)          # *** (masked)
    token.get_secret_value()  # "sk-abc123"

    # Create a secret entry
    entry = SecretEntry(
        name="openai_api_key",
        source=SecretSource.STATIC,
        encrypted_value="FNT:gAAAAABoZ...",
    )
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ──────────────────────────────────────────────────────────────────────────────
# Secret Source
# ──────────────────────────────────────────────────────────────────────────────

class SecretSource(str, Enum):
    """
    Defines how a secret's value is obtained at resolution time.

    Attributes:
        STATIC:  The value was provided directly and stored encrypted.
        LOOKUP:  The value is fetched from a remote URL when resolved.
        ENV:     The value is read from an environment variable when resolved.
    """

    STATIC = "static"
    LOOKUP = "lookup"
    ENV = "env"


# ──────────────────────────────────────────────────────────────────────────────
# SecretStr — safe string wrapper
# ──────────────────────────────────────────────────────────────────────────────

class SecretStr:
    """
    A string wrapper that masks its content in string representations.

    This prevents accidental leakage of sensitive values through logging,
    debugging, or serialization.  The underlying value is only accessible
    via :meth:`get_secret_value`.

    Args:
        value: The sensitive string value to protect.

    Examples::

        secret = SecretStr("my-api-key")
        str(secret)               # '***'
        repr(secret)              # "SecretStr('***')"
        secret.get_secret_value() # 'my-api-key'
    """

    _MASK: str = "***"

    def __init__(self, value: str) -> None:
        if value is None:
            raise ValueError("SecretStr value must not be None")
        self._value = value

    def get_secret_value(self) -> str:
        """
        Return the underlying secret value.

        .. warning::

            Only call this in trusted code paths.  Never log or serialize
            the result.
        """
        return self._value

    def __str__(self) -> str:
        return self._MASK

    def __repr__(self) -> str:
        return f"SecretStr('{self._MASK}')"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def masked(self) -> str:
        """
        Return a masked representation with length hint.

        Shows the first and last character (if long enough) and
        replaces the rest with asterisks.

        Examples::

            SecretStr("sk-abc123").masked()  # "s******3"
            SecretStr("ab").masked()         # "***"
            SecretStr("").masked()           # "***"
        """
        if len(self._value) <= 3:
            return self._MASK
        return f"{self._value[0]}{'*' * (len(self._value) - 2)}{self._value[-1]}"


# ──────────────────────────────────────────────────────────────────────────────
# SecretEntry — persistence model
# ──────────────────────────────────────────────────────────────────────────────

class SecretEntry(BaseModel):
    """
    Represents a stored secret with its metadata.

    The ``encrypted_value`` field contains the Fernet-encrypted secret
    value (prefixed with ``FNT:``).  It is never decrypted or exposed
    through API responses.

    Attributes:
        id:              Unique identifier (auto-generated UUID).
        name:            Human-readable name for the secret (must be unique).
        source:          How the secret value is obtained.
        encrypted_value: Fernet-encrypted value (STATIC source) or lookup config.
        description:     Optional human-readable description.
        tags:            Optional list of tags for categorization.
        created_at:      Timestamp when the secret was created.
        updated_at:      Timestamp when the secret was last updated.
        lookup_url:      URL for LOOKUP source secrets.
        env_var:         Environment variable name for ENV source secrets.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    source: SecretSource = SecretSource.STATIC
    encrypted_value: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    lookup_url: Optional[str] = None
    env_var: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_must_be_valid(cls, v: str) -> str:
        """Validate that the secret name is non-empty and well-formed."""
        v = v.strip()
        if not v:
            raise ValueError("Secret name must not be empty")
        if len(v) > 256:
            raise ValueError("Secret name must be at most 256 characters")
        # Allow alphanumeric, underscores, hyphens, dots, and slashes
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. /")
        invalid = set(v) - allowed
        if invalid:
            raise ValueError(
                f"Secret name contains invalid characters: {invalid!r}. "
                f"Allowed: alphanumeric, underscore, hyphen, dot, space, slash."
            )
        return v

    def to_api_dict(self) -> dict[str, Any]:
        """
        Return a dictionary safe for API responses.

        The ``encrypted_value`` is never included.  A ``value_masked``
        field is added instead to indicate the secret has a value.
        """
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source.value,
            "description": self.description,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "has_value": bool(self.encrypted_value),
            "lookup_url": self.lookup_url,
            "env_var": self.env_var,
        }


# ──────────────────────────────────────────────────────────────────────────────
# API Request/Response models
# ──────────────────────────────────────────────────────────────────────────────

class SecretCreateRequest(BaseModel):
    """Request body for creating a new secret."""

    name: str
    value: str
    source: SecretSource = SecretSource.STATIC
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    lookup_url: Optional[str] = None
    env_var: Optional[str] = None

    @field_validator("value")
    @classmethod
    def value_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Secret value must not be empty")
        return v

    @field_validator("name")
    @classmethod
    def name_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Secret name must not be empty")
        return v

    @field_validator("lookup_url")
    @classmethod
    def lookup_url_required_for_lookup(cls, v: Optional[str], info) -> Optional[str]:
        # When source is LOOKUP, lookup_url must be provided
        source = info.data.get("source", SecretSource.STATIC)
        if source == SecretSource.LOOKUP and not v:
            raise ValueError("lookup_url is required when source is LOOKUP")
        return v

    @field_validator("env_var")
    @classmethod
    def env_var_required_for_env(cls, v: Optional[str], info) -> Optional[str]:
        source = info.data.get("source", SecretSource.STATIC)
        if source == SecretSource.ENV and not v:
            raise ValueError("env_var is required when source is ENV")
        return v


class SecretUpdateRequest(BaseModel):
    """Request body for updating an existing secret."""

    value: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    lookup_url: Optional[str] = None
    env_var: Optional[str] = None


class SecretResponse(BaseModel):
    """API response for a single secret (value always masked)."""

    id: str
    name: str
    source: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    has_value: bool = False
    lookup_url: Optional[str] = None
    env_var: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class SecretListResponse(BaseModel):
    """API response for listing secrets."""

    secrets: list[SecretResponse]
    count: int
