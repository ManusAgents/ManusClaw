"""Tests for webhook system (WebhookManager, HMAC verification, templates)."""

import hashlib
import hmac
import os
import tempfile
import pytest

from app.server.webhooks import WebhookConfig, WebhookManager


@pytest.fixture
def webhook_manager(tmp_path):
    """Create a WebhookManager with a temporary database."""
    db_path = tmp_path / "test_webhooks.db"
    mgr = WebhookManager(db_path=db_path)
    yield mgr
    mgr.close()


# ── WebhookConfig ─────────────────────────────────────────────────────────

def test_webhook_config_to_dict():
    config = WebhookConfig(
        hook_id="hook-1",
        url="https://example.com/webhook",
        prompt_template="Alert: {{payload.message}}",
        hmac_secret="my-secret",
    )
    d = config.to_dict()
    assert d["hook_id"] == "hook-1"
    assert d["url"] == "https://example.com/webhook"
    assert d["hmac_secret_set"] is True
    assert "my-secret" not in d.values()  # Secret never leaked


def test_webhook_config_to_dict_no_secret():
    config = WebhookConfig(hook_id="hook-2")
    d = config.to_dict()
    assert d["hmac_secret_set"] is False


def test_webhook_config_to_db_row():
    config = WebhookConfig(
        hook_id="hook-3",
        url="https://example.com",
        prompt_template="test",
        hmac_secret="secret",
    )
    row = config.to_db_row()
    assert len(row) == 9
    assert row[0] == "hook-3"


# ── Register / Unregister ─────────────────────────────────────────────────

def test_register_webhook(webhook_manager):
    config = WebhookConfig(
        hook_id="test-hook",
        url="https://example.com/hook",
        prompt_template="Hello {{payload.name}}",
        hmac_secret="s3cret",
    )
    result = webhook_manager.register(config)
    assert result.hook_id == "test-hook"
    assert result.created_at > 0

    retrieved = webhook_manager.get("test-hook")
    assert retrieved is not None
    assert retrieved.url == "https://example.com/hook"


def test_unregister_webhook(webhook_manager):
    config = WebhookConfig(hook_id="to-delete")
    webhook_manager.register(config)
    assert webhook_manager.unregister("to-delete") is True
    assert webhook_manager.get("to-delete") is None


def test_unregister_nonexistent(webhook_manager):
    assert webhook_manager.unregister("nope") is False


def test_list_webhooks(webhook_manager):
    for i in range(3):
        webhook_manager.register(WebhookConfig(hook_id=f"hook-{i}"))
    all_hooks = webhook_manager.list_all()
    assert len(all_hooks) == 3


# ── HMAC Verification ──────────────────────────────────────────────────────

def test_verify_hmac_positive(webhook_manager):
    config = WebhookConfig(hook_id="hmac-test", hmac_secret="my-secret-key")
    webhook_manager.register(config)

    payload = b'{"source": "monitor", "message": "CPU high"}'
    signature = hmac.new(
        b"my-secret-key", payload, hashlib.sha256
    ).hexdigest()

    assert webhook_manager.verify_hmac("hmac-test", payload, signature) is True


def test_verify_hmac_negative(webhook_manager):
    config = WebhookConfig(hook_id="hmac-test", hmac_secret="my-secret-key")
    webhook_manager.register(config)

    payload = b'{"source": "monitor", "message": "CPU high"}'
    signature = "bad_signature_value"

    assert webhook_manager.verify_hmac("hmac-test", payload, signature) is False


def test_verify_hmac_no_secret(webhook_manager):
    config = WebhookConfig(hook_id="no-secret-test")
    webhook_manager.register(config)
    # No HMAC configured → always accept
    assert webhook_manager.verify_hmac("no-secret-test", b"data", "anything") is True


def test_verify_hmac_unknown_hook(webhook_manager):
    assert webhook_manager.verify_hmac("nonexistent", b"data", "sig") is False


# ── Trigger ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_unknown_hook(webhook_manager):
    result = await webhook_manager.trigger("nonexistent", {"key": "val"})
    assert result["status"] == "error"
    assert "Unknown webhook" in result["error"]


