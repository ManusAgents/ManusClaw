"""
Free LLM Pool Router
====================

Keyless pool of free, OpenAI-compatible LLM endpoints (Kilo AI Gateway,
Pollinations, opencode zen) with:

- **Health probing** (cached, TTL-based): a connect-level failure marks an
  entry dead for the probe TTL; any HTTP response (even 404/405 on the
  models listing) proves the host is alive.
- **Per-entry cooldowns** with exponential backoff on rate limits (429),
  mirroring :class:`~app.llm.fallback.FallbackStrategy` semantics.
- **A security gate**: prompts whose fused security risk (pattern +
  policy-rail ensemble) meets the configured threshold are NEVER sent to
  keyless cloud endpoints.  They are routed to a local offline router
  (Ollama / LMStudio / GGUF via :func:`build_offline_router`) or fail
  loudly with :class:`FreePoolSecurityError`.  Rationale: free gateways
  explicitly log prompts (``kilo-auto/free``'s own description warns that
  prompts may be logged by upstream providers and are not suitable for
  sensitive data workloads).
- **Model aliasing**: models prefixed with ``free/`` route through the
  pool; ``free/auto`` picks the best healthy entry and lets each entry use
  its default model.

Configuration lives in ``[llm.free_pool]`` (see
:class:`~app.config.LLMFreePoolConfig`).  Built-in default entries can be
overridden by supplying ``entries`` in the config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.config import LLMFreePoolConfig
from app.llm.offline_router import build_offline_router
from app.security.base import RiskAssessment, SecurityRisk, sanitise_message
from app.security.ensemble import EnsembleSecurityAnalyzer
from app.security.pattern import PatternSecurityAnalyzer
from app.security.policy_rails import PolicyRailSecurityAnalyzer

logger = logging.getLogger("manusclaw.free_pool")

FREE_MODEL_PREFIX = "free/"
FREE_AUTO_MODEL = "free/auto"

PROBE_TIMEOUT_S = 8.0
_MAX_ACTION_CHARS = 20_000
_LOCAL_PROVIDER_KEYS = ("lmstudio", "textgen", "openai-compat", "universal")


# ──────────────────────────────────────────────────────────────────────────────
# Pool entries
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FreePoolEntry:
    """One keyless, OpenAI-compatible endpoint in the free pool."""

    name: str
    base_url: str
    api_key: str = "none"
    models: tuple[str, ...] = ()
    notes: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


DEFAULT_POOL_ENTRIES: tuple[FreePoolEntry, ...] = (
    FreePoolEntry(
        name="kilo",
        base_url="https://ai-gateway.kilo.ai/api/v1",
        api_key="none",
        models=(
            "kilo-auto/free",
            "nvidia/nemotron-3.5-super-vl:free",
            "reflection/beam-free",
            "stealth/glyph-cluster",
            "liquid/lfm-3-1.7b:free",
        ),
        notes=(
            "Kilo AI Gateway free tier; kilo-auto/free rotates free models "
            "server-side. The gateway warns prompts may be logged upstream."
        ),
    ),
    FreePoolEntry(
        name="pollinations",
        base_url="https://text.pollinations.ai/openai",
        api_key="none",
        models=("openai",),
        notes="Pollinations OpenAI-compatible endpoint (openai/mistral/llama).",
    ),
    FreePoolEntry(
        name="opencode",
        base_url="https://opencode.ai/zen/v1",
        api_key="public",
        models=("deepseek-v4-flash-free",),
        notes="opencode zen public key (see providers/opencode.toml).",
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────────────

class FreePoolError(RuntimeError):
    """Base error for free-pool routing failures."""


class FreePoolExhaustedError(FreePoolError):
    """All pool entries failed or are cooling down."""


class FreePoolSecurityError(FreePoolError):
    """Prompt was too risky for keyless endpoints and no local route exists."""


class _RateLimited(Exception):
    """Entry returned 429 / quota exhaustion."""


class _Transient(Exception):
    """Network, timeout, or 5xx failure — retry later."""


class _Unusable(Exception):
    """4xx other than 429 — retrying soon will not help."""


# ──────────────────────────────────────────────────────────────────────────────
# Per-entry state (cooldown semantics mirror FallbackStrategy)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _EntryState:
    failure_count: int = 0
    cooldown_until: float = 0.0        # time.monotonic()
    last_error: str = ""
    healthy: bool = True               # last health-probe result
    probed_at: float = -1.0            # time.monotonic() of last probe


# ──────────────────────────────────────────────────────────────────────────────
# HTTP transport seam
# ──────────────────────────────────────────────────────────────────────────────

Transport = Callable[[str, str, dict[str, str], Optional[dict], float],
                     Awaitable[tuple[int, Any]]]


async def _http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Optional[dict],
    timeout_s: float,
) -> tuple[int, Any]:
    """Default transport: httpx (sync client in a worker thread), urllib fallback."""
    try:
        import httpx
    except ImportError:
        return await _urllib_transport(method, url, headers, payload, timeout_s)

    def _do() -> tuple[int, Any]:
        if method == "GET":
            resp = httpx.get(url, headers=headers, timeout=timeout_s)
        else:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout_s)
        try:
            body: Any = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body

    return await asyncio.to_thread(_do)


async def _urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Optional[dict],
    timeout_s: float,
) -> tuple[int, Any]:
    """Stdlib-only fallback transport (mirrors offline_router's approach)."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode() if payload is not None else None

    def _do() -> tuple[int, Any]:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode() or "{}")
            except Exception:
                return exc.code, {"raw": str(exc)[:500]}

    return await asyncio.to_thread(_do)


