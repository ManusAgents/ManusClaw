# Gmail Integration

**Status:** ✅ Implemented

## Description
Gmail read/send via Google API with OAuth2 authentication flow.

## Configuration
| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_GMAIL_TOKEN_PATH` | Path to credentials JSON |

## Install
```
pip install manusclaw[gmail]
```

## Architecture
Uses `google-api-python-client` with `google-auth-httplib2` for HTTP transport. Stores OAuth tokens locally.
