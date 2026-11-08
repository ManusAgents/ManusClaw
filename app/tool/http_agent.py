r"""Postman-Agent tools: stateless HTTP requests, assertions, and environment.

Provides a Postman-like HTTP testing agent exposed over MCP:

- ``http_request``  — single HTTP call with SSRF guard, ``{{env:NAME}}``
  template expansion, ``auth_ref`` secret injection and curl reproduction.
- ``assert_response`` — evaluate JSON assertions against a response body.
- ``env_set`` / ``env_get`` — shared variable store (JSON persisted, with
  secret masking so secret values never enter LLM context).
- ``collection_run`` — run a Postman v2.1 collection via the ``newman``
  CLI and return a compact summary.

Design notes:

- Credentials are referenced, never passed: ``auth_ref`` names an entry in
  the secrets store (:class:`app.secrets.registry.SecretRegistry`), and the
  resolved value is injected server-side.
- Outbound requests are guarded against SSRF: private, loopback,
  link-local (incl. cloud metadata endpoints), multicast and reserved
  addresses are rejected unless ``MANUSCLAW_HTTP_ALLOW_PRIVATE`` is enabled
  or the host is listed in ``MANUSCLAW_HTTP_HOST_ALLOWLIST``. Redirects are
  followed manually so every hop is re-validated.
- All tool output passes through :func:`app.llm.secret_redaction.redact`
  as a final scrub.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import shlex
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

from app.llm.secret_redaction import redact
from app.logger import logger
from app.schema import ToolResult
from app.tool.base import BaseTool

# Maximum characters of a response body echoed back to the agent.
MAX_BODY_CHARS_DEFAULT = 20_000
# Redirect chain cap (each hop is SSRF-validated).
MAX_REDIRECTS = 5
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


class SSRFViolation(Exception):
    """Raised when a request targets a disallowed address."""


class SSRFGuard:
    """Validates URLs against private/loopback/link-local address ranges.

    The guard resolves the hostname and checks *every* resolved IP, which
    closes the classic DNS-rebinding gap of checking the URL string alone.
    """

    def __init__(self, allow_private: bool = False, host_allowlist: Optional[List[str]] = None):
        self.allow_private = allow_private
        self.host_allowlist = [h.lower() for h in (host_allowlist or [])]

    def check(self, url: str) -> str:
        """Validate ``url``; returns the normalized scheme+netloc or raises."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise SSRFViolation(f"Unsupported URL scheme '{parsed.scheme or ''}' (http/https only)")
        if not parsed.hostname:
            raise SSRFViolation("URL has no hostname")

        hostname = parsed.hostname.lower()
        if hostname in self.host_allowlist:
            return url

        for family, _stype, _proto, _canonname, sockaddr in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if self._is_disallowed(ip):
                raise SSRFViolation(
                    f"Host '{hostname}' resolves to disallowed address {ip} "
                    f"(private/loopback/link-local blocked; override with "
                    f"MANUSCLAW_HTTP_ALLOW_PRIVATE or MANUSCLAW_HTTP_HOST_ALLOWLIST)"
                )
        return url

    def _is_disallowed(self, ip: ipaddress._BaseAddress) -> bool:
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )


def build_guard() -> SSRFGuard:
    return SSRFGuard(
        allow_private=_env_flag("MANUSCLAW_HTTP_ALLOW_PRIVATE"),
        host_allowlist=_env_list("MANUSCLAW_HTTP_HOST_ALLOWLIST"),
    )


def parse_json_path(data: Any, path: str) -> Tuple[bool, Any]:
    """Minimal dot/bracket JSON path lookup, e.g. ``data.items[0].id``."""
    if path in ("", "$"):
        return True, data
    normalized = path[2:] if path.startswith("$.") else path
    current = data
    for token in normalized.replace("]", "").replace("[", ".").split("."):
        if token == "":
            continue
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