def _body_says_rate_limit(body: Any) -> bool:
    """Detect quota/rate-limit error bodies regardless of HTTP status quirks."""
    if not isinstance(body, dict):
        return False
    try:
        blob = json.dumps(body, default=str)[:2000].lower()
    except Exception:
        return False
    markers = ("rate limit", "ratelimit", "too many requests", "quota")
    return any(m in blob for m in markers)


def _brief(body: Any, limit: int = 200) -> str:
    try:
        text = json.dumps(body, default=str)
    except Exception:
        text = str(body)
    return sanitise_message(text[:limit])


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def strip_free_prefix(model: Optional[str]) -> Optional[str]:
    """``free/kilo-auto/free`` → ``kilo-auto/free``; non-prefixed models pass through."""
    if model and model.startswith(FREE_MODEL_PREFIX):
        return model[len(FREE_MODEL_PREFIX):]
    return model


def _action_text(messages: Any) -> str:
    """Flatten chat messages into one scannable string for the security gate."""
    parts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)[:_MAX_ACTION_CHARS]


def _annotate(body: Any, entry_name: str, model: str) -> Any:
    """Tag the response model so callers can see which pool entry served it."""
    if isinstance(body, dict):
        annotated = dict(body)
        annotated["model"] = f"{FREE_MODEL_PREFIX}{entry_name}/{model}"
        return annotated
    return body


# ──────────────────────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────────────────────

