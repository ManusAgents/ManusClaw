"""Tests for CredentialPool rotation and exhaustion."""
import pytest
import asyncio
from app.llm.credential_pool import CredentialPool, Credential


@pytest.mark.asyncio
async def test_pool_returns_available_cred():
    pool = CredentialPool([{"api_key": "key1"}, {"api_key": "key2"}])
    cred = await pool.get()
    assert cred is not None
    assert cred.api_key in ("key1", "key2")


@pytest.mark.asyncio
async def test_pool_skips_exhausted():
    pool = CredentialPool([{"api_key": "key1", "priority": 0}, {"api_key": "key2", "priority": 1}])
    cred = await pool.get()
    assert cred.api_key == "key1"
    await pool.mark_exhausted(cred)  # exhaust key1
    next_cred = await pool.get()
    assert next_cred is not None
    assert next_cred.api_key == "key2"


@pytest.mark.asyncio
async def test_pool_all_exhausted_returns_none():
    pool = CredentialPool([{"api_key": "key1"}])
    cred = await pool.get()
    await pool.mark_exhausted(cred)
    no_cred = await pool.get()
    assert no_cred is None


@pytest.mark.asyncio
async def test_pool_success_resets_exhaustion():
    pool = CredentialPool([{"api_key": "key1"}])
    cred = await pool.get()
    await pool.mark_exhausted(cred)
    await pool.mark_success(cred)
    recovered = await pool.get()
    assert recovered is not None


def test_pool_available_count():
    pool = CredentialPool([{"api_key": "k1"}, {"api_key": "k2"}, {"api_key": "k3"}])
    assert pool.available_count == 3
    assert pool.size == 3