class HttpEnvStore:
    """Shared ``{{env:NAME}}`` variable store with JSON persistence.

    Secret entries are persisted like regular ones but are masked in every
    listing/get output; they are only used at expansion time inside
    ``http_request`` and then scrubbed from results by ``redact()``.
    """

    TEMPLATE_PREFIX = "{{env:"

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / ".manusclaw" / "http_env.json"
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("http_env store unreadable, starting empty: %s", exc)
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("http_env store save failed: %s", exc)

    def set(self, name: str, value: str, secret: bool = False) -> None:
        self._data[name] = {"value": value, "secret": bool(secret)}
        self._save()

    def get(self, name: str) -> Optional[Tuple[str, bool]]:
        entry = self._data.get(name)
        if entry is None:
            return None
        return entry["value"], bool(entry.get("secret"))

    def delete(self, name: str) -> bool:
        if name not in self._data:
            return False
        del self._data[name]
        self._save()
        return True

    def list(self) -> Dict[str, str]:
        return {
            name: ("***set***" if entry.get("secret") else entry["value"])
            for name, entry in sorted(self._data.items())
        }

    def expand(self, text: str) -> Tuple[str, List[str]]:
        """Replace ``{{env:NAME}}`` placeholders; returns (expanded, names)."""
        used: List[str] = []
        result = text
        while True:
            start = result.find(self.TEMPLATE_PREFIX)
            if start == -1:
                break
            end = result.find("}}", start + len(self.TEMPLATE_PREFIX))
            if end == -1:
                break
            name = result[start + len(self.TEMPLATE_PREFIX) : end].strip()
            entry = self._data.get(name)
            replacement = str(entry["value"]) if entry else ""
            if entry and name not in used:
                used.append(name)
            result = result[:start] + replacement + result[end + 2 :]
        return result, used


_ENV_STORE = HttpEnvStore()


def resolve_secret(reference: str) -> Optional[str]:
    """Resolve an ``auth_ref`` against the secrets store, if available."""
    from app.secrets.registry import SecretRegistry

    registry = SecretRegistry()
    try:
        if not registry.is_available(reference):
            return None
        secret = registry.resolve(reference)
        return secret.get_secret_value() if secret else None
    except Exception as exc:  # noqa: BLE001 - tool layer converts to ToolResult(error=...)
        logger.warning("auth_ref '%s' resolution failed: %s", reference, exc)
        return None


def build_auth_headers(auth_ref: str, auth_type: str, auth_header: str) -> Tuple[Dict[str, str], str]:
    """Build auth headers from a secret reference.

    Returns ``(headers, curl_auth_fragment)`` where the curl fragment uses a
    ``$AUTH_REF`` placeholder so the reproduced command leaks no secret.
    """
    value = resolve_secret(auth_ref)
    if value is None:
        raise ValueError(
            f"auth_ref '{auth_ref}' not found in secrets store; add it to the "
            f"secrets store instead of passing raw keys"
        )
    auth_type = (auth_type or "bearer").lower()
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {value}"}, "-H 'Authorization: Bearer $AUTH_REF'"
    if auth_type == "apikey_header":
        return {auth_header: value}, f"-H '{auth_header}: $AUTH_REF'"
    if auth_type == "basic":
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}, "-H 'Authorization: Basic $AUTH_REF'"
    raise ValueError(f"Unsupported auth_type '{auth_type}' (bearer|apikey_header|basic)")


def to_curl(method: str, url: str, headers: Dict[str, str], body: Optional[str]) -> str:
    """Build a curl command from *pre-expansion* inputs (no secret values)."""
    parts = [f"curl -X {method} {shlex.quote(url)}"]
    for name, value in headers.items():
        parts.append(f"-H {shlex.quote(f'{name}: {value}')}")
    if body is not None:
        parts.append(f"--data {shlex.quote(body)}")
    return " \\\n  ".join(parts)


