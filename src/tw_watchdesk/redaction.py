from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "authtoken",
    "realtimetoken",
    "token",
    "account",
    "certno",
    "cert",
    "nationalid",
    "pin",
    "password",
    "name",
)

_KEY_VALUE_RE = re.compile(
    r"(?i)\b(authorization|authToken|realtimeToken|token|account|certNo|nationalId|pin|password|name)\b\s*[:=]\s*([^\s,;}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TW_ID_RE = re.compile(r"\b[A-Z][12]\d{8}\b")
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_CERT_PATH_RE = re.compile(r"(?i)[A-Z]:\\[^\s]+\\[^\\\s]+\.(pfx|p12|pem|key|crt)\b")


def redact_text(value: str) -> str:
    text = str(value)
    text = _CERT_PATH_RE.sub("[REDACTED_CERT_PATH]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _TW_ID_RE.sub("[REDACTED_NATIONAL_ID]", text)
    text = _LONG_SECRET_RE.sub(_redact_long_secret, text)
    return text


def redact_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_json(item)
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_json(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def contains_sensitive_text(value: str) -> bool:
    redacted = redact_text(value)
    return redacted != value


def _redact_long_secret(match: re.Match[str]) -> str:
    token = match.group(0)
    has_alpha = any(char.isalpha() for char in token)
    has_digit = any(char.isdigit() for char in token)
    if has_alpha and has_digit:
        return "[REDACTED_SECRET]"
    return token
