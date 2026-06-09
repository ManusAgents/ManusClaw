# IRC Adapter

**Status:** ✅ Implemented

## Description
Pure async IRC client using asyncio TCP streams with automatic PING/PONG keepalive.

## Configuration
| Variable | Description |
|---|---|
| `IRC_SERVER` | IRC hostname (e.g., `irc.libera.chat`) |
| `IRC_PORT` | Port (default: `6667`) |
| `IRC_NICK` | Bot nickname |
| `IRC_CHANNELS` | Comma-separated channels |
| `IRC_PASS` | Optional NickServ password |

## Architecture
Direct TCP connection. PRIVMSG parsed via regex. Messages split to 512-byte IRC limits.
