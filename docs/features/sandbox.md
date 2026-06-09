# Sandbox Backends (SSH + OpenShell)

**Status:** ✅ Implemented

## Description
Two new sandbox backends for code isolation: SSH (remote) and OpenShell (Linux namespaces).

## SSH Sandbox
Remote code execution via asyncssh (preferred) or paramiko (fallback).

### Configuration
| Variable | Description |
|---|---|
| `SSH_SANDBOX_HOST` | Remote hostname |
| `SSH_SANDBOX_USER` | SSH username |
| `SSH_SANDBOX_KEY_PATH` | Private key path |
| `SSH_SANDBOX_PORT` | Port (default: `22`) |

## OpenShell Sandbox
Linux namespace isolation via `unshare --net --mount --pid --fork --map-root-user`.

### Requirements
- Linux only
- `unshare` binary (`util-linux` package)
- Kernel user namespace support

## Factory
```python
from app.sandbox.factory import create_sandbox

# Auto-detects from SANDBOX_BACKEND env var
sandbox = create_sandbox()

# Explicit backend
sandbox = create_sandbox(backend="ssh")
sandbox = create_sandbox(backend="openshell")
```

## Install
```bash
pip install manusclaw[ssh]
```