def evaluate_assertions(data: Any, assertions: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
    """Evaluate assertion dicts ``{path, op, expected}`` against ``data``.

    Ops: eq, ne, contains, exists, gt, lt, regex. Returns (passed, failures).
    """
    import re as _re

    failures: List[str] = []
    passed = 0
    for assertion in assertions:
        path = str(assertion.get("path", ""))
        op = str(assertion.get("op", "eq")).lower()
        expected = assertion.get("expected")
        found, actual = parse_json_path(data, path)

        ok = False
        if op == "exists":
            ok = found
        elif not found:
            ok = False
        elif op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "contains":
            try:
                ok = expected in actual
            except TypeError:
                ok = False
        elif op == "gt":
            try:
                ok = actual > expected
            except TypeError:
                ok = False
        elif op == "lt":
            try:
                ok = actual < expected
            except TypeError:
                ok = False
        elif op == "regex":
            try:
                ok = _re.search(str(expected), str(actual)) is not None
            except _re.error:
                ok = False
        else:
            failures.append(f"{op}: unsupported op '{op}'")
            continue

        if ok:
            passed += 1
        else:
            failures.append(f"{path} {op} {expected!r} — actual {actual!r}")
    return passed, failures


class HttpRequestTool(BaseTool):
    """Send a single HTTP request with SSRF guard and secret-based auth."""

    name: str = "http_request"
    description: str = (
        "Send a single HTTP request (Postman-style). Supports {{env:NAME}} "
        "template expansion in url/headers/params/body, auth via auth_ref "
        "(secret store reference — never pass raw API keys), optional curl "
        "reproduction and SSRF protection against private addresses. "
        "Returns status, headers, body and latency."
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full request URL (http/https)"},
            "method": {"type": "string", "enum": list(HTTP_METHODS), "description": "HTTP method"},
            "headers": {"type": "object", "description": "Request headers"},
            "params": {"type": "object", "description": "Query parameters"},
            "body": {"type": "string", "description": "Raw request body"},
            "json_body": {"type": "object", "description": "JSON body (sets Content-Type)"},
            "auth_ref": {"type": "string", "description": "Secret store reference for auth credentials"},
            "auth_type": {"type": "string", "enum": ["bearer", "apikey_header", "basic"]},
            "auth_header": {"type": "string", "description": "Header name for apikey_header (default X-API-Key)"},
            "timeout": {"type": "number", "description": "Total timeout seconds (default 30)"},
            "follow_redirects": {"type": "boolean", "description": "Follow 3xx hops with re-validation (default true)"},
            "max_body_chars": {"type": "integer", "description": "Response body truncation limit"},
            "include_curl": {"type": "boolean", "description": "Append a curl reproduction (pre-expansion values)"},
        },
        "required": ["url"],
    }

    def __init__(self):
        super().__init__()
        self.guard = build_guard()

    async def execute(
        self,
        url: str = "",
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        json_body: Optional[Any] = None,
        auth_ref: str = "",
        auth_type: str = "bearer",
        auth_header: str = "X-API-Key",
        timeout: float = 30.0,
        follow_redirects: bool = True,
        max_body_chars: int = MAX_BODY_CHARS_DEFAULT,
        include_curl: bool = False,
        **kwargs,
    ) -> ToolResult:
        if not url:
            return ToolResult(error="url is required")
        method = method.upper()
        if method not in HTTP_METHODS:
            return ToolResult(error=f"Unsupported method '{method}'")

        headers = dict(headers or {})
        params = dict(params or {})
        raw_url, raw_headers, raw_params, raw_body = url, dict(headers), dict(params), body
        if json_body is not None:
            raw_body = json.dumps(json_body)
            body = raw_body
            headers.setdefault("Content-Type", "application/json")

        secret_names: List[str] = []
        try:
            url, used = _ENV_STORE.expand(url)
            secret_names.extend(used)
            for key, value in list(headers.items()):
                headers[key], used = _ENV_STORE.expand(str(value))
                secret_names.extend(used)
            for key, value in list(params.items()):
                params[key], used = _ENV_STORE.expand(str(value))
                secret_names.extend(used)
            if body is not None:
                body, used = _ENV_STORE.expand(body)
                secret_names.extend(used)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(error=f"Template expansion failed: {exc}")

        if auth_ref:
            try:
                auth_headers, curl_auth = build_auth_headers(auth_ref, auth_type, auth_header)
            except ValueError as exc:
                return ToolResult(error=str(exc))
            headers.update(auth_headers)
        else:
            curl_auth = None

        try:
            current_url = self.guard.check(url)
        except (SSRFViolation, socket.gaierror, ValueError) as exc:
            return ToolResult(error=f"SSRF guard: {exc}")

        timeout_obj = aiohttp.ClientTimeout(total=float(timeout))
        connector = aiohttp.TCPConnector()
        started = time.monotonic()
        status: Optional[int] = None
        resp_headers: Dict[str, str] = {}
        resp_body = ""
        hops = 0

        try:
            async with aiohttp.ClientSession(timeout=timeout_obj, connector=connector) as session:
                while True:
                    async with session.request(
                        method,
                        current_url,
                        headers=headers,
                        params=params if hops == 0 else None,
                        data=body if method not in ("GET", "HEAD") else None,
                        allow_redirects=False,
                    ) as resp:
                        status = resp.status
                        resp_headers = {k: v for k, v in resp.headers.items()}
                        if status in (301, 302, 303, 307, 308) and follow_redirects and hops < MAX_REDIRECTS:
                            location = resp_headers.get("Location") or resp_headers.get("location")
                            if not location:
                                break
                            if status in (301, 302, 303):
                                method = "GET"
                                body = None
                            next_url = urljoin(current_url, location)
                            try:
                                current_url = self.guard.check(next_url)
                            except (SSRFViolation, socket.gaierror, ValueError) as exc:
                                return ToolResult(error=f"SSRF guard on redirect: {exc}")
                            hops += 1
                            continue
                        resp_body = await resp.text()
                        break
        except asyncio.TimeoutError:
            return ToolResult(error=f"Request timed out after {timeout}s")
        except aiohttp.ClientError as exc:
            return ToolResult(error=f"Request failed: {type(exc).__name__}: {exc}")

        elapsed = time.monotonic() - started
        if len(resp_body) > max_body_chars:
            resp_body = resp_body[:max_body_chars] + f"\n... [truncated {len(resp_body) - max_body_chars} chars]"

        lines = [
            f"STATUS: {status} ({elapsed:.2f}s, {hops} redirect(s))",
            f"URL: {current_url}",
            "HEADERS:",
        ]
        lines.extend(f"  {k}: {v}" for k, v in sorted(resp_headers.items()))
        lines.append("BODY:")
        lines.append(resp_body or "(empty)")

        if include_curl:
            curl_headers = dict(raw_headers)
            if json_body is not None:
                curl_headers.setdefault("Content-Type", "application/json")
            if curl_auth:
                curl_headers.update(dict(self._parse_curl_auth(curl_auth)))
            curl = to_curl(method, raw_url, curl_headers, raw_body)
            if params:
                curl = curl.replace(
                    raw_url, raw_url + ("&" if "?" in raw_url else "?") + "&".join(f"{k}={v}" for k, v in raw_params.items()), 1
                )
            lines.append("CURL:")
            lines.append(curl)

        output = redact("\n".join(lines))
        return ToolResult(output=output, system=f"secrets_expanded={secret_names}" if secret_names else None)

    @staticmethod
    def _parse_curl_auth(fragment: str) -> List[Tuple[str, str]]:
        # fragment like: -H 'Authorization: Bearer $AUTH_REF'
        _flag, quoted = fragment.split(" ", 1)
        header = quoted.strip("'\"")
        name, _, value = header.partition(": ")
        return [(name, value)]