class FreePoolRouter:
    """Routes chat requests across keyless free endpoints with health,
    cooldown, and security-gating guarantees.

    Args:
        config:      ``[llm.free_pool]`` settings; ``None`` uses defaults.
        app_config:  Full :class:`~app.config.AppConfig` used to build the
                     local offline router for risky prompts.  ``None`` means
                     local fallback is unavailable.
        transport:   Async transport seam ``(method, url, headers, payload,
                     timeout_s) -> (status, body)``.  Defaults to httpx/urllib;
                     tests inject fakes here — no real network is required.
    """

    def __init__(
        self,
        config: Optional[LLMFreePoolConfig] = None,
        *,
        app_config: Optional[Any] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self._cfg = config or LLMFreePoolConfig()
        self._app_config = app_config
        self._transport: Transport = transport or _http_transport

        if self._cfg.entries:
            self._entries: tuple[FreePoolEntry, ...] = tuple(
                FreePoolEntry(
                    name=e.name,
                    base_url=e.base_url,
                    api_key=e.api_key,
                    models=tuple(e.models),
                )
                for e in self._cfg.entries
            )
        else:
            self._entries = DEFAULT_POOL_ENTRIES

        self._state: dict[str, _EntryState] = {
            e.name: _EntryState() for e in self._entries
        }
        self._lock = threading.Lock()
        self._rr = 0

        self._gate, self._threshold = self._build_gate()
        self._local_router: Optional[Any] = None
        self._local_router_built = False

    # ── Gate ────────────────────────────────────────────────────────────────

    def _build_gate(self) -> tuple[Optional[EnsembleSecurityAnalyzer], SecurityRisk]:
        raw = (self._cfg.security_threshold or "").strip().lower()
        if raw in ("off", "none", "disabled"):
            return None, SecurityRisk.UNKNOWN
        threshold = SecurityRisk.from_name(raw)
        # from_name() swallows bad input to UNKNOWN; a typo must never
        # silently disable (or over-tighten) the gate.
        if threshold == SecurityRisk.UNKNOWN and raw and raw != "unknown":
            threshold = SecurityRisk.MEDIUM
        ensemble = EnsembleSecurityAnalyzer(
            [PatternSecurityAnalyzer(), PolicyRailSecurityAnalyzer()],
            name="free_pool_gate",
        )
        return ensemble, threshold

    # ── Local routing for risky prompts ─────────────────────────────────────

    def _get_local_router(self) -> Optional[Any]:
        if self._local_router_built:
            return self._local_router
        self._local_router_built = True

        cfg = self._app_config
        if cfg is None:
            return None
        llm = cfg.llm
        provider = (llm.provider or "").strip().lower()
        conf: Optional[dict] = None
        if provider == "ollama":
            conf = {
                "provider": "ollama",
                "model": llm.model,
                "base_url": llm.base_url or "http://localhost:11434",
            }
        elif provider == "gguf":
            # LLMConfig has no model_path field; base_url doubles as the
            # .gguf path for fully-offline routing.
            if llm.base_url:
                conf = {"provider": "gguf", "model_path": llm.base_url}
        elif provider in _LOCAL_PROVIDER_KEYS:
            if llm.base_url:
                conf = {
                    "provider": "openai_compat",
                    "model": llm.model,
                    "base_url": llm.base_url,
                    "api_key": "none",
                }
        if conf is not None:
            try:
                self._local_router = build_offline_router(conf)
            except Exception as exc:
                logger.warning("free_pool: local router build failed: %s", exc)
                self._local_router = None
        return self._local_router

    async def _route_local_or_block(
        self,
        messages: list[dict],
        assessment: RiskAssessment,
        tools: Optional[list],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict:
        reason = f"{assessment.detector_id}: {assessment.reason}"
        if not self._cfg.local_fallback:
            raise FreePoolSecurityError(
                f"Prompt blocked from keyless free endpoints "
                f"(risk {assessment.risk.name}); local_fallback is disabled. {reason}"
            )
        router = self._get_local_router()
        if router is None:
            raise FreePoolSecurityError(
                f"Prompt blocked from keyless free endpoints "
                f"(risk {assessment.risk.name}); no local router available — "
                f"set [llm] provider to ollama/gguf/lmstudio/textgen/openai-compat "
                f"with a base_url. {reason}"
            )
        logger.warning("free_pool: risky prompt routed to local router (%s)", reason)
        response = await router.chat(
            messages,
            tools=tools,
            max_tokens=max_tokens or 2048,
            temperature=temperature,
        )
        local_model = getattr(router, "model", "local")
        return _annotate(response, "local", str(local_model))

    # ── Selection ───────────────────────────────────────────────────────────

    def _candidates(self, requested: Optional[str]) -> list[tuple[FreePoolEntry, str]]:
        """Ordered (entry, model) candidates, round-robin rotated."""
        entries = self._entries
        if not entries:
            return []
        with self._lock:
            offset = self._rr % len(entries)
            self._rr += 1
        ordered = entries[offset:] + entries[:offset]

        out: list[tuple[FreePoolEntry, str]] = []
        if requested in (None, "", "auto"):
            for entry in ordered:
                if entry.models:
                    out.append((entry, entry.models[0]))
            return out

        for entry in ordered:
            if requested in entry.models:
                out.append((entry, requested))
        if not out:
            # Unknown model: pass it through to every entry in order.
            for entry in ordered:
                out.append((entry, requested))
        return out

    # ── Health & cooldown state ─────────────────────────────────────────────

    def _state_for(self, name: str) -> _EntryState:
        with self._lock:
            return self._state[name]

    def _in_cooldown(self, name: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return now < self._state[name].cooldown_until

    def _cooldown_for(self, state: _EntryState) -> float:
        # First failure cools for exactly cooldown_s; subsequent failures
        # grow exponentially, capped at max_cooldown_s.
        return min(
            self._cfg.cooldown_s
            * (self._cfg.cooldown_multiplier ** max(0, state.failure_count - 1)),
            self._cfg.max_cooldown_s,
        )

    def _mark_failed(self, name: str, exc: Exception) -> None:
        with self._lock:
            state = self._state[name]
            state.failure_count += 1
            state.last_error = sanitise_message(str(exc)[:300])
            state.cooldown_until = time.monotonic() + self._cooldown_for(state)
        logger.info(
            "free_pool: entry '%s' failed (%s); cooldown %.0fs",
            name, type(exc).__name__, self._cooldown_for(self._state_for(name)),
        )

    def _mark_success(self, name: str) -> None:
        with self._lock:
            state = self._state[name]
            state.failure_count = 0
            state.cooldown_until = 0.0
            state.last_error = ""
            state.healthy = True

    async def _ensure_healthy(self, entry: FreePoolEntry) -> bool:
        cfg = self._cfg
        now = time.monotonic()
        state = self._state_for(entry.name)
        if now - state.probed_at <= cfg.health_probe_ttl_s:
            return state.healthy
        try:
            status, _ = await self._transport(
                "GET", f"{entry.base_url}/models", entry.headers, None, PROBE_TIMEOUT_S,
            )
            healthy = True  # any HTTP response proves the host is reachable
            logger.debug("free_pool: probe '%s' → HTTP %d", entry.name, status)
        except Exception as exc:
            healthy = False
            logger.info("free_pool: probe '%s' failed: %s", entry.name, type(exc).__name__)
        with self._lock:
            state = self._state[entry.name]
            state.healthy = healthy
            state.probed_at = now
        return healthy

    # ── Chat call against one entry ─────────────────────────────────────────

    async def _call_chat(
        self,
        entry: FreePoolEntry,
        model: str,
        messages: list[dict],
        tools: Optional[list],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or 2048,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        try:
            status, body = await self._transport(
                "POST",
                f"{entry.base_url}/chat/completions",
                entry.headers,
                payload,
                float(self._cfg.timeout),
            )
        except Exception as exc:
            raise _Transient(
                f"{type(exc).__name__}: {sanitise_message(str(exc)[:200])}"
            ) from exc

        if status == 429 or _body_says_rate_limit(body):
            raise _RateLimited(f"HTTP {status}: {_brief(body)}")
        if 200 <= status < 300:
            if isinstance(body, dict) and body.get("choices"):
                return body
            raise _Transient(f"HTTP {status} without choices: {_brief(body)}")
        if 500 <= status < 600:
            raise _Transient(f"HTTP {status}: {_brief(body)}")
        raise _Unusable(f"HTTP {status}: {_brief(body)}")

    # ── Public API ──────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        tools: Optional[list] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **_: Any,
    ) -> dict:
        """Serve a chat completion from the free pool.

        Security gate first: risky prompts go to the local router or raise.
        Then rotate across healthy, non-cooling entries; 429s apply
        exponential-backoff cooldowns and the next entry is tried.
        """
        requested = strip_free_prefix(model)

        if self._gate is not None:
            action = _action_text(messages)
            if action.strip():
                assessment = self._gate.safe_analyze(action, {"tool": "free_pool"})
                if assessment.risk >= self._threshold:
                    return await self._route_local_or_block(
                        messages, assessment, tools, temperature, max_tokens,
                    )

        candidates = self._candidates(requested)
        if not candidates:
            raise FreePoolExhaustedError("Free pool has no entries configured")

        last_error: Optional[Exception] = None
        for entry, entry_model in candidates:
            if self._in_cooldown(entry.name):
                continue
            if self._cfg.health_probe and not await self._ensure_healthy(entry):
                continue
            try:
                body = await self._call_chat(
                    entry, entry_model, messages, tools, temperature, max_tokens,
                )
            except (_RateLimited, _Transient, _Unusable) as exc:
                self._mark_failed(entry.name, exc)
                last_error = exc
                continue
            self._mark_success(entry.name)
            return _annotate(body, entry.name, entry_model)

        raise FreePoolExhaustedError(
            f"All free-pool entries failed or are cooling down; "
            f"last error: {last_error}"
        )

    def health(self) -> dict[str, dict[str, Any]]:
        """Observability snapshot: per-entry health, cooldowns, last error."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "healthy": state.healthy,
                    "failure_count": state.failure_count,
                    "cooldown_remaining_s": max(0.0, state.cooldown_until - now),
                    "last_error": state.last_error,
                }
                for name, state in self._state.items()
            }
