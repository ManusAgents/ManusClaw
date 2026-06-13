"""
Security Defense-in-Depth — Pattern Security Analyzer
======================================================

Regex-based threat detection inspired by OpenHands's pattern-matching
security layer.  Scans action strings for known dangerous patterns and
returns a :class:`RiskAssessment` for each match.

Two scanning corpora:

    * **executable** — patterns that only make sense inside shell
      commands, code blocks, or other executable contexts (e.g.
      ``rm -rf``, ``sudo rm``).  Applied when the tool is ``bash`` or
      ``python_execute``.

    * **all-field** — patterns that are dangerous regardless of context
      (e.g. prompt-injection overrides, identity switches).  Applied to
      *every* action.

Each pattern carries a stable ``detector_id`` (e.g. ``"pattern:rm_rf"``)
so that audit trails remain comparable across releases.

Thread-safety: the compiled regex list is built once at class-definition
time and never mutated, so instances are safe to share across threads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.security.base import (
    RiskAssessment,
    SecurityAnalyzerBase,
    SecurityRisk,
    sanitise_message,
)


# ──────────────────────────────────────────────────────────────────────────────
# Pattern definitions
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Pattern:
    """
    A single compiled regex pattern with its associated risk metadata.

    Attributes:
        detector_id: Stable identifier (e.g. ``"pattern:rm_rf"``).
        regex:       Pre-compiled regular expression.
        risk:        Risk level if this pattern matches.
        reason:      Human-readable explanation (secret-free).
        corpus:      ``"executable"`` or ``"all_field"``.
    """

    detector_id: str
    regex:       re.Pattern[str]
    risk:        SecurityRisk
    reason:      str
    corpus:      str  # "executable" | "all_field"


# ──────────────────────────────────────────────────────────────────────────────
# Pattern catalogue
# ──────────────────────────────────────────────────────────────────────────────

def _build_patterns() -> List[_Pattern]:
    """
    Build the master list of detection patterns.

    Order does not matter — every pattern is evaluated independently
    and the highest-severity match wins.
    """
    raw: List[Tuple[str, str, SecurityRisk, str, str]] = [
        # ── executable corpus ────────────────────────────────────────────
        # detector_id           regex pattern                             risk                  reason                                         corpus
        ("pattern:rm_rf",       r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?-[a-zA-Z]*r[a-zA-Z]*\s+",         SecurityRisk.HIGH,   "Recursive force-delete (rm -rf) detected",                  "executable"),
        ("pattern:sudo_rm",     r"\bsudo\s+rm\s+",                                                      SecurityRisk.HIGH,   "Privileged delete (sudo rm) detected",                       "executable"),
        ("pattern:eval_call",   r"\beval\s*\(",                                                         SecurityRisk.MEDIUM, "Dynamic code execution via eval() detected",                  "executable"),
        ("pattern:subprocess",  r"\bsubprocess\.(call|run|Popen|check_output|check_call)\s*\(",        SecurityRisk.MEDIUM, "Subprocess invocation detected — possible command injection", "executable"),
        ("pattern:curl_pipe_exec", r"curl\s+.*\|\s*(ba)?sh",                                           SecurityRisk.HIGH,   "Download-then-execute (curl | sh) detected",                  "executable"),

        # ── all-field corpus ─────────────────────────────────────────────
        # These are dangerous regardless of the tool context.
        ("pattern:inject_override",  r"(?i)ignore\s+(previous|above|all)\s+instructions",             SecurityRisk.HIGH,   "Prompt-injection: instruction override detected",             "all_field"),
        ("pattern:inject_mode_switch", r"(?i)(you\s+are\s+now|switch\s+to)\s+(root|admin|superuser)", SecurityRisk.HIGH,   "Prompt-injection: mode/role switch detected",                "all_field"),
        ("pattern:inject_identity",  r"(?i)(pretend|act|roleplay)\s+(you\s+are|as)\s+(root|admin|superuser|system)", SecurityRisk.HIGH, "Prompt-injection: identity impersonation detected", "all_field"),
    ]

    compiled: List[_Pattern] = []
    for detector_id, pattern, risk, reason, corpus in raw:
        try:
            compiled.append(
                _Pattern(
                    detector_id=detector_id,
                    regex=re.compile(pattern, re.IGNORECASE | re.DOTALL),
                    risk=risk,
                    reason=reason,
                    corpus=corpus,
                )
            )
        except re.error as exc:
            # Never let a bad regex crash the module import
            import logging
            logging.getLogger("manusclaw.security").warning(
                "Skipping invalid security pattern %s: %s", detector_id, exc
            )
    return compiled


_PATTERNS: List[_Pattern] = _build_patterns()

# Pre-split for fast corpus selection
_EXEC_PATTERNS: List[_Pattern] = [p for p in _PATTERNS if p.corpus == "executable"]
_ALL_PATTERNS:  List[_Pattern] = [p for p in _PATTERNS if p.corpus == "all_field"]


# ──────────────────────────────────────────────────────────────────────────────
# Analyzer
# ──────────────────────────────────────────────────────────────────────────────

class PatternSecurityAnalyzer(SecurityAnalyzerBase):
    """
    Regex-based threat detection analyzer.

    Scans action strings against two corpora:

    1. **executable** — only when the ``tool`` context key is one of
       ``{"bash", "python_execute", "shell", "exec"}``.
   2. **all-field** — always, regardless of tool.

    If multiple patterns match, the highest-severity match wins.
    On equal severity, the *first* match in catalogue order is reported.
    All matched patterns are recorded in ``extras["matched_patterns"]``
    for audit purposes.
    """

    ANALYZER_NAME: str = "pattern"

    # Tools that trigger executable-corpus scanning
    EXECUTABLE_TOOLS: frozenset[str] = frozenset({
        "bash", "python_execute", "shell", "exec", "str_replace_editor",
    })

    def analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Scan *action* for known dangerous patterns.

        Args:
            action:  The command or code string to scan.
            context: Optional dict; the ``"tool"`` key controls corpus
                     selection.  Unknown tools fall back to all-field only.

        Returns:
            The highest-severity :class:`RiskAssessment`, or an
            UNKNOWN-risk assessment if no patterns match.
        """
        if not action or not action.strip():
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:empty",
                reason="Empty action — nothing to scan",
            )

        ctx = context or {}
        tool_name = str(ctx.get("tool", "")).lower()

        # Select which corpus to use
        patterns_to_scan: List[_Pattern] = list(_ALL_PATTERNS)
        if tool_name in self.EXECUTABLE_TOOLS:
            patterns_to_scan = patterns_to_scan + list(_EXEC_PATTERNS)

        # Evaluate
        best: Optional[RiskAssessment] = None
        matched_ids: List[str] = []

        for pat in patterns_to_scan:
            if pat.regex.search(action):
                matched_ids.append(pat.detector_id)
                assessment = RiskAssessment(
                    risk=pat.risk,
                    detector_id=pat.detector_id,
                    reason=pat.reason,
                    extras={"matched_text_snippet": self._safe_snippet(action, pat.regex)},
                )
                if best is None or assessment.risk > best.risk:
                    best = assessment

        if best is None:
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:no_match",
                reason="No dangerous patterns detected",
            )

        # Attach full match list for auditing
        return RiskAssessment(
            risk=best.risk,
            detector_id=best.detector_id,
            reason=best.reason,
            extras={
                **best.extras,
                "matched_patterns": matched_ids,
                "tool": tool_name,
                "corpora_scanned": ["all_field"] + (["executable"] if tool_name in self.EXECUTABLE_TOOLS else []),
            },
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _safe_snippet(action: str, pattern: re.Pattern[str], max_len: int = 60) -> str:
        """
        Extract a short, secret-free snippet around the first match.

        The snippet is truncated to *max_len* and sanitised.
        """
        m = pattern.search(action)
        if not m:
            return ""
        start = max(0, m.start() - 10)
        end = min(len(action), m.end() + 20)
        snippet = action[start:end].replace("\n", "\\n")
        if len(snippet) > max_len:
            snippet = snippet[:max_len] + "..."
        return sanitise_message(snippet)