@pytest.mark.asyncio
async def test_trigger_disabled_hook(webhook_manager):
    config = WebhookConfig(hook_id="disabled-hook", enabled=False)
    webhook_manager.register(config)
    result = await webhook_manager.trigger("disabled-hook", {"key": "val"})
    assert result["status"] == "error"
    assert "disabled" in result["error"]


# ── Prompt template formatting ─────────────────────────────────────────────

def test_format_prompt_simple(webhook_manager):
    config = WebhookConfig(
        hook_id="tpl-test",
        prompt_template="Alert from {{payload.source}}: {{payload.message}}",
    )
    webhook_manager.register(config)
    formatted = webhook_manager._format_prompt(config.prompt_template, {
        "source": "monitor",
        "message": "CPU high",
    })
    assert formatted == "Alert from monitor: CPU high"


def test_format_prompt_missing_field(webhook_manager):
    formatted = webhook_manager._format_prompt("Value: {{payload.unknown.field}}", {"other": 1})
    assert "<unknown.field>" in formatted


def test_format_prompt_empty_template(webhook_manager):
    assert webhook_manager._format_prompt("", {"key": "val"}) == ""


def test_format_prompt_no_placeholders(webhook_manager):
    assert webhook_manager._format_prompt("static text", {"key": "val"}) == "static text"


# ── FastAPI route ordering regression test ────────────────────────────────────
# Regression: POST /webhooks/create was previously matched against
# POST /webhooks/{hook_id} (hook_id="create") and returned 404 because
# no webhook named "create" was registered. The fix re-orders the router
# so literal sub-paths are declared before the parameterised catch-all.

def test_webhook_router_create_endpoint_not_swallowed_by_catchall(tmp_path, monkeypatch):
    """``POST /webhooks/create`` must hit ``create_webhook``, not
    ``trigger_webhook(hook_id="create")``.

    Uses FastAPI's TestClient against the real router so the route-order
    regression is caught at test time, not at runtime in production.
    """
    import app.server.webhook_router as router_mod

    # Isolate webhook DB per test. Patch the router module's bound
    # reference (not the original ``app.server.webhooks.webhook_manager``)
    # because the router imported the singleton by name at module load.
    mgr = WebhookManager(db_path=tmp_path / "router_order.db")
    monkeypatch.setattr(router_mod, "webhook_manager", mgr)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.server.webhook_router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Create — must return 200 with the registered webhook, NOT 404.
    r = client.post("/webhooks/create", json={
        "hook_id": "router-order-hook",
        "prompt_template": "Test {{payload.x}}",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hook_id"] == "router-order-hook"
    assert body["prompt_template"] == "Test {{payload.x}}"

    # List — must include the new hook.
    r = client.get("/webhooks")
    assert r.status_code == 200
    ids = [h["hook_id"] for h in r.json()["webhooks"]]
    assert "router-order-hook" in ids

    # Delete — must succeed.
    r = client.delete("/webhooks/router-order-hook")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted", "hook_id": "router-order-hook"}


def test_webhook_router_trigger_still_works_after_reorder(tmp_path, monkeypatch):
    """The parameterised POST /webhooks/{hook_id} route must still work
    for non-literal hook_ids after the route-order fix.

    NOTE: ``webhook_router`` imports ``webhook_manager`` by name at
    module load, so monkeypatching ``app.server.webhooks.webhook_manager``
    doesn't reach the already-bound reference. We therefore patch the
    bound reference in ``app.server.webhook_router`` directly.
    """
    import app.server.webhook_router as router_mod

    mgr = WebhookManager(db_path=tmp_path / "trigger.db")
    monkeypatch.setattr(router_mod, "webhook_manager", mgr)
    mgr.register(WebhookConfig(
        hook_id="real-hook",
        prompt_template="Hi {{payload.name}}",
    ))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.server.webhook_router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Trigger with a payload that satisfies the template.
    r = client.post("/webhooks/real-hook", json={"name": "world"})
    # The trigger runs the agent (MockLLM in test env), so we expect 200.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["hook_id"] == "real-hook"

