"""
Security Defense-in-Depth — LLM Security Analyzer
====================================================

Uses a large-language model to assess security risk when pattern-based
analysis is ambiguous or returns MEDIUM / UNKNOWN results.

The LLM is never the *only* line of defence — it augments the
deterministic pattern and rail analyzers by catching novel or obfuscated
threats that regexes cannot express.

Design choices:
    * **Opt-in** — the LLM analyzer is only invoked when explicitly
      added to an ensemble; it is **not** part of the default
      PatternSecurityAnalyzer flow.
    * **Fallback-safe** — if the LLM call fails (network, timeout,
      invalid response), the analyzer returns the ``fallback_risk``
      (default: LOW) so the agent loop never blocks.
    * **Budget-aware** — each call consumes tokens; callers can set
      ``max_calls`` to cap usage per session.
    * **Secret-free prompts** — the action string is sanitised before
      being sent to the LLM; responses are validated and clamped to the
      known :class:`SecurityRisk` enum.
    * **Thread-safe** — the call counter is protected by a
      ``threading.Lock``.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from app.security.base import (
    RiskAssessment,
    SecurityAnalyzerBase,
    SecurityRisk,
    sanitise_message,
)


# ──────────────────────────────────────────────────────────────────────────────
# Default LLM prompt
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a security risk analyzer for an AI agent framework called manusclaw.
Your job is to assess the risk level of an action that the agent is about
to execute.

Risk levels:
  UNKNOWN — Cannot determine risk (insufficient information)
  LOW     — Safe action, no security concerns
  MEDIUM  — Potentially dangerous; warrants human review
  HIGH    — Almost certainly malicious or catastrophic; must be blocked

Respond with a JSON object with exactly these keys:
  "risk":   one of "UNKNOWN", "LOW", "MEDIUM", "HIGH"
  "reason": a brief, secret-free explanation (max 200 characters)

Do NOT include any other text. Only output the JSON object.
"""

_ACTION_TEMPLATE = """\
Analyze this action for security risk:
---
{action}
---
Context: {context}
"""


# ──────────────────────────────────────────────────────────────────────────────
# LLM caller protocol
# ──────────────────────────────────────────────────────────────────────────────

# Callable signature: (system_prompt: str, user_prompt: str) -> str
# The caller is responsible for handling retries, timeouts, etc.
LLMCaller = Callable[[str, str], str]


def _default_llm_caller(system_prompt: str, user_prompt: str) -> str:
    """
    Default LLM caller that attempts to use the manusclaw LLM layer.

    Falls back to returning a LOW-risk JSON if the LLM layer is not
    configured or available.  This ensures the analyzer never crashes
    due to missing LLM infrastructure.
    """
    try:
        from app.llm.llm import ask_llm  # type: ignore[import-untyped]
        return ask_llm(system_prompt, user_prompt)
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: return a safe LOW assessment without calling the LLM
    return json.dumps({"risk": "LOW", "reason": "LLM unavailable; defaulted to LOW"})


# ──────────────────────────────────────────────────────────────────────────────
# Analyzer
# ──────────────────────────────────────────────────────────────────────────────

