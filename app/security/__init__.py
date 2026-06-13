"""
Security Defense-in-Depth — manusclaw Security Subsystem
=========================================================

A layered security architecture inspired by OpenHands, adapted for the
manusclaw agent framework.  The subsystem provides:

**Analyzers** (detect threats):
    - :class:`PatternSecurityAnalyzer` — regex-based pattern matching
    - :class:`PolicyRailSecurityAnalyzer` — structural policy rails
    - :class:`LLMSecurityAnalyzer` — AI-powered risk assessment
    - :class:`EnsembleSecurityAnalyzer` — max-severity fusion of multiple analyzers

**Decision policies** (decide what to do):
    - :class:`NeverConfirm` — auto-approve everything
    - :class:`ConfirmRisky` — require human confirmation above threshold

**Encryption** (protect data at rest):
    - :class:`Cipher` — Fernet-based symmetric encryption with token prefix

**Core types**:
    - :class:`SecurityRisk` — UNKNOWN / LOW / MEDIUM / HIGH
    - :class:`RiskAssessment` — immutable assessment result
    - :class:`ConfirmationDecision` — policy decision result

Quick start::

    from app.security import (
        EnsembleSecurityAnalyzer,
        PatternSecurityAnalyzer,
        PolicyRailSecurityAnalyzer,
        ConfirmRisky,
        SecurityRisk,
        Cipher,
    )

    # Build the ensemble
    ensemble = EnsembleSecurityAnalyzer([
        PatternSecurityAnalyzer(),
        PolicyRailSecurityAnalyzer(),
    ])

    # Assess an action
    assessment = ensemble.analyze("rm -rf /", context={"tool": "bash"})

    # Apply confirmation policy
    policy = ConfirmRisky(threshold=SecurityRisk.MEDIUM)
    decision = policy.requires_confirmation(assessment)

    # Encrypt sensitive data
    cipher = Cipher()
    token = cipher.encrypt("secret-value")
    plain = cipher.decrypt(token)
"""

from app.security.base import (
    RiskAssessment,
    SecurityAnalyzerBase,
    SecurityRisk,
    analyze_sequence,
    sanitise_message,
)
from app.security.pattern import PatternSecurityAnalyzer
from app.security.policy_rails import PolicyRailSecurityAnalyzer
from app.security.llm_analyzer import LLMSecurityAnalyzer
from app.security.ensemble import EnsembleSecurityAnalyzer
from app.security.confirmation_policy import (
    ConfirmationDecision,
    ConfirmationPolicy,
    ConfirmRisky,
    NeverConfirm,
)
from app.security.cipher import (
    FERNET_TOKEN_PREFIX,
    Cipher,
    CipherError,
    CipherKeyError,
    CipherTokenError,
    decrypt,
    encrypt,
    get_default_cipher,
    is_encrypted,
)

__all__ = [
    # Core types
    "SecurityRisk",
    "RiskAssessment",
    "SecurityAnalyzerBase",
    "analyze_sequence",
    "sanitise_message",
    # Analyzers
    "PatternSecurityAnalyzer",
    "PolicyRailSecurityAnalyzer",
    "LLMSecurityAnalyzer",
    "EnsembleSecurityAnalyzer",
    # Confirmation policies
    "ConfirmationPolicy",
    "ConfirmationDecision",
    "NeverConfirm",
    "ConfirmRisky",
    # Cipher
    "FERNET_TOKEN_PREFIX",
    "Cipher",
    "CipherError",
    "CipherKeyError",
    "CipherTokenError",
    "encrypt",
    "decrypt",
    "is_encrypted",
    "get_default_cipher",
]
