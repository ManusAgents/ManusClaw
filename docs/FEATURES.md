# ManusClaw v5.0 — Feature Overview

ManusClaw v5.0 introduces **14 new features** spanning voice interaction, messaging expansion, code sandboxing, multi-agent routing, model failover, and more. Every feature follows a **stub-first pattern**: it degrades gracefully when optional dependencies or configuration are missing.

---

## Table of Contents

1. [WhatsApp Business Cloud](#1-whatsapp-business-cloud)
2. [IRC Adapter](#2-irc-adapter)
3. [WebChat Adapter](#3-webchat-adapter)
4. [Matrix Protocol Adapter](#4-matrix-protocol-adapter)
5. [Voice Input/Output System](#5-voice-inputoutput-system)
6. [SSH Remote Access Server](#6-ssh-remote-access-server)
7. [Gmail Integration](#7-gmail-integration)
8. [Live Canvas (A2UI)](#8-live-canvas-a2ui)
9. [Webhook System](#9-webhook-system)
10. [Sandbox Backends (SSH + OpenShell)](#10-sandbox-backends-ssh--openshell)
11. [Multi-Agent Router](#11-multi-agent-router)
12. [Model Failover / Profile Rotation](#12-model-failover--profile-rotation)
13. [Canvas Nodes (Mobile/Desktop)](#13-canvas-nodes-mobiledesktop)
14. [Companion Desktop Apps](#14-companion-desktop-apps)

---

## 1. WhatsApp Business Cloud

**Status:** ✅ Implemented

**Description:** WhatsApp Business Cloud API adapter for sending and receiving messages via WhatsApp webhook endpoints.

**Configuration:**
| Environment Variable | Description |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Bearer token for the WhatsApp Business Cloud API |
| `WHATSAPP_BUSINESS_PHONE_ID` | Phone number ID of the business account |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | Token for webhook verification (default: `manusclaw_verify`) |

**Usage:** Configure env vars, then the adapter auto-starts with the MessagingGateway. Webhook URL: `POST /webhooks/whatsapp`.

**Architecture:** Uses webhook-driven inbound messaging (no polling) via the Facebook Graph API v18.0.

---

## 2. IRC Adapter

**Status:** ✅ Implemented

**Description:** Pure async IRC client using asyncio TCP streams. Connects to IRC servers, joins channels, and relays PRIVMSG events.

**Configuration:**
| Environment Variable | Description |
|---|---|
| `IRC_SERVER` | IRC server hostname (e.g., `irc.libera.chat`) |
| `IRC_PORT` | Port number (default: `6667`) |
| `IRC_NICK` | Bot nickname |
| `IRC_CHANNELS` | Comma-separated channels (e.g., `#general,#dev`) |
| `IRC_PASS` | Optional NickServ password |

**Architecture:** Direct TCP connection with automatic PING/PONG keepalive. Messages are split to 512-byte IRC limits.

---

## 3. WebChat Adapter

**Status:** ✅ Implemented

**Description:** Internal WebSocket-based chat for the built-in web UI. Always configured — no external service needed.

**Configuration:** None required (internal).

**Usage:** The web server routes WebSocket messages through this adapter. Supports register/unregister per client, broadcast, and per-client message routing.

---

## 4. Matrix Protocol Adapter

**Status:** ✅ Implemented

**Description:** Matrix protocol adapter via Homeserver REST API with long-poll sync.

**Configuration:**
| Environment Variable | Description |
|---|---|
| `MATRIX_HOMESERVER` | Homeserver URL (e.g., `https://matrix.org`) |
| `MATRIX_ACCESS_TOKEN` | Bearer token after login |
| `MATRIX_USER_ID` | Full MXID (e.g., `@bot:matrix.org`) |

**Architecture:** Uses `/sync` long-poll with `next_batch` token tracking. Falls back to basic REST if `matrix-nio` is not installed.

---

## 5. Voice Input/Output System

**Status:** ✅ Implemented

**Description:** Complete voice pipeline with wake-word detection, STT, agent interaction, and TTS playback.

**Components:**
- **TTS Providers:** `NullTTS` (stub), `SystemTTS` (pyttsx3), `OpenAITTS` (OpenAI API), `ElevenLabsTTS` (ElevenLabs API)
- **Wake Word:** `VoiceWakeDetector` with porcupine (hw-accelerated), speech_recognition fallback, or stub mode
- **Talk Mode:** Continuous conversation loop (listen → transcribe → agent → speak)

**Configuration:**
| Environment Variable | Description |
|---|---|
| `PICOVOICE_API_KEY` | Picovoice Porcupine wake-word API key |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS API key |
| `OPENAI_API_KEY` | OpenAI TTS API key (also used for LLM) |

**Install:** `pip install manusclaw[voice]`

---

## 6. SSH Remote Access Server

**Status:** ✅ Implemented

**Description:** SSH server for remote gateway control using a restricted shell with whitelisted commands.

**Configuration:**
| Environment Variable | Description |
|---|---|
| `MANUSCLAW_SSH_ENABLED` | Enable SSH server (default: `false`) |
| `MANUSCLAW_SSH_PORT` | SSH port (default: `2222`) |
| `MANUSCLAW_SSH_HOST` | Bind address (default: `0.0.0.0`) |
| `MANUSCLAW_SSH_HOST_KEY` | Path to host key |
| `MANUSCLAW_SSH_AUTH_KEYS` | Path to authorized_keys |

**Commands:** `status`, `restart`, `logs`, `agent --message`, `channels list`, `cron list`, `help`, `exit`

**Install:** `pip install manusclaw[ssh]`

---

## 7. Gmail Integration

**Status:** ✅ Implemented

**Description:** Gmail read/send via Google API with support for automated email workflows.

**Configuration:**
| Environment Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_GMAIL_TOKEN_PATH` | Path to stored credentials JSON |

**Install:** `pip install manusclaw[gmail]`

---

## 8. Live Canvas (A2UI)

**Status:** ✅ Implemented

**Description:** Real-time UI rendering via WebSocket using the Agent-to-UI (A2UI) protocol. Supports text, charts, tables, images, buttons, markdown, and containers.

**Architecture:**
- **A2UI Protocol** (`app/canvas/a2ui.py`): Dataclass definitions for components, events, updates
- **CanvasServer** (`app/canvas/server.py`): WebSocket endpoint at `/ws/canvas/{session_id}`
- **CanvasTool** (`app/canvas/tool.py`): Agent-callable tool for rendering components

**Component Types:** `text`, `chart`, `image`, `button`, `table`, `container`, `markdown`

**Install:** Requires `server` extra (`fastapi` + `uvicorn`).

---

## 9. Webhook System

**Status:** ✅ Implemented

**Description:** Incoming webhook management with HMAC-SHA256 verification and prompt template formatting.

**Architecture:**
- Webhook configurations persisted in SQLite
- Template variables: `{{payload.field}}`
- Entry point: `manusclaw-webhook`

---

## 10. Sandbox Backends (SSH + OpenShell)

**Status:** ✅ Implemented

**Description:** Two new sandbox backends for remote and local code isolation.

**SSH Sandbox:**
| Environment Variable | Description |
|---|---|
| `SSH_SANDBOX_HOST` | Remote hostname |
| `SSH_SANDBOX_USER` | SSH username |
| `SSH_SANDBOX_KEY_PATH` | Path to private key |
| `SSH_SANDBOX_PORT` | Port (default: `22`) |

**OpenShell Sandbox:** Linux-only namespace isolation via `unshare --net --mount --pid --fork`. No configuration needed — uses local `unshare` command.

**Factory:** `create_sandbox(backend="ssh"|"docker"|"openshell")` auto-detects from `SANDBOX_BACKEND` env var.

---

## 11. Multi-Agent Router

**Status:** ✅ Implemented

**Description:** Per-channel multi-agent routing with configurable route rules and LRU-cached agent instances.

**Configuration:** Add to `config.yaml` under `agents` section:

```yaml
agents:
  definitions:
    coder:
      class_path: app.agent.manus.Manus
      system_prompt: "You are a coding specialist..."
      tools: [python_execute, str_replace_editor, bash]
  routes:
    - channel: discord
      agent_name: coder
    - channel: telegram
      user_id: "admin_.*"
      agent_name: manus
```

**Architecture:** RouteRule pattern matching (exact + regex), AgentRegistry with LRU eviction and idle TTL, AgentConfig for per-agent customization.

---

## 12. Model Failover / Profile Rotation

**Status:** ✅ Implemented

**Description:** Cross-provider model failover with configurable priority ordering and cooldown for failed models.

**Features:**
- Priority-ordered model entries (OpenAI → Anthropic → Ollama)
- Automatic cooldown on failure (configurable per model)
- Per-session profile selection
- Success/failure statistics tracking

**Usage:**
```python
from app.llm.profile_rotation import ModelProfile, ProfileRotator

profile = ModelProfile.from_config([
    {"provider": "openai", "model": "gpt-4o", "priority": 0},
    {"provider": "anthropic", "model": "claude-sonnet-4", "priority": 1},
])

rotator = ProfileRotator(profile)
entry = await rotator.get_next()
```

---

## 13. Canvas Nodes (Mobile/Desktop)

**Status:** ✅ Implemented

**Description:** WebSocket protocol for connecting mobile and desktop devices as Live Canvas viewers.

**Components:**
- **Node Protocol** (`app/nodes/protocol.py`): Registration, heartbeat, events, canvas push
- **DeviceManager** (`app/nodes/manager.py`): LRU-cached device registry with capability tracking

**Architecture:** Devices register via WebSocket, receive canvas pushes, and send interaction events (touch, tap, swipe, voice, text input, button press).

---

## 14. Companion Desktop Apps

**Status:** 🏗 Scaffolded

**Description:** System tray companion apps for macOS (using `rumps`) and cross-platform (using `pystray`).

**Install:** `pip install manusclaw[companion]`

**Files:**
- `desktop/macos/menubar.py` — macOS menu bar app
- `desktop/windows_hub/hub.py` — Windows system tray hub
- `desktop/mobile/node_client.py` — Mobile WebSocket client

---

## Quick Start

```bash
# Install everything
pip install manusclaw[all-plus]

# Start the server
manusclaw-server

# Start cron scheduler
manusclaw-cron --run

# Manage sessions
manusclaw-sessions list
manusclaw-sessions history <session_id>

# SSH access (if enabled)
ssh -p 2222 localhost
```
