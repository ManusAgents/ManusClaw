"""Tests for the users database (UserDB): accounts, API keys, ownership."""

import pytest

from app.db.users import UserDB, generate_api_key, hash_key, verify_key


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    """Keep PBKDF2 cheap in tests (iteration count is read at call time)."""
    monkeypatch.setenv("MANUSCLAW_PBKDF2_ITERATIONS", "1000")


@pytest.fixture
def userdb(tmp_path):
    db = UserDB(db_path=tmp_path / "users.db")
    yield db
    db.close()


# ── Key hashing ──────────────────────────────────────────────────────────────

def test_generate_api_key_format():
    key = generate_api_key()
    assert key.startswith("mck_")
    assert len(key) > 20


def test_hash_key_verify_roundtrip():
    stored = hash_key("mck_secret_value")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_key("mck_secret_value", stored) is True
    assert verify_key("mck_wrong_value", stored) is False


def test_hash_key_salt_unique():
    assert hash_key("same") != hash_key("same")


def test_verify_key_malformed_stored_hash():
    assert verify_key("anything", "not-a-valid-hash") is False


# ── Users ────────────────────────────────────────────────────────────────────

async def test_create_user(userdb):
    user = await userdb.create_user("alice", role="admin")
    assert user["username"] == "alice"
    assert user["role"] == "admin"

    fetched = await userdb.get_user(user["user_id"])
    assert fetched is not None
    assert fetched["username"] == "alice"


async def test_create_user_duplicate_raises(userdb):
    await userdb.create_user("bob")
    with pytest.raises(ValueError):
        await userdb.create_user("bob")


async def test_create_user_invalid_role(userdb):
    with pytest.raises(ValueError):
        await userdb.create_user("carol", role="superuser")


async def test_list_users_with_key_counts(userdb):
    user = await userdb.create_user("dave")
    await userdb.create_api_key(user["user_id"], name="k1")
    users = await userdb.list_users()
    assert len(users) == 1
    assert users[0]["active_keys"] == 1


async def test_set_user_disabled(userdb):
    user = await userdb.create_user("erin")
    assert await userdb.set_user_disabled(user["user_id"], True) is True
    fetched = await userdb.get_user(user["user_id"])
    assert fetched["disabled"] is True


# ── API keys ─────────────────────────────────────────────────────────────────

async def test_create_and_resolve_api_key(userdb):
    user = await userdb.create_user("frank")
    row, plaintext = await userdb.create_api_key(user["user_id"], name="test")
    assert plaintext.startswith("mck_")

    resolved = await userdb.resolve_api_key(plaintext)
    assert resolved is not None
    assert resolved["user_id"] == user["user_id"]
    assert resolved["username"] == "frank"
    assert resolved["role"] == "user"
    assert resolved["key_id"] == row["key_id"]


async def test_resolve_wrong_key_returns_none(userdb):
    user = await userdb.create_user("grace")
    _, plaintext = await userdb.create_api_key(user["user_id"])
    assert await userdb.resolve_api_key(plaintext) is not None
    assert await userdb.resolve_api_key("mck_totally_wrong_key_value") is None
    assert await userdb.resolve_api_key("") is None


async def test_revoked_key_not_resolved(userdb):
    user = await userdb.create_user("heidi")
    row, plaintext = await userdb.create_api_key(user["user_id"])
    assert await userdb.revoke_api_key(row["key_id"]) is True
    assert await userdb.resolve_api_key(plaintext) is None


async def test_disabled_user_key_not_resolved(userdb):
    user = await userdb.create_user("ivan")
    _, plaintext = await userdb.create_api_key(user["user_id"])
    await userdb.set_user_disabled(user["user_id"], True)
    assert await userdb.resolve_api_key(plaintext) is None


async def test_revoke_scoped_to_owner(userdb):
    alice = await userdb.create_user("alice2")
    bob = await userdb.create_user("bob2")
    row_a, _ = await userdb.create_api_key(alice["user_id"])
    _, plaintext_b = await userdb.create_api_key(bob["user_id"])

    # Bob tries to revoke Alice's key scoped by user_id → no effect
    assert await userdb.revoke_api_key(row_a["key_id"], user_id=bob["user_id"]) is False
    assert await userdb.resolve_api_key(plaintext_b) is not None


async def test_user_has_raw_key(userdb):
    user = await userdb.create_user("judy")
    assert await userdb.user_has_raw_key(user["user_id"], "boot-key") is False
    await userdb.create_api_key(user["user_id"], raw_key="boot-key")
    assert await userdb.user_has_raw_key(user["user_id"], "boot-key") is True
    assert await userdb.user_has_raw_key(user["user_id"], "other-key") is False


async def test_has_any_active_keys(userdb):
    assert await userdb.has_any_active_keys() is False
    user = await userdb.create_user("kate")
    row, plaintext = await userdb.create_api_key(user["user_id"])
    assert await userdb.has_any_active_keys() is True
    await userdb.revoke_api_key(row["key_id"])
    assert await userdb.has_any_active_keys() is False


async def test_create_api_key_unknown_user_raises(userdb):
    with pytest.raises(ValueError):
        await userdb.create_api_key("nonexistent-user")


# ── Session ownership ────────────────────────────────────────────────────────

async def test_session_owner_first_claim_wins(userdb):
    alice = await userdb.create_user("alice3")
    bob = await userdb.create_user("bob3")
    await userdb.set_session_owner("sess-1", alice["user_id"])
    await userdb.set_session_owner("sess-1", bob["user_id"])  # ignored
    assert await userdb.get_session_owner("sess-1") == alice["user_id"]


async def test_owned_session_ids_newest_first(userdb):
    user = await userdb.create_user("leo")
    await userdb.set_session_owner("s-old", user["user_id"])
    await userdb.set_session_owner("s-new", user["user_id"])
    owned = await userdb.owned_session_ids(user["user_id"], limit=10)
    assert owned[0] == "s-new"
    limited = await userdb.owned_session_ids(user["user_id"], limit=1)
    assert limited == ["s-new"]


async def test_get_session_owner_none_for_unknown(userdb):
    assert await userdb.get_session_owner("nope") is None
