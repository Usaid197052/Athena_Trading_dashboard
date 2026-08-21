"""
Local phone book for WhatsApp name → number lookup.

Reads Contacts/*.vcf (primary) and Contacts/*.csv (optional Google export).
Spoken names are fuzzy-matched; numbers are normalized to WhatsApp JIDs.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sys
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_THRESHOLD = 0.55
_AMBIGUOUS_GAP = 0.08
_DEFAULT_CC = "92"

_PHONE_CSV_RE = re.compile(
    r"^(phone|mobile|cell|tel|mobile phone|home phone|business phone|work phone)"
    r"(\s+\d+)?(\s*-\s*value)?$",
    re.I,
)
_NAME_CSV_RE = re.compile(
    r"^(name|full name|display name|fn|formatted name)$",
    re.I,
)
_GIVEN_CSV_RE = re.compile(r"^(given name|first name|first|given)$", re.I)
_FAMILY_CSV_RE = re.compile(r"^(family name|last name|last|surname)$", re.I)
_NICK_CSV_RE = re.compile(r"^(nickname|nick name|alias)$", re.I)
_TYPE_CSV_RE = re.compile(r"^phone\s+\d+\s*-\s*type$", re.I)

_cache: list[dict[str, Any]] | None = None
_cache_stamp: tuple | None = None
_phone_idx: dict[str, str] | None = None
_phone_idx_stamp: tuple | None = None
_lid_map: dict[str, str] | None = None
_lid_lock = threading.Lock()
_index_rows: list[dict[str, Any]] | None = None
_index_stamp: tuple | None = None
_by_digits: dict[str, str] = {}
_by_lid: dict[str, str] = {}
_by_jid: dict[str, str] = {}
_index_lock = threading.Lock()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def contacts_dir() -> Path:
    d = _base_dir() / "Contacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def invalidate() -> None:
    global _cache, _cache_stamp, _phone_idx, _phone_idx_stamp
    global _index_rows, _index_stamp
    _cache = None
    _cache_stamp = None
    _phone_idx = None
    _phone_idx_stamp = None
    _index_rows = None
    _index_stamp = None


def loaded_count() -> int:
    _ensure_index()
    names = {str(r.get("name") or "") for r in (_index_rows or []) if r.get("name")}
    return len(names) if names else len(_load_rows())


def listed_files() -> list[str]:
    folder = contacts_dir()
    names = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in (".vcf", ".csv"):
            names.append(p.name)
    return names


def import_file(src: str | Path) -> Path:
    """Copy a VCF/CSV into Contacts/ and refresh the cache."""
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(str(src_path))
    if src_path.suffix.lower() not in (".vcf", ".csv"):
        raise ValueError("Use a .vcf or .csv contacts export.")
    dest = contacts_dir() / src_path.name
    shutil.copy2(src_path, dest)
    invalidate()
    n = loaded_count()
    print(f"[Contacts] imported {src_path.name} — {n} contact(s) in index")
    return dest


def _default_country_code() -> str:
    try:
        from memory.config_manager import load_api_keys

        data = load_api_keys()
        wa = data.get("whatsapp") if isinstance(data.get("whatsapp"), dict) else {}
        cc = str(wa.get("default_country_code") or _DEFAULT_CC).strip()
        digits = re.sub(r"\D", "", cc)
        return digits or _DEFAULT_CC
    except Exception:
        return _DEFAULT_CC


def _norm_text(s: str) -> str:
    s = (s or "").lower().replace("\xa0", " ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def normalize_phone(raw: str, country_code: str | None = None) -> str:
    """Return international digits (no +), or empty if unusable."""
    cc = country_code or _default_country_code()
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("00"):
        s = s[2:]
    digits = re.sub(r"\D", "", s)
    if len(digits) < 8 or len(digits) > 15:
        return ""
    if digits.startswith(cc):
        return digits
    # Local Pakistan-style mobiles
    if digits.startswith("0") and len(digits) == 11:
        return cc + digits[1:]
    if digits.startswith("3") and len(digits) == 10:
        return cc + digits
    # Already looks international (other country codes)
    if not digits.startswith("0") and len(digits) >= 11:
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return cc + digits.lstrip("0")
    return cc + digits


def phone_to_jid(digits: str) -> str:
    d = re.sub(r"\D", "", digits or "")
    if not d:
        return ""
    return f"{d}@s.whatsapp.net"


def _with_first_tokens(names: list[str]) -> list[str]:
    """Keep FN/NICKNAME plus the first word of each name as an extra alias."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        raw = (n or "").strip()
        if not raw:
            continue
        key = _norm_text(raw)
        if key and key not in seen:
            out.append(raw)
            seen.add(key)
    for n in list(out):
        parts = n.split()
        if len(parts) < 2:
            continue
        first = parts[0].strip()
        key = _norm_text(first)
        if key and len(key) >= 2 and key not in seen:
            out.append(first)
            seen.add(key)
    return out


