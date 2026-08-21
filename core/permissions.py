"""
Permission decisions for Athena tools — risk levels + voice/text confirmation.
Action-aware for compound tools (e.g. file_controller list = low, delete = high).
User confirms by saying phrases like "go ahead" / "permission granted", or "abort".
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PermissionDecision:
    allowed: bool
    requires_confirmation: bool
    risk_level: RiskLevel
    reason: str
    summary: str = ""


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_CONFIG_PATH = _base_dir() / "config" / "permissions.json"
_DEFAULTS = {
    "auto_execute_low_risk": True,
    "confirm_medium_risk": True,
    "confirm_high_risk": True,
    "deny_critical": True,
    "confirm_timeout_seconds": 120,
}

_session_allow: set[str] = set()


def clear_session_allows() -> None:
    _session_allow.clear()


def remember_session_allow(tool_name: str, action: str = "") -> None:
    key = f"{tool_name}:{action}" if action else tool_name
    _session_allow.add(key)
    # Compound tools: do not unlock delete/copy just because create_folder was granted.
    if tool_name not in (
        "file_controller", "explorer_navigate", "computer_settings",
    ):
        _session_allow.add(tool_name)


def _load_settings() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
    except Exception:
        pass
    return dict(_DEFAULTS)


def get_confirm_timeout() -> float:
    return float(_load_settings().get("confirm_timeout_seconds", 120))


_GRANT_PHRASES = (
    "permission granted",
    "permission is granted",
    "go ahead",
    "go a head",
    "proceed",
    "you may proceed",
    "you can proceed",
    "please proceed",
    "all right",
    "you may",
    "may proceed",
    "yes proceed",
    "yes go ahead",
    "yes you may",
    "permission grant",
    "do it",
    "confirmed",
    "yes go",
    "allow it",
    "allowed",
    "try again",
    "try it again",
    "do it again",
    "go for it",
    "go on",
    "carry on",
    "izin ver",
    "izin veriyorum",
    "devam et",
    "devam",
    "onaylıyorum",
    "onayladim",
    "onayladım",
    "yapabilirsin",
    "aage badho",
    "age badho",
    "ijazat",
    "परमिशन ग्रांटेड",
    "परमिशन",
    "ग्रांटेड",
    "आगे बढ़ो",
    "आगे बढो",
    "गो अहेड",
    "अहेड",
    "हाँ",
    "हां",
    "हा",
    "ہاں",
    "اجازت",
    "آگے بڑھو",
    "ഗോ എഹെഡ്",
    "ഗോ അഹെഡ്",
    "ശരി",
    "go ahead",
)

_DENY_PHRASES = (
    "abort",
    "cancel",
    "deny",
    "denied",
    "don't",
    "do not",
    "stop",
    "never mind",
    "nevermind",
    "no thanks",
    "iptal",
    "vazgeç",
    "vazgec",
    "hayır",
    "hayir",
    "olmaz",
)

_GRANT_EXACT = {
    "yes", "yep", "yeah", "yup", "ok", "okay", "sure", "allow",
    "ahead", "proceed", "confirmed", "granted", "alright",
    "evet", "tamam",
    "हाँ", "हां", "हा", "ہاں", "ശരി", "अहेड",
}
_DENY_EXACT = {"no", "nope", "nah", "deny"}

# Compact forms (spaces stripped) for broken STT like "O kay" / "go a head" / "aga in"
_GRANT_COMPACT = {
    "permissiongranted",
    "permissionisgranted",
    "goahead",
    "goahead",
    "proceed",
    "youmayproceed",
    "youcanproceed",
    "pleaseproceed",
    "youmay",
    "mayproceed",
    "yesyoumayproceed",
    "yesyoumay",
    "yesproceed",
    "yesgoahead",
    "yesgo",
    "doit",
    "confirmed",
    "permissiongrant",
    "allowit",
    "allowed",
    "tryagain",
    "tryitagain",
    "doitagain",
    "goforit",
    "goon",
    "carryon",
    "okaytryagain",
    "oktryagain",
    "okaygoahead",
    "okgoahead",
    "izinver",
    "izinveriyorum",
    "devamet",
    "onayliyorum",
    "onayladim",
    "yapabilirsin",
    "aagebadho",
    "agebadho",
    "ijazat",
    "अहेड",
    "गोअहेड",
    "हाँ",
    "हां",
    "ہاں",
    "اجازت",
    "آگےبڑھو",
    "ഗോഎഹെഡ്",
    "ഗോഅഹെഡ്",
    "ശരി",
}
_DENY_COMPACT = {
    "abort",
    "cancel",
    "deny",
    "denied",
    "dont",
    "donot",
    "stop",
    "nevermind",
    "nothanks",
    "iptal",
    "vazgec",
    "hayir",
    "olmaz",
}


def _normalize_permission_text(text: str) -> tuple[str, str]:
    """Return (spaced lowercase, compact no-space) for matching.

    Keep combining marks (Hindi/Urdu matras). Stripping them with [^\\w]
    turns 'परमिशन ग्रांटेड' into a non-match.
    """
    raw = (text or "").strip().lower()
    kept: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch.isspace() or ch in "'’":
            kept.append(ch)
            continue
        if unicodedata.category(ch).startswith("M"):
            kept.append(ch)
            continue
        kept.append(" ")
    cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
    compact = re.sub(r"[\s'_]+", "", cleaned)
    return cleaned, compact


def classify_user_permission_reply(text: str) -> str | None:
    """
    Return 'grant', 'deny', or None if not a clear permission reply.
    Tolerates broken STT spacing (e.g. 'O kay try aga in' → okay/tryagain).
    """
    cleaned, compact = _normalize_permission_text(text)
    raw_lower = (text or "").strip().lower()
    if not cleaned and not raw_lower:
        return None

    # Short STT abort only — do not match "tell me about …"
    tokens = cleaned.split()
    if 1 <= len(tokens) <= 3:
        joined_all = "".join(tokens)
        if joined_all in {"about", "abort", "abot", "aabort"}:
            return "deny"
        if compact in {"about", "abort", "abot", "अबाउट", "ਅਬੋਰਟ"}:
            return "deny"
        if joined_all in {"अबाउट", "ਅਬੋਰਟ"} or compact in {"अबाउट", "ਅਬੋਰਟ"}:
            return "deny"

    # Deny first (prefer abort over accidental grant)
    for p in _DENY_PHRASES:
        if p in cleaned or p in raw_lower:
            return "deny"
    for p in _DENY_COMPACT:
        if p in compact:
            return "deny"

    for p in _GRANT_PHRASES:
        if p in cleaned or p in raw_lower:
            return "grant"
    for p in _GRANT_COMPACT:
        if p in compact:
            return "grant"

    # Transliterated Hindi/Urdu/Malayalam "go ahead" fragments
    if any(x in compact for x in ("अहेड", "गोअहेड", "ഗോഎഹെഡ", "ഗോഅഹെഡ", "ശരി", "हाँ", "हां", "ہاں", "اجازت")):
        return "grant"

    # STT often splits okay → "o kay", again → "aga in", proceed → "pro ceed"
    if "okay" in compact or compact.startswith("ok"):
        if any(x in compact for x in ("try", "again", "goahead", "proceed", "delete", "allow", "grant")):
            return "grant"
    if "tryagain" in compact or ("try" in compact and "again" in compact):
        return "grant"
    if "goahead" in compact or ("go" in compact and "ahead" in compact):
        return "grant"
    if "proceed" in compact or ("may" in compact and "proceed" in compact):
        return "grant"
    if "youmay" in compact:
        return "grant"

    tokens = cleaned.split()
    # Short confirmations — allow a few filler words (sir, please, then)
    fillers = {"sir", "please", "then", "just", "efendim", "the", "a", "to"}
    content = [t for t in tokens if t not in fillers]
    if len(content) <= 4:
        for t in content:
            if t in _DENY_EXACT:
                return "deny"
        for t in content:
            if t in _GRANT_EXACT:
                return "grant"
        # Compact tokens for split words: o+kay, aga+in
        if len(tokens) >= 2:
            joined = "".join(tokens)
            if any(g in joined for g in ("okay", "goahead", "tryagain", "permissiongranted")):
                return "grant"
            if any(d in joined for d in ("abort", "cancel", "nevermind")):
                return "deny"

    return None


def tool_permission_key(tool_name: str, args: dict[str, Any] | None = None) -> str:
    args = args or {}
    action = str(args.get("action", "")).strip()
    return f"{tool_name}:{action}" if action else tool_name


_LOW_TOOLS = {
    "open_app",
    "web_search",
    "weather_report",
    "screen_process",
    "share_screen",
    "close_camera",
    "youtube_video",
    "system_status",
    "manage_monitor",
    "manage_continuous_monitor",
    "save_memory",
    "flight_finder",
    "spotify_control",
    "shutdown_athena",
    "shutdown_Athena",
    "shutdown_jarvis",
    "sleep_assistant",
    "desktop_control",
    "browser_control",
    "computer_control",
    "file_processor",
    "show_dataframe",
    "reminder",
    "game_updater",
    "mt5_analysis",
    "trading_desk",
    "trading_control",
}

_HIGH_TOOLS = {
    "shell_command",
}

_MEDIUM_TOOLS = {
    "code_helper",
    "dev_agent",
}


def _whatsapp_control_risk(action: str) -> RiskLevel:
    a = (action or "compose").lower().strip().replace(" ", "_").replace("-", "_")
    if a in ("send", "auto_reply"):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _send_message_risk(action: str) -> RiskLevel:
    a = (action or "compose").lower().strip().replace(" ", "_").replace("-", "_")
    if a in ("send",):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _file_controller_risk(action: str) -> RiskLevel:
    a = (action or "").lower().strip()
    if a in ("delete",):
        return RiskLevel.HIGH
    if a in (
        "create_file", "create_folder", "write", "move", "copy", "paste",
        "rename", "organize_desktop",
    ):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _computer_settings_risk(action: str, description: str = "") -> RiskLevel:
    a = (action or "").lower().strip().replace(" ", "_").replace("-", "_")
    desc = (description or "").lower()
    combined = f"{a} {desc}"
    if any(x in combined for x in ("shutdown", "restart", "sleep")):
        return RiskLevel.HIGH
    return RiskLevel.LOW


def _explorer_navigate_risk(action: str) -> RiskLevel:
    # Visual browse only (look / open_folder / go_up). Destructive file ops
    # go through file_controller, which still confirms delete.
    return RiskLevel.LOW


def classify_risk(tool_name: str, args: dict[str, Any] | None = None) -> RiskLevel:
    args = args or {}
    name = (tool_name or "").strip()

    if name == "file_controller":
        return _file_controller_risk(str(args.get("action", "")))

    if name == "computer_settings":
        return _computer_settings_risk(
            str(args.get("action", "")),
            str(args.get("description", "")),
        )

    if name == "explorer_navigate":
        return _explorer_navigate_risk(str(args.get("action", "")))

    if name == "send_message":
        return _send_message_risk(str(args.get("action", "compose")))

    if name == "whatsapp_control":
        return _whatsapp_control_risk(str(args.get("action", "compose")))

    if name in _HIGH_TOOLS:
        return RiskLevel.HIGH

    if name in _MEDIUM_TOOLS:
        return RiskLevel.MEDIUM

    if name in _LOW_TOOLS:
        return RiskLevel.LOW

    return RiskLevel.MEDIUM


def _build_summary(tool_name: str, args: dict[str, Any] | None) -> str:
    args = args or {}
    action = str(args.get("action", "")).strip()
    parts = [tool_name]
    if action:
        parts.append(action)
    for key in ("path", "name", "command", "app_name", "application_name",
                "folder", "query", "receiver", "contact", "description"):
        val = args.get(key)
        if val:
            parts.append(f"{key}={str(val)[:80]}")
            break
    return " · ".join(parts)


def evaluate_permission(
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> PermissionDecision:
    args = args or {}
    settings = _load_settings()
    risk = classify_risk(tool_name, args)
    summary = _build_summary(tool_name, args)
    action = str(args.get("action", "")).strip()

    session_key = f"{tool_name}:{action}" if action else tool_name
    if tool_name in _session_allow or session_key in _session_allow:
        return PermissionDecision(
            allowed=True,
            requires_confirmation=False,
            risk_level=risk,
            reason="Allowed for this session.",
            summary=summary,
        )

    if risk == RiskLevel.CRITICAL:
        denied = bool(settings.get("deny_critical", True))
        return PermissionDecision(
            allowed=not denied,
            requires_confirmation=True,
            risk_level=risk,
            reason="Critical actions are denied by default." if denied else "Critical — confirm.",
            summary=summary,
        )

    if risk == RiskLevel.LOW:
        auto = bool(settings.get("auto_execute_low_risk", True))
        return PermissionDecision(
            allowed=True,
            requires_confirmation=not auto,
            risk_level=risk,
            reason="Low risk auto-execute.",
            summary=summary,
        )

    if risk == RiskLevel.MEDIUM:
        need = bool(settings.get("confirm_medium_risk", True))
        return PermissionDecision(
            allowed=True,
            requires_confirmation=need,
            risk_level=risk,
            reason="Medium risk requires confirmation.",
            summary=summary,
        )

    need = bool(settings.get("confirm_high_risk", True))
    return PermissionDecision(
        allowed=True,
        requires_confirmation=need,
        risk_level=risk,
        reason="High risk requires confirmation.",
        summary=summary,
    )


def requires_confirmation(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    d = evaluate_permission(tool_name, args)
    return d.allowed and d.requires_confirmation
