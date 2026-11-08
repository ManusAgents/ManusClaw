"""Tests for multi-user auth: AuthService core + /auth router endpoints."""

import pytest

from app.db.users import UserDB
from app.server.auth import (
    ANONYMOUS,
    AuthError,
    AuthService,
    UserPrincipal,
    get_principal,
)


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    monkeypatch.setenv("MANUSCLAW_PBKDF2_ITERATIONS", "1000")


@pytest.fixture
def userdb(tmp_path):
    db = UserDB(db_path=tmp_path / "auth.db")
    yield db
    db.close()


@pytest.fixture
def service(userdb):
    return AuthService(userdb=userdb, static_api_key="static-legacy-key")


async def _make_user_key(userdb: UserDB, username: str, role: str = "user"):
    user = await userdb.create_user(username, role=role)
    row, plaintext = await userdb.create_api_key(user["user_id"], name="test")
    return user, row, plaintext


# ── Resolution ───────────────────────────────────────────────────────────────

async def test_resolve_db_key(service, userdb):
    user, row, plaintext = await _make_user_key(userdb, "alice")
    principal = await service.resolve(plaintext)
    assert principal is not None
    assert principal.source == "db_key"
    assert principal.user_id == user["user_id"]
    assert principal.key_id == row["key_id"]


async def test_resolve_legacy_static_key(service):
    principal = await service.resolve("static-legacy-key")
    assert principal is not None
    assert principal.user_id == "legacy-admin"
    assert principal.is_admin


async def test_resolve_garbage_returns_none(service):
    assert await service.resolve("mck_not_a_real_key_at_all") is None


async def test_authenticate_enabled_rejects_bad_key(service):
    with pytest.raises(AuthError):
        await service.authenticate(None)
    with pytest.raises(AuthError):
        await service.authenticate("wrong-key")
    assert await service.authenticate("static-legacy-key") is not None


async def test_authenticate_disabled_mode_is_anonymous(tmp_path):
    empty_db = UserDB(db_path=tmp_path / "empty.db")
    service = AuthService(userdb=empty_db, static_api_key="")
    principal = await service.authenticate(None)
    assert principal is ANONYMOUS
    # Even a garbage key passes in anonymous mode (server is open anyway)
    principal = await service.authenticate("whatever")
    assert principal is ANONYMOUS
    empty_db.close()


async def test_resolve_caches_verification(service, userdb, monkeypatch):
    _, _, plaintext = await _make_user_key(userdb, "cacheuser")
    calls = 0
    original = userdb.resolve_api_key

    async def counting(raw_key):
        nonlocal calls
        calls += 1
        return await original(raw_key)

    monkeypatch.setattr(userdb, "resolve_api_key", counting)
    first = await service.resolve(plaintext)
    second = await service.resolve(plaintext)
    assert first is not None and second is not None
    assert first == second
    assert calls == 1


async def test_invalidate_key_forces_reverify(service, userdb):
    user, row, plaintext = await _make_user_key(userdb, "revokeme")
    assert await service.resolve(plaintext) is not None
    service.invalidate_key(row["key_id"])
    await userdb.revoke_api_key(row["key_id"])
    assert await service.resolve(plaintext) is None


# ── Session claims / access ──────────────────────────────────────────────────

async def test_claim_session_first_user_wins(service, userdb):
    _, _, alice_key = await _make_user_key(userdb, "alice")
    _, _, bob_key = await _make_user_key(userdb, "bob")
    alice = await service.resolve(alice_key)
    bob = await service.resolve(bob_key)

    assert await service.claim_session("sess-x", alice) is True
    assert await service.claim_session("sess-x", bob) is False
    assert await service.can_access_session("sess-x", alice) is True
    assert await service.can_access_session("sess-x", bob) is False


async def test_claim_session_admin_allowed_but_not_reowned(service, userdb):
    _, _, alice_key = await _make_user_key(userdb, "alice")
    _, _, admin_key = await _make_user_key(userdb, "root", role="admin")
    alice = await service.resolve(alice_key)
    admin = await service.resolve(admin_key)

    assert await service.claim_session("sess-y", alice) is True
    assert await service.claim_session("sess-y", admin) is True  # admin may enter
    owner = await userdb.get_session_owner("sess-y")
    assert owner == alice.user_id  # ownership unchanged


async def test_claim_session_anonymous_is_passthrough(service):
    assert await service.claim_session("sess-z", ANONYMOUS) is True


async def test_can_access_unowned_session_requires_admin(service, userdb):
    _, _, alice_key = await _make_user_key(userdb, "alice")
    _, _, admin_key = await _make_user_key(userdb, "root", role="admin")
    alice = await service.resolve(alice_key)
    admin = await service.resolve(admin_key)

    assert await service.can_access_session("unowned", alice) is False
    assert await service.can_access_session("unowned", admin) is True


async def test_owned_session_ids_passthrough(service, userdb):
    user, _, plaintext = await _make_user_key(userdb, "lister")
    await userdb.set_session_owner("s1", user["user_id"])
    principal = await service.resolve(plaintext)
    assert await service.owned_session_ids(principal.user_id, limit=10) == ["s1"]


# ── Bootstrap ────────────────────────────────────────────────────────────────

