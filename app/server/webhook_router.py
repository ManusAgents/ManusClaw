"""ManusClaw Webhook Router.

FastAPI router for incoming webhook management endpoints.

Endpoints:
    POST /webhooks/{hook_id}     — Trigger a webhook (called by external services)
    GET  /webhooks                — List all registered webhooks
    POST /webhooks/create         — Create/register a new webhook
    DELETE /webhooks/{hook_id}   — Unregister a webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from app.logger import logger
from app.server.webhooks import WebhookConfig, WebhookManager, webhook_manager

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# ─── Trigger Webhook ──────────────────────────────────────────────────────────

@router.post("/{hook_id}")
async def trigger_webhook(
    hook_id: str,
    request: Request,
    x_hub_signature: Optional[str] = None,
) -> dict[str, Any]:
    """Trigger a registered webhook with the incoming request payload.

    External services should POST to this endpoint with a JSON payload.
    Optionally include an ``X-Hub-Signature-256`` header for HMAC verification.

    Args:
        hook_id: The registered webhook ID.
        request: The FastAPI request object.
        x_hub_signature: HMAC-SHA256 signature (header ``X-Hub-Signature-256``).

    Returns:
        Trigger result with status and optional agent output.
    """
    # Read raw body for HMAC verification
    body_bytes = await request.body()

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Get webhook config
    config = webhook_manager.get(hook_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Webhook '{hook_id}' not found")

    # HMAC verification
    if config.hmac_secret:
        signature = x_hub_signature or request.headers.get("x-hub-signature-256", "")
        if not signature:
            # Strip 'sha256=' prefix if present
            raise HTTPException(
                status_code=401,
                detail="Missing HMAC signature. Set X-Hub-Signature-256 header.",
            )

        # Normalize signature (remove 'sha256=' prefix)
        clean_sig = signature
        if clean_sig.startswith("sha256="):
            clean_sig = clean_sig[len("sha256="):]

        if not webhook_manager.verify_hmac(hook_id, body_bytes, clean_sig):
            raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # Trigger the webhook
    result = await webhook_manager.trigger(hook_id, payload)

    if result.get("status") == "error":
        status_code = 404 if "Unknown" in result.get("error", "") else 500
        raise HTTPException(status_code=status_code, detail=result["error"])

    return result


# ─── List Webhooks ──────────────────────────────────────────────────────────

@router.get("")
async def list_webhooks() -> dict[str, Any]:
    """List all registered webhooks.

    Returns:
        Dictionary with list of webhook configurations (hmac_secret omitted).
    """
    hooks = webhook_manager.list_all()
    return {
        "webhooks": [h.to_dict() for h in hooks],
        "count": len(hooks),
    }


# ─── Create Webhook ────────────────────────────────────────────────────────

class CreateWebhookRequest:
    """Pydantic-like request body for webhook creation (using dict for simplicity)."""

    def __init__(
        self,
        hook_id: str = "",
        url: str = "",
        prompt_template: str = "",
        hmac_secret: str = "",
        target_session: str = "",
        enabled: bool = True,
    ) -> None:
        self.hook_id = hook_id
        self.url = url
        self.prompt_template = prompt_template
        self.hmac_secret = hmac_secret
        self.target_session = target_session
        self.enabled = enabled


@router.post("/create")
async def create_webhook(request: Request) -> dict[str, Any]:
    """Create and register a new webhook.

    Request body (JSON):
        hook_id:         Unique webhook identifier (auto-generated if omitted)
        url:              Informational URL for the webhook source
        prompt_template:  Template for the agent prompt (``{{payload.field}}`` syntax)
        hmac_secret:      Shared secret for HMAC-SHA256 verification (empty to disable)
        target_session:   Session ID to route agent prompts to (optional)
        enabled:          Whether the webhook is active (default: true)

    Returns:
        The created webhook configuration.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    config = WebhookConfig(
        hook_id=body.get("hook_id", ""),
        url=body.get("url", ""),
        prompt_template=body.get("prompt_template", ""),
        hmac_secret=body.get("hmac_secret", ""),
        target_session=body.get("target_session", ""),
        enabled=body.get("enabled", True),
    )

    if not config.prompt_template:
        raise HTTPException(
            status_code=400,
            detail="prompt_template is required",
        )

    registered = webhook_manager.register(config)

    logger.info(f"[Webhooks] Created webhook: {registered.hook_id}")
    return registered.to_dict()


# ─── Delete Webhook ────────────────────────────────────────────────────────

@router.delete("/{hook_id}")
async def delete_webhook(hook_id: str) -> dict[str, str]:
    """Unregister a webhook by ID.

    Args:
        hook_id: The webhook ID to remove.

    Returns:
        Status message.
    """
    removed = webhook_manager.unregister(hook_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Webhook '{hook_id}' not found")

    logger.info(f"[Webhooks] Deleted webhook: {hook_id}")
    return {"status": "deleted", "hook_id": hook_id}


# ─── Generate HMAC Helper ─────────────────────────────────────────────────

@router.get("/sign/{hook_id}")
async def sign_payload(hook_id: str, payload: str = "") -> dict[str, str]:
    """Generate an HMAC-SHA256 signature for testing webhook verification.

    This is a utility endpoint for development and testing.

    Args:
        hook_id: The webhook ID to generate a signature for.
        payload: The JSON payload string to sign.

    Returns:
        The HMAC signature.
    """
    config = webhook_manager.get(hook_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Webhook '{hook_id}' not found")

    if not config.hmac_secret:
        raise HTTPException(
            status_code=400,
            detail=f"Webhook '{hook_id}' has no HMAC secret configured",
        )

    payload_bytes = payload.encode("utf-8")
    signature = hmac.new(
        config.hmac_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return {
        "hook_id": hook_id,
        "signature": f"sha256={signature}",
        "header_name": "X-Hub-Signature-256",
    }
