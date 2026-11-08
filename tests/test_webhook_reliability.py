"""Tests for webhook reliability: delivery dedup, session affinity, locks."""

import pytest

from app.server.webhook_reliability import WebhookReliability
from app.server.webhooks import WebhookConfig, WebhookManager


@pytest.fixture
def reliability(tmp_path):
    r = WebhookReliability(db_path=tmp_path / "reliability.db")
    yield r
    r.close()


# ── Delivery idempotency ─────────────────────────────────────────────────────

async def test_claim_delivery_first_wins(reliability):
    assert await reliability.claim_delivery("hook", "d1") is True
    assert await reliability.claim_delivery("hook", "d1") is False


async def test_claim_delivery_scoped_per_hook(reliability):
    assert await reliability.claim_delivery("hook-a", "d1") is True
    assert await reliability.claim_delivery("hook-b", "d1") is True


async def test_claim_delivery_empty_id_is_passthrough(reliability):
    # No delivery ID → no dedup (always treated as new)
    assert await reliability.claim_delivery("hook", "") is True
    assert await reliability.claim_delivery("hook", "") is True


async def test_delivery_seen(reliability):
    assert await reliability.delivery_seen("hook", "d9") is False
    await reliability.claim_delivery("hook", "d9")
    assert await reliability.delivery_seen("hook", "d9") is True


# ── Session affinity ─────────────────────────────────────────────────────────

async def test_resolve_session_target_wins(reliability):
    session_id = await reliability.resolve_session_id("hook", "custom-session")
    assert session_id == "custom-session"
    # Persisted and reused
    again = await reliability.resolve_session_id("hook", "")
    assert again == "custom-session"


async def test_resolve_session_stable_default(reliability):
    first = await reliability.resolve_session_id("hook", "")
    second = await reliability.resolve_session_id("hook", "")
    assert first == "webhook-hook"
    assert first == second


async def test_remember_session_overrides(reliability):
    await reliability.remember_session("hook", "pinned-session")
    assert await reliability.resolve_session_id("hook", "") == "pinned-session"


# ── Locks ────────────────────────────────────────────────────────────────────

def test_lock_for_identity_per_hook(reliability):
    lock_a1 = reliability.lock_for("hook-a")
    lock_a2 = reliability.lock_for("hook-a")
    lock_b = reliability.lock_for("hook-b")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


# ── WebhookManager integration ───────────────────────────────────────────────

class FakeAgent:
    """Captures the session_id each construction got."""

    sessions: list = []

    def __init__(self, session_id=None, **kwargs):
        FakeAgent.sessions.append(session_id)

    async def run(self, prompt):
        return "done"


@pytest.fixture(autouse=True)
def reset_fake_agent():
    FakeAgent.sessions = []
    yield
    FakeAgent.sessions = []


@pytest.mark.asyncio
async def test_manager_trigger_dedup_and_affinity(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.manus.Manus", FakeAgent)
    mgr = WebhookManager(db_path=tmp_path / "w.db")
    mgr.register(WebhookConfig(hook_id="aff", prompt_template="Hi {{payload.x}}"))

    r1 = await mgr.trigger("aff", {"x": 1}, delivery_id="d1")
    assert r1["status"] == "ok"
    assert r1["session_id"] == "webhook-aff"

    # Same delivery ID → duplicate ignored, agent NOT run again
    r2 = await mgr.trigger("aff", {"x": 1}, delivery_id="d1")
    assert r2["status"] == "duplicate_ignored"
    assert FakeAgent.sessions == ["webhook-aff"]

    # Different delivery → runs again, SAME session (affinity)
    r3 = await mgr.trigger("aff", {"x": 2}, delivery_id="d2")
    assert r3["status"] == "ok"
    assert r3["session_id"] == "webhook-aff"
    assert FakeAgent.sessions == ["webhook-aff", "webhook-aff"]
    mgr.close()


@pytest.mark.asyncio
async def test_manager_trigger_target_session_respected(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.manus.Manus", FakeAgent)
    mgr = WebhookManager(db_path=tmp_path / "w2.db")
    mgr.register(WebhookConfig(
        hook_id="pinned",
        prompt_template="Go",
        target_session="explicit-session",
    ))

    r = await mgr.trigger("pinned", {}, delivery_id="d1")
    assert r["status"] == "ok"
    assert r["session_id"] == "explicit-session"
    assert FakeAgent.sessions == ["explicit-session"]
    mgr.close()


@pytest.mark.asyncio
async def test_manager_trigger_without_delivery_id_no_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.manus.Manus", FakeAgent)
    mgr = WebhookManager(db_path=tmp_path / "w3.db")
    mgr.register(WebhookConfig(hook_id="plain", prompt_template="Go"))

    r1 = await mgr.trigger("plain", {})
    r2 = await mgr.trigger("plain", {})
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert FakeAgent.sessions == ["webhook-plain", "webhook-plain"]
    mgr.close()


# ── Router-level duplicate handling ──────────────────────────────────────────

def test_router_duplicate_delivery_returns_200(tmp_path, monkeypatch):
    import app.server.webhook_router as router_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.server.webhook_router import router

    monkeypatch.setattr("app.agent.manus.Manus", FakeAgent)
    mgr = WebhookManager(db_path=tmp_path / "router_dup.db")
    monkeypatch.setattr(router_mod, "webhook_manager", mgr)
    mgr.register(WebhookConfig(hook_id="dup-hook", prompt_template="Hello {{payload.a}}"))

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    r1 = client.post("/webhooks/dup-hook", json={"a": 1})
    assert r1.status_code == 200
    assert r1.json()["status"] == "ok"

    # Same body → same hash-derived delivery ID → duplicate, still 200
    r2 = client.post("/webhooks/dup-hook", json={"a": 1})
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"

    # Explicit delivery header overrides body hash
    r3 = client.post(
        "/webhooks/dup-hook", json={"a": 2}, headers={"X-Delivery-ID": "explicit-1"}
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "ok"
    assert FakeAgent.sessions == ["webhook-dup-hook", "webhook-dup-hook"]
