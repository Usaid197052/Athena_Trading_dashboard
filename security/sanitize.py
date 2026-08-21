"""Redact secrets before they reach logs, HUD, or debug files."""
from __future__ import annotations

import re
from typing import Any

_AIza = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
_KEY_JSON = re.compile(
    r'(?i)("(?:gemini_api_key|api_key|ciphertext|password|token)"\s*:\s*")[^"]+(")'
)
_QUERY_KEY = re.compile(r"(?i)(key=)[A-Za-z0-9_\-]{8,}")


def sanitize(text: str) -> str:
    if not text:
        return text
    s = _AIza.sub("AIza…REDACTED", text)
    s = _BEARER.sub(r"\1REDACTED", s)
    s = _KEY_JSON.sub(r"\1REDACTED\2", s)
    s = _QUERY_KEY.sub(r"\1REDACTED", s)
    return s


def sanitize_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return sanitize(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"gemini_api_key", "api_key", "ciphertext", "password", "token", "key"}:
                out[k] = "REDACTED"
            else:
                out[k] = sanitize_obj(v)
        return out
    if isinstance(obj, list):
        return [sanitize_obj(x) for x in obj]
    return obj
