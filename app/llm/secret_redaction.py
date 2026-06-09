from __future__ import annotations
"""Secret redaction — scrubs API keys and tokens from log output."""
import re
from typing import Optional

_PATTERNS = [
    # Generic API keys
    r"(sk-[A-Za-z0-9]{20,})",
    r"(Bearer\s+[A-Za-z0-9_\-\.]{20,})",
    r'(token[\s:=]+[\'"]?)([A-Za-z0-9_\-]{20,})',
    r'(api[_-]?key[\s:=]+[\'"]?)([A-Za-z0-9_\-]{20,})',
    r'(password[\s:=]+[\'"]?)([^\s\'"]{8,})',
    # AWS
    r"(AKIA[A-Z0-9]{16})",
    r"([A-Za-z0-9/+]{40})(?=[^A-Za-z0-9/+]|$)",
]


def redact(text: str, replacement: str = "***REDACTED***") -> str:
    """Replace secrets in text with a placeholder."""
    if not text:
        return text
    result = text
    for pattern in _PATTERNS:
        try:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        except Exception:
            pass
    return result


class RedactingFormatter:
    """Wrap a log message and redact secrets before output."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def format(self, msg: str) -> str:
        if not self.enabled:
            return msg
        return redact(msg)
