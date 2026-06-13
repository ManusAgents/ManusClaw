"""
Security Defense-in-Depth — Ensemble Security Analyzer
========================================================

Combines multiple security analyzers into a single composite analyzer
that returns the **maximum severity** across all sub-analyzers.

This is the primary entry point for the manusclaw security subsystem:
users construct an ``EnsembleSecurityAnalyzer`` with the desired set of
analyzers and call :meth:`analyze` once.  The ensemble handles:

    * Parallel-safe invocation of each sub-analyzer.
    * Crash isolation — a failing analyzer is demoted to LOW risk via
      :meth:`SecurityAnalyzerBase.safe_analyze`.
    * Max-severity fusion — the highest risk wins; all detector IDs are
      preserved in ``extras["all_detectors"]``.
    * Audit trail — every sub-assessment is recorded in
      ``extras["sub_assessments"]`` for post-incident review.

Typical usage::

    ensemble = EnsembleSecurityAnalyzer([
        PatternSecurityAnalyzer(),
        PolicyRailSecurityAnalyzer(),
        # LLMSecurityAnalyzer(max_calls=10),  # optional
    ])

    assessment = ensemble.analyze("rm -rf /", context={"tool": "bash"})
    if assessment.is_risky(SecurityRisk.HIGH):
        raise PermissionDenied("Blocked by security policy")
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from app.security.base import (
    RiskAssessment,
    SecurityAnalyzerBase,
    SecurityRisk,
)


# ──────────────────────────────────────────────────────────────────────────────
# Ensemble analyzer
# ──────────────────────────────────────────────────────────────────────────────

class EnsembleSecurityAnalyzer(SecurityAnalyzerBase):
    """
    Composite analyzer that fuses results from multiple sub-analyzers
    using **max-severity** semantics.

    Args:
        analyzers:  Sequence of sub-analyzers to run.  Order does not
                    affect the fused result but is preserved in the
                    audit trail.
        name:       Optional custom name for the ensemble (appears in
                    ``detector_id`` prefixes).
    """

    ANALYZER_NAME: str = "ensemble"

    def __init__(
        self,
        analyzers: Sequence[SecurityAnalyzerBase],
        name: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._analyzers: List[SecurityAnalyzerBase] = list(analyzers)
        self._name = name or self.ANALYZER_NAME
        self._lock = threading.Lock()  # protects any future mutable state

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def analyzers(self) -> List[SecurityAnalyzerBase]:
        """Read-only snapshot of the current sub-analyzers."""
        return list(self._analyzers)

    @property
    def analyzer_count(self) -> int:
        """Number of sub-analyzers in the ensemble."""
        return len(self._analyzers)

    # ── Analyze ──────────────────────────────────────────────────────────

    def analyze(self, action: str, context: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """
        Run all sub-analyzers and fuse results.

        The fused assessment has:
            * The **maximum** risk level across all sub-assessments.
            * The ``detector_id`` of the highest-risk sub-assessment.
            * A combined ``reason`` that concatenates all sub-reasons.
            * ``extras["all_detectors"]`` — list of all detector IDs.
            * ``extras["sub_assessments"]`` — list of all sub-assessment
              dicts (for audit logging).

        If no sub-analyzers are configured, returns UNKNOWN risk.
        """
        if not self._analyzers:
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self._name}:no_analyzers",
                reason="No sub-analyzers configured in ensemble",
            )

        if not action or not action.strip():
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self._name}:empty",
                reason="Empty action — nothing to scan",
            )

        ctx = context or {}
        sub_assessments: List[RiskAssessment] = []

        # Run each sub-analyzer (crash-proof via safe_analyze)
        for analyzer in self._analyzers:
            assessment = analyzer.safe_analyze(action, ctx)
            sub_assessments.append(assessment)

        # Fuse results
        return self._fuse(sub_assessments)

    # ── Fusion ───────────────────────────────────────────────────────────

    def _fuse(self, assessments: List[RiskAssessment]) -> RiskAssessment:
        """
        Fuse multiple assessments using max-severity semantics.

        The result carries:
            * The highest risk level.
            * The detector_id from the highest-risk assessment (or a
              comma-separated union if multiple share the top risk).
            * All detector IDs in ``extras["all_detectors"]``.
            * All sub-assessments as dicts in
              ``extras["sub_assessments"]``.
        """
        if not assessments:
            return RiskAssessment(
                risk=SecurityRisk.UNKNOWN,
                detector_id=f"{self._name}:empty_fusion",
                reason="No assessments to fuse",
            )

        # Find max risk
        max_risk = max(a.risk for a in assessments)
        top_assessments = [a for a in assessments if a.risk == max_risk]

        # Detector ID: union if multiple share top risk
        if len(top_assessments) == 1:
            fused_detector = top_assessments[0].detector_id
        else:
            fused_detector = ",".join(sorted(set(a.detector_id for a in top_assessments)))

        # Reason: combine top reasons
        reasons = [a.reason for a in top_assessments if a.reason]
        fused_reason = "; ".join(reasons) if reasons else f"Ensemble risk: {max_risk.name}"

        # Build extras
        all_detector_ids = [a.detector_id for a in assessments]
        sub_dicts = [a.to_dict() for a in assessments]

        extras: Dict[str, Any] = {
            "all_detectors": all_detector_ids,
            "sub_assessments": sub_dicts,
            "ensemble_name": self._name,
            "analyzer_count": len(assessments),
        }

        # Merge extras from top assessments (later wins for duplicate keys)
        for a in top_assessments:
            extras.update(a.extras)

        return RiskAssessment(
            risk=max_risk,
            detector_id=fused_detector,
            reason=fused_reason,
            extras=extras,
        )

    # ── Mutation helpers ─────────────────────────────────────────────────

    def add_analyzer(self, analyzer: SecurityAnalyzerBase) -> None:
        """
        Add a sub-analyzer to the ensemble.

        Thread-safe: acquires the internal lock.
        """
        with self._lock:
            self._analyzers.append(analyzer)

    def remove_analyzer(self, analyzer_name: str) -> bool:
        """
        Remove the first sub-analyzer whose ``ANALYZER_NAME`` matches.

        Returns True if an analyzer was removed, False otherwise.
        Thread-safe: acquires the internal lock.
        """
        with self._lock:
            for i, a in enumerate(self._analyzers):
                if a.ANALYZER_NAME == analyzer_name:
                    self._analyzers.pop(i)
                    return True
        return False

    # ── Repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        names = [a.ANALYZER_NAME for a in self._analyzers]
        return f"<EnsembleSecurityAnalyzer analyzers={names}>"
