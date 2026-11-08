from __future__ import annotations

"""
ManusClaw multi-user authentication core.

Replaces the single static ``MANUSCLAW_API_KEY`` with per-user API keys:

- Keys are stored hashed (PBKDF2-HMAC-SHA256) in the users database
  (see :mod:`app.db.users`); the plaintext is shown once at creation.
- ``mck_...`` keys resolve to a :class:`UserPrincipal` via the DB.
- The legacy static ``MANUSCLAW_API_KEY`` keeps working: it authenticates
  as the built-in ``legacy-admin`` principal so existing deployments are
  not broken.
- When no static key is set and no keys exist, the server runs in
  anonymous mode (everything allowed) — the previous default behaviour.
  Setting ``MANUSCLAW_ADMIN_KEY`` bootstraps an ``admin`` user idempotently.

Verification results are cached for a short TTL so the cost of PBKDF2 is
paid once per key, not once per request. Revoking a key invalidates the
cache entry immediately.

CLI (no server required)::

    python -m app.server.auth add-user alice --role admin
    python -m app.server.auth add-key alice --name ci
    python -m app.server.auth list-users
    python -m app.server.auth list-keys alice
    python -m app.server.auth revoke-key <key_id>
    python -m app.server.auth set-role alice admin
"""

import argparse
import asyncio
import hashlib
import hmac
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from app.db.users import UserDB
from app.logger import logger

ANONYMOUS_USER_ID = "anonymous"
LEGACY_USER_ID = "legacy-admin"


@dataclass(frozen=True)
class UserPrincipal:
    """The authenticated identity behind a request."""

    user_id: str
    username: str
    role: str
    source: str  # "db_key" | "legacy_static" | "anonymous"
    key_id: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "source": self.source,
            "key_id": self.key_id,
        }


ANONYMOUS = UserPrincipal(
    user_id=ANONYMOUS_USER_ID,
    username="anonymous",
    role="admin",
    source="anonymous",
)
LEGACY_ADMIN = UserPrincipal(
    user_id=LEGACY_USER_ID,
    username="legacy-admin",
    role="admin",
    source="legacy_static",
)


class AuthError(Exception):
    """Raised when a request presents no valid credentials while auth is on."""


def get_principal(request: Any) -> UserPrincipal:
    """Read the principal stored by the auth dependency (anonymous fallback)."""
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, UserPrincipal) else ANONYMOUS


