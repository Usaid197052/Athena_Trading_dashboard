"""Gemini models for nested (non-Live) tool calls and the voice session.

Defaults are the IDs that work for this API key. Users can override them in
Settings → API Keys & Models; values are stored in config/api_keys.json.
"""

from __future__ import annotations

DEFAULT_FLASH = "gemini-3.6-flash"
DEFAULT_FLASH_LITE = "gemini-3.5-flash-lite"
DEFAULT_LIVE = "models/gemini-2.5-flash-native-audio-preview-12-2025"

# Shown in the settings combo (value, label). Combos are editable for custom IDs.
FLASH_MODELS: list[tuple[str, str]] = [
    ("gemini-3.6-flash", "3.6 Flash — files / tools (recommended)"),
    ("gemini-3.5-flash", "3.5 Flash"),
    ("gemini-flash-latest", "Flash latest (Google alias)"),
]

LITE_MODELS: list[tuple[str, str]] = [
    ("gemini-3.5-flash-lite", "3.5 Flash Lite — vision (recommended)"),
    ("gemini-flash-lite-latest", "Flash Lite latest"),
    ("gemini-3.5-flash", "3.5 Flash"),
]

LIVE_MODELS: list[tuple[str, str]] = [
    (
        "models/gemini-2.5-flash-native-audio-preview-12-2025",
        "Native audio 2.5 Flash (voice)",
    ),
]


def _cfg() -> dict:
    try:
        from memory.config_manager import load_api_keys
        return load_api_keys() or {}
    except Exception:
        return {}


def get_flash_model() -> str:
    v = str(_cfg().get("gemini_flash_model") or DEFAULT_FLASH).strip()
    return v or DEFAULT_FLASH


def get_flash_lite_model() -> str:
    v = str(_cfg().get("gemini_flash_lite_model") or DEFAULT_FLASH_LITE).strip()
    return v or DEFAULT_FLASH_LITE


def get_live_model() -> str:
    v = str(_cfg().get("gemini_live_model") or DEFAULT_LIVE).strip()
    if v and not v.startswith("models/") and "native-audio" in v:
        v = "models/" + v.lstrip("/")
    return v or DEFAULT_LIVE


# Import-time fallbacks (prefer the get_* helpers so Settings changes apply).
GEMINI_FLASH = DEFAULT_FLASH
GEMINI_FLASH_LITE = DEFAULT_FLASH_LITE
