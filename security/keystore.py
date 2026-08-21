"""Windows DPAPI multi-key store for Gemini API keys.

Plaintext gemini_api_key is migrated on first load. Ciphertext never logged.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from memory.config_manager import (
    CONFIG_FILE,
    ensure_config_dir,
    load_api_keys,
    mask_api_key,
)

try:
    import win32crypt  # type: ignore
except Exception:
    win32crypt = None  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _protect(plain: str) -> str:
    if not plain:
        return ""
    if win32crypt is None:
        # Non-Windows fallback: still avoid leaving the key under the old field name.
        return "plain:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")
    blob = win32crypt.CryptProtectData(plain.encode("utf-8"), "AthenaGemini", None, None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def _unprotect(cipher: str) -> str:
    if not cipher:
        return ""
    if cipher.startswith("plain:"):
        return base64.b64decode(cipher[6:].encode("ascii")).decode("utf-8")
    if win32crypt is None:
        raise RuntimeError("DPAPI is not available on this system")
    blob = base64.b64decode(cipher.encode("ascii"))
    result = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    data = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else result
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def _keys(data: dict) -> list[dict[str, Any]]:
    rows = data.get("gemini_keys")
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def migrate_plaintext(data: dict | None = None) -> dict:
    """If gemini_api_key exists in plaintext, encrypt and move it into gemini_keys."""
    ensure_config_dir()
    data = dict(data if data is not None else (load_api_keys() or {}))
    raw = str(data.get("gemini_api_key") or "").strip()
    rows = _keys(data)
    if raw and not rows:
        kid = uuid.uuid4().hex[:10]
        rows = [{
            "id": kid,
            "label": "Primary",
            "enabled": True,
            "created_at": _now(),
            "last_validated_at": "",
            "ciphertext": _protect(raw),
        }]
        data["gemini_keys"] = rows
        data["active_gemini_key_id"] = kid
        data.pop("gemini_api_key", None)
        CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        return data
    if raw and rows:
        # Already have structured keys — drop leftover plaintext.
        data.pop("gemini_api_key", None)
        CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    if rows and not str(data.get("active_gemini_key_id") or ""):
        enabled = next((r for r in rows if r.get("enabled")), rows[0])
        data["active_gemini_key_id"] = enabled.get("id")
        CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return data


def list_keys() -> list[dict[str, Any]]:
    data = migrate_plaintext()
    active = str(data.get("active_gemini_key_id") or "")
    out = []
    for r in _keys(data):
        out.append({
            "id": r.get("id"),
            "label": r.get("label") or "Key",
            "enabled": bool(r.get("enabled", True)),
            "created_at": r.get("created_at") or "",
            "last_validated_at": r.get("last_validated_at") or "",
            "active": r.get("id") == active,
            "masked": mask_api_key(_safe_plain(r)),
        })
    return out


def _safe_plain(row: dict) -> str:
    try:
        return _unprotect(str(row.get("ciphertext") or ""))
    except Exception:
        return ""


def get_active_key() -> str | None:
    data = migrate_plaintext()
    active = str(data.get("active_gemini_key_id") or "")
    for r in _keys(data):
        if r.get("id") == active and r.get("enabled", True):
            plain = _safe_plain(r)
            return plain or None
    # Fallback: first enabled
    for r in _keys(data):
        if r.get("enabled", True):
            plain = _safe_plain(r)
            if plain:
                return plain
    leftover = str((load_api_keys() or {}).get("gemini_api_key") or "").strip()
    return leftover or None


def add_key(plain: str, label: str = "") -> dict[str, Any]:
    plain = (plain or "").strip()
    if len(plain) < 16:
        raise ValueError("That does not look like a complete API key.")
    data = migrate_plaintext()
    rows = _keys(data)
    kid = uuid.uuid4().hex[:10]
    row = {
        "id": kid,
        "label": (label or f"Key {len(rows) + 1}").strip(),
        "enabled": True,
        "created_at": _now(),
        "last_validated_at": "",
        "ciphertext": _protect(plain),
    }
    rows.append(row)
    data["gemini_keys"] = rows
    if not data.get("active_gemini_key_id"):
        data["active_gemini_key_id"] = kid
    data.pop("gemini_api_key", None)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return {"id": kid, "label": row["label"], "masked": mask_api_key(plain)}


def set_active(key_id: str) -> None:
    data = migrate_plaintext()
    ids = {r.get("id") for r in _keys(data)}
    if key_id not in ids:
        raise ValueError("Unknown key.")
    for r in _keys(data):
        if r.get("id") == key_id and not r.get("enabled", True):
            r["enabled"] = True
    data["active_gemini_key_id"] = key_id
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def set_enabled(key_id: str, enabled: bool) -> None:
    data = migrate_plaintext()
    rows = _keys(data)
    found = False
    for r in rows:
        if r.get("id") == key_id:
            r["enabled"] = bool(enabled)
            found = True
    if not found:
        raise ValueError("Unknown key.")
    if not enabled and data.get("active_gemini_key_id") == key_id:
        nxt = next((r["id"] for r in rows if r.get("enabled") and r.get("id") != key_id), "")
        data["active_gemini_key_id"] = nxt
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def remove_key(key_id: str) -> None:
    data = migrate_plaintext()
    rows = [r for r in _keys(data) if r.get("id") != key_id]
    data["gemini_keys"] = rows
    if data.get("active_gemini_key_id") == key_id:
        data["active_gemini_key_id"] = rows[0]["id"] if rows else ""
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def mark_validated(key_id: str) -> None:
    data = migrate_plaintext()
    for r in _keys(data):
        if r.get("id") == key_id:
            r["last_validated_at"] = _now()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def test_key(plain: str | None = None, key_id: str | None = None) -> tuple[bool, str]:
    """Tiny Flash ping. Never logs the key."""
    secret = (plain or "").strip()
    if not secret and key_id:
        data = migrate_plaintext()
        for r in _keys(data):
            if r.get("id") == key_id:
                secret = _safe_plain(r)
                break
    if not secret:
        secret = get_active_key() or ""
    if len(secret) < 16:
        return False, "No API key to test."
    try:
        from google import genai
        from core.gemini_models import get_flash_model
        client = genai.Client(api_key=secret)
        resp = client.models.generate_content(
            model=get_flash_model(),
            contents="Reply with the single word OK.",
        )
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:
            text = "ok"
        if key_id:
            mark_validated(key_id)
        elif data_active_id():
            mark_validated(data_active_id())
        return True, "Key works." if text else "Key works."
    except Exception as e:
        msg = str(e)
        if "API key" in msg or "1007" in msg or "INVALID" in msg.upper():
            return False, "That key was rejected."
        return False, "Could not reach Gemini to test the key."


def data_active_id() -> str:
    return str((migrate_plaintext() or {}).get("active_gemini_key_id") or "")