class LLMSecurityAnalyzer(SecurityAnalyzerBase):
    """
    LLM-backed security risk analyzer.

    Sends the action string to a large-language model for semantic
    analysis when deterministic pattern matching is insufficient.

    Args:
        llm_caller:    Callable that accepts (system_prompt, user_prompt)
                       and returns the LLM's raw text response.
        fallback_risk: Risk level to return if the LLM call fails.
        max_calls:     Maximum number of LLM calls allowed.  Once
                       exhausted, all subsequent calls return
                       ``fallback_risk``.  ``None`` means unlimited.
        timeout_s:     Per-call timeout in seconds (enforced by the
                       caller, not the analyzer itself — used as a
                       hint for logging).
    """

    ANALYZER_NAME: str = "llm"

    def __init__(
        self,
        llm_caller: Optional[LLMCaller] = None,
        fallback_risk: SecurityRisk = SecurityRisk.LOW,
        max_calls: Optional[int] = None,
        timeout_s: float = 10.0,
    ) -> None:
        super().__init__()
        self._llm_caller = llm_caller or _default_llm_caller
        self._fallback_risk = fallback_risk
        self._max_calls = max_calls
        self._timeout_s = timeout_s

        # Thread-safe call counter
        self._call_count = 0
        self._lock = threading.Lock()

    # ── Public properties ────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        """Number of LLM calls made so far."""
        with self._lock:
            return self._call_count

    @property
    def calls_remaining(self) -> Optional[int]:
        """Number of LLM calls remaining, or None if unlimited."""
        if self._max_calls is None:
            return None
        with self._lock:
            return max(0, self._max_calls - self._call_count)

    # ── Analyze ──────────────────────────────────────────────────────────

    def analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Send *action* to the LLM for risk assessment.

        If the call budget is exhausted, returns ``fallback_risk``.
        On any error, returns ``fallback_risk`` with a sanitised reason.
        """
        if not action or not action.strip():
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:empty",
                reason="Empty action — nothing to assess",
            )

        # Check call budget
        if not self._try_consume_call():
            return RiskAssessment(
                risk=self._fallback_risk,
                detector_id=f"{self.ANALYZER_NAME}:budget_exhausted",
                reason=f"LLM call budget exhausted ({self._max_calls}); using fallback",
                extras={"fallback_risk": self._fallback_risk.name},
            )

        # Build prompt
        safe_action = sanitise_message(action[:2000])  # Truncate to avoid token explosion
        safe_context = sanitise_message(json.dumps(context or {}, default=str)[:500])
        user_prompt = _ACTION_TEMPLATE.format(action=safe_action, context=safe_context)

        # Call LLM
        start = time.monotonic()
        try:
            raw_response = self._llm_caller(_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            return RiskAssessment(
                risk=self._fallback_risk,
                detector_id=f"{self.ANALYZER_NAME}:call_error",
                reason=f"LLM call failed (sanitised): {sanitise_message(str(exc)[:200])}",
                extras={
                    "fallback_risk": self._fallback_risk.name,
                    "exception_type": type(exc).__name__,
                },
            )
        elapsed = time.monotonic() - start

        # Parse response
        return self._parse_response(raw_response, elapsed)

    # ── Internal ─────────────────────────────────────────────────────────

    def _try_consume_call(self) -> bool:
        """
        Atomically check the budget and increment the counter.

        Returns True if the call is allowed, False if budget is exhausted.
        """
        with self._lock:
            if self._max_calls is not None and self._call_count >= self._max_calls:
                return False
            self._call_count += 1
            return True

    def _parse_response(self, raw: str, elapsed: float) -> RiskAssessment:
        """
        Parse the LLM response into a :class:`RiskAssessment`.

        Malformed or unexpected responses are clamped to
        ``fallback_risk``.
        """
        extras: Dict[str, Any] = {
            "llm_elapsed_s": round(elapsed, 3),
        }

        try:
            # The LLM should return a JSON object; strip markdown fences
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                # Remove ```json and ``` fences
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                raise ValueError("Response is not a JSON object")

            risk_name = str(parsed.get("risk", "")).upper().strip()
            risk = SecurityRisk.from_name(risk_name)
            reason = str(parsed.get("reason", ""))[:300]  # Cap reason length
            reason = sanitise_message(reason)

            return RiskAssessment(
                risk=risk,
                detector_id=f"{self.ANALYZER_NAME}:assessment",
                reason=reason,
                extras=extras,
            )

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            extras["parse_error"] = sanitise_message(str(exc)[:100])
            extras["raw_preview"] = sanitise_message(raw[:200])
            return RiskAssessment(
                risk=self._fallback_risk,
                detector_id=f"{self.ANALYZER_NAME}:parse_error",
                reason="Could not parse LLM response; using fallback risk",
                extras=extras,
            )

    def reset_budget(self) -> None:
        """Reset the call counter, allowing more LLM calls."""
        with self._lock:
            self._call_count = 0