def jid_to_digits(jid: str) -> str:
    """International digits from a phone JID, or empty for LID/group."""
    s = (jid or "").strip()
    if not s or s.endswith("@lid") or s.endswith("@g.us") or s.endswith("@newsletter"):
        return ""
    user = s.split("@")[0].split(":")[0]
    digits = re.sub(r"\D", "", user)
    if len(digits) < 8 or len(digits) > 15:
        return ""
    return normalize_phone(digits) or digits


def _unfold_vcf(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _decode_vcf_value(value: str, params: str) -> str:
    v = value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";")
    p = params.upper()
    if "QUOTED-PRINTABLE" in p:
        try:
            v = re.sub(
                r"=([0-9A-Fa-f]{2})",
                lambda m: bytes.fromhex(m.group(1)).decode("utf-8", "replace"),
                v.replace("=\n", "").replace("=\r\n", ""),
            )
        except Exception:
            pass
    return v.strip()


def _is_tel_key(key: str) -> bool:
    k = (key or "").upper()
    return k == "TEL" or k.endswith(".TEL") or k.endswith(":TEL")


def _parse_vcf(text: str) -> list[dict[str, Any]]:
    text = _unfold_vcf(text.replace("\r\n", "\n").replace("\r", "\n"))
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        up = line.upper()
        if up == "BEGIN:VCARD":
            current = {"names": [], "phones": []}
            continue
        if up == "END:VCARD":
            if current and current["phones"]:
                names = _with_first_tokens([n for n in current["names"] if n])
                if names:
                    rows.append(
                        {
                            "name": names[0],
                            "aliases": names,
                            "phones": current["phones"],
                        }
                    )
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, _, right = line.partition(":")
        key = left.split(";", 1)[0].upper()
        params = left.upper()
        if "PHOTO" in key:
            continue
        val = _decode_vcf_value(right, params)
        if not val:
            continue
        if key == "FN":
            current["names"].insert(0, val)
        elif key == "N":
            parts = [p.strip() for p in val.split(";") if p.strip()]
            joined = " ".join(parts)
            if joined:
                current["names"].append(joined)
        elif key in ("NICKNAME", "X-NICKNAME"):
            current["names"].append(val)
        elif _is_tel_key(key):
            is_cell = any(t in params for t in ("CELL", "MOBILE", "PREF"))
            current["phones"].append({"raw": val, "prefer": is_cell})
    return rows


