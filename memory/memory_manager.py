import json
import re
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = Lock()
MAX_VALUE_LENGTH = 380
STORE_MAX_CHARS  = 12_000
PROMPT_MAX_CHARS = 2200
LEARNED_PROMPT_MAX = 700

# Backward-compat alias for anything still importing the old name
MEMORY_MAX_CHARS = STORE_MAX_CHARS

_PROTECTED_CATS = frozenset({"identity", "learned", "sessions"})
_TRIM_ORDER = ("notes", "wishes", "relationships", "projects", "preferences")
_RESPONSE_LANG_KEYS = frozenset({"response_language", "language_preference"})
_session_response_language = "English"


def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
        "learned":       {},
    }


def _snake_case(key: str) -> str:
    s = str(key or "").strip()
    if not s:
        return "untitled"
    s = s.replace("-", "_").replace(" ", "_")
    s = re.sub(r"[^\w]", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "untitled"


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()


def _all_entries(memory: dict, categories: tuple[str, ...] | None = None) -> list[tuple]:
    entries = []
    cats = categories if categories is not None else tuple(
        k for k in memory if isinstance(memory.get(k), dict) and k != "sessions"
    )
    for cat in cats:
        items = memory.get(cat, {})
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= STORE_MAX_CHARS:
        return memory
    # Prefer trimming notes/wishes first; never touch protected categories
    for cat in _TRIM_ORDER:
        entries = _all_entries(memory, (cat,))
        entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
        for c, key, _ in entries:
            if len(json.dumps(memory, ensure_ascii=False)) <= STORE_MAX_CHARS:
                return memory
            if c in _PROTECTED_CATS:
                continue
            del memory[c][key]
            print(f"[Memory] 🗑️  Trimmed {c}/{key}")
    # Last resort: trim remaining non-protected dict categories by age
    entries = [
        e for e in _all_entries(memory)
        if e[0] not in _PROTECTED_CATS
    ]
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= STORE_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    return memory


def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _normalize_update_keys(updates: dict, *, top_level: bool = True) -> dict:
    """Snake_case leaf keys; keep top-level category names (identity, learned, …) as-is."""
    out: dict = {}
    for key, value in updates.items():
        keep = top_level and (key in _empty_memory() or key == "sessions")
        nk = key if keep else _snake_case(key)
        if isinstance(value, dict) and "value" not in value:
            out[nk] = _normalize_update_keys(value, top_level=False)
        else:
            out[nk] = value
    return out


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def _strip_session_language(updates: dict) -> str | None:
    """Pull reply-language keys out of a memory update so they never persist."""
    prefs = updates.get("preferences")
    if not isinstance(prefs, dict):
        return None
    lang = None
    for key in list(prefs):
        if _snake_case(key) not in _RESPONSE_LANG_KEYS:
            continue
        val = _entry_val(prefs.pop(key, None))
        if val:
            lang = val
    if not prefs:
        updates.pop("preferences", None)
    return lang


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory_update = _normalize_update_keys(memory_update)
    lang = _strip_session_language(memory_update)
    if lang:
        set_response_language(lang)
        print(f"[Memory] 🌐 session language: {lang} (not persisted)")
    if not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory


def _entry_val(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value") or "").strip()
    return str(entry or "").strip()


def _append_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    if lines and lines[-1] != "":
        lines.append("")
    lines.append(title)
    lines.extend(items)


def _format_learned_block(learned: dict) -> list[str]:
    """Compact learned routines / today / lessons for the prompt pack."""
    if not isinstance(learned, dict) or not learned:
        return []
    out: list[str] = []
    routines = learned.get("routines", {})
    if isinstance(routines, dict) and routines:
        bits = []
        for key in ("work_hours", "primary_apps"):
            val = _entry_val(routines.get(key))
            if val:
                bits.append(f"{key.replace('_', ' ')}: {val}")
        for key, entry in routines.items():
            if key in ("work_hours", "primary_apps"):
                continue
            val = _entry_val(entry)
            if val:
                bits.append(f"{key.replace('_', ' ')}: {val}")
        if bits:
            out.append("Routines: " + "; ".join(bits[:6]))

    today = learned.get("today", {})
    if isinstance(today, dict):
        val = _entry_val(today) if "value" in today else _entry_val(today.get("summary") or today.get("apps"))
        if not val and today:
            # flat map of app -> minutes style
            parts = []
            for k, e in list(today.items())[:5]:
                v = _entry_val(e)
                if v:
                    parts.append(f"{k.replace('_', ' ')} {v}")
            if parts:
                val = ", ".join(parts)
        if val:
            out.append(f"Today: {val}")
    elif isinstance(today, str) and today.strip():
        out.append(f"Today: {today.strip()}")

    lessons = learned.get("lessons", {})
    if isinstance(lessons, dict) and lessons:
        lesson_lines = []
        # Prefer newest by updated date
        items = []
        for key, entry in lessons.items():
            val = _entry_val(entry)
            if not val:
                continue
            updated = entry.get("updated", "0000-00-00") if isinstance(entry, dict) else "0000-00-00"
            items.append((updated, key, val))
        items.sort(reverse=True)
        for _, key, val in items[:4]:
            lesson_lines.append(f"  - {val}")
        if lesson_lines:
            out.append("Lessons:")
            out.extend(lesson_lines)

    # Flat learned keys from save_memory (e.g. learned/work_hours)
    for key, entry in learned.items():
        if key in ("routines", "today", "lessons"):
            continue
        val = _entry_val(entry)
        if val:
            out.append(f"{key.replace('_', ' ').title()}: {val}")

    # Cap learned block size
    text = "\n".join(out)
    if len(text) > LEARNED_PROMPT_MAX:
        # Keep as many full lines as fit
        kept: list[str] = []
        size = 0
        for line in out:
            add = len(line) + (1 if kept else 0)
            if size + add > LEARNED_PROMPT_MAX:
                break
            kept.append(line)
            size += add
        out = kept
    return out


def _pack_lines(header: str, sections: list[list[str]], max_chars: int) -> str:
    """Join sections in order; drop overflow at line boundaries."""
    lines: list[str] = []
    budget = max_chars - len(header) - 1
    if budget < 40:
        return header + "\n"

    for section in sections:
        if not section:
            continue
        # tentative add with blank separator
        candidate = list(lines)
        if candidate and candidate[-1] != "":
            candidate.append("")
        candidate.extend(section)
        packed = "\n".join(candidate)
        if len(packed) <= budget:
            lines = candidate
            continue
        # Add line-by-line until budget
        if lines and lines[-1] != "":
            sep = ""
            if len("\n".join(lines)) + 1 <= budget:
                lines.append("")
        for line in section:
            trial = "\n".join(lines + [line]) if lines else line
            if len(trial) > budget:
                return header + "\n".join(lines) + "\n"
            lines.append(line)
        break  # after first overflow section, stop lower-priority ones

    if not lines:
        return ""
    return header + "\n".join(lines) + "\n"


def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"

    # 1. Identity
    identity_lines: list[str] = []
    identity = memory.get("identity", {}) or {}
    id_fields = ["name", "age", "birthday", "city", "job", "language", "spoken_language", "school", "nationality"]
    user_spoken = ""
    for field in id_fields:
        val = _entry_val(identity.get(field))
        if val:
            if field in ("language", "spoken_language"):
                if val != user_spoken:
                    identity_lines.append(f"User speaks: {val}")
                    user_spoken = val
            else:
                identity_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = _entry_val(entry)
        if val:
            identity_lines.append(f"{key.replace('_', ' ').title()}: {val}")

    # 2. Learned
    learned_lines = _format_learned_block(memory.get("learned", {}) or {})
    if learned_lines:
        learned_section = ["Learned routines (use silently; mention only when helpful):"] + learned_lines
    else:
        learned_section = []

    # 3. Last 2 session summaries (read, do not pop)
    session_lines: list[str] = []
    sessions = memory.get("sessions", [])
    if isinstance(sessions, list) and sessions:
        recent = sessions[-2:]
        bits = []
        for s in recent:
            if not isinstance(s, dict):
                continue
            summary = str(s.get("summary") or "").strip()
            date = str(s.get("date") or "").strip()
            if summary:
                bits.append(f"  - ({date}) {summary}" if date else f"  - {summary}")
        if bits:
            session_lines = ["Recent sessions:"] + bits

    # 4. Preferences + projects
    pref_lines: list[str] = []
    prefs = memory.get("preferences", {}) or {}
    if prefs:
        items = []
        for key, entry in list(prefs.items())[:15]:
            if _snake_case(key) in _RESPONSE_LANG_KEYS:
                continue
            val = _entry_val(entry)
            if val:
                items.append(f"  - {key.replace('_', ' ').title()}: {val}")
        if items:
            pref_lines = ["Preferences:"] + items

    project_lines: list[str] = []
    projects = memory.get("projects", {}) or {}
    if projects:
        items = []
        for key, entry in list(projects.items())[:8]:
            val = _entry_val(entry)
            if val:
                items.append(f"  - {key.replace('_', ' ').title()}: {val}")
        if items:
            project_lines = ["Active Projects / Goals:"] + items

    # 5. Newest relationships / notes / wishes
    def _newest_items(cat: str, title: str, limit: int = 8) -> list[str]:
        data = memory.get(cat, {}) or {}
        if not isinstance(data, dict) or not data:
            return []
        ranked = []
        for key, entry in data.items():
            val = _entry_val(entry)
            if not val:
                continue
            updated = entry.get("updated", "0000-00-00") if isinstance(entry, dict) else "0000-00-00"
            ranked.append((updated, key, val))
        ranked.sort(reverse=True)
        items = [
            f"  - {key.replace('_', ' ').title()}: {val}" if cat != "notes" else f"  - {key}: {val}"
            for _, key, val in ranked[:limit]
        ]
        return [title] + items if items else []

    rel_lines = _newest_items("relationships", "People in their life:", 10)
    note_lines = _newest_items("notes", "Other notes:", 8)
    wish_lines = _newest_items("wishes", "Wishes / Plans / Wants:", 8)

    sections = [
        identity_lines,
        learned_section,
        session_lines,
        pref_lines,
        project_lines,
        rel_lines,
        note_lines,
        wish_lines,
    ]

    result = _pack_lines(header, sections, PROMPT_MAX_CHARS)
    return result


def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes", "learned"}
    if category not in valid:
        category = "notes"
    key = _snake_case(key)
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def reset_response_language() -> None:
    """English on every application start. Session switches are not persisted."""
    global _session_response_language
    with _lock:
        _session_response_language = "English"


def set_response_language(language: str) -> str:
    """Set reply language for this process only (lost on restart)."""
    global _session_response_language
    val = (language or "").strip() or "English"
    with _lock:
        _session_response_language = val
    return val


def is_session_language_key(key: str) -> bool:
    return _snake_case(key) in _RESPONSE_LANG_KEYS


def get_response_language(memory: dict | None = None) -> str:
    """
    Language Athena speaks in this process. Default English.
    Explicit user commands can change it for the current run only.
    Never use identity/language (that is what the user speaks).
    """
    with _lock:
        return _session_response_language or "English"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    key    = _snake_case(key)
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


def forget_learned() -> str:
    """Clear the learned category only (journals cleared separately by learner)."""
    memory = load_memory()
    memory["learned"] = {}
    save_memory(memory)
    return "Cleared learned routines and lessons."


def apply_compact_patch(upsert: dict | None = None, forget_keys: list | None = None) -> None:
    """
    Apply Flash compact patch: upsert facts, forget contradicted keys.
    Never auto-delete identity.name.
    """
    if upsert and isinstance(upsert, dict):
        # Strip identity.name from upsert forget path only; upsert of name is OK if user stated it
        clean = {}
        for cat, items in upsert.items():
            if not isinstance(items, dict):
                continue
            clean[cat] = items
        if clean:
            update_memory(clean)

    if forget_keys and isinstance(forget_keys, list):
        for item in forget_keys:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            cat, key = str(item[0]), str(item[1])
            if cat == "identity" and _snake_case(key) == "name":
                continue
            forget(_snake_case(key), category=cat)


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 7


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def peek_last_session() -> dict | None:
    """
    Return the most recent unbriefed session entry and mark it briefed.
    Does NOT delete the entry — keeps continuity for Live prompt packing.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            # Prefer newest unbriefed; else newest overall once
            entry = None
            for s in reversed(sessions):
                if isinstance(s, dict) and not s.get("briefed"):
                    entry = s
                    break
            if entry is None:
                return None
            entry["briefed"] = True
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return dict(entry)
        except Exception as e:
            print(f"[Memory] ⚠️ peek_last_session error: {e}")
            return None


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Kept for compatibility; prefer peek_last_session for briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None
