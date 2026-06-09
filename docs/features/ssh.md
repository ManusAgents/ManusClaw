# SSH Remote Access Server

**Status:** ✅ Implemented

## Description
SSH server for remote gateway control with public key auth and restricted shell.

## Configuration
| Variable | Description |
|---|---|
| `MANUSCLAW_SSH_ENABLED` | Enable (default: `false`) |
| `MANUSCLAW_SSH_PORT` | Port (default: `2222`) |
| `MANUSCLAW_SSH_HOST` | Bind address (default: `0.0.0.0`) |
| `MANUSCLAW_SSH_HOST_KEY` | Host key path |
| `MANUSCLAW_SSH_AUTH_KEYS` | Authorized keys path |

## Commands
`status`, `restart`, `logs [N]`, `agent --message MSG`, `channels list`, `cron list`, `help`, `exit`

## Security
- Public key authentication only (no passwords)
- Whitelisted commands only — shell metacharacters rejected
- Command arguments validated per-command

## Architecture
Uses `asyncssh` SSHServer callbacks. Restricted shell validates and dispatches. Stub mode if `asyncssh` not installed.

## Install
```
pip install manusclaw[ssh]
```