def _parse_csv(text: str) -> list[dict[str, Any]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []
    fields = [str(f or "").strip() for f in reader.fieldnames]
    name_cols = [f for f in fields if _NAME_CSV_RE.match(f)]
    given_cols = [f for f in fields if _GIVEN_CSV_RE.match(f)]
    family_cols = [f for f in fields if _FAMILY_CSV_RE.match(f)]
    nick_cols = [f for f in fields if _NICK_CSV_RE.match(f)]
    phone_cols = [
        f for f in fields
        if _PHONE_CSV_RE.match(f)
        or (
            any(x in f.lower() for x in ("phone", "mobile", "cell", "tel"))
            and "type" not in f.lower()
            and "email" not in f.lower()
        )
    ]
    type_map: dict[str, str] = {}
    for f in fields:
        if _TYPE_CSV_RE.match(f):
            # "Phone 1 - Type" pairs with "Phone 1 - Value"
            num = re.search(r"\d+", f)
            if num:
                type_map[num.group(0)] = f

    rows: list[dict[str, Any]] = []
    for rec in reader:
        names: list[str] = []
        for col in name_cols:
            v = str(rec.get(col) or "").strip()
            if v:
                names.append(v)
        given = " ".join(str(rec.get(c) or "").strip() for c in given_cols).strip()
        family = " ".join(str(rec.get(c) or "").strip() for c in family_cols).strip()
        combo = f"{given} {family}".strip()
        if combo:
            names.append(combo)
        for col in nick_cols:
            v = str(rec.get(col) or "").strip()
            if v:
                names.append(v)
        names = _with_first_tokens([n for n in names if n])
        phones: list[dict[str, Any]] = []
        for col in phone_cols:
            v = str(rec.get(col) or "").strip()
            if not v:
                continue
            prefer = False
            num = re.search(r"\d+", col)
            if num and type_map.get(num.group(0)):
                t = str(rec.get(type_map[num.group(0)]) or "").lower()
                prefer = any(x in t for x in ("mobile", "cell", "pref"))
            elif "mobile" in col.lower() or "cell" in col.lower():
                prefer = True
            phones.append({"raw": v, "prefer": prefer})
        if names and phones:
            rows.append({"name": names[0], "aliases": names, "phones": phones})
    return rows


def _file_stamp(folder: Path) -> tuple:
    if not folder.is_dir():
        return ()
    items = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in (".vcf", ".csv") and p.is_file():
            try:
                st = p.stat()
                items.append((p.name, st.st_mtime, st.st_size))
            except OSError:
                continue
    return tuple(items)


def _decode_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _load_rows() -> list[dict[str, Any]]:
    global _cache, _cache_stamp
    folder = contacts_dir()
    stamp = _file_stamp(folder)
    if _cache is not None and stamp == _cache_stamp:
        return _cache
    rows: list[dict[str, Any]] = []
    if folder.is_dir():
        vcf_files = sorted(folder.glob("*.vcf"))
        vcf_files.sort(key=lambda p: (p.name.lower() != "contacts.vcf", p.name.lower()))
        for path in vcf_files:
            try:
                text = _decode_bytes(path.read_bytes())
            except Exception:
                continue
            parsed = _parse_vcf(text)
            for r in parsed:
                r["source"] = path.name
            rows.extend(parsed)
            print(f"[Contacts] loaded {len(parsed)} from {path.name}")
        for path in sorted(folder.glob("*.csv")):
            try:
                text = _decode_bytes(path.read_bytes())
            except Exception:
                continue
            parsed = _parse_csv(text)
            for r in parsed:
                r["source"] = path.name
            rows.extend(parsed)
            print(f"[Contacts] loaded {len(parsed)} from {path.name}")
    _cache = rows
    _cache_stamp = stamp
    return rows


def _pick_phone(phones: list[dict[str, Any]], cc: str) -> str:
    preferred = [p for p in phones if p.get("prefer")]
    ordered = preferred + [p for p in phones if not p.get("prefer")]
    seen: set[str] = set()
    for p in ordered:
        digits = normalize_phone(str(p.get("raw") or ""), cc)
        if digits and digits not in seen:
            return digits
        if digits:
            seen.add(digits)
    return ""


def _score(query: str, name: str) -> float:
    q = _norm_text(query)
    n = _norm_text(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.92
    qt = set(q.split())
    nt = set(n.split())
    if qt and qt <= nt:
        return 0.88
    if qt and nt and qt & nt:
        overlap = len(qt & nt) / max(len(qt), 1)
        if overlap >= 0.5:
            return 0.62 + 0.25 * overlap
    return SequenceMatcher(None, q, n).ratio()


def lookup(name: str) -> dict[str, Any] | None:
    """
    Match a spoken name against the Contacts folder.

    Returns:
      {"ok": True, "jid", "name", "digits"} on a unique hit
      {"ok": False, "error": "..."} when several names are too close
      None when nothing matches
    """
    query = (name or "").strip()
    if not query:
        return None
    # Phone-like input is handled by the bridge, not the book
    digits_only = re.sub(r"\D", "", query)
    if digits_only == re.sub(r"[\s+\-()]", "", query) and 8 <= len(digits_only) <= 15:
        return None

    rows = _load_rows()
    if not rows:
        return None

    cc = _default_country_code()
    scored: list[tuple[float, dict[str, Any], str]] = []
    for row in rows:
        best = 0.0
        for alias in row.get("aliases") or [row.get("name") or ""]:
            best = max(best, _score(query, str(alias)))
        if best < _THRESHOLD:
            continue
        phone = _pick_phone(row.get("phones") or [], cc)
        if not phone:
            continue
        scored.append((best, row, phone))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0], reverse=True)
    exact = [s for s in scored if s[0] >= 0.999]
    if exact:
        pool = exact
    else:
        top_score = scored[0][0]
        pool = [s for s in scored if top_score - s[0] <= _AMBIGUOUS_GAP]

    unique_phones = {s[2] for s in pool}
    if len(unique_phones) > 1 and len(pool) > 1:
        labels = []
        seen_n: set[str] = set()
        for _sc, row, phone in pool[:6]:
            label = str(row.get("name") or phone)
            tail = phone[-4:] if len(phone) >= 4 else phone
            shown = f"{label} (...{tail})"
            key = shown.lower()
            if key in seen_n:
                continue
            seen_n.add(key)
            labels.append(shown)
        listing = ", ".join(labels)
        return {
            "ok": False,
            "error": (
                f"Several contacts match '{query}': {listing}. "
                "Say the full saved name."
            ),
        }

    top_score, top_row, top_phone = pool[0]

    display = str(top_row.get("name") or query)
    jid = phone_to_jid(top_phone)
    if not jid:
        return None
    return {
        "ok": True,
        "jid": jid,
        "name": display,
        "digits": top_phone,
        "score": top_score,
        "source": "contacts_book",
        "isGroup": False,
    }


