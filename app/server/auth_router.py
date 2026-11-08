from __future__ import annotations

"""
FastAPI routes and dependencies for ManusClaw multi-user auth.

Dependencies:
    require_user           — resolves X-API-Key to a UserPrincipal (401 on failure)
    require_session_access — additionally enforces session ownership (404 when denied)

Routes (all under ``/auth``):
    GET    /auth/me            — who am I
    POST   /auth/keys          — create an API key for myself (plaintext shown once)
    GET    /auth/keys          — list my API keys
    DELETE /auth/keys/{key_id} — revoke my own key (admins may revoke any)
    GET    /auth/users         — admin: list users
    POST   /auth/users         — admin: create a user with an initial key
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.logger import logger
from app.server.auth import (
    AuthError,
    UserPrincipal,
    auth_service,
    get_principal,
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

router = APIRouter(prefix="/auth", tags=["auth"])


# ─── Dependencies ─────────────────────────────────────────────────────────────

async def require_user(
    request: Request,
    key: Optional[str] = Depends(_api_key_header),
) -> UserPrincipal:
    """Resolve the presented API key to a principal (401 when rejected)."""
    try:
        principal = await auth_service.authenticate(key)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    request.state.principal = principal
    return principal


async def require_session_access(
    session_id: str,
    principal: UserPrincipal = Depends(require_user),
) -> UserPrincipal:
    """Enforce that the principal may access ``session_id`` (404 when denied)."""
    if not await auth_service.can_access_session(session_id, principal):
        raise HTTPException(status_code=404, detail="Session not found.")
    return principal


async def authenticate_ws(websocket: WebSocket) -> Optional[UserPrincipal]:
    """Authenticate a WebSocket handshake (header or ``?api_key=`` query).

    Returns the principal, or None when credentials are required but
    invalid/missing. In anonymous mode returns the anonymous principal.
    """
    token = (
        websocket.query_params.get("api_key")
        or websocket.headers.get("x-api-key", "")
    )
    try:
        return await auth_service.authenticate(token or None)
    except AuthError:
        return None


# ─── Request models ───────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str = ""


class CreateUserRequest(BaseModel):
    username: str
    role: str = "user"
    key_name: str = ""


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/me")
async def whoami(principal: UserPrincipal = Depends(require_user)) -> dict[str, Any]:
    """Return the authenticated principal."""
    return principal.to_dict()


@router.post("/keys")
async def create_my_key(
    body: CreateKeyRequest,
    principal: UserPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """Create an API key for the authenticated DB-backed user.

    The plaintext key is returned exactly once and stored only as a hash.
    Requires a database-backed identity — not available in anonymous or
    legacy static-key mode (use the CLI or ``MANUSCLAW_ADMIN_KEY`` there).
    """
    if principal.source != "db_key":
        raise HTTPException(
            status_code=400,
            detail=(
                "Key creation requires a user account. Use "
                "`python -m app.server.auth add-user` or set MANUSCLAW_ADMIN_KEY."
            ),
        )
    row, plaintext = await auth_service._userdb.create_api_key(
        principal.user_id, name=body.name
    )
    logger.info(f"[Auth] Key {row['key_id']} created for {principal.username}")
    return {**row, "api_key": plaintext}


@router.get("/keys")
async def list_my_keys(
    principal: UserPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """List the authenticated user's API keys (no secrets)."""
    if principal.source != "db_key":
        return {"keys": [], "count": 0}
    keys = await auth_service._userdb.list_api_keys(principal.user_id)
    return {"keys": keys, "count": len(keys)}


@router.delete("/keys/{key_id}")
async def revoke_my_key(
    key_id: str,
    principal: UserPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """Revoke an API key. Users revoke their own; admins revoke any."""
    owner_filter = principal.user_id if not principal.is_admin else None
    revoked = await auth_service._userdb.revoke_api_key(key_id, user_id=owner_filter)
    if not revoked:
        raise HTTPException(status_code=404, detail="Key not found.")
    auth_service.invalidate_key(key_id)
    logger.info(f"[Auth] Key {key_id} revoked by {principal.username}")
    return {"status": "revoked", "key_id": key_id}


@router.get("/users")
async def list_users(
    principal: UserPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """Admin: list users with active key counts."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required.")
    users = await auth_service._userdb.list_users()
    return {"users": users, "count": len(users)}


@router.post("/users")
async def create_user(
    body: CreateUserRequest,
    principal: UserPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """Admin: create a user with an initial API key (plaintext shown once).

    When the server runs in anonymous mode (no users, no static key) this
    endpoint bootstraps the first account — afterwards auth is enforced.
    """
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required.")
    try:
        user = await auth_service._userdb.create_user(body.username, role=body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _, plaintext = await auth_service._userdb.create_api_key(
        user["user_id"], name=body.key_name or "initial"
    )
    logger.info(f"[Auth] User {body.username} created ({body.role}) by {principal.username}")
    return {**user, "api_key": plaintext}
