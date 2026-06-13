"""
Secrets Management — FastAPI Router
=====================================

REST API endpoints for managing secrets.  All responses mask secret values
with ``***`` — raw values are **never** exposed through the API.

Endpoints:

    * ``GET    /secrets``          — List all secrets (masked)
    * ``GET    /secrets/{name}``   — Get a single secret's metadata (masked)
    * ``POST   /secrets``          — Create a new secret
    * ``PUT    /secrets/{name}``   — Update an existing secret
    * ``DELETE /secrets/{name}``   — Delete a secret
    * ``GET    /secrets/{name}/check`` — Check if a secret exists and is available

Authentication:
    All endpoints require the ``X-API-Key`` header when
    ``MANUSCLAW_API_KEY`` is set in the environment.

Usage::

    from app.secrets.router import router as secrets_router
    app.include_router(secrets_router)
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.logger import logger
from app.secrets.models import (
    SecretCreateRequest,
    SecretListResponse,
    SecretResponse,
    SecretSource,
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


# ──────────────────────────────────────────────────────────────────────────────
# Router setup
# ──────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/secrets", tags=["secrets"])

# Module-level store (lazy-initialized)
_store: Optional[SecretsStore] = None
_store_lock = __import__("threading").Lock()


def _get_store() -> SecretsStore:
    """Get or create the module-level secrets store."""
    global _store
    with _store_lock:
        if _store is None:
            _store = FileSecretsStore()
        return _store


# ──────────────────────────────────────────────────────────────────────────────
# API Key dependency (same pattern as main server)
# ──────────────────────────────────────────────────────────────────────────────

_API_KEY = os.getenv("MANUSCLAW_API_KEY", "")


async def require_api_key(
    x_api_key: Optional[str] = None,
) -> None:
    """Validate the API key if MANUSCLAW_API_KEY is configured."""
    if not _API_KEY:
        return
    # FastAPI header dependency
    if x_api_key is None:
        from fastapi.security import APIKeyHeader
        _header = APIKeyHeader(name="X-API-Key", auto_error=False)
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# ──────────────────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────────────────

def _entry_to_response(entry) -> SecretResponse:
    """Convert a SecretEntry to an API response (value always masked)."""
    api_dict = entry.to_api_dict()
    return SecretResponse(
        id=api_dict["id"],
        name=api_dict["name"],
        source=api_dict["source"],
        description=api_dict.get("description", ""),
        tags=api_dict.get("tags", []),
        has_value=api_dict.get("has_value", False),
        lookup_url=api_dict.get("lookup_url"),
        env_var=api_dict.get("env_var"),
        created_at=api_dict.get("created_at", ""),
        updated_at=api_dict.get("updated_at", ""),
    )


def _handle_store_error(exc: SecretsStoreError) -> HTTPException:
    """Convert a store error to an appropriate HTTP exception."""
    if isinstance(exc, SecretNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SecretAlreadyExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SecretEncryptionError):
        return HTTPException(status_code=500, detail="Encryption error — check server logs")
    return HTTPException(status_code=500, detail=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=SecretListResponse)
async def list_secrets():
    """
    List all stored secrets.

    Returns a list of secret metadata.  Secret values are **never** included
    in the response — the ``has_value`` field indicates whether a value is
    stored.
    """
    store = _get_store()
    try:
        entries = store.list_secrets()
        responses = [_entry_to_response(e) for e in entries]
        return SecretListResponse(secrets=responses, count=len(responses))
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)


@router.get("/{name}", response_model=SecretResponse)
async def get_secret(name: str):
    """
    Get a single secret's metadata by name.

    The secret value is **never** returned.  Use the ``has_value`` field
    to check if a value is stored.
    """
    store = _get_store()
    try:
        entry = store.get_entry(name)
        return _entry_to_response(entry)
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)


@router.post("", response_model=SecretResponse, status_code=201)
async def create_secret(req: SecretCreateRequest):
    """
    Create a new secret.

    The secret value is encrypted before storage and can **never** be
    retrieved through the API.

    Request body:
        name:        Unique name for the secret
        value:       The secret value to store (encrypted at rest)
        source:      How the value is obtained (static, lookup, env)
        description: Optional description
        tags:        Optional tags
        lookup_url:  URL for LOOKUP source secrets
        env_var:     Environment variable name for ENV source secrets
    """
    store = _get_store()

    # Validate source-specific fields
    if req.source == SecretSource.LOOKUP and not req.lookup_url:
        raise HTTPException(
            status_code=400,
            detail="lookup_url is required when source is LOOKUP",
        )
    if req.source == SecretSource.ENV and not req.env_var:
        raise HTTPException(
            status_code=400,
            detail="env_var is required when source is ENV",
        )

    try:
        entry = store.set(
            name=req.name,
            value=req.value,
            source=req.source,
            description=req.description,
            tags=req.tags,
            lookup_url=req.lookup_url,
            env_var=req.env_var,
            overwrite=False,
        )
        logger.info(f"[SecretsAPI] Secret created: {req.name} (source={req.source.value})")
        return _entry_to_response(entry)
    except SecretAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)


@router.put("/{name}", response_model=SecretResponse)
async def update_secret(name: str, req: SecretUpdateRequest):
    """
    Update an existing secret.

    Only the fields provided in the request body will be updated.
    To update the secret value, include the ``value`` field.

    The secret value is re-encrypted on each update.
    """
    store = _get_store()

    try:
        # Verify the secret exists first
        existing = store.get_entry(name)
    except SecretNotFoundError:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)

    # Prepare update fields
    new_value = req.value if req.value is not None else None
    new_description = req.description if req.description is not None else existing.description
    new_tags = req.tags if req.tags is not None else existing.tags
    new_lookup_url = req.lookup_url if req.lookup_url is not None else existing.lookup_url
    new_env_var = req.env_var if req.env_var is not None else existing.env_var

    # If no new value provided for STATIC source, we need to keep the existing encrypted value
    # We do this by re-setting with the existing value (which requires decrypting)
    if new_value is None and existing.source == SecretSource.STATIC:
        try:
            # Decrypt the existing value to re-store with updated metadata
            decrypted = store.get(name)
            new_value = decrypted.get_secret_value()
        except Exception:
            # If we can't decrypt, we can't update metadata without losing the value
            raise HTTPException(
                status_code=500,
                detail="Cannot update secret: unable to decrypt existing value",
            )

    try:
        entry = store.set(
            name=name,
            value=new_value or "",
            source=existing.source,
            description=new_description,
            tags=new_tags,
            lookup_url=new_lookup_url,
            env_var=new_env_var,
            overwrite=True,
        )
        logger.info(f"[SecretsAPI] Secret updated: {name}")
        return _entry_to_response(entry)
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)


@router.delete("/{name}")
async def delete_secret(name: str):
    """
    Delete a secret by name.

    This permanently removes the secret and its encrypted value.
    """
    store = _get_store()
    try:
        deleted = store.delete(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
        logger.info(f"[SecretsAPI] Secret deleted: {name}")
        return {"status": "deleted", "name": name}
    except SecretsStoreError as exc:
        raise _handle_store_error(exc)


@router.get("/{name}/check")
async def check_secret(name: str):
    """
    Check if a secret exists and is available for resolution.

    Returns:
        A dictionary with ``exists`` and ``available`` boolean fields.
    """
    store = _get_store()
    exists = store.exists(name)
    available = False

    if exists:
        try:
            # Try to resolve to confirm availability
            store.get(name)
            available = True
        except Exception:
            available = False

    return {
        "name": name,
        "exists": exists,
        "available": available,
    }