class AuthService:
    """Resolves presented API keys to principals and owns session claims."""

    def __init__(
        self,
        userdb: Optional[UserDB] = None,
        static_api_key: Optional[str] = None,
        cache_ttl_s: float = 300.0,
        cache_max_entries: int = 1024,
    ) -> None:
        self._userdb = userdb or UserDB()
        self._static_api_key = (
            os.getenv("MANUSCLAW_API_KEY", "")
            if static_api_key is None
            else static_api_key
        )
        self._cache_ttl_s = cache_ttl_s
        self._cache_max_entries = cache_max_entries
        self._cache: OrderedDict[str, tuple[float, UserPrincipal]] = OrderedDict()

    # ─── Resolution ──────────────────────────────────────────────────────

    async def auth_enabled(self) -> bool:
        """True when credentials are enforced (static key set or keys exist)."""
        if self._static_api_key:
            return True
        return bool(await self._userdb.has_any_active_keys())

    async def resolve(self, raw_key: str) -> Optional[UserPrincipal]:
        """Resolve a plaintext key to a principal, or None if invalid."""
        if not raw_key:
            return None

        cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(cache_key)
        if hit is not None and hit[0] > now:
            self._cache.move_to_end(cache_key)
            return hit[1]

        row = await self._userdb.resolve_api_key(raw_key)
        if row is not None:
            principal = UserPrincipal(
                user_id=row["user_id"],
                username=row["username"],
                role=row["role"],
                source="db_key",
                key_id=row["key_id"],
            )
            self._cache_put(cache_key, principal, now)
            return principal

        if self._static_api_key and hmac.compare_digest(raw_key, self._static_api_key):
            return LEGACY_ADMIN

        return None

    async def authenticate(self, presented_key: Optional[str]) -> UserPrincipal:
        """Authenticate a presented key. Raises AuthError when rejected."""
        raw = (presented_key or "").strip()
        if not await self.auth_enabled():
            return ANONYMOUS
        if raw:
            principal = await self.resolve(raw)
            if principal is not None:
                return principal
        raise AuthError("Invalid or missing API key.")

    def _cache_put(self, cache_key: str, principal: UserPrincipal, now: float) -> None:
        self._cache[cache_key] = (now + self._cache_ttl_s, principal)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)

    def invalidate_key(self, key_id: str) -> None:
        """Drop cached principals for a revoked key (immediate revocation)."""
        for cache_key, (_, principal) in list(self._cache.items()):
            if principal.key_id == key_id:
                self._cache.pop(cache_key, None)

    # ─── Session ownership ───────────────────────────────────────────────

    async def claim_session(self, session_id: str, principal: UserPrincipal) -> bool:
        """Claim ownership of a session for a user.

        Returns False when the session is already owned by a different
        non-admin user. Anonymous mode claims nothing and allows all.
        """
        if principal.source == "anonymous":
            return True
        owner = await self._userdb.get_session_owner(session_id)
        if owner:
            # Already claimed: the owner keeps it; admins may enter but do
            # not steal ownership.
            return owner == principal.user_id or principal.is_admin
        await self._userdb.set_session_owner(session_id, principal.user_id)
        return True

    async def can_access_session(self, session_id: str, principal: UserPrincipal) -> bool:
        """True if the principal may read the session (owner or admin)."""
        if principal.source == "anonymous" or principal.is_admin:
            return True
        owner = await self._userdb.get_session_owner(session_id)
        return owner == principal.user_id

    async def owned_session_ids(self, user_id: str, limit: int = 100) -> list[str]:
        return await self._userdb.owned_session_ids(user_id, limit=limit)

    # ─── Bootstrap ───────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Ensure the admin bootstrap key from ``MANUSCLAW_ADMIN_KEY`` exists.

        Idempotent: the key is only added when it does not already verify
        against the admin user's active keys.
        """
        raw = os.getenv("MANUSCLAW_ADMIN_KEY", "")
        if not raw:
            return
        try:
            user = await self._userdb.get_user_by_name("admin")
            if user is None:
                user = await self._userdb.create_user("admin", role="admin")
            if not await self._userdb.user_has_raw_key(user["user_id"], raw):
                await self._userdb.create_api_key(
                    user["user_id"], name="bootstrap-env", raw_key=raw
                )
                logger.info("[Auth] Admin API key bootstrapped from MANUSCLAW_ADMIN_KEY.")
        except Exception as exc:
            logger.error(f"[Auth] Bootstrap failed: {exc}")

    async def startup(self) -> None:
        """Run startup initialisation (schema is lazy; bootstrap admin)."""
        await self.bootstrap()

    def close(self) -> None:
        self._userdb.close()


auth_service = AuthService()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _print_user(user: dict[str, Any]) -> None:
    disabled = " [disabled]" if user.get("disabled") else ""
    print(f"{user['user_id']:<14} {user['username']:<24} {user['role']:<8}{disabled}")


async def _run_cli(args: argparse.Namespace) -> int:
    service = AuthService()

    if args.cmd == "add-user":
        user = await service._userdb.create_user(args.username, role=args.role)
        _, plaintext = await service._userdb.create_api_key(
            user["user_id"], name=args.key_name
        )
        _print_user(user)
        print(f"API key (shown once): {plaintext}")
        return 0

    if args.cmd == "add-key":
        user = await service._userdb.get_user_by_name(args.username)
        if user is None:
            print(f"User not found: {args.username}")
            return 1
        _, plaintext = await service._userdb.create_api_key(user["user_id"], name=args.name)
        print(f"API key for {args.username} (shown once): {plaintext}")
        return 0

    if args.cmd == "list-users":
        users = await service._userdb.list_users()
        if not users:
            print("No users.")
        else:
            print(f"{'ID':<14} {'USERNAME':<24} {'ROLE':<8}")
            for user in users:
                _print_user(user)
        return 0

    if args.cmd == "list-keys":
        user = await service._userdb.get_user_by_name(args.username)
        if user is None:
            print(f"User not found: {args.username}")
            return 1
        keys = await service._userdb.list_api_keys(user["user_id"], include_revoked=True)
        if not keys:
            print("No keys.")
        for k in keys:
            revoked = " [revoked]" if k["revoked"] else ""
            print(f"{k['key_id']:<14} {k['key_prefix']}...  {k['name']}{revoked}")
        return 0

    if args.cmd == "revoke-key":
        revoked = await service._userdb.revoke_api_key(args.key_id)
        service.invalidate_key(args.key_id)
        print(f"Revoked {args.key_id}." if revoked else f"Key not found: {args.key_id}")
        return 0 if revoked else 1

    if args.cmd == "set-role":
        user = await service._userdb.get_user_by_name(args.username)
        if user is None:
            print(f"User not found: {args.username}")
            return 1
        await service._userdb.set_user_role(user["user_id"], args.role)
        print(f"{args.username} is now {args.role}.")
        return 0

    return 1


def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.server.auth",
        description="ManusClaw user & API key management",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-user", help="Create a user with an initial API key")
    p.add_argument("username")
    p.add_argument("--role", default="user", choices=["user", "admin"])
    p.add_argument("--key-name", default="")

    p = sub.add_parser("add-key", help="Add another API key for a user")
    p.add_argument("username")
    p.add_argument("--name", default="")

    sub.add_parser("list-users", help="List users")

    p = sub.add_parser("list-keys", help="List a user's API keys")
    p.add_argument("username")

    p = sub.add_parser("revoke-key", help="Revoke an API key by key_id")
    p.add_argument("key_id")

    p = sub.add_parser("set-role", help="Change a user's role")
    p.add_argument("username")
    p.add_argument("role", choices=["user", "admin"])

    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    _cli()
