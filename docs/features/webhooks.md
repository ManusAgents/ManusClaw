# Webhook System

**Status:** ✅ Implemented

## Description
Incoming webhook management with HMAC-SHA256 verification and template-based prompt formatting.

## Configuration
Webhooks configured via API or `manusclaw-webhook` CLI. No env vars needed.

## Features
- HMAC-SHA256 signature verification (per-hook secret)
- Template variables: `{{payload.field}}` and `{{payload.nested.field}}`
- SQLite persistence
- Trigger count tracking
- Enable/disable per hook

## Entry Point
```bash
manusclaw-webhook
```

## Architecture
`WebhookManager` → SQLite → Agent trigger. Each trigger creates a `Manus()` agent with the formatted prompt.
