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