def contacts_index_path() -> Path:
    d = _base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "contacts_index.json"


def _digit_variants(digits: str) -> list[str]:
    d = re.sub(r"\D", "", digits or "")
    out: list[str] = []
    for v in (d, normalize_phone(d) or ""):
        if v and v not in out:
            out.append(v)
    cc = _default_country_code()
    if d.startswith("0") and len(d) >= 10:
        alt = cc + d.lstrip("0")
        if alt not in out:
            out.append(alt)
    if d.startswith(cc) and len(d) > len(cc) + 6:
        local = "0" + d[len(cc):]
        if local not in out:
            out.append(local)
    if len(d) >= 10 and d[-10:] not in out:
        out.append(d[-10:])
    return out


def _rebuild_maps(rows: list[dict[str, Any]]) -> None:
    global _by_digits, _by_lid, _by_jid
    _by_digits = {}
    _by_lid = {}
    _by_jid = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        digits = str(row.get("digits") or "").strip()
        jid = str(row.get("jid") or "").strip().lower()
        lid = str(row.get("lid") or "").strip().lower()
        for v in _digit_variants(digits):
            _by_digits.setdefault(v, name)
        if jid:
            _by_jid.setdefault(jid, name)
        if lid.endswith("@lid"):
            _by_lid.setdefault(lid, name)


