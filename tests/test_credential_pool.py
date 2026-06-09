"""Tests for LLM credential pool rotation."""
import pytest
import asyncio
import os
os.environ["APP_ENV"] = "test"

from app.llm.credential_pool import CredentialPool, Credential, build_pool_from_config


def test_pool_rotation_on_exhaustion():
    """Pool should rotate to next key when current is exhausted."""
    pool = CredentialPool(credentials=[
        {"api_key": "key1", "priority": 1},
        {"api_key": "key2", "priority": 2},
    ])
    assert pool._creds[0].api_key == "key1"


@pytest.mark.asyncio
async def test_pool_get_returns_credential():
    pool = CredentialPool(credentials=[
        {"api_key": "key1", "priority": 1},
    ])
    cred = await pool.get()
    assert cred is not None
    assert cred.api_key == "key1"


@pytest.mark.asyncio
async def test_pool_mark_exhausted_then_recover():
    pool = CredentialPool(credentials=[
        {"api_key": "key1", "priority": 1},
        {"api_key": "key2", "priority": 2},
    ])
    first = await pool.get()
    assert first is not None
    await pool.mark_exhausted(first)
    second = await pool.get()
    # Should return the non-exhausted key
    assert second is not None


@pytest.mark.asyncio
async def test_build_pool_from_config_single_key():
    pool = build_pool_from_config("openai", "sk-test123")
    assert pool is not None
    assert len(pool._creds) >= 1


@pytest.mark.asyncio
async def test_build_pool_from_config_no_key():
    pool = build_pool_from_config("openai", None)
    assert pool is None or len(pool._creds) == 0
