"""Logging setup with a redaction filter so credentials never reach the log."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    # Telegram bot tokens: 123456789:AA... — note there is no \b anchor, because
    # the token most often shows up glued to a path, as in ".../bot123456789:AA...".
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
    # Viber auth tokens: 32 hex chars, a dash, more hex
    re.compile(r"\b[0-9a-f]{16}-[0-9a-f]{16}-[0-9a-f]{16}\b"),
    # Bearer / OAuth tokens and anything that looks like one in a query string
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(access_token|refresh_token|client_secret|code|state|auth_token|api_key)"
        r"([\"']?\s*[=:]\s*[\"']?)([A-Za-z0-9._~+/=-]{8,})"
    ),
]

REDACTED = "***redacted***"


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(rf"\1\2{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs secrets from both the format string and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These are chatty and mostly repeat what we already log ourselves.
    for noisy in ("aiogram.event", "aiosqlite", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