async def test_bootstrap_creates_admin_from_env(userdb, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_ADMIN_KEY", "boot-secret-key")
    service = AuthService(userdb=userdb, static_api_key="")
    await service.bootstrap()

    principal = await service.resolve("boot-secret-key")
    assert principal is not None
    assert principal.username == "admin"
    assert principal.role == "admin"


async def test_bootstrap_idempotent(userdb, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_ADMIN_KEY", "boot-secret-key")
    service = AuthService(userdb=userdb, static_api_key="")
    await service.bootstrap()
    await service.bootstrap()

    admin = await userdb.get_user_by_name("admin")
    keys = await userdb.list_api_keys(admin["user_id"])
    assert len(keys) == 1


async def test_bootstrap_noop_without_env(userdb, monkeypatch):
    monkeypatch.delenv("MANUSCLAW_ADMIN_KEY", raising=False)
    service = AuthService(userdb=userdb, static_api_key="")
    await service.bootstrap()
    assert await userdb.list_users() == []


# ── /auth router via TestClient ──────────────────────────────────────────────

def _make_client(monkeypatch, service):
    import app.server.auth_router as auth_router_mod
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.server.auth_router import require_session_access, router

    monkeypatch.setattr(auth_router_mod, "auth_service", service)

    app = FastAPI()
    app.include_router(router)

    @app.get("/probe/{session_id}")
    async def probe(session_id: str, principal=Depends(require_session_access)):
        return {"ok": True, "user": principal.username}

    return TestClient(app)


async def test_router_me_and_admin_endpoints(service, userdb, monkeypatch):
    _, _, admin_key = await _make_user_key(userdb, "admin-user", role="admin")
    client = _make_client(monkeypatch, service)
    headers = {"X-API-Key": admin_key}

    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin-user"
    assert body["role"] == "admin"
    assert body["source"] == "db_key"

    r = client.get("/auth/users", headers=headers)
    assert r.status_code == 200
    assert r.json()["count"] >= 1

    # 401 without / with wrong key
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"X-API-Key": "bad"}).status_code == 401


async def test_router_forbidden_for_non_admin(service, userdb, monkeypatch):
    _, _, user_key = await _make_user_key(userdb, "plainuser")
    client = _make_client(monkeypatch, service)
    headers = {"X-API-Key": user_key}

    assert client.get("/auth/users", headers=headers).status_code == 403
    assert client.post(
        "/auth/users", headers=headers, json={"username": "x"}
    ).status_code == 403


async def test_router_key_lifecycle(service, userdb, monkeypatch):
    _, _, admin_key = await _make_user_key(userdb, "keyadmin", role="admin")
    client = _make_client(monkeypatch, service)
    headers = {"X-API-Key": admin_key}

    r = client.post("/auth/keys", headers=headers, json={"name": "ci"})
    assert r.status_code == 200
    new_key = r.json()["api_key"]
    assert new_key.startswith("mck_")

    # New key works
    r = client.get("/auth/me", headers={"X-API-Key": new_key})
    assert r.status_code == 200
    assert r.json()["username"] == "keyadmin"

    # Listed without secret
    r = client.get("/auth/keys", headers=headers)
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 2
    assert all("api_key" not in k for k in keys)

    # Revoke the new key → immediate rejection
    new_key_id = [k for k in keys if k["name"] == "ci"][0]["key_id"]
    r = client.delete(f"/auth/keys/{new_key_id}", headers=headers)
    assert r.status_code == 200
    assert client.get("/auth/me", headers={"X-API-Key": new_key}).status_code == 401


async def test_router_create_user_returns_initial_key(service, userdb, monkeypatch):
    _, _, admin_key = await _make_user_key(userdb, "creator", role="admin")
    client = _make_client(monkeypatch, service)
    headers = {"X-API-Key": admin_key}

    r = client.post(
        "/auth/users",
        headers=headers,
        json={"username": "newbie", "role": "user"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "newbie"
    assert body["api_key"].startswith("mck_")

    # Newbie can authenticate but is not admin
    r = client.get("/auth/me", headers={"X-API-Key": body["api_key"]})
    assert r.status_code == 200
    assert r.json()["role"] == "user"

    # Duplicate username → 400
    r = client.post(
        "/auth/users", headers=headers, json={"username": "newbie"}
    )
    assert r.status_code == 400


async def test_router_session_access_enforcement(service, userdb, monkeypatch):
    _, _, alice_key = await _make_user_key(userdb, "alice-owner")
    _, _, bob_key = await _make_user_key(userdb, "bob-other")
    _, _, admin_key = await _make_user_key(userdb, "admin-r", role="admin")
    client = _make_client(monkeypatch, service)

    # No ownership yet → non-admin denied (404 hides existence)
    alice_headers = {"X-API-Key": alice_key}
    assert client.get("/probe/sess-a", headers=alice_headers).status_code == 404

    # Alice claims the session, then can read it
    alice = await service.resolve(alice_key)
    assert await service.claim_session("sess-a", alice) is True
    r = client.get("/probe/sess-a", headers=alice_headers)
    assert r.status_code == 200
    assert r.json()["user"] == "alice-owner"

    # Bob denied, admin allowed
    assert client.get("/probe/sess-a", headers={"X-API-Key": bob_key}).status_code == 404
    assert client.get("/probe/sess-a", headers={"X-API-Key": admin_key}).status_code == 200


async def test_router_legacy_static_key(service, monkeypatch):
    client = _make_client(monkeypatch, service)
    r = client.get("/auth/me", headers={"X-API-Key": "static-legacy-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "legacy-admin"
    assert body["source"] == "legacy_static"


async def test_router_anonymous_mode(tmp_path, monkeypatch):
    empty_db = UserDB(db_path=tmp_path / "anon.db")
    service = AuthService(userdb=empty_db, static_api_key="")
    client = _make_client(monkeypatch, service)

    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "anonymous"


def test_get_principal_fallback():
    class FakeState:
        pass

    class FakeRequest:
        state = FakeState()

    assert get_principal(FakeRequest()) is ANONYMOUS


def test_principal_to_dict():
    p = UserPrincipal("u1", "alice", "user", "db_key", "k1")
    d = p.to_dict()
    assert d == {"user_id": "u1", "username": "alice", "role": "user",
                 "source": "db_key", "key_id": "k1"}
