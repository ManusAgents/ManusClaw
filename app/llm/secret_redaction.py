from __future__ import annotations
"""Secret redaction — scrubs API keys and tokens from log output."""
import re
from typing import Optional

_PATTERNS = [
    # Generic API keys
    (r"(sk-[A-Za-z0-9]{20,})", None),
    (r"(Bearer\s+[A-Za-z0-9_\-\.]{20,})", None),
    # FIX: Patterns with prefix capture groups must use backreferences
    # so the prefix (e.g., "token=" or "api_key: ") is preserved in the
    # output. Without \1, the prefix gets replaced along with the secret.
    (r'(token[\s:=]+[\'"]?)([A-Za-z0-9_\-]{20,})', r'\1'),
    (r'(api[_-]?key[\s:=]+[\'"]?)([A-Za-z0-9_\-]{20,})', r'\1'),
    (r'(password[\s:=]+[\'"]?)([^\s\'"]{8,})', r'\1'),
    # AWS — access key ID (starts with AKIA)
    (r"(AKIA[A-Z0-9]{16})", None),
    # FIX: AWS secret key pattern — was too broad (matched ANY 40-char base64
    # string including valid content). Now requires a leading context marker
    # (secret, secret_key, aws_secret) to avoid false positives.
    (r'(?:secret[_-]?key[\s:=]+[\'"]?|aws[_-]?secret[\s:=]+[\'"]?)([A-Za-z0-9/+]{40})', None),
]


def redact(text: str, replacement: str = "***REDACTED***") -> str:
    """Replace secrets in text with a placeholder.

    FIX: For patterns with capture groups (prefix + secret), the prefix
    is preserved using backreferences so that e.g. "token=abc123" becomes
    "token=***REDACTED***" instead of just "***REDACTED***".
    """
    if not text:
        return text
    result = text
    for entry in _PATTERNS:
        if isinstance(entry, str):
            pattern, prefix_group = entry, None
        else:
            pattern, prefix_group = entry
        try:
            if prefix_group:
                # Pattern has a prefix capture group — preserve it
                result = re.sub(pattern, prefix_group + replacement, result, flags=re.IGNORECASE)
            else:
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
