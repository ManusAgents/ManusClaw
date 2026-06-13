"""
Security Defense-in-Depth — Base Module
========================================

Core abstractions for the manusclaw security subsystem, inspired by
OpenHands's security architecture and adapted for the manusclaw agent
framework.

Classes:
    SecurityRisk   — Enum of risk severity levels (UNKNOWN → HIGH)
    RiskAssessment — Immutable assessment result from any analyzer
    SecurityAnalyzerBase — Abstract base that every analyzer must implement

Design principles:
    * Stateless analyzers — each call to ``analyze()`` is pure; no
      mutable instance state leaks between calls.
    * Crash-proof — any exception inside an analyzer is caught and
      promoted to a LOW-risk assessment so the agent loop never dies
      due to a security scanning bug.
    * Audit-friendly — every ``RiskAssessment`` carries a stable
      ``detector_id``, human-readable ``reason``, and an ``extras``
      dict for machine-readable telemetry.
    * No secret leaking — error messages are sanitised before being
      attached to ``RiskAssessment.reason``.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Sequence


# ──────────────────────────────────────────────────────────────────────────────
# Risk severity
# ──────────────────────────────────────────────────────────────────────────────

class SecurityRisk(IntEnum):
    """
    Ordered risk severity.

    ``UNKNOWN < LOW < MEDIUM < HIGH`` — the integer values enable
    straightforward ``max()`` comparisons for ensemble fusion.
    """

    UNKNOWN = 0
    LOW     = 1
    MEDIUM  = 2
    HIGH    = 3

    # Convenience aliases --------------------------------------------------

    @classmethod
    def from_name(cls, name: str) -> "SecurityRisk":
        """Case-insensitive lookup by name; returns UNKNOWN for bad input."""
        try:
            return cls[name.upper()]
        except KeyError:
            return cls.UNKNOWN

    def __str__(self) -> str:  # noqa: D105
        return self.name


# ──────────────────────────────────────────────────────────────────────────────
# Immutable assessment result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskAssessment:
    """
    The result of scanning a single action or command.

    Attributes:
        risk:        Overall risk level.
        detector_id: Stable identifier of the detector that produced this
                     assessment (e.g. ``"pattern:rm_rf"``, ``"rail:catastrophic-delete"``).
        reason:      Human-readable explanation.  Must **not** contain
                     secrets, file contents, or user data.
        extras:      Arbitrary machine-readable metadata for audit logs.
                     Keep values JSON-serialisable and secret-free.
    """

    risk:        SecurityRisk = SecurityRisk.UNKNOWN
    detector_id: str          = "base:unknown"
    reason:      str          = ""
    extras:      Dict[str, Any] = field(default_factory=dict)

    # ── Helpers ──────────────────────────────────────────────────────────

    def is_risky(self, threshold: SecurityRisk = SecurityRisk.MEDIUM) -> bool:
        """Return True if this assessment meets or exceeds *threshold*."""
        return self.risk >= threshold

    def merge(self, other: "RiskAssessment") -> "RiskAssessment":
        """
        Merge two assessments, taking the **higher** risk.

        If both have the same risk level the *other*'s reason is appended.
        The ``detector_id`` is preserved from the higher-risk assessment;
        on equal risk a comma-separated union is used.
        """
        if other.risk > self.risk:
            return other
        if other.risk < self.risk:
            return self
        # Equal risk — merge metadata
        merged_id = f"{self.detector_id},{other.detector_id}"
        merged_reason = f"{self.reason}; {other.reason}" if self.reason else other.reason
        merged_extras = {**self.extras, **other.extras}
        return RiskAssessment(
            risk=other.risk,
            detector_id=merged_id,
            reason=merged_reason,
            extras=merged_extras,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (safe for JSON / audit logs)."""
        return {
            "risk": self.risk.name,
            "risk_value": int(self.risk),
            "detector_id": self.detector_id,
            "reason": self.reason,
            "extras": self.extras,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Secret sanitisation helper
# ──────────────────────────────────────────────────────────────────────────────

# Patterns that commonly appear in leaked secrets
_SECRET_PATTERNS = re.compile(
    r"(?P<key>(?:api[_-]?key|token|secret|password|passwd|auth|credential|private[_-]?key)"
    r"\s*[:=]\s*)['\"]?[A-Za-z0-9_\-+/=]{8,}['\"]?",
    re.IGNORECASE,
)

_REDACTED = "<REDACTED>"


def sanitise_message(msg: str) -> str:
    """
    Strip potential secrets from an error message.

    This is a best-effort filter — it catches the most common patterns
    (API keys, tokens, passwords) but does not guarantee complete removal.
    """
    return _SECRET_PATTERNS.sub(r"\g<key>" + _REDACTED, msg)


# ──────────────────────────────────────────────────────────────────────────────
# Abstract analyzer base
# ──────────────────────────────────────────────────────────────────────────────

class SecurityAnalyzerBase(ABC):
    """
    Abstract base class for all security analyzers.

    Subclasses must implement :meth:`analyze`.  The base class wraps
    every call in a crash-proof try/except so that a bug in a detector
    can never crash the agent loop.
    """

    # Subclasses should override with a stable, human-readable name
    ANALYZER_NAME: str = "base"

    def __init__(self) -> None:
        self._instance_id: str = uuid.uuid4().hex[:8]

    # ── Public API ───────────────────────────────────────────────────────

    def safe_analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Crash-proof entry point.

        Calls :meth:`analyze` and catches **any** exception, converting
        it to a LOW-risk assessment with a sanitised reason so the
        caller never has to worry about the scanner blowing up.
        """
        try:
            result = self.analyze(action, context=context or {})
            # Defensive: ensure the subclass returned the right type
            if not isinstance(result, RiskAssessment):
                return RiskAssessment(
                    risk=SecurityRisk.LOW,
                    detector_id=f"{self.ANALYZER_NAME}:invalid_return",
                    reason="Analyzer returned non-RiskAssessment; downgraded to LOW",
                )
            return result
        except Exception as exc:
            # Never let a scanner crash the agent
            return RiskAssessment(
                risk=SecurityRisk.LOW,
                detector_id=f"{self.ANALYZER_NAME}:exception",
                reason=f"Analyzer error (sanitised): {sanitise_message(str(exc)[:300])}",
                extras={"exception_type": type(exc).__name__},
            )

    @abstractmethod
    def analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Analyse *action* for security risks.

        Args:
            action:  The command, code, or action string to scan.
            context: Optional dict with additional context (e.g. tool name,
                     session ID, user role).  Analyzers should treat
                     unknown keys gracefully.

        Returns:
            A :class:`RiskAssessment` describing the detected risk.
        """
        ...

    # ── Utility ──────────────────────────────────────────────────────────

    def __repr__(self) -> str:  # noqa: D105
        return f"<{self.__class__.__name__} id={self._instance_id}>"


# ──────────────────────────────────────────────────────────────────────────────
# Batch helpers
# ──────────────────────────────────────────────────────────────────────────────

def analyze_sequence(
    analyzers: Sequence[SecurityAnalyzerBase],
    action: str,
    context: Optional[Dict[str, Any]] = None,
) -> RiskAssessment:
    """
    Run multiple analyzers over the same *action* and fuse results
    using :meth:`RiskAssessment.merge` (max-severity semantics).
    """
    current = RiskAssessment()  # UNKNOWN
    for analyzer in analyzers:
        result = analyzer.safe_analyze(action, context)
        current = current.merge(result)
    return current
