"""
PlatformControlTool — autonomous external platform manager.
Supports: GitHub, AWS, Vercel, WordPress, Netlify, HuggingFace,
          Twitter/X, Telegram, Discord, generic REST.
"""
from __future__ import annotations
import json
import os
from typing import Any, Optional
from pydantic import Field

try:
    import httpx
    _HTTPX = True
except ImportError:
    import urllib.request, urllib.error
    _HTTPX = False

from app.tool.base import BaseTool, ToolResult


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _req(method: str, url: str, headers: dict, params: dict = None,
         json_body: Any = None, data: Any = None, timeout: int = 30) -> dict:
    method = method.upper()
    if _HTTPX:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.request(method, url, headers=headers,
                          params=params, json=json_body, data=data)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": r.text}
    else:
        import urllib.request, urllib.parse, json as _json
        full_url = url + (("?" + urllib.parse.urlencode(params)) if params else "")
        raw = _json.dumps(json_body).encode() if json_body else None
        req = urllib.request.Request(full_url, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                try:
                    return {"status": resp.status, "body": _json.loads(body)}
                except Exception:
                    return {"status": resp.status, "body": body}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": e.read().decode()}


# ─────────────────────────────────────────────────────────────────────────────
# platform adapters
# ─────────────────────────────────────────────────────────────────────────────

class _GitHub:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.h = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.BASE + path, self.h, params, body)


class _Vercel:
    BASE = "https://api.vercel.com"

    def __init__(self, token: str, team_id: str = None):
        self.h = {"Authorization": f"Bearer {token}"}
        self.team = team_id

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        p = dict(params or {})
        if self.team:
            p["teamId"] = self.team
        return _req(method, self.BASE + path, self.h, p or None, body)


class _WordPress:
    def __init__(self, site_url: str, username: str, app_password: str):
        import base64
        creds = base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self.base = site_url.rstrip("/") + "/wp-json/wp/v2"
        self.h = {"Authorization": f"Basic {creds}",
                  "Content-Type": "application/json"}

    def call(self, method: str, endpoint: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.base + endpoint, self.h, params, body)


class _HuggingFace:
    BASE = "https://huggingface.co/api"
    INFERENCE = "https://api-inference.huggingface.co"

    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        base = self.INFERENCE if path.startswith("/models") else self.BASE
        return _req(method, base + path, self.h, params, body)


class _Netlify:
    BASE = "https://api.netlify.com/api/v1"

    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.BASE + path, self.h, params, body)


class _Discord:
    BASE = "https://discord.com/api/v10"

    def __init__(self, bot_token: str):
        self.h = {"Authorization": f"Bot {bot_token}",
                  "Content-Type": "application/json"}

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.BASE + path, self.h, params, body)


class _Telegram:
    def __init__(self, bot_token: str):
        self.base = f"https://api.telegram.org/bot{bot_token}"

    def call(self, method: str, endpoint: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.base + "/" + endpoint.lstrip("/"),
                    {}, params, body)


class _GenericREST:
    def __init__(self, base_url: str, token: str = None,
                 auth_scheme: str = "Bearer", extra_headers: dict = None):
        self.base = base_url.rstrip("/")
        self.h = dict(extra_headers or {})
        if token:
            self.h["Authorization"] = f"{auth_scheme} {token}"

    def call(self, method: str, path: str, params: dict = None,
             body: Any = None) -> dict:
        return _req(method, self.base + path, self.h, params, body)


# ─────────────────────────────────────────────────────────────────────────────
# platform registry
# ─────────────────────────────────────────────────────────────────────────────

def _build_platform(platform: str, credentials: dict):
    p = platform.lower()
    if p == "github":
        return _GitHub(credentials["token"])
    if p == "vercel":
        return _Vercel(credentials["token"], credentials.get("team_id"))
    if p in ("wordpress", "wp"):
        return _WordPress(credentials["site_url"],
                          credentials["username"],
                          credentials["app_password"])
    if p in ("huggingface", "hf"):
        return _HuggingFace(credentials["token"])
    if p == "netlify":
        return _Netlify(credentials["token"])
    if p == "discord":
        return _Discord(credentials["bot_token"])
    if p == "telegram":
        return _Telegram(credentials["bot_token"])
    return _GenericREST(
        credentials.get("base_url", ""),
        credentials.get("token"),
        credentials.get("auth_scheme", "Bearer"),
        credentials.get("headers"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# tool
# ─────────────────────────────────────────────────────────────────────────────

class PlatformControlTool(BaseTool):
    """
    Authenticate with any external platform and perform any action.

    Supported platforms (case-insensitive):
      github | vercel | wordpress | huggingface | netlify | discord | telegram | generic

    credentials dict keys by platform:
      github     → token
      vercel     → token, [team_id]
      wordpress  → site_url, username, app_password
      huggingface→ token
      netlify    → token
      discord    → bot_token
      telegram   → bot_token
      generic    → base_url, [token], [auth_scheme], [headers]

    method: GET | POST | PUT | PATCH | DELETE
    path:   API path, e.g.  /repos/owner/repo  or  /sites/{id}/deploys
    params: URL query parameters (dict)
    body:   JSON request body (dict)
    """

    name: str = "platform_control"
    description: str = (
        "Authenticate with and fully control any external platform "
        "(GitHub, Vercel, WordPress, HuggingFace, Netlify, Discord, Telegram, "
        "or any REST API). Can create, read, update, delete any resource "
        "the provided credentials permit."
    )

    # FIX: Proper OpenAI function-calling parameters schema (was using Pydantic Fields incorrectly)
    parameters = {
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "description": "Platform name, e.g. 'github', 'vercel', 'discord', 'telegram', 'netlify', 'huggingface', 'wordpress', 'generic'",
            },
            "credentials": {
                "type": "object",
                "description": (
                    "Auth credentials dict. Keys vary by platform: "
                    "github→token, vercel→token+team_id, wordpress→site_url+username+app_password, "
                    "huggingface→token, netlify→token, discord→bot_token, telegram→bot_token, "
                    "generic→base_url+token+auth_scheme+headers"
                ),
            },
            "method": {
                "type": "string",
                "description": "HTTP method: GET, POST, PUT, PATCH, or DELETE",
                "default": "GET",
            },
            "path": {
                "type": "string",
                "description": "API path to call, e.g. /repos/owner/repo",
            },
            "params": {
                "type": "object",
                "description": "URL query parameters (dict)",
            },
            "body": {
                "type": "object",
                "description": "JSON request body (dict)",
            },
        },
        "required": ["platform", "credentials", "path"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """
        Execute a platform control operation.
        Accepts: platform, credentials, method, path, params, body
        """
        platform = kwargs.get("platform")
        credentials = kwargs.get("credentials", {})
        method = kwargs.get("method", "GET")
        path = kwargs.get("path", "")
        params = kwargs.get("params")
        body = kwargs.get("body")

        if not platform:
            return ToolResult(output="", error="platform is required")

        try:
            adapter = _build_platform(platform, credentials)
            # Fix: run blocking HTTP in thread to avoid blocking event loop
            import asyncio
            result = await asyncio.to_thread(adapter.call, method, path, params, body)
            return ToolResult(
                output=json.dumps(result, indent=2, default=str),
                error=None,
            )
        except Exception as e:
            return ToolResult(output="", error=str(e))
