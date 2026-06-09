# WhatsApp Business Cloud

**Status:** ✅ Implemented

## Description
WhatsApp Business Cloud API adapter for sending and receiving messages via the Facebook Graph API.

## Configuration
| Variable | Description |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Bearer token for the Cloud API |
| `WHATSAPP_BUSINESS_PHONE_ID` | Business phone number ID |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | Webhook verification token |

## Usage
The adapter auto-registers with `MessagingGateway`. Webhook endpoint: `POST /webhooks/whatsapp`.

## Architecture
Webhook-driven (no polling). Inbound messages parsed from webhook payloads. Outbound via `POST /v18.0/{phone_id}/messages`.