def _save_index(rows: list[dict[str, Any]]) -> None:
    try:
        contacts_index_path().write_text(
            json.dumps({"rows": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[Contacts] could not save index: {e}")


def _load_saved_lids() -> dict[str, str]:
    path = contacts_index_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("rows") if isinstance(raw, dict) else raw
        out: dict[str, str] = {}
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            lid = str(row.get("lid") or "").strip().lower()
            digits = str(row.get("digits") or "").strip()
            if lid.endswith("@lid") and digits:
                out[lid] = digits
                out[digits] = lid
        return out
    except Exception:
        return {}


def _rows_from_book() -> list[dict[str, Any]]:
    cc = _default_country_code()
    lidm = dict(_load_lid_map())
    lidm.update(_load_saved_lids())
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _load_rows():
        name = str(row.get("name") or "").strip()
        aliases = list(row.get("aliases") or [name])
        source = str(row.get("source") or "")
        for p in row.get("phones") or []:
            digits = normalize_phone(str(p.get("raw") or ""), cc)
            if not digits:
                continue
            key = (name.lower(), digits)
            if key in seen:
                continue
            seen.add(key)
            jid = phone_to_jid(digits)
            lid = str(lidm.get(digits) or lidm.get(jid) or "")
            if lid and not lid.endswith("@lid"):
                lid = ""
            out.append({
                "name": name,
                "aliases": aliases,
                "digits": digits,
                "jid": jid,
                "lid": lid.lower() if lid else "",
                "source": source,
            })
    return out


def _ensure_index() -> list[dict[str, Any]]:
    global _index_rows, _index_stamp
    stamp = _file_stamp(contacts_dir())
    with _index_lock:
        if _index_rows is not None and stamp == _index_stamp:
            return _index_rows
        rows = _rows_from_book()
        _index_rows = rows
        _index_stamp = stamp
        _rebuild_maps(rows)
        _save_index(rows)
        return rows


def get_contacts_frame():
    """In-memory table of the phone book (pandas DataFrame if installed)."""
    rows = _ensure_index()
    try:
        import pandas as pd
        return pd.DataFrame(rows)
    except Exception:
        return rows


def _phone_index() -> dict[str, str]:
    """Normalized phone digits → saved contact name."""
    _ensure_index()
    return dict(_by_digits)


def lid_map_path() -> Path:
    d = _base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "whatsapp_lid_map.json"


def _load_lid_map() -> dict[str, str]:
    global _lid_map
    if _lid_map is not None:
        return _lid_map
    path = lid_map_path()
    data: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): str(v) for k, v in raw.items() if k and v}
        except Exception:
            data = {}
    _lid_map = data
    return _lid_map


def remember_lid_mapping(lid: str = "", phone_jid: str = "") -> None:
    """Persist LID ↔ phone digits so inbound @lid chats hit the VCF."""
    a = (lid or "").strip()
    b = (phone_jid or "").strip()
    lid_key = ""
    digits = ""
    for val in (a, b):
        if val.endswith("@lid") and not lid_key:
            lid_key = val
        else:
            d = jid_to_digits(val)
            if d:
                digits = d
    if not lid_key or not digits:
        return
    with _lid_lock:
        m = _load_lid_map()
        if m.get(lid_key) == digits and m.get(digits) == lid_key:
            return
        m[lid_key] = digits
        m[digits] = lid_key
        try:
            lid_map_path().write_text(json.dumps(m, indent=2), encoding="utf-8")
        except Exception:
            pass
        _lid_map = m
        _index_apply_lid(lid_key, digits)


def _index_apply_lid(lid: str, digits: str) -> None:
    global _index_rows
    _ensure_index()
    lid_k = lid.strip().lower()
    name = ""
    with _index_lock:
        rows = list(_index_rows or [])
        for row in rows:
            if str(row.get("digits") or "") == digits or str(row.get("jid") or "").lower() == phone_to_jid(digits).lower():
                row["lid"] = lid_k
                name = str(row.get("name") or name)
        if name:
            _by_lid[lid_k] = name
            for v in _digit_variants(digits):
                _by_digits.setdefault(v, name)
            _index_rows = rows
            _save_index(rows)


def phone_for_lid(lid: str) -> str:
    key = (lid or "").strip()
    if not key:
        return ""
    m = _load_lid_map()
    val = m.get(key) or ""
    if not val and not key.endswith("@lid"):
        val = m.get(f"{key}@lid") or ""
    if val.endswith("@lid") or val.endswith("@g.us"):
        return ""
    digits = re.sub(r"\D", "", val.split("@")[0] if "@" in val else val)
    return digits if 8 <= len(digits) <= 15 else ""


def display_for_jid(jid: str, fallback: str = "") -> str:
    """Saved Contacts folder name for this chat JID, else fallback."""
    raw = (jid or "").strip()
    fb = (fallback or "").strip()
    if not raw:
        return fb
    if raw.endswith("@g.us"):
        return fb or raw
    _ensure_index()
    low = raw.lower()
    if low.endswith("@lid"):
        name = _by_lid.get(low)
        if name:
            return name
        digits = phone_for_lid(raw)
        if digits:
            name = _by_digits.get(digits) or _by_digits.get(normalize_phone(digits) or "")
            if name:
                return name
            for v in _digit_variants(digits):
                if _by_digits.get(v):
                    return _by_digits[v]
        return fb
    name = _by_jid.get(low)
    if name:
        return name
    digits = jid_to_digits(raw)
    if digits:
        for v in _digit_variants(digits):
            hit = _by_digits.get(v)
            if hit:
                return hit
    return fb
