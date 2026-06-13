"""
Security Defense-in-Depth — Confirmation Policies
===================================================

Confirmation policies determine whether a risky action requires explicit
human approval before execution.  They sit **above** the analyzers in
the security stack:

    1. Analyzer detects a risk → :class:`RiskAssessment`
    2. Confirmation policy decides → confirm / auto-approve

Two policies are provided:

    * **NeverConfirm** — always auto-approve, regardless of risk.
      Useful for fully autonomous (build-mode) agents or trusted
      environments.

    * **ConfirmRisky** — require human confirmation when the risk
      level meets or exceeds a configurable threshold (default:
      ``SecurityRisk.MEDIUM``).  This is the recommended policy for
      production deployments.

Both policies are stateless and thread-safe.  They implement a common
:class:`ConfirmationPolicy` protocol so they can be swapped at runtime.

Design note: the ``requires_confirmation`` method returns a structured
:class:`ConfirmationDecision` rather than a bare bool, giving the
caller access to the reason and the originating assessment for UI
presentation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.security.base import RiskAssessment, SecurityRisk


# ──────────────────────────────────────────────────────────────────────────────
# Decision result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConfirmationDecision:
    """
    The result of applying a confirmation policy.

    Attributes:
        needs_confirmation: Whether human approval is required.
        reason:            Human-readable explanation (secret-free).
        assessment:        The original :class:`RiskAssessment` that
                           triggered this decision.
        policy_name:       Name of the policy that produced this decision.
    """

    needs_confirmation: bool
    reason:             str
    assessment:         RiskAssessment
    policy_name:        str

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (safe for JSON / audit logs)."""
        return {
            "needs_confirmation": self.needs_confirmation,
            "reason": self.reason,
            "policy_name": self.policy_name,
            "assessment": self.assessment.to_dict(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────────────

class ConfirmationPolicy(ABC):
    """
    Abstract base class for confirmation policies.

    Subclasses must implement :meth:`requires_confirmation`.
    """

    POLICY_NAME: str = "base"

    @abstractmethod
    def requires_confirmation(self, assessment: RiskAssessment) -> ConfirmationDecision:
        """
        Decide whether *assessment* requires human confirmation.

        Args:
            assessment: The risk assessment from the security analyzer.

        Returns:
            A :class:`ConfirmationDecision` with the verdict.
        """
        ...

    def __repr__(self) -> str:  # noqa: D105
        return f"<{self.__class__.__name__}>"


# ──────────────────────────────────────────────────────────────────────────────
# NeverConfirm — always auto-approve
# ──────────────────────────────────────────────────────────────────────────────

class NeverConfirm(ConfirmationPolicy):
    """
    Policy that **never** requires human confirmation.

    Use this for fully autonomous agents (build mode) or when all
    actions are pre-vetted.  Every assessment is auto-approved
    regardless of risk level.
    """

    POLICY_NAME: str = "never_confirm"

    def requires_confirmation(self, assessment: RiskAssessment) -> ConfirmationDecision:
        """
        Always returns ``needs_confirmation=False``.
        """
        return ConfirmationDecision(
            needs_confirmation=False,
            reason="NeverConfirm policy: auto-approve all actions",
            assessment=assessment,
            policy_name=self.POLICY_NAME,
        )


# ──────────────────────────────────────────────────────────────────────────────
# ConfirmRisky — confirm above threshold
# ──────────────────────────────────────────────────────────────────────────────

class ConfirmRisky(ConfirmationPolicy):
    """
    Policy that requires human confirmation when the risk level meets
    or exceeds a configurable threshold.

    Args:
        threshold: Risk level at or above which confirmation is
                   required.  Defaults to ``SecurityRisk.MEDIUM``.
        block_high: If True, HIGH-risk actions are **blocked** outright
                    rather than just requiring confirmation.  This is
                    the safest setting for production deployments.

    Example::

        policy = ConfirmRisky(threshold=SecurityRisk.MEDIUM, block_high=True)

        # MEDIUM risk → needs confirmation
        # HIGH risk   → blocked (cannot be confirmed)
        # LOW risk    → auto-approved
    """

    POLICY_NAME: str = "confirm_risky"

    def __init__(
        self,
        threshold: SecurityRisk = SecurityRisk.MEDIUM,
        block_high: bool = False,
    ) -> None:
        self._threshold = threshold
        self._block_high = block_high

    @property
    def threshold(self) -> SecurityRisk:
        """Current confirmation threshold."""
        return self._threshold

    @property
    def block_high(self) -> bool:
        """Whether HIGH-risk actions are blocked outright."""
        return self._block_high

    def requires_confirmation(self, assessment: RiskAssessment) -> ConfirmationDecision:
        """
        Decide based on the assessment's risk level:

        * Below threshold → auto-approve.
        * At or above threshold (but not HIGH with ``block_high``) →
          needs confirmation.
        * HIGH with ``block_high=True`` → **blocked** (cannot be
          confirmed, ``needs_confirmation=False`` but the caller should
          treat this as a hard deny).

        The distinction between "needs confirmation" and "blocked" is
        communicated via ``reason`` and the caller is expected to check
        for the word "blocked" or inspect ``assessment.risk``.
        """
        risk = assessment.risk

        # Below threshold — auto-approve
        if risk < self._threshold:
            return ConfirmationDecision(
                needs_confirmation=False,
                reason=(
                    f"ConfirmRisky policy: risk {risk.name} below "
                    f"threshold {self._threshold.name}; auto-approved"
                ),
                assessment=assessment,
                policy_name=self.POLICY_NAME,
            )

        # HIGH risk with block_high — hard block
        if risk == SecurityRisk.HIGH and self._block_high:
            return ConfirmationDecision(
                needs_confirmation=False,  # Not confirmable — outright blocked
                reason=(
                    f"ConfirmRisky policy: HIGH risk blocked outright "
                    f"(block_high=True). Action denied."
                ),
                assessment=assessment,
                policy_name=self.POLICY_NAME,
            )

        # At or above threshold — needs confirmation
        return ConfirmationDecision(
            needs_confirmation=True,
            reason=(
                f"ConfirmRisky policy: risk {risk.name} meets or exceeds "
                f"threshold {self._threshold.name}; human confirmation required"
            ),
            assessment=assessment,
            policy_name=self.POLICY_NAME,
        )

    def set_threshold(self, threshold: SecurityRisk) -> None:
        """
        Update the confirmation threshold at runtime.

        Thread-safe for reads; callers should not modify the threshold
        while other threads are evaluating policies.
        """
        self._threshold = threshold
