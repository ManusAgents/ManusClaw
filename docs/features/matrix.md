# Matrix Protocol Adapter

**Status:** ✅ Implemented

## Description
Matrix protocol adapter via Homeserver REST API with long-poll sync loop.

## Configuration
| Variable | Description |
|---|---|
| `MATRIX_HOMESERVER` | Homeserver URL (e.g., `https://matrix.org`) |
| `MATRIX_ACCESS_TOKEN` | Bearer token |
| `MATRIX_USER_ID` | Full MXID (e.g., `@bot:matrix.org`) |

## Architecture
Uses `/sync` long-poll. Tracks `next_batch` token. Filters out own messages. Falls back to REST if `matrix-nio` unavailable.

## Install
```
pip install manusclaw[matrix]
```
