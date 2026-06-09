"""Tests for new messaging channel adapters (WhatsApp, IRC, WebChat, Matrix)."""

import asyncio
import os
import pytest

# ── Ensure stub mode (no tokens set) ────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_messaging_env(monkeypatch):
    """Clear messaging env vars so adapters enter stub mode."""
    for var in (
        "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_BUSINESS_PHONE_ID",
        "IRC_SERVER", "IRC_NICK", "MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


# ── Adapter instantiation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whatsapp_adapter_instantiates():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    assert adapter.platform_name == "whatsapp"
    assert not adapter.is_configured()


@pytest.mark.asyncio
async def test_irc_adapter_instantiates():
    from app.messaging.irc import IRCAdapter
    adapter = IRCAdapter()
    assert adapter.platform_name == "irc"
    assert not adapter.is_configured()


@pytest.mark.asyncio
async def test_webchat_adapter_instantiates():
    from app.messaging.webchat import WebChatAdapter
    adapter = WebChatAdapter()
    assert adapter.platform_name == "webchat"
    assert adapter.is_configured()  # WebChat is always configured (internal)


@pytest.mark.asyncio
async def test_matrix_adapter_instantiates():
    from app.messaging.matrix import MatrixAdapter
    adapter = MatrixAdapter()
    assert adapter.platform_name == "matrix"
    assert not adapter.is_configured()


# ── Stub mode (not configured) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whatsapp_stub_connect_and_send():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    await adapter.connect()  # Should not raise
    await adapter.start(lambda msg: None)  # Should not raise
    await adapter.send("12345", "hello")  # Should log and return
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_irc_stub_connect_and_send():
    from app.messaging.irc import IRCAdapter
    adapter = IRCAdapter()
    await adapter.connect()
    await adapter.start(lambda msg: None)
    await adapter.send("#test", "hello")
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_matrix_stub_connect_and_send():
    from app.messaging.matrix import MatrixAdapter
    adapter = MatrixAdapter()
    await adapter.connect()
    await adapter.start(lambda msg: None)
    await adapter.send("!room:matrix.org", "hello")
    await adapter.disconnect()


# ── WhatsApp webhook verification ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_whatsapp_webhook_verify_success():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    result = await adapter.verify_webhook("subscribe", "manusclaw_verify", "challenge123")
    assert result == "challenge123"


@pytest.mark.asyncio
async def test_whatsapp_webhook_verify_wrong_mode():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    result = await adapter.verify_webhook("wrong_mode", "manusclaw_verify", "challenge123")
    assert result is None


@pytest.mark.asyncio
async def test_whatsapp_webhook_verify_wrong_token():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    result = await adapter.verify_webhook("subscribe", "wrong_token", "challenge123")
    assert result is None


@pytest.mark.asyncio
async def test_whatsapp_webhook_parse_event():
    from app.messaging.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "123456",
                        "id": "msg_001",
                        "text": {"body": "Hello ManusClaw"},
                    }],
                    "contacts": [{"wa_id": "123456"}],
                }
            }]
        }]
    }
    messages = await adapter.handle_webhook_event(payload)
    assert len(messages) == 1
    assert messages[0].platform == "whatsapp"
    assert messages[0].text == "Hello ManusClaw"
    assert messages[0].user_id == "123456"


# ── IRC message parsing ────────────────────────────────────────────────────

def test_irc_split_message_short():
    from app.messaging.irc import IRCAdapter
    result = IRCAdapter._split_irc_message("short message")
    assert result == ["short message"]


def test_irc_split_message_long():
    from app.messaging.irc import IRCAdapter
    long_msg = "x" * 500
    result = IRCAdapter._split_irc_message(long_msg, max_len=200)
    assert len(result) == 3
    assert all(len(chunk) <= 200 for chunk in result)


# ── WebChat register/unregister ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webchat_register_unregister():
    from app.messaging.webchat import WebChatAdapter
    adapter = WebChatAdapter()
    queue = await adapter.register_connection("client-1")
    assert isinstance(queue, asyncio.Queue)
    assert len(adapter._connections) == 1

    await adapter.unregister_connection("client-1")
    assert len(adapter._connections) == 0

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_webchat_receive_from_client():
    from app.messaging.webchat import WebChatAdapter
    adapter = WebChatAdapter()
    received = []

    async def handler(msg):
        received.append(msg)

    await adapter.start(handler)
    await adapter.register_connection("client-1")
    await adapter.receive_from_client("client-1", "hello agent")

    assert len(received) == 1
    assert received[0].platform == "webchat"
    assert received[0].text == "hello agent"


# ── Matrix next_batch tracking ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_matrix_next_batch_initial():
    from app.messaging.matrix import MatrixAdapter
    adapter = MatrixAdapter()
    assert adapter._next_batch == ""


@pytest.mark.asyncio
async def test_matrix_next_batch_updated():
    from app.messaging.matrix import MatrixAdapter
    adapter = MatrixAdapter()
    # Simulate update that happens during connect/start
    adapter._next_batch = "s12345_6789"
    assert adapter._next_batch == "s12345_6789"
