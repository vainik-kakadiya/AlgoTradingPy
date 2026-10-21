"""Logging setup with secret redaction."""

from __future__ import annotations

import logging
import re

# Each pattern keeps group 1 (the label) and replaces the secret value that follows it.
_SECRET_PATTERNS = [
    re.compile(r"((?:access_token|api_secret|request_token|api_key)['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{6,}", re.I),
    re.compile(r"(token\s+)[A-Za-z0-9_\-]+:[A-Za-z0-9_\-]{6,}", re.I),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Kite / twisted can be chatty and may log URLs containing tokens.
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