class AssertResponseTool(BaseTool):
    """Evaluate JSON assertions against a response body."""

    name: str = "assert_response"
    description: str = (
        "Evaluate assertions against a response body. Each assertion is "
        "{path, op, expected} with dot/bracket JSON paths (data.items[0].id) "
        "and ops: eq, ne, contains, exists, gt, lt, regex. Pass the parsed "
        "JSON response as 'response'."
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "response": {"description": "Parsed JSON response body (object, array or scalar)"},
            "assertions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "op": {"type": "string", "enum": ["eq", "ne", "contains", "exists", "gt", "lt", "regex"]},
                        "expected": {"description": "Expected value (omit for exists)"},
                    },
                    "required": ["path", "op"],
                },
                "description": "List of assertions to evaluate",
            },
        },
        "required": ["response", "assertions"],
    }

    async def execute(self, response: Any = None, assertions: Optional[List[Dict[str, Any]]] = None, **kwargs) -> ToolResult:
        if assertions is None:
            return ToolResult(error="assertions is required")
        passed, failures = evaluate_assertions(response, assertions)
        total = passed + len(failures)
        if failures:
            detail = "\n".join(f"FAIL: {f}" for f in failures)
            return ToolResult(output=f"{passed}/{total} passed\n{detail}", error=f"{len(failures)} assertion(s) failed")
        return ToolResult(output=f"PASS: {passed}/{total} assertions")


class EnvSetTool(BaseTool):
    """Set variables in the shared HTTP agent environment store."""

    name: str = "env_set"
    description: str = (
        "Set one or more variables in the shared HTTP agent environment. "
        "Variables are referenced in http_request as {{env:NAME}}. Mark "
        "sensitive values with secret=true — they are masked in all outputs "
        "and only usable via template expansion."
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Variable name (single mode)"},
            "value": {"type": "string", "description": "Variable value (single mode)"},
            "entries": {"type": "object", "description": "Batch mode: {name: value}"},
            "secret": {"type": "boolean", "description": "Mask value in outputs (single mode)"},
        },
    }

    async def execute(self, key: str = "", value: str = "", entries: Optional[Dict[str, str]] = None, secret: bool = False, **kwargs) -> ToolResult:
        if entries:
            for entry_name, entry_value in entries.items():
                _ENV_STORE.set(str(entry_name), str(entry_value))
            return ToolResult(output=f"Set {len(entries)} variable(s): {', '.join(sorted(entries))}")
        if not key:
            return ToolResult(error="Provide 'key'+'value' or batch 'entries'")
        _ENV_STORE.set(key, value, secret=secret)
        shown = "***set***" if secret else value
        return ToolResult(output=f"Set {key}={shown}" + (" (secret)" if secret else ""))


