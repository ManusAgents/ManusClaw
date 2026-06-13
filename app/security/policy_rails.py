"""
Security Defense-in-Depth — Policy Rail Analyzer
==================================================

Structural "rails" that detect whole-action-shape threats which simple
regex token matching can miss.  Each rail evaluates the action **per
segment** (split on semicolon / newline boundaries) to prevent
cross-field false positives — a pattern in segment N must not be
confused with a completely unrelated pattern in segment M.

**Important**: Shell pipes (``|``) are **not** used as segment
delimiters because they *connect* commands into a single logical
operation.  Splitting on pipes would break detection of ``curl | sh``
while failing to prevent false positives (``curl … ; echo bash`` is
already handled by semicolon splitting).

Three structural rails:

1. **fetch-to-exec** — a download (curl, wget) piped to an interpreter
   (sh, bash, python, exec).  This is the most common vector for
   supply-chain compromise.

2. **raw-disk-op** — direct writes to block devices (``dd`` to
   ``/dev/sd*``, ``mkfs``) that can destroy a filesystem in one shot.

3. **catastrophic-delete** — recursive force-delete of critical system
   paths (``/``, ``/etc``, ``/usr``, ``/bin``, ``/sbin``, ``/var``,
   ``/boot``).  Distinguished from ordinary ``rm -rf`` by checking the
   *target path*, not just the flags.

Thread-safety: all state is computed per-call; the compiled regex list
is built once at import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.security.base import (
    RiskAssessment,
    SecurityAnalyzerBase,
    SecurityRisk,
    sanitise_message,
)


# ──────────────────────────────────────────────────────────────────────────────
# Segment splitter
# ──────────────────────────────────────────────────────────────────────────────

# Split on semicolon and newline boundaries only.
# We intentionally do NOT split on `|` (pipe) because pipes connect
# commands into a single logical operation — `curl ... | sh` is one
# action, not two.
_SEGMENT_SPLITTER = re.compile(r";|\n|\r")


def _split_segments(action: str) -> List[str]:
    """
    Split an action string into logical segments for per-segment
    evaluation.  This prevents cross-field false positives where a
    download command in segment 1 and a ``sh`` reference in segment 2
    are unrelated.

    Shell pipes (``|``) are preserved within segments because they
    *connect* commands (e.g. ``curl | sh``) — splitting on pipes would
    break fetch-to-exec detection.
    """
    segments = _SEGMENT_SPLITTER.split(action)
    return [s.strip() for s in segments if s.strip()]


# ──────────────────────────────────────────────────────────────────────────────
# Rail definitions
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Rail:
    """
    A single structural rail definition.

    Attributes:
        detector_id: Stable identifier (e.g. ``"rail:fetch-to-exec"``).
        risk:        Risk level when this rail fires.
        reason:      Human-readable reason (secret-free).
        fetch_pat:   Regex that matches the "fetch" part within a segment.
        exec_pat:    Regex that matches the "exec" part within the *same*
                     segment (or ``None`` if only ``fetch_pat`` matters).
        combined:    If True, both ``fetch_pat`` and ``exec_pat`` must
                     match the *same* segment.  If False, ``fetch_pat``
                     alone is sufficient.
    """

    detector_id: str
    risk:        SecurityRisk
    reason:      str
    fetch_pat:   re.Pattern[str]
    exec_pat:    Optional[re.Pattern[str]]
    combined:    bool


# ──────────────────────────────────────────────────────────────────────────────
# Rail catalogue
# ──────────────────────────────────────────────────────────────────────────────

def _build_rails() -> List[_Rail]:
    """Build and compile all structural rails."""
    return [
        # ── Rail 1: fetch-to-exec ────────────────────────────────────────
        # A download command piped to an interpreter within the SAME segment.
        # Pipes are preserved in segments, so "curl ... | sh" is one segment.
        _Rail(
            detector_id="rail:fetch-to-exec",
            risk=SecurityRisk.HIGH,
            reason="Download piped to interpreter — possible remote code execution",
            fetch_pat=re.compile(
                r"(?:curl|wget|fetch|aria2c)\s+",
                re.IGNORECASE,
            ),
            exec_pat=re.compile(
                r"(?:sh|bash|zsh|dash|ksh|python[0-9.]*|perl|ruby|node|exec|eval|source)\b",
                re.IGNORECASE,
            ),
            combined=True,
        ),

        # ── Rail 2: raw-disk-op ──────────────────────────────────────────
        # Direct write to a block device or filesystem format command.
        _Rail(
            detector_id="rail:raw-disk-op",
            risk=SecurityRisk.HIGH,
            reason="Raw disk operation detected — direct block device write or format",
            fetch_pat=re.compile(
                r"(?:dd\s+if=.*of=/dev/(?:sd|hd|vd|nvme|xvd)\S+"
                r"|mkfs\.\S+"
                r"|>\s*/dev/(?:sd|hd|vd|nvme|xvd)\S+"
                r")",
                re.IGNORECASE,
            ),
            exec_pat=None,
            combined=False,
        ),

        # ── Rail 3: catastrophic-delete ──────────────────────────────────
        # Recursive force-delete targeting critical system paths.
        _Rail(
            detector_id="rail:catastrophic-delete",
            risk=SecurityRisk.HIGH,
            reason="Recursive force-delete of critical system path detected",
            fetch_pat=re.compile(
                # Match: rm with -r and -f flags (in any combination) followed by
                # either / (root) or a critical system path
                r"rm\s+"
                r"(?:"
                # Flag combinations: -rf, -fr, -r -f, -f -r, and variations with other flags
                r"-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+"
                r"|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+"
                r")"
                r"(?:"
                r"/\s*$"           # bare / (root filesystem)
                r"|/\*"            # /* (root glob)
                r"|/etc\b"
                r"|/usr\b"
                r"|/bin\b"
                r"|/sbin\b"
                r"|/var\b"
                r"|/boot\b"
                r"|/lib\b"
                r"|/lib64\b"
                r"|/sys\b"
                r"|/proc\b"
                r"|/root\b"
                r"|/home\b"
                r")",
                re.IGNORECASE,
            ),
            exec_pat=None,
            combined=False,
        ),
    ]


_RAILS: List[_Rail] = _build_rails()


# ──────────────────────────────────────────────────────────────────────────────
# Analyzer
# ──────────────────────────────────────────────────────────────────────────────

class PolicyRailSecurityAnalyzer(SecurityAnalyzerBase):
    """
    Structural policy-rail analyzer.

    Evaluates each action **per segment** (split on semicolon / newline
    boundaries) so that patterns in unrelated segments are never
    conflated.  This eliminates the classic cross-field false positive
    where ``curl http://example.com`` in one segment and ``echo bash``
    in another segment would wrongly trigger the fetch-to-exec rail.

    Shell pipes (``|``) are preserved within segments because they
    *connect* commands into a single logical operation.  ``curl | sh``
    is correctly detected as fetch-to-exec, while ``curl … ; echo bash``
    is not (they are separate segments).

    For *combined* rails (like fetch-to-exec), both the fetch and exec
    patterns must match within the **same** segment.
    """

    ANALYZER_NAME: str = "policy_rails"

    def analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Evaluate *action* against all structural rails.

        Per-segment evaluation prevents cross-field false positives.
        The highest-severity match wins; all triggered rails are listed
        in ``extras["triggered_rails"]`` for audit purposes.
        """
        if not action or not action.strip():
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:empty",
                reason="Empty action — nothing to scan",
            )

        segments = _split_segments(action)
        if not segments:
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:empty",
                reason="No non-empty segments found",
            )

        best: Optional[RiskAssessment] = None
        triggered_ids: List[str] = []
        triggered_details: List[Dict[str, Any]] = []

        for rail in _RAILS:
            for seg_idx, segment in enumerate(segments):
                match = self._evaluate_rail(rail, segment)
                if match is not None:
                    triggered_ids.append(rail.detector_id)
                    triggered_details.append({
                        "rail": rail.detector_id,
                        "segment_index": seg_idx,
                        "segment_preview": self._safe_preview(segment),
                    })
                    if best is None or rail.risk > best.risk:
                        best = RiskAssessment(
                            risk=rail.risk,
                            detector_id=rail.detector_id,
                            reason=rail.reason,
                            extras={
                                "segment_index": seg_idx,
                                "segment_preview": self._safe_preview(segment),
                            },
                        )
                    # No need to check more segments for this rail once matched
                    break

        if best is None:
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self.ANALYZER_NAME}:clear",
                reason="No structural policy violations detected",
                extras={"segments_scanned": len(segments)},
            )

        return RiskAssessment(
            risk=best.risk,
            detector_id=best.detector_id,
            reason=best.reason,
            extras={
                **best.extras,
                "triggered_rails": triggered_ids,
                "triggered_details": triggered_details,
                "segments_scanned": len(segments),
            },
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _evaluate_rail(rail: _Rail, segment: str) -> Optional[re.Match[str]]:
        """
        Evaluate a single rail against a single segment.

        For *combined* rails, both fetch and exec patterns must match
        the same segment.  For non-combined rails, only the fetch
        pattern is checked.
        """
        fetch_match = rail.fetch_pat.search(segment)
        if not fetch_match:
            return None

        if not rail.combined:
            return fetch_match

        # Combined: both must match same segment
        if rail.exec_pat is None:
            return None  # Shouldn't happen for combined, but be safe

        exec_match = rail.exec_pat.search(segment)
        if exec_match:
            return fetch_match
        return None

    @staticmethod
    def _safe_preview(segment: str, max_len: int = 80) -> str:
        """Return a short, sanitised preview of a segment."""
        preview = segment.replace("\n", "\\n").replace("\r", "")
        if len(preview) > max_len:
            preview = preview[:max_len] + "..."
        return sanitise_message(preview)
