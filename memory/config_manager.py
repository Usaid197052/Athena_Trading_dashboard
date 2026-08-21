import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    """Store a Gemini key via the encrypted keystore (falls back to JSON)."""
    key = (gemini_api_key or "").strip()
    if not key:
        return
    try:
        from security.keystore import add_key, set_active
        info = add_key(key, label="Primary")
        set_active(str(info.get("id") or ""))
        return
    except Exception:
        pass
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["gemini_api_key"] = key
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    try:
        from security.keystore import get_active_key
        active = get_active_key()
        if active:
            return active
    except Exception:
        pass
    raw = load_api_keys().get("gemini_api_key")
    return raw if raw else None


def mask_api_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "(not set)"
    if len(k) <= 12:
        return k[:3] + "…"
    return f"{k[:8]}…{k[-4:]}"


def update_config(updates: dict) -> dict:
    """Merge keys into api_keys.json and write. Empty-string values are skipped."""
    ensure_config_dir()
    data = load_api_keys()
    for key, val in (updates or {}).items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip() and key == "gemini_api_key":
            continue
        if key == "gemini_api_key" and isinstance(val, str) and val.strip():
            try:
                from security.keystore import add_key, set_active
                info = add_key(val.strip(), label="Updated")
                set_active(str(info.get("id") or ""))
                data = load_api_keys()
                continue
            except Exception:
                pass
        data[key] = val.strip() if isinstance(val, str) else val
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return data

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


DEFAULT_ASSISTANT_NAME = "Athena"


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'Athena' if not set."""
    return load_api_keys().get("assistant_name", DEFAULT_ASSISTANT_NAME) or DEFAULT_ASSISTANT_NAME


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or DEFAULT_ASSISTANT_NAME
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_content_panel_enabled() -> bool:
    return load_api_keys().get("content_panel_enabled", True)


def save_content_panel_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["content_panel_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")