class EnvGetTool(BaseTool):
    """Read variables from the shared HTTP agent environment store."""

    name: str = "env_get"
    description: str = (
        "Get variables from the shared HTTP agent environment. Secret values "
        "are always masked — reference them in requests with {{env:NAME}} "
        "instead."
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Variable name; omit with all=true to list"},
            "all": {"type": "boolean", "description": "List all variables (masked)"},
        },
    }

    async def execute(self, key: str = "", all: bool = False, **kwargs) -> ToolResult:
        if all or (not key and not all):
            listing = _ENV_STORE.list()
            if not listing:
                return ToolResult(output="Environment is empty")
            lines = [f"{k}={v}" for k, v in listing.items()]
            return ToolResult(output="\n".join(lines))
        entry = _ENV_STORE.get(key)
        if entry is None:
            return ToolResult(output=f"{key} is not set")
        value, is_secret = entry
        shown = "***set***" if is_secret else value
        return ToolResult(output=f"{key}={shown}")


class CollectionRunTool(BaseTool):
    """Run a Postman collection with Newman and summarize the results."""

    name: str = "collection_run"
    description: str = (
        "Run a Postman v2.1 collection via the newman CLI (npm i -g "
        "newman). Returns request stats and first failures. Note: collections "
        "bypass the http_request SSRF guard — only run trusted collection "
        "files."
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Path or URL to the Postman collection JSON"},
            "environment": {"type": "string", "description": "Path or URL to a Postman environment JSON"},
            "folder": {"type": "string", "description": "Run only this collection folder"},
            "iterations": {"type": "integer", "description": "Number of iterations (default 1)"},
            "timeout": {"type": "number", "description": "Overall run timeout seconds (default 600)"},
            "bail": {"type": "boolean", "description": "Stop on first failure"},
        },
        "required": ["collection"],
    }

    async def execute(
        self,
        collection: str = "",
        environment: str = "",
        folder: str = "",
        iterations: int = 1,
        timeout: float = 600.0,
        bail: bool = False,
        **kwargs,
    ) -> ToolResult:
        if not collection:
            return ToolResult(error="collection is required")
        newman = shutil.which("newman")
        if not newman:
            return ToolResult(error="newman not found on PATH — install with: npm install -g newman")

        with tempfile.TemporaryDirectory(prefix="manusclaw-newman-") as tmp_dir:
            report_path = str(Path(tmp_dir) / "report.json")
            cmd = [newman, "run", collection, "--reporters", "json", "--reporter-json-export", report_path]
            if environment:
                cmd += ["-e", environment]
            if folder:
                cmd += ["--folder", folder]
            if iterations and iterations > 1:
                cmd += ["-n", str(iterations)]
            if bail:
                cmd.append("--bail")

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(error=f"newman run timed out after {timeout}s")
            except OSError as exc:
                return ToolResult(error=f"Failed to launch newman: {exc}")

            report_file = Path(report_path)
            if not report_file.exists():
                err = stderr.decode("utf-8", errors="replace")[:2000]
                return ToolResult(error=f"newman produced no JSON report (exit {proc.returncode}). stderr:\n{err}")

            try:
                report = json.loads(report_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return ToolResult(error=f"newman report unreadable: {exc}")

        run = report.get("run", {})
        stats = run.get("stats", {})
        failures = run.get("failures", [])
        summary = {
            "exit_code": proc.returncode,
            "requests": stats.get("requests", {}),
            "assertions": stats.get("assertions", {}),
            "failures": [
                {
                    "source": (f.get("source", {}) or {}).get("name") or (f.get("source", {}) or {}).get("method"),
                    "error": (f.get("error", {}) or {}).get("message"),
                }
                for f in failures[:10]
            ],
            "failure_count": len(failures),
        }
        output = redact(json.dumps(summary, indent=2, default=str))
        return ToolResult(output=output, error=None if proc.returncode == 0 else f"newman exited {proc.returncode}")
