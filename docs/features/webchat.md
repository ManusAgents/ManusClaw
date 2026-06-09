# WebChat Adapter

**Status:** ✅ Implemented

## Description
Internal WebSocket-based chat adapter for the built-in web UI. Always available — no external service required.

## Configuration
None required. Automatically configured.

## Usage
Register/unregister WebSocket clients. Supports per-client messaging and broadcast.

## Architecture
Uses `asyncio.Queue` per client. Messages pushed by the web server flow through `receive_from_client()` → handler → `send()`.
